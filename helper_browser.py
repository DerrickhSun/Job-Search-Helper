"""
Manual apply helper: one Chrome session on the job search page. The user clicks jobs and applies;
the bot polls the Easy Apply modal and fills fields it recognizes. Terminal commands record applies.

When LinkedIn opens an external site in a **new tab** (common for off-site apply), we detect that tab,
record the job from the LinkedIn detail pane (status apply_opened), append a JSONL event, and save a
cover letter .docx under your Downloads folder (or --helper-downloads-dir).

Commands (type in this terminal, then Enter):
  r / record — log the current job detail as \"applied\" (same tracker + Sheets as auto mode)
  q / quit  — save cookies, export CSV, close Chrome
"""

from __future__ import annotations

import json
import logging
import queue
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apply_sheets import append_applied_job_row
from chrome_driver import DEFAULT_COOKIE_PATH, build_chrome, load_cookies, save_cookies
from company_blacklist import is_company_blacklisted, load_company_blacklist
from cover_letter import CoverLetterGenerator, write_cover_letter_docx
from form_filler import EasyApplyFiller
from job_searcher import JobSearcher
from matcher import JobMatcher, print_job_fit_debug
from tracker import ApplicationTracker

log = logging.getLogger(__name__)

# Host/path hints for off-LinkedIn apply flows (new tab after Apply).
_EXTERNAL_APPLY_URL_FRAGMENTS: tuple[str, ...] = (
    "myworkdayjobs.com",
    "myworkday.com",
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "smartrecruiters.com",
    "icims.com",
    "ashbyhq.com",
    "breezy.hr",
    "recruitee.com",
    "workable.com",
    "jobvite.com",
    "taleo.net",
    "oraclecloud.com",
    "successfactors",
    "ultipro",
    "eightfold.ai",
    "applytojob.com",
    "adp.com",
)

_HELPER_APPLY_EVENTS_JSONL = Path("data/helper_external_apply_events.jsonl")


def _safe_filename_component(s: str, max_len: int = 100) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (s or "").strip())
    s = re.sub(r"\s+", " ", s)
    return (s[:max_len] or "unknown").rstrip(" .")


def _cover_docx_path(downloads_dir: Path, company: str, title: str) -> Path:
    c = _safe_filename_component(company or "Company", 70)
    t = _safe_filename_component(title or "Position", 90)
    base = f"{c} - {t}.docx"
    path = downloads_dir / base
    if not path.exists():
        return path
    n = 2
    while True:
        cand = downloads_dir / f"{c} - {t} ({n}).docx"
        if not cand.exists():
            return cand
        n += 1


def _url_looks_like_external_job_apply(url: str) -> bool:
    u = (url or "").strip()
    if not u.lower().startswith("http"):
        return False
    low = u.lower()
    host = urllib.parse.urlparse(u).netloc.lower()
    if "linkedin.com" in host:
        return False
    for frag in _EXTERNAL_APPLY_URL_FRAGMENTS:
        if frag in low or frag in host:
            return True
    if any(x in low for x in ("/apply", "/jobapplication", "/careers/job", "/jobs/")):
        return True
    return False


def _resolve_helper_downloads_dir(args: Any) -> Path:
    raw = getattr(args, "helper_downloads_dir", None)
    p = Path(raw).expanduser() if raw else Path.home() / "Downloads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _append_helper_apply_event(record: dict) -> None:
    _HELPER_APPLY_EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with _HELPER_APPLY_EVENTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _switch_to_window_if_present(driver: Any, handle: str | None) -> bool:
    if not handle:
        return False
    try:
        if handle in driver.window_handles:
            driver.switch_to.window(handle)
            return True
    except Exception:
        pass
    return False


def _restore_tab_after_linkedin_work(
    driver: Any, restore_to: str | None, linkedin_fallback: str | None
) -> None:
    """Prefer returning the user to the tab they had focused; else LinkedIn jobs tab."""
    if _switch_to_window_if_present(driver, restore_to):
        return
    _switch_to_window_if_present(driver, linkedin_fallback)


def _detect_external_apply_new_tabs(
    driver: Any,
    searcher: JobSearcher,
    *,
    linkedin_jobs_tab: str | None,
    handles_seen: set[str],
    resume: dict,
    cover_gen: CoverLetterGenerator,
    cover_cache: dict[str, str],
    matcher: JobMatcher,
    tracker: ApplicationTracker,
    downloads_dir: Path,
    restore_to: str | None = None,
    company_blacklist: list[str] | None = None,
) -> set[str]:
    """
    If new window handles appeared and any looks like an external ATS apply URL, record the current
    LinkedIn job (detail pane), log apply_opened, write cover .docx to downloads_dir, append JSONL.
    Restores ``restore_to`` (the tab the user had selected before this ran) when possible.
    """
    try:
        cur = set(driver.window_handles)
    except Exception:
        return handles_seen
    new_handles = cur - handles_seen
    if not new_handles or not linkedin_jobs_tab:
        return cur

    bl = company_blacklist or []

    for nh in new_handles:
        ext_url = ""
        try:
            driver.switch_to.window(nh)
            ext_url = (driver.current_url or "").strip()
        except Exception:
            continue
        if not _url_looks_like_external_job_apply(ext_url):
            continue

        try:
            if linkedin_jobs_tab in driver.window_handles:
                driver.switch_to.window(linkedin_jobs_tab)
        except Exception:
            continue

        job = searcher.parse_current_job_from_detail_pane(driver)
        if not job:
            log.debug("External apply tab detected but could not parse LinkedIn job (detail pane).")
            continue

        if is_company_blacklisted(job.get("company") or "", bl):
            log.info("Skip external apply record (company blacklisted): %s", job.get("company"))
            continue

        jid = str(job.get("id") or "")
        st = tracker.last_status_for_job(jid)
        if st == "applied":
            log.debug("Skip apply_opened record: already applied id=%s", jid)
            continue
        if st == "apply_opened":
            log.debug("Skip apply_opened record: already recorded id=%s", jid)
            continue

        score = matcher.score(resume, job)
        print_job_fit_debug(
            job.get("company"),
            job.get("title"),
            score,
            note="helper_external_apply (0.0 = gates failed or fit 0; else fit after gates)",
        )
        cl = _cover_letter_for_job(cover_gen, resume, job, cover_cache)
        tracker.log(job, status="apply_opened", score=score, cover_letter=cl)

        docx_path = _cover_docx_path(downloads_dir, job.get("company", ""), job.get("title", ""))
        try:
            write_cover_letter_docx(cl, docx_path)
        except Exception as e:
            log.warning("Could not write cover letter to %s: %s", docx_path, e)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _append_helper_apply_event(
            {
                "ts": ts,
                "linkedin_job_id": jid,
                "title": job.get("title"),
                "company": job.get("company"),
                "linkedin_url": job.get("url"),
                "external_apply_url": ext_url,
                "cover_letter_docx": str(docx_path.resolve()),
                "status": "apply_opened",
            }
        )
        log.info(
            "External apply tab — recorded job %s at %s; cover letter: %s",
            job.get("title"),
            job.get("company"),
            docx_path,
        )

    _restore_tab_after_linkedin_work(driver, restore_to, linkedin_jobs_tab)
    return cur


def _stub_job_for_external_apply(driver: Any) -> dict:
    """When the active tab is not a LinkedIn job URL (e.g. Workday apply), still pass a job dict for cover/cache keys."""
    url = driver.current_url or ""
    slug = re.sub(
        r"[^\w]+",
        "_",
        urllib.parse.urlparse(url).netloc + "_" + urllib.parse.urlparse(url).path,
    )[:120]
    return {
        "id": slug or "external_apply",
        "title": "(external apply)",
        "company": "",
        "location": "",
        "url": url or "https://",
        "description": "",
        "easy_apply": True,
    }


def _cover_letter_for_job(
    cover_gen: CoverLetterGenerator,
    resume: dict,
    job: dict,
    cache: dict[str, str],
) -> str:
    jid = str(job.get("id") or "")
    if not jid:
        return cover_gen.generate(resume, job)
    if jid not in cache:
        cache[jid] = cover_gen.generate(resume, job)
    return cache[jid]


def _stdin_command_loop(q: queue.Queue[str]) -> None:
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if line == "":
            break
        q.put(line.strip().lower())


def run_helper_mode(
    *,
    args: Any,
    resume: dict,
    tracker: ApplicationTracker,
    matcher: JobMatcher,
    cover_gen: CoverLetterGenerator,
    account_first: str | None = None,
) -> None:
    poll = max(0.2, float(args.helper_poll))

    searcher = JobSearcher(
        headless=args.headless,
        session_file=DEFAULT_COOKIE_PATH,
        step_delay=args.step_delay,
        highlight=not args.no_highlight,
        pause_after_navigate=False,
        account_first_name=account_first,
        job_cards_wait_seconds=args.job_cards_wait,
        login_form_wait_seconds=args.login_form_wait,
        next_page_wait_seconds=args.next_page_wait,
        job_description_wait_seconds=args.job_desc_wait,
        login_complete_max_seconds=args.login_poll_max,
        login_complete_max_seconds_no_checkpoint=args.login_poll_max_no_checkpoint,
        posted_within_24h=args.posted_within_24h,
    )

    filler = EasyApplyFiller(
        headless=args.headless,
        step_delay=args.step_delay,
        highlight=not args.no_highlight,
        easy_apply_wait_seconds=0.0,
        apply_click_gap_seconds=0.0,
        apply_review_pause_after_fill_seconds=float(args.helper_review_pause),
        cover_letter_docx_dir=args.cover_letter_dir,
        form_fill_rules_path=getattr(args, "form_fill_rules", None),
        helper_scan_all_tabs=bool(getattr(args, "helper_scan_all_tabs", False)),
    )

    company_blacklist = load_company_blacklist(getattr(args, "company_blacklist", None))

    query = searcher._jobs_search_query(args.keywords, args.location, args.easy_apply_only)
    search_url = f"https://www.linkedin.com/jobs/search/?{query}"

    cmd_q: queue.Queue[str] = queue.Queue()
    stdin_thread = threading.Thread(target=_stdin_command_loop, args=(cmd_q,), daemon=True)
    stdin_thread.start()

    scan = bool(getattr(args, "helper_scan_all_tabs", False))
    log.info(
        "Helper mode: browse and apply in Chrome. Assist checks %s for Easy Apply / Workday "
        "(WebDriver cannot see which tab you clicked — only the last handle it switched to). "
        "Commands: r = record apply, q = quit.",
        "every tab (--helper-scan-all-tabs; may flash tabs)" if scan else "WebDriver's current tab only",
    )

    driver = build_chrome(headless=args.headless)
    last_focused_job_id: str | None = None
    cover_cache: dict[str, str] = {}

    try:
        load_cookies(driver, DEFAULT_COOKIE_PATH)
        searcher._login(driver)

        log.info("Navigating to: %s", search_url)
        driver.get(search_url)
        if searcher.step_delay > 0:
            time.sleep(searcher.step_delay)
        time.sleep(1.2)
        try:
            linkedin_jobs_tab = driver.current_window_handle
        except Exception:
            linkedin_jobs_tab = None

        try:
            handles_seen: set[str] = set(driver.window_handles)
        except Exception:
            handles_seen = set()
        if not handles_seen:
            try:
                handles_seen = {driver.current_window_handle}
            except Exception:
                pass
        downloads_dir = _resolve_helper_downloads_dir(args)
        log.info(
            "When an external apply tab opens: jobs are logged (apply_opened) and cover letters saved under %s; "
            "see also %s",
            downloads_dir,
            _HELPER_APPLY_EVENTS_JSONL,
        )

        running = True
        while running:
            while True:
                try:
                    cmd = cmd_q.get_nowait()
                except queue.Empty:
                    break
                if cmd in ("q", "quit", "exit"):
                    log.info("Quit requested — shutting down helper session.")
                    running = False
                    break
                if cmd in ("r", "record", "applied", "a", "apply"):
                    try:
                        tab_for_r = driver.current_window_handle
                    except Exception:
                        tab_for_r = None
                    if linkedin_jobs_tab and linkedin_jobs_tab in driver.window_handles:
                        try:
                            driver.switch_to.window(linkedin_jobs_tab)
                        except Exception:
                            pass
                    job = searcher.parse_current_job_from_detail_pane(driver)
                    _restore_tab_after_linkedin_work(driver, tab_for_r, linkedin_jobs_tab)
                    if not job:
                        log.warning(
                            "Could not read a job id from the **current tab** — switch to the LinkedIn job "
                            "page (URL with currentJobId=… or /jobs/view/…) and try again."
                        )
                    elif tracker.already_applied(job["id"]):
                        log.info("Already recorded as applied: %s — %s", job["title"], job["company"])
                    else:
                        score = matcher.score(resume, job)
                        print_job_fit_debug(
                            job.get("company"),
                            job.get("title"),
                            score,
                            note="helper_record_apply",
                        )
                        cl = _cover_letter_for_job(cover_gen, resume, job, cover_cache)
                        tracker.log(job, status="applied", score=score, cover_letter=cl)
                        log.info(
                            "Recorded apply: %.0f%% — %s at %s",
                            score * 100,
                            job["title"],
                            job["company"],
                        )
                        append_applied_job_row(
                            job,
                            credentials_path=args.google_sheets_credentials,
                            spreadsheet_id=args.google_spreadsheet_id,
                        )
            if not running:
                break

            # Remember which tab the user is on so we can return after LinkedIn-only work (avoid stealing focus
            # from the external apply tab every poll).
            try:
                tab_before = driver.current_window_handle
            except Exception:
                tab_before = None

            handles_seen = _detect_external_apply_new_tabs(
                driver,
                searcher,
                linkedin_jobs_tab=linkedin_jobs_tab,
                handles_seen=handles_seen,
                resume=resume,
                cover_gen=cover_gen,
                cover_cache=cover_cache,
                matcher=matcher,
                tracker=tracker,
                downloads_dir=downloads_dir,
                restore_to=tab_before,
                company_blacklist=company_blacklist,
            )

            if linkedin_jobs_tab and linkedin_jobs_tab in driver.window_handles:
                try:
                    driver.switch_to.window(linkedin_jobs_tab)
                except Exception:
                    pass
            job_here = searcher.parse_current_job_from_detail_pane(driver)
            _restore_tab_after_linkedin_work(driver, tab_before, linkedin_jobs_tab)
            if job_here and job_here["id"] != last_focused_job_id:
                last_focused_job_id = job_here["id"]
                _cover_letter_for_job(cover_gen, resume, job_here, cover_cache)
                log.info(
                    "Current tab job %s — %s at %s",
                    job_here["id"],
                    job_here["title"],
                    job_here["company"],
                )

            if filler.assist_context_open(driver):
                job_use = job_here if job_here else _stub_job_for_external_apply(driver)
                cl = _cover_letter_for_job(cover_gen, resume, job_use, cover_cache)
                filler.assist_fill_current_modal(driver, resume, cl, job_use)

            time.sleep(poll)

    finally:
        try:
            save_cookies(driver, DEFAULT_COOKIE_PATH)
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass
        tracker.export_csv("output/applications.csv")
        tracker.export_csv("output/apply_opened.csv", statuses=("apply_opened",))
        log.info("Helper session ended. Exports: output/applications.csv (applied), output/apply_opened.csv (external apply tab)")
