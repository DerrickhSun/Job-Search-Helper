"""Paths for CSV outputs under ``output/`` and ``output/archive/``."""

from __future__ import annotations

from pathlib import Path

OUTPUT_DIR = Path("output")
ARCHIVE_DIR = OUTPUT_DIR / "archive"
COVERLETTERS_DIR = OUTPUT_DIR / "coverletters"

APPLICATIONS_CSV = OUTPUT_DIR / "applications.csv"
APPLICATIONS_ARCHIVE_CSV = ARCHIVE_DIR / "applications_archive.csv"
ASSISTED_APPLICATIONS_CSV = OUTPUT_DIR / "assisted_applications.csv"
ASSISTED_APPLICATIONS_HISTORY_CSV = ARCHIVE_DIR / "assisted_applications_history.csv"
GREENHOUSE_DISMISSED_CSV = OUTPUT_DIR / "greenhouse_dismissed.csv"
CONSULTING_COMPANIES_JSON = OUTPUT_DIR / "consulting_companies.json"

LEGACY_CONSULTING_COMPANIES_JSON = Path("data/consulting_companies.json")


def migrate_legacy_consulting_companies_file() -> None:
    """Move ``data/consulting_companies.json`` to ``output/consulting_companies.json`` when only the legacy path exists."""
    if LEGACY_CONSULTING_COMPANIES_JSON.is_file() and not CONSULTING_COMPANIES_JSON.is_file():
        CONSULTING_COMPANIES_JSON.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_CONSULTING_COMPANIES_JSON.replace(CONSULTING_COMPANIES_JSON)


def migrate_legacy_root_archive_files() -> None:
    """
    Move archive CSVs from older ``output/*.csv`` locations into ``output/archive/``
    when the new path does not exist yet.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    legacy_app = OUTPUT_DIR / "applications_archive.csv"
    if legacy_app.is_file() and not APPLICATIONS_ARCHIVE_CSV.is_file():
        legacy_app.replace(APPLICATIONS_ARCHIVE_CSV)
    legacy_hist = OUTPUT_DIR / "assisted_applications_history.csv"
    if legacy_hist.is_file() and not ASSISTED_APPLICATIONS_HISTORY_CSV.is_file():
        legacy_hist.replace(ASSISTED_APPLICATIONS_HISTORY_CSV)
