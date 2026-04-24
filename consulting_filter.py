"""
Heuristics to skip staffing / body-shop style listings (often low pay or opaque client relationships).

A job is treated as consulting-like when:
  - the company name contains the word ``consulting`` as a whole word, or
  - the description mentions ``consultant`` / ``consulting`` / ``consultancy`` / ``client company``
    (word or phrase boundaries).
"""

from __future__ import annotations

import re

_COMPANY_CONSULTING = re.compile(r"\bconsulting\b", re.IGNORECASE)
_DESC_CONSULTANT = re.compile(r"\bconsultants?\b", re.IGNORECASE)
_DESC_CONSULTING = re.compile(r"\bconsulting\b", re.IGNORECASE)
_DESC_CONSULTANCY = re.compile(r"\bconsultancy\b", re.IGNORECASE)
_DESC_CLIENT_COMPANY = re.compile(r"client\s+company", re.IGNORECASE)


def is_consulting_listing(job: dict) -> bool:
    """True when company or description matches consulting / body-shop style signals."""
    company = str(job.get("company") or "")
    if _COMPANY_CONSULTING.search(company):
        return True
    desc = str(job.get("description") or "")
    if _DESC_CONSULTANT.search(desc):
        return True
    if _DESC_CONSULTING.search(desc):
        return True
    if _DESC_CONSULTANCY.search(desc):
        return True
    if _DESC_CLIENT_COMPANY.search(desc):
        return True
    return False
