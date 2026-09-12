"""
Job apply bot: LinkedIn Easy Apply (default) or Greenhouse Recruiting sign-in (``--site greenhouse``).
Run: python main.py --location "United States"
Default resume path is resume.pdf in the working directory; use --resume PATH to override.
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

_TIMING_DEFAULTS = {
    "step_delay": 0.35,
    "job_cards_wait": 10.0,
    "login_form_wait": 5.0,
    "next_page_wait": 3.0,
    "job_desc_wait": 3.0,
    "login_poll_max": 120.0,
    "login_poll_max_no_checkpoint": 30.0,
    "easy_apply_wait": 5.0,
    "apply_click_gap": 1.0,
    "apply_review_pause": 3.0,
    "apply_first_empty_pause": 10.0,
    "greenhouse_login_max_seconds": 600.0,
    "greenhouse_jobs_ready_max_seconds": 90.0,
    "greenhouse_scroll_max_rounds": 50,
    "greenhouse_scroll_pause": 1.2,
}

_TIMING_FILE = Path("data/timing.json")

_PATHS_DEFAULTS = {
    "resume": "resume.pdf",
    "resume_cache": "data/resume_profile.json",
    "listings_log": "data/listings_log.jsonl",
    "cover_letter_dir": "output/coverletters/linkedin",
    "filter_cover_letter_dir": "output/coverletters/filter",
    "greenhouse_cover_letter_dir": "output/coverletters/greenhouse",
    "headshot": "data/selfInSuit.png",
    "form_fill_rules": None,
    "company_blacklist": "data/company_blacklist.json",
    "temporary_company_blacklist": "data/company_blacklist_temporary.json",
    "consulting_companies_memory_path": "output/consulting_companies.json",
    "greenhouse_cookies": "data/selenium_greenhouse_cookies.json",
}

_PATHS_FILE = Path("data/paths.json")

from utils.behavior_config import DEFAULT_BEHAVIOR_PATH as _BEHAVIOR_FILE
from utils.behavior_config import load_behavior_config as _load_behavior_config
from utils.search_config import DEFAULT_SEARCH_PATH as _SEARCH_FILE
from utils.search_config import SEARCH_DEFAULTS as _SEARCH_DEFAULTS


def _load_config(path: Path, defaults: dict) -> dict:
    if path.is_file():
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
            return {**defaults, **{k: v for k, v in overrides.items() if k in defaults}}
        except Exception as e:
            logging.getLogger(__name__).warning("Could not read %s: %s — using built-in defaults", path, e)
    return dict(defaults)


def _load_timing() -> dict:
    return _load_config(_TIMING_FILE, _TIMING_DEFAULTS)


def _load_paths() -> dict:
    return _load_config(_PATHS_FILE, _PATHS_DEFAULTS)


def _load_behavior() -> dict:
    return _load_behavior_config(_BEHAVIOR_FILE)


def _load_search() -> dict:
    return _load_config(_SEARCH_FILE, _SEARCH_DEFAULTS)

# Windows consoles often use cp1252; resume/cover text may contain Unicode (e.g. bullets). UTF-8 avoids
# UnicodeEncodeError when logging DEBUG lines from third-party libraries (e.g. OpenAI request bodies).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from utils.chrome_driver import (
    driver_session_alive,
    log_driver_session_closed,
)
from utils.company_lookup_worker import CompanyLookupWorker
from utils.eval_utils.company_blacklist import (
    is_company_blacklisted,
    load_company_blacklist,
    prune_and_load_temporary_blacklist,
)
from utils.eval_utils.consulting_company_memory import (
    load_consulting_company_memory,
    linkedin_company_slug_from_url,
)
from utils.eval_utils.consulting_filter import (
    is_consulting_listing_from_job_posting_text_only,
    is_consulting_listing_from_listing_company_line_only,
)
from utils.eval_utils.student_job_filter import classify_student_job, student_job_passes_filter
from utils.eval_utils.unpaid_job_filter import is_unpaid_job, unpaid_job_passes_filter
from utils.cover_letter import (
    CoverLetterGenerator,
    cover_letter_docx_path_unique,
    delete_cover_letter_for_job,
    write_cover_letter_docx,
)
from utils.display_utils import (
    StatusAwareStreamHandler,
    clear_status_line,
    print_job_fit_debug,
    print_job_outcome,
    print_job_separator,
)
from utils.dspy_lm import configure_dspy
from utils.form_filler import (
    APPLY_ABORT_DAILY_LIMIT,
    APPLY_ABORT_JOB_TRUST_SAFETY,
    EasyApplyFiller,
)
from utils.greenhouse_session import run_greenhouse_sign_in_flow
from utils.job_records import append_listing_record, warn_if_listings_log_sidecars
from utils.job_searcher import JobSearcher, StopApplyPipeline
from utils.eval_utils.matcher import (
    JobMatcher,
    title_has_overqualified_role_level,
    title_shares_search_keyword_token,
)
from utils.extension_rules import migrate_extension_auto_rules_to_exact
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.output_paths import (
    COVERLETTERS_DIR,
    migrate_legacy_consulting_companies_file,
    migrate_legacy_cover_letter_layout,
    migrate_form_fill_rules,
    migrate_legacy_root_archive_files,
)
from utils.s3_log_sync import PendingChangeTracker
from utils.s3_outputs import (
    release_sync_lock,
    sync_download_output_coordinated,
    sync_upload_output_coordinated,
)
from utils.apply_sheets import sheet_export_url_dedupe_key
from utils.resume_cache import load_or_build_resume
from utils.resume_parser import ResumeParser, first_name_from_resume
from utils.tracker import ApplicationTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        StatusAwareStreamHandler(),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# LinkedIn caps Easy Apply volume; default and hard ceiling per run (use --max-applies 0 for no cap).
MAX_EASY_APPLY_PER_RUN = 30


def _job_searcher_from_args(args, timing: dict, search: dict, **kwargs):
    return JobSearcher(
        headless=args.headless,
        step_delay=timing["step_delay"],
        highlight=not args.no_highlight,
        job_cards_wait_seconds=timing["job_cards_wait"],
        login_form_wait_seconds=timing["login_form_wait"],
        next_page_wait_seconds=timing["next_page_wait"],
        job_description_wait_seconds=timing["job_desc_wait"],
        login_complete_max_seconds=timing["login_poll_max"],
        login_complete_max_seconds_no_checkpoint=timing["login_poll_max_no_checkpoint"],
        posted_within_24h=search["posted_within_24h"],
        auto=getattr(args, "auto", False),
        **kwargs,
    )


def run(
    args,
    timing: dict,
    paths: dict,
    behavior: dict,
    search: dict,
    *,
    cover_letter_changes: PendingChangeTracker,
):
    if getattr(args, "auto", False):
        log.info("Auto mode: no manual-intervention pauses (login failures exit, unfilled fields close the job).")
        timing = {**timing, "apply_review_pause": 0.0, "apply_first_empty_pause": 0.0}

    if args.headless:
        log.info("Chrome: headless (no window).")
    else:
        log.info("Chrome: visible window (use --headless or JOB_APPLIER_HEADLESS=1 to hide).")

    # 0 = unlimited listings evaluated; positive int = cap on how many cards we open.
    max_listings_cap: int | None = None if args.max_jobs <= 0 else args.max_jobs
    # 0 = no cap on successful applies; positive int = stop after that many successful applies (capped).
    if args.max_applies <= 0:
        max_applies_cap = None
    else:
        max_applies_cap = min(int(args.max_applies), MAX_EASY_APPLY_PER_RUN)
        if int(args.max_applies) > MAX_EASY_APPLY_PER_RUN:
            log.info(
                "Capping --max-applies from %d to %d (LinkedIn Easy Apply practical limit per run).",
                int(args.max_applies),
                MAX_EASY_APPLY_PER_RUN,
            )

    if args.debug_jobs_page and args.filter:
        raise SystemExit("error: use either --debug-jobs-page or --filter, not both")

    if args.site == "greenhouse" and (args.debug_jobs_page or args.filter):
        raise SystemExit(
            "error: --debug-jobs-page and --filter are for LinkedIn only; use --site linkedin (default) or omit --site."
        )

    if args.site == "greenhouse":
        if args.headless:
            log.warning(
                "Greenhouse sign-in usually needs a visible browser — use --no-headless if you cannot complete login."
            )
        configure_dspy()
        args.keywords = list(search["keywords"])
        args.location = search["location"]
        args.posted_within_24h = search["posted_within_24h"]
        if getattr(args, "auto", False):
            args.greenhouse_manual_next_listing = False
            args.greenhouse_prompt_before_close = False
        run_greenhouse_sign_in_flow(args, cover_letter_tracker=cover_letter_changes)
        return

    if args.debug_jobs_page:
        log.info(
            "Debug mode: login + job search URL only (no scraping, matching, or applies). "
            "Pass --resume to use your first name for login detection."
        )
        account_first = None
        if Path(paths["resume"]).exists():
            resume_dbg = ResumeParser().parse(str(paths["resume"]))
            account_first = first_name_from_resume(resume_dbg)
            log.info("Using first name %r from resume for login detection", account_first)
        searcher = _job_searcher_from_args(
            args,
            timing,
            search,
            pause_after_navigate=True,
            account_first_name=account_first,
        )
        searcher.search(
            keywords=search["keywords"][0],
            location=search["location"],
            max_jobs=max_listings_cap,
            easy_apply_only=True,
        )
        log.info("Debug session finished.")
        return

    filter_mode = bool(getattr(args, "filter", False))

    configure_dspy()

    # 1. Load resume profile (JSON cache or parse PDF/DOCX)
    resume = load_or_build_resume(
        Path(paths["resume"]),
        Path(paths["resume_cache"]),
        force_reparse=args.force_resume_parse,
    )
    log.info(
        "Profile: %d skills, %d roles, %d projects",
        len(resume["skills"]),
        len(resume["experience"]),
        len(resume.get("projects") or []),
    )

    # 2. Search for jobs (Selenium + Chrome; visible by default)
    log.info(
        "Searching LinkedIn in %s for keyword(s): %s",
        search["location"],
        "; ".join(repr(k) for k in search["keywords"]),
    )
    if filter_mode:
        log.info(
            "Filter mode: skip Easy Apply listings; save suitable external-apply jobs on LinkedIn "
            "(apply later via browser extension)."
        )
    else:
        log.info("Job search filter: Easy Apply only (LinkedIn f_AL).")
    if search["posted_within_24h"]:
        log.info("Job search filter: posted in the past 24 hours (LinkedIn f_TPR=r86400).")
    else:
        log.info("Job search filter: any date posted (no f_TPR).")
    tracker = ApplicationTracker("data/applications.db")
    account_first = first_name_from_resume(resume)
    if account_first:
        log.info("Login detection will look for first name %r on LinkedIn pages", account_first)
    else:
        log.warning(
            "No first name parsed from resume — login detection falls back to URL (contains 'feed')"
        )

    matcher = JobMatcher()
    cover_gen = CoverLetterGenerator()
    company_blacklist = load_company_blacklist(paths["company_blacklist"])
    temp_blacklisted = prune_and_load_temporary_blacklist(paths["temporary_company_blacklist"])
    if temp_blacklisted:
        log.info(
            "Temporary company blacklist active: %d entr%s (%s)",
            len(temp_blacklisted),
            "y" if len(temp_blacklisted) == 1 else "ies",
            ", ".join(temp_blacklisted),
        )
        company_blacklist = company_blacklist + temp_blacklisted
    if company_blacklist:
        log.info("Company blacklist active: %d entr%s", len(company_blacklist), "y" if len(company_blacklist) == 1 else "ies")

    filler = None
    if not filter_mode:
        filler = EasyApplyFiller(
            headless=args.headless,
            step_delay=timing["step_delay"],
            highlight=not args.no_highlight,
            easy_apply_wait_seconds=timing["easy_apply_wait"],
            apply_click_gap_seconds=timing["apply_click_gap"],
            apply_review_pause_after_fill_seconds=timing["apply_review_pause"],
            apply_first_empty_field_pause_after_nav_seconds=timing["apply_first_empty_pause"],
            cover_letter_docx_dir=paths["cover_letter_dir"],
            form_fill_rules_path=paths["form_fill_rules"],
            headshot_image_path=paths["headshot"],
            cover_letter_tracker=cover_letter_changes,
        )

    searcher = _job_searcher_from_args(args, timing, search, account_first_name=account_first)
    consulting_memory_path = Path(paths["consulting_companies_memory_path"])
    consulting_memory = None
    if behavior["skip_consulting"] and behavior["consulting_companies_memory"]:
        consulting_memory = load_consulting_company_memory(consulting_memory_path)
    elif behavior["skip_consulting"]:
        log.info(
            "Consulting company memory disabled; "
            "LinkedIn company pages will be re-fetched when listing heuristics pass."
        )

    apply_stats = {"applied": 0}
    apply_lock = threading.Lock()
    seen_job_ids: set[str] = set()
    seen_job_ids_lock = threading.Lock()
    llm_lock = threading.Lock()
    tracker_lock = threading.Lock()

    def _claim_job_id(jid: str) -> bool:
        jid = str(jid or "").strip()
        if not jid:
            return False
        with seen_job_ids_lock:
            if jid in seen_job_ids:
                return False
            seen_job_ids.add(jid)
            return True

    def _apply_cap_reached() -> bool:
        if max_applies_cap is None:
            return False
        with apply_lock:
            return apply_stats.get("applied", 0) >= max_applies_cap

    def _record_successful_save() -> bool:
        """
        Increment shared successful-save counter if under cap.

        Returns True when the increment was recorded (caller should treat as success toward the run).
        """
        with apply_lock:
            if max_applies_cap is not None and apply_stats.get("applied", 0) >= max_applies_cap:
                return False
            apply_stats["applied"] = int(apply_stats.get("applied", 0)) + 1
            return True

    def _tracker_log(*a, **kw):
        with tracker_lock:
            return tracker.log(*a, **kw)

    company_lookup_worker: CompanyLookupWorker | None = None

    def _run_company_jobs_scan(lookup_driver, company_url: str) -> None:
        """Secondary-driver filter scan of a company's Jobs list (no company-page consulting check)."""
        if not filter_mode:
            return
        if _apply_cap_reached():
            log.info("Company jobs scan skipped — successful-save cap already reached.")
            return
        log.info(
            "Company jobs scan starting for %s (LinkedIn past week — f_TPR=r604800)",
            company_url,
        )
        if not searcher.open_company_jobs_list(lookup_driver, company_url):
            log.warning("Company jobs scan: could not open jobs list for %s", company_url)
            return

        company_display = ""
        for _link, peek in searcher.iter_company_job_peeks(
            lookup_driver,
            claim_job_id=_claim_job_id,
        ):
            if _apply_cap_reached():
                log.info("Company jobs scan stopping — successful-save cap reached.")
                break
            if not driver_session_alive(lookup_driver):
                log_driver_session_closed()
                break

            jid = str(peek.get("id") or "").strip()
            title = str(peek.get("title") or "").strip()
            company = str(peek.get("company") or "").strip() or company_display
            if company:
                company_display = company

            if tracker.already_saved(jid):
                log.info(
                    "Company jobs: skipping already-applied/saved %s at %s",
                    title or jid,
                    company or "(no company)",
                )
                continue
            if is_company_blacklisted(company, company_blacklist):
                log.info(
                    "Company jobs: skipping blacklisted company %s (%s)",
                    company or "(no company)",
                    title or jid,
                )
                tracker.log(peek, status="blacklisted", score=0.0)
                continue
            if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(
                peek
            ):
                log.info(
                    "Company jobs: skipping consulting/staffing listing heuristics: %s at %s",
                    title,
                    company,
                )
                _tracker_log(peek, status="consulting", score=0.0)
                continue
            if peek.get("easy_apply"):
                log.info(
                    "Company jobs: skipping Easy Apply (filter mode): %s at %s",
                    title or jid,
                    company or "(no company)",
                )
                _tracker_log(peek, status="skipped", score=0.0)
                continue
            if title_has_overqualified_role_level(title):
                log.info(
                    "Company jobs: skipping overqualified title (no click): %s at %s",
                    title or jid,
                    company or "(no company)",
                )
                _tracker_log(peek, status="skipped", score=0.0)
                continue
            if not title_shares_search_keyword_token(title, search["keywords"]):
                log.info(
                    "Company jobs: skipping title with no keyword overlap (no click): %s at %s",
                    title or jid,
                    company or "(no company)",
                )
                _tracker_log(peek, status="skipped", score=0.0)
                continue

            # Re-find link by job id — prior iterations may have stale element refs.
            link = None
            for cand in searcher._find_job_card_links(lookup_driver, expand=False):
                p2 = searcher._peek_job_from_list_link(cand)
                if p2 and str(p2.get("id") or "").strip() == jid:
                    link = cand
                    peek = {**peek, **{k: v for k, v in p2.items() if v}}
                    break
            if link is None:
                log.debug("Company jobs: could not re-find card for job_id=%s", jid)
                continue

            job = searcher.complete_company_job(lookup_driver, link, peek)
            if not job:
                continue
            if company and not job.get("company"):
                job = {**job, "company": company}

            try:
                append_listing_record(
                    paths["listings_log"],
                    job,
                    phase="parsed_company_jobs",
                    extra={"source": "company_jobs_scan"},
                )
            except Exception:
                log.debug("Company jobs: listings log append failed", exc_info=True)
            else:
                print_job_separator()
                log.info(
                    "Company jobs: recorded listing %s — %s at %s",
                    job.get("id"),
                    job.get("title"),
                    job.get("company"),
                )

            _process_filter_candidate(
                lookup_driver,
                job,
                skip_company_consulting=True,
                enqueue_company_scan=False,
            )

        log.info("Company jobs scan finished for %s", company_url)

    def _ensure_company_lookup_worker() -> CompanyLookupWorker:
        nonlocal company_lookup_worker
        if company_lookup_worker is None:
            company_lookup_worker = CompanyLookupWorker(
                searcher=searcher,
                headless=args.headless,
                session_file=searcher.session_file,
                company_scan_handler=_run_company_jobs_scan if filter_mode else None,
            )
        return company_lookup_worker

    if filter_mode:
        if max_applies_cap is not None:
            log.info(
                "Will stop after %d successful save(s) this run (--max-applies; use 0 for no cap).",
                max_applies_cap,
            )
        else:
            log.info("No successful-save cap (--max-applies 0).")
    elif max_applies_cap is not None:
        log.info(
            "Will stop after %d successful Easy Apply(ies) this run (--max-applies; use 0 for no cap).",
            max_applies_cap,
        )
    else:
        log.info("No successful-apply cap (--max-applies 0).")
    if max_listings_cap is None:
        log.info(
            "No listing cap (--max-jobs 0): may scan many cards until apply cap or end of search."
        )
    else:
        log.info(
            "Will evaluate at most %d job listing(s) this run (--max-jobs safety cap).",
            max_listings_cap,
        )

    def _require_browser_session(drv) -> None:
        """Stop the pipeline when the main Chrome window was closed during non-browser work (e.g. LLM)."""
        if not driver_session_alive(drv):
            log_driver_session_closed()
            raise StopApplyPipeline("browser closed")

    def _process_filter_candidate(
        driver,
        job: dict,
        *,
        skip_company_consulting: bool,
        enqueue_company_scan: bool,
    ) -> None:
        """
        Shared filter-mode evaluation after a job dict is available: gates, fit, optional consulting,
        cover letter, Save. Used by primary search and secondary company-jobs scan.
        """
        jid = str(job.get("id") or "").strip()
        if _apply_cap_reached():
            log.info("Skipping filter candidate — successful-save cap already reached: %s", jid)
            return

        if tracker.already_saved(job["id"]):
            print_job_outcome(job["title"], job["company"], outcome="skipped", reason="already applied/saved", job_id=jid)
            return

        if not job.get("easy_apply"):
            # Some companies repost the identical listing under a second LinkedIn job id; the
            # id-based check above can't catch that, but both postings' external "Apply" button
            # goes to the same destination — read it and dedupe on that instead.
            dest_url = searcher.selected_job_apply_destination_url(driver)
            if dest_url:
                dest_key = sheet_export_url_dedupe_key(dest_url)
                if dest_key and dest_key in tracker.recorded_url_dedupe_keys():
                    print_job_outcome(
                        job["title"], job["company"], outcome="skipped",
                        reason="duplicate posting (same external apply link)", job_id=jid,
                    )
                    return
                job = {**job, "url": dest_url}

        if is_company_blacklisted(job.get("company") or "", company_blacklist):
            _tracker_log(job, status="blacklisted", score=0.0)
            print_job_outcome(job["title"], job["company"], outcome="blacklisted", job_id=jid)
            return

        student_job_classification = classify_student_job(job["title"], job.get("description") or "")
        if not student_job_passes_filter(student_job_classification, behavior["student_job_mode"]):
            _tracker_log(job, status="student_job_filtered", score=0.0)
            print_job_outcome(
                job["title"], job["company"], outcome="student_job_filtered",
                reason=f"classified as {student_job_classification!r}, mode={behavior['student_job_mode']!r}",
                job_id=jid,
            )
            return

        if not unpaid_job_passes_filter(
            is_unpaid_job(job["title"], job.get("description") or ""), behavior["unpaid_job_mode"]
        ):
            _tracker_log(job, status="unpaid_job_filtered", score=0.0)
            print_job_outcome(
                job["title"], job["company"], outcome="unpaid_job_filtered",
                reason=f"listing mentions \"unpaid\", mode={behavior['unpaid_job_mode']!r}",
                job_id=jid,
            )
            return

        if job.get("easy_apply"):
            _tracker_log(job, status="skipped", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="easy-apply", job_id=jid)
            print_job_outcome(
                job["title"], job["company"], outcome="skipped",
                reason="Easy Apply — filter mode targets external apply only",
                job_id=jid, dismissed=dismissed,
            )
            return

        with llm_lock:
            if not matcher.gates_pass(resume, job):
                print_job_fit_debug(job.get("company"), job.get("title"), None, note="gates_failed")
                _tracker_log(job, status="skipped", score=0.0)
                dismissed = searcher.dismiss_current_job(
                    driver, reason="gates-failed", job_id=str(job.get("id") or "")
                )
                print_job_outcome(
                    job["title"], job["company"], outcome="skipped",
                    reason="education or experience requirements not met",
                    job_id=jid, dismissed=dismissed,
                )
                return

            fit = matcher.fit_score(resume, job)
        print_job_fit_debug(
            job.get("company"),
            job.get("title"),
            fit,
            note=f"min_score={float(args.min_score):.3f}",
        )
        if fit < args.min_score:
            _tracker_log(job, status="skipped", score=fit)
            print_job_outcome(
                job["title"], job["company"], outcome="skipped",
                reason=f"below fit threshold ({args.min_score * 100:.0f}%, scored {fit * 100:.0f}%)",
                job_id=jid,
            )
            return
        log.info("Fit score %.0f%%: %s at %s", fit * 100, job["title"], job["company"])

        if behavior["skip_consulting"] and is_consulting_listing_from_job_posting_text_only(job):
            _tracker_log(job, status="consulting", score=0.0)
            dismissed = searcher.dismiss_current_job(
                driver, reason="consulting-signals", job_id=str(job.get("id") or "")
            )
            print_job_outcome(
                job["title"], job["company"], outcome="consulting",
                reason="consulting/staffing signals in job title or description",
                job_id=jid, dismissed=dismissed,
            )
            return
        if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(job):
            _tracker_log(job, status="consulting", score=0.0)
            dismissed = searcher.dismiss_current_job(
                driver, reason="consulting-signals", job_id=str(job.get("id") or "")
            )
            print_job_outcome(
                job["title"], job["company"], outcome="consulting",
                reason="consulting/staffing on listing company or title line",
                job_id=jid, dismissed=dismissed,
            )
            return

        company_link_li = ""
        if not skip_company_consulting:
            if behavior["skip_consulting"] and consulting_memory is not None:
                if consulting_memory.matches(
                    slug=None,
                    company_display=str(job.get("company") or ""),
                ):
                    _tracker_log(job, status="consulting", score=0.0)
                    dismissed = searcher.dismiss_current_job(
                        driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                    )
                    print_job_outcome(
                        job["title"], job.get("company"), outcome="consulting",
                        reason="remembered consulting company (no company page fetch)",
                        job_id=jid, dismissed=dismissed,
                    )
                    return

            company_link_li = searcher.selected_job_company_link(driver)
            if behavior["skip_consulting"] and consulting_memory is not None and company_link_li:
                slug_only = linkedin_company_slug_from_url(company_link_li)
                if slug_only and consulting_memory.matches(
                    slug=slug_only,
                    company_display=str(job.get("company") or ""),
                ):
                    _tracker_log(job, status="consulting", score=0.0)
                    dismissed = searcher.dismiss_current_job(
                        driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                    )
                    print_job_outcome(
                        job["title"], job.get("company"), outcome="consulting",
                        reason="remembered consulting company by LinkedIn slug (no company page fetch)",
                        job_id=jid, dismissed=dismissed,
                    )
                    return

            if behavior["skip_consulting"] and company_link_li:
                m = re.search(
                    r"(https://www\.linkedin\.com/company/[^/]+)", company_link_li, re.IGNORECASE
                )
                normalized_link = f"{m.group(1)}/about/" if m else company_link_li
                worker = _ensure_company_lookup_worker()
                try:
                    is_consulting = worker.submit_consulting(normalized_link)
                except Exception:
                    log.exception("Company-page consulting check failed — continuing without it.")
                    is_consulting = False
                if is_consulting:
                    _tracker_log(job, status="consulting", score=0.0)
                    if consulting_memory is not None:
                        consulting_memory.remember(
                            slug=linkedin_company_slug_from_url(company_link_li),
                            company_display=str(job.get("company") or ""),
                        )
                    dismissed = searcher.dismiss_current_job(
                        driver, reason="company-page-signals", job_id=str(job.get("id") or "")
                    )
                    print_job_outcome(
                        job["title"], job["company"], outcome="consulting",
                        reason="company page indicates consulting/recruiting (remembered)",
                        job_id=jid, dismissed=dismissed,
                    )
                    return

            # Dedicated-page requirements (primary path only).
            worker = _ensure_company_lookup_worker()
            try:
                added_reqs = worker.submit_dedicated_reqs(jid)
            except Exception:
                log.exception("Dedicated page requirements fetch failed.")
                added_reqs = ""
            if added_reqs and added_reqs not in (job.get("description") or ""):
                log.debug("Dedicated page: appending requirements section to job description.")
                job = {**job, "description": (job.get("description") or "") + "\n\n" + added_reqs}
                with llm_lock:
                    gates_ok = matcher.gates_pass(resume, job)
                if not gates_ok:
                    print_job_fit_debug(
                        job.get("company"), job.get("title"), None, note="gates_failed_dedicated_page"
                    )
                    _tracker_log(job, status="skipped", score=0.0)
                    dismissed = searcher.dismiss_current_job(
                        driver, reason="gates-failed", job_id=str(job.get("id") or "")
                    )
                    print_job_outcome(
                        job["title"], job.get("company"), outcome="skipped",
                        reason="dedicated page requirements not met",
                        job_id=jid, dismissed=dismissed,
                    )
                    return

        if _apply_cap_reached():
            return

        if not driver_session_alive(driver):
            log_driver_session_closed()
            if not skip_company_consulting:
                raise StopApplyPipeline("browser closed")
            return

        log.info("  → Generating cover letter...")
        with llm_lock:
            cover_letter = cover_gen.generate(resume, job)
        docx_path = cover_letter_docx_path_unique(
            paths["filter_cover_letter_dir"],
            site="filter",
            company=str(job.get("company") or ""),
            title=str(job.get("title") or ""),
            job_id=jid,
        )
        try:
            write_cover_letter_docx(cover_letter, docx_path)
            log.info("  → Cover letter: %s", docx_path.resolve())
            try:
                rel = docx_path.resolve().relative_to(COVERLETTERS_DIR.resolve()).as_posix()
                cover_letter_changes.record_write(rel)
            except ValueError:
                pass
        except Exception as e:
            log.warning("  → Could not write cover letter docx: %s", e)
        log.info("  → Saving on LinkedIn (filter mode)...")
        success = searcher.save_current_job(driver, job_id=jid)
        if success:
            _tracker_log(job, status="saved", score=fit, cover_letter=str(docx_path))
        if success and _record_successful_save():
            log.info(
                "  ✓ Saved on LinkedIn (apply later via extension). [%d%s]",
                apply_stats["applied"],
                f"/{max_applies_cap}" if max_applies_cap is not None else "",
            )
            if enqueue_company_scan:
                link = company_link_li or searcher.selected_job_company_link(driver)
                if link:
                    _ensure_company_lookup_worker().submit_company_scan(link)
                    log.info("  → Queued company Jobs scan for secondary driver.")
                else:
                    log.info("  → No company link found — skipping company Jobs scan.")
        elif success:
            log.info("  ✓ Saved on LinkedIn, but apply cap already reached — not counting.")
        else:
            log.warning("  ✗ Could not click Save — check the browser.")

    def maybe_skip_from_list_card_preview(driver, peek: dict) -> bool:
        """
        Blacklist / consulting memory / listing-company heuristics using only list-card text (no job click).

        When this returns True, the card is dismissed (except already-applied) and ``process_listing`` is not run.
        """
        jid = str(peek.get("id") or "").strip()
        if not jid:
            return False
        company = str(peek.get("company") or "").strip()
        title = str(peek.get("title") or "").strip()

        if tracker.already_saved(jid):
            print_job_outcome(title, company, outcome="skipped", reason="already applied/saved (list card)", job_id=jid)
            return True

        if is_company_blacklisted(company, company_blacklist):
            _tracker_log(peek, status="blacklisted", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="blacklisted-list-card", job_id=jid)
            print_job_outcome(
                title, company, outcome="blacklisted", reason="list card (no job click)",
                job_id=jid, dismissed=dismissed,
            )
            return True

        if behavior["skip_consulting"] and consulting_memory is not None:
            if consulting_memory.matches(slug=None, company_display=company):
                _tracker_log(peek, status="consulting", score=0.0)
                dismissed = searcher.dismiss_current_job(
                    driver, reason="consulting-remembered-list-card", job_id=jid
                )
                print_job_outcome(
                    title, company, outcome="consulting",
                    reason="remembered consulting company (list card, no job click)",
                    job_id=jid, dismissed=dismissed,
                )
                return True

        if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(peek):
            _tracker_log(peek, status="consulting", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="consulting-list-card", job_id=jid)
            print_job_outcome(
                title, company, outcome="consulting",
                reason="listing company/title consulting heuristics (list card, no job click)",
                job_id=jid, dismissed=dismissed,
            )
            return True

        if filter_mode and peek.get("easy_apply"):
            _tracker_log(peek, status="skipped", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="easy-apply-list-card", job_id=jid)
            print_job_outcome(
                title, company, outcome="skipped",
                reason="Easy Apply — filter mode targets external apply only (list card)",
                job_id=jid, dismissed=dismissed,
            )
            return True

        if not filter_mode and not peek.get("easy_apply"):
            searcher.recover_easy_apply_filter(driver)
            _tracker_log(peek, status="skipped", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="non-easy-apply-list-card", job_id=jid)
            print_job_outcome(
                title, company, outcome="skipped",
                reason="non-Easy Apply listing on card (Easy Apply filter may have dropped)",
                job_id=jid, dismissed=dismissed,
            )
            return True

        return False

    def process_listing(driver, job: dict) -> None:
        _require_browser_session(driver)

        if filter_mode:
            _process_filter_candidate(
                driver,
                job,
                skip_company_consulting=False,
                enqueue_company_scan=True,
            )
            return

        jid = str(job.get("id") or "").strip()

        if tracker.already_applied(job["id"]):
            print_job_outcome(job["title"], job["company"], outcome="skipped", reason="already applied", job_id=jid)
            return
        if is_company_blacklisted(job.get("company") or "", company_blacklist):
            _tracker_log(job, status="blacklisted", score=0.0)
            print_job_outcome(job["title"], job["company"], outcome="blacklisted", job_id=jid)
            return

        student_job_classification = classify_student_job(job["title"], job.get("description") or "")
        if not student_job_passes_filter(student_job_classification, behavior["student_job_mode"]):
            _tracker_log(job, status="student_job_filtered", score=0.0)
            print_job_outcome(
                job["title"], job["company"], outcome="student_job_filtered",
                reason=f"classified as {student_job_classification!r}, mode={behavior['student_job_mode']!r}",
                job_id=jid,
            )
            return

        if not unpaid_job_passes_filter(
            is_unpaid_job(job["title"], job.get("description") or ""), behavior["unpaid_job_mode"]
        ):
            _tracker_log(job, status="unpaid_job_filtered", score=0.0)
            print_job_outcome(
                job["title"], job["company"], outcome="unpaid_job_filtered",
                reason=f"listing mentions \"unpaid\", mode={behavior['unpaid_job_mode']!r}",
                job_id=jid,
            )
            return

        if not job.get("easy_apply"):
            searcher.recover_easy_apply_filter(driver)
            _tracker_log(job, status="skipped", score=0.0)
            dismissed = searcher.dismiss_current_job(driver, reason="non-easy-apply", job_id=jid)
            print_job_outcome(
                job["title"], job["company"], outcome="skipped",
                reason="non-Easy Apply listing opened (Easy Apply filter may have dropped)",
                job_id=jid, dismissed=dismissed,
            )
            return

        with llm_lock:
            if not matcher.gates_pass(resume, job):
                print_job_fit_debug(job.get("company"), job.get("title"), None, note="gates_failed")
                _tracker_log(job, status="skipped", score=0.0)
                _require_browser_session(driver)
                dismissed = searcher.dismiss_current_job(
                    driver, reason="gates-failed", job_id=str(job.get("id") or "")
                )
                print_job_outcome(
                    job["title"], job["company"], outcome="skipped",
                    reason="education or experience requirements not met",
                    job_id=jid, dismissed=dismissed,
                )
                return

            fit = matcher.fit_score(resume, job)
        print_job_fit_debug(
            job.get("company"),
            job.get("title"),
            fit,
            note=f"min_score={float(args.min_score):.3f}",
        )

        if fit < args.min_score:
            _tracker_log(job, status="skipped", score=fit)
            print_job_outcome(
                job["title"], job["company"], outcome="skipped",
                reason=f"below fit threshold ({args.min_score * 100:.0f}%, scored {fit * 100:.0f}%)",
                job_id=jid,
            )
            return
        log.info("Fit score %.0f%%: %s at %s", fit * 100, job["title"], job["company"])

        _require_browser_session(driver)

        # Company-based consulting checks come last so requirement/fit disqualifications short-circuit first.
        if behavior["skip_consulting"] and is_consulting_listing_from_job_posting_text_only(job):
            _tracker_log(job, status="consulting", score=0.0)
            dismissed = searcher.dismiss_current_job(
                driver, reason="consulting-signals", job_id=str(job.get("id") or "")
            )
            print_job_outcome(
                job["title"], job["company"], outcome="consulting",
                reason="consulting/staffing signals in job title or description",
                job_id=jid, dismissed=dismissed,
            )
            return
        if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(job):
            _tracker_log(job, status="consulting", score=0.0)
            dismissed = searcher.dismiss_current_job(
                driver, reason="consulting-signals", job_id=str(job.get("id") or "")
            )
            print_job_outcome(
                job["title"], job["company"], outcome="consulting",
                reason="consulting/staffing on listing company or title line",
                job_id=jid, dismissed=dismissed,
            )
            return

        company_link_li = None
        if behavior["skip_consulting"] and consulting_memory is not None:
            if consulting_memory.matches(
                slug=None,
                company_display=str(job.get("company") or ""),
            ):
                _tracker_log(job, status="consulting", score=0.0)
                dismissed = searcher.dismiss_current_job(
                    driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                )
                print_job_outcome(
                    job["title"], job.get("company"), outcome="consulting",
                    reason="remembered consulting company (no company page fetch)",
                    job_id=jid, dismissed=dismissed,
                )
                return

        if behavior["skip_consulting"]:
            company_link_li = searcher.selected_job_company_link(driver)
            _require_browser_session(driver)
            if consulting_memory is not None and company_link_li:
                slug_only = linkedin_company_slug_from_url(company_link_li)
                if slug_only and consulting_memory.matches(
                    slug=slug_only,
                    company_display=str(job.get("company") or ""),
                ):
                    _tracker_log(job, status="consulting", score=0.0)
                    dismissed = searcher.dismiss_current_job(
                        driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                    )
                    print_job_outcome(
                        job["title"], job.get("company"), outcome="consulting",
                        reason="remembered consulting company by LinkedIn slug (no company page fetch)",
                        job_id=jid, dismissed=dismissed,
                    )
                    return

        if behavior["skip_consulting"] and company_link_li:
            m = re.search(
                r"(https://www\.linkedin\.com/company/[^/]+)", company_link_li, re.IGNORECASE
            )
            normalized_link = f"{m.group(1)}/about/" if m else company_link_li
            worker = _ensure_company_lookup_worker()
            try:
                is_consulting = worker.submit_consulting(normalized_link)
            except Exception:
                log.exception("Company-page consulting check failed — continuing without it.")
                is_consulting = False
            if is_consulting:
                _tracker_log(job, status="consulting", score=0.0)
                if consulting_memory is not None:
                    consulting_memory.remember(
                        slug=linkedin_company_slug_from_url(company_link_li),
                        company_display=str(job.get("company") or ""),
                    )
                dismissed = searcher.dismiss_current_job(
                    driver, reason="company-page-signals", job_id=str(job.get("id") or "")
                )
                print_job_outcome(
                    job["title"], job["company"], outcome="consulting",
                    reason="company page indicates consulting/recruiting (remembered)",
                    job_id=jid, dismissed=dismissed,
                )
                return

        # Fetch the job's dedicated page to capture "Requirements added by the job poster".
        worker = _ensure_company_lookup_worker()
        try:
            added_reqs = worker.submit_dedicated_reqs(jid)
        except Exception:
            log.exception("Dedicated page requirements fetch failed.")
            added_reqs = ""
        if added_reqs and added_reqs not in (job.get("description") or ""):
            log.debug("Dedicated page: appending requirements section to job description.")
            job = {**job, "description": (job.get("description") or "") + "\n\n" + added_reqs}
            with llm_lock:
                gates_ok = matcher.gates_pass(resume, job)
            if not gates_ok:
                print_job_fit_debug(
                    job.get("company"), job.get("title"), None, note="gates_failed_dedicated_page"
                )
                _tracker_log(job, status="skipped", score=0.0)
                dismissed = searcher.dismiss_current_job(
                    driver, reason="gates-failed", job_id=str(job.get("id") or "")
                )
                print_job_outcome(
                    job["title"], job.get("company"), outcome="skipped",
                    reason="dedicated page requirements not met",
                    job_id=jid, dismissed=dismissed,
                )
                return

        with llm_lock:
            cover_letter = cover_gen.generate(resume, job)
        _require_browser_session(driver)
        log.info("  → Easy Apply (same browser session)...")
        success = filler.apply(job, resume, cover_letter, driver=driver)
        abort_reason = filler.consume_apply_abort_reason()
        if not success and abort_reason == APPLY_ABORT_DAILY_LIMIT:
            log.warning(
                "  → LinkedIn daily application limit reached — stopping the run (more jobs tomorrow)."
            )
            raise StopApplyPipeline("LinkedIn daily application limit reached")
        if not success and abort_reason == APPLY_ABORT_JOB_TRUST_SAFETY:
            _tracker_log(job, status="skipped", score=fit)
            dismissed = searcher.dismiss_current_job(
                driver, reason="job-trust-safety", job_id=str(job.get("id") or "")
            )
            print_job_outcome(
                job["title"], job["company"], outcome="skipped",
                reason="LinkedIn trust/safety warning", job_id=jid, dismissed=dismissed,
            )
            return

        status = "applied" if success else "failed"
        _tracker_log(job, status=status, score=fit, cover_letter=cover_letter)

        if success:
            _record_successful_save()
            log.info("  ✓ Applied successfully!")
            delete_cover_letter_for_job(
                paths["cover_letter_dir"],
                site="linkedin",
                company=str(job.get("company") or ""),
                title=str(job.get("title") or ""),
                job_id=jid,
                tracker=cover_letter_changes,
            )
        else:
            log.warning("  ✗ Application failed — check output/screenshots/")

    processed = 0
    try:
        processed = searcher.run_search_apply_pipeline(
            keywords=list(search["keywords"]),
            location=search["location"],
            max_listings=max_listings_cap,
            easy_apply_only=not filter_mode,
            listings_log_path=paths["listings_log"],
            process_listing=process_listing,
            max_applies=max_applies_cap,
            apply_counter=apply_stats,
            maybe_skip_from_list_card=maybe_skip_from_list_card_preview,
            seen_job_ids=seen_job_ids,
            seen_job_ids_lock=seen_job_ids_lock,
        )
    except StopApplyPipeline as e:
        log.error("Run stopped: %s", e)
    finally:
        if company_lookup_worker is not None:
            log.info("Stopping company lookup worker (dropping any queued scans/lookups)…")
            company_lookup_worker.stop()
        try:
            tracker.export_csv("output/applications.csv")
            log.info("Exported output/applications.csv.")
        except Exception:
            log.exception("Failed to export applications.csv from SQLite tracker.")

    if filter_mode:
        log.info(
            "Finished filter pipeline: %d listing(s) processed, %d saved on LinkedIn (see %s).",
            processed,
            apply_stats["applied"],
            paths["listings_log"],
        )
    else:
        log.info(
            "Finished search pipeline: %d listing(s) processed, %d successful apply(ies) (see %s).",
            processed,
            apply_stats["applied"],
            paths["listings_log"],
        )
        if searcher.easy_apply_filter_recoveries:
            log.info(
                "Easy Apply filter was re-enabled via UI %d time(s) after non-Easy Apply listings appeared.",
                searcher.easy_apply_filter_recoveries,
            )


def _cover_letter_modes_for_run(*, site: str, filter_mode: bool) -> tuple[str, ...]:
    """S3/prune scope: one subfolder under ``output/coverletters/`` per mode."""
    if site == "greenhouse":
        return ("greenhouse",)
    if filter_mode:
        return ("filter",)
    return ("linkedin",)


def _early_cli_flags() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--site", choices=("linkedin", "greenhouse"), default="linkedin")
    p.add_argument("--filter", action="store_true")
    return p.parse_known_args()[0]


def main():
    load_dotenv()
    # Guard for the finally block below, in case something raises before the download call ever
    # assigns it a real value.
    lock_token: str | None = None
    cover_letter_changes = PendingChangeTracker()
    form_fill_rule_changes = PendingChangeTracker()
    timing = _load_timing()
    paths = _load_paths()
    behavior = _load_behavior()
    search = _load_search()
    early = _early_cli_flags()
    cover_modes = _cover_letter_modes_for_run(site=early.site, filter_mode=early.filter)
    if cover_modes:
        log.info(
            "S3 cover letters: active subfolder(s) coverletters/%s (other modes skipped)",
            ", coverletters/".join(cover_modes),
        )
    # Held through the upload in the finally block below, across the entire pipeline run in
    # between — see sync_download_output_coordinated.
    lock_token = sync_download_output_coordinated(cover_letter_modes=cover_modes)
    prune_cover_letters_for_sync(cover_letter_modes=cover_modes, tracker=cover_letter_changes)
    warn_if_listings_log_sidecars(paths.get("listings_log"))
    migrate_legacy_consulting_companies_file()
    migrate_legacy_root_archive_files()
    migrate_legacy_cover_letter_layout()
    migrate_form_fill_rules()
    migrate_extension_auto_rules_to_exact(tracker=form_fill_rule_changes)

    ap = argparse.ArgumentParser(
        description="Job tools: LinkedIn Easy Apply or filter mode, or Greenhouse MyGreenhouse application helper."
    )
    ap.add_argument(
        "--site",
        choices=("linkedin", "greenhouse"),
        default="linkedin",
        help="Job board: linkedin (default: Easy Apply pipeline) or greenhouse — **application helper** "
        "(MyGreenhouse sign-in → jobs; see https://my.greenhouse.io/users/sign_in). Runs one job search per "
        "``--keywords`` phrase (merged), then opens collected **View job** "
        "URLs in order, skips listings that fail education/experience gates (same JobMatcher as LinkedIn), runs "
        "autofill, cover letter DOCX upload when the field exists, and ``checkbox_groups`` rules. By default, "
        "after each helped job this terminal prompts: **n** records to `output/assisted_applications.csv` "
        "(same columns as `applications.csv`; dedupe also uses `output/archive/assisted_applications_history.csv`) "
        "then scans for the next gate-passing listing; **s** scans without "
        "recording; **d** dismisses (like **s** but appends URL + date to `output/greenhouse_dismissed.csv` — "
        "that posting is omitted from job-list collection for 30 days); Enter or **q** stops. "
        "Use --no-greenhouse-manual-next-listing to stop after the first passing "
        "job only. Email is prefilled from --resume-cache when ``email`` is set there.",
    )
    ap.add_argument(
        "--force-resume-parse",
        action="store_true",
        help="Always parse --resume from disk and overwrite --resume-cache.",
    )
    ap.add_argument(
        "--max-applies",
        type=int,
        default=MAX_EASY_APPLY_PER_RUN,
        metavar="N",
        help=f"Stop after N successful Easy Applies this run (default: {MAX_EASY_APPLY_PER_RUN}; "
        f"values above {MAX_EASY_APPLY_PER_RUN} are capped). "
        "Use 0 for no apply cap (run until --max-jobs listings or end of search).",
    )
    ap.add_argument(
        "--max-jobs",
        type=int,
        default=0,
        metavar="N",
        help="Max job listings to open and evaluate per run (default: 0 = no cap). "
        "Use with --max-applies as a safety bound (e.g. --max-jobs 800 --max-applies 30). "
        "LinkedIn shows ~25 listings per page when more pages exist.",
    )
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.6,
        help="Minimum semantic fit score 0–1 after education/years gates pass (default: 0.6).",
    )
    ap.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run Chrome in headless mode (no window). Omit both flags to follow env: JOB_APPLIER_HEADLESS "
        "or HEADLESS set to 1/true/yes enables headless; otherwise the browser is visible. "
        "Use --no-headless to force a visible window even when env is set.",
    )

    ap.add_argument(
        "--auto",
        action="store_true",
        help="Unattended mode: skip all manual-intervention pauses. On login checkpoint/2FA → print an error "
        "and exit instead of waiting. On form fields with no fill rule → close the job immediately "
        "(no pause for manual fill). Greenhouse: disables the per-job prompt and the close-browser prompt.",
    )
    ap.add_argument(
        "--no-highlight",
        action="store_true",
        help="Disable the red outline that shows which element is being clicked",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging for this app on the console and in data/bot.log (default is INFO). "
        "Selenium, OpenAI/LiteLLM, and HTTP client libraries stay at WARNING so logs stay readable.",
    )
    ap.add_argument(
        "--debug-jobs-page",
        action="store_true",
        help="Open LinkedIn job search after login, then wait for Enter; skips resume, scraping, and applies.",
    )
    ap.add_argument(
        "--filter",
        action="store_true",
        help="LinkedIn filter mode: same gates/fit/consulting checks as auto-apply, but **skip Easy Apply** "
        "listings, generate a cover letter to ``output/coverletters/filter/``, click **Save** on LinkedIn, "
        "then have the secondary Chrome scan that company's Jobs list for more suitable roles "
        "(counts toward --max-applies). Syncs ``output/`` (consulting memory + filter cover letters) via S3 "
        "when configured. Uses --max-applies as a cap on successful saves (0 = no cap).",
    )
    ap.add_argument(
        "--export-csv",
        action="store_true",
        help="Write output/applications.csv from data/applications.db and exit. "
        "Applied rows already listed in output/archive/applications_archive.csv are omitted "
        "so re-export after archiving does not duplicate rows on the next archive. "
        "Use when a run was interrupted (Ctrl+C) or you want the CSV to match the DB without re-scraping.",
    )
    args = ap.parse_args()

    if args.headless is None:
        v = (os.environ.get("JOB_APPLIER_HEADLESS") or os.environ.get("HEADLESS") or "").strip().lower()
        args.headless = v in ("1", "true", "yes") or args.auto

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        # Selenium/urllib3 log every HTTP round-trip to the local WebDriver at DEBUG — very noisy.
        for _name in (
            "urllib3",
            "urllib3.connectionpool",
            "selenium",
            "selenium.webdriver",
            "selenium.webdriver.remote",
            "selenium.webdriver.remote.remote_connection",
            "httpcore",
            "httpcore.connection",
            "httpx",
            "openai",
            "openai._base_client",
            "litellm",
        ):
            logging.getLogger(_name).setLevel(logging.WARNING)

    try:
        if args.export_csv:
            tracker = ApplicationTracker("data/applications.db")
            tracker.export_csv("output/applications.csv")
            log.info("Re-exported output/applications.csv from SQLite.")
            return

        if not args.debug_jobs_page and args.site == "linkedin":
            cache_p = Path(paths["resume_cache"])
            need_resume_file = args.force_resume_parse or not cache_p.is_file()
            if need_resume_file and not Path(paths["resume"]).exists():
                raise FileNotFoundError(
                    f"Resume not found: {paths['resume']} — place your resume there or update data/paths.json "
                    "(needed when data/resume_profile.json is missing or with --force-resume-parse)."
                )

        run(args, timing, paths, behavior, search, cover_letter_changes=cover_letter_changes)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        clear_status_line()
        try:
            cover_modes = _cover_letter_modes_for_run(site=args.site, filter_mode=args.filter)
            prune_cover_letters_for_sync(cover_letter_modes=cover_modes, tracker=cover_letter_changes)
            sync_upload_output_coordinated(
                cover_letter_modes=cover_modes,
                lock_token=lock_token,
                cover_letter_changes=cover_letter_changes,
                form_fill_rule_changes=form_fill_rule_changes,
            )
        except KeyboardInterrupt:
            log.info("Shutdown: skipping S3 sync.")
            # sync_upload_output_coordinated() releases the lock itself once it finishes the
            # locked-scope upload — if Ctrl+C landed mid-upload instead, it never got there, so
            # release it here rather than leaving it held until another device judges it stale.
            if lock_token is not None:
                release_sync_lock(lock_token)


if __name__ == "__main__":
    main()
