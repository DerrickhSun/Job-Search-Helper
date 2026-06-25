"""
Look up saved application records by company name or job title.

Searches both active and archive CSVs:
  - output/applications.csv
  - output/archive/applications_archive.csv
  - output/assisted_applications.csv
  - output/archive/assisted_applications_history.csv

Any row whose company or title contains the query (case-insensitive) is printed.

Usage:
    python lookup_application.py
    python lookup_application.py "Acme Corp"
    python lookup_application.py "software engineer"
"""

from __future__ import annotations

import sys

from utils.output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
)
from utils.sheet_csv import read_sheet_csv

_SOURCES = [
    ("applications (active)",   APPLICATIONS_CSV),
    ("applications (archive)",  APPLICATIONS_ARCHIVE_CSV),
    ("assisted (active)",       ASSISTED_APPLICATIONS_CSV),
    ("assisted (archive)",      ASSISTED_APPLICATIONS_HISTORY_CSV),
]

# Column indices in the 6-column sheet layout: ("", company, "", date, url, title)
_COL_COMPANY = 1
_COL_DATE    = 3
_COL_URL     = 4
_COL_TITLE   = 5


def _search(query: str) -> list[tuple[str, list[str]]]:
    """Return (source_label, row) pairs for every row matching the query."""
    q = query.strip().lower()
    results: list[tuple[str, list[str]]] = []
    for label, path in _SOURCES:
        _, rows = read_sheet_csv(path)
        for row in rows:
            company = row[_COL_COMPANY] if len(row) > _COL_COMPANY else ""
            title   = row[_COL_TITLE]   if len(row) > _COL_TITLE   else ""
            if q in company.lower() or q in title.lower():
                results.append((label, row))
    return results


def _print_result(label: str, row: list[str]) -> None:
    company = row[_COL_COMPANY] if len(row) > _COL_COMPANY else ""
    title   = row[_COL_TITLE]   if len(row) > _COL_TITLE   else ""
    date    = row[_COL_DATE]    if len(row) > _COL_DATE    else ""
    url     = row[_COL_URL]     if len(row) > _COL_URL     else ""
    print(f"  [{label}]")
    print(f"    Company : {company or '—'}")
    print(f"    Title   : {title or '—'}")
    print(f"    Date    : {date or '—'}")
    print(f"    URL     : {url or '—'}")


def main() -> None:
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = input("Enter company name or job title to search: ").strip()

    if not query:
        print("No query entered.")
        return

    results = _search(query)

    if not results:
        print(f"No applications found matching '{query}'.")
        return

    print(f"\n{len(results)} match(es) for '{query}':\n")
    for label, row in results:
        _print_result(label, row)
        print()


if __name__ == "__main__":
    main()
