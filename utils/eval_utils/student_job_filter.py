"""
Heuristics to classify a listing as student-only, hybrid, or non-student, and to decide whether a
given classification passes the user's configured ``student_job_mode``.

**Student terms** — any of:
  - ``"pursuing"`` followed by 5 or fewer words then ``"degree"`` (e.g. "pursuing a Bachelor's
    degree", "pursuing a Master of Science degree in Computer Science")
  - ``"current student"``
  - ``"enrolled in"``

**Classification** (checked in this order — first match wins):
  1. Student terms present **and** ``"graduated"`` present → ``hybrid`` (a posting that mentions
     both is read as open to current students *and* graduates, not student-only).
  2. Title contains intern-family text (``intern``/``interns``/``internship``/``internships`` —
     enumerated suffixes, not a bare ``"intern"`` substring, so "international"/"internal" never
     match) **and** no student terms anywhere → ``hybrid``.
  3. Student terms present (and, by elimination from rule 1, no "graduated") → ``student``. This
     covers an intern-titled posting that also has student terms but no "graduated" — still
     ``student``, not ``hybrid``; the intern-title rule only promotes to hybrid when student terms
     are otherwise absent entirely.
  4. Everything else → ``non_student``.
"""

from __future__ import annotations

import re

_PURSUING_DEGREE_RE = re.compile(r"\bpursuing\b(?:\s+\S+){0,5}\s+degree\b", re.IGNORECASE)
_CURRENT_STUDENT_RE = re.compile(r"\bcurrent\s+student\b", re.IGNORECASE)
_ENROLLED_IN_RE = re.compile(r"\benrolled\s+in\b", re.IGNORECASE)
_GRADUATED_RE = re.compile(r"\bgraduated\b", re.IGNORECASE)
# Enumerated suffixes (not a bare "intern" prefix) so "international"/"internal" never match.
_INTERN_TITLE_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)

STUDENT_JOB_MODES = ("student_only", "non_student_only", "both")


def has_student_terms(text: str) -> bool:
    return bool(
        _PURSUING_DEGREE_RE.search(text) or _CURRENT_STUDENT_RE.search(text) or _ENROLLED_IN_RE.search(text)
    )


def classify_student_job(title: str, description: str) -> str:
    """Return ``"student"``, ``"hybrid"``, or ``"non_student"`` — see module docstring for rules."""
    text = f"{title or ''}\n{description or ''}"
    student_terms = has_student_terms(text)
    graduated = bool(_GRADUATED_RE.search(text))
    intern_title = bool(_INTERN_TITLE_RE.search(title or ""))

    if student_terms and graduated:
        return "hybrid"
    if intern_title and not student_terms:
        return "hybrid"
    if student_terms:
        return "student"
    return "non_student"


def student_job_passes_filter(classification: str, mode: str) -> bool:
    """Hybrid always passes; student/non_student only pass under the matching mode (or "both")."""
    if classification == "hybrid" or mode == "both":
        return True
    if mode == "student_only":
        return classification == "student"
    if mode == "non_student_only":
        return classification == "non_student"
    return True  # unrecognized mode -- fail open, matching this codebase's general tolerance for bad config
