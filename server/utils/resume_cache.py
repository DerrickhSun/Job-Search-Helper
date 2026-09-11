"""
Resume profile JSON cache (``data/resume_profile.json`` by default).

After the first parse from PDF/DOCX, the structured dict is written so you can edit fields
(e.g. ``linkedin_url``, ``website_url``, ``example_cover_letter`` — a prior cover letter the bot may reuse
for phrasing) without re-parsing. Re-parse with ``--force-resume-parse``. Each ``experience`` item includes
a string ``description`` (role narrative); older caches may still list ``bullets`` and are read as a fallback.
Each ``projects`` item is ``title``, ``dates``, and ``description`` (optional legacy ``bullets`` for body text).

Optional ``experience_years_cap`` (number): caps the resume ``years`` heuristic used only for
education/years **gates** in ``eval_utils.matcher`` (see ``_estimate_years_experience``), e.g. when calendar
span overstates how senior you are vs per-skill posting requirements.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .resume_parser import ResumeParser

log = logging.getLogger(__name__)

DEFAULT_RESUME_CACHE_PATH = Path("data/resume_profile.json")
# Default source document when --resume is omitted (project cwd is usually the server/ folder).
DEFAULT_RESUME_FILE = Path("resume.pdf")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Resume cache must be a JSON object, got {type(data).__name__}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_or_build_resume(
    resume_file: Path | None,
    cache_path: Path,
    *,
    force_reparse: bool,
) -> dict[str, Any]:
    """
    Load resume data from ``cache_path`` if it exists and ``force_reparse`` is false; otherwise
    parse ``resume_file`` and write the cache file.

    When loading from cache only, ``resume_file`` may be ``None``.
    """
    cache_path = Path(cache_path)

    if force_reparse:
        if not resume_file or not resume_file.is_file():
            raise FileNotFoundError(
                "--force-resume-parse requires an existing --resume file to parse."
            )
        log.info("Re-parsing resume (--force-resume-parse): %s", resume_file)
        data = ResumeParser().parse(str(resume_file))
        _write_json(cache_path, data)
        log.info("Wrote resume profile cache: %s", cache_path.resolve())
        return data

    if cache_path.is_file():
        data = _read_json(cache_path)
        log.info(
            "Loaded resume profile from JSON cache: %s "
            "(use --force-resume-parse to re-parse --resume and overwrite)",
            cache_path.resolve(),
        )
        return data

    if not resume_file or not resume_file.is_file():
        raise FileNotFoundError(
            f"No resume cache at {cache_path} and no resume file at {resume_file!s}; "
            "add resume.pdf (or pass --resume PATH), or create the JSON cache by hand."
        )
    log.info("Parsing resume (no cache yet): %s", resume_file)
    data = ResumeParser().parse(str(resume_file))
    _write_json(cache_path, data)
    log.info(
        "Wrote initial resume profile cache: %s — you can edit this file and re-run without re-parsing.",
        cache_path.resolve(),
    )
    return data
