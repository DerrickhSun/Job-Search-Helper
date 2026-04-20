"""
Easy Apply Form Filler
Uses Selenium + Chrome for LinkedIn Easy Apply flows.

Default is a visible window. Use --headless to hide it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
import re
import time
from pathlib import Path
from typing import Any

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from chrome_driver import DEFAULT_COOKIE_PATH, build_chrome, focus_element, load_cookies
from cover_letter import write_cover_letter_docx

log = logging.getLogger(__name__)


def binary_screening_answer(label: str) -> str | None:
    """
    Common yes/no screening questions (substring match on label; spacing normalized).

    Returns ``\"Yes\"``, ``\"No\"``, or ``None`` if no rule matches.
    """
    l = re.sub(r"\s+", " ", (label or "").lower()).strip()
    if not l:
        return None

    # Visa / employment sponsorship (will require sponsorship for a visa, etc.)
    if ("sponsorship" in l or " sponsor" in l or "sponsor " in l) and any(
        x in l for x in ("visa", "h-1", "h1b", "h1-b", "immigration", "employment visa")
    ):
        return "No"
    if "sponsorship" in l and any(x in l for x in ("require", "requiring", "needed", "need", "will you")):
        return "No"

    # Relatives / family at employer
    if ("relative" in l or "relatives" in l or "family member" in l) and (
        "employed" in l or "work" in l or "working" in l
    ):
        return "No"

    # Previously employed by this company
    if "employed by" in l:
        return "No"

    # Work authorization (US)
    if "authorized" in l and "work" in l:
        return "Yes"

    # Availability — start immediately
    if "immediately" in l and ("start" in l or "begin" in l):
        return "Yes"

    return None


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
    "error_msg": ".artdeco-inline-feedback--error",
}

# Success / follow-up UI after submit is often *not* inside ``.jobs-easy-apply-modal`` — same tab, different layer.
POST_APPLY_DISMISS = (
    'button[aria-label="Not now"]',
    'button[aria-label="Got it"]',
    'button[aria-label="Close"]',
    "button.artdeco-modal__dismiss",
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
        apply_review_pause_after_fill_seconds: float = 10.0,
        cover_letter_docx_dir: Path | str = "output/coverletters",
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

    def apply(self, job: dict, resume: dict, cover_letter: str, driver: Any | None = None) -> bool:
        """
        Clicks Easy Apply and submits the form.

        If ``driver`` is None (default), opens a new Chrome session and navigates to ``job["url"]``.
        If ``driver`` is provided, uses the current page (e.g. job search with the detail panel open)
        and does not close the browser afterward.
        """
        own_driver = driver is None
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

            apply_btn = self._find_apply_button(driver)
            if not apply_btn:
                raise RuntimeError(
                    "Apply button not found: expected #jobs-apply-button-id or Easy Apply fallback"
                )
            if self.highlight:
                focus_element(driver, apply_btn, pause=self.step_delay)
            apply_btn.click()
            self._after_ui_click()
            time.sleep(0.35)

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
        return False

    @staticmethod
    def _button_is_dismiss(btn: Any) -> bool:
        al = (btn.get_attribute("aria-label") or "").lower()
        return "dismiss" in al

    def _click_save_on_dismiss_followup(self, driver: Any) -> None:
        """
        After **Dismiss**, LinkedIn often opens a second dialog: save the application draft or discard.
        Choose **Save** so the flow can finish cleanly.
        """
        time.sleep(0.45)
        for dialog in driver.find_elements(By.CSS_SELECTOR, '[role="dialog"], .artdeco-modal'):
            try:
                if not dialog.is_displayed():
                    continue
            except Exception:
                continue
            blob = (dialog.text or "").lower()
            if blob and "discard" not in blob and "draft" not in blob and "save" not in blob:
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
                    if (btn.text or "").strip().lower() != "save":
                        continue
                    if self.highlight:
                        focus_element(driver, btn, pause=0.15)
                    btn.click()
                    self._after_ui_click()
                    log.info("Clicked Save (visible text) on dismiss follow-up")
                    time.sleep(0.35)
                    return
                except Exception:
                    continue

    def _click_dismiss_header(self, driver: Any) -> bool:
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
                        self._click_save_on_dismiss_followup(driver)
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
        if self._click_dismiss_header(driver):
            self._dismiss_easy_apply_modal_if_open(driver, "after abandon dismiss")
            return False
        log.warning("Dismiss button not found; trying generic modal cleanup")
        self._dismiss_easy_apply_modal_if_open(driver, "abandon fallback")
        return False

    def _fill_form(self, driver, resume: dict, cover_letter: str, job: dict) -> bool:
        max_steps = 10

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
                self._wait_then_dismiss_post_submit(driver, "after submit")
                return True
            if review_btns:
                btn = review_btns[0]
                if self.highlight:
                    focus_element(driver, btn, pause=self.step_delay)
                btn.click()
                self._after_ui_click()
            elif next_btns:
                btn = next_btns[0]
                if self.highlight:
                    focus_element(driver, btn, pause=self.step_delay)
                btn.click()
                self._after_ui_click()
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

    def _label_for_radio_group(self, driver: Any, first_radio) -> str:
        """Best-effort question text for a radio group (fieldset legend or form-element wrapper)."""
        try:
            fs = first_radio.find_element(By.XPATH, "./ancestor::fieldset[1]")
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
        for r in radios:
            val = (r.get_attribute("value") or "").strip().lower()
            if want_yes and val in ("yes", "true", "1", "y", "on"):
                if self.highlight:
                    focus_element(driver, r, pause=0.2)
                r.click()
                return True
            if not want_yes and val in ("no", "false", "0", "n", "off"):
                if self.highlight:
                    focus_element(driver, r, pause=0.2)
                r.click()
                return True
        for r in radios:
            try:
                rid = r.get_attribute("id")
                if not rid:
                    continue
                for lab in driver.find_elements(By.CSS_SELECTOR, f'label[for="{rid}"]'):
                    t = (lab.text or "").strip().lower()
                    if want_yes and t in ("yes", "y"):
                        if self.highlight:
                            focus_element(driver, r, pause=0.2)
                        r.click()
                        return True
                    if not want_yes and t in ("no", "n"):
                        if self.highlight:
                            focus_element(driver, r, pause=0.2)
                        r.click()
                        return True
            except Exception:
                continue
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

    def _fill_step(self, driver: Any, resume: dict, cover_letter: str, job: dict) -> bool:
        """
        Fill the current step. Returns False if we should abandon (unknown required field with no rule);
        the caller will dismiss the modal.
        """
        modal = driver.find_element(By.CSS_SELECTOR, SEL["modal"])

        for input_el in modal.find_elements(By.CSS_SELECTOR, SEL["text_input"]):
            try:
                current = (input_el.get_attribute("value") or "").strip()
                if current:
                    continue
                label = self._get_label(driver, input_el)
                value = self._answer_text_field(label, resume)
                required = self._element_is_required(input_el)
                if value is None:
                    if required:
                        log.warning(
                            "No rule for required text field (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False
                    continue
                if value:
                    if self.highlight:
                        focus_element(driver, input_el, pause=0.2)
                    input_el.clear()
                    input_el.send_keys(value)
                    self._after_field_fill()
            except Exception as e:
                log.debug("Skipping text field: %s", e)

        for ta in modal.find_elements(By.CSS_SELECTOR, SEL["textarea"]):
            try:
                current = (ta.get_attribute("value") or "").strip()
                if current:
                    continue
                label = self._get_label(driver, ta)
                text = self._answer_textarea(label, cover_letter)
                required = self._element_is_required(ta)
                if text is None:
                    if required:
                        log.warning(
                            "No rule for required textarea (job %s) label=%r — abandoning",
                            job.get("id"),
                            label,
                        )
                        return False
                    continue
                if text:
                    if self.highlight:
                        focus_element(driver, ta, pause=0.2)
                    ta.clear()
                    ta.send_keys(text)
                    self._after_field_fill()
            except Exception as e:
                log.debug("Skipping textarea: %s", e)

        for sel_el in modal.find_elements(By.CSS_SELECTOR, SEL["select"]):
            try:
                if not self._select_needs_fill(sel_el):
                    continue
                if not self._element_is_required(sel_el):
                    continue
                label = self._get_label(driver, sel_el)
                opt_els = sel_el.find_elements(By.TAG_NAME, "option")
                preferred = self._answer_select_value(label, opt_els)
                dd = Select(sel_el)
                if preferred:
                    self._apply_select_choice(dd, opt_els, preferred)
                    self._after_field_fill()
                else:
                    log.warning(
                        "No selection rule for required dropdown (job %s) label=%r — abandoning",
                        job.get("id"),
                        label,
                    )
                    return False
            except Exception as e:
                log.debug("Skipping select: %s", e)

        radio_groups: dict[str, list] = defaultdict(list)
        for radio in modal.find_elements(By.CSS_SELECTOR, SEL["radio"]):
            try:
                name = radio.get_attribute("name") or ""
                if name:
                    radio_groups[name].append(radio)
            except Exception:
                continue
        for name, radios in radio_groups.items():
            try:
                if any(r.is_selected() for r in radios):
                    continue
                label = self._label_for_radio_group(driver, radios[0])
                ans = binary_screening_answer(label)
                if ans is None:
                    # Legacy: prefer Yes when value hints yes (unknown questions)
                    if any(
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
                want_yes = ans.strip().lower() == "yes"
                if self._click_yes_no_in_radio_group(driver, radios, want_yes):
                    self._after_field_fill()
            except Exception as e:
                log.debug("Skipping radio group %s: %s", name, e)

        for finp in modal.find_elements(By.CSS_SELECTOR, 'input[type="file"]'):
            try:
                if not self._file_input_is_cover_letter_upload(driver, finp):
                    continue
                if (finp.get_attribute("value") or "").strip():
                    continue
                if not (cover_letter or "").strip():
                    log.warning("Cover letter upload requested but generated cover letter is empty — skipping")
                    continue
                safe_id = re.sub(r"[^\w\-.]+", "_", str(job.get("id", "job")))[:120]
                docx_path = self.cover_letter_docx_dir / f"cover_{safe_id}.docx"
                write_cover_letter_docx(cover_letter, docx_path)
                finp.send_keys(str(docx_path.resolve()))
                self._after_field_fill()
                log.info("Uploaded cover letter as DOCX: %s", docx_path)
            except Exception as e:
                log.warning("Cover letter file upload failed: %s", e)

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

        Marketing / spam consent: decline automated outreach.
        """
        label_l = (label or "").strip().lower()
        screening = binary_screening_answer(label)
        if screening is not None:
            return screening
        if label_l.startswith("would you like to receive"):
            return "No"
        return None

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

        URL-style questions read from the resume / profile JSON (e.g. ``data/resume_profile.json``).
        LinkedIn-style labels use ``linkedin`` / ``linkedin_url``; website labels use ``website`` /
        ``website_url`` / ``personal_website``. If no value is set, returns ``None`` (required → dismiss).
        """
        label = label.lower()
        screening = binary_screening_answer(label)
        if screening is not None:
            return screening

        if any(k in label for k in ("first name", "given name")):
            parts = resume.get("name", "").split()
            return parts[0] if parts else ""
        if any(k in label for k in ("last name", "surname", "family name")):
            parts = resume.get("name", "").split()
            return parts[-1] if len(parts) > 1 else ""
        if "email" in label:
            return resume.get("email", "")
        if "phone" in label or "mobile" in label:
            return resume.get("phone", "")
        if "city" in label:
            return ""
        # Generic total YOE only — not "years of … experience with Python/AI/…" (those need explicit rules).
        if "years" in label and "experience" in label:
            if " with " in label:
                return None
            return str(max(1, len(resume.get("experience", [])) * 2))
        # URLs — keys from resume_profile.json (plain ``linkedin`` / ``website`` preferred)
        if "linkedin" in label:
            for k in ("linkedin", "linkedin_url"):
                v = (resume.get(k) or "").strip()
                if v:
                    return v
            return None
        if "github" in label:
            for k in ("github_url", "github"):
                v = (resume.get(k) or "").strip()
                if v:
                    return v
            return None
        if "portfolio" in label:
            for k in ("portfolio_url", "portfolio"):
                v = (resume.get(k) or "").strip()
                if v:
                    return v
            return None
        if "website" in label:
            for k in ("website", "website_url", "personal_website"):
                v = (resume.get(k) or "").strip()
                if v:
                    return v
            return None

        return None

    def _answer_textarea(self, label: str, cover_letter: str) -> str | None:
        """
        Return text for a textarea, or ``None`` if the label does not match a known pattern
        (caller abandons when the field is required).
        """
        label_l = (label or "").strip().lower()
        screening = binary_screening_answer(label)
        if screening is not None:
            return screening
        if "cover" in label_l:
            return cover_letter
        if "additional" in label_l or "message" in label_l:
            return cover_letter[:500]
        return None

    def _get_label(self, driver, element) -> str:
        try:
            el_id = element.get_attribute("id")
            if el_id:
                labels = driver.find_elements(By.CSS_SELECTOR, f'label[for="{el_id}"]')
                if labels:
                    return labels[0].text

            aria = element.get_attribute("aria-label") or ""
            if aria:
                return aria

            return element.get_attribute("placeholder") or ""
        except Exception:
            return ""
