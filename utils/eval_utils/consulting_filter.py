"""
Heuristics to skip staffing / body-shop style listings (often low pay or opaque client relationships).

**Order of signals** (LinkedIn callers should use this order to limit ``/company/...`` traffic):

1. **Job posting text only** — ``is_consulting_listing_from_job_posting_text_only``: ``title`` + ``description``.
2. **Listing company line + title** — ``is_consulting_listing_from_listing_company_line_only``: card ``company``,
   capital ``IT`` in title/company (not scanned across full description).
3. **Persisted memory** (slug / name) — no company-page fetch.
4. **LinkedIn company-page industry** — last resort; most likely to look like automated browsing when repeated.

Legacy combined helper: ``is_consulting_listing`` = (1) OR (2).

Company **name** uses whole-word ``consulting`` (e.g. ``X Consulting LLC``), ``staffing`` (e.g.
``Acme Staffing Group``), or ``talent`` (e.g. ``Acme Talent Partners`` — common in recruiting / body-shop brands).
Names containing the substring ``IT`` (capital **I** + capital **T** only — case-sensitive, so lowercase
``it`` inside words does not match) are also treated as likely staffing/IT body shops and skipped.

In **posting text** (title + description), bare ``consulting`` is *not* matched alone: postings often list
prior experience in ``quantitative consulting``, ``management consulting``, etc., without the employer being
a consultancy. Instead we match employer-style phrases (``consulting firm``, ``our consulting team``,
``consultancy``, ``consultant`` roles, ``client company``, …).
The same description-style patterns are applied to the **title** as well as the description.
"""

from __future__ import annotations

import re

_COMPANY_CONSULTING = re.compile(r"\bconsulting\b", re.IGNORECASE)
_COMPANY_STAFFING = re.compile(r"\bstaffing\b", re.IGNORECASE)
_COMPANY_TALENT = re.compile(r"\btalent\b", re.IGNORECASE)
_DESC_CONSULTANT = re.compile(r"\bconsultants?\b", re.IGNORECASE)
_DESC_CONSULTANCY = re.compile(r"\bconsultancy\b", re.IGNORECASE)
_DESC_CLIENT_COMPANY = re.compile(r"client\s+company", re.IGNORECASE)
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


def _posting_text_consulting_signals(title: str, description: str) -> bool:
    """Job title + description: employer-style consulting phrases and obvious staffing words in the role text."""
    text = f"{title}\n{description}".strip()
    if not text:
        return False
    if _DESC_CONSULTANT.search(text):
        return True
    if _DESC_CONSULTING_ORG.search(text):
        return True
    if _DESC_OUR_CONSULTING.search(text):
        return True
    if _DESC_CONSULTING_SERVICES.search(text):
        return True
    if _DESC_CONSULTANCY.search(text):
        return True
    if _DESC_CLIENT_COMPANY.search(text):
        return True
    if _COMPANY_CONSULTING.search(text):
        return True
    if _COMPANY_STAFFING.search(text):
        return True
    if _COMPANY_TALENT.search(text):
        return True
    return False


def _company_display_name_consulting_signals(company: str) -> bool:
    """LinkedIn card / listing company line (not the company-page industry field)."""
    if "IT" in company:
        return True
    if _COMPANY_CONSULTING.search(company):
        return True
    if _COMPANY_STAFFING.search(company):
        return True
    if _COMPANY_TALENT.search(company):
        return True
    return False


def _capital_it_in_title_or_company(title: str, company: str) -> bool:
    """Body-shop style ``IT`` in employer or role line only (not scanned across full description)."""
    return "IT" in (title or "") or "IT" in (company or "")


def is_consulting_listing_from_job_posting_text_only(job: dict) -> bool:
    """
    True when **title or job description** alone suggests consulting / staffing (no listing company line).

    Run this **before** listing-company checks and **before** opening LinkedIn company pages.
    """
    return _posting_text_consulting_signals(
        str(job.get("title") or ""),
        str(job.get("description") or ""),
    )


def is_consulting_listing_from_listing_company_line_only(job: dict) -> bool:
    """
    True from the **job title** and **card / listing company name** only (capital ``IT``, consulting words).

    Run after posting-text checks, still **before** any ``/company/.../about`` navigation.
    """
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    if _capital_it_in_title_or_company(title, company):
        return True
    if _company_display_name_consulting_signals(company):
        return True
    return False


def is_consulting_listing(job: dict) -> bool:
    """
    True when the job posting (title + details) or listing company name matches consulting / staffing signals.

    Equivalent to posting-text OR listing-company-line heuristics. Prefer calling the two ``*_only`` helpers
    in order in the LinkedIn pipeline so company-page fetches stay last.
    """
    return is_consulting_listing_from_job_posting_text_only(job) or is_consulting_listing_from_listing_company_line_only(
        job
    )
