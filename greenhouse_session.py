"""
Greenhouse Recruiting (https://app.greenhouse.io) — session and sign-in helpers.

Job application flows for Greenhouse boards can build on ``run_greenhouse_sign_in_flow`` once cookies exist.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from chrome_driver import build_chrome, save_cookies

log = logging.getLogger(__name__)

GREENHOUSE_SIGN_IN_URL = "https://app.greenhouse.io/users/sign_in"
DEFAULT_GREENHOUSE_COOKIE_PATH = Path("data/selenium_greenhouse_cookies.json")


def load_greenhouse_cookies(driver: Any, path: Path) -> None:
    """Restore cookies saved from a prior session (domain ``.greenhouse.io`` / ``app.greenhouse.io``)."""
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read Greenhouse cookies from %s: %s", path, e)
        return
    driver.get("https://app.greenhouse.io/")
    time.sleep(0.4)
    for c in raw:
        try:
            cookie: dict[str, Any] = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".greenhouse.io"),
                "path": c.get("path", "/"),
            }
            if c.get("expiry") is not None:
                cookie["expiry"] = int(c["expiry"])
            if "secure" in c:
                cookie["secure"] = bool(c["secure"])
            driver.add_cookie(cookie)
        except Exception:
            continue
    log.info("Loaded Greenhouse cookies from %s", path)


def run_greenhouse_sign_in_flow(args) -> None:
    """
    Open Chrome on the Greenhouse sign-in page. Loads ``--greenhouse-cookies`` when the file exists.

    After you finish signing in (or confirm the session is already valid), press Enter in this terminal
    to persist cookies and close the browser.
    """
    path = Path(args.greenhouse_cookies)
    driver = build_chrome(headless=args.headless)
    try:
        load_greenhouse_cookies(driver, path)
        log.info("Opening Greenhouse sign-in: %s", GREENHOUSE_SIGN_IN_URL)
        driver.get(GREENHOUSE_SIGN_IN_URL)
        time.sleep(1.0)
        log.info(
            "Complete sign-in in the browser if needed (e.g. Google SSO or email/password on %s). "
            "Cookies will be saved to %s when you continue below.",
            GREENHOUSE_SIGN_IN_URL,
            path.resolve(),
        )
        input("Press Enter here after you are signed in to Greenhouse to save cookies and exit… ")
        save_cookies(driver, path)
        log.info("Greenhouse session saved (%s).", path.resolve())
    finally:
        driver.quit()
