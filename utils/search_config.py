"""
Job-search config (``data/search.json``) — keywords/location/posted_within_24h.

Edited by hand (there's no in-program editor for it), and optionally kept in sync across devices
via S3 (see :func:`utils.s3_outputs.sync_download_output_coordinated`/
:func:`utils.s3_outputs.sync_upload_output_coordinated`) rather than only through git.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SEARCH_PATH = Path("data/search.json")

SEARCH_DEFAULTS: dict[str, Any] = {
    "keywords": ["software developer", "software engineer", "data scientist", "data analyst"],
    "location": "United States",
    "posted_within_24h": True,
}


def load_search_config(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_SEARCH_PATH
    if p.is_file():
        try:
            overrides = json.loads(p.read_text(encoding="utf-8"))
            return {**SEARCH_DEFAULTS, **{k: v for k, v in overrides.items() if k in SEARCH_DEFAULTS}}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not read %s: %s — using built-in defaults", p, e)
    return dict(SEARCH_DEFAULTS)


def save_search_config(config: dict[str, Any], path: Path | str | None = None) -> None:
    p = Path(path) if path else DEFAULT_SEARCH_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_search_config(
    local: dict[str, Any], remote: dict[str, Any], *, local_mtime: float, remote_mtime: float,
) -> dict[str, Any]:
    """
    ``keywords`` unions (dedup, case-insensitive) regardless of which side is newer — a keyword
    added on either device should stick. Every other field (``location``, ``posted_within_24h``,
    and any future scalar setting) takes whichever side was actually written more recently — there's
    no sensible "union" of two different location strings, so this is genuine last-write-wins based
    on real file/object modification time, not just "download always overwrites" (which would
    silently clobber a fresh local edit the human just made but hasn't uploaded yet).
    """
    base = remote if remote_mtime > local_mtime else local
    merged = dict(base)
    seen: set[str] = set()
    merged_keywords: list[str] = []
    for kw in list(local.get("keywords") or []) + list(remote.get("keywords") or []):
        key = str(kw).strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged_keywords.append(kw)
    merged["keywords"] = merged_keywords
    return merged
