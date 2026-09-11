#!/usr/bin/env python3
"""
Confirm ``JobSearcher.selected_job_apply_destination_url`` actually works against a real,
non-Easy-Apply LinkedIn posting.

The external "Apply" control has no ``href`` at all -- LinkedIn opens the destination in a new tab
via JS on click, so this has to click the button, catch the new tab, read its URL, and close it
again. This script exercises that against the live site and reports what happened at each step,
since there's no way to verify real LinkedIn DOM behavior without an actual browser session.

Run with a VISIBLE (non-headless) window so the click/new-tab/close sequence is watchable.

Usage (from inside server/)::

    python scripts/test_apply_destination_read.py
    python scripts/test_apply_destination_read.py --job-id 4442779372
    python scripts/test_apply_destination_read.py --keywords "software engineer"
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

from utils.chrome_driver import build_chrome, load_cookies, quit_chrome, save_cookies
from utils.job_searcher import DEFAULT_JOB_SEARCH_KEYWORDS, JobSearcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--job-id", default="", help="Open this job via currentJobId= (optional).")
    parser.add_argument(
        "--keywords", default=DEFAULT_JOB_SEARCH_KEYWORDS[0],
        help=f"Search keywords when --job-id is omitted (default: {DEFAULT_JOB_SEARCH_KEYWORDS[0]!r}).",
    )
    parser.add_argument("--location", default="United States", help='Search location (default: "United States").')
    parser.add_argument("--wait", type=float, default=4.0, metavar="SEC", help="Seconds to wait after navigation (default: 4).")
    parser.add_argument("--timeout", type=float, default=5.0, metavar="SEC", help="selected_job_apply_destination_url's timeout_seconds (default: 5).")
    args = parser.parse_args()

    searcher = JobSearcher(headless=False, posted_within_24h=False)
    driver = build_chrome(headless=False)
    passed = 0
    failed = 0

    def ok(msg: str) -> None:
        nonlocal passed
        passed += 1
        print(f"  [PASS] {msg}")

    def fail(msg: str) -> None:
        nonlocal failed
        failed += 1
        print(f"  [FAIL] {msg}")

    try:
        load_cookies(driver, searcher.session_file)
        searcher._login(driver)

        if args.job_id:
            url = (
                "https://www.linkedin.com/jobs/search/"
                f"?currentJobId={args.job_id}&f_TPR=r604800"
                "&geoId=92000000&origin=JOB_SEARCH_PAGE_JOB_FILTER"
            )
        else:
            query = searcher._jobs_search_query(args.keywords, args.location, easy_apply_only=False)
            url = f"https://www.linkedin.com/jobs/search/?{query}"

        log.info("Navigating to: %s", url)
        driver.get(url)
        time.sleep(args.wait)

        peek = None
        if not args.job_id:
            links = searcher._find_job_card_links(driver, expand=False)
            if not links:
                fail("No job cards found on the search page.")
                return 1
            # Find the first non-Easy-Apply card -- that's the only kind with this button.
            for link in links:
                candidate = searcher._peek_job_from_list_link(link) or {}
                if not candidate.get("easy_apply"):
                    peek = candidate
                    log.info("Opening first non-Easy-Apply card: id=%s title=%r", peek.get("id"), peek.get("title"))
                    if not searcher._click_job_list_card(driver, link):
                        fail("Could not click the job card.")
                        return 1
                    break
            if peek is None:
                fail("No non-Easy-Apply card found on this page — try different --keywords, or pass --job-id.")
                return 1
            time.sleep(max(1.0, searcher.job_description_wait_seconds))
        else:
            time.sleep(max(1.0, searcher.job_description_wait_seconds))

        original_url = (driver.current_url or "").strip()
        original_handle_count = len(driver.window_handles)
        print("\n" + "=" * 60)
        print("BEFORE: reading the apply destination")
        print("=" * 60)
        print(f"  current_url:          {original_url}")
        print(f"  open window handles:  {original_handle_count}")

        t0 = time.time()
        dest = searcher.selected_job_apply_destination_url(driver, timeout_seconds=args.timeout)
        elapsed = time.time() - t0

        print("\n" + "=" * 60)
        print("RESULT")
        print("=" * 60)
        print(f"  destination URL:      {dest or '(empty — nothing found)'}")
        print(f"  round-trip time:      {elapsed:.2f}s")

        if dest:
            ok(f"Got a destination URL: {dest}")
            if "linkedin.com" in dest.lower():
                fail("Destination still looks like a linkedin.com URL — should have been filtered out.")
            else:
                ok("Destination does not look like a LinkedIn-hosted URL.")
        else:
            fail(
                "No destination URL was returned — check the button selector "
                "(#jobs-apply-button-id) still matches, or increase --timeout."
            )

        cur = (driver.current_url or "").strip()
        handles = driver.window_handles
        print(f"\n  current_url after:    {cur}")
        print(f"  open window handles:  {len(handles)}")

        if len(handles) == original_handle_count:
            ok(f"Window handle count restored to {original_handle_count} (no leaked tab).")
        else:
            fail(f"Window handle count changed ({original_handle_count} -> {len(handles)}) — a tab may have leaked.")

        if cur == original_url:
            ok("Back on the original job listing URL.")
        else:
            fail(f"Current URL changed ({original_url!r} -> {cur!r}) — focus did not restore cleanly.")

        print(f"\nResult: {passed} passed, {failed} failed.")
        return 0 if failed == 0 else 1
    finally:
        try:
            save_cookies(driver, searcher.session_file)
        except Exception:
            pass
        quit_chrome(driver)


if __name__ == "__main__":
    raise SystemExit(main())
