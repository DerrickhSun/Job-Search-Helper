#!/usr/bin/env python3
"""
Manual test: reach LinkedIn's "We found more results related to your search..." divider quickly,
and verify JobSearcher stops there instead of walking into the loosely-related suggestions past it.

Narrows results with the Remote + "Under 10 applicants" quick filters (on top of Easy Apply) so a
small, easily-exhausted result set reaches the divider fast. This script is read-only — it never
opens or applies to a job, it only scrolls/counts the list, the same primitives
run_search_apply_pipeline uses internally:
  - JobSearcher._expand_virtualized_job_list  (stops early once the divider is visible)
  - JobSearcher._more_related_results_marker_present
  - JobSearcher._find_job_card_links

Usage (from repo root):
  python scripts/test_more_related_results_marker.py
  python scripts/test_more_related_results_marker.py --keywords "underwater basket weaving"
  python scripts/test_more_related_results_marker.py --headless
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

from utils.chrome_driver import build_chrome, load_cookies, quit_chrome, save_cookies  # noqa: E402
from utils.job_searcher import DEFAULT_JOB_SEARCH_KEYWORDS, JobSearcher  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Best-effort text/aria-label match for a quick-filter pill or dropdown option. LinkedIn's filter
# bar classes are hashed/unstable (same SDUI pattern as everywhere else this repo has had to work
# around) — matching by what a user would actually read is the only thing likely to still work
# next month. If a filter opens a sub-menu (e.g. a workplace-type dropdown with its own "Remote"
# checkbox + "Show results" button) rather than toggling directly, the first click likely only
# opens that menu — this script re-checks and, if the filter still isn't confirmed active, pauses
# for you to finish it by hand rather than guessing at a multi-step flow blindly.
_FIND_AND_CLICK_JS = """
const needle = arguments[0].toLowerCase();
const candidates = document.querySelectorAll(
  'button, [role="button"], [role="radio"], [role="checkbox"], a, label'
);
for (const el of candidates) {
  if (!el.offsetParent && el.getClientRects().length === 0) continue;  // skip hidden
  const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.textContent || '')).trim().toLowerCase();
  if (label.includes(needle)) {
    el.scrollIntoView({block: 'center'});
    el.click();
    return true;
  }
}
return false;
"""


def _try_click_filter_by_text(driver, needle: str) -> bool:
    try:
        clicked = bool(driver.execute_script(_FIND_AND_CLICK_JS, needle))
    except Exception as e:
        log.debug("Filter click attempt for %r raised: %s", needle, e)
        return False
    if clicked:
        log.info("Clicked a filter control matching %r.", needle)
        time.sleep(1.0)
    else:
        log.warning("Could not find a visible filter control matching %r.", needle)
    return clicked


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Test the 'more related results' divider stopping logic against a narrowed search."
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
        help="Run Chrome headless (default: visible window — recommended so you can confirm the "
        "filters actually applied and watch it reach the divider).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=8.0,
        metavar="SEC",
        help="Seconds to wait after navigation before applying filters (default: 8).",
    )
    parser.add_argument(
        "--max-cards",
        type=int,
        default=200,
        metavar="N",
        help="Safety cap on cards counted before giving up if the divider is never found (default: 200).",
    )
    args = parser.parse_args()

    searcher = JobSearcher(headless=args.headless, posted_within_24h=False)
    driver = build_chrome(headless=args.headless)
    try:
        load_cookies(driver, searcher.session_file)
        searcher._login(driver)

        query = searcher._jobs_search_query(args.keywords, args.location, easy_apply_only=True)
        url = f"https://www.linkedin.com/jobs/search/?{query}"
        log.info("Opening Easy-Apply-filtered search: %s", url)
        driver.get(url)
        time.sleep(args.wait)

        mode = searcher._detect_job_search_mode(driver)
        if mode == "raw":
            log.warning(
                "LinkedIn search mode: raw/classic — the 'more related results' divider only "
                "appears in AI-assisted mode, so this test cannot pass until the session is "
                "switched (there's a 'Try AI job search' button on the page)."
            )
        elif mode == "ai_assisted":
            log.info("LinkedIn search mode: AI-assisted — divider should appear once results run out.")
        else:
            log.info("LinkedIn search mode: could not be determined from the page.")

        remote_ok = _try_click_filter_by_text(driver, "remote")
        applicants_ok = _try_click_filter_by_text(driver, "under 10 applicants")

        if not (remote_ok and applicants_ok):
            print(
                "\nCouldn't auto-click one or both filters (Remote / Under 10 applicants) — "
                "the current LinkedIn markup for them isn't what this script guessed.\n"
                "Please apply them by hand in the browser now (and click Show results if either "
                "opened a dropdown), then press Enter here to continue…",
                flush=True,
            )
            input()
        else:
            print(
                "\nFilters clicked — glance at the browser to confirm Remote + Under 10 applicants "
                "are actually active (and click Show results if a dropdown is still open), "
                "then press Enter to start scanning the list…",
                flush=True,
            )
            input()

        log.info("Scanning job list for the 'more related results' divider (read-only, no applies)...")
        seen_marker = False
        rounds = 0
        while True:
            rounds += 1
            searcher._expand_virtualized_job_list(driver)
            searcher._scroll_job_list_to_top(driver)
            n = len(searcher._find_job_card_links(driver, expand=False))
            marker_now = searcher._more_related_results_marker_present(driver)
            log.info(
                "Round %d: %d card(s) in the DOM, divider present=%s", rounds, n, marker_now
            )
            if marker_now:
                seen_marker = True
                break
            if n >= args.max_cards:
                log.warning(
                    "Hit safety cap of %d cards without ever seeing the divider — either the "
                    "filters didn't narrow things enough, or the divider text/selector no longer "
                    "matches. Inspect the browser.",
                    args.max_cards,
                )
                break
            if not searcher._has_next_page(driver):
                log.warning(
                    "No Next page and no divider seen — search may have run out of real results "
                    "without LinkedIn showing the divider at all for this query."
                )
                break
            # Not expected in practice for a single narrowed page, but stay honest about it.
            log.info("More pages available and no divider yet — this script only scans one page.")
            break

        print()
        if seen_marker:
            print(f"PASS — divider detected after {rounds} round(s), {n} card(s) counted before it.")
        else:
            print(f"NOT CONFIRMED — divider was not detected within {rounds} round(s)/{n} card(s).")
        print(
            "\nInspect the browser to sanity-check the count against what's actually shown.\n"
            "Press Enter to close Chrome…",
            flush=True,
        )
        input()

        save_cookies(driver, searcher.session_file)
        return 0 if seen_marker else 1
    finally:
        quit_chrome(driver)


if __name__ == "__main__":
    raise SystemExit(main())
