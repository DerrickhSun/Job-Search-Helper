"""
Append data rows from active CSV(s) into archive history CSV(s), then reset each active file to
header-only.

**Default:** archives both ``output/applications.csv`` and ``output/assisted_applications.csv``.

LinkedIn dedupe still treats archived application rows as prior applies:
:class:`tracker.ApplicationTracker.already_applied` reads both archive paths in addition to
``data/applications.db``.

Run from inside server/::

    python archive_applications.py
    python archive_applications.py --applications-only
    python archive_applications.py --assisted-only

When ``S3_OUTPUT_BUCKET`` is set in ``.env``, downloads ``output/`` from S3 before archiving and
uploads after (same as ``main.py``; see docs/s3_outputs.md).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from utils.output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    migrate_legacy_consulting_companies_file,
    migrate_form_fill_rules,
    migrate_legacy_root_archive_files,
)
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.s3_log_sync import PendingChangeTracker
from utils.s3_outputs import sync_download_output_coordinated, sync_upload_output_coordinated
from utils.sheet_csv import read_sheet_csv, sort_sheet_rows_by_date, union_sheet_rows, write_sheet_csv


def _archive_one(*, active_csv: Path, history_csv: Path) -> int:
    """
    Merge ``active_csv`` rows into ``history_csv`` (deduped by job URL, same process as ``sync.py``),
    then reset ``active_csv`` to header only. Returns the number of newly-added (non-duplicate) rows.
    """
    active_header, active_rows = read_sheet_csv(active_csv)
    history_header, history_rows = read_sheet_csv(history_csv)

    merged = union_sheet_rows(history_rows, active_rows)
    added = len(merged) - len(history_rows)
    if added:
        merged = sort_sheet_rows_by_date(merged)

    write_sheet_csv(history_csv, history_header or active_header, merged)
    write_sheet_csv(active_csv, active_header, [])

    return added


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
    cover_letter_changes = PendingChangeTracker()
    # Held through the upload in the finally block below — the archive merge in between is a
    # read-modify-write over the whole CSV, so it needs the same lock scope as sync.py's merge.
    lock_token = sync_download_output_coordinated()
    prune_cover_letters_for_sync(tracker=cover_letter_changes)
    migrate_legacy_consulting_companies_file()
    migrate_legacy_root_archive_files()
    migrate_form_fill_rules()

    do_applications = not args.assisted_only
    do_assisted = not args.applications_only

    try:
        if do_applications:
            n = _archive_one(active_csv=APPLICATIONS_CSV, history_csv=APPLICATIONS_ARCHIVE_CSV)
            print(f"Applications: archived {n} new (deduped) row(s) -> {APPLICATIONS_ARCHIVE_CSV.resolve()}")
            print(f"Applications: reset {APPLICATIONS_CSV.resolve()} to header only.")

        if do_assisted:
            n = _archive_one(active_csv=ASSISTED_APPLICATIONS_CSV, history_csv=ASSISTED_APPLICATIONS_HISTORY_CSV)
            print(f"Assisted: archived {n} new (deduped) row(s) -> {ASSISTED_APPLICATIONS_HISTORY_CSV.resolve()}")
            print(f"Assisted: reset {ASSISTED_APPLICATIONS_CSV.resolve()} to header only.")
    finally:
        prune_cover_letters_for_sync(tracker=cover_letter_changes)
        sync_upload_output_coordinated(lock_token=lock_token, cover_letter_changes=cover_letter_changes)


if __name__ == "__main__":
    main()
