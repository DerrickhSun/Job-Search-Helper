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
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from selenium.common.exceptions import NoSuchElementException

# Sentinel for "unlimited" quota math (last page, no max_jobs cap).
_MAX_QUOTA = 2**30
from selenium.webdriver.common.by import By

from .chrome_driver import (
    DEFAULT_COOKIE_PATH,
    build_chrome,
    driver_session_alive,
    focus_element,
    load_cookies,
    log_driver_session_closed,
    save_cookies,
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
    # Job search list: ``li.scaffold-layout__list-item[data-occludable-job-id]``; link may be
    # ``/jobs/view/ID/`` (two-pane) or ``currentJobId=`` (search results URL).
    "job_card_title": '[class*="job-card-job-posting-card-wrapper__title"]',
    "job_card_company": '[class*="job-card-job-posting-card-wrapper__company-name"]',
    "job_card_company_alt": '[class*="job-card-job-posting-card-wrapper__primary-description"]',
    # Two-pane list: company / location sit in the lockup, not inside the title ``<a>``.
    "job_card_company_row": ".artdeco-entity-lockup__subtitle span",
    "job_card_location": '[class*="job-card-job-posting-card-wrapper__location"]',
    "job_card_location_row": ".artdeco-entity-lockup__caption .job-card-container__metadata-wrapper li span",
    "job_description": ".jobs-description__content",
    "next_page": 'button[aria-label="View next page"]',
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

    def _jobs_search_query(self, keywords: str, location: str, easy_apply_only: bool) -> str:
        params: dict[str, str] = {"keywords": keywords, "location": location}
        if easy_apply_only:
            params["f_LF"] = "f_AL"
        if self.posted_within_24h:
            params["f_TPR"] = "r86400"
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
            driver.quit()

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
        seen_job_ids: set[str] = set()
        apply_goal_met = False
        driver_closed = False
        stop_requested = False
        kw_list = normalize_search_keywords(keywords)

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
                driver.get(url)
                self._pause()
                time.sleep(1.2)

                while max_listings is None or processed < max_listings:
                    if driver_closed:
                        break
                    if not driver_session_alive(driver):
                        log_driver_session_closed()
                        driver_closed = True
                        break

                    self._wait_job_list(driver)

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
                    self._scroll_job_list_to_top(driver)

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
                                self._scroll_job_list_to_top(driver)
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
                        if jid in seen_job_ids:
                            log.info("Skipping duplicate job id %s (already processed this session)", jid)
                            continue
                        seen_job_ids.add(jid)

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
                        except Exception:
                            log.exception(
                                "Pipeline error for %s at %s",
                                job.get("title"),
                                job.get("company"),
                            )

                        page_done += 1
                        processed += 1
                        if stop_requested:
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
                    next_els = driver.find_elements(By.CSS_SELECTOR, SEL["next_page"])
                    if not next_els:
                        log.info("No further results pages")
                        break
                    next_btn = next_els[0]
                    if self.highlight:
                        focus_element(driver, next_btn, pause=self.step_delay)
                    next_btn.click()
                    self._pause()
                    time.sleep(1.2)

                if apply_goal_met or driver_closed:
                    break

            if not driver_closed:
                save_cookies(driver, self.session_file)
            return processed
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def _wait_job_list(self, driver) -> None:
        if self.job_cards_wait_seconds > 0:
            log.info(
                "Waiting %.1fs for job list to render",
                self.job_cards_wait_seconds,
            )
            time.sleep(self.job_cards_wait_seconds)

    def _left_rail_job_links(self, driver):
        """Primary selector: title links inside virtualized list rows (avoids right-pane duplicates)."""
        return driver.find_elements(
            By.CSS_SELECTOR,
            'li[data-occludable-job-id] a[href*="/jobs/view/"]',
        )

    def _job_list_scroll_pick_js(self) -> str:
        """Returns JS that defines ``pick()`` → scrollable job-list element or null."""
        return """
            const pick = () => {
              const sels = [
                '.jobs-search-results-list',
                '[class*="jobs-search-results-list"]',
                '.scaffold-layout__list-container',
                'div[class*="scaffold-layout__list"]',
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

    def _apply_job_list_scroll(self, driver, mode: str) -> None:
        """
        ``mode``: ``top`` | ``bottom`` | ``page_down`` — scroll the left-rail list, not the window only.
        ``page_down`` nudges by ~one viewport so virtualization mounts rows in the middle, not only at the end.
        """
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

    def _scroll_job_list_to_top(self, driver) -> None:
        """After loading the list, scroll back to the first card so indices match top-to-bottom order."""
        self._apply_job_list_scroll(driver, "top")
        time.sleep(0.35)

    def _tail_load_job_list(self, driver) -> int | None:
        """
        After the count stops rising briefly, LinkedIn may still append rows (network / observers).
        Extra bottom + page-down passes; return new count if it grew, else None.
        """
        before = len(self._left_rail_job_links(driver))
        best = before
        for _ in range(self.job_list_tail_pass_rounds):
            self._apply_job_list_scroll(driver, "bottom")
            self._apply_job_list_scroll(driver, "page_down")
            time.sleep(self.job_list_scroll_pause * 1.25)
            cur = len(self._left_rail_job_links(driver))
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
        for round_i in range(self.job_list_scroll_max_rounds):
            n = len(self._left_rail_job_links(driver))
            if n > 0 and n == last_n:
                stable += 1
                if stable >= self.job_list_scroll_stable_rounds:
                    grown = self._tail_load_job_list(driver)
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
            self._apply_job_list_scroll(driver, "bottom")
            self._apply_job_list_scroll(driver, "page_down")
            time.sleep(self.job_list_scroll_pause)

        log.info(
            "Virtual job list: hit max %d scroll rounds (last count %d link(s))",
            self.job_list_scroll_max_rounds,
            last_n,
        )

    def _find_job_card_links(self, driver, *, expand: bool = True):
        """
        Job rows are ``<li class="... scaffold-layout__list-item ... ember-view ..." data-occludable-job-id>``.
        We target the job link inside each row, then read ``data-occludable-job-id`` when parsing.

        If ``expand`` is True, scroll the left rail first so virtualized rows mount (see
        ``_expand_virtualized_job_list``). Pipeline callers usually pass ``expand=False`` after
        a single expand per page.
        """
        if expand:
            self._expand_virtualized_job_list(driver)
        attempts: list[tuple[str, str]] = [
            # Two-pane jobs UI: relative ``/jobs/view/ID/`` (no currentJobId, no linkedin.com in href)
            ("css", 'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="/jobs/view/"]'),
            ("css", 'li[data-occludable-job-id] a[href*="/jobs/view/"]'),
            (
                "css",
                'li.scaffold-layout__list-item[data-occludable-job-id] a.job-card-container__link',
            ),
            ("css", 'li[data-occludable-job-id] a.job-card-container__link'),
            # Search-results URL with currentJobId=…
            ("css", 'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="currentJobId"]'),
            (
                "css",
                'li.scaffold-layout__list-item[data-occludable-job-id] a[href*="linkedin.com/jobs"]',
            ),
            ("css", 'li[data-occludable-job-id] a[href*="currentJobId"]'),
            ("css", 'li[data-occludable-job-id] a[href*="linkedin.com/jobs"]'),
            ("css", 'a.ember-view[href*="currentJobId"]'),
            ("css", 'a[class*="ember-view"][href*="currentJobId"]'),
            ("css", 'div.ember-view a[href*="currentJobId"]'),
            ("xpath", "//div[contains(@class,'ember-view')]//a[contains(@href,'currentJobId')]"),
            ("css", 'a[class*="job-card-job-posting-card-wrapper__card-link"]'),
            ("xpath", "//a[contains(@class,'ember-view') and contains(@href,'currentJobId')]"),
            ("xpath", "//a[contains(@class, 'job-card-job-posting-card-wrapper__card-link')]"),
            # Last resort: any /jobs/view/ link (may include detail pane — prefer scoped selectors above)
            ("css", 'a[href*="/jobs/view/"]'),
            ("css", 'a[href*="currentJobId"]'),
        ]
        for kind, sel in attempts:
            if kind == "css":
                links = driver.find_elements(By.CSS_SELECTOR, sel)
            else:
                links = driver.find_elements(By.XPATH, sel)
            if links:
                log.info("Matched %d job list link(s) via %s %r", len(links), kind, sel)
                return links
        return []

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
            return len(self._left_rail_job_links(driver))
        prev = -1
        best = 0
        for attempt in range(10):
            self._expand_virtualized_job_list(driver)
            self._scroll_job_list_to_top(driver)
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
        """The job row ``li`` (has ``data-occludable-job-id`` when using scaffold list)."""
        for xpath in (
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

    def _read_job_description_panel(self, driver) -> str:
        """
        Read ``.jobs-description__content`` after scrolling it — LinkedIn often lazy-loads sections
        (e.g. \"Requirements added by the job poster\") below the first viewport.
        """
        sel = SEL["job_description"]
        out = ""
        for attempt in range(2):
            if attempt:
                time.sleep(1.0)
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if not els:
                continue
            el = els[0]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", el)
                time.sleep(0.55)
            except Exception:
                pass
            try:
                out = (el.text or "").strip()
            except Exception:
                out = ""
            if out:
                break
        if not out:
            return ""
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", els[0])
                time.sleep(0.65)
                out = (els[0].text or "").strip() or out
            except Exception:
                pass
        return out

    def _peek_job_from_list_link(self, link) -> dict | None:
        """
        Read job id, title, company, location, Easy Apply from a **list card** without opening the job pane.

        LinkedIn often puts the company in the entity lockup (including ``span[dir='ltr']`` with obfuscated
        classes); the title may live inside the same row's job link.
        """
        try:
            href = (link.get_attribute("href") or "").strip()

            li_row = link
            jid_attr = ""
            try:
                li_row = self._list_item_for_link(link)
                jid_attr = (li_row.get_attribute("data-occludable-job-id") or "").strip()
            except Exception:
                pass

            if not href and not jid_attr:
                return None

            title = self._text_from_first_match(
                link,
                (SEL["job_card_title"], "strong"),
            )
            company = self._text_from_first_match(
                link,
                (SEL["job_card_company"], SEL["job_card_company_alt"]),
            )
            if not company:
                company = self._text_from_first_match(
                    li_row,
                    (
                        SEL["job_card_company_row"],
                        ".artdeco-entity-lockup__subtitle span[dir='ltr']",
                        ".artdeco-entity-lockup__subtitle span",
                        ".artdeco-entity-lockup__subtitle",
                    ),
                )
            loc = self._text_from_first_match(link, (SEL["job_card_location"],))
            if not loc:
                loc = self._text_from_first_match(
                    li_row,
                    (SEL["job_card_location_row"],),
                )

            if not title:
                title = (link.get_attribute("aria-label") or "").strip()
            if not title:
                parts = [p.strip() for p in (link.text or "").split("\n") if p.strip()]
                if len(parts) >= 1:
                    title = parts[0]
                if len(parts) >= 2 and not company:
                    company = parts[1]
                if len(parts) >= 3 and not loc:
                    loc = parts[2]

            easy_apply = self._easy_apply_near_card_link(link)

            job_id, full_url = job_id_and_view_url_from_href(href)
            if jid_attr:
                job_id = jid_attr
                full_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            elif not job_id:
                return None

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

    def _complete_job_after_peek(self, driver, link, peek: dict) -> dict | None:
        """Open the job card (detail pane), read description, and merge into ``peek``."""
        try:
            if self.highlight:
                focus_element(driver, link, pause=self.step_delay)
            link.click()
            self._pause()
            time.sleep(0.9)

            time.sleep(self.job_description_wait_seconds)
            description = self._read_job_description_panel(driver)

            return {**peek, "description": description}
        except Exception as e:
            log.debug("Error completing job after peek: %s", e)
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
        selectors: list[str] = []
        jid = (job_id or "").strip()
        if jid:
            selectors.extend(
                (
                    f'li[data-occludable-job-id="{jid}"] button[aria-label^="Dismiss "][aria-label$=" job"]',
                    f'li[data-occludable-job-id="{jid}"] button.job-card-container__action',
                )
            )
        selectors.extend(
            (
                # Fallback to active/selected row when no explicit id match is available.
                'li.scaffold-layout__list-item--active button[aria-label^="Dismiss "][aria-label$=" job"]',
                'li.jobs-search-results__list-item--active button[aria-label^="Dismiss "][aria-label$=" job"]',
                'li[aria-current="true"] button[aria-label^="Dismiss "][aria-label$=" job"]',
                # Last resort: any visible dismiss action button.
                'button.job-card-container__action[aria-label^="Dismiss "][aria-label$=" job"]',
                'button[class*="job-card-container__action"][aria-label*="Dismiss"][aria-label$=" job"]',
            )
        )

        btn = None
        for css in selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                matches = []
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

    def selected_job_company_link(self, driver) -> str:
        """
        Return the selected job's company LinkedIn URL from the detail pane, or empty string.
        """
        selectors = (
            ".job-details-jobs-unified-top-card__company-name a[href]",
            ".jobs-unified-top-card__company-name a[href]",
            "a[data-test-app-aware-link][href*='/company/']",
            "a[href*='linkedin.com/company/']",
        )
        for css in selectors:
            try:
                matches = driver.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                matches = []
            for el in matches:
                href = (el.get_attribute("href") or "").strip()
                if "linkedin.com/company/" in href:
                    return href
        return ""

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

    def parse_current_job_from_detail_pane(self, driver) -> dict | None:
        """
        Best-effort job dict from the **currently selected** listing (URL + right-hand detail pane).
        Used in manual/helper mode where the user clicks jobs instead of the automated pipeline.
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
        raise RuntimeError(
            f"Login did not complete within {max_seconds:.0f}s (first name / feed URL not detected)"
        )
