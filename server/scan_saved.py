"""
Scan LinkedIn's My Jobs tracker (saved jobs) for external Apply destinations, detecting
Greenhouse/Ashby the same way ``main.py --filter`` does live -- backfills
``output/easy_apply_companies.json`` for jobs that were saved without ever going through that
detection (manually saved on LinkedIn directly, imported via the extension, or saved before this
feature existed).

Two drivers, like ``main.py``: the primary stays on the My Jobs tracker page, scrolling through
each page of saved jobs and clicking the tracker's own "Next" pagination button
(``NEXT_PAGE_BUTTON_CSS``) once scrolling stops turning up new rows -- the list is a fixed number
of rows per page plus pagination, not purely infinite-scroll; each newly-seen job's id is handed
off to a secondary Chrome worker (``utils/company_lookup_worker.py``) that opens the job via the
two-pane search view, clicks external Apply, and reads the destination
(``JobSearcher.fetch_job_apply_destination_by_id`` / ``selected_job_apply_destination_url``) --
kept on a second driver so the primary never has to navigate away from (and re-establish scroll/
page position on) the tracker for each check. A job is skipped without opening its page at all if
its company is already in ``easy_apply_company_memory`` (companies overwhelmingly stick to one
ATS -- the same assumption ``main.py`` already makes for live search results).

Run from inside server/::

    python scan_saved.py
    python scan_saved.py --headless
    python scan_saved.py --max-scans 50
    python scan_saved.py --scroll-pause 2.5

Verified live: the redirect-destination read (``selected_job_apply_destination_url``, via the
two-pane search view rather than the dedicated single-job page, which does not expose the same
apply button) and pagination (the tracker's "Next" button, selected by its stable
``data-testid`` rather than LinkedIn's own hashed/regenerated class names) were both confirmed
against real saved jobs after each was found to have originally been wrong -- see git history for
what each looked like before. Row-extraction itself mirrors the browser extension's own
highlighting code (``extension/content_scripts/content.js::extractCompanyFromTrackerLine`` /
``highlightEasyApplyJobs``), which is already shipped and live-verified.
"""

from __future__ import annotations

import argparse
import logging
import re

from dotenv import load_dotenv
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from utils.chrome_driver import (
    DEFAULT_COOKIE_PATH,
    build_chrome,
    driver_session_alive,
    interruptible_sleep,
    load_cookies,
    quit_chrome,
    save_cookies,
)
from utils.company_lookup_worker import CompanyLookupWorker
from utils.eval_utils.easy_apply_company_memory import (
    DEFAULT_EASY_APPLY_MEMORY_PATH,
    EasyApplyCompanyMemory,
    detect_easy_apply_service,
    load_easy_apply_company_memory,
)
from utils.job_searcher import JobSearcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

JOBS_TRACKER_URL = "https://www.linkedin.com/jobs-tracker/"
JOB_LINK_CSS = 'a[href*="/jobs/view/"]'
JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")

# data-testid is stable; LinkedIn's own class names on this button are hashed/generated and
# regenerate across deploys, so they're not safe to select on.
NEXT_PAGE_BUTTON_CSS = 'button[data-testid="pagination-controls-next-button-visible"]'

# Marks a tracker row's link once its (job_id, title, company) have been read this run, so
# re-scanning after each scroll (needed to discover newly lazy-loaded rows) doesn't re-check ones
# already handled.
_SCANNED_ATTR = "data-jobapplyer-scan-checked"


def _extract_company_from_tracker_line(text: str) -> str:
    """Tracker rows read "Company · Location" (or "... (Remote)") in one line -- mirrors
    extension/content_scripts/content.js::extractCompanyFromTrackerLine."""
    return (text or "").split("·")[0].strip()


def _read_new_tracker_rows(driver) -> list[tuple[str, str, str]]:
    """(job_id, title, company) for tracker rows not yet marked as read this run."""
    rows: list[tuple[str, str, str]] = []
    for link in driver.find_elements(By.CSS_SELECTOR, JOB_LINK_CSS):
        try:
            if link.get_attribute(_SCANNED_ATTR):
                continue
            href = link.get_attribute("href") or ""
            m = JOB_ID_RE.search(href)
            if not m:
                continue
            paragraphs = link.find_elements(By.TAG_NAME, "p")
            if len(paragraphs) < 2:
                continue
            title = (paragraphs[0].text or "").strip()
            company = _extract_company_from_tracker_line(paragraphs[1].text)
            driver.execute_script(f"arguments[0].setAttribute('{_SCANNED_ATTR}', '1');", link)
            if company:
                rows.append((m.group(1), title, company))
        except StaleElementReferenceException:
            continue
    return rows


def _click_next_page(driver) -> bool:
    """Click the tracker's pagination "Next" button. False if it's absent/disabled (last page)
    or the click failed -- the list is paginated (a fixed set of rows per page plus a Next
    button), not purely infinite-scroll, so this has to run whenever scrolling stops turning up
    new rows."""
    try:
        buttons = driver.find_elements(By.CSS_SELECTOR, NEXT_PAGE_BUTTON_CSS)
    except WebDriverException:
        return False
    for btn in buttons:
        try:
            if not btn.is_displayed() or not btn.is_enabled():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            try:
                btn.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", btn)
            return True
        except (StaleElementReferenceException, WebDriverException):
            continue
    return False


def scan_saved_jobs(
    primary_driver,
    worker: CompanyLookupWorker,
    memory: EasyApplyCompanyMemory,
    *,
    max_scans: int | None,
    scroll_pause: float,
    idle_rounds_before_stop: int = 3,
) -> tuple[int, int]:
    """
    Scroll through the tracker, checking each newly-seen saved job's external apply destination.

    Returns ``(checked, remembered)`` counts.
    """
    checked = 0
    remembered = 0
    idle_rounds = 0

    while driver_session_alive(primary_driver):
        if max_scans is not None and checked >= max_scans:
            log.info("Reached --max-scans cap (%d).", max_scans)
            break

        rows = _read_new_tracker_rows(primary_driver)
        if not rows:
            idle_rounds += 1
            if idle_rounds < idle_rounds_before_stop:
                try:
                    primary_driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                except WebDriverException:
                    break
                if not interruptible_sleep(scroll_pause, primary_driver):
                    break
                continue

            # Scrolling stopped turning up anything new on this page -- try advancing to the
            # next page before concluding we've actually reached the end of the saved list.
            if _click_next_page(primary_driver):
                log.info("End of page reached — advanced via the tracker's \"Next\" button.")
                idle_rounds = 0
                if not interruptible_sleep(scroll_pause, primary_driver):
                    break
                continue

            log.info(
                "No new saved jobs and no further pages after %d idle scroll(s) — reached the "
                "end of the list.",
                idle_rounds,
            )
            break

        idle_rounds = 0
        for job_id, title, company in rows:
            if max_scans is not None and checked >= max_scans:
                break
            checked += 1

            existing = memory.lookup(slug=None, company_display=company)
            if existing:
                log.info(
                    "[%d] %r at %r — already known (%s), skipping page visit.",
                    checked, title, company, existing.get("service"),
                )
                continue

            log.info("[%d] %r at %r — checking apply destination…", checked, title, company)
            dest = worker.submit_apply_destination(job_id)
            service = detect_easy_apply_service(dest) if dest else None
            if service:
                memory.remember(slug=None, company_display=company, service=service)
                remembered += 1
                log.info("  -> %s (remembered)", service)
            else:
                log.info("  -> not Greenhouse/Ashby (or Easy Apply / no external destination)")

        try:
            primary_driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except WebDriverException:
            break
        if not interruptible_sleep(scroll_pause, primary_driver):
            break

    return checked, remembered


def run(*, headless: bool, max_scans: int | None, scroll_pause: float) -> tuple[int, int]:
    searcher = JobSearcher(headless=headless, session_file=DEFAULT_COOKIE_PATH)
    memory = load_easy_apply_company_memory(DEFAULT_EASY_APPLY_MEMORY_PATH)

    primary_driver = build_chrome(headless=headless)
    worker = CompanyLookupWorker(
        searcher=searcher, headless=headless, session_file=DEFAULT_COOKIE_PATH
    )
    try:
        load_cookies(primary_driver, DEFAULT_COOKIE_PATH)
        searcher._login(primary_driver)

        log.info("Navigating to %s", JOBS_TRACKER_URL)
        primary_driver.get(JOBS_TRACKER_URL)
        if not interruptible_sleep(2.0, primary_driver):
            return 0, 0

        if "jobs-tracker" not in (primary_driver.current_url or "").lower():
            log.warning("Unexpected URL after navigation: %s", primary_driver.current_url)

        checked, remembered = scan_saved_jobs(
            primary_driver,
            worker,
            memory,
            max_scans=max_scans,
            scroll_pause=scroll_pause,
        )
        save_cookies(primary_driver, DEFAULT_COOKIE_PATH)
        return checked, remembered
    finally:
        worker.stop()
        quit_chrome(primary_driver)


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(
        description="Scan LinkedIn's My Jobs tracker for Greenhouse/Ashby apply destinations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--headless", action="store_true", help="Run both Chrome windows headless.")
    ap.add_argument(
        "--max-scans",
        type=int,
        default=0,
        help="Stop after checking this many saved jobs (0 = no cap).",
    )
    ap.add_argument(
        "--scroll-pause",
        type=float,
        default=1.5,
        help="Seconds to wait after each scroll for lazy-loaded rows (default: 1.5).",
    )
    args = ap.parse_args()

    cap = None if args.max_scans <= 0 else int(args.max_scans)
    try:
        checked, remembered = run(
            headless=args.headless,
            max_scans=cap,
            scroll_pause=max(0.2, float(args.scroll_pause)),
        )
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130

    log.info("Done — checked %d saved job(s), recorded %d as Greenhouse/Ashby.", checked, remembered)
    print(
        f"Checked {checked} saved job(s); recorded {remembered} new "
        f"Greenhouse/Ashby compan{'y' if remembered == 1 else 'ies'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
