"""
Shared read / merge / write helpers for the sheet-layout application CSVs.

Every tracking CSV — ``applications.csv``, ``assisted_applications.csv``, and their archive
counterparts — uses the same 6-column layout::

    ("", company, "", date, url, title)

These helpers give all entry points (``tracker.export_csv``, ``archive_applications.py``, ``sync.py``)
one consistent merge process: read rows, union them deduped by job-URL key, and write them back.
Keeping the logic here means dedupe behaves identically everywhere and matches ``already_applied``.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from .apply_sheets import _is_applications_sheet_header_row, sheet_export_url_dedupe_key

log = logging.getLogger(__name__)

SHEET_HEADER: tuple[str, ...] = ("", "company", "", "date", "url", "title")


def sheet_row_key(row: list[str]) -> str:
    """URL dedupe key for a sheet row (column E = index 4); empty when the row has no usable URL."""
    url = row[4] if len(row) > 4 else ""
    return sheet_export_url_dedupe_key(url or "")


def read_sheet_csv(path: Path | str) -> tuple[list[str], list[list[str]]]:
    """
    Return ``(header, data_rows)`` for a sheet-layout CSV.

    A missing/unreadable file yields the default header and no rows. Header and blank rows are skipped.
    """
    p = Path(path)
    header = list(SHEET_HEADER)
    data: list[list[str]] = []
    if not p.is_file():
        return header, data
    try:
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or not any((c or "").strip() for c in row):
                    continue
                if _is_applications_sheet_header_row(row):
                    header = row
                    continue
                data.append(row)
    except OSError as e:
        log.warning("Could not read sheet CSV %s: %s", p.resolve(), e)
        return list(SHEET_HEADER), []
    return header, data


def union_sheet_rows(*row_lists: list[list[str]]) -> list[list[str]]:
    """
    Concatenate row lists in order, dropping rows whose URL key was already seen.

    Rows with no usable URL key are always kept (they cannot be deduped). Earlier lists win, so pass the
    authoritative/shared rows first and this machine's new rows last.
    """
    seen: set[str] = set()
    out: list[list[str]] = []
    for rows in row_lists:
        for row in rows:
            k = sheet_row_key(row)
            if k:
                if k in seen:
                    continue
                seen.add(k)
            out.append(row)
    return out


def sheet_row_keys(rows: list[list[str]]) -> set[str]:
    """Set of non-empty URL dedupe keys for the given rows."""
    return {k for row in rows if (k := sheet_row_key(row))}


def write_sheet_csv(path: Path | str, header: list[str] | tuple[str, ...], rows: list[list[str]]) -> None:
    """Write ``header`` + ``rows`` to ``path`` in the sheet layout (creates parent dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(list(header))
        w.writerows(rows)
