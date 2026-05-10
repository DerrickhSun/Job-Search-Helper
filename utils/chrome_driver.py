"""Selenium Chrome setup, cookie persistence, and optional focus outline for debugging."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger(__name__)

DEFAULT_COOKIE_PATH = Path("data/selenium_linkedin_cookies.json")


def build_chrome(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    # Reduce idle overhead vs default Chromium flags where helpful
    opts.add_argument("--disable-extensions")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    if not headless:
        try:
            driver.maximize_window()
        except Exception:
            driver.set_window_size(1400, 900)
    return driver


def focus_element(driver: webdriver.Chrome, element, pause: float = 0.35) -> None:
    """Scroll into view and briefly outline the element (visible window debugging)."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.12)
        driver.execute_script("arguments[0].style.outline = '3px solid crimson';", element)
        time.sleep(pause)
        driver.execute_script("arguments[0].style.outline = '';", element)
    except Exception as e:
        log.debug("focus_element: %s", e)


def load_cookies(driver: webdriver.Chrome, path: Path = DEFAULT_COOKIE_PATH) -> None:
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read cookies from %s: %s", path, e)
        return

    driver.get("https://www.linkedin.com/")
    time.sleep(0.4)
    for c in raw:
        try:
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".linkedin.com"),
                "path": c.get("path", "/"),
            }
            if c.get("expiry") is not None:
                cookie["expiry"] = int(c["expiry"])
            if "secure" in c:
                cookie["secure"] = bool(c["secure"])
            driver.add_cookie(cookie)
        except Exception:
            continue
    log.info("Loaded cookies from %s", path)


def save_cookies(driver: webdriver.Chrome, path: Path = DEFAULT_COOKIE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = driver.get_cookies()
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    log.info("Saved %d cookies to %s", len(cookies), path)
