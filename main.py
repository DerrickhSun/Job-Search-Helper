"""
LinkedIn Auto-Apply Bot
Run: python main.py --resume resume.pdf --keywords "software engineer" --location "United States"
"""

import argparse
import logging
from pathlib import Path

from apply_sheets import append_applied_job_row
from cover_letter import CoverLetterGenerator
from dspy_lm import configure_dspy
from form_filler import EasyApplyFiller
from job_records import DEFAULT_LISTINGS_LOG
from job_searcher import JobSearcher
from matcher import JobMatcher
from resume_cache import DEFAULT_RESUME_CACHE_PATH, load_or_build_resume
from resume_parser import ResumeParser, first_name_from_resume
from tracker import ApplicationTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log"),
    ],
)
log = logging.getLogger(__name__)


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
        **kwargs,
    )


def run(args):
    # 0 = unlimited listings; positive int = cap (default 100).
    max_jobs_cap: int | None = None if args.max_jobs <= 0 else args.max_jobs

    if args.debug_jobs_page:
        log.info(
            "Debug mode: login + job search URL only (no scraping, matching, or applies). "
            "Pass --resume to use your first name for login detection."
        )
        account_first = None
        if args.resume and Path(args.resume).exists():
            resume_dbg = ResumeParser().parse(args.resume)
            account_first = first_name_from_resume(resume_dbg)
            log.info("Using first name %r from resume for login detection", account_first)
        searcher = _job_searcher_from_args(
            args,
            pause_after_navigate=True,
            account_first_name=account_first,
        )
        searcher.search(
            keywords=args.keywords,
            location=args.location,
            max_jobs=max_jobs_cap,
            easy_apply_only=args.easy_apply_only,
        )
        log.info("Debug session finished.")
        return

    configure_dspy()

    # 1. Load resume profile (JSON cache or parse PDF/DOCX)
    resume = load_or_build_resume(
        Path(args.resume) if args.resume else None,
        Path(args.resume_cache),
        force_reparse=args.force_resume_parse,
    )
    log.info("Profile: %d skills, %d roles", len(resume["skills"]), len(resume["experience"]))

    # 2. Search for jobs (Selenium + Chrome; visible by default)
    log.info("Searching LinkedIn for: %s in %s", args.keywords, args.location)
    if args.easy_apply_only:
        log.info("Job search filter: Easy Apply only (LinkedIn f_AL).")
    else:
        log.info(
            "Job search filter: all listings (no Easy Apply URL filter). "
            "Cards without Easy Apply are skipped for apply until other flows exist."
        )
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
    filler = EasyApplyFiller(
        headless=args.headless,
        step_delay=args.step_delay,
        highlight=not args.no_highlight,
        easy_apply_wait_seconds=args.easy_apply_wait,
        apply_click_gap_seconds=args.apply_click_gap,
        apply_review_pause_after_fill_seconds=args.apply_review_pause,
        cover_letter_docx_dir=args.cover_letter_dir,
    )

    searcher = _job_searcher_from_args(args, account_first_name=account_first)
    if max_jobs_cap is None:
        log.info(
            "No job listing cap (--max-jobs 0): processing until this search has no more pages or cards."
        )
    else:
        log.info(
            "Will evaluate up to %d job listing(s) this run (default cap 100; use --max-jobs 0 for no limit).",
            max_jobs_cap,
        )

    def process_listing(driver, job: dict) -> None:
        if tracker.already_applied(job["id"]):
            log.info("Skipping (already applied): %s at %s", job["title"], job["company"])
            return
        if not job.get("easy_apply"):
            log.info(
                "Skipping (no Easy Apply on card — external apply not implemented yet): %s at %s",
                job["title"],
                job["company"],
            )
            tracker.log(job, status="skipped", score=0.0)
            return

        score = matcher.score(resume, job)
        log.info("Score %.0f%%: %s at %s", score * 100, job["title"], job["company"])

        if score < args.min_score:
            log.info("  → Below threshold (%.0f%%), skipping apply", args.min_score * 100)
            tracker.log(job, status="skipped", score=score)
            return

        cover_letter = cover_gen.generate(resume, job)
        log.info("  → Easy Apply (same browser session)...")
        success = filler.apply(job, resume, cover_letter, driver=driver)
        status = "applied" if success else "failed"
        tracker.log(job, status=status, score=score, cover_letter=cover_letter)

        if success:
            log.info("  ✓ Applied successfully!")
            append_applied_job_row(
                job,
                credentials_path=args.google_sheets_credentials,
                spreadsheet_id=args.google_spreadsheet_id,
            )
        else:
            log.warning("  ✗ Application failed — check output/screenshots/")

    processed = searcher.run_search_apply_pipeline(
        keywords=args.keywords,
        location=args.location,
        max_jobs=max_jobs_cap,
        easy_apply_only=args.easy_apply_only,
        listings_log_path=args.listings_log,
        process_listing=process_listing,
    )
    log.info("Finished search pipeline: %d listing(s) processed (see %s).", processed, args.listings_log)

    # Export summary
    tracker.export_csv("output/applications.csv")
    log.info("Done. Summary exported to output/applications.csv")


def main():
    ap = argparse.ArgumentParser(description="LinkedIn Easy Apply bot")
    ap.add_argument(
        "--resume",
        default=None,
        help="Path to your resume PDF or DOCX (required on first run or with --force-resume-parse; "
        "optional if data/resume_profile.json already exists).",
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
    ap.add_argument("--keywords", default="software engineer", help="Job search keywords")
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
        "--max-jobs",
        type=int,
        default=100,
        metavar="N",
        help="Max job listings to walk through per run (default: 100). "
        "Use 0 for no limit. LinkedIn shows ~25 per page when more pages exist.",
    )
    ap.add_argument("--min-score", type=float, default=0.6, help="Minimum match score 0-1 (default: 0.6)")
    ap.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode (no window). Default is a visible browser for debugging.",
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
        "--debug-jobs-page",
        action="store_true",
        help="Open LinkedIn job search after login, then wait for Enter; skips resume, scraping, and applies.",
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
    args = ap.parse_args()

    if not args.debug_jobs_page:
        cache_p = Path(args.resume_cache)
        need_resume_file = args.force_resume_parse or not cache_p.is_file()
        if need_resume_file:
            if not args.resume:
                raise SystemExit(
                    "error: --resume is required when the profile JSON does not exist yet "
                    "or when using --force-resume-parse (unless --debug-jobs-page)"
                )
            if not Path(args.resume).exists():
                raise FileNotFoundError(f"Resume not found: {args.resume}")

    run(args)


if __name__ == "__main__":
    main()
