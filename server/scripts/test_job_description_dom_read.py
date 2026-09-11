#!/usr/bin/env python3
"""
Confirm job descriptions are fully readable from the DOM without clicking "…more".

Opens a LinkedIn job in the two-pane search UI, then compares:

  1. Selenium ``el.text``          — often truncated by LinkedIn's CSS line-clamp
  2. ``JobSearcher._dom_text``     — ``innerText`` / ``textContent`` (current reader)
  3. ``_read_job_description_panel`` — full panel reader used by the bot
  4. Optional ground truth: click "…more" **only inside About-the-job roots**, then
     re-read ``_dom_text`` (must not touch trending-employee feed cards)

PASS when (2)/(3) are at least as long as (1), and (when expandable) match (4)
within a small length tolerance.

Usage (from inside server/)::

  python scripts/test_job_description_dom_read.py
  python scripts/test_job_description_dom_read.py --job-id 4450141052
  python scripts/test_job_description_dom_read.py --keywords "software engineer"
  python scripts/test_job_description_dom_read.py --preview-only
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

from selenium.webdriver.common.by import By

from utils.chrome_driver import build_chrome, load_cookies, quit_chrome, save_cookies
from utils.job_searcher import DEFAULT_JOB_SEARCH_KEYWORDS, JobSearcher, SEL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _preview(text: str, n: int = 240) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def _print_full(title: str, text: str) -> None:
    print(f"\n{'=' * 60}")
    print(title)
    print("=" * 60)
    print(text or "(empty)")
    print("=" * 60)


def _safe_expand_in_job_roots_only(searcher: JobSearcher, driver) -> int:
    """
    Click job-description "…more" toggles only under About-the-job roots.
    Returns how many buttons were clicked (0 if none / none safe).
    """
    roots = searcher._job_description_roots(driver)
    if not roots:
        return 0
    clicked = 0
    for root in roots:
        try:
            btns = root.find_elements(
                By.CSS_SELECTOR, 'button[data-testid="expandable-text-button"]'
            )
        except Exception:
            continue
        for btn in btns:
            try:
                if searcher._element_inside_feed_or_profile_link(btn):
                    continue
                driver.execute_script("arguments[0].click();", btn)
                clicked += 1
            except Exception:
                continue
    if clicked:
        time.sleep(0.6)
    return clicked


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--job-id",
        default="",
        help="Open this job via currentJobId= on a search URL (optional).",
    )
    parser.add_argument(
        "--keywords",
        default=DEFAULT_JOB_SEARCH_KEYWORDS[0],
        help=f"Search keywords when --job-id is omitted (default: {DEFAULT_JOB_SEARCH_KEYWORDS[0]!r}).",
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
        default=4.0,
        metavar="SEC",
        help="Seconds to wait after navigation before reading (default: 4).",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Print 240-char previews instead of the full description text.",
    )
    args = parser.parse_args()

    searcher = JobSearcher(headless=args.headless, posted_within_24h=False)
    driver = build_chrome(headless=args.headless)
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
            query = searcher._jobs_search_query(
                args.keywords, args.location, easy_apply_only=True
            )
            url = f"https://www.linkedin.com/jobs/search/?{query}"

        log.info("Navigating to: %s", url)
        driver.get(url)
        time.sleep(args.wait)

        # Ensure a job is selected in the right pane.
        if not args.job_id:
            links = searcher._find_job_card_links(driver, expand=False)
            if not links:
                fail("No job cards found on the search page.")
                return 1
            peek = searcher._peek_job_from_list_link(links[0]) or {}
            jid = str(peek.get("id") or "")
            log.info("Opening first card: id=%s title=%r", jid, peek.get("title"))
            if not searcher._click_job_list_card(driver, links[0]):
                fail("Could not click first job card.")
                return 1
            time.sleep(max(1.0, searcher.job_description_wait_seconds))
        else:
            time.sleep(max(1.0, searcher.job_description_wait_seconds))

        el = searcher._find_job_description_element(driver)
        if el is None:
            roots = searcher._job_description_roots(driver)
            el = roots[0] if roots else None
        if el is None:
            fail(
                "No job description element found "
                f"(roots={SEL['job_description_root']!r}, desc={SEL['job_description']!r})."
            )
            return 1

        try:
            visible = (el.text or "").strip()
        except Exception:
            visible = ""
        dom_before = searcher._dom_text(driver, el)
        panel = searcher._read_job_description_panel(driver)

        print("\n" + "=" * 60)
        print("LENGTHS")
        print("=" * 60)
        print(f"  Selenium .text (visible/clamped): {len(visible):6d} chars")
        print(f"  _dom_text (before any expand):    {len(dom_before):6d} chars")
        print(f"  _read_job_description_panel:      {len(panel):6d} chars")
        if args.preview_only:
            print("\nPREVIEW (_dom_text):")
            print(f"  {_preview(dom_before)}")
            print("\nPREVIEW (panel reader):")
            print(f"  {_preview(panel)}")
        else:
            _print_full("FULL TEXT — Selenium .text (visible/clamped)", visible)
            _print_full("FULL TEXT — _dom_text (before any expand)", dom_before)
            _print_full("FULL TEXT — _read_job_description_panel", panel)

        if not dom_before:
            fail("_dom_text returned empty.")
        else:
            ok(f"_dom_text returned {len(dom_before)} chars.")

        if not panel:
            fail("_read_job_description_panel returned empty.")
        else:
            ok(f"_read_job_description_panel returned {len(panel)} chars.")

        if visible and len(dom_before) + 20 < len(visible):
            fail(
                f"_dom_text shorter than visible .text "
                f"({len(dom_before)} < {len(visible)}) — unexpected."
            )
        elif visible and len(dom_before) > len(visible) + 40:
            ok(
                f"_dom_text is longer than visible .text "
                f"({len(dom_before)} > {len(visible)}) — clamp bypass looks healthy."
            )
        elif visible:
            ok(
                f"_dom_text length >= visible .text "
                f"({len(dom_before)} vs {len(visible)})."
            )
        else:
            ok("Visible .text was empty; skipped clamp comparison.")

        # Ground truth: expand only inside job roots, then re-read.
        clicked = _safe_expand_in_job_roots_only(searcher, driver)
        el2 = searcher._find_job_description_element(driver) or el
        dom_after = searcher._dom_text(driver, el2)
        print("\n" + "=" * 60)
        print(f"AFTER SAFE EXPAND (clicked {clicked} job-root …more button(s))")
        print("=" * 60)
        print(f"  _dom_text (after expand):         {len(dom_after):6d} chars")
        if args.preview_only:
            print(f"  preview: {_preview(dom_after)}")
        else:
            _print_full("FULL TEXT — _dom_text (after safe expand)", dom_after)

        if clicked == 0:
            ok("No job-root …more button found (nothing to expand) — DOM read stands alone.")
        else:
            # Allow small whitespace / button-label differences after expand.
            if abs(len(dom_after) - len(dom_before)) <= max(30, int(0.02 * max(len(dom_after), 1))):
                ok(
                    f"Pre-expand _dom_text matches post-expand length "
                    f"({len(dom_before)} ≈ {len(dom_after)}) — full text was already in the DOM."
                )
            elif len(dom_before) >= len(dom_after) - 30:
                ok(
                    f"Pre-expand _dom_text already had the content "
                    f"({len(dom_before)} vs after {len(dom_after)})."
                )
            else:
                fail(
                    f"Post-expand text is substantially longer "
                    f"({len(dom_after)} vs before {len(dom_before)}). "
                    f"LinkedIn may be truncating the DOM until expand — "
                    f"revisit the reader."
                )

        # Sanity: we must not have navigated to a feed post.
        cur = (driver.current_url or "").lower()
        if "/feed/" in cur or "urn:li:activity" in cur:
            fail(f"Browser left jobs context after expand (url={cur[:160]}).")
        else:
            ok("Still on a jobs URL after the test.")

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
