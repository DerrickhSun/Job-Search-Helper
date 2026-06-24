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
    "consulting_companies_memory_path": "output/consulting_companies.json",
    "greenhouse_cookies": "data/selenium_greenhouse_cookies.json",
}

_PATHS_FILE = Path("data/paths.json")

_BEHAVIOR_DEFAULTS = {
    "skip_consulting": True,
    "consulting_companies_memory": True,
    "greenhouse_manual_next_listing": True,
    "greenhouse_prefetch": True,
    "greenhouse_prompt_before_close": True,
    "greenhouse_date_posted": None,
    "greenhouse_gate_probe_max_listings": 0,
}

_BEHAVIOR_FILE = Path("data/behavior.json")

_SEARCH_DEFAULTS: dict = {
    "keywords": ["software developer", "software engineer", "data scientist", "data analyst"],
    "location": "United States",
    "posted_within_24h": True,
}

_SEARCH_FILE = Path("data/search.json")


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
    return _load_config(_BEHAVIOR_FILE, _BEHAVIOR_DEFAULTS)


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
    build_chrome,
    driver_session_alive,
    load_cookies,
    log_driver_session_closed,
    save_cookies,
)
from utils.company_blacklist import is_company_blacklisted, load_company_blacklist
from utils.consulting_company_memory import (
    load_consulting_company_memory,
    linkedin_company_slug_from_url,
)
from utils.consulting_filter import (
    is_consulting_listing_from_job_posting_text_only,
    is_consulting_listing_from_listing_company_line_only,
)
from utils.cover_letter import (
    CoverLetterGenerator,
    cover_letter_docx_path_unique,
    write_cover_letter_docx,
)
from utils.dspy_lm import configure_dspy
from utils.form_filler import (
    APPLY_ABORT_DAILY_LIMIT,
    APPLY_ABORT_JOB_TRUST_SAFETY,
    EasyApplyFiller,
)
from utils.greenhouse_session import run_greenhouse_sign_in_flow
from utils.job_searcher import JobSearcher, StopApplyPipeline
from utils.matcher import JobMatcher, print_job_fit_debug
from utils.extension_rules import migrate_extension_auto_rules_to_exact
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.output_paths import (
    migrate_legacy_consulting_companies_file,
    migrate_legacy_cover_letter_layout,
    migrate_form_fill_rules,
    migrate_legacy_root_archive_files,
)
from utils.s3_outputs import sync_download_output, sync_upload_output
from utils.resume_cache import load_or_build_resume
from utils.resume_parser import ResumeParser, first_name_from_resume
from utils.tracker import ApplicationTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
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


def run(args, timing: dict, paths: dict, behavior: dict, search: dict):
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
        run_greenhouse_sign_in_flow(args)
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

    company_lookup_driver = None

    def ensure_company_lookup_driver():
        nonlocal company_lookup_driver
        if company_lookup_driver is not None:
            return company_lookup_driver
        log.info("Starting second Chrome session for LinkedIn company-page consulting checks.")
        company_lookup_driver = build_chrome(headless=args.headless)
        try:
            load_cookies(company_lookup_driver, searcher.session_file)
        except Exception:
            log.debug("Company lookup driver: cookie load failed", exc_info=True)
        log.info(
            "Company lookup browser: verifying LinkedIn session (same flow as main window — feed, "
            "Welcome Back / saved account, or email/password)."
        )
        try:
            searcher._login(company_lookup_driver)
        except Exception:
            log.exception("Company lookup driver: LinkedIn login failed; closing second Chrome.")
            try:
                company_lookup_driver.quit()
            except Exception:
                pass
            company_lookup_driver = None
            raise
        return company_lookup_driver

    apply_stats = {"applied": 0}
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

    def maybe_skip_from_list_card_preview(driver, peek: dict) -> bool:
        """
        Blacklist / consulting memory / listing-company heuristics using only list-card text (no job click).

        When this returns True, the card is dismissed (except already-applied) and ``process_listing`` is not run.
        """
        jid = str(peek.get("id") or "").strip()
        if not jid:
            return False
        if tracker.already_applied(jid):
            log.info(
                "Skipping from list card (already applied, no job click): %s at %s",
                peek.get("title"),
                peek.get("company"),
            )
            return True

        company = str(peek.get("company") or "").strip()
        title = str(peek.get("title") or "").strip()

        if is_company_blacklisted(company, company_blacklist):
            log.info(
                "Skipping from list card (blacklisted company, no job click): %s at %s",
                title or "(no title)",
                company or "(no company)",
            )
            tracker.log(peek, status="blacklisted", score=0.0)
            if searcher.dismiss_current_job(driver, reason="blacklisted-list-card", job_id=jid):
                log.info("  → Dismissed on LinkedIn from list card.")
            return True

        if behavior["skip_consulting"] and consulting_memory is not None:
            if consulting_memory.matches(slug=None, company_display=company):
                log.info(
                    "Skipping from list card (remembered consulting company, no job click): %s at %s",
                    title or "(no title)",
                    company or "(no company)",
                )
                tracker.log(peek, status="consulting", score=0.0)
                if searcher.dismiss_current_job(
                    driver, reason="consulting-remembered-list-card", job_id=jid
                ):
                    log.info("  → Dismissed on LinkedIn from list card.")
                return True

        if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(peek):
            log.info(
                "Skipping from list card (listing company / title consulting heuristics, no job click): %s at %s",
                title,
                company,
            )
            tracker.log(peek, status="consulting", score=0.0)
            if searcher.dismiss_current_job(driver, reason="consulting-list-card", job_id=jid):
                log.info("  → Dismissed on LinkedIn from list card.")
            return True

        if filter_mode and peek.get("easy_apply"):
            log.info(
                "Skipping from list card (Easy Apply — filter mode targets external apply only): %s at %s",
                title or "(no title)",
                company or "(no company)",
            )
            tracker.log(peek, status="skipped", score=0.0)
            if searcher.dismiss_current_job(driver, reason="easy-apply-list-card", job_id=jid):
                log.info("  → Dismissed on LinkedIn from list card.")
            return True

        if not filter_mode and not peek.get("easy_apply"):
            log.info(
                "Non-Easy Apply listing on card (Easy Apply filter may have dropped): %s at %s",
                title or "(no title)",
                company or "(no company)",
            )
            searcher.recover_easy_apply_filter(driver)
            tracker.log(peek, status="skipped", score=0.0)
            if searcher.dismiss_current_job(driver, reason="non-easy-apply-list-card", job_id=jid):
                log.info("  → Dismissed on LinkedIn from list card.")
            return True

        return False

    def process_listing(driver, job: dict) -> None:
        _require_browser_session(driver)

        jid = str(job.get("id") or "").strip()

        if tracker.already_applied(job["id"]):
            log.info("Skipping (already applied): %s at %s", job["title"], job["company"])
            return
        if is_company_blacklisted(job.get("company") or "", company_blacklist):
            log.info("Skipping (company blacklisted): %s at %s", job["title"], job["company"])
            tracker.log(job, status="blacklisted", score=0.0)
            return

        if filter_mode:
            if job.get("easy_apply"):
                log.info(
                    "Skipping (Easy Apply — filter mode targets external apply only): %s at %s",
                    job["title"],
                    job["company"],
                )
                tracker.log(job, status="skipped", score=0.0)
                if searcher.dismiss_current_job(
                    driver, reason="easy-apply", job_id=jid
                ):
                    log.info("  → Dismissed on LinkedIn.")
                return
        elif not job.get("easy_apply"):
            log.info(
                "Non-Easy Apply job opened (Easy Apply filter may have dropped): %s at %s",
                job["title"],
                job["company"],
            )
            searcher.recover_easy_apply_filter(driver)
            tracker.log(job, status="skipped", score=0.0)
            if searcher.dismiss_current_job(driver, reason="non-easy-apply", job_id=jid):
                log.info("  → Dismissed on LinkedIn.")
            return

        if not matcher.gates_pass(resume, job):
            log.info(
                "Skipping (education or experience requirements not met): %s at %s",
                job["title"],
                job["company"],
            )
            print_job_fit_debug(job.get("company"), job.get("title"), None, note="gates_failed")
            tracker.log(job, status="skipped", score=0.0)
            _require_browser_session(driver)
            if searcher.dismiss_current_job(driver, reason="gates-failed", job_id=str(job.get("id") or "")):
                log.info("  → Dismissed on LinkedIn to avoid revisiting this non-qualifying listing.")
            return

        fit = matcher.fit_score(resume, job)
        log.info("Fit score %.0f%%: %s at %s", fit * 100, job["title"], job["company"])
        print_job_fit_debug(
            job.get("company"),
            job.get("title"),
            fit,
            note=f"min_score={float(args.min_score):.3f}",
        )

        if fit < args.min_score:
            log.info("  → Below fit threshold (%.0f%%), skipping apply", args.min_score * 100)
            tracker.log(job, status="skipped", score=fit)
            return

        _require_browser_session(driver)

        # Company-based consulting checks come last so requirement/fit disqualifications short-circuit first.
        if behavior["skip_consulting"] and is_consulting_listing_from_job_posting_text_only(job):
            log.info(
                "Skipping (consulting / staffing signals in job title or description): %s at %s",
                job["title"],
                job["company"],
            )
            tracker.log(job, status="consulting", score=0.0)
            if searcher.dismiss_current_job(driver, reason="consulting-signals", job_id=str(job.get("id") or "")):
                log.info("  → Dismissed on LinkedIn to avoid revisiting this consulting listing.")
            return
        if behavior["skip_consulting"] and is_consulting_listing_from_listing_company_line_only(job):
            log.info(
                "Skipping (consulting / staffing on listing company or title line): %s at %s",
                job["title"],
                job["company"],
            )
            tracker.log(job, status="consulting", score=0.0)
            if searcher.dismiss_current_job(driver, reason="consulting-signals", job_id=str(job.get("id") or "")):
                log.info("  → Dismissed on LinkedIn to avoid revisiting this consulting listing.")
            return

        company_link_li = None
        if behavior["skip_consulting"] and consulting_memory is not None:
            if consulting_memory.matches(
                slug=None,
                company_display=str(job.get("company") or ""),
            ):
                log.info(
                    "Skipping (remembered consulting company — no LinkedIn company page fetch): %s at %s",
                    job["title"],
                    job.get("company"),
                )
                tracker.log(job, status="consulting", score=0.0)
                if searcher.dismiss_current_job(
                    driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                ):
                    log.info("  → Dismissed on LinkedIn to avoid revisiting this consulting listing.")
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
                    log.info(
                        "Skipping (remembered consulting company by LinkedIn slug — no company page fetch): %s at %s",
                        job["title"],
                        job.get("company"),
                    )
                    tracker.log(job, status="consulting", score=0.0)
                    if searcher.dismiss_current_job(
                        driver, reason="consulting-remembered", job_id=str(job.get("id") or "")
                    ):
                        log.info("  → Dismissed on LinkedIn to avoid revisiting this consulting listing.")
                    return

        if behavior["skip_consulting"] and company_link_li:
            m = re.search(r"(https://www\.linkedin\.com/company/[^/]+)", company_link_li, re.IGNORECASE)
            normalized_link = f"{m.group(1)}/about/" if m else company_link_li
            lookup_driver = ensure_company_lookup_driver()
            if lookup_driver is not None and not driver_session_alive(lookup_driver):
                log.warning(
                    "Company lookup Chrome session ended — skipping company-page consulting check."
                )
                nonlocal company_lookup_driver
                try:
                    company_lookup_driver.quit()
                except Exception:
                    pass
                company_lookup_driver = None
            elif lookup_driver is not None and searcher.company_page_looks_consulting(
                lookup_driver, normalized_link
            ):
                log.info(
                    "Skipping (company page indicates consulting/recruiting): %s at %s",
                    job["title"],
                    job["company"],
                )
                tracker.log(job, status="consulting", score=0.0)
                if consulting_memory is not None:
                    consulting_memory.remember(
                        slug=linkedin_company_slug_from_url(company_link_li),
                        company_display=str(job.get("company") or ""),
                    )
                    log.info("  → Recorded company in consulting memory (%s).", consulting_memory.path)
                if searcher.dismiss_current_job(
                    driver, reason="company-page-signals", job_id=str(job.get("id") or "")
                ):
                    log.info("  → Dismissed on LinkedIn to avoid revisiting this consulting listing.")
                return

        if filter_mode:
            _require_browser_session(driver)
            log.info("  → Generating cover letter...")
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
            except Exception as e:
                log.warning("  → Could not write cover letter docx: %s", e)
            log.info("  → Saving on LinkedIn (filter mode)...")
            success = searcher.save_current_job(driver, job_id=jid)
            if success:
                apply_stats["applied"] += 1
                log.info("  ✓ Saved on LinkedIn (apply later via extension).")
            else:
                log.warning("  ✗ Could not click Save — check the browser.")
            return

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
            tracker.log(job, status="skipped", score=fit)
            if searcher.dismiss_current_job(
                driver, reason="job-trust-safety", job_id=str(job.get("id") or "")
            ):
                log.info("  → Dismissed job card (LinkedIn trust/safety warning).")
            else:
                log.warning("  → Trust/safety warning seen but job card dismiss button not found.")
            return

        status = "applied" if success else "failed"
        applied_at = tracker.log(job, status=status, score=fit, cover_letter=cover_letter)

        if success:
            apply_stats["applied"] += 1
            log.info("  ✓ Applied successfully!")
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
        )
    except StopApplyPipeline as e:
        log.error("Run stopped: %s", e)
    finally:
        if company_lookup_driver is not None:
            try:
                save_cookies(company_lookup_driver, searcher.session_file)
            except Exception:
                log.debug("Company lookup driver: cookie save failed", exc_info=True)
            try:
                company_lookup_driver.quit()
            except Exception:
                pass
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
    sync_download_output(cover_letter_modes=cover_modes)
    prune_cover_letters_for_sync(cover_letter_modes=cover_modes)
    migrate_legacy_consulting_companies_file()
    migrate_legacy_root_archive_files()
    migrate_legacy_cover_letter_layout()
    migrate_form_fill_rules()
    migrate_extension_auto_rules_to_exact()

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
        "and sync ``output/`` (consulting memory + filter cover letters) via S3 when configured. "
        "Uses --max-applies as a cap on successful saves (0 = no cap).",
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
        args.headless = v in ("1", "true", "yes")

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

        run(args, timing, paths, behavior, search)
    finally:
        cover_modes = _cover_letter_modes_for_run(site=args.site, filter_mode=args.filter)
        prune_cover_letters_for_sync(cover_letter_modes=cover_modes)
        sync_upload_output(cover_letter_modes=cover_modes)


if __name__ == "__main__":
    main()
