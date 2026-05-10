"""
Append rows from ``output/applications.csv`` into ``output/archive/applications_archive.csv``,
then reset the active file to header-only (same layout as ``archive_assisted_applications.py``).

LinkedIn dedupe still treats archived rows as prior applies: :meth:`tracker.ApplicationTracker.already_applied`
reads both CSV paths in addition to ``data/applications.db``.

Run from repo root: ``python archive_applications.py``
"""

from __future__ import annotations

import csv

from utils.output_paths import (
    APPLICATIONS_ARCHIVE_CSV as ARCHIVE,
    APPLICATIONS_CSV as APPLICATIONS,
    migrate_legacy_root_archive_files,
)

HEADER = ("", "company", "", "date", "url", "title")


def _is_header_row(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return (row[0] or "").strip() == "" and (row[1] or "").strip().lower() == "company"


def main() -> None:
    migrate_legacy_root_archive_files()
    APPLICATIONS.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    if APPLICATIONS.is_file():
        with APPLICATIONS.open(newline="", encoding="utf-8") as f:
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

    archive_exists = ARCHIVE.is_file() and ARCHIVE.stat().st_size > 0
    with ARCHIVE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not archive_exists:
            w.writerow(header)
        w.writerows(data_rows)

    with APPLICATIONS.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)

    print(f"Archived {len(data_rows)} data row(s) -> {ARCHIVE.resolve()}")
    print(f"Reset {APPLICATIONS.resolve()} to header only.")


if __name__ == "__main__":
    main()
