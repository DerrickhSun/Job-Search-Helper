"""
Greenhouse **MyGreenhouse** candidate portal (https://my.greenhouse.io) — session helpers.

Recruiter tooling lives on ``app.greenhouse.io``; candidate sign-in and job search use ``my.greenhouse.io``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from chrome_driver import build_chrome, save_cookies
from cover_letter import CoverLetterGenerator, write_cover_letter_docx
from greenhouse_fill_rules import DEFAULT_GREENHOUSE_RULES_PATH, GreenhouseFillRulesEngine
from matcher import JobMatcher, print_job_fit_debug
from resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE, load_or_build_resume

log = logging.getLogger(__name__)

MY_GREENHOUSE_ORIGIN = "https://my.greenhouse.io"
GREENHOUSE_SIGN_IN_URL = f"{MY_GREENHOUSE_ORIGIN}/users/sign_in"
GREENHOUSE_DASHBOARD_URL = f"{MY_GREENHOUSE_ORIGIN}/dashboard"
DEFAULT_GREENHOUSE_COOKIE_PATH = Path("data/selenium_greenhouse_cookies.json")


def _load_resume_for_greenhouse(args: Any) -> dict[str, Any] | None:
    cache = Path(getattr(args, "resume_cache", DEFAULT_RESUME_CACHE_PATH))
    resume_pdf = Path(getattr(args, "resume", DEFAULT_RESUME_FILE))
    try:
        return load_or_build_resume(
            resume_pdf if resume_pdf.is_file() else None,
            cache,
            force_reparse=bool(getattr(args, "force_resume_parse", False)),
        )
    except Exception as e:
        log.warning("Could not load resume for Greenhouse: %s", e)
        return None


def _location_is_united_states(location: str) -> bool:
    low = (location or "").strip().lower()
    if not low:
        return True
    return (
        "united states" in low
        or low
        in (
            "us",
            "usa",
            "u.s.",
            "u.s.a.",
            "u.s",
            "united states of america",
        )
    )


def my_greenhouse_jobs_search_url(args: Any) -> str:
    """
    Build MyGreenhouse ``/jobs`` query string with ``query``, ``location``, optional US centroid
    (``lat`` / ``lon`` / ``location_type`` / ``country_short_name``), and ``date_posted`` — same shape as
    ``https://my.greenhouse.io/jobs?query=…&location=United%20States&lat=…&date_posted=past_ten_days``.
    """
    kw_list = getattr(args, "keywords", None) or []
    query_str = " ".join(str(k) for k in kw_list).strip()
    loc = (getattr(args, "location", None) or "United States").strip() or "United States"

    explicit_dp = getattr(args, "greenhouse_date_posted", None)
    if isinstance(explicit_dp, str) and explicit_dp.strip():
        date_posted = explicit_dp.strip()
    else:
        date_posted = "past_ten_days" if getattr(args, "posted_within_24h", True) else "all"

    params: list[tuple[str, str]] = []
    if query_str:
        params.append(("query", query_str))
    params.append(("location", loc))
    if _location_is_united_states(loc):
        params.extend(
            [
                ("lat", "39.71614"),
                ("lon", "-96.999246"),
                ("location_type", "country"),
                ("country_short_name", "US"),
            ]
        )
    if date_posted.lower() not in ("", "all", "any", "any_time"):
        params.append(("date_posted", date_posted))

    return f"{MY_GREENHOUSE_ORIGIN}/jobs?{urlencode(params)}"


def _is_candidate_dashboard(url: str) -> bool:
    """True when the browser is on the MyGreenhouse dashboard (logged-in home)."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return False
    if p.netloc.lower() != "my.greenhouse.io":
        return False
    path = (p.path or "/").rstrip("/") or "/"
    return path == "/dashboard"


def load_greenhouse_cookies(driver: Any, path: Path) -> None:
    """Restore cookies saved from a prior session (``my.greenhouse.io`` / ``.greenhouse.io``)."""
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read Greenhouse cookies from %s: %s", path, e)
        return
    if raw is None:
        log.warning("Greenhouse cookie file %s is JSON null — ignoring.", path)
        return
    if not isinstance(raw, list):
        log.warning(
            "Greenhouse cookie file %s must be a JSON array of cookies (got %s) — ignoring.",
            path,
            type(raw).__name__,
        )
        return
    if not raw:
        return

    driver.get(f"{MY_GREENHOUSE_ORIGIN}/")
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


def _read_resume_profile_email(cache_path: Path) -> str | None:
    """Return a trimmed ``email`` string from resume profile JSON, or ``None`` if missing or invalid."""
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("Could not read resume profile for email (%s): %s", cache_path, e)
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("email")
    if not isinstance(raw, str):
        return None
    email = raw.strip()
    if "@" not in email or len(email) < 5:
        return None
    return email


def _try_submit_my_greenhouse_email_step(driver: Any, email: str) -> None:
    """
    If the sign-in page shows an email field, fill it and submit (button in the same form, else Enter).

    MyGreenhouse may still require SSO or a magic link afterward; :func:`_wait_for_my_greenhouse_dashboard`
    unchanged for detecting a completed session.
    """
    email = (email or "").strip()
    if not email:
        return
    short_wait = WebDriverWait(driver, 2)
    el = None
    candidates: list[tuple[Any, str]] = [
        (By.CSS_SELECTOR, 'input[type="email"]'),
        (By.CSS_SELECTOR, "input#user_email"),
        (By.CSS_SELECTOR, 'input[name="user[email]"]'),
        (By.CSS_SELECTOR, 'input[name="email"]'),
        (By.XPATH, "//input[contains(translate(@name,'EMAIL','email'),'email')]"),
    ]
    for by, sel in candidates:
        try:
            cand = short_wait.until(EC.element_to_be_clickable((by, sel)))
            if cand.is_displayed() and cand.is_enabled():
                el = cand
                break
        except (TimeoutException, Exception):
            continue
    if el is None:
        log.debug("MyGreenhouse sign-in: no email field found — skipping auto-fill.")
        return
    try:
        el.clear()
    except Exception:
        pass
    try:
        el.click()
        el.send_keys(email)
    except Exception as e:
        log.warning("Could not enter email on MyGreenhouse sign-in: %s", e)
        return
    log.info("Filled MyGreenhouse sign-in email from resume profile.")
    submitted = False
    try:
        form = el.find_element(By.XPATH, "./ancestor::form[1]")
        for sub_sel in ('button[type="submit"]', 'input[type="submit"]'):
            try:
                btn = form.find_element(By.CSS_SELECTOR, sub_sel)
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    submitted = True
                    log.info("Submitted MyGreenhouse email step (form %s).", sub_sel)
                    break
            except NoSuchElementException:
                continue
        if not submitted:
            hints = (
                "continue",
                "submit",
                "next",
                "sign in",
                "log in",
                "send link",
                "email me",
            )
            for btn in form.find_elements(By.TAG_NAME, "button"):
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                t = (btn.text or "").strip().lower()
                if any(h in t for h in hints):
                    btn.click()
                    submitted = True
                    log.info("Submitted MyGreenhouse email step (button %r).", (btn.text or "").strip()[:48])
                    break
    except NoSuchElementException:
        pass
    if not submitted:
        try:
            el.send_keys(Keys.RETURN)
            log.info("Submitted MyGreenhouse email step (Enter).")
        except Exception as e:
            log.debug("MyGreenhouse email step: no button click, Enter failed: %s", e)


def _wait_for_my_greenhouse_dashboard(
    driver: Any, *, max_seconds: float, poll_s: float = 1.25
) -> bool:
    """Poll until ``current_url`` is the candidate dashboard or ``max_seconds`` elapses."""
    deadline = time.monotonic() + max(1.0, float(max_seconds))
    while time.monotonic() < deadline:
        try:
            cur = driver.current_url or ""
        except Exception:
            cur = ""
        if _is_candidate_dashboard(cur):
            log.info("Detected MyGreenhouse dashboard: %s", cur)
            return True
        time.sleep(poll_s)
    return False


def _greenhouse_scroll_metrics(driver: Any) -> tuple[int, int]:
    """
    Return ``(job_card_count, content_scroll_height)`` for MyGreenhouse.

    Job cards use ``[data-provides="search-result"]``. The lazy-load scroller is
    ``[data-provides="scroll-container"]`` (``overflow-auto``); its ``scrollHeight`` grows as rows append.
    If that node is missing, falls back to document scroll height and a link-based job estimate.
    """
    try:
        raw = driver.execute_script(
            """
            try {
              const sc = document.querySelector('[data-provides="scroll-container"]');
              let cards = document.querySelectorAll('[data-provides="search-result"]').length;
              if (!cards) {
                const set = new Set();
                for (const a of document.querySelectorAll('a[href*="greenhouse.io"]')) {
                  try {
                    const u = new URL(a.href);
                    const p = u.pathname || '';
                    if (p.includes('/jobs/') || u.searchParams.has('gh_jid'))
                      set.add(u.origin + u.pathname + '?' + u.searchParams.toString());
                  } catch (e) {}
                }
                cards = set.size;
              }
              let sh = 0;
              if (sc) sh = sc.scrollHeight;
              else sh = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              return [cards, sh];
            } catch (e) { return [0, 0]; }
            """
        )
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return int(raw[0] or 0), int(raw[1] or 0)
    except Exception:
        pass
    return 0, 0


def _scroll_greenhouse_viewport(driver: Any) -> None:
    """Scroll the MyGreenhouse inner list (``[data-provides="scroll-container"]``), then the window."""
    try:
        driver.execute_script(
            """
            (function () {
              var sc = document.querySelector('[data-provides="scroll-container"]');
              if (sc) {
                sc.scrollTop = sc.scrollHeight;
                try {
                  sc.dispatchEvent(new Event('scroll', { bubbles: true }));
                } catch (e) {}
              }
              var sy = Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight
              );
              window.scrollTo(0, sy);
            })();
            """
        )
    except Exception as e:
        log.debug("Greenhouse viewport scroll: %s", e)


def _scroll_greenhouse_jobs_to_load_more(
    driver: Any,
    *,
    max_rounds: int = 50,
    pause_s: float = 1.2,
    stable_needed: int = 4,
) -> None:
    """
    Scroll the MyGreenhouse ``scroll-container`` until lazy-loaded ``search-result`` cards stop increasing
    and the container's ``scrollHeight`` stops growing (no more rows appended).
    """
    max_rounds = max(1, int(max_rounds))
    pause_s = max(0.3, float(pause_s))
    stable_needed = max(1, int(stable_needed))
    prev_count, prev_h = _greenhouse_scroll_metrics(driver)
    stable = 0

    for i in range(max_rounds):
        _scroll_greenhouse_viewport(driver)
        time.sleep(pause_s)
        cur_count, cur_h = _greenhouse_scroll_metrics(driver)

        grew = (cur_count > prev_count) or (cur_h > prev_h + 8)
        if grew:
            stable = 0
            log.debug(
                "Greenhouse scroll round %d: job cards/links≈%d (was %d), scrollHeight≈%d (was %d)",
                i + 1,
                cur_count,
                prev_count,
                cur_h,
                prev_h,
            )
        else:
            stable += 1
            if stable >= stable_needed:
                log.info(
                    "Greenhouse job list: no more jobs loading after %d scroll round(s) "
                    "(~%d job card(s) in DOM).",
                    i + 1,
                    cur_count,
                )
                return

        prev_count = cur_count
        prev_h = cur_h

    log.info(
        "Greenhouse job list: stopped after %d scroll round(s) (max rounds); ~%d job card(s) in DOM.",
        max_rounds,
        prev_count,
    )


def collect_my_greenhouse_view_job_hrefs(driver: Any) -> list[str]:
    """
    Collect ``href`` values from MyGreenhouse job cards whose primary CTA reads like **View job**
    (the links are ``<a class="btn …" href="…">`` with that label).
    Order follows DOM; duplicates are dropped.
    """
    try:
        raw = driver.execute_script(
            """
            try {
              const out = [];
              const seen = new Set();
              for (const a of document.querySelectorAll('a[href]')) {
                const label = (a.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!label.includes('view job')) continue;
                let h = (a.getAttribute('href') || '').trim();
                if (!h) continue;
                try {
                  const abs = new URL(h, document.baseURI || location.href).href;
                  if (seen.has(abs)) continue;
                  seen.add(abs);
                  out.push(abs);
                } catch (e) {}
              }
              return out;
            } catch (e) { return []; }
            """
        )
    except Exception as e:
        log.warning("Could not collect View job links: %s", e)
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def _try_click_apply_button_fallback(driver: Any, *, timeout_s: float = 12) -> bool:
    """
    Fallback when MyGreenhouse autofill is unavailable: click a visible **Apply** button once.
    """
    lo = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )
    apply_xpath = (
        f"//button[normalize-space({lo})='apply'] | "
        f"//a[normalize-space({lo})='apply'] | "
        f"//*[@role='button'][normalize-space({lo})='apply']"
    )
    wait = WebDriverWait(driver, max(3.0, float(timeout_s)))
    try:
        el = wait.until(EC.element_to_be_clickable((By.XPATH, apply_xpath)))
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.15)
        except Exception:
            pass
        driver.execute_script("arguments[0].click();", el)
        log.info("Clicked Apply button fallback (no Autofill with MyGreenhouse control found).")
        time.sleep(1.2)
        return True
    except TimeoutException:
        return False
    except Exception as e:
        log.debug("Greenhouse Apply-button fallback click failed: %s", e)
        return False


def _try_click_mygreenhouse_autofill(driver: Any, *, timeout_s: float = 28) -> bool:
    """
    On a Greenhouse job application page, click **Autofill with MyGreenhouse** once.

    Waits for the control to become clickable (embed may hydrate after load), then performs a single click.
    """
    lo = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )
    combined_xpath = (
        f"//button[contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')] | "
        f"//a[contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')] | "
        f"//*[@role='button'][contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')]"
    )
    wait = WebDriverWait(driver, max(5.0, float(timeout_s)))
    try:
        el = wait.until(EC.element_to_be_clickable((By.XPATH, combined_xpath)))
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.15)
        except Exception:
            pass
        driver.execute_script("arguments[0].click();", el)
        log.info("Clicked Autofill with MyGreenhouse (once).")
        time.sleep(1.5)
        return True
    except TimeoutException:
        pass
    except Exception as e:
        log.debug("MyGreenhouse autofill XPath click: %s", e)

    try:
        ok = driver.execute_script(
            """
            (function () {
              function want(t) {
                if (!t) return false;
                var s = String(t).replace(/\\s+/g, ' ').trim().toLowerCase();
                return s.indexOf('autofill') >= 0 && s.indexOf('mygreenhouse') >= 0;
              }
              var nodes = document.querySelectorAll('button, a, [role="button"]');
              for (var i = 0; i < nodes.length; i++) {
                var n = nodes[i];
                try {
                  var label = (n.innerText || n.textContent || '');
                  if (!want(label)) continue;
                  var r = n.getBoundingClientRect();
                  if (r.width < 2 || r.height < 2) continue;
                  n.scrollIntoView({block: 'center'});
                  n.click();
                  return true;
                } catch (e) {}
              }
              return false;
            })();
            """
        )
        if ok:
            log.info("Clicked Autofill with MyGreenhouse (once, script fallback).")
            time.sleep(1.5)
            return True
    except Exception as e:
        log.debug("MyGreenhouse autofill script fallback: %s", e)

    # Some listings have only a plain "Apply" control (no MyGreenhouse autofill entry point).
    return _try_click_apply_button_fallback(driver)


def visit_first_greenhouse_job_and_autofill(driver: Any, view_job_hrefs: list[str]) -> str | None:
    """
    Prototype: open the first **View job** URL and trigger MyGreenhouse autofill on that application page.
    Returns the opened listing URL on success, else ``None``.
    """
    if not view_job_hrefs:
        log.info("No View job links — skipping first-job autofill visit.")
        return None
    first = view_job_hrefs[0].strip()
    if not first:
        return None
    log.info("Opening first job application page (autofill prototype): %s", first)
    try:
        driver.get(first)
    except Exception as e:
        log.warning("Could not navigate to first job URL: %s", e)
        return None
    time.sleep(2.0)
    if not _try_click_mygreenhouse_autofill(driver):
        log.warning(
            "Did not find a clickable Autofill with MyGreenhouse or Apply control on the page — "
            "complete start-apply manually if it appears after load."
        )
    return first


def _greenhouse_listing_id(listing_url: str) -> str:
    """Stable-ish id for filenames from a Greenhouse job board URL."""
    try:
        u = urlparse(listing_url)
        parts = [p for p in (u.path or "").split("/") if p]
        if parts:
            tail = parts[-1].split("?")[0].replace(".", "_")
            if tail:
                return tail[:80]
    except Exception:
        pass
    return re.sub(r"[^\w\-.]+", "_", listing_url)[:80]


def _scrape_greenhouse_job_for_cover_letter(driver: Any, listing_url: str) -> dict[str, Any]:
    """Best-effort job title / company / description from the embedded application page for cover letter context."""
    try:
        raw = driver.execute_script(
            """
            try {
              const h1 = document.querySelector('h1');
              const t = (h1 && h1.innerText) ? h1.innerText.trim() : '';
              let company = '';
              const c1 = document.querySelector('[data-company-name]');
              if (c1) company = (c1.innerText || '').trim();
              if (!company) {
                const og = document.querySelector('meta[property="og:site_name"]');
                if (og) company = (og.getAttribute('content') || '').trim();
              }
              let desc = '';
              const main = document.querySelector(
                '#content, main .content, main, [data-job-detail], .content'
              );
              if (main) desc = (main.innerText || '').trim();
              return { title: t, company: company, description: desc.slice(0, 8000) };
            } catch (e) {
              return { title: '', company: '', description: '' };
            }
            """
        )
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    title = (raw.get("title") or "").strip() or "Role"
    company = (raw.get("company") or "").strip() or "Company"
    description = (raw.get("description") or "").strip()
    if not description:
        description = f"Job listing: {listing_url}"
    jid = _greenhouse_listing_id(listing_url)
    return {"id": jid, "title": title, "company": company, "description": description}


def _upload_file_to_greenhouse_cover_letter_input(driver: Any, docx_path: Path, *, timeout_s: float = 22) -> bool:
    """
    Upload a DOCX to Greenhouse's **Cover Letter** widget (``input#cover_letter`` inside ``div.file-upload``).

    Selenium usually accepts ``send_keys`` on the hidden file input; if that fails, clicks **Attach** once then retries.
    """
    ap = str(docx_path.resolve())
    if not docx_path.is_file():
        log.warning("Cover letter file missing at %s", ap)
        return False
    wait = WebDriverWait(driver, max(5.0, float(timeout_s)))
    try:
        label = wait.until(EC.presence_of_element_located((By.ID, "upload-label-cover_letter")))
        root = label.find_element(By.XPATH, "./ancestor::div[contains(@class,'file-upload')][1]")
    except (TimeoutException, NoSuchElementException):
        try:
            root = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[contains(@class,'file-upload')][.//label[@id='upload-label-cover_letter']]")
                )
            )
        except (TimeoutException, NoSuchElementException):
            log.warning("Greenhouse cover letter upload region (upload-label-cover_letter) not found.")
            return False
    def _send_to_input() -> bool:
        try:
            finp = root.find_element(By.CSS_SELECTOR, 'input#cover_letter[type="file"]')
        except NoSuchElementException:
            return False
        finp.send_keys(ap)
        return True

    try:
        if _send_to_input():
            log.info("Uploaded cover letter DOCX to Greenhouse (input#cover_letter): %s", ap)
            return True
    except Exception as e:
        log.debug("Direct send_keys to Greenhouse cover letter input: %s", e)
    try:
        for btn in root.find_elements(By.XPATH, ".//button[normalize-space()='Attach']"):
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                time.sleep(0.4)
                break
        if _send_to_input():
            log.info("Uploaded cover letter DOCX after Attach: %s", ap)
            return True
    except Exception as e:
        log.warning("Greenhouse cover letter upload failed: %s", e)
    return False


def maybe_upload_greenhouse_cover_letter(
    driver: Any,
    args: Any,
    view_job_hrefs: list[str],
    *,
    resume: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> None:
    """
    On the current Greenhouse application page, generate a cover letter (same generator as LinkedIn) and
    attach the DOCX via the **Cover Letter** file field.

    Pass ``resume`` and ``job`` when already loaded (e.g. after gate checks) to avoid duplicate work.
    """
    if not view_job_hrefs:
        return
    listing = (view_job_hrefs[0] or "").strip()
    if not listing:
        return
    if resume is None:
        resume = _load_resume_for_greenhouse(args)
    if resume is None:
        log.warning("Skipping Greenhouse cover letter: could not load resume.")
        return
    try:
        if job is None:
            job = _scrape_greenhouse_job_for_cover_letter(driver, listing)
        cover_text = CoverLetterGenerator().generate(resume, job)
    except Exception as e:
        log.warning("Skipping Greenhouse cover letter: generation failed: %s", e)
        return
    if not (cover_text or "").strip():
        log.warning("Generated cover letter is empty — skipping Greenhouse upload.")
        return
    docx_dir = Path(getattr(args, "cover_letter_dir", Path("output/coverletters")))
    docx_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-.]+", "_", str(job.get("id", "job")))[:120]
    out_file = docx_dir / f"gh_cover_{safe}.docx"
    try:
        write_cover_letter_docx(cover_text, out_file)
    except Exception as e:
        log.warning("Could not write Greenhouse cover letter DOCX: %s", e)
        return
    time.sleep(0.8)
    _upload_file_to_greenhouse_cover_letter_input(driver, out_file)


def _greenhouse_fieldset_legend_text(fs: Any) -> str:
    try:
        leg = fs.find_element(By.CSS_SELECTOR, "legend")
        return (leg.text or "").strip()
    except NoSuchElementException:
        return (fs.text or "")[:500].strip()


def _click_checkbox_in_fieldset_by_label(fs: Any, choose_label: str) -> bool:
    """Select a checkbox in ``fs`` whose visible label matches ``choose_label`` (normalized)."""
    want = " ".join((choose_label or "").lower().split())
    if not want:
        return False
    for inp in fs.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]'):
        try:
            iid = (inp.get_attribute("id") or "").strip()
            if not iid:
                continue
            lab = fs.find_element(By.XPATH, f'.//label[@for="{iid}"]')
            opt = " ".join((lab.text or "").lower().split())
            if opt != want:
                continue
            if inp.is_selected():
                log.info('Checkbox group: option %r already selected.', choose_label)
                return True
            try:
                lab.click()
            except Exception:
                inp.click()
            time.sleep(0.25)
            log.info('Checkbox group: selected option %r (from Greenhouse rules).', choose_label)
            return True
        except NoSuchElementException:
            continue
        except Exception as e:
            log.debug("Checkbox row: %s", e)
            continue
    log.debug("No checkbox option matched label %r inside fieldset.", choose_label)
    return False


def maybe_apply_greenhouse_checkbox_rules(driver: Any, args: Any) -> bool:
    """
    For each ``fieldset.checkbox``, if ``data/greenhouse_fill_rules.json`` matches the legend, select
    ``choose_label`` for that rule (same ``match`` vocabulary as LinkedIn ``form_fill_rules.json``).
    """
    rules_path = Path(getattr(args, "greenhouse_fill_rules", DEFAULT_GREENHOUSE_RULES_PATH))
    try:
        engine = GreenhouseFillRulesEngine(rules_path)
    except Exception as e:
        log.warning("Greenhouse fill rules could not be loaded (%s).", e)
        return False
    if not engine.has_checkbox_rules():
        return False
    any_applied = False
    try:
        fieldsets = driver.find_elements(By.CSS_SELECTOR, "fieldset.checkbox")
    except Exception:
        return False
    for fs in fieldsets:
        try:
            legend = _greenhouse_fieldset_legend_text(fs)
            want = engine.checkbox_group_choice(legend)
            if not want:
                continue
            if _click_checkbox_in_fieldset_by_label(fs, want):
                any_applied = True
        except Exception as e:
            log.debug("Greenhouse checkbox fieldset: %s", e)
            continue
    return any_applied


def _pause_until_user_closes_browser() -> None:
    """Block until Enter so the user can inspect the browser (development / debugging)."""
    try:
        input("Press Enter here to close Chrome (MyGreenhouse session)… ")
    except EOFError:
        pass


def run_greenhouse_sign_in_flow(args) -> None:
    """
    Open Chrome on MyGreenhouse sign-in, wait until ``/dashboard``, then open ``/jobs?query=…`` using
    ``--keywords``, scroll to load lazy results, collect **View job** URLs, write them to JSON, open the
    **first** job page and click **Autofill with MyGreenhouse** (prototype), generate a cover letter from the
    resume cache (same pipeline as LinkedIn), attach it to the **Cover Letter** file field when present,
    apply ``data/greenhouse_fill_rules.json`` checkbox rules when present, save cookies, then (by default)
    wait for Enter before closing Chrome.

    Hard gates (education + minimum years), same as LinkedIn ``JobMatcher.gates_pass``, run before cover letter
    generation and checkbox automation; if they fail, those steps are skipped (browser stays on the page for
    manual review).
    """
    path = Path(args.greenhouse_cookies)
    max_wait = float(getattr(args, "greenhouse_login_max_seconds", 600.0))
    prompt_before_close = bool(getattr(args, "greenhouse_prompt_before_close", True))
    driver = None
    try:
        driver = build_chrome(headless=args.headless)
        load_greenhouse_cookies(driver, path)

        def _save_greenhouse_session_snapshot(note: str) -> None:
            """
            Persist the current MyGreenhouse session cookies as soon as login state is confirmed.
            This reduces re-login friction if later steps fail mid-run.
            """
            try:
                driver.get(f"{MY_GREENHOUSE_ORIGIN}/")
                time.sleep(0.4)
                save_cookies(driver, path)
                log.info("Saved Greenhouse cookies (%s): %s", note, path.resolve())
            except Exception as e:
                log.warning("Could not save Greenhouse cookies (%s): %s", note, e)

        log.info("Opening MyGreenhouse sign-in (candidates): %s", GREENHOUSE_SIGN_IN_URL)
        driver.get(GREENHOUSE_SIGN_IN_URL)
        time.sleep(1.0)

        if _is_candidate_dashboard(driver.current_url or ""):
            log.info("Already on dashboard (session from cookies).")
            _save_greenhouse_session_snapshot("dashboard already active")
        else:
            cache_path = Path(getattr(args, "resume_cache", DEFAULT_RESUME_CACHE_PATH))
            profile_email = _read_resume_profile_email(cache_path)
            if profile_email:
                _try_submit_my_greenhouse_email_step(driver, profile_email)
                time.sleep(0.6)
            else:
                log.debug(
                    "No usable email in %s — enter email manually in the browser if prompted.",
                    cache_path,
                )
            log.info(
                "Complete any remaining sign-in steps in the browser (e.g. Google SSO or email link). "
                "Waiting up to %.0fs for URL %s …",
                max_wait,
                GREENHOUSE_DASHBOARD_URL,
            )
            if not _wait_for_my_greenhouse_dashboard(driver, max_seconds=max_wait):
                log.warning(
                    "Timed out waiting for dashboard — saving cookies anyway, then exiting. "
                    "Try a visible window (--no-headless) or increase --greenhouse-login-max-seconds.",
                )
                save_cookies(driver, path)
                return
            _save_greenhouse_session_snapshot("dashboard reached after sign-in")

        jobs_url = my_greenhouse_jobs_search_url(args)
        log.info("Opening MyGreenhouse job search: %s", jobs_url)
        driver.get(jobs_url)
        time.sleep(1.5)
        _scroll_greenhouse_jobs_to_load_more(
            driver,
            max_rounds=int(args.greenhouse_scroll_max_rounds),
            pause_s=float(args.greenhouse_scroll_pause),
        )
        view_job_hrefs = collect_my_greenhouse_view_job_hrefs(driver)
        log.info("Collected %d distinct View job link(s).", len(view_job_hrefs))
        out_path = Path("output/greenhouse_view_job_links.json")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(view_job_hrefs, indent=2), encoding="utf-8")
            log.info("Wrote job URLs to %s", out_path.resolve())
        except OSError as e:
            log.warning("Could not write %s: %s", out_path, e)
        first_opened = visit_first_greenhouse_job_and_autofill(driver, view_job_hrefs)
        if first_opened:
            resume = _load_resume_for_greenhouse(args)
            if resume is None:
                log.warning(
                    "Skipping Greenhouse gates / cover letter / checkbox rules — resume profile unavailable."
                )
            else:
                job = _scrape_greenhouse_job_for_cover_letter(driver, first_opened)
                matcher = JobMatcher()
                if not matcher.gates_pass(resume, job):
                    log.info(
                        "Skipping Greenhouse cover letter and checkbox rules "
                        "(education or experience requirements not met): %s at %s",
                        job.get("title"),
                        job.get("company"),
                    )
                    print_job_fit_debug(
                        job.get("company"),
                        job.get("title"),
                        None,
                        note="gates_failed_greenhouse",
                    )
                else:
                    maybe_upload_greenhouse_cover_letter(
                        driver, args, [first_opened], resume=resume, job=job
                    )
                    maybe_apply_greenhouse_checkbox_rules(driver, args)
        save_cookies(driver, path)
        log.info("Greenhouse session saved (%s).", path.resolve())
    finally:
        if driver is not None:
            if prompt_before_close:
                log.info(
                    "Leaving the browser open — inspect the page, then press Enter in this terminal to quit Chrome."
                )
                _pause_until_user_closes_browser()
            driver.quit()
