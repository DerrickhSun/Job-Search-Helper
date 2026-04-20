"""
Application Tracker
Logs every job to SQLite and exports ``applications.csv`` in the same 6-column layout as Google Sheets
(successful applies only: empty, company, empty, date, url, title).
"""

import csv
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from apply_sheets import applied_sheet_row

log = logging.getLogger(__name__)


def _applied_at_to_mdy(iso: str) -> str:
    """Format stored UTC ISO timestamps as ``MM/DD/YYYY`` for column D."""
    s = (iso or "").strip()
    if not s:
        return date.today().strftime("%m/%d/%Y")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except ValueError:
        return s[:10] if len(s) >= 10 else date.today().strftime("%m/%d/%Y")


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

    def log(
        self,
        job: dict,
        status: str,
        score: float = 0.0,
        cover_letter: str = "",
    ):
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
                    datetime.utcnow().isoformat(),
                ),
            )
        log.debug("Tracked: %s — %s (%s)", job.get("title"), job.get("company"), status)

    def export_csv(self, path: str):
        """
        Export successful applies only, same columns as the Sheet: A empty, B company, C empty,
        D date, E url, F title.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT company, url, title, applied_at FROM applications
                WHERE status = 'applied'
                ORDER BY applied_at ASC
                """
            ).fetchall()

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        header = ("", "company", "", "date", "url", "title")
        with open(out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for company, url, title, applied_at in rows:
                job = {"company": company or "", "url": url or "", "title": title or ""}
                writer.writerow(applied_sheet_row(job, _applied_at_to_mdy(applied_at or "")))

        log.info("Exported %d applied job(s) to %s (sheet column layout)", len(rows), out)

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
        return {"total": total, "applied": applied, "skipped": skipped, "failed": failed}

    def _conn(self):
        return sqlite3.connect(self.db_path)
