"""
Shared row layout and CSV/URL utilities for application tracking.

Used by tracker.py, sheet_csv.py, process_extension.py, and greenhouse_session.py.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def format_apply_date_mdy(iso: str) -> str:
    """
    Turn a stored apply instant (ISO string, usually UTC from ``tracker.log``) into ``MM/DD/YYYY``
    in the **machine's local timezone** so CSV column D and Google Sheets match what calendar day
    you applied on locally.

    Legacy rows used naive ``datetime.utcnow().isoformat()`` — those are interpreted as UTC.
    """
    s = (iso or "").strip()
    if not s:
        return datetime.now(timezone.utc).astimezone().strftime("%m/%d/%Y")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m/%d/%Y")
    except ValueError:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            y, m, d = s[:10].split("-")
            return f"{int(m):02d}/{int(d):02d}/{y}"
        return date.today().strftime("%m/%d/%Y")


def applied_sheet_row(job: dict[str, Any], date_mdy: str | None = None) -> list[str]:
    """
    One row in the shared layout (Google Sheet + ``applications.csv``):

    A empty, B company, C empty, D date ``MM/DD/YYYY``, E job URL, F job title.
    """
    d = date_mdy if date_mdy is not None else date.today().strftime("%m/%d/%Y")
    return [
        "",
        (job.get("company") or "").strip(),
        "",
        d,
        (job.get("url") or "").strip(),
        (job.get("title") or "").strip(),
    ]


def _is_applications_sheet_header_row(row: list[str]) -> bool:
    if not row or len(row) < 2:
        return False
    return (row[0] or "").strip() == "" and (row[1] or "").strip().lower() == "company"


_SHEET_LINKEDIN_VIEW_ID_RE = re.compile(r"/jobs/view/(\d+)", re.I)
_SHEET_LINKEDIN_CURRENT_JOB_ID_RE = re.compile(r"[\?&]currentJobId=(\d+)", re.I)


def linkedin_job_id_from_sheet_job_url(url: str) -> str | None:
    """
    Parse LinkedIn numeric job id from column E of the applications sheet export
    (``/jobs/view/ID`` or ``currentJobId=`` on ``linkedin.com``).
    """
    u = (url or "").strip()
    if not u or "linkedin.com" not in u.lower():
        return None
    m = _SHEET_LINKEDIN_VIEW_ID_RE.search(u)
    if m:
        return m.group(1)
    m = _SHEET_LINKEDIN_CURRENT_JOB_ID_RE.search(u)
    if m:
        return m.group(1)
    return None


def linkedin_job_ids_from_applications_sheet_csvs(paths: Sequence[Path | str]) -> frozenset[str]:
    """
    Collect LinkedIn job ids from one or more CSV files in the sheet layout
    (``output/applications.csv``, ``output/archive/applications_archive.csv``, etc.).
    """
    out: set[str] = set()
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            continue
        try:
            with p.open(newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if not row or len(row) < 5:
                        continue
                    if _is_applications_sheet_header_row(row):
                        continue
                    jid = linkedin_job_id_from_sheet_job_url(row[4])
                    if jid:
                        out.add(jid)
        except Exception as e:
            log.warning("Could not read %s for LinkedIn apply dedupe: %s", p.resolve(), e)
    return frozenset(out)


def normalize_greenhouse_job_url(url: str) -> str:
    """
    Canonical comparison key for Greenhouse-related job URLs: scheme + host + path (lowercased),
    no query string or fragment — so the same job with different ``gh_src`` / token params still matches
    rows already stored in the applications DB.
    """
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        path = (p.path or "").rstrip("/")
        return f"{scheme}://{netloc}{path}".lower()
    except Exception:
        return u.lower()


def sheet_export_url_dedupe_key(url: str) -> str:
    """
    Stable key for matching an applications-sheet row (column E) to the archive / active CSV.
    LinkedIn uses numeric job id; Greenhouse uses :func:`normalize_greenhouse_job_url`; else full URL lowercased.
    """
    jid = linkedin_job_id_from_sheet_job_url(url)
    if jid:
        return f"li:{jid}"
    u = (url or "").strip()
    if not u:
        return ""
    if "greenhouse" in u.lower():
        k = normalize_greenhouse_job_url(u)
        return f"gh:{k}" if k else ""
    return f"u:{u.lower()}"


# Backwards-compatible private alias (older imports used the leading underscore).
_sheet_export_url_dedupe_key = sheet_export_url_dedupe_key
