"""
Company blacklist for skipping applies. Matching is case-insensitive and ignores punctuation.

List entries in ``data/company_blacklist.json`` as lowercase phrases without punctuation
(e.g. ``"sun west mortgage company inc"``) — they match LinkedIn display names like
``Sun West Mortgage Company, Inc.`` after the same normalization.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data/company_blacklist.json")


def normalize_company_name(s: str) -> str:
    """Lowercase; non-alphanumeric runs become a single space; strip."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_company_blacklist(path: Path | str | None = None) -> list[str]:
    """Load raw blacklist strings from JSON (``companies`` or ``blacklist`` array). Missing file → []."""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.is_file():
        return []
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read company blacklist %s: %s", p, e)
        return []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("companies") or data.get("blacklist") or []
    else:
        entries = []
    out = [str(x).strip() for x in entries if str(x).strip()]
    log.debug("Loaded %d company blacklist entr%s from %s", len(out), "y" if len(out) == 1 else "ies", p)
    return out


def is_company_blacklisted(company: str, blacklist_entries: list[str]) -> bool:
    """
    True if ``company`` matches any blacklist entry after normalization.

    Match rules (``nb`` = normalized entry, ``nc`` = normalized company):
    - exact: ``nc == nb``
    - substring (min 5 chars on the shorter side): ``nb in nc`` or ``nc in nb``
    """
    if not blacklist_entries:
        return False
    nc = normalize_company_name(company)
    if not nc:
        return False
    for raw in blacklist_entries:
        nb = normalize_company_name(raw)
        if not nb:
            continue
        if nc == nb:
            return True
        shorter, longer = (nb, nc) if len(nb) <= len(nc) else (nc, nb)
        if len(shorter) < 5:
            continue
        if shorter in longer:
            return True
    return False
