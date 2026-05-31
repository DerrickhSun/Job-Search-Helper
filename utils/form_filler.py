"""
Easy Apply Form Filler
Uses Selenium + Chrome for LinkedIn Easy Apply flows.

Default is a visible window. Use --headless to hide it.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from .chrome_driver import DEFAULT_COOKIE_PATH, build_chrome, focus_element, load_cookies
from .cover_letter import cover_letter_docx_path_unique, write_cover_letter_docx
from .form_fill_rules import DISCARD_APPLY, FormFillRulesEngine

log = logging.getLogger(__name__)

# Easy Apply “Photo” / headshot steps: ``send_keys`` with an absolute path to this file (if it exists).
DEFAULT_HEADSHOT_IMAGE = Path("data/selfInSuit.png")

SEL = {
    # Primary apply CTA on the job detail pane (two-pane search or /jobs/view/…).
    "apply_button_id": "jobs-apply-button-id",
    "easy_apply_btn": 'button.jobs-apply-button, button[aria-label*="Easy Apply"]',
    "modal": ".jobs-easy-apply-modal",
    "next_btn": 'button[aria-label="Continue to next step"]',
    "review_btn": 'button[aria-label="Review your application"]',
    "submit_btn": 'button[aria-label="Submit application"]',
    "close_btn": 'button[aria-label="Dismiss"], button[aria-label="dismiss"]',
    # Post-submit success / blocking overlay — must dismiss before the next job in the same session.
    "done_btn": (
        'button[aria-label="Done"], '
        ".jobs-easy-apply-modal button[aria-label=\"Done\"], "
        'button[data-test-modal-close-btn]'
    ),
    "upload_resume": 'input[name="file"]',
    "text_input": "input[type='text'], input[type='number'], input[type='tel']",
    "textarea": "textarea",
    "select": "select",
    "radio": "input[type='radio']",
    "linkedin_radio_fieldset": 'fieldset[data-test-form-builder-radio-button-form-component="true"]',
    "error_msg": ".artdeco-inline-feedback--error",
}

# LinkedIn "Job search safety reminder" / possible fraud pre-apply dialog (not the Easy Apply sheet).
JOB_TRUST_SAFETY_MODAL_CONTENT = ".job-trust-pre-apply-safety-tips-modal__content"
JOB_TRUST_SAFETY_MODAL_DISMISS = (
    "button.artdeco-modal__dismiss[data-test-modal-close-btn], "
    "button[data-test-modal-close-btn].artdeco-modal__dismiss"
)

APPLY_ABORT_JOB_TRUST_SAFETY = "job_trust_safety_reminder"

# LinkedIn daily Easy Apply submission cap. When reached, an inline feedback message appears (e.g.
# "We limit daily submissions to maintain quality and prevent bots… Save this job and apply tomorrow.").
APPLY_ABORT_DAILY_LIMIT = "linkedin_daily_application_limit"
LINKEDIN_DAILY_LIMIT_MESSAGE = "artdeco-inline-feedback__message"
LINKEDIN_DAILY_LIMIT_SUBSTRINGS = (
    "we limit daily submissions",
    "apply tomorrow",
)

# Success / follow-up UI after submit is often *not* inside ``.jobs-easy-apply-modal`` — same tab, different layer.
POST_APPLY_DISMISS = (
    'button[aria-label="Not now"]',
    'button[aria-label="Got it"]',
    'button[aria-label="Close"]',
    "button.artdeco-modal__dismiss",
)

# Workday-hosted apply flows (new tab from LinkedIn “Apply”): no LinkedIn modal; often nested iframes.
# Several selectors — tenants vary; we only need one match to treat the context as fillable.
WORKDAY_FIELD_MARKERS: tuple[str, ...] = (
    'input[data-automation-id="email"]',
    'input[data-automation-id="Email"]',
    '[data-automation-id="formField-email"]',
    '[data-automation-id="formField-Email"]',
    'input[autocomplete="email"]',
    '[data-automation-id*="email"]',
)

WORKDAY_URL_SUBSTRINGS: tuple[str, ...] = (
    "myworkdayjobs.com",
    "myworkday.com",
)

# Greenhouse-hosted job application (company career site or ``boards.greenhouse.io`` embed, often in an iframe).
# Distinct from LinkedIn Easy Apply; used by ``_resolve_fill_root`` and helper assist context detection.
GREENHOUSE_APPLY_FIELD_MARKERS: tuple[str, ...] = (
    "input.input__single-line",
    "input.input.input__single-line",
    'input[id^="question_"]',
    "div.field-wrapper input.input",
    "div.text-input-wrapper input.input",
)

# Honeypot / anti-bot fields — never fill (label often contains "website" and would match website rules).
WORKDAY_SKIP_AUTOMATION_IDS: frozenset[str] = frozenset(
    {
        "beecatcher",
    }
)

# Shown after **Dismiss** on an in-progress application — save draft vs discard.
DRAFT_SAVE_ARIA = (
    'button[aria-label="Save"]',
    'button[aria-label="save"]',
    'button[aria-label="Save application"]',
    'button[aria-label*="Save application"]',
)


class EasyApplyFiller:
    def __init__(
        self,
        headless: bool = False,
        screenshot_dir: str = "output/screenshots",
        session_file: Path | str = DEFAULT_COOKIE_PATH,
        step_delay: float = 0.35,
        highlight: bool = True,
        easy_apply_wait_seconds: float = 5.0,
        apply_click_gap_seconds: float = 1.0,
        apply_review_pause_after_fill_seconds: float = 3.0,
        apply_first_empty_field_pause_after_nav_seconds: float = 10.0,
        cover_letter_docx_dir: Path | str = "output/coverletters",
        form_fill_rules_path: Path | str | None = None,
        helper_scan_all_tabs: bool = False,
        headshot_image_path: Path | str | None = None,
    ):
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = Path(session_file)
        self.cover_letter_docx_dir = Path(cover_letter_docx_dir)
        self.cover_letter_docx_dir.mkdir(parents=True, exist_ok=True)
        self.step_delay = step_delay
        self.highlight = highlight and not headless
        self.easy_apply_wait_seconds = max(0.0, float(easy_apply_wait_seconds))
        self.apply_click_gap_seconds = max(0.0, float(apply_click_gap_seconds))
        self.apply_review_pause_after_fill_seconds = max(
            0.0, float(apply_review_pause_after_fill_seconds)
        )
        self.apply_first_empty_field_pause_after_nav_seconds = max(
            0.0, float(apply_first_empty_field_pause_after_nav_seconds)
        )
        self._user_pause_pending_after_nav = False
        self._user_pause_consumed_this_step = False
        self._apply_abort_reason: str | None = None
        self._rules = FormFillRulesEngine(
            Path(form_fill_rules_path) if form_fill_rules_path else None,
            apply_source="linkedin",
        )
        # Helper mode: if False, only the current WebDriver tab is checked (no tab switching; avoids focus
        # stealing). If True, every tab is scanned (needed when Workday opens in a new tab WebDriver did not
        # switch to). WebDriver has no API for “the tab the user clicked last.”
        self.helper_scan_all_tabs = bool(helper_scan_all_tabs)
        self.headshot_image_path = (
            Path(headshot_image_path) if headshot_image_path is not None else DEFAULT_HEADSHOT_IMAGE
        )

    @staticmethod
    def _default_content(driver: Any) -> None:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    def _workday_markers_present(self, driver: Any) -> bool:
        """True if any Workday-style marker exists in the **current** document context."""
        for sel in WORKDAY_FIELD_MARKERS:
            try:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                continue
        for xp in (
            "//input[@data-automation-id='email']",
            "//*[@data-automation-id='formField-email']",
        ):
            try:
                if driver.find_elements(By.XPATH, xp):
                    return True
            except Exception:
                continue
        # Some drivers/pages behave more reliably than pure CSS for attribute selectors.
        try:
            if driver.execute_script(
                """
                return !!(
                  document.querySelector('input[data-automation-id="email"]') ||
                  document.querySelector('[data-automation-id="formField-email"]') ||
                  document.querySelector('input[autocomplete="email"]')
                );
                """
            ):
                return True
        except Exception:
            pass
        return False

    def _url_looks_like_workday_jobs(self, driver: Any) -> bool:
        try:
            u = (driver.current_url or "").lower()
        except Exception:
            return False
        return any(s in u for s in WORKDAY_URL_SUBSTRINGS)

    def _workday_apply_shell_present_js(self, driver: Any) -> bool:
        """True when the candidate apply MFE shell is mounted (even if inputs are not in DOM yet)."""
        try:
            return bool(
                driver.execute_script(
                    """
                    return !!(
                      document.querySelector('[data-automation-id="applyFlowPage"]') ||
                      document.querySelector('[data-automation-id="signInFormo"]') ||
                      document.querySelector('[data-mfe-id="applyFlow"]') ||
                      document.querySelector('form[data-automation-id="signInFormo"]')
                    );
                    """
                )
            )
        except Exception:
            return False

    def _label_looks_like_robot_trap(self, label: str | None) -> bool:
        """Heuristic for honeypot labels (e.g. “for robots only, do not enter if you're human”)."""
        low = (label or "").lower()
        if "robots only" in low:
            return True
        if "do not enter" in low and "human" in low:
            return True
        return False

    def _automation_id_is_skipped(self, input_el: Any) -> bool:
        try:
            return (input_el.get_attribute("data-automation-id") or "").strip().lower() in WORKDAY_SKIP_AUTOMATION_IDS
        except Exception:
            return False

    def _greenhouse_apply_markers_present(self, driver: Any) -> bool:
        """True when the current document looks like a Greenhouse job application (embedded board)."""
        for sel in GREENHOUSE_APPLY_FIELD_MARKERS:
            try:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
            except Exception:
                continue
        return False

    def _find_greenhouse_job_application_body(self, driver: Any, depth: int = 0) -> Any | None:
        """
        Return ``body`` in the document or nested ``iframe`` / ``frame`` that contains Greenhouse apply fields.

        On success, ``driver`` is left focused on that document (possibly nested). On failure, returns ``None``
        with ``driver`` back at the starting context of the failed branch (same pattern as Workday).
        """
        if depth > 10:
            return None
        if self._greenhouse_apply_markers_present(driver):
            return driver.find_element(By.TAG_NAME, "body")

        # Prefer known Greenhouse job-board iframes (e.g. Webflow ``#grnhse_iframe``) before scanning every
        # iframe — career pages often embed many frames (Termly, HubSpot, …) and the apply form is isolated.
        priority_iframe_selectors = (
            "iframe#grnhse_iframe",
            "iframe[id='grnhse_iframe']",
            "iframe[src*='job-boards.greenhouse.io']",
            "iframe[src*='boards.greenhouse.io/embed']",
            "iframe[src*='greenhouse.io/embed/job_app']",
        )
        for sel in priority_iframe_selectors:
            for fr in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    driver.switch_to.frame(fr)
                except Exception:
                    continue
                inner = self._find_greenhouse_job_application_body(driver, depth + 1)
                if inner is not None:
                    return inner
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    self._default_content(driver)
                    return None

        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
            except Exception:
                continue
            inner = self._find_greenhouse_job_application_body(driver, depth + 1)
            if inner is not None:
                return inner
            try:
                driver.switch_to.parent_frame()
            except Exception:
                self._default_content(driver)
                return None
        return None

    def _find_workday_fill_body(self, driver: Any, depth: int = 0) -> Any | None:
        """
        Return ``body`` in the document or nested ``iframe``/``frame`` that contains Workday markers.
        On success, ``driver`` is left focused on that document (possibly nested). On failure, returns
        ``None`` with ``driver`` back at the starting context of the failed branch.
        """
        if depth > 10:
            return None
        if self._workday_markers_present(driver):
            return driver.find_element(By.TAG_NAME, "body")
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
            except Exception:
                continue
            inner = self._find_workday_fill_body(driver, depth + 1)
            if inner is not None:
                return inner
            try:
                driver.switch_to.parent_frame()
            except Exception:
                self._default_content(driver)
                return None
        return None

    def _resolve_fill_root(self, driver: Any) -> Any | None:
        """
        LinkedIn: visible ``.jobs-easy-apply-modal``. Workday: ``body`` in the document or nested iframes
        when ``WORKDAY_FIELD_MARKERS`` match. Greenhouse: ``body`` when embedded apply fields match
        (``input.input__single-line``, ``input[id^="question_"]``, etc.), including inside iframes.
        Leaves ``driver`` inside the iframe when the form lives there.
        """
        self._default_content(driver)
        for el in driver.find_elements(By.CSS_SELECTOR, SEL["modal"]):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue
        wd = self._find_workday_fill_body(driver, 0)
        if wd is not None:
            return wd
        self._default_content(driver)
        if self._url_looks_like_workday_jobs(driver) and self._workday_apply_shell_present_js(driver):
            return driver.find_element(By.TAG_NAME, "body")
        gh = self._find_greenhouse_job_application_body(driver, 0)
        if gh is not None:
            log.debug(
                "Fill root: Greenhouse embedded job application (url=%s)",
                (driver.current_url or "")[:160],
            )
            return gh
        self._default_content(driver)
        u = (driver.current_url or "").lower()
        if "workday" in u or "myworkdayjobs" in u:
            log.debug(
                "Workday-like URL but no markers matched (%s). "
                "Possible shadow-DOM fields, different data-automation-id values, or page still loading.",
                driver.current_url,
            )
        return None

    def _assist_context_open_single_tab(self, driver: Any) -> bool:
        """Check only the current WebDriver window (no ``switch_to.window``)."""
        if self._easy_apply_modal_is_open(driver):
            return True
        self._default_content(driver)
        if self._workday_markers_present(driver):
            return True
        if self._url_looks_like_workday_jobs(driver) and self._workday_apply_shell_present_js(driver):
            return True
        wd = self._find_workday_fill_body(driver, 0)
        self._default_content(driver)
        if wd is not None:
            return True
        gh = self._find_greenhouse_job_application_body(driver, 0)
        self._default_content(driver)
        return gh is not None

    def _assist_context_open_scan_all_tabs(self, driver: Any) -> bool:
        """
        Walk every window handle. Needed when apply opens Workday in a new tab but WebDriver still points
        at LinkedIn — **but** each ``switch_to`` can briefly activate that tab in Chrome (annoying).
        """
        try:
            handles = list(driver.window_handles)
        except Exception:
            handles = []
        if not handles:
            return False
        try:
            original = driver.current_window_handle
        except Exception:
            original = None

        for h in handles:
            try:
                driver.switch_to.window(h)
            except Exception:
                continue
            try:
                self._default_content(driver)
                if self._easy_apply_modal_is_open(driver):
                    log.debug("assist_context: Easy Apply modal on tab %s", h[-8:])
                    return True
                if self._workday_markers_present(driver):
                    log.debug("assist_context: Workday markers on tab %s url=%s", h[-8:], driver.current_url[:80])
                    return True
                if self._url_looks_like_workday_jobs(driver) and self._workday_apply_shell_present_js(driver):
                    log.debug("assist_context: Workday shell on tab %s url=%s", h[-8:], driver.current_url[:80])
                    return True
                wd = self._find_workday_fill_body(driver, 0)
                self._default_content(driver)
                if wd is not None:
                    log.debug("assist_context: Workday form in iframe on tab %s", h[-8:])
                    return True
                gh = self._find_greenhouse_job_application_body(driver, 0)
                self._default_content(driver)
                if gh is not None:
                    log.debug(
                        "assist_context: Greenhouse embedded apply on tab %s url=%s",
                        h[-8:],
                        (driver.current_url or "")[:100],
                    )
                    return True
            except Exception as e:
                log.debug("assist_context: tab scan skip: %s", e)
                try:
                    self._default_content(driver)
                except Exception:
                    pass
                continue

        if original:
            try:
                driver.switch_to.window(original)
                self._default_content(driver)
            except Exception:
                pass
        return False

    def assist_context_open(self, driver: Any) -> bool:
        """
        True if we should run assist: LinkedIn Easy Apply sheet open, or a Workday-style apply form.

        By default only the **current WebDriver tab** is inspected (no programmatic tab switching).
        Set ``helper_scan_all_tabs`` to also scan other tabs (can steal focus in Chrome).
        """
        if self.helper_scan_all_tabs:
            return self._assist_context_open_scan_all_tabs(driver)
        return self._assist_context_open_single_tab(driver)

    @staticmethod
    def _label_is_cover_letter_field(label: str) -> bool:
        """True when the control is clearly for a cover letter (LinkedIn may pre-fill stale text)."""
        n = FormFillRulesEngine.normalize_label(label)
        if not n:
            return False
        if "cover letter" in n:
            return True
        return "cover" in n and "letter" in n

    def _control_is_cover_letter_field(self, driver: Any, el) -> bool:
        """Uses field label/aria/placeholder and Easy Apply wrapper text (same idea as file-upload detection)."""
        if self._label_is_cover_letter_field(self._get_label(driver, el)):
            return True
        for xpath in (
            "./ancestor::div[contains(@class,'jobs-easy-apply-form-element')][1]",
            "./ancestor::fieldset[1]",
            "./ancestor::div[contains(@class,'jobs-easy-apply-form')][1]",
        ):
            try:
                wrap = el.find_element(By.XPATH, xpath)
                if self._label_is_cover_letter_field(wrap.text or ""):
                    return True
            except Exception:
                continue
        return False

    def _control_text_snapshot(self, el) -> str:
        """Best-effort current text (React often mirrors into ``value`` or inner text)."""
        try:
            v = (el.get_attribute("value") or "").strip()
        except Exception:
            v = ""
        if v:
            return v
        try:
            return (el.text or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _js_pointer_activate(driver: Any, el) -> None:
        """
        Scroll into view and dispatch mouse + focus events.

        Some embedded apply UIs (Greenhouse-style wrappers) ignore a bare Selenium ``click()`` on the
        ``input`` until the visible chrome receives a real activation sequence.
        """
        try:
            driver.execute_script(
                """
                const el = arguments[0];
                if (!el || !el.ownerDocument) return;
                el.scrollIntoView({block: 'center', inline: 'nearest'});
                const view = el.ownerDocument.defaultView;
                const opts = { bubbles: true, cancelable: true, view: view };
                try {
                  el.dispatchEvent(new MouseEvent('mousedown', opts));
                  el.dispatchEvent(new MouseEvent('mouseup', opts));
                  el.dispatchEvent(new MouseEvent('click', opts));
                } catch (e) {}
                try { el.focus(); } catch (e2) {}
                """,
                el,
            )
        except Exception:
            pass

    def _click_labelish(self, driver: Any, lab) -> None:
        """Activate + click a label (or label-like) element."""
        self._js_pointer_activate(driver, lab)
        try:
            lab.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", lab)
            except Exception:
                pass
        time.sleep(0.1)

    def _activate_text_control_before_fill(self, driver: Any, input_el) -> None:
        """
        Click/focus the field chrome before ``send_keys``.

        Embedded Greenhouse often places ``<label for=…>`` as a **sibling** of the ``input`` inside
        ``div.input-wrapper`` (label first, then input). An ``ancestor::label`` XPath never matches that
        pattern — we must hit ``label[for=id]`` or ``preceding-sibling::label`` first, then wrappers
        (``input-wrapper--active``), then the input.
        """
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", input_el)
        except Exception:
            pass
        time.sleep(0.06)

        iid = ""
        try:
            iid = (input_el.get_attribute("id") or "").strip()
        except Exception:
            pass

        # 1) Label associated by @for (Greenhouse: sibling label inside .input-wrapper)
        if iid:
            try:
                for lab in input_el.find_elements(
                    By.XPATH,
                    f'./ancestor::div[contains(@class,"input-wrapper")][1]//label[@for="{iid}"]',
                ):
                    try:
                        if lab.is_displayed():
                            self._click_labelish(driver, lab)
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                for lab in driver.find_elements(By.CSS_SELECTOR, f'label[for="{iid}"]'):
                    try:
                        if lab.is_displayed():
                            self._click_labelish(driver, lab)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        # 2) Sibling labels (same parent as input — common GH / Webflow embed)
        for sib_xp in ("./preceding-sibling::label[1]", "./following-sibling::label[1]"):
            try:
                lab = input_el.find_element(By.XPATH, sib_xp)
                if lab.is_displayed():
                    self._click_labelish(driver, lab)
            except (NoSuchElementException, Exception):
                continue

        wrapper_xpaths = (
            "./ancestor::div[contains(@class,'input-wrapper')][1]",
            "./ancestor::div[contains(@class,'text-input-wrapper')][1]",
            "./ancestor::div[contains(@class,'field-wrapper')][1]",
            "./ancestor::div[contains(@class,'single-line-text')][1]",
            "./ancestor::div[contains(@class,'textarea-wrapper')][1]",
        )
        for xp in wrapper_xpaths:
            try:
                wrap = input_el.find_element(By.XPATH, xp)
                if not wrap.is_displayed():
                    continue
                self._js_pointer_activate(driver, wrap)
                try:
                    wrap.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", wrap)
                    except Exception:
                        pass
                time.sleep(0.12)
            except (NoSuchElementException, Exception):
                continue

        self._js_pointer_activate(driver, input_el)
        try:
            driver.execute_script("arguments[0].focus();", input_el)
        except Exception:
            pass
        try:
            input_el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", input_el)
            except Exception:
                pass
        try:
            ActionChains(driver).move_to_element(input_el).pause(0.05).click().perform()
        except Exception:
            pass
        time.sleep(0.1)

    def _replace_text_control_value(self, driver: Any, el, text: str) -> None:
        """Select-all and replace — ``clear()`` alone often leaves LinkedIn’s draft cover letter."""
        self._activate_text_control_before_fill(driver, el)
        if self.highlight:
            focus_element(driver, el, pause=0.12)
        time.sleep(0.05)
        try:
            el.clear()
        except Exception:
            pass
        mod = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        el.send_keys(mod, "a")
        el.send_keys(Keys.BACKSPACE)
        el.send_keys(text)

    def _pause(self) -> None:
        if self.step_delay > 0:
            time.sleep(self.step_delay)

    def _after_ui_click(self) -> None:
        """Pause after a button click so you can verify the UI during debugging (see ``apply_click_gap_seconds``)."""
        if self.apply_click_gap_seconds > 0:
            time.sleep(self.apply_click_gap_seconds)

    def _after_field_fill(self) -> None:
        """Pause after we type or change a field so you can review (see ``apply_review_pause_after_fill_seconds``)."""
        if self.apply_review_pause_after_fill_seconds > 0:
            time.sleep(self.apply_review_pause_after_fill_seconds)

    def _maybe_pause_for_user_on_first_empty_field(self, label: str = "") -> None:
        """
        After Continue/Review, pause once on the first empty control on the new step so the user can
        fill fields we do not auto-fill.
        """
        if not self._user_pause_pending_after_nav:
            return
        self._user_pause_pending_after_nav = False
        pause = self.apply_first_empty_field_pause_after_nav_seconds
        if pause <= 0:
            return
        hint = f" ({label[:100]})" if label else ""
        log.info(
            "Pausing %.1fs for manual fill — first empty field after Continue/Review%s",
            pause,
            hint,
        )
        time.sleep(pause)
        self._user_pause_consumed_this_step = True

    def consume_apply_abort_reason(self) -> str | None:
        """Return and clear the reason the last :meth:`apply` aborted early (if any)."""
        reason = self._apply_abort_reason
        self._apply_abort_reason = None
        return reason

    @staticmethod
    def _dialog_is_job_trust_safety_modal(el: Any) -> bool:
        """True for LinkedIn's pre-apply trust / fraud warning layer (``role="dialog"``)."""
        try:
            if not el.is_displayed():
                return False
        except Exception:
            return False
        try:
            if el.find_elements(By.CSS_SELECTOR, JOB_TRUST_SAFETY_MODAL_CONTENT):
                return True
        except Exception:
            pass
        blob = (el.text or "").lower()
        if "job search safety reminder" in blob:
            return True
        if "research the company" in blob and "report suspicious jobs" in blob:
            return True
        if "job-trust-pre-apply-safety-tips" in (el.get_attribute("class") or "").lower():
            return True
        return False

    def _job_trust_safety_modal_element(self, driver: Any) -> Any | None:
        for el in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"], .artdeco-modal'):
            try:
                if self._dialog_is_job_trust_safety_modal(el):
                    return el
            except Exception:
                continue
        for el in driver.find_elements(By.CSS_SELECTOR, JOB_TRUST_SAFETY_MODAL_CONTENT):
            try:
                if not el.is_displayed():
                    continue
                parent = el.find_element(
                    By.XPATH, './ancestor::*[@role="dialog" or contains(@class,"artdeco-modal")][1]'
                )
                if parent:
                    return parent
            except Exception:
                return el
        return None

    def _dismiss_job_trust_safety_modal(self, driver: Any) -> bool:
        """Close the trust/safety reminder dialog via its Dismiss (X) control."""
        modal = self._job_trust_safety_modal_element(driver)
        if modal is None:
            return False
        for sel in (
            JOB_TRUST_SAFETY_MODAL_DISMISS,
            'button[aria-label="Dismiss"]',
            'button[aria-label="dismiss"]',
        ):
            try:
                for btn in modal.find_elements(By.CSS_SELECTOR, sel):
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    if self.highlight:
                        focus_element(driver, btn, pause=self.step_delay)
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.35)
                    if self._job_trust_safety_modal_element(driver) is None:
                        log.info("Closed LinkedIn job trust/safety reminder dialog.")
                        return True
            except Exception:
                continue
        return False

    def _linkedin_daily_limit_reached(self, driver: Any) -> bool:
        """
        True when LinkedIn's daily Easy Apply submission cap message is on the page (inline feedback
        such as "We limit daily submissions … Save this job and apply tomorrow.").

        The message can sit under a grayed-out Apply button; such text is often not "visible" to Selenium,
        so we read ``textContent`` (via JS) rather than ``element.text`` (which is empty for hidden nodes).
        """
        subs = list(LINKEDIN_DAILY_LIMIT_SUBSTRINGS)
        try:
            found = driver.execute_script(
                """
                const subs = arguments[0];
                const nodes = document.querySelectorAll(
                  '.artdeco-inline-feedback__message, .artdeco-inline-feedback, [class*="inline-feedback"]'
                );
                for (const el of nodes) {
                  const t = (el.textContent || '').toLowerCase();
                  for (const s of subs) { if (t.includes(s)) return true; }
                }
                return false;
                """,
                subs,
            )
            if found:
                return True
        except Exception as e:
            log.debug("Daily-limit JS scan failed (%s); falling back to element scan.", e)

        try:
            els = driver.find_elements(By.CSS_SELECTOR, f".{LINKEDIN_DAILY_LIMIT_MESSAGE}")
        except Exception:
            els = []
        for el in els:
            try:
                text = (el.get_attribute("textContent") or el.text or "").strip().lower()
            except Exception:
                continue
            if text and any(s in text for s in LINKEDIN_DAILY_LIMIT_SUBSTRINGS):
                return True
        return False

    def _abort_apply_for_daily_limit(self, driver: Any, job: dict, *, when: str) -> bool:
        """If the daily-limit message is present, record the abort reason and signal the caller to stop."""
        if not self._linkedin_daily_limit_reached(driver):
            return False
        log.warning(
            "LinkedIn daily application limit reached (%s) at %s — %s. Stopping further applies.",
            job.get("title"),
            job.get("company"),
            when,
        )
        self._dismiss_easy_apply_modal_if_open(driver, "after daily limit")
        self._apply_abort_reason = APPLY_ABORT_DAILY_LIMIT
        return True

    def _abort_apply_for_job_trust_safety(self, driver: Any, job: dict) -> bool:
        """
        If the pre-apply trust/safety modal is open, dismiss it and signal the caller to skip this job.

        Returns True when the modal was present and handled.
        """
        if self._job_trust_safety_modal_element(driver) is None:
            return False
        log.warning(
            "LinkedIn job trust/safety reminder for %s at %s — skipping apply",
            job.get("title"),
            job.get("company"),
        )
        self._dismiss_job_trust_safety_modal(driver)
        self._dismiss_easy_apply_modal_if_open(driver, "after job trust safety")
        self._apply_abort_reason = APPLY_ABORT_JOB_TRUST_SAFETY
        return True

    def apply(self, job: dict, resume: dict, cover_letter: str, driver: Any | None = None) -> bool:
        """
        Clicks Easy Apply and submits the form.

        If ``driver`` is None (default), opens a new Chrome session and navigates to ``job["url"]``.
        If ``driver`` is provided, uses the current page (e.g. job search with the detail panel open)
        and does not close the browser afterward.
        """
        own_driver = driver is None
        self._apply_abort_reason = None
        try:
            if own_driver:
                driver = build_chrome(headless=self.headless)
                load_cookies(driver, self.session_file)
                driver.get(job["url"])
                self._pause()
                time.sleep(1.5)

            time.sleep(self.easy_apply_wait_seconds)

            # Leftover success / error modal blocks the next Apply on the same driver.
            self._dismiss_easy_apply_modal_if_open(driver, "before apply")

            # When the daily submission cap is hit, LinkedIn replaces the Apply button with the limit
            # message, so check before the (slow) apply-button poll.
            if self._abort_apply_for_daily_limit(driver, job, when="Apply button replaced by limit message"):
                return False

            apply_btn = self._find_apply_button(driver)
            if not apply_btn:
                if self._abort_apply_for_daily_limit(
                    driver, job, when="no Apply button — limit message shown"
                ):
                    return False
                raise RuntimeError(
                    "Apply button not found: expected #jobs-apply-button-id or Easy Apply fallback"
                )
            if self.highlight:
                focus_element(driver, apply_btn, pause=self.step_delay)
            apply_btn.click()
            self._after_ui_click()
            time.sleep(0.35)

            if self._abort_apply_for_job_trust_safety(driver, job):
                return False

            if self._abort_apply_for_daily_limit(driver, job, when="after clicking Apply"):
                return False

            return self._fill_form(driver, resume, cover_letter, job)
        except Exception as e:
            log.error("Application failed for %s at %s: %s", job["title"], job["company"], e)
            if driver:
                try:
                    path = self.screenshot_dir / f"error_{job['id']}.png"
                    driver.save_screenshot(str(path))
                except Exception:
                    pass
            return False
        finally:
            if own_driver and driver:
                driver.quit()

    def _find_apply_button(self, driver: Any):
        """
        First try the job view apply control ``#jobs-apply-button-id``, then legacy Easy Apply selectors.
        Polls briefly — the detail pane can lag after clicking a card in search results.
        """
        aid = SEL["apply_button_id"]
        pause = max(0.25, min(0.6, self.step_delay))
        for _ in range(24):
            try:
                el = driver.find_element(By.ID, aid)
                if el.is_displayed():
                    return el
            except NoSuchElementException:
                pass
            els = driver.find_elements(By.CSS_SELECTOR, SEL["easy_apply_btn"])
            if els:
                return els[0]
            time.sleep(pause)
        try:
            return driver.find_element(By.ID, aid)
        except NoSuchElementException:
            pass
        els = driver.find_elements(By.CSS_SELECTOR, SEL["easy_apply_btn"])
        return els[0] if els else None

    def _easy_apply_modal_is_open(self, driver: Any) -> bool:
        """True when the Easy Apply dialog is visible (blocks clicking Apply on the next job)."""
        for el in driver.find_elements(By.CSS_SELECTOR, SEL["modal"]):
            try:
                if el.is_displayed():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _element_in_dialog_or_modal_shell(el) -> bool:
        """True if ``el`` is under a dialog / artdeco modal (post-submit success is often a separate layer)."""
        try:
            cur = el
            for _ in range(14):
                cur = cur.find_element(By.XPATH, "..")
                role = (cur.get_attribute("role") or "").lower()
                cls = (cur.get_attribute("class") or "").lower()
                if role == "dialog":
                    return True
                if "artdeco-modal" in cls:
                    return True
                if "jobs-easy-apply-modal" in cls:
                    return True
                if (cur.tag_name or "").lower() == "body":
                    return False
        except Exception:
            pass
        return False

    def _visible_post_apply_control(self, driver: Any):
        """
        A Done / Dismiss / close control that sits in a dialog/modal shell (avoids unrelated Done buttons).
        LinkedIn may show these *outside* ``.jobs-easy-apply-modal`` after submit.
        """
        for group in (SEL["done_btn"], SEL["close_btn"], ", ".join(POST_APPLY_DISMISS)):
            for btn in driver.find_elements(By.CSS_SELECTOR, group):
                try:
                    if btn.is_displayed() and btn.is_enabled() and self._element_in_dialog_or_modal_shell(btn):
                        return btn
                except Exception:
                    continue
        return None

    def _blocking_apply_ui_open(self, driver: Any) -> bool:
        """
        True when some overlay still blocks the next **Apply** — either the Easy Apply sheet or a
        follow-up success / confirmation dialog (often a different DOM subtree than ``.jobs-easy-apply-modal``).
        """
        if self._easy_apply_modal_is_open(driver):
            return True
        if self._visible_post_apply_control(driver) is not None:
            return True
        # Visible generic dialog shells (late-mounted success UI)
        for sel in ('[role="dialog"]', ".artdeco-modal"):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        h = el.rect.get("height") or 0
                        w = el.rect.get("width") or 0
                        if h >= 80 and w >= 200:
                            t = (el.text or "").lower()
                            if any(
                                k in t
                                for k in (
                                    "application",
                                    "applied",
                                    "submitted",
                                    "success",
                                    "congrat",
                                )
                            ):
                                return True
                except Exception:
                    continue
        return False

    def _close_extra_browser_windows(self, driver: Any) -> None:
        """
        If LinkedIn opened a second window/tab, close it and return focus to the **jobs** tab.

        We prefer a handle whose URL looks like the job search/detail page so we do not close the
        main session when the new tab briefly becomes ``current_window_handle``.
        """
        try:
            handles = list(driver.window_handles)
        except Exception:
            return
        if len(handles) <= 1:
            return
        scored: list[tuple[int, Any]] = []
        for h in handles:
            try:
                driver.switch_to.window(h)
                url = (driver.current_url or "").lower()
                score = 0
                if "linkedin.com" in url:
                    score += 2
                if "jobs" in url:
                    score += 3
                if "/jobs/" in url:
                    score += 2
                scored.append((score, h))
            except Exception:
                scored.append((0, h))
        scored.sort(key=lambda t: t[0], reverse=True)
        keep = scored[0][1] if scored else handles[0]
        for h in handles:
            if h == keep:
                continue
            try:
                driver.switch_to.window(h)
                driver.close()
                log.info("Closed extra browser window so the job session can continue")
            except Exception:
                continue
        try:
            driver.switch_to.window(keep)
        except Exception:
            try:
                driver.switch_to.window(driver.window_handles[0])
            except Exception:
                pass

    def _click_done_or_close_in_modal(self, driver: Any) -> bool:
        """Try Done (post-submit), Dismiss, then other post-apply controls. Returns True if something was clicked."""
        extra = self._visible_post_apply_control(driver)
        if extra:
            try:
                if self.highlight:
                    focus_element(driver, extra, pause=0.2)
                extra.click()
                self._after_ui_click()
                if self._button_is_dismiss(extra):
                    self._click_save_on_dismiss_followup(driver)
                return True
            except Exception:
                pass
        for sel in (SEL["done_btn"], SEL["close_btn"], ", ".join(POST_APPLY_DISMISS)):
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        if self.highlight:
                            focus_element(driver, btn, pause=0.2)
                        btn.click()
                        self._after_ui_click()
                        if sel == SEL["close_btn"]:
                            self._click_save_on_dismiss_followup(driver)
                        return True
                except Exception:
                    continue
        try:
            modal = driver.find_element(By.CSS_SELECTOR, SEL["modal"])
            for btn in modal.find_elements(
                By.XPATH, ".//button[contains(normalize-space(), 'Done')]"
            ):
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    self._after_ui_click()
                    return True
        except Exception:
            pass
        for xp in (
            "//div[@role='dialog']//button[contains(normalize-space(), 'Done')]",
            "//*[contains(@class,'artdeco-modal')]//button[contains(normalize-space(), 'Done')]",
        ):
            for btn in driver.find_elements(By.XPATH, xp):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        btn.click()
                        self._after_ui_click()
                        return True
                except Exception:
                    continue
            return False

    def _dismiss_easy_apply_modal_if_open(self, driver: Any, context: str = "") -> None:
        """
        Close any overlay that blocks the next **Apply**: Easy Apply sheet, post-submit success in another
        layer, or an extra browser window.
        """
        self._close_extra_browser_windows(driver)
        if not self._blocking_apply_ui_open(driver):
            return
        log.info(
            "Apply-blocking UI still open%s — dismissing (Done / Dismiss / close)",
            f" ({context})" if context else "",
        )
        for attempt in range(18):
            self._close_extra_browser_windows(driver)
            if not self._blocking_apply_ui_open(driver):
                return
            if not self._click_done_or_close_in_modal(driver):
                self._click_dismiss_header(driver)
            time.sleep(0.35)
        if self._blocking_apply_ui_open(driver):
            log.warning(
                "Apply-blocking UI may still be visible after dismiss attempts%s — next apply may fail",
                f" ({context})" if context else "",
            )

    def _wait_then_dismiss_post_submit(self, driver: Any, context: str) -> None:
        """After **Submit**, success UI may mount a moment later in a different modal layer."""
        self._close_extra_browser_windows(driver)
        for _ in range(22):
            if self._blocking_apply_ui_open(driver):
                break
            time.sleep(0.35)
        self._dismiss_easy_apply_modal_if_open(driver, context)

    @staticmethod
    def _element_is_required(el) -> bool:
        if el.get_attribute("required") is not None:
            return True
        return (el.get_attribute("aria-required") or "").lower() == "true"

    def _modal_has_unfilled_required_fields(self, driver: Any) -> bool:
        """True if a required control is still empty (we could not complete the step)."""
        try:
            modal = driver.find_element(By.CSS_SELECTOR, SEL["modal"])
        except Exception:
            return False
        for el in modal.find_elements(By.CSS_SELECTOR, SEL["text_input"]):
            try:
                if not self._element_is_required(el):
                    continue
                if not (el.get_attribute("value") or "").strip():
                    return True
            except Exception:
                continue
        for el in modal.find_elements(By.CSS_SELECTOR, SEL["textarea"]):
            try:
                if not self._element_is_required(el):
                    continue
                if not (el.get_attribute("value") or "").strip():
                    return True
            except Exception:
                continue
        for el in modal.find_elements(By.CSS_SELECTOR, SEL["select"]):
            try:
                if not self._element_is_required(el):
                    continue
                if self._select_needs_fill(el):
                    return True
            except Exception:
                continue
        for el in modal.find_elements(By.CSS_SELECTOR, 'input[type="file"]'):
            try:
                if not self._element_is_required(el):
                    continue
                if not (el.get_attribute("value") or "").strip():
                    return True
            except Exception:
                continue
        for fs in modal.find_elements(By.CSS_SELECTOR, SEL["linkedin_radio_fieldset"]):
            try:
                if not self._linkedin_radio_fieldset_is_required(fs):
                    continue
                if not self._linkedin_radio_fieldset_is_selected(fs):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _value_looks_like_placeholder_blank(value: str) -> bool:
        """
        True for LinkedIn placeholder text used in repeatable cards (e.g. ``--``, ``- -``, ``– –``, ``— —``).
        """
        raw = (value or "").strip()
        if not raw:
            return True
        compact = raw.replace(" ", "")
        compact = compact.replace("\u2013", "-").replace("\u2014", "-")
        return compact in ("-", "--")

    def _has_empty_repeatable_education_grouping(self, root: Any) -> bool:
        """
        Detect LinkedIn Easy Apply repeatable ``Education`` cards that are effectively blank.

        Some flows pre-populate card shells where School shows placeholder dashes (``--``).
        Treat this as an unfillable required section so we abandon the apply in auto mode.
        """
        for grouping in root.find_elements(By.CSS_SELECTOR, ".jobs-easy-apply-repeatable-groupings__groupings"):
            try:
                heading = " ".join((grouping.text or "").split()).lower()
            except Exception:
                heading = ""
            if "education" not in heading:
                continue
            cards = grouping.find_elements(By.CSS_SELECTOR, ".artdeco-card")
            if not cards:
                return True
            for card in cards:
                fields: dict[str, str] = {}
                for row in card.find_elements(By.CSS_SELECTOR, ".mb1"):
                    try:
                        name = ""
                        value = ""
                        labels = row.find_elements(By.CSS_SELECTOR, "span.t-12")
                        vals = row.find_elements(By.CSS_SELECTOR, "span.t-14")
                        if labels:
                            name = " ".join((labels[0].text or "").split()).lower()
                        if vals:
                            value = " ".join((vals[0].text or "").split())
                        if name:
                            fields[name] = value
                    except Exception:
                        continue
                if "school" in fields and self._value_looks_like_placeholder_blank(fields["school"]):
                    return True
        return False

    @staticmethod
    def _button_is_dismiss(btn: Any) -> bool:
        al = (btn.get_attribute("aria-label") or "").lower()
        return "dismiss" in al

    def _click_dismiss_followup(self, driver: Any, *, discard: bool) -> None:
        """
        After **Dismiss**, LinkedIn often opens a second dialog: save the application draft or discard.
        Choose **Save** (default abandon) or **Discard** when a form rule rejects the job.
        """
        time.sleep(0.45)
        want = "discard" if discard else "save"
        for dialog in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"], .artdeco-modal'):
            try:
                if not dialog.is_displayed():
                    continue
            except Exception:
                continue
            blob = (dialog.text or "").lower()
            if blob and "discard" not in blob and "draft" not in blob and "save" not in blob:
                continue
            if discard:
                for sel in (
                    'button[aria-label="Discard"]',
                    'button[aria-label="discard"]',
                    'button[aria-label*="Discard application"]',
                ):
                    for btn in dialog.find_elements(By.CSS_SELECTOR, sel):
                        try:
                            if not btn.is_displayed() or not btn.is_enabled():
                                continue
                            al = (btn.get_attribute("aria-label") or "").lower()
                            if "discard" not in al:
                                continue
                            if self.highlight:
                                focus_element(driver, btn, pause=0.15)
                            btn.click()
                            self._after_ui_click()
                            log.info("Clicked Discard on dismiss follow-up (save vs discard)")
                            time.sleep(0.35)
                            return
                        except Exception:
                            continue
                for btn in dialog.find_elements(By.TAG_NAME, "button"):
                    try:
                        if not btn.is_displayed() or not btn.is_enabled():
                            continue
                        if (btn.text or "").strip().lower() != "discard":
                            continue
                        if self.highlight:
                            focus_element(driver, btn, pause=0.15)
                        btn.click()
                        self._after_ui_click()
                        log.info("Clicked Discard (visible text) on dismiss follow-up")
                        time.sleep(0.35)
                        return
                    except Exception:
                        continue
                continue
            for sel in DRAFT_SAVE_ARIA:
                for btn in dialog.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if not btn.is_displayed() or not btn.is_enabled():
                            continue
                        al = (btn.get_attribute("aria-label") or "").lower()
                        if "save" not in al and (btn.text or "").strip().lower() != "save":
                            continue
                        if self.highlight:
                            focus_element(driver, btn, pause=0.15)
                        btn.click()
                        self._after_ui_click()
                        log.info("Clicked Save on dismiss follow-up (save vs discard)")
                        time.sleep(0.35)
                        return
                    except Exception:
                        continue
            for btn in dialog.find_elements(By.TAG_NAME, "button"):
                try:
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    if (btn.text or "").strip().lower() != want:
                        continue
                    if self.highlight:
                        focus_element(driver, btn, pause=0.15)
                    btn.click()
                    self._after_ui_click()
                    log.info("Clicked %s (visible text) on dismiss follow-up", want.capitalize())
                    time.sleep(0.35)
                    return
                except Exception:
                    continue

    def _click_save_on_dismiss_followup(self, driver: Any) -> None:
        """After **Dismiss**, choose **Save** on the draft follow-up dialog."""
        self._click_dismiss_followup(driver, discard=False)

    def _click_dismiss_header(self, driver: Any, *, discard_draft: bool = False) -> bool:
        """Close the flow via the header **Dismiss** control (``aria-label`` Dismiss / dismiss)."""
        for sel in (
            'button[aria-label="Dismiss"]',
            'button[aria-label="dismiss"]',
        ):
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        if self.highlight:
                            focus_element(driver, btn, pause=0.2)
                        btn.click()
                        self._after_ui_click()
                        time.sleep(0.45)
                        self._click_dismiss_followup(driver, discard=discard_draft)
                        return True
                except Exception:
                    continue
        return False

    def _abandon_apply_and_dismiss(self, driver: Any, job: dict, reason: str) -> bool:
        """
        Leave the application without submitting: click **Dismiss** so the same session can apply elsewhere.
        Always returns False (apply did not complete).
        """
        log.warning("Abandoning Easy Apply for %s — %s", job.get("id"), reason)
        if self._click_dismiss_header(driver, discard_draft=False):
            self._dismiss_easy_apply_modal_if_open(driver, "after abandon dismiss")
            return False
        log.warning("Dismiss button not found; trying generic modal cleanup")
        self._dismiss_easy_apply_modal_if_open(driver, "abandon fallback")
        return False

    def _abandon_apply_and_discard(self, driver: Any, job: dict, reason: str) -> bool:
        """
        Close and discard the in-progress application (Dismiss, then Discard on the draft dialog).
        Always returns False (apply did not complete).
        """
        log.warning("Discarding Easy Apply for %s — %s", job.get("id"), reason)
        if self._click_dismiss_header(driver, discard_draft=True):
            self._dismiss_easy_apply_modal_if_open(driver, "after discard dismiss")
            return False
        log.warning("Dismiss button not found; trying generic modal cleanup")
        self._dismiss_easy_apply_modal_if_open(driver, "discard fallback")
        return False

    def _handle_screening_discard(self, driver: Any, job: dict, ans: str | None, *, assist: bool) -> bool:
        """If ``ans`` is :data:`DISCARD_APPLY`, discard the apply unless in assist mode. Returns True when handled."""
        if ans != DISCARD_APPLY:
            return False
        if assist:
            log.debug("Assist: form rule requested discard — leaving apply open for user label handling")
            return False
        self._abandon_apply_and_discard(driver, job, "screening rule rejected this job (discard apply)")
        return True

    def _fill_form(self, driver, resume: dict, cover_letter: str, job: dict) -> bool:
        max_steps = 10
        self._user_pause_pending_after_nav = False

        for step in range(max_steps):
            self._pause()
            time.sleep(0.6)

            modals = driver.find_elements(By.CSS_SELECTOR, SEL["modal"])
            if not modals:
                log.warning("Modal closed unexpectedly at step %d", step)
                return False

            if not self._fill_step(driver, resume, cover_letter, job):
                self._abandon_apply_and_dismiss(
                    driver,
                    job,
                    "required field with no matching rule — cannot auto-fill safely",
                )
                return False

            if self._modal_has_unfilled_required_fields(driver):
                self._abandon_apply_and_dismiss(
                    driver,
                    job,
                    "required field(s) still empty after fill — cannot complete this form",
                )
                return False

            errors = driver.find_elements(By.CSS_SELECTOR, SEL["error_msg"])
            if errors:
                error_text = errors[0].text
                log.warning("Validation error at step %d: %s", step, error_text)
                driver.save_screenshot(str(self.screenshot_dir / f"validation_{job['id']}_step{step}.png"))
                self._abandon_apply_and_dismiss(driver, job, f"validation error: {error_text}")
                return False

            submit_btns = driver.find_elements(By.CSS_SELECTOR, SEL["submit_btn"])
            review_btns = driver.find_elements(By.CSS_SELECTOR, SEL["review_btn"])
            next_btns = driver.find_elements(By.CSS_SELECTOR, SEL["next_btn"])

            if submit_btns:
                btn = submit_btns[0]
                if self.highlight:
                    focus_element(driver, btn, pause=self.step_delay)
                log.info("Submitting application...")
                btn.click()
                self._after_ui_click()
                time.sleep(0.6)
                if self._abort_apply_for_daily_limit(driver, job, when="after clicking Submit"):
                    return False
                self._wait_then_dismiss_post_submit(driver, "after submit")
                return True
            if review_btns:
                btn = review_btns[0]
                if self.highlight:
                    focus_element(driver, btn, pause=self.step_delay)
                btn.click()
                self._after_ui_click()
                self._user_pause_pending_after_nav = True
            elif next_btns:
                btn = next_btns[0]
                if self.highlight:
                    focus_element(driver, btn, pause=self.step_delay)
                btn.click()
                self._after_ui_click()
                self._user_pause_pending_after_nav = True
            else:
                log.warning("No navigation button found at step %d", step)
                self._abandon_apply_and_dismiss(
                    driver,
                    job,
                    "no Continue / Review / Submit — closing modal",
                )
                return False

        log.error("Exceeded max steps (%d) without submitting", max_steps)
        self._abandon_apply_and_dismiss(driver, job, "max form steps exceeded")
        return False

    @staticmethod
    def _choice_labels_equivalent(want: str, option: str) -> bool:
        """Match rule answers (Yes/No) to option label text or values (true/false, etc.)."""
        w = (want or "").strip().lower()
        o = (option or "").strip().lower()
        if not w or not o:
            return False
        if w == o:
            return True
        yes = frozenset({"yes", "y", "true", "1", "on"})
        no = frozenset({"no", "n", "false", "0", "off"})
        return (w in yes and o in yes) or (w in no and o in no)

    def _click_radio_target(self, driver: Any, el: Any) -> None:
        if self.highlight:
            focus_element(driver, el, pause=0.2)
        try:
            el.click()
        except Exception:
            driver.execute_script("arguments[0].click();", el)

    def _legend_for_linkedin_radio_fieldset(self, fieldset: Any) -> str:
        """Question text from LinkedIn ``data-test-form-builder-radio-button-form-component`` fieldsets."""
        for sel in (
            "[data-test-form-builder-radio-button-form-component__title]",
            "legend .fb-dash-form-element__label",
            "legend",
        ):
            try:
                els = fieldset.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = (els[0].text or "").strip()
                    if t:
                        return t
            except Exception:
                continue
        return ""

    @staticmethod
    def _linkedin_radio_fieldset_is_required(fieldset: Any) -> bool:
        try:
            for el in fieldset.find_elements(
                By.CSS_SELECTOR,
                "legend, legend .fb-dash-form-element__label, [data-test-form-builder-radio-button-form-component__required]",
            ):
                cls = (el.get_attribute("class") or "").lower()
                if "is-required" in cls or "required" in cls:
                    return True
        except Exception:
            pass
        for inp in fieldset.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
            try:
                if (inp.get_attribute("aria-required") or "").lower() == "true":
                    return True
                if inp.get_attribute("required") is not None:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _linkedin_radio_fieldset_is_selected(fieldset: Any) -> bool:
        for inp in fieldset.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
            try:
                if inp.is_selected():
                    return True
            except Exception:
                continue
        return False

    def _click_choice_in_radio_container(self, driver: Any, container: Any, choose_label: str) -> bool:
        """
        Click a radio option inside a fieldset or group by visible label / LinkedIn data-test attrs.
        ``choose_label`` is usually ``Yes`` or ``No`` from ``screening_yes_no`` rules.
        """
        target = (choose_label or "").strip()
        if not target:
            return False
        for opt in container.find_elements(By.CSS_SELECTOR, "[data-test-text-selectable-option]"):
            try:
                inp_list = opt.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                lab_list = opt.find_elements(By.CSS_SELECTOR, "label[data-test-text-selectable-option__label]")
                label_attr = ""
                if lab_list:
                    label_attr = (lab_list[0].get_attribute("data-test-text-selectable-option__label") or "").strip()
                opt_text = (lab_list[0].text or "").strip() if lab_list else ""
                inp_attr = ""
                if inp_list:
                    inp_attr = (inp_list[0].get_attribute("data-test-text-selectable-option__input") or "").strip()
                    if not inp_attr:
                        inp_attr = (inp_list[0].get_attribute("value") or "").strip()
                for candidate in (label_attr, opt_text, inp_attr):
                    if candidate and self._choice_labels_equivalent(target, candidate):
                        click_el = lab_list[0] if lab_list else (inp_list[0] if inp_list else opt)
                        self._click_radio_target(driver, click_el)
                        time.sleep(0.15)
                        for inp in container.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
                            try:
                                if inp.is_selected():
                                    return True
                            except Exception:
                                continue
                        return False
            except Exception:
                continue
        for r in container.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
            try:
                val = (r.get_attribute("value") or "").strip()
                rid = r.get_attribute("id") or ""
                label_text = ""
                if rid:
                    for lab in container.find_elements(By.CSS_SELECTOR, f'label[for="{rid}"]'):
                        label_text = (lab.text or "").strip()
                        if self._choice_labels_equivalent(target, label_text):
                            self._click_radio_target(driver, lab)
                            return True
                if val and self._choice_labels_equivalent(target, val):
                    self._click_radio_target(driver, r)
                    return True
            except Exception:
                continue
        return False

    def _fill_linkedin_form_builder_radio_fieldsets(
        self,
        driver: Any,
        job: dict,
        root: Any,
        *,
        assist: bool,
    ) -> tuple[bool, set[str]]:
        """
        LinkedIn Easy Apply single-choice groups (``data-test-form-builder-radio-button-form-component``).
        Uses ``screening_yes_no`` rules on the fieldset legend. Returns (ok, processed input ``name``s).
        """
        processed_names: set[str] = set()
        for fs in root.find_elements(By.CSS_SELECTOR, SEL["linkedin_radio_fieldset"]):
            try:
                for inp in fs.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
                    n = (inp.get_attribute("name") or "").strip()
                    if n:
                        processed_names.add(n)

                if self._linkedin_radio_fieldset_is_selected(fs):
                    continue

                label = self._legend_for_linkedin_radio_fieldset(fs)
                self._maybe_pause_for_user_on_first_empty_field(label)
                if self._linkedin_radio_fieldset_is_selected(fs):
                    continue

                required = self._linkedin_radio_fieldset_is_required(fs)
                ans = self._rules.screening_yes_no(label)
                if self._handle_screening_discard(driver, job, ans, assist=assist):
                    return False, processed_names
                if ans is None:
                    if required and not assist:
                        log.warning(
                            "No rule for required LinkedIn radio question (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False, processed_names
                    if required and assist:
                        log.debug(
                            "Assist: leaving required LinkedIn radio unanswered (no rule) label=%r",
                            label,
                        )
                    continue

                if self._click_choice_in_radio_container(driver, fs, ans):
                    self._after_field_fill()
                    log.info(
                        "Selected %r for LinkedIn radio question: %s",
                        ans,
                        (label or "")[:120],
                    )
                elif required and not assist:
                    log.warning(
                        "Could not select %r for required LinkedIn radio (job %s) label=%r — abandoning",
                        ans,
                        job.get("id"),
                        label,
                    )
                    return False, processed_names
            except Exception as e:
                log.debug("Skipping LinkedIn radio fieldset: %s", e)
        return True, processed_names

    def _label_for_radio_group(self, driver: Any, first_radio) -> str:
        """Best-effort question text for a radio group (fieldset legend or form-element wrapper)."""
        try:
            fs = first_radio.find_element(By.XPATH, "./ancestor::fieldset[1]")
            t = self._legend_for_linkedin_radio_fieldset(fs)
            if t:
                return t
            legs = fs.find_elements(By.TAG_NAME, "legend")
            if legs:
                t = (legs[0].text or "").strip()
                if t:
                    return t
        except Exception:
            pass
        try:
            wrap = first_radio.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'jobs-easy-apply-form-element')][1]",
            )
            t = (wrap.text or "").strip()
            if t:
                return t
        except Exception:
            pass
        try:
            wrap = first_radio.find_element(By.XPATH, "./ancestor::div[contains(@class,'fb-dash')][1]")
            t = (wrap.text or "").strip()
            if t:
                return t
        except Exception:
            pass
        return ""

    def _click_yes_no_in_radio_group(self, driver: Any, radios: list, want_yes: bool) -> bool:
        """Click the Yes or No control in a group of radios. Returns True if a click occurred."""
        want = "Yes" if want_yes else "No"
        for r in radios:
            try:
                fs = r.find_element(
                    By.XPATH,
                    "./ancestor::fieldset[@data-test-form-builder-radio-button-form-component][1]",
                )
                if self._click_choice_in_radio_container(driver, fs, want):
                    return True
            except NoSuchElementException:
                pass
            except Exception:
                continue
        for r in radios:
            val = (r.get_attribute("value") or "").strip().lower()
            if want_yes and val in ("yes", "true", "1", "y", "on"):
                if self._click_radio_control(driver, r):
                    return True
            if not want_yes and val in ("no", "false", "0", "n", "off"):
                if self._click_radio_control(driver, r):
                    return True
        for r in radios:
            try:
                rid = r.get_attribute("id")
                if not rid:
                    continue
                for lab in driver.find_elements(By.CSS_SELECTOR, f'label[for="{rid}"]'):
                    t = (lab.text or "").strip().lower()
                    if want_yes and t in ("yes", "y"):
                        if self._click_radio_control(driver, r, label=lab):
                            return True
                    if not want_yes and t in ("no", "n"):
                        if self._click_radio_control(driver, r, label=lab):
                            return True
            except Exception:
                continue
        return False

    def _click_radio_control(self, driver: Any, radio: Any, *, label: Any | None = None) -> bool:
        """Click a radio input; prefer the visible ``label`` when LinkedIn hides the input."""
        try:
            click_el = label or radio
            if self.highlight:
                focus_element(driver, click_el, pause=0.2)
            click_el.click()
            time.sleep(0.12)
            return radio.is_selected()
        except Exception:
            return False

    def _file_input_is_cover_letter_upload(self, driver: Any, el) -> bool:
        """True when this ``input[type=file]`` is for a cover letter (not résumé/CV)."""
        label = (self._get_label(driver, el) or "").lower()
        aria = (el.get_attribute("aria-label") or "").lower()
        if label.strip() in ("cv", "résumé", "resume") or (
            ("resume" in label or "résumé" in label) and "cover" not in label
        ):
            return False
        for blob in (label, aria):
            if "cover" in blob and "letter" in blob:
                return True
        for xpath in (
            "./ancestor::div[contains(@class,'jobs-easy-apply-form-element')][1]",
            "./ancestor::fieldset[1]",
            "./ancestor::div[contains(@class,'jobs-easy-apply-form')][1]",
        ):
            try:
                leg = el.find_element(By.XPATH, xpath)
                b = (leg.text or "").lower()
                if "cover letter" in b or ("upload" in b and "cover" in b and "letter" in b):
                    return True
            except Exception:
                continue
        return False

    def _headshot_path_for_upload(self) -> Path | None:
        p = self.headshot_image_path
        if p.is_file():
            return p.resolve()
        return None

    def _click_photo_upload_ctas(self, driver: Any, root) -> None:
        """
        LinkedIn often shows a visible **Photo** control before the ``input[type=file]`` is usable.
        Click matching buttons so the file input is present / focused in the DOM.
        """
        for el in root.find_elements(By.CSS_SELECTOR, "button, [role='button'], label"):
            try:
                if not el.is_displayed() or not el.is_enabled():
                    continue
                raw = (el.text or "").strip()
                text = " ".join(raw.lower().split())
                aria = (el.get_attribute("aria-label") or "").strip().lower()
                if not text and not aria:
                    continue
                wants = text == "photo" or aria == "photo"
                if not wants and aria:
                    wants = ("photo" in aria or "headshot" in aria) and (
                        "upload" in aria or "add" in aria or "choose" in aria
                    )
                if not wants:
                    continue
                if self.highlight:
                    focus_element(driver, el, pause=0.2)
                el.click()
                self._after_ui_click()
                time.sleep(0.35)
                log.info("Clicked Photo / headshot control to enable file upload")
            except Exception:
                continue

    def _file_input_is_photo_upload(self, driver: Any, el) -> bool:
        """True for headshot / photo widgets (not résumé, not cover letter)."""
        if self._file_input_is_cover_letter_upload(driver, el):
            return False
        label = (self._get_label(driver, el) or "").lower()
        aria = (el.get_attribute("aria-label") or "").lower()
        accept = (el.get_attribute("accept") or "").lower()
        blob = f"{label} {aria}"
        try:
            wrap = el.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'jobs-easy-apply-form-element')][1]",
            )
            blob += " " + (wrap.text or "").lower()
        except Exception:
            pass
        if "cover letter" in blob:
            return False
        resumeish = ("résumé" in blob or "resume" in blob or " cv" in blob or blob.strip().startswith("cv"))
        if resumeish and not any(k in blob for k in ("photo", "headshot", "picture", "portrait", "image")):
            return False
        if any(k in blob for k in ("photo", "headshot", "portrait")):
            return True
        if "picture" in blob and "cover" not in blob:
            return True
        if accept and "image" in accept and "pdf" not in accept and "doc" not in accept:
            if "resume" not in blob and "cv" not in blob and "cover" not in blob:
                return True
        return False

    def assist_fill_current_modal(self, driver: Any, resume: dict, cover_letter: str, job: dict) -> None:
        """
        Fill whatever we can on the current Easy Apply step **without** clicking Next/Submit.
        Also supports Workday-style apply pages (``data-automation-id`` fields, often in an iframe).
        Unknown required fields are left for the user. Safe to call repeatedly (skips non-empty fields).
        """
        try:
            self._fill_step(driver, resume, cover_letter, job, assist=True)
        except Exception as e:
            log.debug("Assist fill pass skipped: %s", e)
        finally:
            self._default_content(driver)

    def _fill_step(
        self,
        driver: Any,
        resume: dict,
        cover_letter: str,
        job: dict,
        *,
        assist: bool = False,
        _after_user_pause_retry: bool = False,
    ) -> bool:
        """
        Fill the current step. Returns False if we should abandon (unknown required field with no rule);
        the caller will dismiss the modal — unless ``assist`` is True (manual apply mode: skip unknowns).

        After the manual-fill pause on a new step, runs a second pass so user-filled values are seen
        before we validate or click Continue / Review / Submit.
        """
        if not _after_user_pause_retry:
            self._user_pause_consumed_this_step = False
        root = self._resolve_fill_root(driver)
        if root is None:
            if assist:
                return True
            log.warning("No fill root: no LinkedIn Easy Apply modal and no Workday-style apply fields found")
            return False

        if self._has_empty_repeatable_education_grouping(root):
            if assist:
                log.debug(
                    "Assist: detected repeatable education section with empty School placeholder; leaving for user"
                )
            else:
                log.warning(
                    "Detected repeatable Education section with empty School placeholder "
                    "(LinkedIn draft card values like '--') — abandoning"
                )
                return False

        filled_cover_letter_as_text = False

        for input_el in root.find_elements(By.CSS_SELECTOR, SEL["text_input"]):
            try:
                label = self._get_label(driver, input_el)
                if self._control_is_cover_letter_field(driver, input_el):
                    if not (cover_letter or "").strip():
                        log.warning(
                            "Cover letter text field detected but generated cover letter is empty — skipping"
                        )
                        continue
                    self._replace_text_control_value(driver, input_el, cover_letter)
                    self._after_field_fill()
                    filled_cover_letter_as_text = True
                    log.info("Filled cover letter into text field (replaced any prior / LinkedIn draft text).")
                    continue

                current = (input_el.get_attribute("value") or "").strip()
                if current:
                    continue
                if self._automation_id_is_skipped(input_el):
                    continue
                if self._label_looks_like_robot_trap(label):
                    continue
                self._maybe_pause_for_user_on_first_empty_field(label)
                current = (input_el.get_attribute("value") or "").strip()
                if current:
                    continue
                required = self._element_is_required(input_el)
                candidates = self._rules.text_input_fill_candidates(label, resume)
                if candidates and DISCARD_APPLY in candidates:
                    if self._handle_screening_discard(driver, job, DISCARD_APPLY, assist=assist):
                        return False
                if not candidates:
                    if assist and "email" in (label or "").lower():
                        log.debug(
                            "Assist: email field label matched rules but no value (set email in data/resume_profile.json): %r",
                            label[:120],
                        )
                    if required and not assist:
                        log.warning(
                            "No rule for required text field (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False
                    if required and assist:
                        log.debug("Assist: leaving required text field empty (no rule) label=%r", label)
                    continue
                filled = False
                for try_val in candidates:
                    if try_val == DISCARD_APPLY:
                        continue
                    if not try_val:
                        continue
                    self._activate_text_control_before_fill(driver, input_el)
                    if self.highlight:
                        focus_element(driver, input_el, pause=0.12)
                    try:
                        input_el.clear()
                    except Exception:
                        pass
                    input_el.send_keys(try_val)
                    self._after_field_fill()
                    time.sleep(0.28)
                    after = (input_el.get_attribute("value") or "").strip()
                    if after:
                        filled = True
                        if len(candidates) > 1 and try_val != candidates[0]:
                            log.info(
                                "Text field accepted fallback value for label=%r (tried %d option(s)).",
                                (label or "")[:100],
                                candidates.index(try_val) + 1,
                            )
                        if self._rules.text_input_press_enter_after_fill(label):
                            try:
                                input_el.send_keys(Keys.RETURN)
                                self._after_field_fill()
                                time.sleep(0.35)
                                log.debug(
                                    "Pressed Enter after fill for autocomplete label=%r",
                                    (label or "")[:120],
                                )
                            except Exception:
                                log.debug(
                                    "Press Enter after fill failed label=%r",
                                    (label or "")[:120],
                                    exc_info=True,
                                )
                        break
                    try:
                        input_el.clear()
                    except Exception:
                        pass
                if not filled and len(candidates) > 1:
                    log.debug(
                        "All rule values left field empty after fill attempts label=%r",
                        (label or "")[:120],
                    )
                if not filled and required and not assist:
                    log.warning(
                        "Required text field stayed empty after rule fill attempts (job %s) label=%r — abandoning",
                        job.get("id"),
                        label,
                    )
                    return False
                if not filled and required and assist:
                    log.debug("Assist: required text field still empty after candidates label=%r", (label or "")[:120])
            except Exception as e:
                log.debug("Skipping text field: %s", e)

        for ta in root.find_elements(By.CSS_SELECTOR, SEL["textarea"]):
            try:
                label = self._get_label(driver, ta)
                is_cover = self._control_is_cover_letter_field(driver, ta)
                current = self._control_text_snapshot(ta)
                if current and not is_cover:
                    continue
                if not current or is_cover:
                    self._maybe_pause_for_user_on_first_empty_field(label)
                    current = self._control_text_snapshot(ta)
                    if current and not is_cover:
                        continue

                text = self._answer_textarea(label, cover_letter)
                if self._handle_screening_discard(driver, job, text, assist=assist):
                    return False
                if text is None and is_cover and (cover_letter or "").strip():
                    text = cover_letter
                required = self._element_is_required(ta)
                if text is None:
                    if required and not assist:
                        log.warning(
                            "No rule for required textarea (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False
                    if required and assist:
                        log.debug("Assist: leaving required textarea empty (no rule) label=%r", label)
                    continue
                if text:
                    if is_cover:
                        self._replace_text_control_value(driver, ta, text)
                        filled_cover_letter_as_text = True
                        log.info(
                            "Filled cover letter into textarea (replaced any prior / LinkedIn draft text)."
                        )
                    else:
                        self._activate_text_control_before_fill(driver, ta)
                        if self.highlight:
                            focus_element(driver, ta, pause=0.12)
                        ta.clear()
                        ta.send_keys(text)
                    self._after_field_fill()
            except Exception as e:
                log.debug("Skipping textarea: %s", e)

        for sel_el in root.find_elements(By.CSS_SELECTOR, SEL["select"]):
            try:
                if not self._select_needs_fill(sel_el):
                    continue
                label = self._get_label(driver, sel_el)
                self._maybe_pause_for_user_on_first_empty_field(label)
                if not self._select_needs_fill(sel_el):
                    continue
                if not self._element_is_required(sel_el):
                    continue
                opt_els = sel_el.find_elements(By.TAG_NAME, "option")
                preferred = self._answer_select_value(label, opt_els)
                if self._handle_screening_discard(driver, job, preferred, assist=assist):
                    return False
                dd = Select(sel_el)
                if preferred:
                    self._apply_select_choice(dd, opt_els, preferred)
                    self._after_field_fill()
                else:
                    if assist:
                        log.debug(
                            "Assist: skipping required dropdown (no rule) label=%r",
                            label,
                        )
                    else:
                        log.warning(
                            "No selection rule for required dropdown (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False
            except Exception as e:
                log.debug("Skipping select: %s", e)

        linkedin_radio_ok, linkedin_radio_names = self._fill_linkedin_form_builder_radio_fieldsets(
            driver, job, root, assist=assist
        )
        if not linkedin_radio_ok:
            return False

        radio_groups: dict[str, list] = defaultdict(list)
        for radio in root.find_elements(By.CSS_SELECTOR, SEL["radio"]):
            try:
                name = radio.get_attribute("name") or ""
                if name and name in linkedin_radio_names:
                    continue
                if name:
                    radio_groups[name].append(radio)
            except Exception:
                continue
        for name, radios in radio_groups.items():
            try:
                if any(r.is_selected() for r in radios):
                    continue
                label = self._label_for_radio_group(driver, radios[0])
                self._maybe_pause_for_user_on_first_empty_field(label)
                if any(r.is_selected() for r in radios):
                    continue
                ans = self._rules.screening_yes_no(label)
                if self._handle_screening_discard(driver, job, ans, assist=assist):
                    return False
                if ans is None:
                    # Legacy: prefer Yes when value hints yes (unknown questions) — not in assist mode
                    if not assist and any(
                        (r.get_attribute("value") or "").lower() in ("yes", "true", "1")
                        for r in radios
                    ):
                        for r in radios:
                            val = (r.get_attribute("value") or "").lower()
                            if val in ("yes", "true", "1"):
                                if self.highlight:
                                    focus_element(driver, r, pause=0.2)
                                r.click()
                                self._after_field_fill()
                                break
                    continue
                if ans.strip().lower() in ("yes", "no"):
                    want_yes = ans.strip().lower() == "yes"
                    if self._click_yes_no_in_radio_group(driver, radios, want_yes):
                        self._after_field_fill()
                else:
                    try:
                        fs = radios[0].find_element(By.XPATH, "./ancestor::fieldset[1]")
                    except Exception:
                        fs = None
                    clicked = False
                    if fs is not None:
                        clicked = self._click_choice_in_radio_container(driver, fs, ans)
                    if not clicked:
                        for r in radios:
                            val = (r.get_attribute("value") or "").strip()
                            if val and self._choice_labels_equivalent(ans, val):
                                if self.highlight:
                                    focus_element(driver, r, pause=0.2)
                                r.click()
                                self._after_field_fill()
                                clicked = True
                                break
                    if clicked:
                        self._after_field_fill()
            except Exception as e:
                log.debug("Skipping radio group %s: %s", name, e)

        self._click_photo_upload_ctas(driver, root)

        for finp in root.find_elements(By.CSS_SELECTOR, 'input[type="file"]'):
            try:
                if filled_cover_letter_as_text:
                    log.info(
                        "Skipping cover letter file upload — cover letter was already entered as text on this step."
                    )
                    continue
                if (finp.get_attribute("value") or "").strip():
                    continue
                if self._file_input_is_cover_letter_upload(driver, finp):
                    if not (cover_letter or "").strip():
                        log.warning("Cover letter upload requested but generated cover letter is empty — skipping")
                        continue
                    docx_path = cover_letter_docx_path_unique(
                        self.cover_letter_docx_dir,
                        site="linkedin",
                        company=str(job.get("company") or ""),
                        title=str(job.get("title") or ""),
                        job_id=str(job.get("id") or "job"),
                    )
                    write_cover_letter_docx(cover_letter, docx_path)
                    finp.send_keys(str(docx_path.resolve()))
                    self._after_field_fill()
                    log.info("Uploaded cover letter as DOCX: %s", docx_path)
                    continue
                if self._file_input_is_photo_upload(driver, finp):
                    photo_path = self._headshot_path_for_upload()
                    if photo_path is None:
                        msg = (
                            f"Photo upload field present but headshot file not found: {self.headshot_image_path}"
                        )
                        if self._element_is_required(finp) and not assist:
                            log.warning("%s — abandoning", msg)
                            return False
                        log.warning("%s — skipping", msg)
                        continue
                    finp.send_keys(str(photo_path))
                    self._after_field_fill()
                    log.info("Uploaded headshot for photo field: %s", photo_path)
            except Exception as e:
                log.warning("File upload failed: %s", e)

        if self._user_pause_consumed_this_step and not _after_user_pause_retry:
            log.info("Re-scanning step after manual-fill pause (re-check empty fields and auto-fill).")
            return self._fill_step(
                driver,
                resume,
                cover_letter,
                job,
                assist=assist,
                _after_user_pause_retry=True,
            )
        return True

    def _select_needs_fill(self, sel_el) -> bool:
        """True when the dropdown is still on the placeholder / unset."""
        cur = (sel_el.get_attribute("value") or "").strip()
        if not cur:
            return True
        if cur.lower() in ("select an option", "select"):
            return True
        return False

    def _answer_select_value(self, label: str, _opt_els) -> str | None:
        """
        Return the ``value=`` (or matching visible text) we should choose, or ``None`` if there is
        no rule — the caller will abandon the application (Dismiss) instead of guessing (e.g. "Yes").
        Rules: ``data/form_fill_rules.json`` (``selects`` + ``screening_yes_no``).
        """
        return self._rules.answer_select(label)

    def _apply_select_choice(self, dd: Select, opt_els, preferred_value: str) -> None:
        """Set dropdown to ``preferred_value`` (matches ``value=`` or visible text)."""
        pv = preferred_value.strip()
        for o in opt_els:
            v = (o.get_attribute("value") or "").strip()
            t = (o.text or "").strip()
            if v.lower() == pv.lower():
                dd.select_by_value(v)
                return
            if t.lower() == pv.lower():
                dd.select_by_visible_text(t)
                return
        dd.select_by_value(pv)

    def _answer_text_field(self, label: str, resume: dict) -> str | None:
        """
        Return text to type, ``""`` when the label matches a rule that intentionally leaves the field
        blank, or ``None`` when there is nothing we can truthfully fill (caller abandons if required).
        Rules: ``data/form_fill_rules.json`` (``text_inputs`` + ``screening_yes_no``); resume keys match
        ``data/resume_profile.json``.
        """
        return self._rules.answer_text_field(label, resume)

    def _answer_textarea(self, label: str, cover_letter: str) -> str | None:
        """
        Return text for a textarea, or ``None`` if the label does not match a known pattern
        (caller abandons when the field is required). Rules: ``data/form_fill_rules.json`` (``textareas``).
        """
        return self._rules.answer_textarea(label, cover_letter)

    def _get_label(self, driver, element) -> str:
        """
        Prefer ``<label for=id>`` text; then aria / placeholder; then Workday-style
        ``data-automation-id``, ``autocomplete``, ``formField-*`` wrappers.
        """
        try:
            el_id = element.get_attribute("id")
            if el_id:
                labels = driver.find_elements(By.CSS_SELECTOR, f'label[for="{el_id}"]')
                if labels:
                    t = (labels[0].text or "").strip()
                    t = re.sub(r"\s*\*+\s*$", "", t).strip()
                    if t:
                        return t

            aria = (element.get_attribute("aria-label") or "").strip()
            if aria:
                return re.sub(r"\s*\*+\s*$", "", aria).strip()

            pl = (element.get_attribute("placeholder") or "").strip()
            if pl:
                return pl

            dai = (element.get_attribute("data-automation-id") or "").strip()
            if dai:
                return dai

            aut = (element.get_attribute("autocomplete") or "").strip().lower()
            if aut == "email":
                return "email"
            if aut in ("tel", "phone"):
                return "phone"

            el_type = (element.get_attribute("type") or "").strip().lower()
            if el_type == "email":
                return "email"

            try:
                wrap = element.find_element(
                    By.XPATH,
                    './ancestor::*[starts-with(@data-automation-id, "formField-")][1]',
                )
                fid = (wrap.get_attribute("data-automation-id") or "").strip()
                if fid.startswith("formField-"):
                    tail = fid[len("formField-") :].strip()
                    return tail.replace("-", " ").strip() or fid
            except NoSuchElementException:
                pass
        except Exception:
            pass
            return ""
