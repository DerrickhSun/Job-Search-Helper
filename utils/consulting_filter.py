"""
Heuristics to skip staffing / body-shop style listings (often low pay or opaque client relationships).

Company **name** uses whole-word ``consulting`` (e.g. ``X Consulting LLC``), ``staffing`` (e.g.
``Acme Staffing Group``), or ``talent`` (e.g. ``Acme Talent Partners`` — common in recruiting / body-shop brands).
Names containing the substring ``IT`` (capital **I** + capital **T** only — case-sensitive, so lowercase
``it`` inside words does not match) are also treated as likely staffing/IT body shops and skipped.

In the **description**, bare ``consulting`` is *not* matched: postings often list prior experience in
``quantitative consulting``, ``management consulting``, etc., without the employer being a consultancy.
Instead we match employer-style phrases (``consulting firm``, ``our consulting team``, ``consultancy``,
``consultant`` roles, ``client company``, singular ``our client`` but not ``our clients``, …).
"""

from __future__ import annotations

import re

_COMPANY_CONSULTING = re.compile(r"\bconsulting\b", re.IGNORECASE)
_COMPANY_STAFFING = re.compile(r"\bstaffing\b", re.IGNORECASE)
_COMPANY_TALENT = re.compile(r"\btalent\b", re.IGNORECASE)
_DESC_CONSULTANT = re.compile(r"\bconsultants?\b", re.IGNORECASE)
_DESC_CONSULTANCY = re.compile(r"\bconsultancy\b", re.IGNORECASE)
_DESC_CLIENT_COMPANY = re.compile(r"client\s+company", re.IGNORECASE)
# Singular "our client" (staffing/third-party); \b after "client" avoids matching the prefix of "our clients".
_DESC_OUR_CLIENT_SINGULAR = re.compile(r"\bour\s+client\b", re.IGNORECASE)
# Employer is a consulting org (avoids "… experience in … consulting, or …" industry lists).
_DESC_CONSULTING_ORG = re.compile(
    r"\bconsulting\s+(?:firm|company|companies|agency|agencies|group|groups|practice|practices)\b",
    re.IGNORECASE,
)
_DESC_OUR_CONSULTING = re.compile(
    r"\bour\s+consulting\s+(?:practice|team|teams|division|divisions|services|arm|arms|business|group|unit)\b",
    re.IGNORECASE,
)
_DESC_CONSULTING_SERVICES = re.compile(r"\bconsulting\s+services\b", re.IGNORECASE)


def is_consulting_listing(job: dict) -> bool:
    """True when company or description matches consulting / body-shop style signals."""
    company = str(job.get("company") or "")
    if "IT" in company:
        return True
    if _COMPANY_CONSULTING.search(company):
        return True
    if _COMPANY_STAFFING.search(company):
        return True
    if _COMPANY_TALENT.search(company):
        return True
    desc = str(job.get("description") or "")
    if _DESC_CONSULTANT.search(desc):
        return True
    if _DESC_CONSULTING_ORG.search(desc):
        return True
    if _DESC_OUR_CONSULTING.search(desc):
        return True
    if _DESC_CONSULTING_SERVICES.search(desc):
        return True
    if _DESC_CONSULTANCY.search(desc):
        return True
    if _DESC_CLIENT_COMPANY.search(desc):
        return True
    if _DESC_OUR_CLIENT_SINGULAR.search(desc):
        return True
    return False
