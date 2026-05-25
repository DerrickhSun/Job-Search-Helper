"""
Append data rows from active CSV(s) into archive history CSV(s), then reset each active file to
header-only.

**Default:** archives both ``output/applications.csv`` and ``output/assisted_applications.csv``.

LinkedIn dedupe still treats archived application rows as prior applies:
:class:`tracker.ApplicationTracker.already_applied` reads both archive paths in addition to
``data/applications.db``.

Run from repo root::

    python archive_applications.py
    python archive_applications.py --applications-only
    python archive_applications.py --assisted-only

When ``S3_OUTPUT_BUCKET`` is set in ``.env``, downloads ``output/`` from S3 before archiving and
uploads after (same as ``main.py``; see docs/s3_outputs.md).
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from dotenv import load_dotenv

from utils.output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    migrate_legacy_consulting_companies_file,
    migrate_legacy_root_archive_files,
)
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.s3_outputs import sync_download_output, sync_upload_output

HEADER = ("", "company", "", "date", "url", "title")


def _is_header_row(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return (row[0] or "").strip() == "" and (row[1] or "").strip().lower() == "company"


def _archive_one(*, active_csv: Path, history_csv: Path) -> int:
    """Append non-header rows from ``active_csv`` to ``history_csv``; reset ``active_csv`` to header only. Returns row count."""
    active_csv.parent.mkdir(parents=True, exist_ok=True)
    history_csv.parent.mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    if active_csv.is_file():
        with active_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

    header = list(HEADER)
    data_rows: list[list[str]] = []
    for row in rows:
        if not row:
            continue
        if _is_header_row(row):
            header = row
            continue
        data_rows.append(row)

    history_exists = history_csv.is_file() and history_csv.stat().st_size > 0
    with history_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not history_exists:
            w.writerow(header)
        w.writerows(data_rows)

    with active_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)

    return len(data_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move application tracking rows from active CSV(s) into archive CSV(s)."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--applications-only",
        action="store_true",
        help="Only archive output/applications.csv (skip assisted).",
    )
    group.add_argument(
        "--assisted-only",
        action="store_true",
        help="Only archive output/assisted_applications.csv (skip applications).",
    )
    args = parser.parse_args()

    load_dotenv()
    sync_download_output()
    prune_cover_letters_for_sync()
    migrate_legacy_consulting_companies_file()
    migrate_legacy_root_archive_files()

    do_applications = not args.assisted_only
    do_assisted = not args.applications_only

    try:
        if do_applications:
            n = _archive_one(active_csv=APPLICATIONS_CSV, history_csv=APPLICATIONS_ARCHIVE_CSV)
            print(f"Applications: archived {n} data row(s) -> {APPLICATIONS_ARCHIVE_CSV.resolve()}")
            print(f"Applications: reset {APPLICATIONS_CSV.resolve()} to header only.")

        if do_assisted:
            n = _archive_one(active_csv=ASSISTED_APPLICATIONS_CSV, history_csv=ASSISTED_APPLICATIONS_HISTORY_CSV)
            print(f"Assisted: archived {n} data row(s) -> {ASSISTED_APPLICATIONS_HISTORY_CSV.resolve()}")
            print(f"Assisted: reset {ASSISTED_APPLICATIONS_CSV.resolve()} to header only.")
    finally:
        prune_cover_letters_for_sync()
        sync_upload_output()


if __name__ == "__main__":
    main()
