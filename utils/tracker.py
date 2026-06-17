"""
Application Tracker
Logs every job to SQLite and exports CSV in the same 6-column layout as Google Sheets
(A empty, B company, C empty, D date, E job URL, F title):

- ``applications.csv`` — status ``applied`` (manual ``r`` / successful auto-applies). Export omits rows
  whose job URL already appears in the archive CSV (same keys as ``already_applied``) so re-export after
  archiving does not refill ``applications.csv`` with jobs that would duplicate on the next archive.
- ``output/archive/applications_archive.csv`` — optional archive (see root ``archive_applications.py``,
  which also archives assisted rows by default); LinkedIn
  ``already_applied`` also matches job ids found in column E of this file
- ``apply_opened.csv`` — status ``apply_opened`` (legacy external-apply-tab captures)
- ``consulting`` — skipped for staffing / consulting heuristics (see ``consulting_filter``)
"""

import csv
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from .apply_sheets import (
    _is_applications_sheet_header_row,
    _sheet_export_url_dedupe_key,
    applied_sheet_row,
    format_apply_date_mdy,
    linkedin_job_ids_from_applications_sheet_csvs,
    normalize_greenhouse_job_url,
)
from .output_paths import APPLICATIONS_ARCHIVE_CSV, APPLICATIONS_CSV
from .sheet_csv import read_sheet_csv, union_sheet_rows, write_sheet_csv

log = logging.getLogger(__name__)

DEFAULT_APPLICATIONS_SHEET_CSV = APPLICATIONS_CSV
DEFAULT_APPLICATIONS_ARCHIVE_CSV = APPLICATIONS_ARCHIVE_CSV


def _archive_row_keys_for_export_dedupe(archive_csv: Path) -> frozenset[str]:
    keys: set[str] = set()
    if not archive_csv.is_file():
        return frozenset()
    try:
        with archive_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or len(row) < 5:
                    continue
                if _is_applications_sheet_header_row(row):
                    continue
                k = _sheet_export_url_dedupe_key(row[4])
                if k:
                    keys.add(k)
    except Exception as e:
        log.warning("Could not read archive for export dedupe %s: %s", archive_csv.resolve(), e)
        return frozenset()
    return frozenset(keys)


class ApplicationTracker:
    def __init__(
        self,
        db_path: str = "data/applications.db",
        *,
        linkedin_sheet_export_paths: Sequence[Path | str] | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
        self._linkedin_sheet_export_paths: tuple[Path, ...] = tuple(
            Path(p)
            for p in (
                linkedin_sheet_export_paths
                if linkedin_sheet_export_paths is not None
                else (DEFAULT_APPLICATIONS_SHEET_CSV, DEFAULT_APPLICATIONS_ARCHIVE_CSV)
            )
        )
        self._linkedin_ids_from_sheet_exports: frozenset[str] | None = None
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    id          TEXT PRIMARY KEY,
                    title       TEXT,
                    company     TEXT,
                    location    TEXT,
                    url         TEXT,
                    status      TEXT,
                    score       REAL,
                    cover_letter TEXT,
                    applied_at  TEXT
                )
            """)

    def _linkedin_applied_ids_from_sheet_exports(self) -> frozenset[str]:
        if self._linkedin_ids_from_sheet_exports is None:
            self._linkedin_ids_from_sheet_exports = linkedin_job_ids_from_applications_sheet_csvs(
                self._linkedin_sheet_export_paths
            )
        return self._linkedin_ids_from_sheet_exports

    def invalidate_linkedin_sheet_export_cache(self) -> None:
        """Clear cached ids if ``applications.csv`` / ``output/archive/`` files change during a long run."""
        self._linkedin_ids_from_sheet_exports = None

    def already_applied(self, job_id: str) -> bool:
        jid = (job_id or "").strip()
        if not jid:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM applications WHERE id = ? AND status = 'applied'",
                (jid,),
            ).fetchone()
        if row is not None:
            return True
        return jid in self._linkedin_applied_ids_from_sheet_exports()

    def recorded_greenhouse_job_url_keys(
        self, *, statuses: tuple[str, ...] = ("applied", "apply_opened")
    ) -> frozenset[str]:
        """
        Normalized Greenhouse-style ``url`` values already logged for the given statuses.

        Used to skip MyGreenhouse search cards that point at jobs you have already applied to (or opened
        for external apply), since Greenhouse does not dedupe like LinkedIn.
        """
        if not statuses:
            return frozenset()
        placeholders = ",".join("?" * len(statuses))
        keys: set[str] = set()
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT url FROM applications
                WHERE url != '' AND status IN ({placeholders})
                """,
                statuses,
            ).fetchall()
        for (u,) in rows:
            if not u or not isinstance(u, str) or "greenhouse" not in u.lower():
                continue
            k = normalize_greenhouse_job_url(u)
            if k:
                keys.add(k)
        return frozenset(keys)

    def last_status_for_job(self, job_id: str) -> str | None:
        """Latest stored status for this LinkedIn job id, or None if never logged."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM applications WHERE id = ?",
                (job_id,),
            ).fetchone()
        return row[0] if row else None

    def log(
        self,
        job: dict,
        status: str,
        score: float = 0.0,
        cover_letter: str = "",
    ) -> str:
        """Persist row and return ``applied_at`` ISO (UTC) for Google Sheets / display consistency."""
        applied_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO applications
                  (id, title, company, location, url, status, score, cover_letter, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.get("id", ""),
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("location", ""),
                    job.get("url", ""),
                    status,
                    round(score, 3),
                    cover_letter,
                    applied_at,
                ),
            )
        log.debug("Tracked: %s — %s (%s)", job.get("title"), job.get("company"), status)
        return applied_at

    def export_csv(self, path: str, *, statuses: tuple[str, ...] = ("applied",)):
        """
        Export rows in the Google Sheet layout: A empty, B company, C empty, D date, E url, F title.

        Default ``statuses`` is ``("applied",)`` (successful auto-applies / manual records).
        Rows whose job URL already appears in ``output/archive/applications_archive.csv`` are omitted
        so ``--export-csv`` does not refill ``applications.csv`` with jobs you have already archived
        (which would duplicate them on the next archive run).

        Use ``statuses=("apply_opened",)`` for external-apply-tab captures.
        """
        if not statuses:
            raise ValueError("statuses must not be empty")
        placeholders = ",".join("?" * len(statuses))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT company, url, title, applied_at FROM applications
                WHERE status IN ({placeholders})
                ORDER BY applied_at ASC
                """,
                statuses,
            ).fetchall()

        archived_keys = (
            _archive_row_keys_for_export_dedupe(APPLICATIONS_ARCHIVE_CSV)
            if statuses == ("applied",)
            else frozenset()
        )
        written: list[tuple] = []
        excluded = 0
        for company, url, title, applied_at in rows:
            k = _sheet_export_url_dedupe_key(url or "")
            if k and k in archived_keys:
                excluded += 1
                continue
            written.append((company, url, title, applied_at))

        out = Path(path)

        # Non-destructive export (same merge process as sync.py / archive rollover): preserve rows already
        # in the target CSV (e.g. the copy downloaded from S3 / written by another machine), since this
        # machine's local DB does not contain applies recorded elsewhere. Without this, regenerating from
        # the local DB would clobber cross-machine history.
        existing_header, existing_rows = read_sheet_csv(out)
        db_rows = [
            applied_sheet_row(
                {"company": company or "", "url": url or "", "title": title or ""},
                format_apply_date_mdy(applied_at or ""),
            )
            for company, url, title, applied_at in written
        ]
        merged = union_sheet_rows(existing_rows, db_rows)
        write_sheet_csv(out, existing_header, merged)

        log.info(
            "Exported %d job(s) (%d new from local DB, %d preserved, statuses=%s) to %s (sheet column layout)",
            len(merged),
            len(merged) - len(existing_rows),
            len(existing_rows),
            ",".join(statuses),
            out,
        )
        if excluded:
            log.info(
                "Excluded %d applied job(s) whose URL already appears in %s (not re-exporting archived applies).",
                excluded,
                APPLICATIONS_ARCHIVE_CSV.resolve(),
            )

    def summary(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            applied = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='applied'"
            ).fetchone()[0]
            skipped = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='skipped'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='failed'"
            ).fetchone()[0]
            apply_opened = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='apply_opened'"
            ).fetchone()[0]
            blacklisted = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='blacklisted'"
            ).fetchone()[0]
            consulting = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status='consulting'"
            ).fetchone()[0]
        return {
            "total": total,
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "apply_opened": apply_opened,
            "blacklisted": blacklisted,
            "consulting": consulting,
        }

    def _conn(self):
        return sqlite3.connect(self.db_path)
