"""
Unsave all jobs on LinkedIn's My Jobs tracker — no S3, no applying.

Opens https://www.linkedin.com/jobs-tracker/ in Chrome (reuses saved LinkedIn cookies / login flow),
then for each saved job: overflow menu → Unsave. The page reloads after each
unsave, so the script always targets the first job and waits for reload — no scrolling.

Run from repo root::

    python cleanup.py
    python cleanup.py --headless
    python cleanup.py --max-unsaves 50
"""

from __future__ import annotations

import argparse
import logging
import time

from dotenv import load_dotenv
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from utils.chrome_driver import (
    DEFAULT_COOKIE_PATH,
    build_chrome,
    driver_session_alive,
    focus_element,
    interruptible_sleep,
    load_cookies,
    save_cookies,
    scroll_into_view,
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
DEFAULT_RELOAD_DELAY = 2.0

OVERFLOW_MENU_CSS = 'button[aria-label="Overflow menu"]'
MENU_CSS = '[role="menu"]'
UNSAVE_XPATHS = (
    "//div[@role='menu']//p[normalize-space()='Unsave']/ancestor::div[@role='button'][1]",
    "//div[@role='menu']//*[normalize-space()='Unsave']/ancestor::div[@role='button'][1]",
    "//div[@role='menu']//*[@role='menuitem' and contains(normalize-space(), 'Unsave')]",
)


def _visible_elements(driver, css: str) -> list:
    out = []
    for el in driver.find_elements(By.CSS_SELECTOR, css):
        try:
            if el.is_displayed() and el.is_enabled():
                out.append(el)
        except StaleElementReferenceException:
            continue
    return out


def _click(driver, el, *, highlight: bool = False, pause: float = 0.35) -> None:
    scroll_into_view(driver, el)
    if highlight:
        focus_element(driver, el, pause=pause)
    try:
        el.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", el)


def _dismiss_open_menu(driver) -> None:
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(0.15)


def _find_unsave_in_open_menu(driver):
    for xp in UNSAVE_XPATHS:
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
    return None


def _wait_for_menu(driver, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _visible_elements(driver, MENU_CSS):
            if _find_unsave_in_open_menu(driver) is not None:
                return True
        time.sleep(0.15)
    return False


def _unsave_first_visible_job(driver, *, highlight: bool, step_delay: float) -> bool:
    """Open the first overflow menu on the page and click Unsave. Returns True on success."""
    menus = _visible_elements(driver, OVERFLOW_MENU_CSS)
    if not menus:
        return False

    btn = menus[0]
    _click(driver, btn, highlight=highlight, pause=step_delay)
    if not _wait_for_menu(driver):
        log.warning("Overflow menu opened but Unsave was not found — closing menu.")
        _dismiss_open_menu(driver)
        return False

    unsave = _find_unsave_in_open_menu(driver)
    if unsave is None:
        _dismiss_open_menu(driver)
        return False

    _click(driver, unsave, highlight=highlight, pause=step_delay)
    if step_delay > 0:
        time.sleep(step_delay)
    return True


def unsave_all_saved_jobs(
    driver,
    *,
    max_unsaves: int | None = None,
    highlight: bool = True,
    step_delay: float = 0.35,
    reload_delay: float = DEFAULT_RELOAD_DELAY,
) -> int:
    """
    Repeatedly unsave the first saved job on the tracker until none remain.

    LinkedIn reloads the tracker after each unsave, so we always click the first
    overflow menu and wait for the page to settle before the next attempt.

    Returns the number of jobs unsaved.
    """
    unsaved = 0

    while driver_session_alive(driver):
        if max_unsaves is not None and unsaved >= max_unsaves:
            log.info("Reached --max-unsaves cap (%d).", max_unsaves)
            break

        if not _visible_elements(driver, OVERFLOW_MENU_CSS):
            log.info("No more saved jobs found on the tracker page.")
            break

        if _unsave_first_visible_job(driver, highlight=highlight, step_delay=step_delay):
            unsaved += 1
            log.info("Unsaved job %d — waiting for page reload.", unsaved)
            if not interruptible_sleep(reload_delay, driver):
                break
            continue

        log.warning("Could not unsave the first job — stopping.")
        break

    return unsaved


def run(
    *,
    headless: bool,
    step_delay: float,
    reload_delay: float,
    max_unsaves: int | None,
    no_highlight: bool,
) -> int:
    searcher = JobSearcher(
        headless=headless,
        session_file=DEFAULT_COOKIE_PATH,
        step_delay=step_delay,
        highlight=not no_highlight,
    )
    driver = build_chrome(headless=headless)
    try:
        load_cookies(driver, DEFAULT_COOKIE_PATH)
        searcher._login(driver)

        log.info("Navigating to %s", JOBS_TRACKER_URL)
        driver.get(JOBS_TRACKER_URL)
        if not interruptible_sleep(2.0, driver):
            return 0

        if "jobs-tracker" not in (driver.current_url or "").lower():
            log.warning("Unexpected URL after navigation: %s", driver.current_url)

        count = unsave_all_saved_jobs(
            driver,
            max_unsaves=max_unsaves,
            highlight=searcher.highlight,
            step_delay=step_delay,
            reload_delay=reload_delay,
        )
        save_cookies(driver, DEFAULT_COOKIE_PATH)
        return count
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(
        description="Unsave all jobs on LinkedIn My Jobs tracker (no S3)."
    )
    ap.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    ap.add_argument(
        "--step-delay",
        type=float,
        default=0.35,
        help="Pause between UI steps (default: 0.35).",
    )
    ap.add_argument(
        "--reload-delay",
        type=float,
        default=DEFAULT_RELOAD_DELAY,
        help="Seconds to wait after each unsave for the page reload (default: 2.0).",
    )
    ap.add_argument(
        "--max-unsaves",
        type=int,
        default=0,
        help="Stop after this many unsaves (0 = no cap).",
    )
    ap.add_argument(
        "--no-highlight",
        action="store_true",
        help="Do not outline controls before clicking.",
    )
    args = ap.parse_args()

    cap = None if args.max_unsaves <= 0 else int(args.max_unsaves)
    try:
        n = run(
            headless=args.headless,
            step_delay=max(0.0, float(args.step_delay)),
            reload_delay=max(0.0, float(args.reload_delay)),
            max_unsaves=cap,
            no_highlight=args.no_highlight,
        )
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130

    log.info("Done — unsaved %d job(s) on LinkedIn tracker.", n)
    print(f"Unsaved {n} job(s) on LinkedIn tracker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
