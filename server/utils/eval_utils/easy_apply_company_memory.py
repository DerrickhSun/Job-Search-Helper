"""
Persistent memory of which companies post jobs through an easy-to-apply ATS (Greenhouse or
Ashby), detected from the external "Apply" destination URL for a non-Easy-Apply LinkedIn
listing. Companies overwhelmingly stick to one ATS, so this is tracked per-company (like
:mod:`consulting_company_memory`) rather than re-derived per job.

Unlike the consulting memory, entries here expire: a company not seen in any job for
``STALE_AFTER_DAYS`` is pruned on load, and a company whose ATS later reads as neither Greenhouse
nor Ashby is removed immediately (see :meth:`EasyApplyCompanyMemory.forget`) rather than kept
around with stale information -- companies do migrate ATS providers.

File format (JSON): ``{"version": 1, "companies": [{"slug": str|null,
"normalized_name": str|null, "service": "greenhouse"|"ashby", "last_seen": "YYYY-MM-DD"}, ...]}``
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .company_blacklist import normalize_company_name
from ..output_paths import EASY_APPLY_COMPANIES_JSON

log = logging.getLogger(__name__)

DEFAULT_EASY_APPLY_MEMORY_PATH = EASY_APPLY_COMPANIES_JSON
_MEMORY_VERSION = 1

# A company not seen in any job posting for this long is pruned on load -- keeps the file from
# growing forever with employers that stopped showing up in searches long ago.
STALE_AFTER_DAYS = 30

EASY_APPLY_SERVICES: tuple[str, ...] = ("greenhouse", "ashby")


def detect_easy_apply_service(url: str) -> str | None:
    """
    "greenhouse"/"ashby" if the (external apply destination) URL is hosted on, or embeds, that
    ATS, else None.

    Checks both the ATS's own hosted subdomain (``boards.greenhouse.io``, ``jobs.ashbyhq.com``)
    and its embeddable-widget query param on a company's own custom domain (``gh_jid=``,
    ``ashby_jid=``) -- e.g. ``https://superhuman.com/company/careers/jobs?ashby_jid=...`` is
    Ashby embedded on Superhuman's own domain, not Ashby's hosted subdomain at all, and would be
    missed by a hostname-only check.
    """
    u = (url or "").strip().lower()
    if not u:
        return None
    if "greenhouse.io" in u or "gh_jid=" in u:
        return "greenhouse"
    if "ashbyhq.com" in u or "ashby_jid=" in u:
        return "ashby"
    return None


def _name_matches_memory(normalized_job_company: str, stored_name: str) -> bool:
    """Same spirit as blacklist/consulting-memory: exact or meaningful substring overlap."""
    if not normalized_job_company or not stored_name:
        return False
    nc, nb = normalized_job_company, stored_name
    if nc == nb:
        return True
    shorter, longer = (nb, nc) if len(nb) <= len(nc) else (nc, nb)
    if len(shorter) < 5:
        return False
    return shorter in longer


@dataclass
class EasyApplyCompanyMemory:
    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    def _find_index(self, *, slug: str | None, company_display: str) -> int | None:
        s = (slug or "").strip().lower() or None
        nc = normalize_company_name(company_display)
        for i, e in enumerate(self.entries):
            if s and e.get("slug") == s:
                return i
            stored_name = e.get("normalized_name")
            if nc and stored_name and _name_matches_memory(nc, stored_name):
                return i
        return None

    def lookup(self, *, slug: str | None, company_display: str) -> dict[str, Any] | None:
        i = self._find_index(slug=slug, company_display=company_display)
        return self.entries[i] if i is not None else None

    def remember(self, *, slug: str | None, company_display: str, service: str) -> None:
        """Record (or refresh) that this company's external applies go through ``service``."""
        if service not in EASY_APPLY_SERVICES:
            raise ValueError(f"Unknown easy-apply service: {service!r}")
        s = (slug or "").strip().lower() or None
        nc = normalize_company_name(company_display) or None
        i = self._find_index(slug=slug, company_display=company_display)
        entry = {
            "slug": s or (self.entries[i].get("slug") if i is not None else None),
            "normalized_name": nc or (self.entries[i].get("normalized_name") if i is not None else None),
            "service": service,
            "last_seen": date.today().isoformat(),
        }
        if i is not None:
            self.entries[i] = entry
        else:
            self.entries.append(entry)
        self.save()

    def forget(self, *, slug: str | None, company_display: str) -> bool:
        """Remove a company's entry (it no longer appears to use either ATS). True if one existed."""
        i = self._find_index(slug=slug, company_display=company_display)
        if i is None:
            return False
        del self.entries[i]
        self.save()
        return True

    def prune_stale(self, *, max_age_days: int = STALE_AFTER_DAYS, today: date | None = None) -> int:
        """Drop entries not seen in ``max_age_days``; returns the count removed. Rewrites the file if any were."""
        cur = today or date.today()
        kept: list[dict[str, Any]] = []
        removed = 0
        for e in self.entries:
            try:
                last_seen = date.fromisoformat(str(e.get("last_seen", "")))
            except ValueError:
                kept.append(e)  # Unparseable -- keep rather than guess (matches temp-blacklist convention).
                continue
            if (cur - last_seen).days > max_age_days:
                removed += 1
            else:
                kept.append(e)
        if removed:
            self.entries = kept
            self.save()
        return removed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _MEMORY_VERSION, "companies": self.entries}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.debug("Saved easy-apply company memory to %s", self.path)


def load_easy_apply_company_memory(path: Path | str | None = None) -> EasyApplyCompanyMemory:
    p = Path(path) if path else DEFAULT_EASY_APPLY_MEMORY_PATH
    mem = EasyApplyCompanyMemory(path=p)
    if not p.is_file():
        log.debug("No easy-apply company memory file yet at %s (created on first detection).", p)
        return mem
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read easy-apply company memory %s: %s", p, e)
        return mem
    if isinstance(data, dict):
        for e in data.get("companies") or []:
            if not isinstance(e, dict):
                continue
            service = e.get("service")
            if service not in EASY_APPLY_SERVICES:
                continue
            slug = e.get("slug")
            name = e.get("normalized_name")
            if not slug and not name:
                continue
            mem.entries.append(
                {
                    "slug": (str(slug).strip().lower() or None) if slug else None,
                    "normalized_name": normalize_company_name(str(name)) or None if name else None,
                    "service": service,
                    "last_seen": str(e.get("last_seen") or ""),
                }
            )
    removed = mem.prune_stale()
    if removed:
        log.info("Easy-apply company memory: pruned %d stale entr%s (unseen 30+ days).", removed, "y" if removed == 1 else "ies")
    log.info("Easy-apply company memory: %d compan%s tracked (%s)", len(mem.entries), "y" if len(mem.entries) == 1 else "ies", p)
    return mem
