"""
Secondary Chrome worker for LinkedIn company-page consulting checks and filter-mode
company Jobs scans.

Owns the second WebDriver exclusively on one background thread. Primary submits commands
via a queue; consulting / dedicated-requirements calls block until the worker finishes
(so a company-jobs scan that is in flight must complete first).
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

from .chrome_driver import (
    build_chrome,
    driver_session_alive,
    load_cookies,
    quit_chrome,
    save_cookies,
)

log = logging.getLogger(__name__)

_STOP = object()


@dataclass
class _Cmd:
    kind: str  # "consulting" | "dedicated_reqs" | "company_scan" | "stop"
    payload: Any = None
    future: Future | None = None


class CompanyLookupWorker:
    """
    Background owner of the company-lookup Chrome session.

    ``submit_consulting`` / ``submit_dedicated_reqs`` return results and wait if a
    company scan (or earlier command) is still running. ``submit_company_scan`` is
    fire-and-forget.
    """

    def __init__(
        self,
        *,
        searcher: Any,
        headless: bool,
        session_file: Any,
        company_scan_handler: Callable[[Any, str], None] | None = None,
    ) -> None:
        self._searcher = searcher
        self._headless = bool(headless)
        self._session_file = session_file
        self._company_scan_handler = company_scan_handler
        self._q: queue.Queue[_Cmd | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._driver: Any = None
        self._started = False
        self._lock = threading.Lock()

    @property
    def driver(self) -> Any:
        return self._driver

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name="company-lookup-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 120.0) -> None:
        """Drain in-flight work (including company scans), then quit Chrome."""
        with self._lock:
            if not self._started:
                return
        self._q.put(_STOP)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning("Company lookup worker did not stop within %.0fs.", timeout)

    def submit_consulting(self, company_url: str) -> bool:
        """Block until the worker checks the company About page. True = consulting/recruiting."""
        self.start()
        fut: Future[bool] = Future()
        self._q.put(_Cmd(kind="consulting", payload=company_url, future=fut))
        return bool(fut.result())

    def submit_dedicated_reqs(self, job_id: str) -> str:
        """Block until the worker fetches dedicated-page requirements text."""
        self.start()
        fut: Future[str] = Future()
        self._q.put(_Cmd(kind="dedicated_reqs", payload=job_id, future=fut))
        return str(fut.result() or "")

    def submit_company_scan(self, company_url: str) -> None:
        """
        Queue a company Jobs-tab scan (non-blocking).

        The worker runs ``company_scan_handler(driver, company_url)`` when free.
        """
        if not company_url or self._company_scan_handler is None:
            return
        self.start()
        self._q.put(_Cmd(kind="company_scan", payload=company_url, future=None))

    def _ensure_driver(self) -> Any:
        if self._driver is not None and driver_session_alive(self._driver):
            return self._driver
        if self._driver is not None:
            quit_chrome(self._driver)
            self._driver = None
        log.info("Starting second Chrome session for LinkedIn company-page consulting checks.")
        driver = build_chrome(headless=self._headless)
        try:
            load_cookies(driver, self._session_file)
        except Exception:
            log.debug("Company lookup driver: cookie load failed", exc_info=True)
        log.info(
            "Company lookup browser: verifying LinkedIn session (same flow as main window — feed, "
            "Welcome Back / saved account, or email/password)."
        )
        try:
            self._searcher._login(driver)
        except Exception:
            log.exception("Company lookup driver: LinkedIn login failed; closing second Chrome.")
            quit_chrome(driver)
            raise
        self._driver = driver
        return driver

    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    # Finish any commands already queued before stop was sent.
                    while True:
                        try:
                            nxt = self._q.get_nowait()
                        except queue.Empty:
                            break
                        if nxt is _STOP:
                            continue
                        self._handle(nxt)  # type: ignore[arg-type]
                    break
                self._handle(item)  # type: ignore[arg-type]
        except Exception:
            log.exception("Company lookup worker crashed.")
        finally:
            drv = self._driver
            self._driver = None
            if drv is not None:
                try:
                    save_cookies(drv, self._session_file)
                except Exception:
                    log.debug("Company lookup driver: cookie save failed", exc_info=True)
                quit_chrome(drv)
            log.info("Company lookup worker stopped.")

    def _handle(self, cmd: _Cmd) -> None:
        fut = cmd.future
        try:
            if cmd.kind == "consulting":
                driver = self._ensure_driver()
                result = self._searcher.company_page_looks_consulting(driver, str(cmd.payload or ""))
                if fut is not None and not fut.done():
                    fut.set_result(bool(result))
                return
            if cmd.kind == "dedicated_reqs":
                driver = self._ensure_driver()
                result = self._searcher.fetch_dedicated_page_requirements(
                    driver, str(cmd.payload or "")
                )
                if fut is not None and not fut.done():
                    fut.set_result(str(result or ""))
                return
            if cmd.kind == "company_scan":
                url = str(cmd.payload or "").strip()
                handler = self._company_scan_handler
                if not url or handler is None:
                    return
                driver = self._ensure_driver()
                try:
                    handler(driver, url)
                except Exception:
                    log.exception("Company jobs scan failed for %s", url)
                return
            log.warning("Company lookup worker: unknown command kind %r", cmd.kind)
            if fut is not None and not fut.done():
                fut.set_result(False if cmd.kind == "consulting" else "")
        except Exception as e:
            log.exception("Company lookup worker command %r failed", cmd.kind)
            if fut is not None and not fut.done():
                fut.set_exception(e)
