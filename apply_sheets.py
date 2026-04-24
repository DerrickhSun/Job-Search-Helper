"""
Append successful job applications to a Google Sheet tab named ``{year} Auto`` (e.g. ``2026 Auto``).

Requires a Google Cloud **service account** JSON key with Sheets API enabled. Share the spreadsheet
with the service account email (``client_email`` in the JSON) as **Editor**.

Environment (optional if you pass CLI flags):
  - ``GOOGLE_SHEETS_CREDENTIALS`` — path to the service account JSON file
  - ``GOOGLE_SHEETS_SPREADSHEET_ID`` — spreadsheet id (defaults to the project sheet)
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# Default: spreadsheet from user config; override with GOOGLE_SHEETS_SPREADSHEET_ID
DEFAULT_SPREADSHEET_ID = "1EPkvbfDqhp0kIvA1A_kc4jBB-gZXiRu1JMUYrSo6aso"

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


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


def _worksheet_title_for_today() -> str:
    return f"{datetime.now().year} Auto"


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


def append_applied_job_row(
    job: dict[str, Any],
    *,
    credentials_path: Path | str | None = None,
    spreadsheet_id: str | None = None,
    applied_at_iso: str | None = None,
) -> None:
    """
    Append one row for a successful apply.

    Col A empty, B company, C empty, D apply date (local ``MM/DD/YYYY``, same basis as ``applications.csv``),
    E job URL, F job title. Pass ``applied_at_iso`` from ``tracker.log`` so the sheet matches the DB row.
    """
    path = credentials_path or os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not path:
        log.debug("Google Sheets: set GOOGLE_SHEETS_CREDENTIALS or pass credentials_path — skipping")
        return
    cred_path = Path(path)
    if not cred_path.is_file():
        log.warning("Google Sheets: credentials file not found: %s", cred_path)
        return

    sid = (spreadsheet_id or os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID).strip()

    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from gspread.exceptions import WorksheetNotFound
    except ImportError as e:
        log.warning("Google Sheets: install gspread and google-auth (%s)", e)
        return

    try:
        creds = Credentials.from_service_account_file(str(cred_path), scopes=_SCOPES)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sid)
        tab = _worksheet_title_for_today()
        try:
            ws = sh.worksheet(tab)
        except WorksheetNotFound:
            ws = sh.add_worksheet(title=tab, rows=2000, cols=8)
            log.info("Google Sheets: created worksheet %r", tab)

        iso = applied_at_iso or datetime.now(timezone.utc).isoformat()
        row = applied_sheet_row(job, format_apply_date_mdy(iso))
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("Google Sheets: logged apply to %r — %s at %s", tab, job.get("title"), job.get("company"))
    except Exception as e:
        log.warning("Google Sheets: could not append row: %s", e)
