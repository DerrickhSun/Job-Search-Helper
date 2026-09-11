#!/usr/bin/env python3
"""
Test that the two-phase requirements fetch works correctly:

  Phase 1 — Search view (main driver):
    Navigate to the job search page with the job open in the two-pane detail panel.
    Read the description using _read_job_description_panel.
    Verify the job shown matches the expected job ID.
    Confirm requirements are NOT present (they are omitted by LinkedIn in this view).

  Phase 2 — Dedicated page fetch (lookup driver):
    Call fetch_dedicated_page_requirements, which navigates to /jobs/view/{id}/.
    Confirm requirements ARE returned.

Usage (from repo root):
  python scripts/test_requirements_scrape.py
  python scripts/test_requirements_scrape.py --job-id 4430237392
  python scripts/test_requirements_scrape.py --headless
"""

from __future__ import annotations

import argparse
import logging
import re
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
from utils.job_searcher import JobSearcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_JOB_ID = "4430237392"
# No f_C (company) filter here — it must work for any --job-id, not just one company's postings.
SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?currentJobId=4430237392&f_TPR=r604800"
    "&geoId=92000000&origin=JOB_SEARCH_PAGE_JOB_FILTER"
)
MARKER = "Requirements added by the job poster"


def _job_id_from_url(url: str) -> str:
    m = re.search(r"currentJobId=(\d+)", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/jobs/view/(\d+)", url, re.I)
    return m.group(1) if m else ""


def _print_section(title: str, text: str) -> None:
    print(f"\n{'=' * 60}")
    print(title)
    print("=" * 60)
    print(text or "(empty)")
    print("=" * 60)


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--job-id",
        default=DEFAULT_JOB_ID,
        help=f"LinkedIn job ID to test (default: {DEFAULT_JOB_ID}).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (default: visible window).",
    )
    args = parser.parse_args()

    searcher = JobSearcher(headless=args.headless, posted_within_24h=False)
    main_driver = build_chrome(headless=args.headless)
    lookup_driver = build_chrome(headless=args.headless)
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
        # ── Login both drivers ────────────────────────────────────────────────
        for label, drv in (("main", main_driver), ("lookup", lookup_driver)):
            load_cookies(drv, searcher.session_file)
            searcher._login(drv)
            log.info("%s driver: logged in.", label)

        # ── Phase 1: search view (main driver) ───────────────────────────────
        print("\n\nPHASE 1 — Search view (two-pane, main driver)")

        search_url = re.sub(r"currentJobId=\d+", f"currentJobId={args.job_id}", SEARCH_URL)
        log.info("Navigating to search URL: %s", search_url)
        main_driver.get(search_url)
        time.sleep(3.0)

        current_url = main_driver.current_url
        detected_id = _job_id_from_url(current_url)
        log.info("Current URL after navigation: %s", current_url)

        if detected_id == args.job_id:
            ok(f"Job ID in URL matches expected ({args.job_id}).")
        else:
            fail(
                f"Job ID mismatch — expected {args.job_id!r}, URL shows {detected_id!r}. "
                "LinkedIn may have redirected or the job listing has expired."
            )

        search_desc = searcher._read_job_description_panel(main_driver)
        _print_section("SEARCH VIEW DESCRIPTION", search_desc)

        if MARKER not in search_desc:
            ok("Requirements section is absent from search view (expected — LinkedIn omits it here).")
        else:
            fail(
                "Requirements section unexpectedly present in search view — "
                "LinkedIn may have changed their page structure."
            )

        # ── Phase 2: dedicated page fetch (lookup driver) ────────────────────
        print("\n\nPHASE 2 — Dedicated page fetch (lookup driver)")

        dedicated_url = f"https://www.linkedin.com/jobs/view/{args.job_id}/"
        log.info("Navigating lookup driver to: %s", dedicated_url)
        lookup_driver.get(dedicated_url)
        import time as _time; _time.sleep(3.0)
        panel_text = searcher._read_job_description_panel(lookup_driver)
        _print_section("DEDICATED PAGE — panel reader (SEL['job_description'])", panel_text)
        if MARKER in panel_text:
            ok("Panel reader already captures the requirements section (no XPath fallback needed).")
        else:
            print(f"  [INFO] Panel reader did not find {MARKER!r} — XPath fetch will be used.")

        req_text = searcher.fetch_dedicated_page_requirements(lookup_driver, args.job_id)
        _print_section("REQUIREMENTS FROM DEDICATED PAGE (fetch_dedicated_page_requirements)", req_text)

        if req_text:
            ok(f"fetch_dedicated_page_requirements returned requirements text.")
            req_lines = [
                line.strip()
                for line in req_text.splitlines()
                if line.strip().startswith("•") or "years of work experience" in line.lower()
            ]
            if req_lines:
                print("  Requirement lines detected:")
                for line in req_lines:
                    print(f"    {line}")
        else:
            fail(
                "fetch_dedicated_page_requirements returned empty — "
                "either the dedicated page has no requirements section or navigation failed."
            )

        if req_text and req_text not in search_desc:
            ok("Requirements text is new (not already in search view description).")
        elif req_text:
            fail("Requirements text was already present in the search view description — nothing to append.")

        # ── Summary ──────────────────────────────────────────────────────────
        print(f"\n{'=' * 60}")
        print(f"RESULT: {passed} passed, {failed} failed")
        print("=" * 60)

        save_cookies(main_driver, searcher.session_file)
        return 0 if failed == 0 else 1

    finally:
        quit_chrome(main_driver)
        quit_chrome(lookup_driver)


if __name__ == "__main__":
    raise SystemExit(main())
