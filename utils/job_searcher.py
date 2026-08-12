"""
Job Searcher
Uses Selenium + Chrome for LinkedIn job search. The main flow processes **one listing at a time**:
open card → parse details → append to a log file → run your callback (score, cover letter, Easy Apply)
without navigating away in a second browser session.

Default is a visible Chrome window. Use --headless for no UI.

Session cookies: data/selenium_linkedin_cookies.json
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from selenium.common.exceptions import NoSuchElementException, WebDriverException

# Sentinel for "unlimited" quota math (last page, no max_jobs cap).
_MAX_QUOTA = 2**30

# LinkedIn ``f_TPR`` windows (seconds since post). Primary search uses 24h; company scans use 7d.
_F_TPR_PAST_24H = "r86400"
_F_TPR_PAST_WEEK = "r604800"  # 7 * 86400

# Newer LinkedIn jobs list: clickable cards (not ``<a href="/jobs/view/…">``).
_JOB_CARD_BUTTON_CSS = 'div[role="button"][componentkey^="job-card-component-ref-"]'
_JOB_CARD_REF_RE = re.compile(r"job-card-component-ref-(\d+)", re.IGNORECASE)
from selenium.webdriver.common.by import By

from .chrome_driver import (
    DEFAULT_COOKIE_PATH,
    build_chrome,
    driver_session_alive,
    focus_element,
    interruptible_sleep,
    load_cookies,
    log_driver_session_closed,
    quit_chrome,
    save_cookies,
    scroll_into_view,
)
from .job_records import append_listing_record

log = logging.getLogger(__name__)


class StopApplyPipeline(Exception):
    """
    Raised from a ``process_listing`` callback to stop :meth:`JobSearcher.run_search_apply_pipeline`
    cleanly (no more listings/pages/keywords), e.g. when LinkedIn's daily application limit is hit.
    """


# After ``driver.get`` on ``https://www.linkedin.com/login``, wait so a delayed auto-login / device-trust
# redirect can complete before we interact with the form.
LINKEDIN_LOGIN_PAGE_POST_NAV_DELAY_S = 10.0

# After the first ``driver.get`` on ``https://www.linkedin.com/feed/``, wait so cookie-driven redirects
# (e.g. delayed / device sign-in) can finish before we navigate to ``/login`` again.
LINKEDIN_FEED_FIRST_NAV_DELAY_S = 5.0

# Used when ``--keywords`` is omitted (LinkedIn pipeline and Greenhouse MyGreenhouse multi-search).
DEFAULT_JOB_SEARCH_KEYWORDS: tuple[str, ...] = (
    "software developer",
    "software engineer",
    "data scientist",
    "data analyst",
)

_MIN_FIRST_NAME_LEN = 2


def normalize_search_keywords(keywords: str | Sequence[str]) -> list[str]:
    """Strip and drop empties; ``str`` is treated as a single query."""
    if isinstance(keywords, str):
        parts = [keywords]
    else:
        parts = list(keywords)
    out = [p.strip() for p in parts if (p or "").strip()]
    if not out:
        raise ValueError("At least one non-empty keyword is required")
    return out


def job_id_and_view_url_from_href(href: str) -> tuple[str, str]:
    """
    LinkedIn list links often use ``jobs/search-results/?currentJobId=...``; older links use ``/jobs/view/ID/``.
    Returns (job_id, preferred_url_for_opening_job_page).
    """
    if not href:
        return "", ""
    parsed = urllib.parse.urlparse(href)
    q = urllib.parse.parse_qs(parsed.query)
    if "currentJobId" in q and q["currentJobId"]:
        jid = q["currentJobId"][0].strip()
        view = f"https://www.linkedin.com/jobs/view/{jid}/"
        return jid, view
    m = re.search(r"/jobs/view/(\d+)", href)
    if m:
        jid = m.group(1)
        return jid, f"https://www.linkedin.com/jobs/view/{jid}/"
    return href, href


def _linkedin_url_blocks_logged_in_session(url: str) -> bool:
    """True when the path is clearly a login / challenge flow (not the feed home)."""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower().rstrip("/") or "/"
    except Exception:
        return True
    barriers = (
        "/login",
        "/uas/login",
        "/checkpoint",
        "/challenge",
        "/authwall",
        "/signup",
        "/start",
    )
    if path in barriers or any(path.startswith(b + "/") for b in barriers):
        return True
    if "/uas/" in path:
        return True
    return False


def _linkedin_url_is_feed_home(url: str) -> bool:
    """True only when the URL path is /feed (not trk=feed or redirect=...feed... on /login)."""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower().rstrip("/") or "/"
    except Exception:
        return False
    return path == "/feed" or path.startswith("/feed/")


def _linkedin_url_is_authenticated_jobs_area(url: str) -> bool:
    """True when already on the logged-in Jobs experience (delayed auth sometimes lands here, not /feed)."""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except Exception:
        return False
    if not path.startswith("/jobs"):
        return False
    if "guest" in path or "authwall" in path:
        return False
    return True


def _linkedin_url_on_credential_or_device_flow(url: str) -> bool:
    """
    True when the browser is already on LinkedIn sign-in or device-trust / checkpoint flow
    (common right after loading ``/feed/`` with stale cookies). Not feed or logged-in jobs home.
    """
    if _linkedin_url_is_feed_home(url) or _linkedin_url_is_authenticated_jobs_area(url):
        return False
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except Exception:
        return False
    p = path.rstrip("/") or "/"
    if p == "/login" or p.startswith("/login/"):
        return True
    if "checkpoint" in path or "challenge" in path:
        return True
    if "/uas/" in path:
        return True
    if "authwall" in path:
        return True
    return False


def _find_login_element(driver, css: str, *, attempts: int = 12, delay_s: float = 0.5):
    """Wait for LinkedIn login DOM (slow loads or markup changes)."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return driver.find_element(By.CSS_SELECTOR, css)
        except NoSuchElementException as e:
            last = e
            time.sleep(delay_s)
    assert last is not None
    raise last


def _page_contains_first_name(driver, first_name: str) -> bool:
    fn = (first_name or "").strip()
    if len(fn) < _MIN_FIRST_NAME_LEN:
        return False
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False
    return bool(re.search(r"\b" + re.escape(fn) + r"\b", body, re.IGNORECASE))


SEL = {
    "email_input": 'input[name="session_key"]',
    "password_input": 'input[name="session_password"]',
    "sign_in_btn": 'button[type="submit"]',
    # Remembered account on login: ``aria-label="Login as First Last"``.
    "saved_account_login": 'button[aria-label^="Login as "]',
    # ``Welcome Back`` + saved profile (cookies weak but browser remembers user).
    "welcome_back_heading": "h1.header__content__heading",
    # Job search list: prefer new ``div[role=button][componentkey^=job-card-component-ref-]`` cards;
    # legacy: ``li.scaffold-layout__list-item[data-occludable-job-id]`` with ``/jobs/view/`` links.
    "job_card_title": '[class*="job-card-job-posting-card-wrapper__title"]',
    "job_card_company": '[class*="job-card-job-posting-card-wrapper__company-name"]',
    "job_card_company_alt": '[class*="job-card-job-posting-card-wrapper__primary-description"]',
    # Two-pane list: company / location sit in the lockup, not inside the title ``<a>``.
    "job_card_company_row": ".artdeco-entity-lockup__subtitle span",
    "job_card_location": '[class*="job-card-job-posting-card-wrapper__location"]',
    "job_card_location_row": ".artdeco-entity-lockup__caption .job-card-container__metadata-wrapper li span",
    "job_card_button": _JOB_CARD_BUTTON_CSS,
    # Newer SDUI markup uses hashed/atomic class names (no stable class to hook), so prefer the
    # stable data-testid / componentkey attributes; ``.jobs-description__content`` is legacy fallback.
    # ``expandable-text-box`` is a *sibling* of the "…more" toggle button, not its ancestor, so
    # selecting it directly avoids picking up that button's text.
    "job_description": (
        'span[data-testid="expandable-text-box"]',
        'div[componentkey^="JobDetails_AboutTheJob_"]',
        ".jobs-description__content",
    ),
    # "…more" toggle that visually clamps the description text above; click before reading.
    "job_description_more_button": 'button[data-testid="expandable-text-button"]',
    # LinkedIn SDUI: data-testid; legacy: aria-label on artdeco pagination.
    "next_page": (
        'button[data-testid="pagination-controls-next-button-visible"], '
        'button[aria-label="View next page"], '
        'button[aria-label*="next page"]'
    ),
    # Company page — Jobs tab / See all jobs (filter-mode secondary scan).
    "company_jobs_tab": (
        "a.org-page-navigation__item[href*='/jobs'], "
        "a[href*='/company/'][href*='/jobs'][data-control-name*='jobs'], "
        "nav a[href*='/jobs']"
    ),
    "company_show_all_jobs": (
        "a[href*='/jobs/search'][href*='f_C='], "
        "a.jobs-search-box__submit-button, "
        "a[href*='f_C=']"
    ),
    # Detail pane — Save / Unsave job (filter mode).
    "job_save_btn": (
        'button[aria-label^="Save "][aria-label*="job"], '
        'button[aria-label*="Save job"], '
        "button.jobs-save-button, "
        ".jobs-save-button, "
        ".job-details-jobs-unified-top-card button[aria-label*='Save'], "
        ".jobs-details-top-card__actions button[aria-label*='Save']"
    ),
    "job_unsave_btn": (
        'button[aria-label^="Unsave "][aria-label*="job"], '
        'button[aria-label*="Unsave job"], '
        "button.jobs-save-button[aria-pressed='true']"
    ),
    # Job search filter bar — Easy Apply and All filters (LinkedIn drops f_AL mid-session sometimes).
    "all_filters_btn": (
        'button[data-control-name="all_filters"], '
        'button[aria-label*="Show all filters"], '
        'button[aria-label*="All filters"], '
        "button.search-reusables__all-filters-pill"
    ),
    "easy_apply_filter_pill": (
        'button[aria-label="Easy Apply filter."], '
        'button[aria-label*="Easy Apply filter"]'
    ),
    # Newer LinkedIn filter bar: radio/checkbox chip (not a pill / not All-filters modal).
    "easy_apply_filter_radio": (
        'div[role="radio"][aria-label="Filter by Easy Apply"], '
        'div[role="radio"][aria-label*="Easy Apply"], '
        'div[role="checkbox"][aria-label="Filter by Easy Apply"], '
        'div[role="checkbox"][aria-label*="Easy Apply"]'
    ),
    "easy_apply_filter_modal_toggle": ".search-reusables__advanced-filters-binary-toggle",
    "easy_apply_filter_modal_label": "label[for='f_LF-f_AL']",
    "easy_apply_filter_modal_input": "input#f_LF-f_AL",
    "all_filters_apply_btn": (
        'button[data-test-reusables-filters-modal-show-results-button], '
        'button[data-test-reusables-filters-modal-show-results-button="true"], '
        "button.search-reusables__secondary-filters-show-results-button, "
        "button.reusable-search-filters-buttons.search-reusables__secondary-filters-show-results-button, "
        'button[data-control-name="all_filters_apply"], '
        'button[aria-label*="Apply current filters"]'
    ),
}


class JobSearcher:
    def __init__(
        self,
        headless: bool = False,
        session_file: Path | str = DEFAULT_COOKIE_PATH,
        step_delay: float = 0.35,
        highlight: bool = True,
        pause_after_navigate: bool = False,
        account_first_name: str | None = None,
        job_cards_wait_seconds: float = 10.0,
        login_form_wait_seconds: float = 5.0,
        next_page_wait_seconds: float = 3.0,
        job_description_wait_seconds: float = 3.0,
        login_complete_max_seconds: float = 120.0,
        login_complete_max_seconds_no_checkpoint: float = 30.0,
        job_list_scroll_max_rounds: int = 120,
        job_list_scroll_pause: float = 0.4,
        job_list_scroll_stable_rounds: int = 7,
        job_list_tail_pass_rounds: int = 12,
        jobs_per_results_page: int = 25,
        posted_within_24h: bool = True,
        auto: bool = False,
    ):
        self.headless = headless
        self.session_file = Path(session_file)
        self.step_delay = step_delay
        self.highlight = highlight and not headless
        self.pause_after_navigate = pause_after_navigate
        self.account_first_name = (account_first_name or "").strip() or None
        self.job_cards_wait_seconds = max(0.0, float(job_cards_wait_seconds))
        self.login_form_wait_seconds = max(0.0, float(login_form_wait_seconds))
        self.next_page_wait_seconds = max(0.0, float(next_page_wait_seconds))
        self.job_description_wait_seconds = max(0.0, float(job_description_wait_seconds))
        self.login_complete_max_seconds = max(1.0, float(login_complete_max_seconds))
        self.login_complete_max_seconds_no_checkpoint = max(
            1.0, float(login_complete_max_seconds_no_checkpoint)
        )
        self.job_list_scroll_max_rounds = max(1, int(job_list_scroll_max_rounds))
        self.job_list_scroll_pause = max(0.05, float(job_list_scroll_pause))
        self.job_list_scroll_stable_rounds = max(3, int(job_list_scroll_stable_rounds))
        self.job_list_tail_pass_rounds = max(4, int(job_list_tail_pass_rounds))
        # LinkedIn typically shows 25 jobs per search page when another page exists.
        self.jobs_per_results_page = max(1, int(jobs_per_results_page))
        # Same as UI "Date posted → Past 24 hours" (seconds since post).
        self.posted_within_24h = bool(posted_within_24h)
        self.auto = bool(auto)
        # Times LinkedIn dropped the Easy Apply filter and we re-enabled it via the filter UI.
        self.easy_apply_filter_recoveries = 0

    def _jobs_search_query(self, keywords: str, location: str, easy_apply_only: bool) -> str:
        params: dict[str, str] = {"keywords": keywords, "location": location}
        if easy_apply_only:
            params["f_LF"] = "f_AL"
        if self.posted_within_24h:
            params["f_TPR"] = _F_TPR_PAST_24H
        return urllib.parse.urlencode(params)

    def _pause(self) -> None:
        if self.step_delay > 0:
            time.sleep(self.step_delay)

    def _remaining_slots(self, processed: int, max_jobs: int | None) -> int:
        """Slots left under ``max_jobs``; if ``max_jobs`` is None, return a large int for quota math."""
        if max_jobs is None:
            return _MAX_QUOTA
        return max(0, max_jobs - processed)

    def search(
        self,
        keywords: str,
        location: str,
        max_jobs: int | None = None,
        easy_apply_only: bool = True,
    ) -> list[dict]:
        """Only used for ``--debug-jobs-page`` (opens search, waits for Enter)."""
        if not self.pause_after_navigate:
            raise RuntimeError("Use run_search_apply_pipeline for full runs")

        driver = build_chrome(headless=self.headless)
        try:
            load_cookies(driver, self.session_file)
            self._login(driver)

            query = self._jobs_search_query(keywords, location, easy_apply_only)
            url = f"https://www.linkedin.com/jobs/search/?{query}"

            log.info("Navigating to: %s", url)
            driver.get(url)
            self._pause()
            time.sleep(1.2)

            log.info(
                "Debug: job search page is open — inspect the browser. "
                "Press Enter in this terminal to close Chrome and continue."
            )
            input()
            save_cookies(driver, self.session_file)
            return []
        finally:
            quit_chrome(driver)

    def run_search_apply_pipeline(
        self,
        keywords: str | Sequence[str],
        location: str,
        max_listings: int | None,
        easy_apply_only: bool,
        listings_log_path: Path | str,
        process_listing: Callable[[Any, dict], None],
        *,
        max_applies: int | None = None,
        apply_counter: dict[str, int] | None = None,
        maybe_skip_from_list_card: Callable[[Any, dict], bool] | None = None,
        seen_job_ids: set[str] | None = None,
        seen_job_ids_lock: threading.Lock | None = None,
    ) -> int:
        """
        One browser session: for each search result, click the card, parse job fields, append a row to
        ``listings_log_path``, then call ``process_listing(driver, job)``.

        If ``maybe_skip_from_list_card`` is set, it is called with ``(driver, peek_job)`` after reading each
        list card **without** opening the job. When it returns True, the callback has fully handled the card
        (e.g. dismiss + tracker); the pipeline skips opening the detail pane and does not call
        ``process_listing``. Use this for blacklist / consulting memory / listing-only heuristics.

        ``keywords`` may be a single string or a sequence of queries. After one query runs out of result
        pages (no Next), the next query is loaded in the same session until ``max_listings`` is reached,
        ``max_applies`` successful applies is reached (when set with ``apply_counter``), or every query is
        exhausted. Job IDs are deduplicated across the whole session.

        If ``max_listings`` is ``None``, there is no listing cap: the run continues until there are no more
        result pages, the apply cap is met, or the list is exhausted. Otherwise at most that many listings
        are processed (each counts once toward ``processed``).

        If ``max_applies`` is set and ``apply_counter`` is provided (e.g. ``{"applied": N}`` mutated by the
        caller on each successful apply), the run stops as soon as ``applied >= max_applies``, even when
        ``max_listings`` is not reached.

        ``seen_job_ids`` (optional) is a shared set of job IDs already handled this session. When provided
        with ``seen_job_ids_lock``, the pipeline claims IDs under that lock so a secondary company-jobs
        scanner can skip the same postings.

        If ``easy_apply_only`` is true, the search URL includes LinkedIn's Easy Apply filter (``f_LF=f_AL``).
        If false, the search shows all jobs for the keywords/location.
        When ``JobSearcher`` was constructed with ``posted_within_24h=True`` (default), ``f_TPR=r86400``
        limits results to the past 24 hours (same as the Date posted → Past 24 hours filter).

        Pagination: when the Next control is available, we aim for ``jobs_per_results_page`` jobs (default 25)
        on that page before advancing — matching typical LinkedIn page size. Job IDs are deduplicated for
        the whole session so the same posting is not processed twice if the list shifts.
        """
        driver = build_chrome(headless=self.headless)
        listings_log_path = Path(listings_log_path)
        processed = 0
        if seen_job_ids is None:
            seen_job_ids = set()
        apply_goal_met = False
        driver_closed = False
        stop_requested = False
        kw_list = normalize_search_keywords(keywords)

        def _claim_job_id(jid: str) -> bool:
            """Return True if this id is newly claimed for this session; False if already seen."""
            if seen_job_ids_lock is not None:
                with seen_job_ids_lock:
                    if jid in seen_job_ids:
                        return False
                    seen_job_ids.add(jid)
                    return True
            if jid in seen_job_ids:
                return False
            seen_job_ids.add(jid)
            return True

        try:
            load_cookies(driver, self.session_file)
            self._login(driver)

            for kw_index, keyword in enumerate(kw_list):
                if driver_closed or stop_requested:
                    break
                if not driver_session_alive(driver):
                    log_driver_session_closed()
                    driver_closed = True
                    break

                if max_listings is not None and processed >= max_listings:
                    break

                log.info(
                    "Search keyword %d of %d: %r (%d listing(s) processed so far)",
                    kw_index + 1,
                    len(kw_list),
                    keyword,
                    processed,
                )

                query = self._jobs_search_query(keyword, location, easy_apply_only)
                url = f"https://www.linkedin.com/jobs/search/?{query}"

                log.info("Navigating to: %s", url)
                try:
                    driver.get(url)
                except WebDriverException:
                    log_driver_session_closed()
                    driver_closed = True
                    break
                self._pause()
                time.sleep(1.2)
                if easy_apply_only:
                    self.ensure_easy_apply_filter_on(driver)

                while max_listings is None or processed < max_listings:
                    if driver_closed:
                        break
                    if not driver_session_alive(driver):
                        log_driver_session_closed()
                        driver_closed = True
                        break

                    if not self._wait_job_list(driver):
                        driver_closed = True
                        break

                    has_next = self._has_next_page(driver)
                    remaining = self._remaining_slots(processed, max_listings)
                    # Full pages: LinkedIn usually shows ``jobs_per_results_page`` jobs when Next exists.
                    if has_next:
                        quota = min(self.jobs_per_results_page, remaining)
                    else:
                        quota = remaining

                    cap_msg = "no limit" if max_listings is None else str(max_listings)
                    log.info(
                        "Results page: has_next=%s, quota=%d job(s) on this page (%d already processed, cap %s)",
                        has_next,
                        quota,
                        processed,
                        cap_msg,
                    )

                    if has_next:
                        self._ensure_job_links_count(driver, quota)
                    else:
                        self._expand_virtualized_job_list(driver)
                    if self._driver_stopped(driver):
                        driver_closed = True
                        break
                    self._scroll_job_list_to_top(driver)
                    if self._driver_stopped(driver):
                        driver_closed = True
                        break

                    n = len(self._find_job_card_links(driver, expand=False))
                    log.info("Found %d job list link(s) in the DOM after loading", n)
                    if n == 0:
                        log.warning(
                            "No job list links found (tried /jobs/view/, job-card-container__link, "
                            "scaffold list + data-occludable-job-id, currentJobId). Scroll the left rail into view."
                        )

                    page_done = 0
                    i = 0
                    while page_done < quota and (
                        max_listings is None or processed < max_listings
                    ):
                        if driver_closed:
                            break
                        if not driver_session_alive(driver):
                            log_driver_session_closed()
                            driver_closed = True
                            break

                        links_now = self._find_job_card_links(driver, expand=False)
                        if i >= len(links_now):
                            if has_next:
                                self._ensure_job_links_count(driver, quota)
                                if self._driver_stopped(driver):
                                    driver_closed = True
                                    break
                                self._scroll_job_list_to_top(driver)
                                if self._driver_stopped(driver):
                                    driver_closed = True
                                    break
                                links_now = self._find_job_card_links(driver, expand=False)
                        if i >= len(links_now):
                            log.warning(
                                "Stopping this page at %d/%d job(s): only %d link(s) in the list (has_next=%s).",
                                page_done,
                                quota,
                                len(links_now),
                                has_next,
                            )
                            break

                        link = links_now[i]
                        peek = self._peek_job_from_list_link(link)
                        i += 1
                        if not peek:
                            log.debug("Skipping empty list-card peek at index %d", i - 1)
                            continue

                        jid = str(peek.get("id") or "").strip()
                        if not jid:
                            log.debug("Skipping job with no id at index %d", i - 1)
                            continue
                        if not _claim_job_id(jid):
                            log.info("Skipping duplicate job id %s (already processed this session)", jid)
                            continue

                        if maybe_skip_from_list_card is not None and maybe_skip_from_list_card(
                            driver, peek
                        ):
                            append_listing_record(
                                listings_log_path,
                                peek,
                                phase="skipped_list_card",
                                extra={"search_keyword": keyword},
                            )
                            log.info(
                                "Skipped from list card (no job pane opened) — %s at %s (log: %s)",
                                peek.get("title"),
                                peek.get("company"),
                                listings_log_path,
                            )
                            page_done += 1
                            processed += 1
                            if (
                                max_applies is not None
                                and apply_counter is not None
                                and apply_counter.get("applied", 0) >= max_applies
                            ):
                                apply_goal_met = True
                                log.info(
                                    "Reached successful apply cap (%d) — stopping search.",
                                    max_applies,
                                )
                                break
                            continue

                        job = self._complete_job_after_peek(driver, link, peek)
                        if not job:
                            log.debug("Skipping incomplete parse after opening job at index %d", i - 1)
                            continue

                        append_listing_record(
                            listings_log_path,
                            job,
                            phase="parsed",
                            extra={"search_keyword": keyword},
                        )
                        log.info(
                            "Recorded listing %s — %s at %s (log: %s)",
                            job.get("id"),
                            job.get("title"),
                            job.get("company"),
                            listings_log_path,
                        )

                        try:
                            process_listing(driver, job)
                        except StopApplyPipeline as e:
                            log.warning("Stopping search/apply pipeline early: %s", e)
                            stop_requested = True
                            if "browser" in str(e).lower():
                                driver_closed = True
                        except WebDriverException:
                            log_driver_session_closed()
                            driver_closed = True
                        except Exception:
                            log.exception(
                                "Pipeline error for %s at %s",
                                job.get("title"),
                                job.get("company"),
                            )

                        page_done += 1
                        processed += 1
                        if driver_closed or stop_requested:
                            break
                        if (
                            max_applies is not None
                            and apply_counter is not None
                            and apply_counter.get("applied", 0) >= max_applies
                        ):
                            apply_goal_met = True
                            log.info(
                                "Reached successful apply cap (%d) — stopping search.",
                                max_applies,
                            )
                            break

                    if apply_goal_met or driver_closed or stop_requested:
                        break

                    if max_listings is not None and processed >= max_listings:
                        break

                    if not has_next:
                        log.info(
                            "No Next page control (or disabled) — end of results for keyword %r.",
                            keyword,
                        )
                        break

                    if page_done < quota:
                        log.warning(
                            "Expected %d job(s) on this page before Next, but only processed %d — "
                            "continuing to next page anyway (virtual list may have fewer mounted links).",
                            quota,
                            page_done,
                        )

                    if not driver_session_alive(driver):
                        log_driver_session_closed()
                        driver_closed = True
                        break

                    time.sleep(self.next_page_wait_seconds)
                    if self._driver_stopped(driver):
                        driver_closed = True
                        break
                    try:
                        next_els = driver.find_elements(By.CSS_SELECTOR, SEL["next_page"])
                    except WebDriverException:
                        log_driver_session_closed()
                        driver_closed = True
                        break
                    if not next_els:
                        log.info("No further results pages")
                        break
                    next_btn = next_els[0]
                    if self.highlight:
                        focus_element(driver, next_btn, pause=self.step_delay)
                    try:
                        next_btn.click()
                    except WebDriverException:
                        log_driver_session_closed()
                        driver_closed = True
                        break
                    self._pause()
                    time.sleep(1.2)

                if apply_goal_met or driver_closed:
                    break

            if not driver_closed:
                save_cookies(driver, self.session_file)
            return processed
        except KeyboardInterrupt:
            log.info("Run interrupted — saving progress and shutting down.")
            if not driver_closed:
                try:
                    save_cookies(driver, self.session_file)
                except Exception:
                    pass
            raise StopApplyPipeline("interrupted by user")
        except WebDriverException:
            log_driver_session_closed()
            return processed
        finally:
            quit_chrome(driver)

    def _driver_stopped(self, driver) -> bool:
        """True when the browser session is gone — list scroll / pagination should stop."""
        if driver_session_alive(driver):
            return False
        log_driver_session_closed()
        return True

    def _wait_job_list(self, driver) -> bool:
        """Wait for the job list to render. Returns False if the driver session ended."""
        if self.job_cards_wait_seconds <= 0:
            return not self._driver_stopped(driver)
        log.info(
            "Waiting %.1fs for job list to render",
            self.job_cards_wait_seconds,
        )
        return interruptible_sleep(self.job_cards_wait_seconds, driver)

    def _left_rail_job_links(self, driver):
        """Primary selector: new role=button job cards, else legacy title links in list rows."""
        cards = driver.find_elements(By.CSS_SELECTOR, _JOB_CARD_BUTTON_CSS)
        if cards:
            return cards
        return driver.find_elements(
            By.CSS_SELECTOR,
            'li[data-occludable-job-id] a[href*="/jobs/view/"]',
        )

    def _count_left_rail_job_links(self, driver) -> int | None:
        """Return left-rail link count, or None when the browser session is no longer usable."""
        if self._driver_stopped(driver):
            return None
        try:
            return len(self._left_rail_job_links(driver))
        except WebDriverException:
            log_driver_session_closed()
            return None

    def _job_list_scroll_pick_js(self) -> str:
        """Returns JS that defines ``pick()`` → scrollable job-list element or null."""
        return """
            const pick = () => {
              const sels = [
                '.jobs-search-results-list',
                '[class*="jobs-search-results-list"]',
                '.scaffold-layout__list-container',
                'div[class*="scaffold-layout__list"]',
                'div[class*="jobs-search-two-pane__wrapper"]',
              ];
              for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.scrollHeight > el.clientHeight + 2) return el;
              }
              const ul = document.querySelector('ul.scaffold-layout__list');
              if (ul) {
                let p = ul.parentElement;
                for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
                  if (p.scrollHeight > p.clientHeight + 2) return p;
                }
              }
              return null;
            };
        """

    def _apply_job_list_scroll(self, driver, mode: str) -> bool:
        """
        ``mode``: ``top`` | ``bottom`` | ``page_down`` — scroll the left-rail list, not the window only.
        ``page_down`` nudges by ~one viewport so virtualization mounts rows in the middle, not only at the end.

        Returns False when the browser session is no longer usable.
        """
        if self._driver_stopped(driver):
            return False
        try:
            driver.execute_script(
            self._job_list_scroll_pick_js()
            + """
            const mode = arguments[0];
            const el = pick();
            if (!el) {
              if (mode === 'page_down') window.scrollBy(0, 720);
              else if (mode === 'bottom') window.scrollBy(0, 1200);
              return;
            }
            if (mode === 'top') el.scrollTop = 0;
            else if (mode === 'bottom') el.scrollTop = el.scrollHeight;
            else if (mode === 'page_down') {
              const step = Math.max(120, el.clientHeight * 0.88);
              el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + step);
            }
            """,
                mode,
            )
            return True
        except WebDriverException:
            log_driver_session_closed()
            return False

    def _scroll_job_list_to_top(self, driver) -> None:
        """After loading the list, scroll back to the first card so indices match top-to-bottom order."""
        if not self._apply_job_list_scroll(driver, "top"):
            return
        time.sleep(0.35)

    def _tail_load_job_list(self, driver) -> int | None:
        """
        After the count stops rising briefly, LinkedIn may still append rows (network / observers).
        Extra bottom + page-down passes; return new count if it grew, else None.
        """
        before = self._count_left_rail_job_links(driver)
        if before is None:
            return None
        best = before
        for _ in range(self.job_list_tail_pass_rounds):
            if self._driver_stopped(driver):
                return None
            if not self._apply_job_list_scroll(driver, "bottom"):
                return None
            if not self._apply_job_list_scroll(driver, "page_down"):
                return None
            time.sleep(self.job_list_scroll_pause * 1.25)
            cur = self._count_left_rail_job_links(driver)
            if cur is None:
                return None
            if cur > best:
                best = cur
        if best > before:
            log.info(
                "Virtual job list: tail pass increased count %d → %d",
                before,
                best,
            )
            return best
        return None

    def _expand_virtualized_job_list(self, driver) -> None:
        """
        LinkedIn's left rail uses occlusion: off-screen rows may lack a link until scrolled into view.

        We combine **jump to bottom** and **page-down** steps so we do not skip middle segments. We only
        stop after the count is stable for several rounds *and* a tail pass does not increase it — reducing
        false \"done\" when loading is bursty.
        """
        stable = 0
        last_n = -1
        zero_streak = 0
        for round_i in range(self.job_list_scroll_max_rounds):
            if self._driver_stopped(driver):
                return
            n = self._count_left_rail_job_links(driver)
            if n is None:
                return
            if n <= 0:
                zero_streak += 1
                # Empty left rail for many rounds almost always means wrong page chrome
                # (e.g. /jobs/search-results/) — abort instead of burning ~minute of scrolls.
                if zero_streak >= 12:
                    log.warning(
                        "Virtual job list: still 0 left-rail link(s) after %d scroll rounds — stopping expand",
                        round_i + 1,
                    )
                    return
            else:
                zero_streak = 0
            if n > 0 and n == last_n:
                stable += 1
                if stable >= self.job_list_scroll_stable_rounds:
                    grown = self._tail_load_job_list(driver)
                    if grown is None and not driver_session_alive(driver):
                        return
                    if grown is not None:
                        stable = 0
                        last_n = grown
                        continue
                    log.info(
                        "Virtual job list: done at %d left-rail link(s) (main + tail, %d rounds)",
                        n,
                        round_i + 1,
                    )
                    return
            else:
                stable = 0
            last_n = n
            if not self._apply_job_list_scroll(driver, "bottom"):
                return
            if not self._apply_job_list_scroll(driver, "page_down"):
                return
            time.sleep(self.job_list_scroll_pause)

        log.info(
            "Virtual job list: hit max %d scroll rounds (last count %d link(s))",
            self.job_list_scroll_max_rounds,
            last_n,
        )

    def _find_job_card_links(self, driver, *, expand: bool = True):
        """
        Return clickable job-card elements in the left rail.

        Prefer LinkedIn's newer ``div[role=button][componentkey^=job-card-component-ref-]`` cards.
        Fall back to legacy ``li[data-occludable-job-id] a[href*=/jobs/view/]`` rows.

        Intentionally does **not** use a bare ``a[href*=/jobs/view/]`` last resort — that matches
        detail-pane / related-job anchors and clicking them navigates off the search page.
        """
        if self._driver_stopped(driver):
            return []
        if expand:
            self._expand_virtualized_job_list(driver)
            if self._driver_stopped(driver):
                return []

        # 1) New Voyager job cards (role=button). Deduplicate by job id.
        try:
            raw_cards = driver.find_elements(By.CSS_SELECTOR, _JOB_CARD_BUTTON_CSS)
        except WebDriverException:
            log_driver_session_closed()
            return []
        if raw_cards:
            cards: list = []
            seen: set[str] = set()
            for el in raw_cards:
                try:
                    if not el.is_displayed():
                        continue
                except Exception:
                    continue
                jid = self._job_id_from_card_element(el)
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                cards.append(el)
            if cards:
                log.info(
                    "Matched %d job card(s) via css %r",
                    len(cards),
                    _JOB_CARD_BUTTON_CSS,
                )
                return cards

        # 2) Legacy scaffold list links.
        attempts: list[tuple[str, str]] = [
            ("css", 'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="/jobs/view/"]'),
            ("css", 'li[data-occludable-job-id] a[href*="/jobs/view/"]'),
            (
                "css",
                'li.scaffold-layout__list-item[data-occludable-job-id] a.job-card-container__link',
            ),
            ("css", 'li[data-occludable-job-id] a.job-card-container__link'),
            ("css", 'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="currentJobId"]'),
            (
                "css",
                'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="linkedin.com/jobs"]',
            ),
            ("css", 'li[data-occludable-job-id] a[href*="currentJobId"]'),
            ("css", 'li[data-occludable-job-id] a[href*="linkedin.com/jobs"]'),
            ("css", 'a[class*="job-card-job-posting-card-wrapper__card-link"]'),
        ]
        for kind, sel in attempts:
            try:
                if kind == "css":
                    links = driver.find_elements(By.CSS_SELECTOR, sel)
                else:
                    links = driver.find_elements(By.XPATH, sel)
            except WebDriverException:
                log_driver_session_closed()
                return []
            if links:
                log.info("Matched %d job list link(s) via %s %r", len(links), kind, sel)
                return links
        return []

    @staticmethod
    def _job_id_from_card_element(el) -> str:
        """Extract numeric job id from a card element (componentkey or data-occludable-job-id)."""
        try:
            ck = (el.get_attribute("componentkey") or "").strip()
        except Exception:
            ck = ""
        m = _JOB_CARD_REF_RE.search(ck)
        if m:
            return m.group(1)
        try:
            jid = (el.get_attribute("data-occludable-job-id") or "").strip()
        except Exception:
            jid = ""
        if jid.isdigit():
            return jid
        try:
            for xpath in (
                "./ancestor-or-self::*[@data-occludable-job-id][1]",
                "./ancestor-or-self::div[@role='button' and contains(@componentkey,'job-card-component-ref-')][1]",
            ):
                try:
                    anc = el.find_element(By.XPATH, xpath)
                except Exception:
                    continue
                ck2 = (anc.get_attribute("componentkey") or "").strip()
                m2 = _JOB_CARD_REF_RE.search(ck2)
                if m2:
                    return m2.group(1)
                j2 = (anc.get_attribute("data-occludable-job-id") or "").strip()
                if j2.isdigit():
                    return j2
        except Exception:
            pass
        return ""

    def _has_next_page(self, driver) -> bool:
        """True when the Next control exists and is actionable (more search results pages)."""
        try:
            els = driver.find_elements(By.CSS_SELECTOR, SEL["next_page"])
            if not els:
                return False
            btn = els[0]
            if not btn.is_displayed():
                return False
            if (btn.get_attribute("aria-disabled") or "").lower() == "true":
                return False
            if btn.get_attribute("disabled") is not None:
                return False
            cls = btn.get_attribute("class") or ""
            if "artdeco-button--disabled" in cls:
                return False
            return bool(btn.is_enabled())
        except Exception:
            log.debug("has_next_page: could not read Next button", exc_info=True)
            return False

    def _ensure_job_links_count(self, driver, min_count: int) -> int:
        """
        Re-run list expansion until at least ``min_count`` left-rail links are in the DOM (or no growth).

        When ``_has_next_page`` is true we expect about ``jobs_per_results_page`` jobs on this page;
        virtualization may require several passes before that many ``<a>`` nodes exist.
        """
        if min_count <= 0:
            n = self._count_left_rail_job_links(driver)
            return n if n is not None else 0
        prev = -1
        best = 0
        for attempt in range(10):
            if self._driver_stopped(driver):
                return best
            self._expand_virtualized_job_list(driver)
            if self._driver_stopped(driver):
                return best
            self._scroll_job_list_to_top(driver)
            if self._driver_stopped(driver):
                return best
            n = len(self._find_job_card_links(driver, expand=False))
            best = max(best, n)
            if n >= min_count:
                log.info(
                    "Job list: %d link(s) in DOM (>= %d requested) after load pass %d",
                    n,
                    min_count,
                    attempt + 1,
                )
                return n
            if n == prev and attempt >= 2:
                log.info(
                    "Job list: stuck at %d link(s) (wanted %d) after %d passes — continuing with what mounted",
                    n,
                    min_count,
                    attempt + 1,
                )
                return n
            prev = n
        log.info(
            "Job list: ending ensure with %d link(s) (best %d, wanted %d)",
            n,
            best,
            min_count,
        )
        return best

    def _text_from_first_match(self, root, selectors: tuple[str, ...]) -> str:
        for css in selectors:
            try:
                el = root.find_element(By.CSS_SELECTOR, css)
                t = (el.text or "").strip()
                if t:
                    return t
            except Exception:
                continue
        return ""

    def _list_item_for_link(self, link):
        """The job card root: new role=button card, else legacy ``li`` row."""
        for xpath in (
            "./ancestor-or-self::div[@role='button' and starts-with(@componentkey,'job-card-component-ref-')][1]",
            "./ancestor::li[contains(@class,'scaffold-layout__list-item')][1]",
            "./ancestor::li[@data-occludable-job-id][1]",
            "./ancestor::li[1]",
        ):
            try:
                return link.find_element(By.XPATH, xpath)
            except Exception:
                continue
        return link

    def _easy_apply_near_card_link(self, link) -> bool:
        """Detect Easy Apply from list row (classes or label text)."""
        try:
            row = self._list_item_for_link(link)
        except Exception:
            row = link
        try:
            if row.find_elements(By.CSS_SELECTOR, "[class*='easy-apply']"):
                return True
            if row.find_elements(By.CSS_SELECTOR, "[class*='EasyApply']"):
                return True
            return "easy apply" in (row.text or "").lower()
        except Exception:
            return False

    def _url_has_easy_apply_filter(self, driver) -> bool:
        """True when the current jobs search URL includes LinkedIn's Easy Apply filter param."""
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(driver.current_url).query)
        except Exception:
            return False
        for val in q.get("f_LF", []):
            if "f_AL" in (val or ""):
                return True
        for val in q.get("f_AL", []):
            if (val or "").lower() in ("true", "1"):
                return True
        return False

    def _find_visible_element(self, driver, css_selectors: tuple[str, ...] | list[str]):
        for css in css_selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, css)
            except WebDriverException:
                return None
            for el in matches:
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
        return None

    def _click_interactive_element(self, driver, el) -> bool:
        if el is None:
            return False
        try:
            if self.highlight:
                focus_element(driver, el, pause=self.step_delay)
            el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                log.debug("Click failed on filter control", exc_info=True)
                return False
        self._pause()
        return True

    def _easy_apply_filter_pill(self, driver):
        pill = self._find_visible_element(driver, (SEL["easy_apply_filter_pill"],))
        if pill is not None:
            return pill
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//div[contains(@class,'search-reusables')]//button[contains(normalize-space(.),'Easy Apply')]",
            ):
                if el.is_displayed():
                    return el
        except Exception:
            pass
        return None

    def _easy_apply_filter_radio(self, driver):
        """
        Newer filter-bar control: ``div[role=radio][aria-label='Filter by Easy Apply']`` with an
        inner checkbox + ``Easy Apply`` label (no All-filters modal).
        """
        el = self._find_visible_element(driver, (SEL["easy_apply_filter_radio"],))
        if el is not None:
            return el
        try:
            for cand in driver.find_elements(
                By.XPATH,
                "//div[@role='radio' or @role='checkbox']"
                "[contains(@aria-label,'Easy Apply') or .//label[contains(normalize-space(.),'Easy Apply')]]",
            ):
                try:
                    if cand.is_displayed():
                        return cand
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _control_aria_on(self, el) -> bool:
        if el is None:
            return False
        checked = (el.get_attribute("aria-checked") or "").strip().lower()
        if checked == "true":
            return True
        pressed = (el.get_attribute("aria-pressed") or "").strip().lower()
        if pressed == "true":
            return True
        cls = (el.get_attribute("class") or "").lower()
        return "artdeco-pill--selected" in cls or "artdeco-pill--green" in cls

    def easy_apply_filter_active(self, driver) -> bool:
        """True when Easy Apply filter is on (URL param and/or filter chip ``aria-checked``)."""
        if self._url_has_easy_apply_filter(driver):
            return True
        radio = self._easy_apply_filter_radio(driver)
        if self._control_aria_on(radio):
            return True
        pill = self._easy_apply_filter_pill(driver)
        return self._control_aria_on(pill)

    def _click_easy_apply_filter_pill(self, driver, *, only_if_off: bool = True) -> bool:
        pill = self._easy_apply_filter_pill(driver)
        if pill is None:
            return False
        if only_if_off and self._control_aria_on(pill):
            return True
        return self._click_interactive_element(driver, pill)

    def _click_easy_apply_filter_radio(self, driver, *, only_if_off: bool = True) -> bool:
        """Click the inline ``Filter by Easy Apply`` radio/checkbox chip on the search filter bar."""
        radio = self._easy_apply_filter_radio(driver)
        if radio is None:
            log.debug("Easy Apply recovery: Filter by Easy Apply radio/chip not found.")
            return False
        if only_if_off and self._control_aria_on(radio):
            log.debug("Easy Apply recovery: Filter by Easy Apply radio already checked.")
            return True
        # Prefer the visible label / inner control so the click lands on the chip UI.
        targets: list = []
        try:
            for css in ("label", "input[type='checkbox']", "[aria-label='Filter by Easy Apply']"):
                for el in radio.find_elements(By.CSS_SELECTOR, css):
                    try:
                        if el.is_displayed() or (el.tag_name or "").lower() == "input":
                            targets.append(el)
                    except Exception:
                        continue
        except Exception:
            pass
        targets.append(radio)
        for target in targets:
            if not self._click_interactive_element(driver, target):
                continue
            time.sleep(0.6)
            if self._url_has_easy_apply_filter(driver) or self._control_aria_on(
                self._easy_apply_filter_radio(driver)
            ):
                log.info("Easy Apply recovery: clicked Filter by Easy Apply radio/chip.")
                return True
        log.warning("Easy Apply recovery: clicked Filter by Easy Apply chip but filter still off.")
        return False

    def _easy_apply_modal_toggle_container(self, driver):
        """``search-reusables__advanced-filters-binary-toggle`` row for Easy Apply in All filters."""
        try:
            containers = driver.find_elements(
                By.CSS_SELECTOR,
                SEL["easy_apply_filter_modal_toggle"],
            )
        except Exception:
            return None
        for container in containers:
            try:
                blob = (container.text or "").lower()
                inner = (container.get_attribute("innerHTML") or "").lower()
            except Exception:
                continue
            if "easy apply" in blob or "easy apply" in inner:
                return container
        return None

    def _easy_apply_modal_toggle_input(self, driver):
        container = self._easy_apply_modal_toggle_container(driver)
        if container is None:
            return None
        for css in (
            'input[role="switch"][type="checkbox"]',
            "input.artdeco-toggle__button",
        ):
            try:
                return container.find_element(By.CSS_SELECTOR, css)
            except Exception:
                continue
        return None

    def _easy_apply_modal_toggle_label(self, driver):
        inp = self._easy_apply_modal_toggle_input(driver)
        if inp is None:
            return None
        toggle_id = (inp.get_attribute("id") or "").strip()
        if toggle_id:
            try:
                return driver.find_element(By.CSS_SELECTOR, f'label[for="{toggle_id}"]')
            except Exception:
                pass
        container = self._easy_apply_modal_toggle_container(driver)
        if container is None:
            return None
        try:
            return container.find_element(By.CSS_SELECTOR, "label.artdeco-toggle__label")
        except Exception:
            return None

    def _easy_apply_modal_toggle_is_on(self, inp) -> bool:
        if inp is None:
            return False
        checked = (inp.get_attribute("aria-checked") or "").strip().lower()
        if checked == "true":
            return True
        try:
            return bool(inp.is_selected())
        except Exception:
            return False

    def _click_easy_apply_modal_toggle(self, driver) -> bool:
        """
        Flip the **Easy Apply** ``artdeco-toggle`` switch inside the All filters panel.

        LinkedIn uses ``search-reusables__advanced-filters-binary-toggle`` with
        ``input[role='switch']`` and ``label.artdeco-toggle__label`` (not ``f_LF-f_AL``).
        """
        inp = self._easy_apply_modal_toggle_input(driver)
        if inp is None:
            log.debug("Easy Apply recovery: artdeco toggle input not found in modal.")
            return False
        if self._easy_apply_modal_toggle_is_on(inp):
            log.debug("Easy Apply recovery: artdeco toggle already on.")
            return True

        label = self._easy_apply_modal_toggle_label(driver)
        toggle_div = None
        container = self._easy_apply_modal_toggle_container(driver)
        if container is not None:
            try:
                toggle_div = container.find_element(By.CSS_SELECTOR, ".artdeco-toggle")
            except Exception:
                pass
        for target in (label, toggle_div, inp):
            if target is None:
                continue
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            except Exception:
                pass
            if not self._click_interactive_element(driver, target):
                continue
            time.sleep(0.4)
            if self._easy_apply_modal_toggle_is_on(inp):
                log.info("Easy Apply recovery: turned on artdeco toggle in All filters modal.")
                return True

        log.debug("Easy Apply recovery: clicked toggle but aria-checked did not change.")
        return False

    def _find_filters_modal_show_results_button(self, driver):
        btn = self._find_visible_element(driver, (SEL["all_filters_apply_btn"],))
        if btn is not None:
            return btn
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//button[contains(@class,'search-reusables__secondary-filters-show-results-button')]",
            ):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
            for el in driver.find_elements(
                By.XPATH,
                "//button[.//span[contains(@class,'artdeco-button__text') and contains(.,'results')]]",
            ):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _click_filters_modal_show_results(self, driver) -> bool:
        """Click **Show N results** at the bottom of the All filters panel."""
        deadline = time.time() + 8.0
        btn = None
        while time.time() < deadline:
            btn = self._find_filters_modal_show_results_button(driver)
            if btn is not None:
                break
            time.sleep(0.25)
        if btn is None:
            log.warning("Easy Apply recovery: Show results button not found in filters modal.")
            return False
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:
            pass
        time.sleep(0.25)
        if not self._click_interactive_element(driver, btn):
            return False
        log.info("Easy Apply recovery: clicked Show results in filters modal.")
        time.sleep(1.0)
        return True

    def _enable_easy_apply_via_all_filters_modal(self, driver) -> bool:
        """
        Open **All filters**, enable Easy Apply, and apply — LinkedIn's primary filter UI path.
        """
        all_filters = self._find_visible_element(driver, (SEL["all_filters_btn"],))
        if all_filters is None:
            log.debug("Easy Apply recovery: All filters button not found.")
            return False
        if not self._click_interactive_element(driver, all_filters):
            return False
        time.sleep(0.65)

        deadline = time.time() + 6.0
        while time.time() < deadline and self._easy_apply_modal_toggle_input(driver) is None:
            time.sleep(0.25)

        toggled = self._click_easy_apply_modal_toggle(driver)
        if not toggled:
            label = self._find_visible_element(driver, (SEL["easy_apply_filter_modal_label"],))
            if label is not None:
                toggled = self._click_interactive_element(driver, label)
        if not toggled:
            try:
                for inp in driver.find_elements(By.CSS_SELECTOR, SEL["easy_apply_filter_modal_input"]):
                    if not inp.is_displayed():
                        continue
                    if inp.is_selected():
                        toggled = True
                        break
                    toggled = self._click_interactive_element(driver, inp)
                    break
            except Exception:
                pass
        if not toggled:
            log.warning("Easy Apply recovery: Easy Apply toggle not found or could not be turned on.")
            return False

        inp = self._easy_apply_modal_toggle_input(driver)
        if inp is not None and not self._easy_apply_modal_toggle_is_on(inp):
            log.warning("Easy Apply recovery: toggle still off after click — skipping Show results.")
            return False

        return self._click_filters_modal_show_results(driver)

    def _enable_easy_apply_filter_ui(self, driver) -> bool:
        """
        Re-enable Easy Apply:

        1. All filters modal (legacy path), then classic Easy Apply pill.
        2. If still off — inline ``Filter by Easy Apply`` radio/checkbox chip (newer LinkedIn UI).
        """
        if self._driver_stopped(driver):
            return False
        if self.easy_apply_filter_active(driver):
            return True

        if self._enable_easy_apply_via_all_filters_modal(driver):
            time.sleep(0.8)
            if self.easy_apply_filter_active(driver):
                return True

        if self._click_easy_apply_filter_pill(driver, only_if_off=True):
            time.sleep(0.8)
            if self.easy_apply_filter_active(driver):
                return True

        log.info(
            "Easy Apply recovery: modal/pill path did not activate filter — "
            "trying Filter by Easy Apply radio/chip."
        )
        if self._click_easy_apply_filter_radio(driver, only_if_off=True):
            time.sleep(0.8)

        return self.easy_apply_filter_active(driver)

    def ensure_easy_apply_filter_on(self, driver) -> bool:
        """After navigation, confirm Easy Apply filter is still on (no recovery counter)."""
        if self.easy_apply_filter_active(driver):
            return True
        log.info("Easy Apply filter not active after search navigation — enabling via filter UI…")
        ok = self._enable_easy_apply_filter_ui(driver)
        if ok:
            log.info("Easy Apply filter is now active.")
        else:
            log.warning("Could not confirm Easy Apply filter is active after navigation.")
        return ok

    def recover_easy_apply_filter(self, driver) -> bool:
        """
        LinkedIn sometimes drops ``f_AL`` mid-session. Open filters and turn Easy Apply back on.

        Increments :attr:`easy_apply_filter_recoveries` when recovery is attempted.
        """
        if self.easy_apply_filter_active(driver):
            return True
        self.easy_apply_filter_recoveries += 1
        n = self.easy_apply_filter_recoveries
        log.warning(
            "LinkedIn Easy Apply filter appears off (non-Easy Apply listing seen) — recovery #%d…",
            n,
        )
        ok = self._enable_easy_apply_filter_ui(driver)
        if ok:
            log.info("Easy Apply filter re-enabled via UI (recovery #%d).", n)
            self._wait_job_list(driver)
        else:
            log.warning("Easy Apply filter recovery #%d did not confirm filter is active.", n)
        return ok

    def _find_job_description_element(self, driver):
        """First element matching any ``job_description`` selector, tried in priority order."""
        for css in SEL["job_description"]:
            els = driver.find_elements(By.CSS_SELECTOR, css)
            if els:
                return els[0]
        return None

    def _expand_job_description(self, driver) -> None:
        """
        Click the "…more" toggle(s) that visually clamp the description text, if present.

        Uses a JS click since the toggle can carry ``pointer-events: none`` once expanded —
        that CSS only blocks real pointer hit-testing, not a programmatic ``.click()``.
        """
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, SEL["job_description_more_button"])
        except Exception:
            return
        for btn in btns:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                continue

    def _read_job_description_panel(self, driver) -> str:
        """
        Read the job description panel after scrolling it — LinkedIn often lazy-loads sections
        (e.g. \"Requirements added by the job poster\") below the first viewport, and visually
        clamps the text behind a "…more" toggle that must be clicked to read the full text.
        """
        out = ""
        for attempt in range(2):
            if attempt:
                time.sleep(1.0)
            el = self._find_job_description_element(driver)
            if el is None:
                continue
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", el)
                time.sleep(0.55)
            except Exception:
                pass
            self._expand_job_description(driver)
            # The expand click can replace the description node (React re-render) — re-find it.
            el = self._find_job_description_element(driver) or el
            try:
                out = (el.text or "").strip()
            except Exception:
                out = ""
            if out:
                break
        if not out:
            return ""
        el = self._find_job_description_element(driver)
        if el is not None:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", el)
                time.sleep(0.65)
                self._expand_job_description(driver)
                el = self._find_job_description_element(driver) or el
                out = (el.text or "").strip() or out
            except Exception:
                pass
        return out

    def _peek_job_from_list_link(self, link) -> dict | None:
        """
        Read job id, title, company, location, Easy Apply from a **list card** without opening the job pane.

        Supports newer ``role=button`` job cards (``componentkey=job-card-component-ref-{id}``) and
        legacy scaffold ``li`` + title-link rows.
        """
        try:
            card = self._list_item_for_link(link)
            job_id = self._job_id_from_card_element(card) or self._job_id_from_card_element(link)

            href = ""
            try:
                href = (link.get_attribute("href") or "").strip()
            except Exception:
                href = ""

            if not job_id and href:
                job_id, _ = job_id_and_view_url_from_href(href)
            if not job_id:
                try:
                    jid_attr = (card.get_attribute("data-occludable-job-id") or "").strip()
                except Exception:
                    jid_attr = ""
                job_id = jid_attr if jid_attr.isdigit() else ""

            if not job_id:
                return None

            full_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

            title = self._text_from_first_match(
                card,
                (
                    SEL["job_card_title"],
                    "strong",
                    "p span[aria-hidden='true']",
                    "p span",
                ),
            )
            company = self._text_from_first_match(
                card,
                (
                    SEL["job_card_company"],
                    SEL["job_card_company_alt"],
                    SEL["job_card_company_row"],
                    ".artdeco-entity-lockup__subtitle span[dir='ltr']",
                    ".artdeco-entity-lockup__subtitle span",
                    ".artdeco-entity-lockup__subtitle",
                ),
            )
            loc = self._text_from_first_match(
                card,
                (
                    SEL["job_card_location"],
                    SEL["job_card_location_row"],
                ),
            )

            if not title or not company or not loc:
                parts = [p.strip() for p in (card.text or "").split("\n") if p.strip()]
                # Drop accessibility prefixes like "Selected, …"
                cleaned: list[str] = []
                for p in parts:
                    if p.lower().startswith("selected,"):
                        p = p.split(",", 1)[-1].strip() or p
                    if p in cleaned:
                        continue
                    cleaned.append(p)
                if not title and cleaned:
                    title = cleaned[0]
                # Company is usually the line after title; skip dismiss/meta noise.
                skip = {"viewed", "promoted", "easy apply", "be an early applicant"}
                meta_i = 1
                while meta_i < len(cleaned) and cleaned[meta_i].lower() in skip:
                    meta_i += 1
                if not company and meta_i < len(cleaned):
                    # Prefer a short company-like line (not "18 hours ago", not "·").
                    for cand in cleaned[meta_i:]:
                        cl = cand.lower()
                        if cand in {".", "·"} or "ago" in cl or "alumni" in cl:
                            continue
                        if cl.startswith("dismiss "):
                            continue
                        company = cand
                        break
                if not loc:
                    for cand in cleaned:
                        if "," in cand and "ago" not in cand.lower() and cand != company and cand != title:
                            loc = cand
                            break

            if title and title.lower().startswith("selected,"):
                title = title.split(",", 1)[-1].strip() or title

            easy_apply = self._easy_apply_near_card_link(link)

            return {
                "id": job_id,
                "title": title,
                "company": company,
                "location": loc,
                "url": full_url,
                "description": "",
                "easy_apply": easy_apply,
            }
        except Exception as e:
            log.debug("Error peeking job list link: %s", e)
            return None

    def _jobs_search_shell_present(self, driver) -> bool:
        """True when the two-pane search left rail is still mounted (not a lone /jobs/view/ page)."""
        try:
            if driver.find_elements(
                By.CSS_SELECTOR,
                f"{_JOB_CARD_BUTTON_CSS}, "
                "li.scaffold-layout__list-item[data-occludable-job-id], "
                "li[data-occludable-job-id], "
                ".scaffold-layout__list, "
                ".jobs-search-results-list",
            ):
                return True
        except WebDriverException:
            self._driver_stopped(driver)
            return False
        url = (driver.current_url or "").lower()
        # Search SPA often keeps results in the URL even when the rail selector set changes.
        return "jobs/search" in url or "search-results" in url or "currentjobid=" in url

    def _click_job_list_card(self, driver, link) -> bool:
        """
        Select a listing by clicking the **job card** (``role=button`` / list row), never a
        ``/jobs/view/`` title link — those navigate the primary window off search results.
        """
        card = self._list_item_for_link(link)
        scroll_into_view(driver, card)

        # New UI: the card itself is role=button — click it (avoid dismiss control).
        try:
            role = (card.get_attribute("role") or "").strip().lower()
            ck = (card.get_attribute("componentkey") or "").strip()
        except Exception:
            role, ck = "", ""
        if role == "button" and "job-card-component-ref-" in ck.lower():
            try:
                if self.highlight:
                    focus_element(driver, card, pause=self.step_delay)
                # Prefer a metadata line inside the card so we don't hit Dismiss.
                inner = None
                for css in ("p", "span[aria-hidden='true']", "figure"):
                    try:
                        for el in card.find_elements(By.CSS_SELECTOR, css):
                            try:
                                if not el.is_displayed():
                                    continue
                                # Skip text inside the dismiss control.
                                if el.find_elements(
                                    By.XPATH,
                                    "./ancestor::button[contains(@aria-label,'Dismiss')][1]",
                                ):
                                    continue
                            except Exception:
                                continue
                            inner = el
                            break
                    except Exception:
                        continue
                    if inner is not None:
                        break
                target = inner or card
                try:
                    target.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", card)
                self._pause()
                time.sleep(0.35)
                return True
            except Exception:
                log.debug("New job-card role=button click failed", exc_info=True)

        # Legacy UI: click non-link regions inside the list item.
        candidates: list = []
        for css in (
            ".artdeco-entity-lockup__subtitle",
            ".artdeco-entity-lockup__caption",
            ".job-card-container__metadata-wrapper",
            "[class*='job-card-job-posting-card-wrapper__primary-description']",
            "[class*='job-card-container']",
            "div.job-card-list__entity-lockup",
        ):
            try:
                for el in card.find_elements(By.CSS_SELECTOR, css):
                    try:
                        tag = (el.tag_name or "").lower()
                        href = (el.get_attribute("href") or "").strip()
                    except Exception:
                        continue
                    if tag == "a" or "/jobs/view/" in href.lower():
                        continue
                    if el.is_displayed():
                        candidates.append(el)
                        break
            except Exception:
                continue
            if candidates:
                break
        candidates.append(card)

        for el in candidates:
            try:
                if self.highlight:
                    focus_element(driver, el, pause=self.step_delay)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
                self._pause()
                time.sleep(0.35)
                return True
            except Exception:
                log.debug("Job card click target failed; trying next", exc_info=True)
                continue
        return False

    def _recover_jobs_search_if_navigated_away(self, driver, *, job_id: str = "") -> bool:
        """
        If a card click left the search shell for ``/jobs/view/…``, go back.

        Returns True when the search shell is present afterward.
        """
        if self._jobs_search_shell_present(driver):
            return True
        url = (driver.current_url or "").strip()
        log.warning(
            "Job open left the search results UI (url=%s%s) — navigating back.",
            url[:160],
            f", job_id={job_id}" if job_id else "",
        )
        try:
            driver.back()
            time.sleep(1.0)
        except WebDriverException:
            self._driver_stopped(driver)
            return False
        if self._jobs_search_shell_present(driver):
            return True
        # One more attempt — LinkedIn sometimes needs a second back through history.
        try:
            driver.back()
            time.sleep(1.0)
        except WebDriverException:
            self._driver_stopped(driver)
            return False
        return self._jobs_search_shell_present(driver)

    def _complete_job_after_peek(self, driver, link, peek: dict) -> dict | None:
        """Open the job **card** (detail pane on search), read description, and merge into ``peek``."""
        jid = str((peek or {}).get("id") or "").strip()
        try:
            for attempt in range(2):
                if not self._click_job_list_card(driver, link):
                    log.debug("Could not click job list card for job_id=%s", jid or "n/a")
                    return None

                if self._jobs_search_shell_present(driver):
                    break

                if not self._recover_jobs_search_if_navigated_away(driver, job_id=jid):
                    log.warning(
                        "Still off the jobs search page after back() — skipping open for job_id=%s",
                        jid or "n/a",
                    )
                    return None
                if attempt == 0:
                    log.warning(
                        "Restored jobs search UI after accidental /jobs/view/ navigation "
                        "(job_id=%s) — retrying card click.",
                        jid or "n/a",
                    )
                    # List remounted; re-resolve this job's card by id when possible.
                    if jid:
                        for cand in self._find_job_card_links(driver, expand=False):
                            p2 = self._peek_job_from_list_link(cand)
                            if p2 and str(p2.get("id") or "").strip() == jid:
                                link = cand
                                break
                    continue
                log.warning(
                    "Card click still left search UI (job_id=%s) — skipping this listing open.",
                    jid or "n/a",
                )
                return None
            else:
                return None

            time.sleep(0.55)
            time.sleep(self.job_description_wait_seconds)
            description = self._read_job_description_panel(driver)

            return {**peek, "description": description}
        except Exception as e:
            log.debug("Error completing job after peek: %s", e)
            try:
                self._recover_jobs_search_if_navigated_away(driver, job_id=jid)
            except Exception:
                pass
            return None

    def _parse_job_at_card_index(
        self, driver, index: int, links: list | None = None
    ) -> dict | None:
        if links is None:
            links = self._find_job_card_links(driver)
        if index >= len(links):
            return None
        link = links[index]
        peek = self._peek_job_from_list_link(link)
        if not peek:
            return None
        return self._complete_job_after_peek(driver, link, peek)

    def dismiss_current_job(self, driver, *, reason: str | None = None, job_id: str | None = None) -> bool:
        """
        Dismiss a LinkedIn job card from the left rail.

        If ``job_id`` is provided, targets that exact ``data-occludable-job-id`` row first.
        Returns True when a dismiss button was found/clicked, otherwise False.
        """
        if self._driver_stopped(driver):
            return False
        selectors: list[str] = []
        jid = (job_id or "").strip()
        if jid:
            selectors.extend(
                (
                    f'div[role="button"][componentkey="job-card-component-ref-{jid}"] '
                    f'button[aria-label^="Dismiss "][aria-label$=" job"]',
                    f'div[componentkey="job-card-component-ref-{jid}"] '
                    f'button[aria-label^="Dismiss "][aria-label$=" job"]',
                    f'li[data-occludable-job-id="{jid}"] button[aria-label^="Dismiss "][aria-label$=" job"]',
                    f'li[data-occludable-job-id="{jid}"] button.job-card-container__action',
                )
            )
        selectors.extend(
            (
                'div[role="button"][componentkey^="job-card-component-ref-"] '
                'button[aria-label^="Dismiss "][aria-label$=" job"]',
                # Fallback to active/selected row when no explicit id match is available.
                'li.scaffold-layout__list-item--active button[aria-label^="Dismiss "][aria-label$=" job"]',
                'li.jobs-search-results__list-item--active button[aria-label^="Dismiss "][aria-label$=" job"]',
                'li[aria-current="true"] button[aria-label^="Dismiss "][aria-label$=" job"]',
                # Last resort: any visible dismiss action button.
                'button.job-card-container__action[aria-label^="Dismiss "][aria-label$=" job"]',
                'button[class*="job-card-container__action"][aria-label*="Dismiss"][aria-label$=" job"]',
                'button[aria-label^="Dismiss "][aria-label$=" job"]',
            )
        )

        btn = None
        for css in selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, css)
            except WebDriverException:
                self._driver_stopped(driver)
                return False
            if matches:
                btn = matches[0]
                break

        if btn is None:
            log.debug(
                "LinkedIn dismiss: no dismiss button found (reason=%s, job_id=%s)",
                reason or "n/a",
                jid or "n/a",
            )
            return False

        try:
            if self.highlight:
                focus_element(driver, btn, pause=self.step_delay)
            btn.click()
        except Exception:
            # LinkedIn overlays can intercept clicks; JS click is a practical fallback.
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                log.debug("LinkedIn dismiss: click failed (reason=%s)", reason or "n/a", exc_info=True)
                return False

        time.sleep(0.35)
        log.info(
            "Dismissed LinkedIn job card%s%s",
            f" ({reason})" if reason else "",
            f" [job_id={jid}]" if jid else "",
        )
        return True

    def save_current_job(self, driver, *, job_id: str | None = None) -> bool:
        """
        Click **Save** on the open job detail pane. Returns True when the job is saved or was already saved.

        If ``job_id`` is provided, also tries the matching list-row save control as a fallback.
        """
        if self._driver_stopped(driver):
            return False

        def _visible_matches(css: str) -> list:
            try:
                return [el for el in driver.find_elements(By.CSS_SELECTOR, css) if el.is_displayed()]
            except WebDriverException:
                self._driver_stopped(driver)
                return []

        def _saved_state_present() -> bool:
            """
            True when the job shows a saved state. Covers the classic ``Unsave`` control and the
            newer LinkedIn UI whose toggle simply reads **Saved** (no ``Unsave`` label at all).
            """
            if _visible_matches(SEL["job_unsave_btn"]):
                return True
            saved_xpath = (
                "//button[normalize-space(.)='Saved' or @aria-label='Saved' "
                "or contains(@aria-label,'Unsave') "
                "or .//*[normalize-space(.)='Saved']]"
            )
            try:
                return any(el.is_displayed() for el in driver.find_elements(By.XPATH, saved_xpath))
            except WebDriverException:
                self._driver_stopped(driver)
                return False

        if _saved_state_present():
            log.info("LinkedIn job already saved (saved-state control visible).")
            return True

        save_selectors: list[str] = [SEL["job_save_btn"]]
        jid = (job_id or "").strip()
        if jid:
            save_selectors.extend(
                (
                    f'li[data-occludable-job-id="{jid}"] button[aria-label^="Save "]',
                    f'li[data-occludable-job-id="{jid}"] button[aria-label*="Save job"]',
                )
            )
        save_selectors.extend(
            (
                'li.scaffold-layout__list-item--active button[aria-label^="Save "]',
                'li[aria-current="true"] button[aria-label^="Save "]',
            )
        )

        btn = None
        for css in save_selectors:
            matches = _visible_matches(css)
            if matches:
                btn = matches[0]
                break

        if btn is None:
            log.warning(
                "LinkedIn save: no Save button found (job_id=%s)",
                jid or "n/a",
            )
            return False

        try:
            if self.highlight:
                focus_element(driver, btn, pause=self.step_delay)
            btn.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                log.debug("LinkedIn save: click failed (job_id=%s)", jid or "n/a", exc_info=True)
                return False

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if _saved_state_present():
                log.info("Saved LinkedIn job%s", f" [job_id={jid}]" if jid else "")
                return True
            time.sleep(0.25)

        log.warning(
            "Clicked Save but saved-state control not detected afterward (job_id=%s)",
            jid or "n/a",
        )
        return False

    def selected_job_company_link(self, driver) -> str:
        """
        Return the selected job's company LinkedIn URL from the detail pane, or empty string.
        """
        if self._driver_stopped(driver):
            return ""
        selectors = (
            ".job-details-jobs-unified-top-card__company-name a[href]",
            ".jobs-unified-top-card__company-name a[href]",
            "a[data-test-app-aware-link][href*='/company/']",
            "a[href*='linkedin.com/company/']",
        )
        for css in selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, css)
            except WebDriverException:
                self._driver_stopped(driver)
                return ""
            for el in matches:
                try:
                    href = (el.get_attribute("href") or "").strip()
                except WebDriverException:
                    self._driver_stopped(driver)
                    return ""
                if "linkedin.com/company/" in href:
                    return href
        return ""

    def fetch_dedicated_page_requirements(self, lookup_driver, job_id: str) -> str:
        """
        Navigate to the job's dedicated page and return the "Requirements added by the job poster"
        section text (empty string if the section is absent or the page fails to load).

        LinkedIn omits this section from the two-pane search-results view, so the main driver's
        scraped description never contains it. This method polls directly for the marker element
        via XPath rather than relying on .jobs-description__content, which does not contain the
        requirements <p> elements (they are siblings in the parent container).
        """
        _MARKER = "Requirements added by the job poster"
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        try:
            lookup_driver.get(url)
        except Exception as e:
            log.debug("fetch_dedicated_page_requirements: navigation failed for %s: %s", url, e)
            return ""

        # Poll until the marker element appears, scrolling to the bottom each round to
        # trigger any lazy-loaded content below the fold.
        deadline = time.time() + 10.0
        found = False
        while time.time() < deadline:
            try:
                lookup_driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
            try:
                els = lookup_driver.find_elements(
                    By.XPATH, f"//*[contains(text(), '{_MARKER}')]"
                )
                if any(e.is_displayed() for e in els):
                    found = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not found:
            log.debug(
                "fetch_dedicated_page_requirements: %r not found on %s after timeout", _MARKER, url
            )
            return ""

        # Collect the marker element text + all following siblings (the bullet-point <p> elements).
        parts: list[str] = [_MARKER]
        try:
            siblings = lookup_driver.find_elements(
                By.XPATH, f"//*[contains(text(), '{_MARKER}')]/following-sibling::*"
            )
            for sib in siblings:
                t = (sib.text or "").strip()
                if t:
                    parts.append(t)
        except Exception as e:
            log.debug("fetch_dedicated_page_requirements: sibling read error: %s", e)

        result = "\n".join(parts).strip()
        log.debug(
            "fetch_dedicated_page_requirements: captured %d chars for job %s", len(result), job_id
        )
        return result

    def company_page_looks_consulting(self, company_driver, company_url: str) -> bool:
        """
        True when company page industry contains consulting/recruiting signals.

        Industry signals: ``consult``, ``recruit``
        """
        url = (company_url or "").strip()
        if not url:
            return False
        try:
            company_driver.get(url)
            time.sleep(1.1)
        except Exception:
            log.debug("Company lookup: failed to open %s", url, exc_info=True)
            return False

        industry_text = ""
        for css in (
            ".org-top-card-summary-info-list__info-item",
            ".organization-top-card-summary-info-list__info-item",
        ):
            try:
                els = company_driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                els = []
            for el in els:
                t = (el.text or "").strip()
                if t:
                    industry_text = t
                    break
            if industry_text:
                break

        industry_l = industry_text.lower()
        industry_hit = bool("consult" in industry_l or "recruit" in industry_l)

        if industry_hit:
            log.info(
                "Company lookup flagged consulting signals (industry_hit=%s): %s",
                industry_hit,
                url,
            )
            return True
        return False

    @staticmethod
    def company_jobs_base_url(company_url: str) -> str:
        """Normalize a company (or About) URL to ``…/company/{slug}/jobs/``."""
        raw = (company_url or "").strip()
        if not raw:
            return ""
        m = re.search(r"(https?://(?:www\.)?linkedin\.com/company/[^/?#]+)", raw, re.IGNORECASE)
        if not m:
            return ""
        return f"{m.group(1).rstrip('/')}/jobs/"

    @staticmethod
    def _linkedin_query_param(url: str, name: str) -> str | None:
        """First non-empty query value for ``name`` (case-insensitive key match)."""
        raw = (url or "").strip()
        if not raw:
            return None
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(raw).query, keep_blank_values=False)
        except ValueError:
            return None
        for key, values in q.items():
            if key.lower() == name.lower() and values:
                v = (values[0] or "").strip()
                if v:
                    return v
        return None

    @classmethod
    def classic_company_jobs_search_url(
        cls,
        source_url: str,
        *,
        f_tpr: str = _F_TPR_PAST_WEEK,
    ) -> str | None:
        """
        Build a classic two-pane ``/jobs/search/?f_C=…&f_TPR=…`` URL from a company Jobs /
        ``search-results`` / ``jobs/search`` link.

        LinkedIn's newer ``/jobs/search-results/`` company expansion pages do not expose the
        left-rail card markup our scanners use, so company scans must land on ``/jobs/search/``.
        """
        raw = (source_url or "").strip()
        if not raw:
            return None
        company_id = cls._linkedin_query_param(raw, "f_C")
        if not company_id:
            return None
        params: dict[str, str] = {"f_C": company_id}
        if f_tpr:
            params["f_TPR"] = f_tpr
        geo = cls._linkedin_query_param(raw, "geoId")
        if geo:
            params["geoId"] = geo
        return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

    def _apply_company_jobs_posted_window(self, driver, *, f_tpr: str = _F_TPR_PAST_WEEK) -> bool:
        """
        Navigate to a classic company ``/jobs/search/`` URL with ``f_tpr`` (default: past week).

        Accepts the current page URL or a ``f_C=`` deep link on the page. Returns True when
        navigation was attempted successfully.
        """
        if self._driver_stopped(driver):
            return False
        try:
            current = (driver.current_url or "").strip()
        except WebDriverException:
            return False

        target = self.classic_company_jobs_search_url(current, f_tpr=f_tpr)
        if not target:
            # Still on /company/.../jobs/ — find a search deep-link that carries f_C.
            href = ""
            try:
                for css in (
                    "a[href*='f_C=']",
                    "a[href*='/jobs/search']",
                    "a[href*='/jobs/search-results']",
                ):
                    for el in driver.find_elements(By.CSS_SELECTOR, css):
                        try:
                            if not el.is_displayed():
                                continue
                        except Exception:
                            continue
                        cand = (el.get_attribute("href") or "").strip()
                        if self._linkedin_query_param(cand, "f_C"):
                            href = cand
                            break
                    if href:
                        break
            except WebDriverException:
                href = ""
            target = self.classic_company_jobs_search_url(href, f_tpr=f_tpr) if href else None

        if not target:
            log.warning(
                "Company jobs: could not build classic /jobs/search/ URL with %s date filter.",
                f_tpr,
            )
            return False

        if (current or "").rstrip("/") == target.rstrip("/"):
            return True

        log.info("Company jobs: opening classic search with date filter %s → %s", f_tpr, target)
        try:
            driver.get(target)
            time.sleep(1.5)
        except WebDriverException:
            log.debug("Company jobs: failed to open classic date-filtered search", exc_info=True)
            return False
        return True

    def open_company_jobs_list(self, driver, company_url: str) -> bool:
        """
        From a company page URL, open the Jobs tab and click **Show all jobs** / **See all jobs**
        so the secondary driver lands on a company-filtered jobs search list.

        After landing, navigates to classic ``/jobs/search/?f_C=…&f_TPR=r604800`` (Past week) —
        intentionally wider than the primary search's past-24-hours window. Newer LinkedIn
        ``/jobs/search-results/`` company pages are avoided because they do not expose scannable
        left-rail job cards.

        Returns True when job list links are visible afterward.
        """
        if self._driver_stopped(driver):
            return False
        jobs_url = self.company_jobs_base_url(company_url)
        if not jobs_url:
            log.warning("Company jobs: could not derive jobs URL from %r", company_url)
            return False

        try:
            driver.get(jobs_url)
            time.sleep(1.2)
        except WebDriverException:
            log.debug("Company jobs: failed to open %s", jobs_url, exc_info=True)
            return False

        # Prefer an explicit "Show/See all jobs" control that deep-links into jobs/search?f_C=…
        show_all = None
        for css in (
            SEL["company_show_all_jobs"],
            "a[href*='/jobs/search']",
            "a[href*='f_C=']",
        ):
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, css):
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    label = ((el.text or "") + " " + (el.get_attribute("aria-label") or "")).lower()
                    href = (el.get_attribute("href") or "").lower()
                    if "f_c=" in href or "show all" in label or "see all" in label or "see jobs" in label:
                        show_all = el
                        break
                if show_all is not None:
                    break
            except WebDriverException:
                continue

        if show_all is None:
            # XPath fallback on visible link text.
            for xp in (
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), 'show all jobs')]",
                "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), 'see all jobs')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), 'show all jobs')]",
                "//a[contains(@href,'f_C=')]",
            ):
                try:
                    for el in driver.find_elements(By.XPATH, xp):
                        try:
                            if el.is_displayed():
                                show_all = el
                                break
                        except Exception:
                            continue
                    if show_all is not None:
                        break
                except WebDriverException:
                    continue

        if show_all is not None:
            show_href = ""
            try:
                show_href = (show_all.get_attribute("href") or "").strip()
            except Exception:
                show_href = ""
            classic = self.classic_company_jobs_search_url(show_href, f_tpr=_F_TPR_PAST_WEEK)
            if classic:
                log.info(
                    "Company jobs: opening Show all jobs as classic past-week search → %s",
                    classic,
                )
                try:
                    driver.get(classic)
                    time.sleep(1.5)
                except WebDriverException:
                    log.debug("Company jobs: classic Show all navigation failed", exc_info=True)
                    return False
            else:
                try:
                    if self.highlight:
                        focus_element(driver, show_all, pause=self.step_delay)
                    show_all.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", show_all)
                    except Exception:
                        log.debug("Company jobs: Show all jobs click failed", exc_info=True)
                        return False
                time.sleep(1.5)
                if not self._apply_company_jobs_posted_window(driver, f_tpr=_F_TPR_PAST_WEEK):
                    return False
        else:
            log.info(
                "Company jobs: no Show/See all jobs control — deriving classic search from %s",
                jobs_url,
            )
            if not self._apply_company_jobs_posted_window(driver, f_tpr=_F_TPR_PAST_WEEK):
                return False

        if not self._wait_job_list(driver):
            log.warning("Company jobs: job list did not appear after opening %s", jobs_url)
            return False
        return True

    def iter_company_job_peeks(
        self,
        driver,
        *,
        max_cards: int | None = None,
        claim_job_id: Callable[[str], bool] | None = None,
    ):
        """
        Yield ``(link, peek)`` for jobs on the current company / search jobs list.

        ``claim_job_id(jid)`` if provided should return True when this session should process the id
        (and record it as seen). Duplicates are skipped.
        """
        if self._driver_stopped(driver):
            return
        self._expand_virtualized_job_list(driver)
        if self._driver_stopped(driver):
            return
        self._scroll_job_list_to_top(driver)
        links = self._find_job_card_links(driver, expand=False)
        log.info("Company jobs: found %d list link(s) to scan", len(links))
        yielded = 0
        i = 0
        while i < len(links):
            if max_cards is not None and yielded >= max_cards:
                break
            if self._driver_stopped(driver):
                break
            # Re-query periodically — virtual list may remount nodes after scrolls/clicks.
            if i >= len(links):
                links = self._find_job_card_links(driver, expand=False)
            if i >= len(links):
                break
            link = links[i]
            i += 1
            peek = self._peek_job_from_list_link(link)
            if not peek:
                continue
            jid = str(peek.get("id") or "").strip()
            if not jid:
                continue
            if claim_job_id is not None and not claim_job_id(jid):
                log.info("Company jobs: skipping duplicate job id %s", jid)
                continue
            yield link, peek
            yielded += 1
            # Refresh link list after each open (detail pane clicks can invalidate elements).
            links = self._find_job_card_links(driver, expand=False)

    def complete_company_job(self, driver, link, peek: dict) -> dict | None:
        """Open a company-list card and return the full job dict (same shape as search pipeline)."""
        return self._complete_job_after_peek(driver, link, peek)

    def parse_current_job_from_detail_pane(self, driver) -> dict | None:
        """
        Best-effort job dict from the **currently selected** listing (URL + right-hand detail pane).
        Used when parsing the job from the LinkedIn detail pane outside the automated list walk.
        """
        url = (driver.current_url or "").strip()
        job_id = ""
        m = re.search(r"currentJobId=(\d+)", url, re.I)
        if m:
            job_id = m.group(1)
        else:
            m = re.search(r"/jobs/view/(\d+)", url, re.I)
            if m:
                job_id = m.group(1)
        if not job_id:
            return None

        full_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

        title = ""
        for css in (
            ".jobs-unified-top-card__job-title",
            ".jobs-details-top-card__title-text",
            "h1.jobs-unified-top-card__job-title",
            "div[class*='jobs-details-top-card'] h1",
            "h1[class*='job-title']",
        ):
            title = self._text_from_first_match(driver, (css,))
            if title:
                break

        company = ""
        for css in (
            ".job-details-jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name",
            "a[class*='company-name']",
        ):
            company = self._text_from_first_match(driver, (css,))
            if company:
                break

        loc = ""
        for css in (
            ".job-details-jobs-unified-top-card__primary-description",
            ".jobs-unified-top-card__bullet",
            ".jobs-unified-top-card__workplace-type",
        ):
            loc = self._text_from_first_match(driver, (css,))
            if loc:
                break

        if not title:
            try:
                title = driver.find_element(By.TAG_NAME, "h1").text.strip()
            except Exception:
                title = ""

        time.sleep(max(0.5, min(self.job_description_wait_seconds, 2.0)))
        description = self._read_job_description_panel(driver)

        return {
            "id": job_id,
            "title": title or "(unknown title)",
            "company": company,
            "location": loc,
            "url": full_url,
            "description": description,
            "easy_apply": True,
        }

    def _login_page_shows_welcome_back_saved_account(self, driver) -> bool:
        """
        True when LinkedIn shows the **Welcome Back** saved-session flow and/or a ``Login as …`` button.

        Cookies may be rejected while Chromium still has a remembered profile for one-click continue.
        """
        try:
            h = driver.find_element(By.CSS_SELECTOR, SEL["welcome_back_heading"])
            if "welcome back" in (h.text or "").strip().lower():
                return True
        except Exception:
            pass
        for css in (".header__content__heading", "h1[class*='header__content__heading']"):
            try:
                h = driver.find_element(By.CSS_SELECTOR, css)
                if "welcome back" in (h.text or "").strip().lower():
                    return True
            except Exception:
                continue
        try:
            if driver.find_elements(By.CSS_SELECTOR, SEL["saved_account_login"]):
                return True
        except Exception:
            pass
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            bl = body.lower()
            if "welcome back" in bl and ("login as" in bl or "sign in to stay" in bl):
                return True
        except Exception:
            pass
        return False

    def _try_click_saved_account_login(self, driver) -> bool:
        """
        If LinkedIn shows a remembered account (``Login as …`` on ``member-profile__details``), click it
        to continue the session when cookies are not enough but the browser still knows the user.
        """
        selectors = (
            'button.member-profile__details[aria-label^="Login as "]',
            SEL["saved_account_login"],
            ".member-profile-block button.member-profile__details",
            'button[class*="member-profile__details"]',
        )
        candidates: list = []
        for css in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                els = []
            for el in els:
                try:
                    if el.is_displayed() and el.is_enabled():
                        candidates.append(el)
                except Exception:
                    continue
            if candidates:
                break

        if not candidates:
            return False

        btn = None
        fn = self.account_first_name or ""
        if fn and len(fn) >= _MIN_FIRST_NAME_LEN:
            fn_lower = fn.lower()
            for el in candidates:
                label = (el.get_attribute("aria-label") or "").lower()
                if fn_lower in label:
                    btn = el
                    break
        if btn is None:
            btn = candidates[0]

        label = (btn.get_attribute("aria-label") or "").strip() or "(saved account)"
        log.info("Clicking LinkedIn saved-account button: %s", label)
        try:
            if self.highlight:
                focus_element(driver, btn, pause=self.step_delay)
            btn.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except Exception:
                log.debug("Saved-account button click failed", exc_info=True)
                return False

        self._pause()
        time.sleep(1.2)
        return True

    def _session_looks_logged_in(self, driver) -> bool:
        # Use feed *path* only — "feed" in the raw URL matches login pages (?trk=feed, redirect=...feed...).
        # When a first name is set, still accept /feed path first: feed loads before nav shows the name.
        url = driver.current_url or ""
        if _linkedin_url_blocks_logged_in_session(url):
            return False
        if _linkedin_url_is_feed_home(url):
            return True
        if _linkedin_url_is_authenticated_jobs_area(url):
            return True
        if self.account_first_name and len(self.account_first_name) >= _MIN_FIRST_NAME_LEN:
            return _page_contains_first_name(driver, self.account_first_name)
        return False

    @staticmethod
    def _wait_after_initial_linkedin_feed_nav() -> None:
        log.info(
            "Waiting %.0fs after opening LinkedIn so cookie-based redirects (e.g. delayed sign-in) can finish.",
            LINKEDIN_FEED_FIRST_NAV_DELAY_S,
        )
        time.sleep(LINKEDIN_FEED_FIRST_NAV_DELAY_S)

    @staticmethod
    def _wait_on_linkedin_login_page_for_delayed_auth() -> None:
        log.info(
            "Waiting %.0fs on the LinkedIn login page in case the session completes automatically.",
            LINKEDIN_LOGIN_PAGE_POST_NAV_DELAY_S,
        )
        time.sleep(LINKEDIN_LOGIN_PAGE_POST_NAV_DELAY_S)

    def _skip_redundant_linkedin_login_get(self, driver) -> bool:
        """When cookies already sent us to sign-in / checkpoint, avoid a second ``get(/login)``."""
        return _linkedin_url_on_credential_or_device_flow(driver.current_url or "")

    def _login_return_if_session_ready(self, driver, *, note: str) -> bool:
        """If the browser already has an authenticated session, log and return True (skip credential form)."""
        if self._session_looks_logged_in(driver):
            log.info("Login complete without credential step (%s).", note)
            return True
        return False

    def _login(self, driver) -> None:
        if self.account_first_name:
            log.info("Login check: looking for first name %r on the page", self.account_first_name)
        driver.get("https://www.linkedin.com/feed/")
        self._pause()
        self._wait_after_initial_linkedin_feed_nav()

        if self._session_looks_logged_in(driver):
            log.info("Already logged in (session restored)")
            return

        if self._skip_redundant_linkedin_login_get(driver):
            log.info(
                "LinkedIn already on a sign-in or device-trust URL after cookies; skipping extra /login navigation."
            )
        else:
            log.info("Opening LinkedIn login page (saved account or email/password).")
            driver.get("https://www.linkedin.com/login")
        self._pause()
        self._wait_on_linkedin_login_page_for_delayed_auth()
        time.sleep(self.login_form_wait_seconds)

        if self._login_return_if_session_ready(driver, note="after login-page wait"):
            return

        if self._login_page_shows_welcome_back_saved_account(driver):
            log.info("LinkedIn shows Welcome Back / saved profile — trying one-click login.")
            if self._try_click_saved_account_login(driver):
                if "checkpoint" in driver.current_url or "captcha" in driver.current_url.lower():
                    if self.auto:
                        raise StopApplyPipeline("2FA/CAPTCHA checkpoint after saved-account click — exiting (auto mode)")
                    log.warning(
                        "2FA/CAPTCHA after saved-account click — complete it manually in Chrome "
                        f"(polling up to {self.login_complete_max_seconds:.0f}s)"
                    )
                    self._wait_until_logged_in(driver, self.login_complete_max_seconds)
                else:
                    self._wait_until_logged_in(driver, self.login_complete_max_seconds_no_checkpoint)
                if self._session_looks_logged_in(driver):
                    log.info("Login successful (saved account)")
                    return
            log.info("Saved-account path did not complete session; falling back to email/password.")

        if self._login_return_if_session_ready(driver, note="before email/password form"):
            return

        email = os.environ.get("LINKEDIN_EMAIL", "")
        password = os.environ.get("LINKEDIN_PASSWORD", "")

        if not email or not password:
            raise EnvironmentError(
                "Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in your .env file "
                "(needed when cookies and saved-account login are not enough)."
            )

        log.info("Logging in as %s", email)
        if not self._skip_redundant_linkedin_login_get(driver):
            driver.get("https://www.linkedin.com/login")
        self._pause()
        self._wait_on_linkedin_login_page_for_delayed_auth()
        time.sleep(self.login_form_wait_seconds)

        if self._login_return_if_session_ready(driver, note="after second login-page wait"):
            return

        email_el = _find_login_element(driver, SEL["email_input"])
        if self.highlight:
            focus_element(driver, email_el, pause=self.step_delay)
        email_el.clear()
        email_el.send_keys(email)

        pw_el = _find_login_element(driver, SEL["password_input"])
        pw_el.clear()
        pw_el.send_keys(password)

        sign_in = _find_login_element(driver, SEL["sign_in_btn"])
        if self.highlight:
            focus_element(driver, sign_in, pause=self.step_delay)
        sign_in.click()
        time.sleep(2.5)

        if "checkpoint" in driver.current_url or "captcha" in driver.current_url.lower():
            if self.auto:
                raise StopApplyPipeline("2FA/CAPTCHA checkpoint detected — exiting (auto mode)")
            log.warning(
                "2FA/CAPTCHA detected — complete it manually in Chrome "
                f"(polling up to {self.login_complete_max_seconds:.0f}s)"
            )
            self._wait_until_logged_in(driver, self.login_complete_max_seconds)
        else:
            self._wait_until_logged_in(driver, self.login_complete_max_seconds_no_checkpoint)

        log.info("Login successful")

    def _wait_until_logged_in(self, driver, max_seconds: float) -> None:
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            if self._session_looks_logged_in(driver):
                return
            time.sleep(1.0)
        if self.auto:
            raise StopApplyPipeline(
                f"Login did not complete within {max_seconds:.0f}s — exiting (auto mode)"
            )
        raise RuntimeError(
            f"Login did not complete within {max_seconds:.0f}s (first name / feed URL not detected)"
        )
