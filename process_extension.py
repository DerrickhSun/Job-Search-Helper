"""
Read job data exported by the browser extension from the user's Downloads folder.

Imports saved jobs into ``output/assisted_applications.csv`` and merges screening answers into
``output/form_fill_rules/auto_rules.json`` (with interactive conflict resolution).

The extension writes:
  - saved_jobs.txt — one job per line: ``company, title, url`` or ``company, YYYY-MM-DD, title, url``
  - saved_jobs_application_questions.txt (or saved_job_application_questions.txt)

Run from repo root::

    python process_extension.py
    python process_extension.py --downloads-dir "C:\\Users\\you\\Downloads"
    python process_extension.py --print-only
    python process_extension.py --dry-run
    python process_extension.py --skip-jobs
    python process_extension.py --skip-questions
    python process_extension.py --no-interactive
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from utils.apply_sheets import applied_sheet_row
from utils.extension_rules import (
    SAVED_JOBS_QUESTIONS_FILENAME,
    migrate_extension_auto_rules_to_exact,
    process_extension_questions,
    print_questions_summary,
    resolve_questions_file_path,
)
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.output_paths import (
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    migrate_form_fill_rules,
    migrate_legacy_root_archive_files,
)
from utils.s3_outputs import sync_download_output, sync_upload_output
from utils.sheet_csv import (
    read_sheet_csv,
    sheet_row_key,
    sheet_row_keys,
    union_sheet_rows,
    write_sheet_csv,
)

SAVED_JOBS_FILENAME = "saved_jobs.txt"

_LINE_URL_RE = re.compile(r"(https?://\S+)\s*$")
_EXTENSION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _extension_date_to_mdy(raw: str) -> str | None:
    s = (raw or "").strip()
    if not _EXTENSION_DATE_RE.match(s):
        return None
    y, m, d = s.split("-")
    return f"{int(m):02d}/{int(d):02d}/{y}"


def parse_saved_job_line(line: str) -> dict[str, Any] | None:
    """
    Parse one extension line into ``{company, title, url, date_mdy}``.

    Supported layouts (URL is always last)::

        Company, Job Title, https://...
        Company, 2026-06-12, Job Title, https://...
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    m = _LINE_URL_RE.search(text)
    if not m:
        return None
    url = m.group(1).rstrip("/")
    prefix = text[: m.start()].rstrip().rstrip(",")
    if not prefix:
        return None

    parts = [p.strip() for p in prefix.split(",") if p.strip()]
    if len(parts) < 2:
        return None

    company = parts[0]
    date_mdy: str | None = None
    if len(parts) == 2:
        title = parts[1]
    elif len(parts) == 3 and _EXTENSION_DATE_RE.match(parts[1]):
        date_mdy = _extension_date_to_mdy(parts[1])
        title = parts[2]
    else:
        if _EXTENSION_DATE_RE.match(parts[1]):
            date_mdy = _extension_date_to_mdy(parts[1])
            title = ", ".join(parts[2:])
        else:
            title = ", ".join(parts[1:])

    if not company or not title or not url:
        return None
    if date_mdy is None:
        date_mdy = date.today().strftime("%m/%d/%Y")
    return {
        "company": company,
        "title": title,
        "url": url,
        "date_mdy": date_mdy,
    }


def parse_saved_jobs_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(parsed_jobs, invalid_lines)``."""
    jobs: list[dict[str, Any]] = []
    invalid: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        parsed = parse_saved_job_line(line)
        if parsed is None:
            invalid.append(line)
        else:
            jobs.append(parsed)
    return jobs, invalid


def parse_saved_jobs_file(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        return [], []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return parse_saved_jobs_text(text)


def import_saved_jobs_to_assisted(
    jobs: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[int, int, list[dict[str, Any]]]:
    """
    Append new jobs to ``output/assisted_applications.csv``.

    Skips rows whose URL already appears in the active or archive assisted CSVs.
    Returns ``(added_count, skipped_count, skipped_jobs)``.
    """
    migrate_legacy_root_archive_files()
    header, active = read_sheet_csv(ASSISTED_APPLICATIONS_CSV)
    _, history = read_sheet_csv(ASSISTED_APPLICATIONS_HISTORY_CSV)
    seen = sheet_row_keys(history) | sheet_row_keys(active)

    to_add: list[list[str]] = []
    skipped_jobs: list[dict[str, Any]] = []
    for job in jobs:
        row = applied_sheet_row(job, job.get("date_mdy"))
        key = sheet_row_key(row)
        if key and key in seen:
            skipped_jobs.append(job)
            continue
        to_add.append(row)
        if key:
            seen.add(key)

    if to_add and not dry_run:
        ASSISTED_APPLICATIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
        write_sheet_csv(ASSISTED_APPLICATIONS_CSV, header, union_sheet_rows(active, to_add))

    return len(to_add), len(skipped_jobs), skipped_jobs


def _print_file(path: Path, *, label: str) -> bool:
    """Print file contents or a missing-file notice. Returns True when the file existed."""
    print(f"=== {label} ===")
    print(f"Path: {path.resolve()}")
    if not path.is_file():
        print(f"Not found: {path.name} does not exist at this location.")
        print()
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("(file is empty)")
    else:
        print(text.rstrip())
    print()
    return True


def _print_jobs_import_summary(
    *,
    parsed: list[dict[str, Any]],
    invalid: list[str],
    added: int,
    skipped: int,
    skipped_jobs: list[dict[str, Any]],
    dry_run: bool,
) -> None:
    print("=== Jobs import summary ===")
    print(f"Parsed {len(parsed)} job(s) from {SAVED_JOBS_FILENAME}")
    if invalid:
        print(f"Skipped {len(invalid)} unparseable line(s):")
        for line in invalid:
            print(f"  • {line}")
    if dry_run:
        print("Dry run — no CSV changes written.")
    print(f"Added {added} row(s) to {ASSISTED_APPLICATIONS_CSV.as_posix()}")
    print(f"Skipped {skipped} duplicate(s) already in assisted CSV or archive")
    if skipped_jobs:
        for job in skipped_jobs:
            print(f"  • {job.get('title')} at {job.get('company')} ({job.get('url')})")
    if added and not dry_run:
        print(f"Wrote: {ASSISTED_APPLICATIONS_CSV.resolve()}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import extension exports: assisted applications CSV and form fill rules."
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Folder containing extension export files (default: your Downloads folder).",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Only print extension files; do not import.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report actions without writing CSV or rule files.",
    )
    parser.add_argument(
        "--skip-jobs",
        action="store_true",
        help="Do not import saved_jobs.txt into assisted_applications.csv.",
    )
    parser.add_argument(
        "--skip-questions",
        action="store_true",
        help="Do not process saved_jobs_application_questions.txt.",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Do not prompt for conflict resolution; list conflicts only.",
    )
    args = parser.parse_args()

    load_dotenv()
    sync_download_output()
    prune_cover_letters_for_sync()
    migrate_legacy_root_archive_files()
    migrate_form_fill_rules()
    migrated = migrate_extension_auto_rules_to_exact(dry_run=args.dry_run)
    if migrated:
        print(f"Migrated {migrated} extension auto rule(s) to exact label matching.")

    downloads = (args.downloads_dir or _default_downloads_dir()).expanduser().resolve()
    if not downloads.is_dir():
        print(f"Downloads folder not found: {downloads}", file=sys.stderr)
        return 1

    print(f"Looking in: {downloads}\n")

    saved_jobs_path = downloads / SAVED_JOBS_FILENAME
    questions_path = resolve_questions_file_path(downloads)
    found_jobs_file = saved_jobs_path.is_file()
    found_questions_file = questions_path.is_file()

    if args.print_only:
        _print_file(saved_jobs_path, label=SAVED_JOBS_FILENAME)
        _print_file(questions_path, label=SAVED_JOBS_QUESTIONS_FILENAME)
        if not found_jobs_file and not found_questions_file:
            print("No extension export files found.")
            return 1
        return 0

    if not found_jobs_file and not found_questions_file:
        print("No extension export files found.")
        return 1

    exit_code = 0

    if not args.skip_jobs and found_jobs_file:
        parsed, invalid = parse_saved_jobs_file(saved_jobs_path)
        if not parsed:
            print(f"No parseable jobs in {SAVED_JOBS_FILENAME}.")
            if invalid:
                print("Unparseable lines:")
                for line in invalid:
                    print(f"  • {line}")
            exit_code = 1
        else:
            added, skipped, skipped_jobs = import_saved_jobs_to_assisted(parsed, dry_run=args.dry_run)
            _print_jobs_import_summary(
                parsed=parsed,
                invalid=invalid,
                added=added,
                skipped=skipped,
                skipped_jobs=skipped_jobs,
                dry_run=args.dry_run,
            )
    elif not args.skip_jobs:
        print(f"=== {SAVED_JOBS_FILENAME} ===")
        print(f"Not found: {saved_jobs_path.resolve()}")
        print()

    if not args.skip_questions:
        if found_questions_file:
            q_result = process_extension_questions(
                questions_path,
                dry_run=args.dry_run,
                interactive=not args.no_interactive,
            )
            print_questions_summary(questions_path, q_result, dry_run=args.dry_run)
            if q_result.conflicts and (args.dry_run or args.no_interactive):
                print("Re-run without --no-interactive to resolve conflicts.")
        else:
            print(f"=== {SAVED_JOBS_QUESTIONS_FILENAME} ===")
            print(f"Not found: {questions_path.resolve()}")
            print()

    if not args.print_only and not args.dry_run:
        prune_cover_letters_for_sync()
        sync_upload_output()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
