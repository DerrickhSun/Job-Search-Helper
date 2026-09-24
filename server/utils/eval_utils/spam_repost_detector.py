"""
Detects companies suspected of spam-reposting the same job title repeatedly -- not a LinkedIn
issue or a bug in our own processing, just some employers throwing out many near-duplicate
postings. Flags a company once it has more than ``SPAM_MIN_COUNT`` applications under the *same*
title within the last ``SPAM_WINDOW_DAYS`` days.

Deliberately a live-recomputed cache, not a persisted/pruned memory file like
``easy_apply_company_memory.py``: the flag has nothing to "fall off" that needs managing -- a
company simply stops matching the moment enough of its applications age out of the rolling
window, the next time the cache happens to be rebuilt. The only staleness is the cache's own age,
bounded by ``CACHE_MAX_AGE_SECONDS`` and only ever paid for by whoever actually asks (see
``get_recent_applications_cache`` -- lazy, no background refresh; an idle process pays nothing).

Reads the same four sources ``lookup_application.py`` already searches (bot auto-applies +
assisted/extension-recorded applications, active + archive each), so this covers both without
needing to touch either write path or merge into ``data/applications.db``. Since the window is
only 30 days, rebuilding from scratch is cheap regardless of how much total history exists in the
archives -- the cost is bounded by the window, not by total history.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

from ..output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
)
from ..sheet_csv import read_sheet_csv
from .company_blacklist import normalize_company_name

log = logging.getLogger(__name__)

SPAM_WINDOW_DAYS = 30
SPAM_MIN_COUNT = 3
CACHE_MAX_AGE_SECONDS = 300  # 5 minutes

# Same four sources lookup_application.py searches: bot auto-applies (+ archive) and
# assisted/extension-recorded applications (+ archive).
_SOURCES = (
    APPLICATIONS_CSV,
    APPLICATIONS_ARCHIVE_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
)

# Column indices in the 6-column sheet layout: ("", company, "", date, url, title).
_COL_COMPANY = 1
_COL_DATE = 3
_COL_TITLE = 5


def _parse_mdy(date_str: str) -> datetime | None:
    try:
        return datetime.strptime((date_str or "").strip(), "%m/%d/%Y")
    except ValueError:
        return None


def build_recent_applications_cache(
    *, window_days: int = SPAM_WINDOW_DAYS
) -> dict[str, dict[str, int]]:
    """
    ``{normalized_company: {normalized_title: count}}`` for applications within the last
    `window_days`, across both bot auto-applies and assisted/extension-recorded applications.
    """
    cutoff = datetime.now() - timedelta(days=window_days)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in _SOURCES:
        _, rows = read_sheet_csv(path)
        for row in rows:
            date_str = row[_COL_DATE] if len(row) > _COL_DATE else ""
            dt = _parse_mdy(date_str)
            if dt is None or dt < cutoff:
                continue
            company = row[_COL_COMPANY] if len(row) > _COL_COMPANY else ""
            title = row[_COL_TITLE] if len(row) > _COL_TITLE else ""
            nc = normalize_company_name(company)
            nt = (title or "").strip().lower()
            if not nc or not nt:
                continue
            counts[nc][nt] += 1
    return {c: dict(titles) for c, titles in counts.items()}


_cache: dict[str, dict[str, int]] | None = None
_cache_built_at: float = 0.0


def get_recent_applications_cache(
    *, max_age_seconds: float = CACHE_MAX_AGE_SECONDS
) -> dict[str, dict[str, int]]:
    """
    Lazily-rebuilt cache -- only recomputed when actually accessed and older than
    `max_age_seconds` (or never built yet). No background refresh; an idle process pays nothing.
    """
    global _cache, _cache_built_at
    now = time.time()
    if _cache is None or (now - _cache_built_at) > max_age_seconds:
        _cache = build_recent_applications_cache()
        _cache_built_at = now
        log.debug("Rebuilt recent-applications cache: %d compan(y/ies) tracked.", len(_cache))
    return _cache


def record_application(company: str, title: str) -> None:
    """
    Increment the live cache directly (call right after logging a new application) instead of
    re-reading CSVs -- keeps a long-running ``main.py`` session's own new applies reflected
    immediately without any extra file I/O, and without waiting for the next CSV export.
    """
    cache = get_recent_applications_cache()
    nc = normalize_company_name(company)
    nt = (title or "").strip().lower()
    if not nc or not nt:
        return
    cache.setdefault(nc, {})
    cache[nc][nt] = cache[nc].get(nt, 0) + 1


def company_spam_suspected(company: str, *, min_count: int = SPAM_MIN_COUNT) -> bool:
    """True if `company` has more than `min_count` applications under the same title within the
    tracked window."""
    nc = normalize_company_name(company)
    if not nc:
        return False
    titles = get_recent_applications_cache().get(nc, {})
    return any(count > min_count for count in titles.values())
