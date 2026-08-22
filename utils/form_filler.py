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
from typing import TYPE_CHECKING, Any

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from .chrome_driver import (
    DEFAULT_COOKIE_PATH,
    build_chrome,
    driver_session_alive,
    focus_element,
    interruptible_sleep,
    scroll_into_view,
    load_cookies,
    log_driver_session_closed,
    quit_chrome,
)
from .cover_letter import cover_letter_docx_path_unique, write_cover_letter_docx
from .display_utils import waiting_message
from .form_fill_rules import DISCARD_APPLY, FormFillRulesEngine
from .output_paths import COVERLETTERS_DIR, LINKEDIN_COVERLETTERS_DIR

if TYPE_CHECKING:  # avoids a circular import
    from .s3_log_sync import PendingChangeTracker

log = logging.getLogger(__name__)

# Easy Apply “Photo” / headshot steps: ``send_keys`` with an absolute path to this file (if it exists).
DEFAULT_HEADSHOT_IMAGE = Path("data/selfInSuit.png")

SEL = {
    # Primary apply CTA on the job detail pane (two-pane search or /jobs/view/…).
    "apply_button_id": "jobs-apply-button-id",
    # Newer LinkedIn: ``<a aria-label="Easy Apply to this job" href="…/apply/?openSDUIApplyFlow=true…">``.
    # Some rollouts use ``LinkedIn Apply to …`` instead of ``Easy Apply``.
    # Legacy: ``button.jobs-apply-button`` / ``#jobs-apply-button-id``.
    "easy_apply_btn": (
        'a[aria-label="Easy Apply to this job"], '
        'a[aria-label*="Easy Apply to this job"], '
        'a[aria-label*="Easy Apply"][href*="/apply"], '
        'a[aria-label*="LinkedIn Apply to"], '
        'button[aria-label*="LinkedIn Apply to"], '
        'a[href*="openSDUIApplyFlow=true"], '
        'button.jobs-apply-button, '
        'button[aria-label*="Easy Apply"]'
    ),
    # Classic light-DOM Easy Apply sheet (still used after openSDUIApplyFlow clicks).
    "modal": (
        ".jobs-easy-apply-modal, "
        'div[role="dialog"].jobs-easy-apply-modal, '
        'div[data-test-modal][role="dialog"].artdeco-modal'
    ),
    # Alternate SDUI host used on some /jobs/search-results/ rollouts.
    "sdui_shadow_host": '[data-testid="interop-shadowdom"]',
    "next_btn": (
        'button[aria-label="Continue to next step"], '
        'button[aria-label*="Continue to next step"], '
        'button[data-easy-apply-next-button], '
        'button[data-live-test-easy-apply-next-button]'
    ),
    "review_btn": (
        'button[aria-label="Review your application"], '
        'button[aria-label*="Review your application"]'
    ),
    "submit_btn": (
        'button[aria-label="Submit application"], '
        'button[aria-label*="Submit application"]'
    ),
    "close_btn": 'button[aria-label="Dismiss"], button[aria-label="dismiss"], button[aria-label="Close"]',
    # Post-submit success / blocking overlay — must dismiss before the next job in the same session.
    "done_btn": (
        'button[aria-label="Done"], '
        ".jobs-easy-apply-modal button[aria-label=\"Done\"]"
    ),
    "upload_resume": 'input[name="file"]',
    "text_input": "input[type='text'], input[type='number'], input[type='tel']",
    "textarea": "textarea",
    "select": "select",
    "radio": "input[type='radio']",
    "linkedin_radio_fieldset": 'fieldset[data-test-form-builder-radio-button-form-component="true"]',
    "checkbox": "input[type='checkbox']",
    "linkedin_checkbox_fieldset": (
        'fieldset[data-test-form-builder-checkbox-form-component="true"], '
        'fieldset[data-test-checkbox-form-component="true"]'
    ),
    "error_msg": ".artdeco-inline-feedback--error",
}

# LinkedIn "Job search safety reminder" / possible fraud pre-apply dialog (not the Easy Apply sheet).
JOB_TRUST_SAFETY_MODAL_CONTENT = ".job-trust-pre-apply-safety-tips-modal__content"
JOB_TRUST_SAFETY_MODAL_DISMISS = (
    "button.artdeco-modal__dismiss[data-test-modal-close-btn], "
    "button[data-test-modal-close-btn].artdeco-modal__dismiss"
)

APPLY_ABORT_JOB_TRUST_SAFETY = "job_trust_safety_reminder"

# LinkedIn daily Easy Apply submission cap. Two known presentations:
#  - older: inline feedback message under a grayed-out Apply button (e.g. "We limit daily
#    submissions to maintain quality and prevent bots… Save this job and apply tomorrow.").
#  - newer: a popup dialog on Apply click, identified by its stable (locale-independent)
#    ``data-sdui-screen`` value — "You reached today's Easy Apply limit … Save this job and
#    continue applying tomorrow."
APPLY_ABORT_DAILY_LIMIT = "linkedin_daily_application_limit"
LINKEDIN_DAILY_LIMIT_MESSAGE = "artdeco-inline-feedback__message"
LINKEDIN_DAILY_LIMIT_DIALOG_SCREEN = "com.linkedin.sdui.flagshipnav.jobs.EasyApplyFuseLimitDialogModal"
LINKEDIN_DAILY_LIMIT_SUBSTRINGS = (
    "we limit daily submissions",
    "apply tomorrow",
    "applying tomorrow",
    "easy apply limit",
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
DRAFT_SAVE_SELECTORS: tuple[str, ...] = (
    'button[data-control-name="save_application_btn"]',
    "button[data-test-dialog-primary-btn]",
    "button.artdeco-modal__actionbar--confirm-dialog button.artdeco-button--primary",
)
DRAFT_DISCARD_SELECTORS: tuple[str, ...] = (
    'button[data-control-name="discard_application_confirm_btn"]',
    'button[data-control-name="discard_application_btn"]',
    "button[data-test-dialog-secondary-btn]",
    "button.artdeco-modal__actionbar--confirm-dialog button.artdeco-button--secondary",
)
DRAFT_CONFIRM_DIALOG_SELECTORS: tuple[str, ...] = (
    '[role="alertdialog"].artdeco-modal--layer-confirmation',
    '[role="alertdialog"][data-test-modal]',
    '[role="alertdialog"]',
    "h2[data-test-dialog-title]",
)
SAVE_APPLICATION_PROMPT_TITLE = "save this application"
SAVE_APPLICATION_PROMPT_ROOT_CSS = (
    '[role="alertdialog"].artdeco-modal--layer-confirmation, '
    '[role="alertdialog"][data-test-modal]'
)
# Pierces open shadow roots — LinkedIn often mounts artdeco confirms outside light DOM.
_SAVE_PROMPT_DEEP_QUERY_JS = """
const queryAllDeep = (selector, root = document) => {
  const out = [];
  try {
    out.push(...root.querySelectorAll(selector));
  } catch (e) {}
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      out.push(...queryAllDeep(selector, el.shadowRoot));
    }
  }
  return out;
};
"""


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
        cover_letter_docx_dir: Path | str = LINKEDIN_COVERLETTERS_DIR,
        form_fill_rules_path: Path | str | None = None,
        helper_scan_all_tabs: bool = False,
        headshot_image_path: Path | str | None = None,
        cover_letter_tracker: "PendingChangeTracker | None" = None,
    ):
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = Path(session_file)
        self.cover_letter_docx_dir = Path(cover_letter_docx_dir)
        self.cover_letter_docx_dir.mkdir(parents=True, exist_ok=True)
        # Records each cover letter this filler writes, so a caller can flush them to S3's
        # operation log after the run (see utils/s3_log_sync.py) — None is a safe no-op.
        self.cover_letter_tracker = cover_letter_tracker
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
        # After clicking **Save** on "Save this application?", leftover Easy Apply chrome must not
        # be closed with Dismiss/X — that re-opens the confirm or discards the draft we just saved.
        self._saved_apply_draft_this_flow = False

    @staticmethod
    def _default_content(driver: Any) -> None:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    def _ensure_top_document(self, driver: Any) -> None:
        """Save/dismiss prompts live on the top document — not inside Workday/Greenhouse iframes."""
        self._default_content(driver)

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
        LinkedIn: visible Easy Apply sheet (legacy ``.jobs-easy-apply-modal`` or SDUI shadow dialog).
        Workday: ``body`` in the document or nested iframes when ``WORKDAY_FIELD_MARKERS`` match.
        Greenhouse: ``body`` when embedded apply fields match
        (``input.input__single-line``, ``input[id^="question_"]``, etc.), including inside iframes.
        Leaves ``driver`` inside the iframe when the form lives there.
        """
        self._default_content(driver)
        modal = self._find_easy_apply_modal(driver)
        if modal is not None:
            return modal
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
        scroll_into_view(driver, el)
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

    def _wait_for_typeahead_dropdown(self, driver, timeout: float = 2.0) -> bool:
        """
        Wait up to timeout seconds for a typeahead suggestion dropdown to appear.

        Needed for autocomplete fields (e.g. Location) where pressing Enter before suggestions
        load produces a validation error. Returns True if a dropdown was detected.
        """
        _TYPEAHEAD_SELECTORS = (
            "[role='listbox']",
            ".basic-typeahead__triggered-content",
            "ul.fb-typeahead-list",
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sel in _TYPEAHEAD_SELECTORS:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed():
                            return True
                except Exception:
                    pass
            time.sleep(0.1)
        return False

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
        text = (
            f"Pausing {pause:.1f}s for manual fill — first empty field after Continue/Review{hint}"
        )
        with waiting_message(text):
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
        if self._stop_dismiss_if_driver_closed(driver):
            return False
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
                    scroll_into_view(driver, btn)
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
        True when LinkedIn's daily Easy Apply submission cap is signaled — either the newer
        popup dialog (matched by its stable, locale-independent ``data-sdui-screen`` value) or
        the older inline feedback message under a grayed-out Apply button (matched by English
        text substrings, since it carries no comparable stable attribute).

        The inline message can sit under a grayed-out Apply button; such text is often not
        "visible" to Selenium, so we read ``textContent`` (via JS) rather than ``element.text``
        (which is empty for hidden nodes).
        """
        try:
            found = driver.execute_script(
                "return !!document.querySelector(arguments[0]);",
                f'[data-sdui-screen="{LINKEDIN_DAILY_LIMIT_DIALOG_SCREEN}"]',
            )
            if found:
                return True
        except Exception as e:
            log.debug("Daily-limit dialog scan failed (%s); falling back to text scan.", e)

        subs = list(LINKEDIN_DAILY_LIMIT_SUBSTRINGS)
        try:
            found = driver.execute_script(
                """
                const subs = arguments[0];
                const nodes = document.querySelectorAll(
                  '.artdeco-inline-feedback__message, .artdeco-inline-feedback, [class*="inline-feedback"], '
                  + 'dialog[data-testid="dialog"]'
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

    def _attempt_easy_apply_click(self, driver: Any, job: dict) -> bool | None:
        """
        Find the Apply control, click it once, and wait for the sheet to open.

        Returns ``True`` when the modal opened (caller proceeds to fill), ``False`` when a
        definitive stop condition fired — daily limit, trust/safety modal, or an
        invalid-looking control (already logged; caller must not retry) — or ``None`` when the
        click produced no modal and nothing else explains why (caller may click again; observed
        live where a manual click on the same job worked fine, so this is a real, retryable race
        rather than a permanently broken control).
        """
        if self._abort_apply_for_daily_limit(driver, job, when="Apply button replaced by limit message"):
            return False

        apply_btn = self._find_apply_button(driver)
        if not apply_btn:
            if self._abort_apply_for_daily_limit(
                driver, job, when="no Apply button — limit message shown"
            ):
                return False
            raise RuntimeError(
                "Apply button not found: expected Easy Apply link "
                "(aria-label Easy Apply to this job / openSDUIApplyFlow) "
                "or legacy #jobs-apply-button-id / button.jobs-apply-button"
            )
        if not self._apply_control_looks_valid(apply_btn):
            href = (apply_btn.get_attribute("href") or "")[:180]
            label = (apply_btn.get_attribute("aria-label") or apply_btn.text or "")[:120]
            log.warning(
                "Refusing to click Apply-looking control that is not a jobs Easy Apply "
                "target (tag=%s aria-label=%r href=%r)",
                (apply_btn.tag_name or "").lower(),
                label,
                href,
            )
            return False
        try:
            pre_url = (driver.current_url or "")
        except WebDriverException:
            pre_url = ""
        # Read tag once, up front — scroll_into_view()/focus_element() below can trigger a
        # lazy-load/intersection-observer re-render that detaches apply_btn from the DOM, and
        # a second .tag_name read afterward would raise StaleElementReferenceException with
        # no guard around it (this is what caused the apply flow to crash uncaught).
        tag = (apply_btn.tag_name or "").lower()
        log.info(
            "Clicking Easy Apply control: tag=%s aria-label=%r href=%r",
            tag,
            (apply_btn.get_attribute("aria-label") or apply_btn.text or "")[:120],
            (apply_btn.get_attribute("href") or "")[:180],
        )
        scroll_into_view(driver, apply_btn)
        if self.highlight:
            focus_element(driver, apply_btn, pause=self.step_delay)
        # Native click first — a JS-dispatched click (execute_script) produces an untrusted
        # DOM event, and LinkedIn's apply-flow handler appears to silently reject those
        # (observed as the click landing on the search-results URL with an eBP=NOT_ELIGIBLE_
        # FOR_CHARGING query param instead of opening the modal). A native Selenium click can
        # occasionally fall through to the <a>'s literal href and leave the jobs search shell
        # (e.g. landing on /feed/update/…) — _recover_if_left_jobs_context below is the safety
        # net for that, so we no longer need to prefer JS click to avoid it.
        clicked = False
        try:
            apply_btn.click()
            clicked = True
        except StaleElementReferenceException:
            # LinkedIn's SDUI apply flow can mutate the DOM synchronously on click (e.g.
            # swapping in the daily-limit popup) — the click itself already landed even
            # though Selenium's call raises stale. Do NOT retry with this same handle.
            log.debug("Apply button went stale right after click (likely UI mutated on click).")
            clicked = True
        except Exception:
            clicked = False
        if not clicked:
            try:
                driver.execute_script("arguments[0].click();", apply_btn)
            except StaleElementReferenceException:
                log.debug(
                    "Apply button went stale on JS click fallback (likely UI mutated on click)."
                )
        self._after_ui_click()

        # Navigation away from jobs can be async — poll briefly instead of checking once. This
        # already navigates back on our behalf, so a click that fell through to the href is
        # itself a retryable case, not a hard abort.
        if self._recover_if_left_jobs_context(
            driver, job, pre_url=pre_url, context="after Easy Apply click"
        ):
            return None

        if self._abort_apply_for_job_trust_safety(driver, job):
            return False

        if self._abort_apply_for_daily_limit(driver, job, when="after clicking Apply"):
            return False

        # The new Easy Apply control is an <a href="…/apply/?openSDUIApplyFlow=true…">.
        # The sheet (still ``.jobs-easy-apply-modal`` in current UI) can take a moment to mount
        # after that click — do not start filling until it is visible. A longer wait here used
        # to be needed on the theory that fresh "Easy Apply" clicks are just slower to load than
        # "Continue" (resuming a draft); that turned out to be wrong — the real failure mode is
        # a click that silently doesn't register at all (confirmed live: a manual click on the
        # same job worked immediately), which no amount of waiting fixes. The caller now retries
        # the click itself on a timeout, so a shorter wait here means failing fast into that
        # retry instead of stalling on a click that was never going to open anything.
        modal = self._wait_for_easy_apply_modal(driver, timeout_s=8.0)
        if modal is None:
            try:
                fail_url = (driver.current_url or "")
            except WebDriverException:
                fail_url = ""
            log.warning(
                "Easy Apply sheet did not open after clicking Apply "
                "(expected .jobs-easy-apply-modal or SDUI shadow dialog; url=%s)",
                fail_url[:200],
            )
            self._recover_if_left_jobs_context(
                driver, job, pre_url=pre_url, context="after Easy Apply modal wait"
            )
            return None
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

            if not interruptible_sleep(self.easy_apply_wait_seconds, driver):
                return False

            # Leftover success / error modal blocks the next Apply on the same driver.
            # If the previous job saved an Easy Apply draft, do not Dismiss/X that leftover
            # sheet — that can discard the draft. Soft-close (Escape / wait) instead.
            if self._saved_apply_draft_this_flow:
                self._soft_close_overlays_after_draft_save(driver)
            else:
                self._dismiss_easy_apply_modal_if_open(driver, "before apply")
            self._saved_apply_draft_this_flow = False
            if self._stop_dismiss_if_driver_closed(driver):
                return False

            # Click Apply and wait for the sheet — retry once if the click produced no modal and
            # nothing else explains why (observed live: a manual click on the same job worked
            # fine right after an automated click silently didn't, so this is a real race worth
            # retrying, not a permanently broken control).
            max_click_attempts = 2
            opened = False
            for attempt in range(max_click_attempts):
                if attempt:
                    log.info(
                        "Easy Apply sheet did not open — retrying click (attempt %d/%d) for %s",
                        attempt + 1,
                        max_click_attempts,
                        job.get("id"),
                    )
                    time.sleep(1.5)
                result = self._attempt_easy_apply_click(driver, job)
                if result is True:
                    opened = True
                    break
                if result is False:
                    return False
                # result is None — retryable; loop continues (or exits if out of attempts)
            if not opened:
                try:
                    path = self.screenshot_dir / f"error_{job['id']}.png"
                    driver.save_screenshot(str(path))
                except Exception:
                    pass
                return False

            return self._fill_form(driver, resume, cover_letter, job)
        except WebDriverException as e:
            if not driver_session_alive(driver):
                log_driver_session_closed()
            else:
                log.warning("WebDriver error during Easy Apply (browser still alive) — skipping job: %s", e)
            return False
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
                quit_chrome(driver)

    def _apply_control_looks_valid(self, el: Any) -> bool:
        """
        True when ``el`` looks like the job-pane Easy Apply control — not a feed/post link.

        Rejects ``/feed/``, ``/posts/``, and ``urn:li:activity`` hrefs that have been observed
        when a click lands on the wrong overlay/promo instead of Easy Apply.
        """
        try:
            tag = (el.tag_name or "").lower()
            href = (el.get_attribute("href") or "").strip().lower()
            label = (el.get_attribute("aria-label") or el.text or "").strip().lower()
            cls = (el.get_attribute("class") or "").lower()
        except Exception:
            return False
        bad_bits = ("/feed/", "/posts/", "urn:li:activity", "/feed/update")
        if href and any(b in href for b in bad_bits):
            return False
        if tag == "a":
            if "opensduiapplyflow" in href:
                return True
            if "/jobs/" in href and "/apply" in href:
                return True
            if "easy apply" in label and "/apply" in href:
                return True
            return False
        if tag == "button":
            if "jobs-apply-button" in cls or "easy apply" in label or "linkedin apply" in label:
                return True
            aid = (el.get_attribute("id") or "").strip()
            return aid == SEL["apply_button_id"]
        return False

    def _url_looks_like_jobs_context(self, url: str) -> bool:
        u = (url or "").lower()
        if not u:
            return True
        if any(b in u for b in ("/feed/", "/posts/", "urn:li:activity", "/feed/update")):
            return False
        return "linkedin.com/jobs" in u or "/jobs/" in u

    def _recover_if_left_jobs_context(
        self,
        driver: Any,
        job: dict,
        *,
        pre_url: str = "",
        context: str = "",
        timeout_s: float = 2.5,
    ) -> bool:
        """
        If the browser left the LinkedIn jobs UI (e.g. landed on ``/feed/update/…``), go back.

        Returns True when a recovery navigation was performed.
        """
        deadline = time.monotonic() + max(0.3, float(timeout_s))
        left_url = ""
        while time.monotonic() < deadline:
            try:
                cur = (driver.current_url or "")
            except WebDriverException:
                return False
            if cur and not self._url_looks_like_jobs_context(cur):
                left_url = cur
                break
            if not interruptible_sleep(0.2, driver):
                return False
        else:
            try:
                cur = (driver.current_url or "")
            except WebDriverException:
                return False
            if not cur or self._url_looks_like_jobs_context(cur):
                return False
            left_url = cur

        log.warning(
            "Left jobs context%s (url=%s) for %s at %s — recovering (pre_url=%s).",
            f" ({context})" if context else "",
            left_url[:220],
            job.get("title"),
            job.get("company"),
            (pre_url or "")[:180],
        )
        try:
            if pre_url and self._url_looks_like_jobs_context(pre_url):
                driver.get(pre_url)
            else:
                driver.back()
            time.sleep(1.0)
        except WebDriverException:
            try:
                driver.back()
                time.sleep(1.0)
            except WebDriverException:
                pass
        return True

    def _job_details_roots(self, driver: Any) -> list:
        """Prefer searching Easy Apply inside the right-hand job details pane."""
        roots: list = []
        for sel in (
            ".jobs-search__job-details--container",
            ".jobs-search__job-details",
            ".job-details-jobs-unified-top-card",
            ".jobs-details",
            "#job-details",
            "div.scaffold-layout__detail",
        ):
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed():
                            roots.append(el)
                    except Exception:
                        continue
            except Exception:
                continue
            if roots:
                break
        return roots

    def _element_is_genuinely_clickable(self, driver: Any, el: Any) -> bool:
        """
        Stronger check than Selenium's ``is_displayed()``.

        LinkedIn's SDUI system duplicates content for accessibility (e.g. the aria-hidden +
        visually-hidden label pattern found elsewhere in this file) — it's plausible the same
        applies to interactive elements: a "ghost" instance that is technically displayed
        (non-zero size, display != none) but invisible/non-interactive (opacity: 0, or a
        duplicate mid-transition), which ``is_displayed()`` alone would accept. Verify real
        on-screen opacity/position, and that this element (not some other overlay) is actually
        what ``elementFromPoint`` returns at its own center — matching the observed symptom of
        a click producing no visible focus-highlight outline and no effect.
        """
        try:
            return bool(
                driver.execute_script(
                    """
                    const el = arguments[0];
                    const style = getComputedStyle(el);
                    if (parseFloat(style.opacity) === 0) return false;
                    if (style.pointerEvents === 'none') return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return false;
                    const top = document.elementFromPoint(cx, cy);
                    return !!(top && (top === el || el.contains(top) || top.contains(el)));
                    """,
                    el,
                )
            )
        except Exception:
            return True  # don't block on a check we couldn't run

    def _find_apply_button(self, driver: Any):
        """
        Find the job-pane Easy Apply control.

        Prefer the newer ``<a aria-label="Easy Apply to this job">`` / ``openSDUIApplyFlow`` link,
        then legacy ``#jobs-apply-button-id`` / ``button.jobs-apply-button``.
        Polls briefly — the detail pane can lag after clicking a card in search results.
        """
        aid = SEL["apply_button_id"]
        pause = max(0.25, min(0.6, self.step_delay))

        def _visible_easy_apply():
            detail_roots = self._job_details_roots(driver)
            root_passes: list = list(detail_roots) if detail_roots else []
            root_passes.append(driver)  # whole document last

            seen: set[int] = set()
            for root in root_passes:
                try:
                    if root is driver:
                        els = driver.find_elements(By.CSS_SELECTOR, SEL["easy_apply_btn"])
                    else:
                        els = root.find_elements(By.CSS_SELECTOR, SEL["easy_apply_btn"])
                except Exception:
                    continue
                for el in els:
                    try:
                        key = id(el)
                        if key in seen:
                            continue
                        seen.add(key)
                        if not el.is_displayed():
                            continue
                        disabled = (el.get_attribute("aria-disabled") or "").strip().lower()
                        if disabled in ("true", "1"):
                            continue
                        if not self._apply_control_looks_valid(el):
                            continue
                        if not self._element_is_genuinely_clickable(driver, el):
                            continue
                        label = (el.get_attribute("aria-label") or el.text or "").lower()
                        href = (el.get_attribute("href") or "").lower()
                        if "easy apply" in label or "opensduiapplyflow" in href or "/apply" in href:
                            return el
                        tag = (el.tag_name or "").lower()
                        if tag == "button" and "jobs-apply-button" in (
                            (el.get_attribute("class") or "").lower()
                        ):
                            return el
                    except Exception:
                        continue
            return None

        for _ in range(24):
            if not driver_session_alive(driver):
                log_driver_session_closed()
                return None
            found = _visible_easy_apply()
            if found is not None:
                return found
            try:
                el = driver.find_element(By.ID, aid)
                if el.is_displayed():
                    return el
            except NoSuchElementException:
                pass
            except Exception:
                log_driver_session_closed()
                return None
            if not interruptible_sleep(pause, driver):
                return None

        found = _visible_easy_apply()
        if found is not None:
            return found
        try:
            el = driver.find_element(By.ID, aid)
            if el.is_displayed():
                return el
        except NoSuchElementException:
            pass
        return None

    def _find_legacy_easy_apply_modal(self, driver: Any):
        """Visible classic ``.jobs-easy-apply-modal`` / artdeco apply dialog in the light DOM."""
        for el in driver.find_elements(By.CSS_SELECTOR, SEL["modal"]):
            try:
                if not el.is_displayed():
                    continue
                # Prefer the real Easy Apply sheet over unrelated artdeco dialogs matched by
                # the broader ``[data-test-modal]`` fallback.
                cls = (el.get_attribute("class") or "").lower()
                labelled = (el.get_attribute("aria-labelledby") or "").lower()
                if "jobs-easy-apply-modal" in cls or labelled == "jobs-apply-header":
                    return el
                # Broader match only if it looks like an apply dialog.
                text = (el.text or "").lower()
                if "apply to" in text or "contact info" in text:
                    return el
            except Exception:
                continue
        # Header id is stable in the Inspect markup you shared.
        try:
            header = driver.find_element(By.ID, "jobs-apply-header")
            dlg = header.find_element(
                By.XPATH,
                './ancestor::*[@role="dialog" or contains(@class,"jobs-easy-apply-modal")][1]',
            )
            if dlg.is_displayed():
                return dlg
        except Exception:
            pass
        return None

    def _find_sdui_apply_modal(self, driver: Any):
        """
        SDUI apply dialog inside ``[data-testid="interop-shadowdom"]`` open shadow root
        (some LinkedIn rollouts). Returns None when that host is absent.
        """
        try:
            hosts = driver.find_elements(By.CSS_SELECTOR, SEL["sdui_shadow_host"])
        except Exception:
            return None
        for host in hosts:
            try:
                shadow = host.shadow_root
            except Exception:
                continue
            if shadow is None:
                continue
            try:
                dialogs = shadow.find_elements(
                    By.CSS_SELECTOR, '[role="dialog"], [role="alertdialog"]'
                )
            except Exception:
                continue
            for dialog in dialogs:
                try:
                    if not dialog.is_displayed():
                        continue
                    rect = dialog.rect or {}
                    if float(rect.get("width") or 0) < 100 or float(rect.get("height") or 0) < 100:
                        continue
                    return dialog
                except Exception:
                    continue
        return None

    def _find_easy_apply_modal(self, driver: Any):
        """Legacy light-DOM Easy Apply sheet, else SDUI shadow dialog."""
        legacy = self._find_legacy_easy_apply_modal(driver)
        if legacy is not None:
            return legacy
        return self._find_sdui_apply_modal(driver)

    def _wait_for_easy_apply_modal(self, driver: Any, timeout_s: float = 12.0):
        """Poll until the Easy Apply sheet is visible (or timeout)."""
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        while time.monotonic() < deadline:
            if self._stop_dismiss_if_driver_closed(driver):
                return None
            modal = self._find_easy_apply_modal(driver)
            if modal is not None:
                return modal
            if not interruptible_sleep(0.35, driver):
                return None
        return self._find_easy_apply_modal(driver)

    def _easy_apply_modal_is_open(self, driver: Any) -> bool:
        """True when the Easy Apply dialog is visible (blocks clicking Apply on the next job)."""
        return self._find_easy_apply_modal(driver) is not None

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
        if self._draft_save_confirm_open(driver):
            return None
        for group in (SEL["done_btn"], SEL["close_btn"], ", ".join(POST_APPLY_DISMISS)):
            for btn in driver.find_elements(By.CSS_SELECTOR, group):
                try:
                    if btn.is_displayed() and btn.is_enabled() and self._element_in_dialog_or_modal_shell(btn):
                        if self._button_in_save_confirm_dialog(btn):
                            continue
                        return btn
                except Exception:
                    continue
        return None

    def _blocking_apply_ui_open(self, driver: Any) -> bool:
        """
        True when some overlay still blocks the next **Apply** — either the Easy Apply sheet or a
        follow-up success / confirmation dialog (often a different DOM subtree than ``.jobs-easy-apply-modal``).
        """
        if self._draft_save_confirm_open(driver):
            return True
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

    def _stop_dismiss_if_driver_closed(self, driver: Any) -> bool:
        """Return True when the browser session is gone — dismiss/cleanup should stop."""
        if driver_session_alive(driver):
            return False
        log_driver_session_closed()
        return True

    def _close_extra_browser_windows(self, driver: Any) -> None:
        """
        If LinkedIn opened a second window/tab, close it and return focus to the **jobs** tab.

        We prefer a handle whose URL looks like the job search/detail page so we do not close the
        main session when the new tab briefly becomes ``current_window_handle``.
        """
        if not driver_session_alive(driver):
            return
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
        if self._stop_dismiss_if_driver_closed(driver):
            return False
        self._ensure_top_document(driver)
        if self._save_application_prompt_if_open(driver):
            return True
        extra = None if self._saved_apply_draft_this_flow else self._visible_post_apply_control(driver)
        if extra:
            try:
                if self._skip_dismiss_click_for_save_prompt(driver, extra):
                    return True
                scroll_into_view(driver, extra)
                if self.highlight:
                    focus_element(driver, extra, pause=0.2)
                extra.click()
                self._after_ui_click()
                if self._button_is_dismiss(extra) and not self._button_in_save_confirm_dialog(extra):
                    self._wait_for_save_application_prompt(driver, timeout_s=2.5)
                    self._save_application_prompt_if_open(driver)
                return True
            except Exception:
                pass
        done_only = (SEL["done_btn"],) if self._saved_apply_draft_this_flow else (
            SEL["done_btn"],
            ", ".join(POST_APPLY_DISMISS),
        )
        for sel in done_only:
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        if self._skip_dismiss_click_for_save_prompt(driver, btn):
                            return True
                        scroll_into_view(driver, btn)
                        if self.highlight:
                            focus_element(driver, btn, pause=0.2)
                        btn.click()
                        self._after_ui_click()
                        return True
                except Exception:
                    continue
        if self._saved_apply_draft_this_flow:
            return False
        for btn in driver.find_elements(By.CSS_SELECTOR, SEL["close_btn"]):
            try:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                if self._skip_dismiss_click_for_save_prompt(driver, btn):
                    return True
                scroll_into_view(driver, btn)
                if self.highlight:
                    focus_element(driver, btn, pause=0.2)
                btn.click()
                self._after_ui_click()
                self._wait_for_save_application_prompt(driver, timeout_s=2.5)
                self._save_application_prompt_if_open(driver)
                return True
            except Exception:
                continue
        try:
            modal = self._find_easy_apply_modal(driver)
            if modal is not None:
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
        if self._stop_dismiss_if_driver_closed(driver):
            return
        self._close_extra_browser_windows(driver)
        if not self._blocking_apply_ui_open(driver):
            return
        log.info(
            "Apply-blocking UI still open%s — dismissing (Done / Dismiss / close)",
            f" ({context})" if context else "",
        )
        for attempt in range(18):
            if self._stop_dismiss_if_driver_closed(driver):
                return
            self._close_extra_browser_windows(driver)
            if not self._blocking_apply_ui_open(driver):
                return
            if self._save_application_prompt_if_open(driver):
                continue
            if self._saved_apply_draft_this_flow:
                log.info(
                    "Easy Apply draft was saved — not clicking Dismiss/X on leftover overlay%s",
                    f" ({context})" if context else "",
                )
                self._soft_close_overlays_after_draft_save(driver)
                return
            if not self._click_done_or_close_in_modal(driver):
                if self._draft_save_confirm_open(driver):
                    self._log_save_prompt_probe(driver, context or "dismiss loop")
                    self._save_application_prompt_if_open(driver)
                    continue
                self._click_easy_apply_sheet_dismiss(driver)
            if not interruptible_sleep(0.35, driver):
                return
            self._wait_for_apply_overlays_closed(driver, timeout_s=1.5)
        if self._blocking_apply_ui_open(driver):
            log.warning(
                "Apply-blocking UI may still be visible after dismiss attempts%s — next apply may fail",
                f" ({context})" if context else "",
            )

    def _wait_then_dismiss_post_submit(self, driver: Any, context: str) -> None:
        """After **Submit**, success UI may mount a moment later in a different modal layer."""
        if self._stop_dismiss_if_driver_closed(driver):
            return
        self._close_extra_browser_windows(driver)
        for _ in range(22):
            if self._stop_dismiss_if_driver_closed(driver):
                return
            if self._blocking_apply_ui_open(driver):
                break
            if not interruptible_sleep(0.35, driver):
                return
        self._dismiss_easy_apply_modal_if_open(driver, context)

    @staticmethod
    def _element_is_required(el) -> bool:
        if el.get_attribute("required") is not None:
            return True
        return (el.get_attribute("aria-required") or "").lower() == "true"

    def _modal_has_unfilled_required_fields(self, driver: Any) -> bool:
        """True if a required control is still empty (we could not complete the step)."""
        modal = self._find_easy_apply_modal(driver)
        if modal is None:
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

    @staticmethod
    def _save_prompt_title_matches(text: str) -> bool:
        return SAVE_APPLICATION_PROMPT_TITLE in (text or "").strip().lower()

    @staticmethod
    def _element_visible_js(driver: Any, el: Any) -> bool:
        try:
            return bool(
                driver.execute_script(
                    """
                    const el = arguments[0];
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return false;
                    const st = window.getComputedStyle(el);
                    return st.visibility !== 'hidden'
                      && st.display !== 'none'
                      && Number(st.opacity || '1') > 0.05;
                    """,
                    el,
                )
            )
        except Exception:
            return False

    def _save_application_btn_visible_js(self, driver: Any) -> bool:
        """Most reliable signal: visible ``button[data-control-name="save_application_btn"]``."""
        self._ensure_top_document(driver)
        try:
            return bool(
                driver.execute_script(
                    _SAVE_PROMPT_DEEP_QUERY_JS
                    + """
                    const isVisible = (el) => {
                      if (!el) return false;
                      const r = el.getBoundingClientRect();
                      if (r.width < 2 || r.height < 2) return false;
                      const st = window.getComputedStyle(el);
                      return st.visibility !== 'hidden'
                        && st.display !== 'none'
                        && Number(st.opacity || '1') > 0.05;
                    };
                    for (const btn of queryAllDeep(
                      'button[data-control-name="save_application_btn"]'
                    )) {
                      if (isVisible(btn)) return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _find_save_prompt_in_shadow_hosts(self, driver: Any):
        """Selenium-side search inside ``interop-shadowdom`` and nested open shadow roots."""
        self._ensure_top_document(driver)
        try:
            hosts = driver.find_elements(By.CSS_SELECTOR, SEL["sdui_shadow_host"])
        except Exception:
            hosts = []
        for host in hosts:
            try:
                shadow = host.shadow_root
            except Exception:
                continue
            if shadow is None:
                continue
            try:
                for btn in shadow.find_elements(
                    By.CSS_SELECTOR, 'button[data-control-name="save_application_btn"]'
                ):
                    if self._element_visible_js(driver, btn):
                        return btn
            except Exception:
                continue
            try:
                for title_el in shadow.find_elements(
                    By.CSS_SELECTOR, "h2[data-test-dialog-title]"
                ):
                    title = (title_el.text or title_el.get_attribute("textContent") or "").strip()
                    if self._save_prompt_title_matches(title) and self._element_visible_js(
                        driver, title_el
                    ):
                        return title_el
            except Exception:
                continue
        return None

    def _find_save_application_confirm_dialog(self, driver: Any):
        """
        The ``Save this application?`` layer (``role="alertdialog"``) above the Easy Apply sheet.

        Markup (Aug 2026):
        ``div[role="alertdialog"][data-test-modal].artdeco-modal--layer-confirmation``
        with ``h2[data-test-dialog-title]`` → "Save this application?"
        """
        self._ensure_top_document(driver)
        shadow_hit = self._find_save_prompt_in_shadow_hosts(driver)
        if shadow_hit is not None:
            return shadow_hit
        try:
            for btn in driver.find_elements(
                By.CSS_SELECTOR, 'button[data-control-name="save_application_btn"]'
            ):
                try:
                    if not self._element_visible_js(driver, btn):
                        continue
                    return btn.find_element(
                        By.XPATH,
                        './ancestor::*[@role="alertdialog"][1]',
                    )
                except Exception:
                    try:
                        if self._element_visible_js(driver, btn):
                            return btn
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            for title_el in driver.find_elements(By.CSS_SELECTOR, "h2[data-test-dialog-title]"):
                try:
                    title = (title_el.text or title_el.get_attribute("textContent") or "").strip()
                    if not self._save_prompt_title_matches(title):
                        continue
                    if not self._element_visible_js(driver, title_el):
                        continue
                    return title_el.find_element(
                        By.XPATH,
                        './ancestor::*[@role="alertdialog"][1]',
                    )
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for dlg in driver.find_elements(By.CSS_SELECTOR, SAVE_APPLICATION_PROMPT_ROOT_CSS):
                try:
                    if dlg.find_elements(
                        By.CSS_SELECTOR, 'button[data-control-name="save_application_btn"]'
                    ):
                        return dlg
                    title = (dlg.text or dlg.get_attribute("textContent") or "").lower()
                    if SAVE_APPLICATION_PROMPT_TITLE in title:
                        return dlg
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _save_confirm_present_js(self, driver: Any) -> bool:
        """Detect the save-vs-discard confirm by title / alertdialog (not the job-page Save control)."""
        self._ensure_top_document(driver)
        if self._save_application_btn_visible_js(driver):
            return True
        try:
            return bool(
                driver.execute_script(
                    _SAVE_PROMPT_DEEP_QUERY_JS
                    + """
                    const titleNeedle = arguments[0];
                    const isVisible = (el) => {
                      if (!el) return false;
                      const r = el.getBoundingClientRect();
                      if (r.width < 2 || r.height < 2) return false;
                      const st = window.getComputedStyle(el);
                      return st.visibility !== 'hidden'
                        && st.display !== 'none'
                        && Number(st.opacity || '1') > 0.05;
                    };
                    for (const btn of queryAllDeep(
                      'button[data-control-name="save_application_btn"]'
                    )) {
                      if (isVisible(btn)) return true;
                    }
                    for (const h2 of queryAllDeep('h2[data-test-dialog-title]')) {
                      const t = (h2.textContent || h2.innerText || '').trim().toLowerCase();
                      if (!t.includes(titleNeedle)) continue;
                      if (isVisible(h2)) return true;
                      const dlg = h2.closest('[role="alertdialog"]');
                      if (dlg && isVisible(dlg)) return true;
                    }
                    for (const dlg of queryAllDeep(
                      '[role="alertdialog"].artdeco-modal--layer-confirmation, [role="alertdialog"][data-test-modal]'
                    )) {
                      const t = (dlg.textContent || dlg.innerText || '').toLowerCase();
                      if (t.includes(titleNeedle) && isVisible(dlg)) return true;
                    }
                    return false;
                    """,
                    SAVE_APPLICATION_PROMPT_TITLE,
                )
            )
        except Exception:
            return False

    def _draft_save_confirm_open(self, driver: Any) -> bool:
        """True when LinkedIn's save-vs-discard confirm is on screen after closing Easy Apply."""
        self._ensure_top_document(driver)
        if self._save_application_btn_visible_js(driver):
            return True
        if self._find_save_application_confirm_dialog(driver) is not None:
            return True
        if self._save_confirm_dialog_roots(driver):
            return True
        if self._save_confirm_present_js(driver):
            return True
        return False

    def _log_save_prompt_probe(self, driver: Any, context: str = "") -> None:
        """One-line diagnostic when dismiss logic runs but save prompt handling is unclear."""
        self._ensure_top_document(driver)
        try:
            btn_count = len(
                driver.find_elements(
                    By.CSS_SELECTOR, 'button[data-control-name="save_application_btn"]'
                )
            )
            title_count = len(driver.find_elements(By.CSS_SELECTOR, "h2[data-test-dialog-title]"))
            alert_count = len(driver.find_elements(By.CSS_SELECTOR, SAVE_APPLICATION_PROMPT_ROOT_CSS))
            deep_btn_count = driver.execute_script(
                _SAVE_PROMPT_DEEP_QUERY_JS
                + "return queryAllDeep('button[data-control-name=\"save_application_btn\"]').length;"
            )
            deep_title_count = driver.execute_script(
                _SAVE_PROMPT_DEEP_QUERY_JS
                + "return queryAllDeep('h2[data-test-dialog-title]').length;"
            )
        except Exception:
            btn_count = title_count = alert_count = -1
            deep_btn_count = deep_title_count = -1
        log.info(
            "Save-prompt probe%s: open=%s save_btn_js=%s light(save_btns=%d titles=%d alertdialogs=%d) "
            "deep(save_btns=%s titles=%s)",
            f" ({context})" if context else "",
            self._draft_save_confirm_open(driver),
            self._save_application_btn_visible_js(driver),
            btn_count,
            title_count,
            alert_count,
            deep_btn_count,
            deep_title_count,
        )

    def _wait_for_save_application_prompt(self, driver: Any, timeout_s: float = 2.5) -> bool:
        """Poll briefly after Easy Apply X — the confirm mounts a moment later."""
        deadline = time.monotonic() + max(0.25, float(timeout_s))
        while time.monotonic() < deadline:
            if self._draft_save_confirm_open(driver):
                return True
            if not interruptible_sleep(0.15, driver):
                return False
        return self._draft_save_confirm_open(driver)

    def _click_save_confirm_js(self, driver: Any, *, discard: bool) -> bool:
        """Click Save/Discard only inside the save-application ``alertdialog`` prompt."""
        self._ensure_top_document(driver)
        try:
            return bool(
                driver.execute_script(
                    _SAVE_PROMPT_DEEP_QUERY_JS
                    + """
                    const discard = arguments[0];
                    const titleNeedle = arguments[1];
                    const isVisible = (el) => {
                      if (!el) return false;
                      const r = el.getBoundingClientRect();
                      if (r.width < 2 || r.height < 2) return false;
                      const st = window.getComputedStyle(el);
                      return st.visibility !== 'hidden'
                        && st.display !== 'none'
                        && Number(st.opacity || '1') > 0.05;
                    };
                    const roots = [];
                    for (const h2 of queryAllDeep('h2[data-test-dialog-title]')) {
                      const t = (h2.textContent || h2.innerText || '').trim().toLowerCase();
                      if (!t.includes(titleNeedle)) continue;
                      const dlg = h2.closest('[role="alertdialog"]') || h2.parentElement;
                      if (dlg) roots.push(dlg);
                    }
                    for (const dlg of queryAllDeep(
                      '[role="alertdialog"].artdeco-modal--layer-confirmation, [role="alertdialog"][data-test-modal]'
                    )) {
                      const t = (dlg.textContent || dlg.innerText || '').toLowerCase();
                      if (t.includes(titleNeedle)
                          || dlg.querySelector('button[data-control-name="save_application_btn"]')) {
                        roots.push(dlg);
                      }
                    }
                    const unique = [...new Set(roots)];
                    const selectors = discard
                      ? [
                          'button[data-control-name="discard_application_confirm_btn"]',
                          'button[data-test-dialog-secondary-btn]',
                          '.artdeco-modal__actionbar--confirm-dialog button.artdeco-button--secondary',
                        ]
                      : [
                          'button[data-control-name="save_application_btn"]',
                          'button[data-test-dialog-primary-btn]',
                          '.artdeco-modal__actionbar--confirm-dialog button.artdeco-button--primary',
                        ];
                    for (const root of unique) {
                      for (const sel of selectors) {
                        for (const btn of root.querySelectorAll(sel)) {
                          if (!isVisible(btn)) continue;
                          btn.click();
                          return true;
                        }
                      }
                      for (const btn of root.querySelectorAll('button')) {
                        const t = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                        if (t !== (discard ? 'discard' : 'save')) continue;
                        if (btn.classList.contains('artdeco-modal__dismiss')) continue;
                        if (!isVisible(btn)) continue;
                        btn.click();
                        return true;
                      }
                    }
                    for (const sel of selectors) {
                      for (const btn of queryAllDeep(sel)) {
                        if (!isVisible(btn)) continue;
                        btn.click();
                        return true;
                      }
                    }
                    return false;
                    """,
                    discard,
                    SAVE_APPLICATION_PROMPT_TITLE,
                )
            )
        except Exception:
            return False

    def _resolve_save_application_confirm(
        self, driver: Any, *, discard: bool, timeout_s: float = 8.0
    ) -> bool:
        """Poll until the save/discard confirm appears, then click the right action."""
        self._ensure_top_document(driver)
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        while time.monotonic() < deadline:
            if self._stop_dismiss_if_driver_closed(driver):
                return False
            if not self._draft_save_confirm_open(driver):
                if not interruptible_sleep(0.25, driver):
                    return False
                continue
            if self._click_draft_confirm_button(driver, discard=discard):
                if not discard:
                    self._saved_apply_draft_this_flow = True
                return True
            if self._click_save_confirm_js(driver, discard=discard):
                want = "Discard" if discard else "Save"
                log.info("Clicked %s on save-application confirm (JS)", want)
                self._after_ui_click()
                time.sleep(0.35)
                if not discard:
                    self._saved_apply_draft_this_flow = True
                return True
            if not interruptible_sleep(0.25, driver):
                return False
        return False

    def _save_application_prompt_if_open(self, driver: Any, *, discard: bool = False) -> bool:
        """
        If the save/discard confirm (``Save this application?``) is visible, click Save or Discard.
        Always call this **before** clicking an Easy Apply sheet X — never click X while this prompt is up.

        Returns True whenever the prompt is detected (even if the click fails) so callers skip X.
        """
        if not self._draft_save_confirm_open(driver):
            return False
        want = "Discard" if discard else "Save"
        log.info("Save-application prompt open — clicking %s (skipping dismiss X)", want)
        if self._resolve_save_application_confirm(driver, discard=discard, timeout_s=4.0):
            self._wait_for_apply_overlays_closed(driver, timeout_s=4.0)
        else:
            log.warning(
                "Save-application prompt visible but %s could not be clicked — will not click Easy Apply X",
                want,
            )
        return True

    def _click_easy_apply_sheet_dismiss(self, driver: Any, *, discard_draft: bool = False) -> bool:
        """
        Close the Easy Apply sheet via its header X.

        Checks for the save/discard confirm **before** clicking X; if the prompt is already up, clicks
        Save (or Discard) instead.
        """
        self._ensure_top_document(driver)
        if self._save_application_prompt_if_open(driver, discard=discard_draft):
            return True
        modal = self._find_easy_apply_modal(driver)
        if modal is None:
            return False
        clicked = False
        for sel in (
            'button[data-test-modal-close-btn]',
            "button.artdeco-modal__dismiss",
            'button[aria-label="Dismiss"]',
            'button[aria-label="dismiss"]',
        ):
            try:
                for btn in modal.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if not btn.is_displayed() or not btn.is_enabled():
                            continue
                        if self._skip_dismiss_click_for_save_prompt(
                            driver, btn, discard=discard_draft
                        ):
                            return True
                        if self.highlight:
                            focus_element(driver, btn, pause=0.2)
                        log.info(
                            "Clicking Easy Apply sheet dismiss (save prompt open=%s)",
                            self._draft_save_confirm_open(driver),
                        )
                        self._click_element(driver, btn)
                        self._after_ui_click()
                        clicked = True
                        break
                    except Exception:
                        continue
                if clicked:
                    break
            except Exception:
                continue
        if clicked:
            self._wait_for_save_application_prompt(driver, timeout_s=3.0)
            self._save_application_prompt_if_open(driver, discard=discard_draft)
        return clicked

    def _wait_for_apply_overlays_closed(self, driver: Any, timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        while time.monotonic() < deadline:
            if self._stop_dismiss_if_driver_closed(driver):
                return False
            self._ensure_top_document(driver)
            if self._draft_save_confirm_open(driver):
                self._save_application_prompt_if_open(driver)
            if not self._easy_apply_modal_is_open(driver) and not self._draft_save_confirm_open(
                driver
            ):
                return True
            if not interruptible_sleep(0.25, driver):
                return False
        return not self._easy_apply_modal_is_open(driver) and not self._draft_save_confirm_open(
            driver
        )

    def _soft_close_overlays_after_draft_save(self, driver: Any, timeout_s: float = 5.0) -> None:
        """
        After **Save** on the draft confirm, wait for overlays to unmount.

        Do **not** click Easy Apply Dismiss/X — LinkedIn often leaves the sheet in the DOM
        briefly, and a second X can discard the draft we just saved. Escape is a one-shot
        fallback that typically closes leftover chrome without a new save/discard prompt.
        """
        if self._stop_dismiss_if_driver_closed(driver):
            return
        # Give the Save request a beat before any further UI action / next-job click.
        if not interruptible_sleep(0.8, driver):
            return
        if self._wait_for_apply_overlays_closed(driver, timeout_s=timeout_s):
            return
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            log.info("Sent Escape after Easy Apply draft save (leftover overlay still present)")
        except Exception:
            pass
        interruptible_sleep(0.5, driver)
        self._wait_for_apply_overlays_closed(driver, timeout_s=2.0)

    def _draft_save_confirm_dialogs(self, driver: Any) -> list:
        """Confirm layers after closing an in-progress Easy Apply (save vs discard)."""
        out: list = []
        primary = self._find_save_application_confirm_dialog(driver)
        if primary is not None:
            out.append(primary)
        seen: set[str] = {getattr(primary, "id", "") or ""}
        for sel in DRAFT_CONFIRM_DIALOG_SELECTORS:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                continue
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    role = (el.get_attribute("role") or "").lower()
                    if role == "dialog" and "jobs-easy-apply-modal" in (
                        el.get_attribute("class") or ""
                    ):
                        continue
                    key = el.id or str(id(el))
                    if key in seen:
                        continue
                    seen.add(key)
                    blob = (el.text or "").lower()
                    if role == "alertdialog" or "save this application" in blob:
                        out.append(el)
                except Exception:
                    continue
        return out

    @staticmethod
    def _button_visible_text_is(btn: Any, want: str) -> bool:
        raw = (btn.text or "").strip().lower()
        if raw == want:
            return True
        try:
            span = btn.find_element(By.CSS_SELECTOR, ".artdeco-button__text")
            return (span.text or "").strip().lower() == want
        except Exception:
            return False

    @staticmethod
    def _button_in_save_confirm_dialog(btn: Any) -> bool:
        """True when ``btn`` lives inside the save-vs-discard ``alertdialog`` prompt."""
        try:
            btn.find_element(
                By.XPATH,
                './ancestor::*[@role="alertdialog"][1]',
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _button_in_easy_apply_modal(btn: Any) -> bool:
        """True when ``btn`` is the Easy Apply sheet header X (not the save confirm layer)."""
        try:
            btn.find_element(
                By.XPATH,
                './ancestor::*[contains(@class,"jobs-easy-apply-modal")][1]',
            )
            return True
        except Exception:
            return False

    def _skip_dismiss_click_for_save_prompt(self, driver: Any, btn: Any, *, discard: bool = False) -> bool:
        """
        Return True when ``btn`` must not be clicked because the save/discard confirm is up.
        Handles Save/Discard and blocks Easy Apply X spam.
        """
        if self._button_in_save_confirm_dialog(btn):
            return True
        if self._button_in_easy_apply_modal(btn) and self._draft_save_confirm_open(driver):
            self._save_application_prompt_if_open(driver, discard=discard)
            return True
        if self._draft_save_confirm_open(driver):
            self._save_application_prompt_if_open(driver, discard=discard)
            return True
        return False

    @staticmethod
    def _save_confirm_dialog_roots(driver: Any) -> list:
        """Return save/discard confirm ``alertdialog`` elements (never the job-page Save control)."""
        roots: list = []
        seen: set[str] = set()

        def _add(dlg) -> None:
            try:
                key = dlg.id or str(id(dlg))
                if key in seen:
                    return
                seen.add(key)
                roots.append(dlg)
            except Exception:
                pass

        try:
            for btn in driver.find_elements(
                By.CSS_SELECTOR, 'button[data-control-name="save_application_btn"]'
            ):
                try:
                    dlg = btn.find_element(
                        By.XPATH,
                        './ancestor::*[@role="alertdialog"][1]',
                    )
                    _add(dlg)
                except Exception:
                    _add(btn)
        except Exception:
            pass
        try:
            for title_el in driver.find_elements(By.CSS_SELECTOR, "h2[data-test-dialog-title]"):
                try:
                    title = (title_el.text or title_el.get_attribute("textContent") or "").strip()
                    if SAVE_APPLICATION_PROMPT_TITLE not in title.lower():
                        continue
                    dlg = title_el.find_element(
                        By.XPATH,
                        './ancestor::*[@role="alertdialog"][1]',
                    )
                    _add(dlg)
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for dlg in driver.find_elements(By.CSS_SELECTOR, SAVE_APPLICATION_PROMPT_ROOT_CSS):
                try:
                    has_save_btn = bool(
                        dlg.find_elements(
                            By.CSS_SELECTOR,
                            'button[data-control-name="save_application_btn"]',
                        )
                    )
                    title = (dlg.text or dlg.get_attribute("textContent") or "").lower()
                    if SAVE_APPLICATION_PROMPT_TITLE not in title and not has_save_btn:
                        continue
                    _add(dlg)
                except Exception:
                    continue
        except Exception:
            pass
        return roots

    @staticmethod
    def _click_element(driver: Any, btn: Any) -> None:
        try:
            scroll_into_view(driver, btn)
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)

    def _click_draft_confirm_button(self, driver: Any, *, discard: bool) -> bool:
        """
        Click **Save** or **Discard** inside the save-application ``alertdialog`` only.
        Returns True when a control was clicked.
        """
        want = "discard" if discard else "save"
        scopes = self._save_confirm_dialog_roots(driver)
        if not scopes:
            return False

        selectors = DRAFT_DISCARD_SELECTORS if discard else DRAFT_SAVE_SELECTORS
        for scope in scopes:
            search_roots: list[Any] = []
            try:
                bars = scope.find_elements(
                    By.CSS_SELECTOR, ".artdeco-modal__actionbar--confirm-dialog"
                )
                search_roots.extend(bars)
            except Exception:
                pass
            search_roots.append(scope)
            for root in search_roots:
                for sel in selectors:
                    try:
                        buttons = root.find_elements(By.CSS_SELECTOR, sel)
                    except Exception:
                        continue
                    for btn in buttons:
                        try:
                            if not self._button_in_save_confirm_dialog(btn):
                                continue
                            if self.highlight:
                                focus_element(driver, btn, pause=0.15)
                            self._click_element(driver, btn)
                            self._after_ui_click()
                            log.info("Clicked %s on save-application confirm", want.capitalize())
                            time.sleep(0.35)
                            if not discard:
                                self._saved_apply_draft_this_flow = True
                            return True
                        except Exception:
                            continue
                try:
                    for btn in root.find_elements(By.TAG_NAME, "button"):
                        try:
                            if not self._button_in_save_confirm_dialog(btn):
                                continue
                            if not self._button_visible_text_is(btn, want):
                                continue
                            cls = (btn.get_attribute("class") or "").lower()
                            if "artdeco-modal__dismiss" in cls:
                                continue
                            if self.highlight:
                                focus_element(driver, btn, pause=0.15)
                            self._click_element(driver, btn)
                            self._after_ui_click()
                            log.info(
                                "Clicked %s (visible text) on save-application confirm",
                                want.capitalize(),
                            )
                            time.sleep(0.35)
                            if not discard:
                                self._saved_apply_draft_this_flow = True
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
        return False

    def _click_dismiss_followup(self, driver: Any, *, discard: bool) -> bool:
        """
        After **Dismiss**, LinkedIn opens a save-vs-discard confirm (``role="alertdialog"``).
        Returns True when Save/Discard was clicked.
        """
        if self._stop_dismiss_if_driver_closed(driver):
            return False
        return self._resolve_save_application_confirm(driver, discard=discard, timeout_s=8.0)

    def _click_save_on_dismiss_followup(self, driver: Any) -> bool:
        """After **Dismiss**, choose **Save** on the draft follow-up dialog."""
        return self._click_dismiss_followup(driver, discard=False)

    def _click_dismiss_header(self, driver: Any, *, discard_draft: bool = False) -> bool:
        """Close the Easy Apply sheet (checks save prompt before any X click)."""
        if self._stop_dismiss_if_driver_closed(driver):
            return False
        return self._click_easy_apply_sheet_dismiss(driver, discard_draft=discard_draft)

    def _abandon_apply_and_dismiss(self, driver: Any, job: dict, reason: str) -> bool:
        """
        Leave the application without submitting: dismiss sheet, save draft, continue scanning.
        Always returns False (apply did not complete).
        """
        if self._stop_dismiss_if_driver_closed(driver):
            return False
        log.warning("Abandoning Easy Apply for %s — %s", job.get("id"), reason)
        self._saved_apply_draft_this_flow = False
        self._log_save_prompt_probe(driver, "before abandon dismiss")
        self._click_easy_apply_sheet_dismiss(driver, discard_draft=False)
        self._log_save_prompt_probe(driver, "after abandon dismiss click")
        if self._saved_apply_draft_this_flow:
            log.info(
                "Easy Apply draft saved for %s — waiting for overlays without clicking Dismiss/X",
                job.get("id"),
            )
            self._soft_close_overlays_after_draft_save(driver)
            return False
        self._wait_for_apply_overlays_closed(driver, timeout_s=6.0)
        self._dismiss_easy_apply_modal_if_open(driver, "after abandon dismiss")
        return False

    def _abandon_apply_and_discard(self, driver: Any, job: dict, reason: str) -> bool:
        """
        Close and discard the in-progress application (Dismiss, then Discard on the draft dialog).
        Always returns False (apply did not complete).
        """
        if self._stop_dismiss_if_driver_closed(driver):
            return False
        log.warning("Discarding Easy Apply for %s — %s", job.get("id"), reason)
        self._click_easy_apply_sheet_dismiss(driver, discard_draft=True)
        self._wait_for_apply_overlays_closed(driver, timeout_s=6.0)
        self._dismiss_easy_apply_modal_if_open(driver, "after discard dismiss")
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

            modal = self._find_easy_apply_modal(driver)
            if modal is None and step == 0:
                modal = self._wait_for_easy_apply_modal(driver, timeout_s=8.0)
            if modal is None:
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

            errors = []
            try:
                errors = [
                    e
                    for e in modal.find_elements(By.CSS_SELECTOR, SEL["error_msg"])
                    if e.is_displayed()
                ]
            except Exception:
                errors = driver.find_elements(By.CSS_SELECTOR, SEL["error_msg"])
            if errors:
                error_text = errors[0].text
                # Try to find the label of the failing field for easier debugging.
                field_label = ""
                try:
                    container = errors[0].find_element(
                        By.XPATH,
                        "ancestor::*[self::div or self::fieldset][.//label][1]",
                    )
                    lbl = container.find_element(By.CSS_SELECTOR, "label")
                    field_label = (lbl.text or "").strip().splitlines()[0][:120]
                except Exception:
                    pass
                if field_label:
                    log.warning(
                        "Validation error at step %d (field: %r): %s",
                        step, field_label, error_text,
                    )
                else:
                    log.warning("Validation error at step %d: %s", step, error_text)
                shot_path = self.screenshot_dir / f"validation_{job['id']}_step{step}.png"
                try:
                    scroll_into_view(driver, errors[0])
                    time.sleep(0.15)
                    driver.save_screenshot(str(shot_path))
                    log.info("Validation screenshot saved: %s", shot_path)
                except Exception as e:
                    log.warning("Could not save validation screenshot %s: %s", shot_path, e)
                self._abandon_apply_and_dismiss(driver, job, f"validation error: {error_text}")
                return False

            submit_btns = self._visible_controls_in(modal, driver, SEL["submit_btn"])
            review_btns = self._visible_controls_in(modal, driver, SEL["review_btn"])
            next_btns = self._visible_controls_in(modal, driver, SEL["next_btn"])

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
    def _visible_controls_in(root: Any, driver: Any, css: str) -> list:
        """Prefer controls inside the apply sheet; fall back to the top document."""
        found: list = []
        for scope in (root, driver):
            if scope is None:
                continue
            try:
                els = scope.find_elements(By.CSS_SELECTOR, css)
            except Exception:
                continue
            for el in els:
                try:
                    if el.is_displayed() and el.is_enabled():
                        found.append(el)
                except Exception:
                    continue
            if found:
                return found
        return found

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
        """
        Click a radio option. ``focus_element``'s scroll can trigger a re-render that detaches
        ``el`` — if that happens mid-click, do not retry with this same now-stale handle (the
        JS fallback would raise the identical ``StaleElementReferenceException``, uncaught);
        the click likely already landed, so just let the caller's selection-state check decide.
        """
        if self.highlight:
            focus_element(driver, el, pause=0.2)
        try:
            el.click()
        except StaleElementReferenceException:
            return
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except StaleElementReferenceException:
                return

    @staticmethod
    def _dedupe_repeated_label_text(text: str) -> str:
        """
        Collapse LinkedIn's aria-hidden + visually-hidden duplicate-text accessibility pattern.

        Newer ``fb-dash-form-element`` labels/legends render the question text twice — once in an
        ``aria-hidden="true"`` span (visible copy) and once in a ``visually-hidden`` span (screen-reader
        copy, taken out of flow via ``position: absolute``). Because that second span is out-of-flow,
        Selenium's ``.text`` (which mirrors Chrome's rendered-text serialization) inserts a line break
        around it, yielding ``"Question?\\nQuestion?"`` instead of one copy. Collapse that back down so
        label matching (especially ``exact`` rules) and logs see the question once.
        """
        t = (text or "").strip()
        if not t:
            return t
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if len(lines) >= 2 and len(set(lines)) == 1:
            return lines[0]
        return t

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
                    t = self._dedupe_repeated_label_text(els[0].text or "")
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

    def _resolve_and_click_choice(
        self,
        driver: Any,
        job: dict,
        label: str,
        candidates: list[str],
        click_fn,
        *,
        required: bool,
        assist: bool,
        kind: str,
    ) -> tuple[bool, str | None]:
        """
        Try ``candidates`` (priority order — see :meth:`FormFillRulesEngine.screening_yes_no_candidates`)
        against ``click_fn(candidate) -> bool`` in order, stopping at the first that succeeds (the field
        actually offers that option). ``DISCARD_APPLY`` anywhere in the list short-circuits to discarding
        the apply immediately (it is not a clickable option).

        Returns ``(ok, matched_candidate)``. ``ok`` is ``False`` only when the caller must abandon this
        apply (``return False`` up the stack) — either a discard rule fired, or every candidate was tried
        and none matched an available option on a required field. ``matched_candidate`` is ``None`` when
        nothing was selected (no rule, or a non-required field left as-is).
        """
        if DISCARD_APPLY in candidates:
            if self._handle_screening_discard(driver, job, DISCARD_APPLY, assist=assist):
                return False, None
            candidates = [c for c in candidates if c != DISCARD_APPLY]
        if not candidates:
            if required and not assist:
                log.warning(
                    "No rule for required %s (job %s) label=%r — abandoning",
                    kind,
                    job.get("id"),
                    label,
                )
                return False, None
            if required and assist:
                log.debug("Assist: leaving required %s unanswered (no rule) label=%r", kind, label)
            return True, None
        for i, cand in enumerate(candidates):
            if click_fn(cand):
                self._after_field_fill()
                if i == 0:
                    log.info("Selected %r for %s: %s", cand, kind, (label or "")[:120])
                else:
                    log.info(
                        "Selected fallback %r (priority %d/%d) for %s: %s",
                        cand,
                        i + 1,
                        len(candidates),
                        kind,
                        (label or "")[:120],
                    )
                return True, cand
        if required and not assist:
            log.warning(
                "Could not select any of %r for required %s (job %s) label=%r — abandoning",
                candidates,
                kind,
                job.get("id"),
                label,
            )
            return False, None
        return True, None

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
                candidates = self._rules.screening_yes_no_candidates(label)
                ok, _matched = self._resolve_and_click_choice(
                    driver,
                    job,
                    label,
                    candidates,
                    lambda cand: self._click_choice_in_radio_container(driver, fs, cand),
                    required=required,
                    assist=assist,
                    kind="LinkedIn radio question",
                )
                if not ok:
                    return False, processed_names
            except Exception as e:
                log.debug("Skipping LinkedIn radio fieldset: %s", e)
        return True, processed_names

    def _click_checkbox_target(self, driver: Any, el: Any) -> None:
        """Same stale-handle hardening as ``_click_radio_target`` — see its docstring."""
        if self.highlight:
            focus_element(driver, el, pause=0.2)
        try:
            el.click()
        except StaleElementReferenceException:
            return
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
            except StaleElementReferenceException:
                return

    def _legend_for_linkedin_checkbox_fieldset(self, fieldset: Any) -> str:
        """Question text for a LinkedIn checkbox fieldset (single toggle or multi-option group)."""
        for sel in (
            "[data-test-form-builder-checkbox-form-component__title]",
            "[data-test-checkbox-form-component__title]",
            "legend .fb-dash-form-element__label",
            "legend",
        ):
            try:
                els = fieldset.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = self._dedupe_repeated_label_text(els[0].text or "")
                    if t:
                        return t
            except Exception:
                continue
        return ""

    @staticmethod
    def _linkedin_checkbox_fieldset_is_required(fieldset: Any) -> bool:
        try:
            for el in fieldset.find_elements(
                By.CSS_SELECTOR,
                "legend, legend .fb-dash-form-element__label, "
                "[data-test-form-builder-checkbox-form-component__required], "
                "[data-test-checkbox-form-component__required]",
            ):
                cls = (el.get_attribute("class") or "").lower()
                if "is-required" in cls or "required" in cls:
                    return True
        except Exception:
            pass
        for inp in fieldset.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
            try:
                if (inp.get_attribute("aria-required") or "").lower() == "true":
                    return True
                if inp.get_attribute("required") is not None:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _checkbox_option_label(container: Any, cb: Any) -> str:
        """Visible label text for one checkbox option (LinkedIn's selectable-option wrapper, or <label for>)."""
        try:
            lab_list = cb.find_elements(
                By.XPATH,
                "./ancestor::*[self::div or self::li][1]//label[@data-test-text-selectable-option__label]",
            )
            if lab_list:
                t = (lab_list[0].text or "").strip()
                if t:
                    return t
        except Exception:
            pass
        try:
            cid = cb.get_attribute("id") or ""
            if cid:
                for lab in container.find_elements(By.CSS_SELECTOR, f'label[for="{cid}"]'):
                    t = (lab.text or "").strip()
                    if t:
                        return t
        except Exception:
            pass
        return (cb.get_attribute("value") or "").strip()

    def _click_checkbox_option_by_label(self, driver: Any, container: Any, choose_label: str) -> bool:
        """Check the checkbox option in ``container`` whose visible label matches ``choose_label``."""
        target = (choose_label or "").strip()
        if not target:
            return False
        for cb in container.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
            try:
                label_text = self._checkbox_option_label(container, cb)
                if not label_text or not self._choice_labels_equivalent(target, label_text):
                    continue
                if not cb.is_selected():
                    click_el = cb
                    cid = cb.get_attribute("id") or ""
                    if cid:
                        labs = container.find_elements(By.CSS_SELECTOR, f'label[for="{cid}"]')
                        if labs:
                            click_el = labs[0]
                    self._click_checkbox_target(driver, click_el)
                return True
            except Exception:
                continue
        return False

    def _fill_linkedin_checkbox_fieldsets(
        self,
        driver: Any,
        job: dict,
        root: Any,
        *,
        assist: bool,
    ) -> tuple[bool, set[str]]:
        """
        LinkedIn Easy Apply checkbox questions.

        A fieldset with exactly **one** checkbox is a yes/no toggle for the question in its legend —
        uses ``screening_yes_no`` rules (check it for "Yes", leave/uncheck for "No"), same rules already
        written for radio Yes/No questions (e.g. sponsorship). A fieldset with **multiple** checkboxes is
        a multi-option group (e.g. "How did you hear about us") — uses ``checkbox_groups`` rules, the same
        category already used for Greenhouse ``fieldset.checkbox``.

        Matches known LinkedIn form-builder attributes first, then falls back to any ``fieldset``
        containing checkbox inputs (attribute names are not confirmed against a live example — this
        fallback keeps the feature working even if the specific attribute guess above is wrong).

        Returns ``(ok, processed input names)`` — mirrors ``_fill_linkedin_form_builder_radio_fieldsets``.
        """
        processed_names: set[str] = set()
        seen_ids: set[str] = set()
        fieldsets = []
        candidates = list(root.find_elements(By.CSS_SELECTOR, SEL["linkedin_checkbox_fieldset"]))
        candidates += [
            fs
            for fs in root.find_elements(By.CSS_SELECTOR, "fieldset")
            if fs.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        ]
        for fs in candidates:
            key = fs.id
            if key in seen_ids:
                continue
            seen_ids.add(key)
            fieldsets.append(fs)

        for fs in fieldsets:
            try:
                checkboxes = fs.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                if not checkboxes:
                    continue
                for inp in checkboxes:
                    n = (inp.get_attribute("name") or "").strip()
                    if n:
                        processed_names.add(n)

                label = self._legend_for_linkedin_checkbox_fieldset(fs)
                self._maybe_pause_for_user_on_first_empty_field(label)
                checkboxes = fs.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                if not checkboxes:
                    continue
                required = self._linkedin_checkbox_fieldset_is_required(fs)

                if len(checkboxes) == 1:
                    cb = checkboxes[0]
                    ans = self._rules.screening_yes_no(label)
                    if self._handle_screening_discard(driver, job, ans, assist=assist):
                        return False, processed_names
                    if ans is None:
                        if required and not assist:
                            log.warning(
                                "No rule for required LinkedIn checkbox question (job %s) label=%r — abandoning",
                                job.get("id"),
                                label,
                            )
                            return False, processed_names
                        if required and assist:
                            log.debug(
                                "Assist: leaving required LinkedIn checkbox unanswered (no rule) label=%r",
                                label,
                            )
                        continue
                    want_checked = ans.strip().lower() == "yes"
                    if cb.is_selected() != want_checked:
                        self._click_checkbox_target(driver, cb)
                    self._after_field_fill()
                    log.info(
                        "Set checkbox %s for LinkedIn checkbox question: %s",
                        "checked" if want_checked else "unchecked",
                        (label or "")[:120],
                    )
                else:
                    if any(cb.is_selected() for cb in checkboxes):
                        continue
                    candidates = self._rules.checkbox_group_choice_candidates(label)
                    ok, _matched = self._resolve_and_click_choice(
                        driver,
                        job,
                        label,
                        candidates,
                        lambda cand: self._click_checkbox_option_by_label(driver, fs, cand),
                        required=required,
                        assist=assist,
                        kind="LinkedIn checkbox group",
                    )
                    if not ok:
                        return False, processed_names
            except Exception as e:
                log.debug("Skipping LinkedIn checkbox fieldset: %s", e)
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

    def _record_cover_letter_write(self, docx_path: Path) -> None:
        """Tell ``self.cover_letter_tracker`` (if set) about a cover letter this filler just wrote,
        so it gets pushed to S3 as a logged add entry next time the caller flushes the tracker."""
        if self.cover_letter_tracker is None:
            return
        try:
            rel = docx_path.resolve().relative_to(COVERLETTERS_DIR.resolve()).as_posix()
        except ValueError:
            return
        self.cover_letter_tracker.record_write(rel)

    def _save_text_cover_letter(self, cover_letter: str, job: dict) -> None:
        """Write cover letter to disk when it was submitted as typed text rather than a file upload."""
        try:
            docx_path = cover_letter_docx_path_unique(
                self.cover_letter_docx_dir,
                site="linkedin",
                company=str(job.get("company") or ""),
                title=str(job.get("title") or ""),
                job_id=str(job.get("id") or "job"),
            )
            write_cover_letter_docx(cover_letter, docx_path)
            log.info("Saved text-field cover letter as DOCX: %s", docx_path)
            self._record_cover_letter_write(docx_path)
        except Exception as e:
            log.warning("Could not save text-field cover letter: %s", e)

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
                    self._save_text_cover_letter(cover_letter, job)
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
                                self._wait_for_typeahead_dropdown(driver)
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
                        self._save_text_cover_letter(text, job)
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
                candidates = self._rules.answer_select_candidates(label)
                dd = Select(sel_el)
                ok, _matched = self._resolve_and_click_choice(
                    driver,
                    job,
                    label,
                    candidates,
                    lambda cand: self._apply_select_choice(dd, opt_els, cand),
                    required=True,
                    assist=assist,
                    kind="dropdown",
                )
                if not ok:
                    return False
            except Exception as e:
                log.debug("Skipping select: %s", e)

        linkedin_radio_ok, linkedin_radio_names = self._fill_linkedin_form_builder_radio_fieldsets(
            driver, job, root, assist=assist
        )
        if not linkedin_radio_ok:
            return False

        linkedin_checkbox_ok, _linkedin_checkbox_names = self._fill_linkedin_checkbox_fieldsets(
            driver, job, root, assist=assist
        )
        if not linkedin_checkbox_ok:
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
                ans_candidates = self._rules.screening_yes_no_candidates(label)
                if DISCARD_APPLY in ans_candidates:
                    if self._handle_screening_discard(driver, job, DISCARD_APPLY, assist=assist):
                        return False
                    ans_candidates = [c for c in ans_candidates if c != DISCARD_APPLY]
                if not ans_candidates:
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

                clicked_ans = None
                for ans in ans_candidates:
                    if ans.strip().lower() in ("yes", "no"):
                        want_yes = ans.strip().lower() == "yes"
                        if self._click_yes_no_in_radio_group(driver, radios, want_yes):
                            clicked_ans = ans
                            break
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
                                    clicked = True
                                    break
                        if clicked:
                            clicked_ans = ans
                            break
                if clicked_ans is not None:
                    self._after_field_fill()
                    if clicked_ans != ans_candidates[0]:
                        log.info(
                            "Radio group %r: selected fallback answer %r (priority %d/%d).",
                            (label or "")[:100],
                            clicked_ans,
                            ans_candidates.index(clicked_ans) + 1,
                            len(ans_candidates),
                        )
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
                    self._record_cover_letter_write(docx_path)
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

    def _apply_select_choice(self, dd: Select, opt_els, preferred_value: str) -> bool:
        """Set dropdown to ``preferred_value`` (matches ``value=`` or visible text). Returns True on success."""
        pv = preferred_value.strip()
        for o in opt_els:
            v = (o.get_attribute("value") or "").strip()
            t = (o.text or "").strip()
            if v.lower() == pv.lower():
                dd.select_by_value(v)
                return True
            if t.lower() == pv.lower():
                dd.select_by_visible_text(t)
                return True
        try:
            dd.select_by_value(pv)
            return True
        except Exception:
            return False

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
                    t = self._dedupe_repeated_label_text(labels[0].text or "")
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
