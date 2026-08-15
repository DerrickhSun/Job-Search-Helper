"""
Company blacklist for skipping applies. Matching is case-insensitive and ignores punctuation.

List entries in ``data/company_blacklist.json`` as lowercase phrases without punctuation
(e.g. ``"sun west mortgage company inc"``) — they match LinkedIn display names like
``Sun West Mortgage Company, Inc.`` after the same normalization.

``data/company_blacklist_temporary.json`` holds the same kind of entries but each with an
``until`` expiry date (``YYYY-MM-DD``) — for companies that cap how many applications they'll
accept in a given window rather than being permanently off-limits. Call
:func:`prune_and_load_temporary_blacklist` once at the start of a run: it drops (and rewrites the
file without) any entry whose date has passed, and returns the company names still active, which
the caller merges into the regular blacklist list passed to :func:`is_company_blacklisted`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data/company_blacklist.json")
_DEFAULT_TEMP_PATH = Path("data/company_blacklist_temporary.json")


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


def load_temporary_blacklist(path: Path | str | None = None) -> list[dict[str, str]]:
    """
    Raw temporary-blacklist entries: ``[{"company": ..., "until": "YYYY-MM-DD"}, ...]``.

    Missing file, unreadable JSON, or malformed entries → skipped/empty, same tolerance as
    :func:`load_company_blacklist`.
    """
    p = Path(path) if path else _DEFAULT_TEMP_PATH
    if not p.is_file():
        return []
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read temporary company blacklist %s: %s", p, e)
        return []
    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, dict):
        raw_entries = data.get("companies") or []
    else:
        raw_entries = []
    out: list[dict[str, str]] = []
    for e in raw_entries:
        if not isinstance(e, dict):
            continue
        company = str(e.get("company") or "").strip()
        until = str(e.get("until") or "").strip()
        if company and until:
            out.append({"company": company, "until": until})
    return out


def _write_temporary_blacklist(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"companies": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def prune_and_load_temporary_blacklist(
    path: Path | str | None = None, *, today: date | None = None
) -> list[str]:
    """
    Drop expired entries from the temporary blacklist (rewriting the file if any were removed),
    and return the company names still active — for companies with a limit on how many
    applications they'll accept in a given window, not a permanent blacklist entry.

    Call once at the start of a run; merge the returned names into the list passed to
    :func:`is_company_blacklisted` (e.g. ``company_blacklist += prune_and_load_temporary_blacklist()``).
    An entry with an unparseable ``until`` date is kept as-is (logged) rather than silently
    dropped or treated as permanent.
    """
    p = Path(path) if path else _DEFAULT_TEMP_PATH
    entries = load_temporary_blacklist(p)
    if not entries:
        return []
    cur = today or date.today()
    kept: list[dict[str, str]] = []
    removed = 0
    for e in entries:
        until_str = e["until"]
        try:
            until_date = date.fromisoformat(until_str)
        except ValueError:
            log.warning(
                "Temporary blacklist: %r has an unparseable date %r (expected YYYY-MM-DD) — "
                "keeping it active until fixed by hand.",
                e["company"],
                until_str,
            )
            kept.append(e)
            continue
        if until_date < cur:
            removed += 1
            log.info(
                "Temporary blacklist: %r expired (was until %s) — removing.", e["company"], until_str
            )
        else:
            kept.append(e)
    if removed:
        _write_temporary_blacklist(p, kept)
        log.info(
            "Temporary blacklist: removed %d expired entr%s.", removed, "y" if removed == 1 else "ies"
        )
    return [e["company"] for e in kept]
