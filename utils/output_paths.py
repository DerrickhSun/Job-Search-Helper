"""Paths for CSV outputs under ``output/`` and ``output/archive/``."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
ARCHIVE_DIR = OUTPUT_DIR / "archive"
COVERLETTERS_DIR = OUTPUT_DIR / "coverletters"
LINKEDIN_COVERLETTERS_DIR = COVERLETTERS_DIR / "linkedin"
FILTER_COVERLETTERS_DIR = COVERLETTERS_DIR / "filter"
GREENHOUSE_COVERLETTERS_DIR = COVERLETTERS_DIR / "greenhouse"
FORM_FILL_RULES_DIR = OUTPUT_DIR / "form_fill_rules"

# Subfolder names under ``output/coverletters/`` (one per apply mode).
COVER_LETTER_MODES: tuple[str, ...] = ("linkedin", "filter", "greenhouse")


def cover_letter_output_dirs() -> tuple[Path, ...]:
    """Mode-specific cover letter directories under ``output/coverletters/``."""
    return (LINKEDIN_COVERLETTERS_DIR, FILTER_COVERLETTERS_DIR, GREENHOUSE_COVERLETTERS_DIR)


def cover_letter_mode_names() -> frozenset[str]:
    return frozenset(COVER_LETTER_MODES)


def cover_letter_dir_for_mode(mode: str) -> Path | None:
    m = (mode or "").strip().lower()
    if m in cover_letter_mode_names():
        return COVERLETTERS_DIR / m
    return None


APPLICATIONS_CSV = OUTPUT_DIR / "applications.csv"
APPLICATIONS_ARCHIVE_CSV = ARCHIVE_DIR / "applications_archive.csv"
ASSISTED_APPLICATIONS_CSV = OUTPUT_DIR / "assisted_applications.csv"
ASSISTED_APPLICATIONS_HISTORY_CSV = ARCHIVE_DIR / "assisted_applications_history.csv"
GREENHOUSE_DISMISSED_CSV = OUTPUT_DIR / "greenhouse_dismissed.csv"
CONSULTING_COMPANIES_JSON = OUTPUT_DIR / "consulting_companies.json"

LEGACY_CONSULTING_COMPANIES_JSON = Path("data/consulting_companies.json")
LEGACY_FORM_FILL_RULES_DIR = Path("data/form_fill_rules")
LEGACY_FORM_FILL_RULES_FILE = Path("data/form_fill_rules.json")
FORM_FILL_RULES_SEED_DIR = Path("defaults/form_fill_rules")


def _dir_has_json_rules(path: Path) -> bool:
    return path.is_dir() and any(path.glob("*.json"))


def _copy_json_rules(src: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_file in sorted(src.glob("*.json"), key=lambda p: p.name.lower()):
        shutil.copy2(src_file, dest / src_file.name)
        copied += 1
    return copied


def migrate_form_fill_rules() -> None:
    """
    Ensure ``output/form_fill_rules/`` exists (S3-synced runtime rules).

    When the directory has no ``*.json`` yet:
    1. Copy from legacy ``data/form_fill_rules/`` (one-time upgrade from git-tracked layout).
    2. Else copy bundled defaults from ``defaults/form_fill_rules/``.
    """
    if _dir_has_json_rules(FORM_FILL_RULES_DIR):
        return

    if _dir_has_json_rules(LEGACY_FORM_FILL_RULES_DIR):
        n = _copy_json_rules(LEGACY_FORM_FILL_RULES_DIR, FORM_FILL_RULES_DIR)
        log.info(
            "Migrated %d form fill rule file(s) from %s -> %s",
            n,
            LEGACY_FORM_FILL_RULES_DIR,
            FORM_FILL_RULES_DIR,
        )
        return

    if _dir_has_json_rules(FORM_FILL_RULES_SEED_DIR):
        n = _copy_json_rules(FORM_FILL_RULES_SEED_DIR, FORM_FILL_RULES_DIR)
        log.info(
            "Seeded %d form fill rule file(s) from %s -> %s",
            n,
            FORM_FILL_RULES_SEED_DIR,
            FORM_FILL_RULES_DIR,
        )
        return

    log.warning(
        "No form fill rules found — expected S3 sync, %s, or %s",
        LEGACY_FORM_FILL_RULES_DIR,
        FORM_FILL_RULES_SEED_DIR,
    )


def migrate_legacy_cover_letter_layout() -> None:
    """
    Move flat ``output/coverletters/*.docx`` and legacy sibling folders into mode subdirs.

    One-time upgrade from older layouts (flat ``coverletters/`` or ``filter_coverletters/`` /
    ``greenhouse_coverletters`` at ``output/`` root).
    """
    for d in cover_letter_output_dirs():
        d.mkdir(parents=True, exist_ok=True)

    moved = 0
    if COVERLETTERS_DIR.is_dir():
        for src in sorted(COVERLETTERS_DIR.glob("*.docx"), key=lambda p: p.name.lower()):
            dest = LINKEDIN_COVERLETTERS_DIR / src.name
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                n = 2
                while dest.exists():
                    dest = LINKEDIN_COVERLETTERS_DIR / f"{stem}__{n}{suffix}"
                    n += 1
            src.replace(dest)
            moved += 1

    legacy_dirs = (
        (OUTPUT_DIR / "filter_coverletters", FILTER_COVERLETTERS_DIR),
        (OUTPUT_DIR / "greenhouse_coverletters", GREENHOUSE_COVERLETTERS_DIR),
    )
    for legacy_dir, dest_dir in legacy_dirs:
        if not legacy_dir.is_dir():
            continue
        for src in sorted(legacy_dir.rglob("*.docx"), key=lambda p: str(p).lower()):
            dest = dest_dir / src.name
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                n = 2
                while dest.exists():
                    dest = dest_dir / f"{stem}__{n}{suffix}"
                    n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            moved += 1

    if moved:
        log.info(
            "Migrated %d cover letter file(s) into %s/{linkedin,filter,greenhouse}/",
            moved,
            COVERLETTERS_DIR,
        )


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
