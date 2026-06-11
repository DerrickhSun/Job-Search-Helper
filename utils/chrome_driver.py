"""Selenium Chrome setup, cookie persistence, and optional focus outline for debugging."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger(__name__)

DRIVER_SESSION_CLOSED_MSG = (
    "Chrome window was closed or the WebDriver session ended — saving progress and shutting down."
)

DEFAULT_COOKIE_PATH = Path("data/selenium_linkedin_cookies.json")

# Cap how long a health probe may block when Chrome was killed (chromedriver can hang otherwise).
_DRIVER_ALIVE_PROBE_TIMEOUT = 3.0

# HTTP read timeout for WebDriver commands (fail faster when the browser window is gone).
_DRIVER_COMMAND_TIMEOUT = 12.0


def _running_in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("CHROME_DOCKER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def build_chrome(headless: bool = False) -> webdriver.Chrome:
    opts = Options()
    chrome_bin = (os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_SHIM") or "").strip()
    if chrome_bin:
        opts.binary_location = chrome_bin
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    # Reduce idle overhead vs default Chromium flags where helpful
    opts.add_argument("--disable-extensions")
    if _running_in_container() or os.environ.get("CHROME_NO_SANDBOX", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

    chromedriver_path = (os.environ.get("CHROMEDRIVER_PATH") or "").strip()
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    try:
        driver.command_executor.set_timeout(_DRIVER_COMMAND_TIMEOUT)
    except Exception:
        log.debug("Could not set WebDriver command timeout", exc_info=True)
    if not headless:
        try:
            driver.maximize_window()
        except Exception:
            driver.set_window_size(1400, 900)
    return driver


def _probe_driver_session_alive(driver: webdriver.Chrome) -> bool:
    """Fast session probe — must not call ``execute_script`` (can hang when Chrome was killed)."""
    try:
        handles = driver.window_handles
        if not handles:
            return False
        current = driver.current_window_handle
        return current in handles
    except WebDriverException:
        return False


def driver_session_alive(driver: webdriver.Chrome | None) -> bool:
    """False when Chrome was closed or the WebDriver session is no longer reachable."""
    if driver is None:
        return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_probe_driver_session_alive, driver)
        try:
            return fut.result(timeout=_DRIVER_ALIVE_PROBE_TIMEOUT)
        except concurrent.futures.TimeoutError:
            log.debug(
                "driver_session_alive: probe timed out after %.1fs",
                _DRIVER_ALIVE_PROBE_TIMEOUT,
            )
            return False


def interruptible_sleep(
    seconds: float,
    driver: webdriver.Chrome | None,
    *,
    poll: float = 0.4,
) -> bool:
    """
    Sleep up to ``seconds``, polling session health. Returns False when the driver session ended.
    """
    if seconds <= 0:
        return driver_session_alive(driver)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not driver_session_alive(driver):
            log_driver_session_closed()
            return False
        time.sleep(min(poll, max(0.0, deadline - time.time())))
    return driver_session_alive(driver)


def log_driver_session_closed() -> None:
    log.info(DRIVER_SESSION_CLOSED_MSG)


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
    if not driver_session_alive(driver):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cookies = driver.get_cookies()
    except WebDriverException as e:
        log.debug("save_cookies: session unavailable (%s)", e)
        return
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    log.info("Saved %d cookies to %s", len(cookies), path)
