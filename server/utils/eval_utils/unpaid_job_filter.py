"""
Heuristic to detect an unpaid listing: the word "unpaid" appearing anywhere in the title or
description. Unlike the student-job filter, there's no hybrid case here — a listing either says
"unpaid" or it doesn't.
"""

from __future__ import annotations

import re

_UNPAID_RE = re.compile(r"\bunpaid\b", re.IGNORECASE)

UNPAID_JOB_MODES = ("include", "exclude")


def is_unpaid_job(title: str, description: str) -> bool:
    text = f"{title or ''}\n{description or ''}"
    return bool(_UNPAID_RE.search(text))


def unpaid_job_passes_filter(is_unpaid: bool, mode: str) -> bool:
    """"exclude" rejects unpaid listings; "include" (or an unrecognized mode) passes everything."""
    if mode == "exclude":
        return not is_unpaid
    return True
