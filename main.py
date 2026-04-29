"""
LinkedIn Auto-Apply Bot
Run: python main.py --location "United States"
Default resume path is resume.pdf in the working directory; use --resume PATH to override.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles often use cp1252; resume/cover text may contain Unicode (e.g. bullets). UTF-8 avoids
# UnicodeEncodeError when logging DEBUG lines from third-party libraries (e.g. OpenAI request bodies).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from apply_sheets import append_applied_job_row
from company_blacklist import is_company_blacklisted, load_company_blacklist
from consulting_filter import is_consulting_listing
from cover_letter import CoverLetterGenerator
from dspy_lm import configure_dspy
from form_filler import DEFAULT_HEADSHOT_IMAGE, EasyApplyFiller
from helper_browser import run_helper_mode
from job_records import DEFAULT_LISTINGS_LOG
from job_searcher import JobSearcher
from matcher import JobMatcher, print_job_fit_debug
from resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE, load_or_build_resume
from resume_parser import ResumeParser, first_name_from_resume
from tracker import ApplicationTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Default search queries: when one runs out of LinkedIn pages, the next is used in the same session.
DEFAULT_KEYWORDS = (
    "software engineer",
    "ai",
    "data scientist",
    "data analyst",
)


def _job_searcher_from_args(args, **kwargs):
    """Shared Selenium timing knobs (all fixed sleeps / polling, no WebDriverWait)."""
    return JobSearcher(
        headless=args.headless,
        step_delay=args.step_delay,
        highlight=not args.no_highlight,
        job_cards_wait_seconds=args.job_cards_wait,
        login_form_wait_seconds=args.login_form_wait,
        next_page_wait_seconds=args.next_page_wait,
        job_description_wait_seconds=args.job_desc_wait,
        login_complete_max_seconds=args.login_poll_max,
        login_complete_max_seconds_no_checkpoint=args.login_poll_max_no_checkpoint,
        posted_within_24h=args.posted_within_24h,
        **kwargs,
    )


def run(args):
    if args.headless:
        log.info("Chrome: headless (no window).")
    else:
        log.info("Chrome: visible window (use --headless or JOB_APPLIER_HEADLESS=1 to hide).")

    # 0 = unlimited listings evaluated; positive int = cap on how many cards we open.
    max_listings_cap: int | None = None if args.max_jobs <= 0 else args.max_jobs
    # 0 = no cap on successful applies; positive int = stop after that many successful applies.
    max_applies_cap: int | None = None if args.max_applies <= 0 else args.max_applies

    if args.debug_jobs_page and args.helper:
        raise SystemExit("error: use either --debug-jobs-page or --helper, not both")

    if args.debug_jobs_page:
        log.info(
            "Debug mode: login + job search URL only (no scraping, matching, or applies). "
            "Pass --resume to use your first name for login detection."
        )
        account_first = None
        if Path(args.resume).exists():
            resume_dbg = ResumeParser().parse(str(args.resume))
            account_first = first_name_from_resume(resume_dbg)
            log.info("Using first name %r from resume for login detection", account_first)
        searcher = _job_searcher_from_args(
            args,
            pause_after_navigate=True,
            account_first_name=account_first,
        )
        searcher.search(
            keywords=args.keywords[0],
            location=args.location,
            max_jobs=max_listings_cap,
            easy_apply_only=args.easy_apply_only,
        )
        log.info("Debug session finished.")
        return

    if args.helper:
        configure_dspy()
        resume = load_or_build_resume(
            Path(args.resume),
            Path(args.resume_cache),
            force_reparse=args.force_resume_parse,
        )
        log.info(
            "Helper mode: browse LinkedIn and apply yourself; the bot assists Easy Apply fields when the "
            "modal is open. Type r + Enter here to record an apply, q + Enter to quit."
        )
        log.info("Profile: %d skills, %d roles", len(resume["skills"]), len(resume["experience"]))
        if args.easy_apply_only:
            log.info("Job search filter: Easy Apply only (LinkedIn f_AL).")
        else:
            log.info("Job search filter: all listings (no f_AL).")
        if args.posted_within_24h:
            log.info("Job search filter: posted in the past 24 hours (LinkedIn f_TPR=r86400).")
        else:
            log.info("Job search filter: any date posted (no f_TPR).")
        if args.headless:
            log.warning("Helper mode usually needs a visible window — omit --headless to see the browser.")
        tracker = ApplicationTracker("data/applications.db")
        account_first = first_name_from_resume(resume)
        matcher = JobMatcher()
        cover_gen = CoverLetterGenerator()
        run_helper_mode(
            args=args,
            resume=resume,
            tracker=tracker,
            matcher=matcher,
            cover_gen=cover_gen,
            account_first=account_first,
        )
        return

    configure_dspy()

    # 1. Load resume profile (JSON cache or parse PDF/DOCX)
    resume = load_or_build_resume(
        Path(args.resume),
        Path(args.resume_cache),
        force_reparse=args.force_resume_parse,
    )
    log.info("Profile: %d skills, %d roles", len(resume["skills"]), len(resume["experience"]))

    # 2. Search for jobs (Selenium + Chrome; visible by default)
    log.info(
        "Searching LinkedIn in %s for keyword(s): %s",
        args.location,
        "; ".join(repr(k) for k in args.keywords),
    )
    if args.easy_apply_only:
        log.info("Job search filter: Easy Apply only (LinkedIn f_AL).")
    else:
        log.info(
            "Job search filter: all listings (no Easy Apply URL filter). "
            "Cards without Easy Apply are skipped for apply until other flows exist."
        )
    if args.posted_within_24h:
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
    company_blacklist = load_company_blacklist(args.company_blacklist)
    if company_blacklist:
        log.info("Company blacklist active: %d entr%s", len(company_blacklist), "y" if len(company_blacklist) == 1 else "ies")

    filler = EasyApplyFiller(
        headless=args.headless,
        step_delay=args.step_delay,
        highlight=not args.no_highlight,
        easy_apply_wait_seconds=args.easy_apply_wait,
        apply_click_gap_seconds=args.apply_click_gap,
        apply_review_pause_after_fill_seconds=args.apply_review_pause,
        cover_letter_docx_dir=args.cover_letter_dir,
        form_fill_rules_path=args.form_fill_rules,
        headshot_image_path=args.headshot,
    )

    searcher = _job_searcher_from_args(args, account_first_name=account_first)
    apply_stats = {"applied": 0}
    if max_applies_cap is not None:
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

    def process_listing(driver, job: dict) -> None:
        if tracker.already_applied(job["id"]):
            log.info("Skipping (already applied): %s at %s", job["title"], job["company"])
            return
        if is_company_blacklisted(job.get("company") or "", company_blacklist):
            log.info("Skipping (company blacklisted): %s at %s", job["title"], job["company"])
            tracker.log(job, status="blacklisted", score=0.0)
            return
        if args.skip_consulting and is_consulting_listing(job):
            log.info("Skipping (consulting / staffing indicators): %s at %s", job["title"], job["company"])
            tracker.log(job, status="consulting", score=0.0)
            return
        if not job.get("easy_apply"):
            log.info(
                "Skipping (no Easy Apply on card — external apply not implemented yet): %s at %s",
                job["title"],
                job["company"],
            )
            tracker.log(job, status="skipped", score=0.0)
            return

        if not matcher.gates_pass(resume, job):
            log.info(
                "Skipping (education or experience requirements not met): %s at %s",
                job["title"],
                job["company"],
            )
            print_job_fit_debug(job.get("company"), job.get("title"), None, note="gates_failed")
            tracker.log(job, status="skipped", score=0.0)
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

        cover_letter = cover_gen.generate(resume, job)
        log.info("  → Easy Apply (same browser session)...")
        success = filler.apply(job, resume, cover_letter, driver=driver)
        status = "applied" if success else "failed"
        applied_at = tracker.log(job, status=status, score=fit, cover_letter=cover_letter)

        if success:
            apply_stats["applied"] += 1
            log.info("  ✓ Applied successfully!")
            append_applied_job_row(
                job,
                credentials_path=args.google_sheets_credentials,
                spreadsheet_id=args.google_spreadsheet_id,
                applied_at_iso=applied_at,
            )
        else:
            log.warning("  ✗ Application failed — check output/screenshots/")

    processed = searcher.run_search_apply_pipeline(
        keywords=list(args.keywords),
        location=args.location,
        max_listings=max_listings_cap,
        easy_apply_only=args.easy_apply_only,
        listings_log_path=args.listings_log,
        process_listing=process_listing,
        max_applies=max_applies_cap,
        apply_counter=apply_stats,
    )
    log.info(
        "Finished search pipeline: %d listing(s) processed, %d successful apply(ies) (see %s).",
        processed,
        apply_stats["applied"],
        args.listings_log,
    )

    # Export summary (same 6-column sheet layout: company, date, LinkedIn job URL, title)
    tracker.export_csv("output/applications.csv")
    tracker.export_csv("output/apply_opened.csv", statuses=("apply_opened",))
    log.info("Done. Exports: output/applications.csv (applied), output/apply_opened.csv (external apply tab)")


def main():
    load_dotenv()

    ap = argparse.ArgumentParser(description="LinkedIn Easy Apply bot")
    ap.add_argument(
        "--resume",
        type=Path,
        default=DEFAULT_RESUME_FILE,
        metavar="PATH",
        help=f"Resume PDF or DOCX (default: {DEFAULT_RESUME_FILE}). "
        "If data/resume_profile.json exists, that cache is used unless --force-resume-parse.",
    )
    ap.add_argument(
        "--resume-cache",
        type=Path,
        default=DEFAULT_RESUME_CACHE_PATH,
        metavar="PATH",
        help=f"Read/write structured resume JSON (default: {DEFAULT_RESUME_CACHE_PATH}). "
        "If the file exists, the resume file is not parsed unless --force-resume-parse.",
    )
    ap.add_argument(
        "--force-resume-parse",
        action="store_true",
        help="Always parse --resume from disk and overwrite --resume-cache.",
    )
    ap.add_argument(
        "--keywords",
        nargs="*",
        default=None,
        metavar="TERM",
        help="LinkedIn job search queries (space-separated). When one query runs out of result pages, the "
        "next is used in the same browser session until --max-jobs is reached or all queries are exhausted. "
        f"Omit this flag to use the default list: {', '.join(DEFAULT_KEYWORDS)}.",
    )
    ap.add_argument(
        "--location",
        default="United States",
        help='LinkedIn job search location (default: "United States" — country-wide, not remote-only).',
    )
    ap.add_argument(
        "--easy-apply-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Restrict the LinkedIn search to Easy Apply jobs (default: on). "
        "Use --no-easy-apply-only to include all jobs; only Easy Apply is auto-applied for now.",
    )
    ap.add_argument(
        "--posted-within-24h",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Restrict the LinkedIn search to jobs posted in the past 24 hours (default: on; URL f_TPR=r86400, "
        'same as the "Past 24 hours" date filter). Use --no-posted-within-24h for any posting date.',
    )
    ap.add_argument(
        "--max-applies",
        type=int,
        default=100,
        metavar="N",
        help="Stop after N successful Easy Applies this run (default: 100). "
        "Use 0 for no apply cap (run until --max-jobs listings or end of search).",
    )
    ap.add_argument(
        "--max-jobs",
        type=int,
        default=0,
        metavar="N",
        help="Max job listings to open and evaluate per run (default: 0 = no cap). "
        "Use with --max-applies as a safety bound (e.g. --max-jobs 800 --max-applies 100). "
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
        "--step-delay",
        type=float,
        default=0.35,
        help="Seconds to pause between major UI steps when the window is visible (default: 0.35)",
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
        "--helper",
        action="store_true",
        help="Manual apply mode: open job search; you click jobs and submit applications. The bot fills "
        "recognized Easy Apply fields when the modal is open. In this terminal: r = record apply, q = quit.",
    )
    ap.add_argument(
        "--helper-poll",
        type=float,
        default=0.75,
        metavar="SEC",
        help="Seconds between background scans for the Easy Apply modal (default: 0.75).",
    )
    ap.add_argument(
        "--helper-review-pause",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Seconds to pause after each assisted field fill in helper mode (default: 0).",
    )
    ap.add_argument(
        "--helper-scan-all-tabs",
        action="store_true",
        help="Helper mode: scan every tab for Easy Apply / Workday (may briefly activate each tab in Chrome). "
        "Default checks only WebDriver's current tab (no tab switching). Use when apply opens Workday in a "
        "new tab; Selenium does not know which tab you last clicked.",
    )
    ap.add_argument(
        "--helper-downloads-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Helper mode: folder for cover letter .docx files when an external apply tab is detected "
        "(default: your Downloads folder).",
    )
    ap.add_argument(
        "--job-cards-wait",
        type=float,
        default=10.0,
        metavar="SEC",
        help="Seconds to sleep before reading job cards on each results page (default: 10).",
    )
    ap.add_argument(
        "--login-form-wait",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Seconds to sleep after opening /login before filling the form (default: 5).",
    )
    ap.add_argument(
        "--next-page-wait",
        type=float,
        default=3.0,
        metavar="SEC",
        help="Seconds to sleep before looking for the job search 'next page' button (default: 3).",
    )
    ap.add_argument(
        "--job-desc-wait",
        type=float,
        default=3.0,
        metavar="SEC",
        help="Seconds to sleep after clicking a job card before reading the description (default: 3).",
    )
    ap.add_argument(
        "--login-poll-max",
        type=float,
        default=120.0,
        metavar="SEC",
        help="Max seconds to poll (1s interval) for login after 2FA/checkpoint (default: 120).",
    )
    ap.add_argument(
        "--login-poll-max-no-checkpoint",
        type=float,
        default=30.0,
        metavar="SEC",
        help="Max seconds to poll for login after password submit when no checkpoint (default: 30).",
    )
    ap.add_argument(
        "--easy-apply-wait",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Seconds to sleep before clicking Easy Apply (default: 5).",
    )
    ap.add_argument(
        "--apply-click-gap",
        type=float,
        default=1.0,
        metavar="SEC",
        help="Seconds to wait after each Easy Apply button click (Apply / Next / Review / Submit / Done) "
        "for debugging (default: 1). Set to 0 for faster runs.",
    )
    ap.add_argument(
        "--apply-review-pause",
        type=float,
        default=10.0,
        metavar="SEC",
        help="Seconds to wait after filling an empty field (text, textarea, dropdown, radio) so you can "
        "review it (default: 10). Set to 0 to disable.",
    )
    ap.add_argument(
        "--listings-log",
        type=Path,
        default=DEFAULT_LISTINGS_LOG,
        help="Append one JSON line per parsed listing (default: data/listings_log.jsonl).",
    )
    ap.add_argument(
        "--cover-letter-dir",
        type=Path,
        default=Path("output/coverletters"),
        metavar="DIR",
        help='Save generated cover letter .docx files here when the form has "Upload cover letter" '
        "(default: output/coverletters).",
    )
    ap.add_argument(
        "--headshot",
        type=Path,
        default=DEFAULT_HEADSHOT_IMAGE,
        metavar="PATH",
        help="PNG/JPEG used when Easy Apply asks for a photo or headshot (default: data/selfInSuit.png).",
    )
    ap.add_argument(
        "--form-fill-rules",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON rules for screening questions and field fills (default: data/form_fill_rules.json).",
    )
    ap.add_argument(
        "--company-blacklist",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON file of company strings to never auto-apply to (default: data/company_blacklist.json). "
        "Matching ignores case and punctuation; see that file for the format.",
    )
    ap.add_argument(
        "--skip-consulting",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip jobs when the company name includes consulting (whole word) or the description suggests "
        "a consultancy/staffing employer (consultant, consulting firm/company, consultancy, client company, "
        "etc.; bare 'consulting' in the description is ignored to avoid industry-experience false positives). "
        "Default: on. Use --no-skip-consulting to disable.",
    )
    ap.add_argument(
        "--google-sheets-credentials",
        type=Path,
        default=None,
        metavar="PATH",
        help="Google service account JSON for logging successful applies to Sheets "
        "(or set GOOGLE_SHEETS_CREDENTIALS). If unset, sheet logging is skipped.",
    )
    ap.add_argument(
        "--google-spreadsheet-id",
        type=str,
        default=None,
        metavar="ID",
        help="Spreadsheet id (default: project sheet or GOOGLE_SHEETS_SPREADSHEET_ID env).",
    )
    ap.add_argument(
        "--export-csv",
        action="store_true",
        help="Write output/applications.csv and output/apply_opened.csv from data/applications.db and exit. "
        "Use when a run was interrupted (Ctrl+C) or you want CSVs to match the DB without re-scraping.",
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

    if args.export_csv:
        tracker = ApplicationTracker("data/applications.db")
        tracker.export_csv("output/applications.csv")
        tracker.export_csv("output/apply_opened.csv", statuses=("apply_opened",))
        log.info("Re-exported output/applications.csv and output/apply_opened.csv from SQLite.")
        return

    if not args.debug_jobs_page:
        cache_p = Path(args.resume_cache)
        need_resume_file = args.force_resume_parse or not cache_p.is_file()
        if need_resume_file and not Path(args.resume).exists():
            raise FileNotFoundError(
                f"Resume not found: {args.resume} — add this file or pass --resume PATH "
                "(needed when data/resume_profile.json is missing or with --force-resume-parse)."
            )

    run(args)


if __name__ == "__main__":
    main()
