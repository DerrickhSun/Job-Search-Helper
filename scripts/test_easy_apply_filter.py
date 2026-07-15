#!/usr/bin/env python3
"""
Manual test: open LinkedIn job search **without** Easy Apply, then enable the filter via UI.

Usage (from repo root):
  python scripts/test_easy_apply_filter.py
  python scripts/test_easy_apply_filter.py --keywords "software engineer" --location "United States"
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.parse
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

from utils.chrome_driver import build_chrome, load_cookies, quit_chrome, save_cookies  # noqa: E402
from utils.job_searcher import DEFAULT_JOB_SEARCH_KEYWORDS, JobSearcher  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Test LinkedIn Easy Apply filter recovery (non-EA search → enable filter)."
    )
    parser.add_argument(
        "--keywords",
        default=DEFAULT_JOB_SEARCH_KEYWORDS[0],
        help=f"Search keywords (default: {DEFAULT_JOB_SEARCH_KEYWORDS[0]!r}).",
    )
    parser.add_argument(
        "--location",
        default="United States",
        help='Search location (default: "United States").',
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (default: visible window).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=8.0,
        metavar="SEC",
        help="Seconds to wait after navigation before applying filter (default: 8).",
    )
    args = parser.parse_args()

    searcher = JobSearcher(headless=args.headless, posted_within_24h=False)
    driver = build_chrome(headless=args.headless)
    try:
        load_cookies(driver, searcher.session_file)
        searcher._login(driver)

        query = searcher._jobs_search_query(args.keywords, args.location, easy_apply_only=False)
        url = f"https://www.linkedin.com/jobs/search/?{query}"
        log = logging.getLogger(__name__)
        log.info("Opening search WITHOUT Easy Apply: %s", url)
        driver.get(url)
        time.sleep(args.wait)

        before_url = driver.current_url
        before_active = searcher.easy_apply_filter_active(driver)
        log.info("Before recovery: easy_apply_filter_active=%s", before_active)
        log.info("Before recovery URL: %s", before_url)

        if before_active:
            log.warning(
                "Easy Apply filter already appears active — URL may still include f_AL from a redirect. "
                "Inspect the browser; recovery will still run if the pill is off."
            )

        ok = searcher.recover_easy_apply_filter(driver)

        after_url = driver.current_url
        after_active = searcher.easy_apply_filter_active(driver)
        log.info("After recovery: easy_apply_filter_active=%s (recover returned %s)", after_active, ok)
        log.info("After recovery URL: %s", after_url)
        log.info("Recovery attempts this run: %d", searcher.easy_apply_filter_recoveries)

        parsed = urllib.parse.urlparse(after_url)
        log.info("Query params: %s", urllib.parse.parse_qs(parsed.query))

        print(
            "\nInspect the browser — Easy Apply filter should be on and results should refresh.\n"
            "Press Enter to close Chrome…",
            flush=True,
        )
        input()

        save_cookies(driver, searcher.session_file)
        return 0 if ok and after_active else 1
    finally:
        quit_chrome(driver)


if __name__ == "__main__":
    raise SystemExit(main())
