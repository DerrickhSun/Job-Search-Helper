"""
Greenhouse **MyGreenhouse** candidate portal (https://my.greenhouse.io) — session helpers.

Recruiter tooling lives on ``app.greenhouse.io``; candidate sign-in and job search use ``my.greenhouse.io``.
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .company_blacklist import is_company_blacklisted, load_company_blacklist
from .chrome_driver import (
    build_chrome,
    driver_session_alive,
    focus_element,
    log_driver_session_closed,
    save_cookies,
)
from .cover_letter import CoverLetterGenerator, cover_letter_docx_path_unique, delete_cover_letter_for_job, write_cover_letter_docx
from .dspy_lm import configure_dspy
from .form_fill_rules import DEFAULT_RULES_PATH, FormFillRulesEngine
from .job_searcher import DEFAULT_JOB_SEARCH_KEYWORDS
from .matcher import JobMatcher, print_job_fit_debug
from .output_paths import (
    ASSISTED_APPLICATIONS_CSV as ASSISTED_GREENHOUSE_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV as ASSISTED_GREENHOUSE_HISTORY_CSV,
    GREENHOUSE_COVERLETTERS_DIR,
    GREENHOUSE_DISMISSED_CSV,
)
from .resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE, load_or_build_resume
from .tracker import ApplicationTracker, normalize_greenhouse_job_url

log = logging.getLogger(__name__)

# Serializes appends to the dismissed / assisted CSVs: with --greenhouse-prefetch the scanner thread and
# the presenter (main) thread can both write, and CSV appends are not atomic across threads.
_GREENHOUSE_CSV_WRITE_LOCK = threading.Lock()

MY_GREENHOUSE_ORIGIN = "https://my.greenhouse.io"
GREENHOUSE_SIGN_IN_URL = f"{MY_GREENHOUSE_ORIGIN}/users/sign_in"
GREENHOUSE_DASHBOARD_URL = f"{MY_GREENHOUSE_ORIGIN}/dashboard"
DEFAULT_GREENHOUSE_COOKIE_PATH = Path("data/selenium_greenhouse_cookies.json")
DEFAULT_APPLICATIONS_DB = Path("data/applications.db")

# After opening MyGreenhouse sign-in, poll briefly for a cookie-based redirect before touching the form.
GREENHOUSE_SIGN_IN_POST_NAV_DELAY_S = 10.0
GREENHOUSE_SIGN_IN_REDIRECT_POLL_S = 0.5

# Harvest job description from embedded boards (same priority idea as EasyApplyFiller).
GREENHOUSE_DESCRIPTION_IFRAME_SELECTORS: tuple[str, ...] = (
    "iframe#grnhse_iframe",
    "iframe[id='grnhse_iframe']",
    "iframe[src*='job-boards.greenhouse.io']",
    "iframe[src*='boards.greenhouse.io/embed']",
    "iframe[src*='greenhouse.io/embed/job_app']",
)

_RE_JOB_DESC_YEARSISH = re.compile(
    r"\d\s*[-–]\s*\d+\s*years?|\d+\s*\+?\s*years?\s+of\s+(?:professional\s+|work\s+)?experience",
    re.IGNORECASE,
)

_GREENHOUSE_JOB_PAGE_HARVEST_JS = """
return (function () {
  var selectors = [
    '.job__description', '.job-description', '#job_description',
    '[class*="JobDescription"]', '[class*="job__description"]', '[data-job-description]',
    '#app_body', '#content', 'main .content', 'main', '[data-job-detail]', '.content'
  ];
  var best = '';
  for (var si = 0; si < selectors.length; si++) {
    try {
      var nodes = document.querySelectorAll(selectors[si]);
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        try {
          var t = (el && el.innerText) ? String(el.innerText).trim() : '';
          if (t.length > best.length) best = t;
        } catch (e1) {}
      }
    } catch (e2) {}
  }
  var h1 = document.querySelector('h1');
  var title = (h1 && h1.innerText) ? h1.innerText.trim() : '';
  var company = '';
  var c1 = document.querySelector('[data-company-name]');
  if (c1) company = (c1.innerText || '').trim();
  if (!company) {
    var og = document.querySelector('meta[property="og:site_name"]');
    if (og) company = (og.getAttribute('content') || '').trim();
  }
  return { title: title, company: company, description: best };
})();
"""


def _greenhouse_description_rank(text: str | None) -> tuple[int, int]:
    """(1, len) when text looks like it contains a years-of-experience line; else (0, len)."""
    t = (text or "").strip()
    if not t:
        return (0, 0)
    has_y = bool(_RE_JOB_DESC_YEARSISH.search(t))
    return (1 if has_y else 0, len(t))


def _greenhouse_description_beats(a: str, b: str) -> bool:
    """True if ``b`` is a better description blob than ``a`` for gate / matcher purposes."""
    return _greenhouse_description_rank(b) > _greenhouse_description_rank(a)


def _harvest_greenhouse_job_dict_current_document(driver: Any) -> dict[str, str]:
    try:
        raw = driver.execute_script(_GREENHOUSE_JOB_PAGE_HARVEST_JS)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    desc = (raw.get("description") or "").strip()
    if len(desc) > 120_000:
        desc = desc[:120_000]
    return {
        "title": (raw.get("title") or "").strip(),
        "company": (raw.get("company") or "").strip(),
        "description": desc,
    }


def _merge_greenhouse_harvest_dicts(primary: dict[str, str], secondary: dict[str, str]) -> dict[str, str]:
    dp = (primary.get("description") or "").strip()
    ds = (secondary.get("description") or "").strip()
    win, lose = (secondary, primary) if _greenhouse_description_beats(dp, ds) else (primary, secondary)
    return {
        "title": (win.get("title") or lose.get("title") or "").strip(),
        "company": (win.get("company") or lose.get("company") or "").strip(),
        "description": ((win.get("description") or lose.get("description") or "").strip()),
    }


def _greenhouse_iframe_elements_for_job_harvest(driver: Any) -> list[Any]:
    _greenhouse_switch_default_content(driver)
    seen: set[int] = set()
    out: list[Any] = []
    for sel in GREENHOUSE_DESCRIPTION_IFRAME_SELECTORS:
        for fr in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                k = id(fr)
                if k in seen:
                    continue
                seen.add(k)
                out.append(fr)
            except Exception:
                continue
    for fr in driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
        try:
            k = id(fr)
            if k in seen:
                continue
            seen.add(k)
            out.append(fr)
        except Exception:
            continue
    return out[:40]


def _harvest_greenhouse_job_from_open_tabs(driver: Any) -> dict[str, str]:
    """
    Merge the richest job-description text from the top document and likely Greenhouse iframes.

    Company career pages often put the JD in ``.job__description`` while ``querySelector('#content')`` can
    match a **smaller** node first — gates then miss phrases like ``3-5 years of professional experience``.
    """
    _greenhouse_switch_default_content(driver)
    best = _harvest_greenhouse_job_dict_current_document(driver)
    for fr in _greenhouse_iframe_elements_for_job_harvest(driver):
        try:
            _greenhouse_switch_default_content(driver)
            driver.switch_to.frame(fr)
            cand = _harvest_greenhouse_job_dict_current_document(driver)
            best = _merge_greenhouse_harvest_dicts(best, cand)
        except Exception as e:
            log.debug("Greenhouse job harvest: iframe skip (%s)", e)
        finally:
            _greenhouse_switch_default_content(driver)
    return best


def _skip_keys_from_assisted_applications_csv_path(path: Path) -> set[str]:
    """Normalized job URL keys from one assisted-applications CSV (current or history archive)."""
    if not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or len(row) < 5:
                    continue
                if (row[1] or "").strip().lower() == "company" and (row[0] or "").strip() == "":
                    continue
                url = (row[4] or "").strip()
                if not url:
                    continue
                k = normalize_greenhouse_job_url(url)
                if k:
                    keys.add(k)
    except Exception as e:
        log.debug("Could not read %s for dedupe: %s", path, e)
    return keys


def _skip_keys_from_assisted_greenhouse_csv() -> set[str]:
    """Normalized URLs from active assisted CSV and ``output/archive/assisted_applications_history.csv`` (manual ``n``)."""
    return _skip_keys_from_assisted_applications_csv_path(ASSISTED_GREENHOUSE_CSV) | _skip_keys_from_assisted_applications_csv_path(
        ASSISTED_GREENHOUSE_HISTORY_CSV
    )


_DISMISS_RETENTION_DAYS = 30


def _parse_greenhouse_dismissed_date(s: str):
    """Parse ``YYYY-MM-DD`` or ISO datetime string to a ``date``, or ``None``."""
    from datetime import date, datetime, timezone

    t = (s or "").strip()
    if not t:
        return None
    if len(t) >= 10 and t[4] == "-" and t[7] == "-":
        try:
            return date.fromisoformat(t[:10])
        except ValueError:
            return None
    try:
        if "T" in t:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.date()
    except ValueError:
        return None
    return None


def prune_and_read_greenhouse_dismissed_url_keys() -> frozenset[str]:
    """
    Remove dismissals older than :data:`_DISMISS_RETENTION_DAYS` calendar days (vs :func:`datetime.date.today`),
    rewrite ``output/greenhouse_dismissed.csv`` with kept rows, return normalized URL keys for dedupe/skip.
    """
    from datetime import date

    p = GREENHOUSE_DISMISSED_CSV
    today = date.today()
    keys: set[str] = set()
    kept: list[tuple[str, str]] = []
    if not p.is_file():
        return frozenset()
    try:
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except OSError as e:
        log.warning("Could not read %s: %s — skipping dismissal skip list.", p, e)
        return frozenset()

    start = 0
    if rows and len(rows[0]) >= 2:
        a = (rows[0][0] or "").strip().lower()
        b = (rows[0][1] or "").strip().lower()
        if a == "url" and b in ("dismissed_on", "date", "dismissed_at"):
            start = 1

    pruned = 0
    for row in rows[start:]:
        if not row:
            continue
        u = (row[0] or "").strip()
        if not u:
            continue
        date_s = (row[1] or "").strip() if len(row) > 1 else ""
        d = _parse_greenhouse_dismissed_date(date_s)
        if d is None:
            kept.append((u, date_s or today.isoformat()))
            k = normalize_greenhouse_job_url(u)
            if k:
                keys.add(k)
            continue
        if (today - d).days > _DISMISS_RETENTION_DAYS:
            pruned += 1
            continue
        kept.append((u, d.isoformat()))
        k = normalize_greenhouse_job_url(u)
        if k:
            keys.add(k)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["url", "dismissed_on"])
            w.writerows(kept)
    except OSError as e:
        log.warning("Could not rewrite %s after pruning dismissals: %s", p, e)

    if pruned:
        log.info(
            "Greenhouse dismissals: pruned %d row(s) older than %d days — %d kept in %s.",
            pruned,
            _DISMISS_RETENTION_DAYS,
            len(kept),
            p.as_posix(),
        )
    return frozenset(keys)


def _append_greenhouse_dismissed(listing_url: str, *, source: str = "terminal") -> None:
    from datetime import date

    u = (listing_url or "").strip()
    if not u:
        log.warning("Greenhouse dismiss: empty URL — not writing CSV.")
        return
    GREENHOUSE_DISMISSED_CSV.parent.mkdir(parents=True, exist_ok=True)
    row_date = date.today().isoformat()
    with _GREENHOUSE_CSV_WRITE_LOCK:
        new_file = not GREENHOUSE_DISMISSED_CSV.is_file()
        with GREENHOUSE_DISMISSED_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["url", "dismissed_on"])
            w.writerow([u, row_date])
    log.info(
        "Recorded Greenhouse dismiss (%s) to %s",
        source,
        GREENHOUSE_DISMISSED_CSV.resolve(),
    )


def load_greenhouse_skip_url_keys(db_path: Path | str | None = None) -> frozenset[str]:
    """
    Normalized Greenhouse job ``url`` keys to skip when collecting listings: ``applied`` / ``apply_opened``
    rows in the applications database plus URLs in ``output/assisted_applications.csv`` and
    ``output/archive/assisted_applications_history.csv`` (archived manual ``n`` rows), plus URLs in
    ``output/greenhouse_dismissed.csv`` dismissed within the last 30 days
    (file is pruned of older rows on each load).
    """
    keys: set[str] = set(_skip_keys_from_assisted_greenhouse_csv())
    try:
        keys |= set(prune_and_read_greenhouse_dismissed_url_keys())
    except Exception as e:
        log.warning("Could not merge Greenhouse dismissal skip keys: %s", e)
    p = Path(db_path) if db_path is not None else DEFAULT_APPLICATIONS_DB
    if p.is_file():
        try:
            keys |= set(ApplicationTracker(str(p)).recorded_greenhouse_job_url_keys())
        except Exception as e:
            log.warning("Could not load prior application URLs for Greenhouse dedupe (%s): %s", p, e)
    return frozenset(keys)


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


def my_greenhouse_jobs_search_url(args: Any, *, query: str = "") -> str:
    """
    Build MyGreenhouse ``/jobs`` URL with a **single** ``query=`` phrase, ``location``, optional US centroid
    (``lat`` / ``lon`` / ``location_type`` / ``country_short_name``), and ``date_posted`` — same shape as
    ``https://my.greenhouse.io/jobs?query=…&location=United%20States&lat=…&date_posted=past_ten_days``.

    Each ``--keywords`` entry is searched separately in :func:`run_greenhouse_sign_in_flow`; pass ``query=""``
    to open the jobs page with location/date filters only.
    """
    query_str = (query or "").strip()
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
    ok = 0
    for c in raw:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
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
            if "httpOnly" in c:
                cookie["httpOnly"] = bool(c["httpOnly"])
            ss = c.get("sameSite")
            if isinstance(ss, str) and ss.strip():
                sl = ss.strip().lower()
                if sl == "strict":
                    cookie["sameSite"] = "Strict"
                elif sl == "lax":
                    cookie["sameSite"] = "Lax"
                elif sl == "none":
                    cookie["sameSite"] = "None"
            driver.add_cookie(cookie)
            ok += 1
        except Exception as e:
            log.debug("Greenhouse add_cookie skipped (%s): %s", c.get("name"), e)
            continue
    log.info("Loaded %d/%d Greenhouse cookie entr(y/ies) from %s", ok, len(raw), path)
    if ok == 0 and len(raw) > 0:
        log.warning(
            "No Greenhouse cookies were restored (all add_cookie calls failed). "
            "File may be expired, wrong domain, or from a different Chrome profile.",
        )


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


def _greenhouse_page_says_expired(driver: Any) -> bool:
    """True when MyGreenhouse shows the Devise-style stale-session / CSRF error."""
    try:
        blob = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        return False
    if "expired" not in blob:
        return False
    return any(
        phrase in blob
        for phrase in (
            "page has expired",
            "page expired",
            "session expired",
            "please try again",
            "sign in again",
        )
    )


def _open_my_greenhouse_sign_in(driver: Any) -> None:
    """Navigate to the candidate sign-in page (fresh GET for a new CSRF token)."""
    driver.get(GREENHOUSE_SIGN_IN_URL)
    time.sleep(0.35)


def _try_my_greenhouse_email_sign_in(driver: Any, email: str) -> None:
    """
    Fill and submit the email step on a **fresh** sign-in page.

    Reloads once when MyGreenhouse responds with "page has expired" (stale CSRF / session).
    """
    email = (email or "").strip()
    if not email:
        return
    _open_my_greenhouse_sign_in(driver)
    _try_submit_my_greenhouse_email_step(driver, email)
    time.sleep(0.5)
    if _greenhouse_page_says_expired(driver):
        log.warning(
            "MyGreenhouse sign-in page reported expired — reloading sign-in and retrying email once."
        )
        _open_my_greenhouse_sign_in(driver)
        _try_submit_my_greenhouse_email_step(driver, email)
        time.sleep(0.5)


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


def _reveal_greenhouse_job_cards_for_collection(driver: Any) -> None:
    """
    Scroll each ``search-result`` card into view so lazy-hydrated **View job** / job-board links render.
    """
    try:
        driver.execute_script(
            """
            try {
              const sc = document.querySelector('[data-provides="scroll-container"]');
              const cards = [...document.querySelectorAll('[data-provides="search-result"]')];
              for (const card of cards) {
                try {
                  card.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                  if (sc) {
                    const r = card.getBoundingClientRect();
                    const sr = sc.getBoundingClientRect();
                    if (r.top < sr.top || r.bottom > sr.bottom) {
                      sc.scrollTop += (r.top - sr.top) - Math.min(120, sr.height * 0.2);
                    }
                  }
                } catch (e) {}
              }
            } catch (e) {}
            """
        )
    except Exception as e:
        log.debug("Greenhouse reveal cards: %s", e)
    time.sleep(0.35)


def _filter_greenhouse_listings_by_company_blacklist(
    rows: list[dict[str, str]],
    company_blacklist: list[str],
) -> list[dict[str, str]]:
    """Drop rows whose card ``company`` matches :func:`is_company_blacklisted`."""
    if not company_blacklist:
        return rows
    kept: list[dict[str, str]] = []
    skipped = 0
    for row in rows:
        company = (row.get("company") or "").strip()
        if is_company_blacklisted(company, company_blacklist):
            skipped += 1
            log.debug(
                "Greenhouse job list: skip blacklisted company %r (%s)",
                company or "(unknown)",
                (row.get("title") or row.get("url") or "").strip(),
            )
            continue
        kept.append(row)
    if skipped:
        log.info(
            "Greenhouse job list: skipped %d listing(s) from blacklisted companies.",
            skipped,
        )
    return kept


def collect_my_greenhouse_view_job_listings(
    driver: Any,
    *,
    skip_url_keys: frozenset[str] | None = None,
    company_blacklist: list[str] | None = None,
) -> list[dict[str, str]]:
    """
    Collect job-board apply URLs from MyGreenhouse ``search-result`` cards together with **company** and
    **title** parsed from each card.

    Prefer any ``job-boards.greenhouse.io/.../jobs/...`` (or ``gh_jid``) link inside the card; fall back to
    legacy anchors whose visible label includes **View job**.

    Each item is ``{"url": "...", "company": "...", "title": "..."}``. Order follows DOM; duplicate URLs
    are dropped (first card wins).

    If ``company_blacklist`` is set (from :func:`load_company_blacklist`), rows whose card ``company`` matches
    the blacklist are omitted.

    If ``skip_url_keys`` is set (from :func:`load_greenhouse_skip_url_keys`), rows whose normalized URL
    matches a prior ``applied`` / ``apply_opened`` application, assisted CSV, recent dismissal
    (``output/greenhouse_dismissed.csv``), etc., are omitted.
    """
    try:
        raw = driver.execute_script(
            """
            try {
              const out = [];
              const seen = new Set();

              function cardMeta(card) {
                let title = '';
                let company = '';
                if (!card) return { title, company };
                const titleEl = card.querySelector('h4.section-title');
                if (titleEl) {
                  title = (titleEl.innerText || titleEl.textContent || '').trim();
                  if (!title) title = (titleEl.getAttribute('title') || '').trim();
                }
                const col = titleEl ? titleEl.closest('.flex.flex-col') : null;
                if (col) {
                  for (const p of col.querySelectorAll('p.body')) {
                    if (p.classList.contains('body__secondary')) continue;
                    const t = (p.textContent || '').trim();
                    if (t && t !== title) { company = t; break; }
                  }
                }
                if (!company) {
                  const img = card.querySelector('img.company-logo__logo, .company-logo img[alt]');
                  if (img) company = (img.getAttribute('alt') || '').trim();
                }
                return { title, company };
              }

              function linkLabel(a) {
                const bits = [
                  a.textContent || '',
                  a.getAttribute('aria-label') || '',
                  a.getAttribute('title') || '',
                ];
                return bits.join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
              }

              function isJobBoardHref(href) {
                try {
                  const u = new URL(href, document.baseURI || location.href);
                  const host = (u.hostname || '').toLowerCase();
                  if (!host.includes('greenhouse.io')) return false;
                  const path = (u.pathname || '').toLowerCase();
                  if (path.includes('/jobs/')) return true;
                  if (u.searchParams.has('gh_jid')) return true;
                  return false;
                } catch (e) { return false; }
              }

              function pickJobUrlFromCard(card) {
                const ranked = [];
                for (const a of card.querySelectorAll('a[href]')) {
                  const h = (a.getAttribute('href') || '').trim();
                  if (!h || h.startsWith('#')) continue;
                  let abs;
                  try {
                    abs = new URL(h, document.baseURI || location.href).href;
                  } catch (e) { continue; }
                  if (!isJobBoardHref(abs)) continue;
                  const label = linkLabel(a);
                  let score = 0;
                  if (label.includes('view job')) score += 100;
                  else if (label.includes('view role')) score += 80;
                  else if (label.includes('view')) score += 40;
                  if (a.classList && a.classList.contains('btn')) score += 30;
                  if (abs.includes('/jobs/')) score += 10;
                  ranked.push({ url: abs, score });
                }
                ranked.sort((x, y) => y.score - x.score);
                return ranked.length ? ranked[0].url : '';
              }

              function pushRow(url, card) {
                if (!url || seen.has(url)) return;
                seen.add(url);
                const meta = cardMeta(card);
                out.push({
                  url: url,
                  title: meta.title || '',
                  company: meta.company || '',
                });
              }

              const cards = document.querySelectorAll('[data-provides="search-result"]');
              for (const card of cards) {
                const url = pickJobUrlFromCard(card);
                if (url) pushRow(url, card);
              }

              if (!out.length) {
                for (const a of document.querySelectorAll('a[href]')) {
                  const label = linkLabel(a);
                  if (!label.includes('view job')) continue;
                  const h = (a.getAttribute('href') || '').trim();
                  if (!h) continue;
                  let abs;
                  try {
                    abs = new URL(h, document.baseURI || location.href).href;
                  } catch (e) { continue; }
                  let card = a.closest('[data-provides="search-result"]');
                  if (!card) {
                    let n = a;
                    for (let depth = 0; depth < 10 && n; depth++) {
                      n = n.parentElement;
                      if (n && n.querySelector && n.querySelector('h4.section-title')) {
                        card = n;
                        break;
                      }
                    }
                  }
                  pushRow(abs, card);
                }
              }

              return out;
            } catch (e) { return []; }
            """
        )
    except Exception as e:
        log.warning("Could not collect View job listings from cards: %s", e)
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        u = (row.get("url") or "").strip()
        if not u:
            continue
        out.append(
            {
                "url": u,
                "title": (row.get("title") or "").strip(),
                "company": (row.get("company") or "").strip(),
            }
        )
    out = _filter_greenhouse_listings_by_company_blacklist(out, company_blacklist or [])
    if skip_url_keys:
        filtered: list[dict[str, str]] = []
        skipped = 0
        for row in out:
            key = normalize_greenhouse_job_url(row["url"])
            if key and key in skip_url_keys:
                skipped += 1
                continue
            filtered.append(row)
        if skipped:
            log.info(
                "Greenhouse job list: skipped %d listing(s) whose URL matches a prior application, assisted "
                "record, or recent dismissal (see %s).",
                skipped,
                GREENHOUSE_DISMISSED_CSV.as_posix(),
            )
        out = filtered
    return out


def collect_my_greenhouse_view_job_hrefs(
    driver: Any,
    *,
    skip_url_keys: frozenset[str] | None = None,
    company_blacklist: list[str] | None = None,
) -> list[str]:
    """
    Collect absolute **View job** URLs only (same cards as :func:`collect_my_greenhouse_view_job_listings`).
    Prefer the listings collector when you need company/title from the job board.
    """
    return [
        x["url"]
        for x in collect_my_greenhouse_view_job_listings(
            driver, skip_url_keys=skip_url_keys, company_blacklist=company_blacklist
        )
        if x.get("url")
    ]


def _coerce_greenhouse_job_board_entry(raw: Any) -> dict[str, str | None]:
    """
    Normalize a job-board row: legacy JSON list of URL strings, or dicts from
    :func:`collect_my_greenhouse_view_job_listings` with ``url``, ``company``, ``title``.
    """
    if isinstance(raw, str):
        u = raw.strip()
        return {"url": u, "company": None, "title": None} if u else {"url": "", "company": None, "title": None}
    if isinstance(raw, dict):
        u = (raw.get("url") or raw.get("href") or "").strip()
        c = (raw.get("company") or "").strip() or None
        t = (raw.get("title") or "").strip() or None
        return {"url": u, "company": c, "title": t}
    return {"url": "", "company": None, "title": None}


def _mygreenhouse_jobs_page_looks_cloudfront_blocked(driver: Any) -> bool:
    """
    True when the document looks like CloudFront's HTML 403 / "Request blocked" edge page
    (common when MyGreenhouse rate-limits or WAF-blocks scripted traffic).
    """
    try:
        src = (driver.page_source or "")
    except Exception:
        return False
    low = src[:120_000].lower()
    if "cloudfront" not in low:
        return False
    return (
        "403 error" in low
        or "request blocked" in low
        or "the request could not be satisfied" in low
        or "generated by cloudfront" in low
    )


def _wait_for_greenhouse_view_job_links(
    driver: Any,
    *,
    min_count: int = 1,
    max_seconds: float = 90.0,
    poll_s: float = 1.0,
) -> bool:
    """
    Poll until ``collect_my_greenhouse_view_job_listings`` reports at least ``min_count`` **View job** rows
    in the DOM (hydration / layout ready).

    Uses **no** ``skip_url_keys`` filter so we do not block when the first visible cards are only duplicates
    (applied / assisted / dismissed): the filtered count can stay at 0 until we scroll to load more rows.
    Callers should pass ``skip_url_keys`` only on the **post-scroll** collection pass.
    """
    deadline = time.monotonic() + max(5.0, float(max_seconds))
    poll_s = max(0.35, float(poll_s))
    min_count = max(1, int(min_count))
    last_n = 0
    _reveal_greenhouse_job_cards_for_collection(driver)
    while time.monotonic() < deadline:
        listings = collect_my_greenhouse_view_job_listings(driver, skip_url_keys=None)
        last_n = len(listings)
        if last_n >= min_count:
            log.info("MyGreenhouse /jobs: %d View job link(s) in DOM (ready to scroll / dedupe).", last_n)
            return True
        time.sleep(poll_s)
    log.warning(
        "MyGreenhouse /jobs: timed out after %.0fs with only %d View job link(s) in DOM — continuing to scroll/collect.",
        max_seconds,
        last_n,
    )
    return False


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


def _greenhouse_switch_default_content(driver: Any) -> None:
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


def _mygreenhouse_autofill_xpath() -> str:
    lo = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )
    return (
        f"//button[contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')] | "
        f"//a[contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')] | "
        f"//*[@role='button'][contains({lo}, 'autofill') and contains({lo}, 'mygreenhouse')]"
    )


def _attempt_mygreenhouse_autofill_click_in_document(driver: Any, *, timeout_s: float) -> bool:
    """
    Click **Autofill with MyGreenhouse** in the *current* browsing context only (no iframe descent).

    Tries clickable XPath, then presence + JS click (some boards block WebDriver "clickable"), then
    Greenhouse pill button classes, then a JS node scan.
    """
    timeout_s = max(1.5, float(timeout_s))
    combined_xpath = _mygreenhouse_autofill_xpath()
    wait = WebDriverWait(driver, timeout_s)
    try:
        el = wait.until(EC.element_to_be_clickable((By.XPATH, combined_xpath)))
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.12)
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
        el2 = WebDriverWait(driver, min(4.0, timeout_s)).until(
            EC.presence_of_element_located((By.XPATH, combined_xpath))
        )
        if el2.is_displayed():
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el2)
            time.sleep(0.12)
            driver.execute_script("arguments[0].click();", el2)
            log.info("Clicked Autofill with MyGreenhouse (presence + JS click).")
            time.sleep(1.5)
            return True
    except (TimeoutException, Exception) as e:
        log.debug("MyGreenhouse autofill presence click: %s", e)

    try:
        for sel in ("button.btn.btn--pill.btn--secondary", "button.btn--pill.btn--secondary"):
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if not btn.is_displayed():
                        continue
                    raw = (btn.text or btn.get_attribute("innerText") or "").lower()
                    raw = re.sub(r"\s+", " ", raw).strip()
                    compact = raw.replace(" ", "")
                    if "autofill" in raw and "mygreenhouse" in compact:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.1)
                        driver.execute_script("arguments[0].click();", btn)
                        log.info("Clicked Autofill with MyGreenhouse (pill button CSS match).")
                        time.sleep(1.5)
                        return True
                except Exception:
                    continue
    except Exception as e:
        log.debug("MyGreenhouse autofill CSS scan: %s", e)

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

    return False


def _attempt_mygreenhouse_autofill_in_iframes(driver: Any, *, timeout_s: float, depth: int = 0) -> bool:
    """
    Depth-first search for the autofill CTA in the current document and nested ``iframe`` / ``frame``.

    Hosted boards (e.g. company career sites) often embed ``boards.greenhouse.io`` in an iframe; the
    top document has no MyGreenhouse controls.
    """
    if depth > 14:
        return False
    sub_wait = float(timeout_s) if depth == 0 else min(8.0, max(2.0, float(timeout_s) * 0.35))
    if _attempt_mygreenhouse_autofill_click_in_document(driver, timeout_s=sub_wait):
        return True
    try:
        n = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
    except Exception:
        n = 0
    for i in range(n):
        try:
            driver.switch_to.frame(i)
        except Exception:
            continue
        try:
            if _attempt_mygreenhouse_autofill_in_iframes(driver, timeout_s=timeout_s, depth=depth + 1):
                return True
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                _greenhouse_switch_default_content(driver)
                return False
    return False


def _try_click_mygreenhouse_autofill(driver: Any, *, timeout_s: float = 28) -> bool:
    """
    On a Greenhouse job application page, click **Autofill with MyGreenhouse** once.

    Searches the top document and nested iframes (embed pattern). Waits for hydration where possible,
    then uses XPath, pill-button CSS, or a JS scan, with a JS click to reduce overlay intercept issues.
    """
    _greenhouse_switch_default_content(driver)
    if _attempt_mygreenhouse_autofill_in_iframes(driver, timeout_s=max(5.0, float(timeout_s))):
        return True
    _greenhouse_switch_default_content(driver)
    # Some listings have only a plain "Apply" control (no MyGreenhouse autofill entry point).
    return _try_click_apply_button_fallback(driver)


def navigate_greenhouse_listing(driver: Any, listing_url: str, *, index: int, total: int) -> bool:
    """Open one **View job** URL and wait briefly for the application shell to render."""
    u = (listing_url or "").strip()
    if not u:
        return False
    if not driver_session_alive(driver):
        return False
    log.info("Opening job listing %d/%d: %s", index, total, u)
    try:
        driver.get(u)
    except Exception as e:
        log.warning("Could not navigate to job URL: %s", e)
        return False
    if not driver_session_alive(driver):
        return False
    time.sleep(2.0)
    return True


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
        log.warning("Could not load resume for Greenhouse gates/helpers: %s", e)
        return None


def _assisted_greenhouse_job_publication_dict(entry: dict[str, str | None], job: dict[str, Any]) -> dict[str, Any]:
    """Row fields for ``output/assisted_applications.csv`` (same columns as ``applications.csv``)."""
    url = (entry.get("url") or "").strip()
    return {
        "id": job.get("id") or _greenhouse_listing_id(url),
        "title": (job.get("title") or entry.get("title") or "").strip(),
        "company": (job.get("company") or entry.get("company") or "").strip(),
        "url": url,
        "location": (job.get("location") or "") or "",
    }


def _append_assisted_greenhouse_application(job: dict[str, Any]) -> None:
    from datetime import datetime, timezone

    from .apply_sheets import applied_sheet_row, format_apply_date_mdy

    ASSISTED_GREENHOUSE_CSV.parent.mkdir(parents=True, exist_ok=True)
    iso = datetime.now(timezone.utc).isoformat()
    row = applied_sheet_row(job, format_apply_date_mdy(iso))
    with _GREENHOUSE_CSV_WRITE_LOCK:
        new_file = not ASSISTED_GREENHOUSE_CSV.is_file()
        with ASSISTED_GREENHOUSE_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(("", "company", "", "date", "url", "title"))
            w.writerow(row)
    log.info("Recorded assisted Greenhouse application to %s", ASSISTED_GREENHOUSE_CSV.resolve())


def _prompt_greenhouse_after_assisted_job(
    *,
    listing_num: int,
    total: int,
    remaining_after_current: int,
    company: str,
    title: str,
) -> str:
    """
    After the user finishes on the employer apply page:

    * ``next_applied`` — ``n`` (or ``y`` / ``next``): record to assisted CSV, then scan for the next listing.
    * ``next_skip`` — ``s`` (or ``skip``): do not record; still scan for the next gate-passing job.
    * ``next_dismiss`` — ``d`` (or ``dismiss``): like ``s`` but append URL + date to ``greenhouse_dismissed.csv`` (skipped in job search collection for 30 days).
    * ``stay`` — Enter alone or ``q``: stop the helper loop here.
    """
    first_next_listing_num = listing_num + 1
    if remaining_after_current <= 0:
        forward_hint = (
            "**No** further collected listings exist after this one in this run. If you choose **n** or **s**, "
            "the helper will not open more tabs (on **n** it still records, then exits the forward loop)."
        )
    else:
        forward_hint = (
            f"then open remaining collected URLs **in order** (listings **#{first_next_listing_num}–#{total}**, "
            f"**{remaining_after_current}** tab(s)) — **one browser navigation at a time** — until one passes "
            "education/experience/clearance gates. Each gate check can take **tens of seconds**; watch the browser."
        )
    tail = (
        f"  • Type 'n' then Enter if you submitted an application — record to {ASSISTED_GREENHOUSE_CSV.as_posix()}, "
        f"{forward_hint}\n"
        f"  • Type 's' then Enter to continue without recording (skipped or abandoned apply).\n"
        f"  • Type 'd' then Enter to dismiss — same as 's' but record URL + today's date to "
        f"{GREENHOUSE_DISMISSED_CSV.as_posix()} (that job is omitted from MyGreenhouse job-list collection for "
        f"{_DISMISS_RETENTION_DAYS} days).\n"
        f"  • Enter alone or 'q' to stop here.\n"
        "Your choice: "
    )
    try:
        raw = input(
            f"[Greenhouse helper] Listing {listing_num}/{total}: {title!r} at {company!r}\n"
            f"After applying on the employer site:\n{tail}"
        )
    except EOFError:
        return "stay"
    s = (raw or "").strip().lower()
    if not s:
        return "stay"
    # First word only, strip trailing punctuation (e.g. "n." or "n," → "n")
    token = s.split()[0].rstrip(".,;:!?")
    if token in ("q", "quit"):
        return "stay"
    if token in ("s", "skip"):
        return "next_skip"
    if token in ("d", "dismiss"):
        return "next_dismiss"
    if token in ("n", "y", "yes", "next", "applied"):
        return "next_applied"
    log.info("Unrecognized input %r — stopping (same as Enter).", (raw or "").strip()[:40])
    return "stay"


def _run_greenhouse_post_gate_automation(
    driver: Any,
    args: Any,
    listing_url: str,
    *,
    company_from_search_card: str | None = None,
    title_from_search_card: str | None = None,
    resume: dict[str, Any] | None = None,
) -> None:
    """Post–gate-pass helpers: MyGreenhouse autofill, cover letter upload, checkbox rules (not full auto-submit)."""
    if not _try_click_mygreenhouse_autofill(driver):
        log.warning(
            "Did not find a clickable Autofill with MyGreenhouse or Apply control on the page — "
            "complete start-apply manually if it appears after load."
        )
    maybe_upload_greenhouse_cover_letter(
        driver,
        args,
        listing_url,
        company_from_search_card=company_from_search_card,
        title_from_search_card=title_from_search_card,
        resume=resume,
    )
    maybe_apply_greenhouse_checkbox_rules(driver, args)


def _run_greenhouse_application_helper_concurrent(
    driver: Any,
    args: Any,
    ordered: list[dict[str, str | None]],
    total: int,
    resume: dict[str, Any],
    matcher: JobMatcher,
) -> bool:
    """
    Producer/consumer variant of the Greenhouse helper loop.

    A second **headless** Chrome (no window — listing pages are public) scans ``ordered`` against the gates
    and pushes each passing ``(index, entry, job)`` onto a small queue; the logged-in ``driver`` (visible
    unless ``--headless``) navigates to each queued job, presents/autofills it, and runs the same n/s/d/q
    prompt as the sequential path. While the queue is empty but the scanner is still running, the presenter
    logs a "Loading" message about every 10s.

    Returns ``True`` when the concurrent path ran (caller should return), or ``False`` when the scanner
    browser could not be started (caller should fall back to the single-driver sequential scan).
    """
    try:
        scanner = build_chrome(headless=True)
    except Exception as e:
        log.warning("Greenhouse prefetch: could not start scanner Chrome (%s).", e)
        return False
    log.info(
        "Greenhouse prefetch: scanner Chrome headless (presenter uses the logged-in window).",
    )

    work_q: queue.Queue = queue.Queue(maxsize=10)
    stop_event = threading.Event()

    def _producer() -> None:
        try:
            for k, entry in enumerate(ordered):
                if stop_event.is_set():
                    break
                if not driver_session_alive(scanner):
                    log.warning("Greenhouse prefetch: scanner session closed — stopping scan.")
                    break
                url = (entry.get("url") or "").strip()
                if not url:
                    continue
                log.info("Greenhouse prefetch: scanning listing %d/%d for gates …", k + 1, total)
                if not navigate_greenhouse_listing(scanner, url, index=k + 1, total=total):
                    if not driver_session_alive(scanner):
                        break
                    continue
                job = _scrape_greenhouse_job_for_cover_letter(
                    scanner,
                    url,
                    company_from_search_card=entry.get("company"),
                    title_from_search_card=entry.get("title"),
                )
                if not matcher.gates_pass(resume, job):
                    log.info(
                        "Greenhouse prefetch: gates failed for listing %d/%d (%s at %s).",
                        k + 1,
                        total,
                        job.get("title"),
                        job.get("company"),
                    )
                    print_job_fit_debug(
                        job.get("company"),
                        job.get("title"),
                        None,
                        note="greenhouse_gates_failed_scan",
                    )
                    _append_greenhouse_dismissed(url, source="gates_failed")
                    continue
                log.info(
                    "Greenhouse prefetch: gates passed for listing %d/%d (%s at %s) — queued for presentation.",
                    k + 1,
                    total,
                    job.get("title"),
                    job.get("company"),
                )
                # Block until the presenter makes room, but wake periodically so stop_event is honored.
                while not stop_event.is_set():
                    try:
                        work_q.put((k, entry, job), timeout=1.0)
                        break
                    except queue.Full:
                        continue
        except Exception as e:
            log.warning("Greenhouse prefetch: scanner thread error: %s", e)
        finally:
            try:
                scanner.quit()
            except Exception:
                pass
            work_q.put(None)  # sentinel: scanning complete

    producer = threading.Thread(target=_producer, name="greenhouse-scanner", daemon=True)
    producer.start()

    presented_any = False
    try:
        while True:
            if not driver_session_alive(driver):
                log_driver_session_closed()
                break
            try:
                item = work_q.get(timeout=10.0)
            except queue.Empty:
                if producer.is_alive():
                    log.info("Loading — still scanning for the next matching job …")
                    continue
                break  # scanner finished; sentinel already drained or none queued
            if item is None:
                if not presented_any:
                    log.warning(
                        "No collected listing passed education/experience gates (scanned %d). "
                        "Browser left on the last page reached for manual review.",
                        total,
                    )
                else:
                    log.info("Greenhouse helper: no more gate-passing listings — stopping.")
                break

            k, entry, job = item
            url = (entry.get("url") or "").strip()
            log.info(
                "Greenhouse helper: presenting listing %d/%d (%s at %s) — autofill / cover / checkbox.",
                k + 1,
                total,
                job.get("title"),
                job.get("company"),
            )
            if not navigate_greenhouse_listing(driver, url, index=k + 1, total=total):
                if not driver_session_alive(driver):
                    log_driver_session_closed()
                    break
                log.warning(
                    "Greenhouse helper: could not navigate presenter to listing %d/%d — skipping.",
                    k + 1,
                    total,
                )
                continue
            _run_greenhouse_post_gate_automation(
                driver,
                args,
                url,
                company_from_search_card=entry.get("company"),
                title_from_search_card=entry.get("title"),
                resume=resume,
            )
            presented_any = True

            pub = _assisted_greenhouse_job_publication_dict(entry, job)
            remaining_after_current = max(0, total - (k + 1))
            action = _prompt_greenhouse_after_assisted_job(
                listing_num=k + 1,
                total=total,
                remaining_after_current=remaining_after_current,
                company=pub.get("company") or "Company",
                title=pub.get("title") or "Role",
            )
            log.info("Greenhouse helper: post-job prompt returned action=%r", action)
            if action == "stay":
                log.info(
                    "Greenhouse helper: stopping — stay at this job (Enter / q / unrecognized / EOF on prompt)."
                )
                break
            if action == "next_applied":
                _append_assisted_greenhouse_application(pub)
                delete_cover_letter_for_job(
                    Path(getattr(args, "greenhouse_cover_letter_dir", GREENHOUSE_COVERLETTERS_DIR)),
                    site="greenhouse",
                    company=str(pub.get("company") or ""),
                    title=str(pub.get("title") or ""),
                    job_id=str(pub.get("id") or "job"),
                )
            elif action == "next_dismiss":
                _append_greenhouse_dismissed((pub.get("url") or url).strip(), source="terminal_d")
    finally:
        stop_event.set()
        # Unblock a producer that may be waiting on a full queue, then wait for it to quit the scanner.
        try:
            while True:
                work_q.get_nowait()
        except queue.Empty:
            pass
        producer.join(timeout=15.0)
    return True


def run_greenhouse_application_helper(driver: Any, args: Any, view_job_entries: list[Any]) -> None:
    """
    **Greenhouse application helper** (not an auto-submitter): walk collected **View job** rows in order,
    run the same **education / experience** gates as LinkedIn (:class:`JobMatcher`) on each page until one
    passes, then run MyGreenhouse autofill, cover letter upload, and ``checkbox_groups`` rules.

    ``view_job_entries`` is a list of URL strings (legacy) or dicts ``{"url", "company", "title"}`` from
    :func:`collect_my_greenhouse_view_job_listings` — company/title from the MyGreenhouse job card are used
    for gates and cover letters when the apply page omits them. Rows from companies in
    ``data/company_blacklist.json`` (``--company-blacklist``) and rows whose URL matches a prior Greenhouse
    ``applied`` / ``apply_opened`` record in ``data/applications.db`` are omitted (same URL normalization as
    when collecting).

    Listings that fail the gate are skipped (**also appended to** ``output/greenhouse_dismissed.csv`` **so they are
    omitted from MyGreenhouse job-list collection for 30 days**, same as terminal **d**); the browser moves to the
    next URL until a pass or the list ends.
    With ``--greenhouse-manual-next-listing`` (default on), after **each** successful autofill/cover/checkbox pass
    the terminal asks for **n** (submitted application — append a row to ``output/assisted_applications.csv`` in the
    same layout as ``applications.csv``), **s** (continue to the next gate-passing job without recording),
    **d** (dismiss — like **s** but also append URL + date to ``output/greenhouse_dismissed.csv`` for 30-day
    omission from job-list collection), or **Enter** / **q** to stop. Disable that loop with ``--no-greenhouse-manual-next-listing``.
    Use ``--greenhouse-gate-probe-max-listings`` to cap how many URLs are in the collected list for probing (0 =
    entire list).
    """
    skip_keys = load_greenhouse_skip_url_keys()
    company_blacklist = load_company_blacklist(getattr(args, "company_blacklist", None))
    seen: set[str] = set()
    ordered: list[dict[str, str | None]] = []
    skipped_dup = 0
    skipped_blacklist = 0
    for raw in view_job_entries:
        entry = _coerce_greenhouse_job_board_entry(raw)
        u = entry.get("url") or ""
        if not u or u in seen:
            continue
        if company_blacklist and is_company_blacklisted(entry.get("company") or "", company_blacklist):
            skipped_blacklist += 1
            continue
        if skip_keys:
            key = normalize_greenhouse_job_url(u)
            if key and key in skip_keys:
                skipped_dup += 1
                continue
        seen.add(u)
        ordered.append(entry)
    if skipped_blacklist:
        log.info(
            "Greenhouse helper: omitted %d listing(s) from blacklisted companies.",
            skipped_blacklist,
        )
    if skipped_dup:
        log.info(
            "Greenhouse helper: omitted %d listing(s) whose URL matches a prior application, assisted record, "
            "or recent dismissal (see %s and %s).",
            skipped_dup,
            DEFAULT_APPLICATIONS_DB,
            GREENHOUSE_DISMISSED_CSV.as_posix(),
        )
    if not ordered:
        log.info("No View job links — skipping Greenhouse application helper.")
        return

    log.info(
        "Greenhouse application helper: %d collected View job row(s); gate probe + autofill/cover/checkbox per job.",
        len(ordered),
    )

    cap = int(getattr(args, "greenhouse_gate_probe_max_listings", 0) or 0)
    if cap > 0 and len(ordered) > cap:
        log.info(
            "Greenhouse gate probe: trying first %d of %d collected listing(s) (--greenhouse-gate-probe-max-listings).",
            cap,
            len(ordered),
        )
        ordered = ordered[:cap]

    try:
        configure_dspy()
    except EnvironmentError as e:
        log.warning(
            "DSPy not fully configured (%s) — gate extraction may fall back to regex heuristics.",
            e,
        )
    resume = _load_resume_for_greenhouse(args)
    if resume is None:
        log.warning("Skipping Greenhouse application helper (no resume profile).")
        return

    matcher = JobMatcher()
    total = len(ordered)

    # Concurrent prefetch: a headless scanner Chrome queues gate-passing jobs while the logged-in driver
    # (visible unless --headless) presents/autofills them.
    # Only meaningful when the manual next-listing prompt loop is on (otherwise we present just one job).
    if bool(getattr(args, "greenhouse_prefetch", True)) and bool(
        getattr(args, "greenhouse_manual_next_listing", True)
    ):
        if _run_greenhouse_application_helper_concurrent(driver, args, ordered, total, resume, matcher):
            return
        log.info("Greenhouse prefetch unavailable — falling back to single-driver sequential scan.")

    chosen_index = -1
    gate_pass_job: dict[str, Any] | None = None
    for i, entry in enumerate(ordered, start=1):
        if not driver_session_alive(driver):
            log_driver_session_closed()
            return
        url = entry["url"] or ""
        if not url:
            continue
        if not navigate_greenhouse_listing(driver, url, index=i, total=total):
            if not driver_session_alive(driver):
                log_driver_session_closed()
                return
            continue
        job = _scrape_greenhouse_job_for_cover_letter(
            driver,
            url,
            company_from_search_card=entry.get("company"),
            title_from_search_card=entry.get("title"),
        )
        if matcher.gates_pass(resume, job):
            chosen_index = i - 1
            gate_pass_job = job
            log.info(
                "Greenhouse helper: gates passed — running autofill / cover / checkbox for listing %d/%d: %s at %s",
                i,
                total,
                job.get("title"),
                job.get("company"),
            )
            break
        log.info(
            "Greenhouse gates failed for listing %d/%d — trying next: %s at %s",
            i,
            total,
            job.get("title"),
            job.get("company"),
        )
        print_job_fit_debug(
            job.get("company"),
            job.get("title"),
            None,
            note="greenhouse_gates_failed",
        )
        _append_greenhouse_dismissed(url, source="gates_failed")

    if chosen_index < 0 or gate_pass_job is None:
        log.warning(
            "No collected listing passed education/experience gates (tried %d). "
            "Browser left on the last page reached for manual review.",
            total,
        )
        return

    chosen_entry = ordered[chosen_index]
    chosen_url = (chosen_entry.get("url") or "").strip()
    _run_greenhouse_post_gate_automation(
        driver,
        args,
        chosen_url,
        company_from_search_card=chosen_entry.get("company"),
        title_from_search_card=chosen_entry.get("title"),
        resume=resume,
    )

    if not bool(getattr(args, "greenhouse_manual_next_listing", True)):
        return

    j = chosen_index
    current_entry = chosen_entry
    current_job = gate_pass_job

    while True:
        if not driver_session_alive(driver):
            log_driver_session_closed()
            break

        pub = _assisted_greenhouse_job_publication_dict(current_entry, current_job)
        remaining_after_current = max(0, total - (j + 1))
        action = _prompt_greenhouse_after_assisted_job(
            listing_num=j + 1,
            total=total,
            remaining_after_current=remaining_after_current,
            company=pub.get("company") or "Company",
            title=pub.get("title") or "Role",
        )
        log.info("Greenhouse helper: post-job prompt returned action=%r", action)
        if action == "stay":
            log.info(
                "Greenhouse helper: stopping — stay at this job (Enter / q / unrecognized / EOF on prompt)."
            )
            break
        if action == "next_applied":
            _append_assisted_greenhouse_application(pub)
            delete_cover_letter_for_job(
                Path(getattr(args, "greenhouse_cover_letter_dir", GREENHOUSE_COVERLETTERS_DIR)),
                site="greenhouse",
                company=str(pub.get("company") or ""),
                title=str(pub.get("title") or ""),
                job_id=str(pub.get("id") or "job"),
            )
        elif action == "next_dismiss":
            _append_greenhouse_dismissed(
                (pub.get("url") or current_entry.get("url") or "").strip(),
                source="terminal_d",
            )

        found_k: int | None = None
        found_entry: dict[str, str | None] | None = None
        found_job: dict[str, Any] | None = None
        scan_range = range(j + 1, len(ordered))
        n_to_try = sum(1 for k in scan_range if (ordered[k].get("url") or "").strip())
        if n_to_try == 0:
            log.info(
                "Greenhouse helper: no further listing URLs after index %d — skipping forward navigation.",
                j,
            )
        else:
            log.info(
                "Greenhouse helper: forward gate scan will try up to %d listing(s) "
                "(listings #%d–#%d in this run); "
                "expect one `driver.get` per row and several seconds of gate work each.",
                n_to_try,
                j + 2,
                total,
            )
        for k in scan_range:
            if not driver_session_alive(driver):
                log_driver_session_closed()
                return
            entry = ordered[k]
            url = entry["url"] or ""
            if not url:
                continue
            log.info(
                "Greenhouse helper: gate scan navigating to listing %d/%d (forward pass) …",
                k + 1,
                total,
            )
            if not navigate_greenhouse_listing(driver, url, index=k + 1, total=total):
                if not driver_session_alive(driver):
                    log_driver_session_closed()
                    return
                continue
            job = _scrape_greenhouse_job_for_cover_letter(
                driver,
                url,
                company_from_search_card=entry.get("company"),
                title_from_search_card=entry.get("title"),
            )
            if not matcher.gates_pass(resume, job):
                log.info(
                    "Greenhouse helper: gates failed for listing %d/%d (%s at %s) — continuing scan for "
                    "next valid job.",
                    k + 1,
                    total,
                    job.get("title"),
                    job.get("company"),
                )
                print_job_fit_debug(
                    job.get("company"),
                    job.get("title"),
                    None,
                    note="greenhouse_gates_failed_scan",
                )
                _append_greenhouse_dismissed(url, source="gates_failed")
                continue
            found_k = k
            found_entry = entry
            found_job = job
            break

        if found_k is None or found_entry is None or found_job is None:
            log.info(
                "Greenhouse helper: stopping forward scan after action=%r — no listing passed gates in "
                "positions %d–%d of %d (list exhausted, or remaining postings failed education / experience / "
                "clearance gates). Browser left on the last page opened.",
                action,
                j + 2,
                total,
                total,
            )
            break

        log.info(
            "Greenhouse helper: gates passed for listing %d/%d (%s at %s) — running autofill / cover / checkbox.",
            found_k + 1,
            total,
            found_job.get("title"),
            found_job.get("company"),
        )
        next_url = (found_entry.get("url") or "").strip()
        _run_greenhouse_post_gate_automation(
            driver,
            args,
            next_url,
            company_from_search_card=found_entry.get("company"),
            title_from_search_card=found_entry.get("title"),
            resume=resume,
        )
        j = found_k
        current_entry = found_entry
        current_job = found_job


# Backwards-compatible name for older scripts or docs.
run_greenhouse_first_listing_if_gates_pass = run_greenhouse_application_helper


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


def _scrape_greenhouse_job_for_cover_letter(
    driver: Any,
    listing_url: str,
    *,
    company_from_search_card: str | None = None,
    title_from_search_card: str | None = None,
) -> dict[str, Any]:
    """
    Best-effort job title / company / description for cover letter and gates.

    Prefer ``company`` / ``title`` from the MyGreenhouse **search card** when provided — employer apply pages
    often omit company text or only show a logo.

    Description text is taken from the **richest** block on the page (including ``.job__description`` and
    embedded Greenhouse iframes). A single ``querySelector('#content, …')`` often hits a small region and
    drops qualification text, which makes experience gates see ``unspecified`` incorrectly.
    """
    try:
        raw = _harvest_greenhouse_job_from_open_tabs(driver)
    except Exception:
        raw = {"title": "", "company": "", "description": ""}
    title = (title_from_search_card or "").strip() or (raw.get("title") or "").strip() or "Role"
    company = (company_from_search_card or "").strip() or (raw.get("company") or "").strip() or "Company"
    description = (raw.get("description") or "").strip()
    if not description:
        description = f"Job listing: {listing_url}"
    jid = _greenhouse_listing_id(listing_url)
    return {"id": jid, "title": title, "company": company, "description": description}


def _click_greenhouse_attach_for_cover(root: Any, driver: Any) -> bool:
    """Click an **Attach** control inside the cover-letter file-upload region (JS click; pill-style CTAs)."""
    candidates: list[Any] = []
    try:
        candidates.extend(root.find_elements(By.XPATH, ".//button[normalize-space()='Attach']"))
    except Exception:
        pass
    try:
        for b in root.find_elements(By.CSS_SELECTOR, "button.btn--pill, button.btn.btn--pill"):
            if b not in candidates:
                candidates.append(b)
    except Exception:
        pass
    for btn in candidates:
        try:
            if not btn.is_displayed():
                continue
            label = re.sub(r"\s+", " ", (btn.text or "").strip()).lower()
            if label != "attach":
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.1)
            driver.execute_script("arguments[0].click();", btn)
            return True
        except Exception:
            continue
    return False


def _probe_greenhouse_cover_in_document(driver: Any, *, timeout_s: float) -> bool:
    """
    True if this browsing context has Greenhouse's standard cover-letter upload UI
    (``upload-label-cover_letter`` / ``input#cover_letter`` / wrapper ``div.file-upload``).
    """
    total = max(0.5, float(timeout_s))
    per = max(0.35, total / 3.0)
    for by, sel in (
        (By.ID, "upload-label-cover_letter"),
        (By.CSS_SELECTOR, 'input#cover_letter[type="file"]'),
        (
            By.XPATH,
            "//div[contains(@class,'file-upload')][.//label[@id='upload-label-cover_letter']]",
        ),
    ):
        try:
            el = WebDriverWait(driver, per).until(EC.presence_of_element_located((by, sel)))
            if el is not None:
                return True
        except (TimeoutException, NoSuchElementException):
            continue
        except Exception:
            continue
    return False


def _probe_greenhouse_cover_in_iframes_recursive(driver: Any, *, timeout_s: float, depth: int) -> bool:
    if depth > 14:
        return False
    sub_wait = float(timeout_s) if depth == 0 else min(3.0, max(0.6, float(timeout_s) * 0.35))
    if _probe_greenhouse_cover_in_document(driver, timeout_s=sub_wait):
        return True
    try:
        n = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
    except Exception:
        n = 0
    for i in range(n):
        try:
            driver.switch_to.frame(i)
        except Exception:
            continue
        try:
            if _probe_greenhouse_cover_in_iframes_recursive(driver, timeout_s=timeout_s, depth=depth + 1):
                return True
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                _greenhouse_switch_default_content(driver)
                return False
    return False


def _greenhouse_cover_letter_widget_available(driver: Any, *, timeout_s: float = 5.0) -> bool:
    """
    Whether the apply flow exposes a Greenhouse cover-letter upload (same discovery order as upload:
    current context first, then ``default_content`` + nested iframes).
    """
    tmo = max(1.0, float(timeout_s))
    if _probe_greenhouse_cover_in_document(driver, timeout_s=tmo):
        return True
    log.debug("Greenhouse cover letter control not in current document — probing from top through iframes.")
    _greenhouse_switch_default_content(driver)
    return _probe_greenhouse_cover_in_iframes_recursive(driver, timeout_s=tmo, depth=0)


def _try_upload_greenhouse_cover_in_document(driver: Any, docx_path: Path, *, timeout_s: float) -> bool:
    """
    Upload a DOCX to Greenhouse's **Cover Letter** widget in the *current* browsing context only.

    ``input#cover_letter`` inside ``div.file-upload``; if ``send_keys`` fails, click **Attach** then retry.
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
        if _click_greenhouse_attach_for_cover(root, driver):
            time.sleep(0.45)
        if _send_to_input():
            log.info("Uploaded cover letter DOCX after Attach: %s", ap)
            return True
    except Exception as e:
        log.warning("Greenhouse cover letter upload failed: %s", e)
    return False


def _upload_greenhouse_cover_in_iframes_recursive(
    driver: Any, docx_path: Path, *, timeout_s: float, depth: int = 0
) -> bool:
    """Depth-first: cover-letter file-upload in this document or nested ``iframe`` / ``frame``."""
    if depth > 14:
        return False
    sub_wait = float(timeout_s) if depth == 0 else min(12.0, max(3.0, float(timeout_s) * 0.35))
    if _try_upload_greenhouse_cover_in_document(driver, docx_path, timeout_s=sub_wait):
        return True
    try:
        n = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
    except Exception:
        n = 0
    for i in range(n):
        try:
            driver.switch_to.frame(i)
        except Exception:
            continue
        try:
            if _upload_greenhouse_cover_in_iframes_recursive(driver, docx_path, timeout_s=timeout_s, depth=depth + 1):
                return True
        finally:
            try:
                driver.switch_to.parent_frame()
            except Exception:
                _greenhouse_switch_default_content(driver)
                return False
    return False


def _upload_file_to_greenhouse_cover_letter_input(driver: Any, docx_path: Path, *, timeout_s: float = 22) -> bool:
    """
    Upload a DOCX to Greenhouse's **Cover Letter** widget (``input#cover_letter`` inside ``div.file-upload``).

    Tries the **current** document first (after autofill you may already be inside the board iframe), then
    ``default_content`` and a depth-first iframe search — same embed pattern as **Autofill with MyGreenhouse**.
    """
    tmo = max(5.0, float(timeout_s))
    if _try_upload_greenhouse_cover_in_document(driver, docx_path, timeout_s=tmo):
        return True
    log.debug("Greenhouse cover letter region not in current document — searching from top through iframes.")
    _greenhouse_switch_default_content(driver)
    if _upload_greenhouse_cover_in_iframes_recursive(driver, docx_path, timeout_s=tmo, depth=0):
        return True
    log.warning("Greenhouse cover letter upload region (upload-label-cover_letter) not found.")
    return False


def maybe_upload_greenhouse_cover_letter(
    driver: Any,
    args: Any,
    listing_url: str,
    *,
    company_from_search_card: str | None = None,
    title_from_search_card: str | None = None,
    resume: dict[str, Any] | None = None,
) -> None:
    """
    On the current Greenhouse application page: if a standard cover-letter upload control exists, generate
    a cover letter (same generator as LinkedIn) and attach the DOCX. Skips generation when no such control
    is found (optional-field listings, wrong embed, etc.).
    """
    listing = (listing_url or "").strip()
    if not listing:
        log.debug("Skipping Greenhouse cover letter: empty listing URL.")
        return

    if not _greenhouse_cover_letter_widget_available(driver, timeout_s=5.0):
        log.info("No Greenhouse cover letter upload on this page — skipping cover letter generation.")
        return

    if resume is not None:
        resume_dict = resume
    else:
        cache = Path(getattr(args, "resume_cache", DEFAULT_RESUME_CACHE_PATH))
        resume_pdf = Path(getattr(args, "resume", DEFAULT_RESUME_FILE))
        try:
            resume_dict = load_or_build_resume(
                resume_pdf if resume_pdf.is_file() else None,
                cache,
                force_reparse=bool(getattr(args, "force_resume_parse", False)),
            )
        except Exception as e:
            log.warning("Skipping Greenhouse cover letter: could not load resume (%s).", e)
            return

    try:
        job = _scrape_greenhouse_job_for_cover_letter(
            driver,
            listing,
            company_from_search_card=company_from_search_card,
            title_from_search_card=title_from_search_card,
        )
        log.info(
            "Generating Greenhouse cover letter for %r at %r …",
            job.get("title") or "",
            job.get("company") or "",
        )
        cover_text = CoverLetterGenerator().generate(resume_dict, job)
    except Exception as e:
        log.warning("Skipping Greenhouse cover letter: generation failed: %s", e)
        return
    if not (cover_text or "").strip():
        log.warning("Generated cover letter is empty — skipping Greenhouse upload.")
        return
    docx_dir = Path(getattr(args, "greenhouse_cover_letter_dir", GREENHOUSE_COVERLETTERS_DIR))
    out_file = cover_letter_docx_path_unique(
        docx_dir,
        site="greenhouse",
        company=str(job.get("company") or ""),
        title=str(job.get("title") or ""),
        job_id=str(job.get("id") or "job"),
    )
    try:
        write_cover_letter_docx(cover_text, out_file)
    except Exception as e:
        log.warning("Could not write Greenhouse cover letter DOCX: %s", e)
        return
    time.sleep(0.8)
    if _upload_file_to_greenhouse_cover_letter_input(driver, out_file):
        log.info("Greenhouse cover letter attached: %s", out_file.resolve())
    else:
        log.warning(
            "Greenhouse cover letter DOCX written to %s but upload control was not found on the page.",
            out_file.resolve(),
        )


def _greenhouse_fieldset_legend_text(fs: Any) -> str:
    try:
        leg = fs.find_element(By.CSS_SELECTOR, "legend")
        return (leg.text or "").strip()
    except NoSuchElementException:
        return (fs.text or "")[:500].strip()


def _click_checkbox_in_fieldset_by_label(driver: Any, fs: Any, choose_label: str) -> bool:
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
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", lab)
            except Exception:
                pass
            time.sleep(0.06)
            try:
                focus_element(driver, lab, pause=0.18)
            except Exception:
                pass
            try:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true,view:window}));
                    el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true,cancelable:true,view:window}));
                    el.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window}));
                    """,
                    lab,
                )
            except Exception:
                pass
            try:
                lab.click()
            except Exception:
                try:
                    inp.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", inp)
            time.sleep(0.25)
            log.info('Checkbox group: selected option %r (from form fill rules).', choose_label)
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
    For each ``fieldset.checkbox``, if the form fill rules (``output/form_fill_rules/`` or ``--form-fill-rules``)
    match the legend under ``checkbox_groups``, select ``choose_label`` for that rule.
    """
    rules_path = Path(args.form_fill_rules) if getattr(args, "form_fill_rules", None) else DEFAULT_RULES_PATH
    try:
        engine = FormFillRulesEngine(rules_path, apply_source="greenhouse")
    except Exception as e:
        log.warning("Form fill rules could not be loaded for Greenhouse (%s).", e)
        return False
    if not engine.has_checkbox_groups():
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
            if _click_checkbox_in_fieldset_by_label(driver, fs, want):
                any_applied = True
        except Exception as e:
            log.debug("Greenhouse checkbox fieldset: %s", e)
            continue
    return any_applied


def _save_greenhouse_session_cookies(driver: Any, path: Path, note: str) -> None:
    """
    Open ``my.greenhouse.io`` in the active tab, then persist cookies.

    Selenium's ``get_cookies()`` is scoped to the current document; after visiting a job-board host we must
    navigate here so the file includes the MyGreenhouse candidate session. Call this **after** any manual
    review pause if the user should stay on the application page until then.
    """
    try:
        driver.get(f"{MY_GREENHOUSE_ORIGIN}/")
        time.sleep(0.4)
        save_cookies(driver, path)
        log.info("Saved Greenhouse cookies (%s): %s", note, path.resolve())
    except Exception as e:
        log.warning("Could not save Greenhouse cookies (%s): %s", note, e)


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
    """
    path = Path(args.greenhouse_cookies)
    max_wait = float(getattr(args, "greenhouse_login_max_seconds", 600.0))
    prompt_before_close = bool(getattr(args, "greenhouse_prompt_before_close", True))
    driver = None
    try:
        driver = build_chrome(headless=args.headless)
        load_greenhouse_cookies(driver, path)

        log.info("Opening MyGreenhouse sign-in (candidates): %s", GREENHOUSE_SIGN_IN_URL)
        _open_my_greenhouse_sign_in(driver)
        log.info(
            "Polling up to %.0fs for a cookie-based redirect to the dashboard …",
            GREENHOUSE_SIGN_IN_POST_NAV_DELAY_S,
        )
        if _wait_for_my_greenhouse_dashboard(
            driver,
            max_seconds=GREENHOUSE_SIGN_IN_POST_NAV_DELAY_S,
            poll_s=GREENHOUSE_SIGN_IN_REDIRECT_POLL_S,
        ):
            log.info("Already on dashboard (session from cookies).")
            _save_greenhouse_session_cookies(driver, path, "dashboard already active")
        else:
            cache_path = Path(getattr(args, "resume_cache", DEFAULT_RESUME_CACHE_PATH))
            profile_email = _read_resume_profile_email(cache_path)
            if profile_email:
                _try_my_greenhouse_email_sign_in(driver, profile_email)
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
                _save_greenhouse_session_cookies(driver, path, "timeout waiting for dashboard")
                return
            _save_greenhouse_session_cookies(driver, path, "dashboard reached after sign-in")

        ready_max = float(getattr(args, "greenhouse_jobs_ready_max_seconds", 90.0))
        skip_keys = load_greenhouse_skip_url_keys()
        company_blacklist = load_company_blacklist(getattr(args, "company_blacklist", None))
        if company_blacklist:
            log.info(
                "Company blacklist active: %d entr%s",
                len(company_blacklist),
                "y" if len(company_blacklist) == 1 else "ies",
            )
        if skip_keys:
            log.info(
                "Greenhouse dedupe: %d distinct Greenhouse job URL(s) already in %s (applied / apply_opened).",
                len(skip_keys),
                DEFAULT_APPLICATIONS_DB,
            )

        raw_kw = getattr(args, "keywords", None) or []
        kw_list = [str(k).strip() for k in raw_kw if str(k).strip()]
        if not kw_list:
            kw_list = list(DEFAULT_JOB_SEARCH_KEYWORDS)
            log.info(
                "No keywords on args — using default search list (%d): %s",
                len(kw_list),
                "; ".join(repr(k) for k in kw_list),
            )

        view_job_listings: list[dict[str, str]] = []
        seen_norm_urls: set[str] = set()
        for ki, phrase in enumerate(kw_list):
            if not driver_session_alive(driver):
                log_driver_session_closed()
                break
            jobs_url = my_greenhouse_jobs_search_url(args, query=phrase)
            label = phrase if phrase else "(no query)"
            log.info(
                "MyGreenhouse job search %d/%d — %r → %s",
                ki + 1,
                len(kw_list),
                label,
                jobs_url,
            )
            # CloudFront/WAF sometimes returns an HTML 403 block page; wait and re-navigate a few times.
            _CLOUDFRONT_BLOCK_MAX_ATTEMPTS = 6  # 1 initial load + 5 retries (10s wait between each)
            loaded_ok = False
            for block_try in range(_CLOUDFRONT_BLOCK_MAX_ATTEMPTS):
                driver.get(jobs_url)
                time.sleep(1.5)
                if not _mygreenhouse_jobs_page_looks_cloudfront_blocked(driver):
                    loaded_ok = True
                    break
                log.warning(
                    "MyGreenhouse jobs page looks like a CloudFront 403/block for %r (load %d/%d).",
                    label,
                    block_try + 1,
                    _CLOUDFRONT_BLOCK_MAX_ATTEMPTS,
                )
                if block_try + 1 >= _CLOUDFRONT_BLOCK_MAX_ATTEMPTS:
                    break
                log.info("Waiting 10s before retrying the same search URL …")
                time.sleep(10.0)
            if not loaded_ok:
                log.warning(
                    "Skipping keyword %r — still blocked after %d load attempt(s); trying next keyword.",
                    label,
                    _CLOUDFRONT_BLOCK_MAX_ATTEMPTS,
                )
                continue
            time.sleep(0.5)
            _wait_for_greenhouse_view_job_links(driver, max_seconds=ready_max)
            _scroll_greenhouse_jobs_to_load_more(
                driver,
                max_rounds=int(getattr(args, "greenhouse_scroll_max_rounds", 50)),
                pause_s=float(getattr(args, "greenhouse_scroll_pause", 1.2)),
            )
            _reveal_greenhouse_job_cards_for_collection(driver)
            batch = collect_my_greenhouse_view_job_listings(
                driver, skip_url_keys=skip_keys, company_blacklist=company_blacklist
            )
            card_count, _ = _greenhouse_scroll_metrics(driver)
            if card_count > 0 and not batch:
                log.info(
                    "Greenhouse: %d job card(s) in DOM but 0 URL(s) collected — retrying after "
                    "scrolling each card into view.",
                    card_count,
                )
                _reveal_greenhouse_job_cards_for_collection(driver)
                time.sleep(0.5)
                batch = collect_my_greenhouse_view_job_listings(
                    driver, skip_url_keys=skip_keys, company_blacklist=company_blacklist
                )
            if card_count > 0 and not batch:
                log.warning(
                    "Greenhouse: %d search-result card(s) visible but no job-board URLs found. "
                    "The MyGreenhouse list layout may have changed, or cards are empty placeholders.",
                    card_count,
                )
            added = 0
            for row in batch:
                u = (row.get("url") or "").strip()
                key = normalize_greenhouse_job_url(u) or u
                if not key or key in seen_norm_urls:
                    continue
                seen_norm_urls.add(key)
                view_job_listings.append(row)
                added += 1
            log.info(
                "After %r: %d row(s) on page, %d new merged (total distinct: %d).",
                label,
                len(batch),
                added,
                len(view_job_listings),
            )

        log.info(
            "Collected %d distinct View job row(s) across %d keyword search(es) (URL + company/title from cards).",
            len(view_job_listings),
            len(kw_list),
        )
        out_path = Path("output/greenhouse_view_job_links.json")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(view_job_listings, indent=2), encoding="utf-8")
            log.info("Wrote job listings to %s", out_path.resolve())
        except OSError as e:
            log.warning("Could not write %s: %s", out_path, e)
        if driver_session_alive(driver):
            run_greenhouse_application_helper(driver, args, view_job_listings)
        elif view_job_listings:
            log_driver_session_closed()
        # When prompting for manual review, defer cookie snapshot until after Enter so we do not navigate
        # away from the application tab first (get_cookies is document-scoped; saving still needs my.greenhouse.io).
        if not prompt_before_close:
            _save_greenhouse_session_cookies(driver, path, "end of run after first-job helpers")
            log.info("Greenhouse session saved (%s).", path.resolve())
    finally:
        if driver is not None:
            if prompt_before_close and driver_session_alive(driver):
                log.info(
                    "Leaving the browser open — inspect the page, then press Enter in this terminal to quit Chrome."
                )
                _pause_until_user_closes_browser()
                _save_greenhouse_session_cookies(driver, path, "after manual review, before quit")
                log.info("Greenhouse session saved (%s).", path.resolve())
            try:
                driver.quit()
            except Exception:
                pass
