"""
Application Tracker
Logs every job to SQLite and exports CSV in the same 6-column layout as Google Sheets
(A empty, B company, C empty, D date, E job URL, F title):

- ``applications.csv`` — status ``applied`` (manual ``r`` / successful auto-applies)
- ``apply_opened.csv`` — status ``apply_opened`` (helper: external apply tab opened)
- ``consulting`` — skipped for staffing / consulting heuristics (see ``consulting_filter``)
"""

import csv
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from apply_sheets import applied_sheet_row, format_apply_date_mdy

log = logging.getLogger(__name__)


class ApplicationTracker:
    def __init__(self, db_path: str = "data/applications.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
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

    def already_applied(self, job_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM applications WHERE id = ? AND status = 'applied'",
                (job_id,),
            ).fetchone()
        return row is not None

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

        Default ``statuses`` is ``("applied",)`` (manual helper ``r`` / successful auto-applies).
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

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        header = ("", "company", "", "date", "url", "title")
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for company, url, title, applied_at in rows:
                job = {"company": company or "", "url": url or "", "title": title or ""}
                writer.writerow(applied_sheet_row(job, format_apply_date_mdy(applied_at or "")))

        log.info(
            "Exported %d job(s) (statuses=%s) to %s (sheet column layout)",
            len(rows),
            ",".join(statuses),
            out,
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
