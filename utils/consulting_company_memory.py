"""
Persistent memory of LinkedIn companies flagged as consulting / recruiting via company-page checks.

Reduces repeat navigations to ``linkedin.com/company/.../about`` for the same employer across runs.

File format (JSON): ``{"version": 1, "slugs": [...], "normalized_company_names": [...]}``
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .company_blacklist import normalize_company_name
from .output_paths import CONSULTING_COMPANIES_JSON

log = logging.getLogger(__name__)

DEFAULT_CONSULTING_MEMORY_PATH = CONSULTING_COMPANIES_JSON
_MEMORY_VERSION = 1


def linkedin_company_slug_from_url(url: str) -> str | None:
    """Return the ``company`` path segment from a LinkedIn company URL, lowercased, or None."""
    if not (url or "").strip():
        return None
    m = re.search(r"linkedin\.com/company/([^/?#]+)", url, re.IGNORECASE)
    if not m:
        return None
    slug = (m.group(1) or "").strip().strip("/").lower()
    return slug or None


def _name_matches_memory(normalized_job_company: str, stored_name: str) -> bool:
    """Same spirit as blacklist: exact or meaningful substring overlap."""
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
class ConsultingCompanyMemory:
    """Slugs from LinkedIn company URLs plus normalized display names seen when flagged."""

    path: Path
    slugs: set[str] = field(default_factory=set)
    normalized_company_names: set[str] = field(default_factory=set)

    def matches(self, *, slug: str | None, company_display: str) -> bool:
        if slug:
            s = slug.strip().lower()
            if s and s in self.slugs:
                return True
        nc = normalize_company_name(company_display)
        if not nc:
            return False
        if nc in self.normalized_company_names:
            return True
        for stored in self.normalized_company_names:
            if _name_matches_memory(nc, stored):
                return True
        return False

    def remember(self, *, slug: str | None, company_display: str) -> None:
        if slug:
            s = slug.strip().lower()
            if s:
                self.slugs.add(s)
        nc = normalize_company_name(company_display)
        if nc:
            self.normalized_company_names.add(nc)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _MEMORY_VERSION,
            "slugs": sorted(self.slugs),
            "normalized_company_names": sorted(self.normalized_company_names),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.debug("Saved consulting company memory to %s", self.path)


def load_consulting_company_memory(path: Path | str | None = None) -> ConsultingCompanyMemory:
    p = Path(path) if path else DEFAULT_CONSULTING_MEMORY_PATH
    mem = ConsultingCompanyMemory(path=p)
    if not p.is_file():
        log.debug("No consulting company memory file yet at %s (created when a company page flags consulting).", p)
        return mem
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read consulting company memory %s: %s", p, e)
        return mem
    if isinstance(data, dict):
        for s in data.get("slugs") or []:
            if isinstance(s, str) and s.strip():
                mem.slugs.add(s.strip().lower())
        for n in data.get("normalized_company_names") or data.get("names") or []:
            nn = normalize_company_name(str(n))
            if nn:
                mem.normalized_company_names.add(nn)
    log.info(
        "Consulting company memory: %d LinkedIn slug(s), %d stored name(s) (%s)",
        len(mem.slugs),
        len(mem.normalized_company_names),
        p,
    )
    return mem
