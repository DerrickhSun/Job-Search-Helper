"""
Append rows from ``output/assisted_applications.csv`` into ``output/archive/assisted_applications_history.csv``,
then reset the active file to header-only.

Run from repo root: ``python archive_assisted_applications.py``
"""

from __future__ import annotations

import csv

from utils.output_paths import (
    ASSISTED_APPLICATIONS_HISTORY_CSV as HISTORY,
    ASSISTED_APPLICATIONS_CSV as ASSISTED,
    migrate_legacy_root_archive_files,
)

HEADER = ("", "company", "", "date", "url", "title")


def _is_header_row(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return (row[0] or "").strip() == "" and (row[1] or "").strip().lower() == "company"


def main() -> None:
    migrate_legacy_root_archive_files()
    ASSISTED.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)

    rows: list[list[str]] = []
    if ASSISTED.is_file():
        with ASSISTED.open(newline="", encoding="utf-8") as f:
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

    history_exists = HISTORY.is_file() and HISTORY.stat().st_size > 0
    with HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not history_exists:
            w.writerow(header)
        w.writerows(data_rows)

    with ASSISTED.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)

    print(f"Archived {len(data_rows)} data row(s) -> {HISTORY.resolve()}")
    print(f"Reset {ASSISTED.resolve()} to header only.")


if __name__ == "__main__":
    main()
