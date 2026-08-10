"""
Reconcile this machine's tracking CSVs with S3 — no scraping, no applying.

Use this when you have applied on more than one computer and want every machine (and S3) to converge on
the same history without losing rows. It is intentionally side-effect free beyond the CSV sync below.

Steps (see docs/s3_outputs.md for the underlying S3 helpers):

1. Snapshot the local CSVs (long-term archives + short-term active files) *before* downloading, because
   ``sync_download_output`` overwrites local files with the S3 copy.
2. Download ``output/`` from S3 (this machine now holds the shared S3 state).
3. **Merge long-term memory:** union the pre-download local archive rows with the S3 archive rows and
   rewrite the archive CSVs.
4. Build the merged long-term key set from those archives.
5. **Prune short-term memory:** for ``applications.csv`` and ``assisted_applications.csv``, union the local
   snapshot with the S3 copy, then drop any row already present in long-term memory.
6. **Merge the remainder of short-term memory** back to disk and upload ``output/`` to S3.

Run from repo root::

    python sync.py

When ``S3_OUTPUT_BUCKET`` is unset the download/upload steps are no-ops, so this still safely merges the
local archive into the local short-term files.
"""

from __future__ import annotations

from dotenv import load_dotenv

from utils.job_records import warn_if_listings_log_sidecars
from utils.output_cleanup import prune_cover_letters_for_sync
from utils.output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    migrate_legacy_consulting_companies_file,
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


def main() -> None:
    load_dotenv()

    # 1. Snapshot local state before download overwrites it with the S3 copy.
    local_app_archive = read_sheet_csv(APPLICATIONS_ARCHIVE_CSV)
    local_assisted_archive = read_sheet_csv(ASSISTED_APPLICATIONS_HISTORY_CSV)
    local_applications = read_sheet_csv(APPLICATIONS_CSV)
    local_assisted = read_sheet_csv(ASSISTED_APPLICATIONS_CSV)

    # 2. Pull the shared S3 state (overwrites the local files read above).
    sync_download_output()
    prune_cover_letters_for_sync()
    warn_if_listings_log_sidecars()
    migrate_legacy_consulting_companies_file()
    migrate_legacy_root_archive_files()
    migrate_form_fill_rules()

    s3_app_archive = read_sheet_csv(APPLICATIONS_ARCHIVE_CSV)
    s3_assisted_archive = read_sheet_csv(ASSISTED_APPLICATIONS_HISTORY_CSV)
    s3_applications = read_sheet_csv(APPLICATIONS_CSV)
    s3_assisted = read_sheet_csv(ASSISTED_APPLICATIONS_CSV)

    # 3. Merge long-term memory (S3 archive ∪ local archive) and rewrite the archives.
    app_archive_rows = union_sheet_rows(s3_app_archive[1], local_app_archive[1])
    assisted_archive_rows = union_sheet_rows(s3_assisted_archive[1], local_assisted_archive[1])
    app_archive_header = s3_app_archive[0] or local_app_archive[0]
    assisted_archive_header = s3_assisted_archive[0] or local_assisted_archive[0]
    write_sheet_csv(APPLICATIONS_ARCHIVE_CSV, app_archive_header, app_archive_rows)
    write_sheet_csv(ASSISTED_APPLICATIONS_HISTORY_CSV, assisted_archive_header, assisted_archive_rows)
    print(
        f"Long-term: applications archive {len(app_archive_rows)} row(s), "
        f"assisted archive {len(assisted_archive_rows)} row(s)."
    )

    # 4. Merged long-term key set (jobs we already consider 'remembered').
    long_term_keys = sheet_row_keys(app_archive_rows) | sheet_row_keys(assisted_archive_rows)

    # 5/6. For each short-term file: union local+S3, drop rows already in long-term, rewrite, upload.
    for label, path, local_snap, s3_snap in (
        ("applications", APPLICATIONS_CSV, local_applications, s3_applications),
        ("assisted", ASSISTED_APPLICATIONS_CSV, local_assisted, s3_assisted),
    ):
        merged = union_sheet_rows(s3_snap[1], local_snap[1])
        remaining = [row for row in merged if sheet_row_key(row) not in long_term_keys]
        dropped = len(merged) - len(remaining)
        header = s3_snap[0] or local_snap[0]
        write_sheet_csv(path, header, remaining)
        print(
            f"Short-term ({label}): {len(merged)} merged, {dropped} already in long-term, "
            f"{len(remaining)} kept -> {path.resolve()}"
        )

    prune_cover_letters_for_sync()
    sync_upload_output()


if __name__ == "__main__":
    main()
