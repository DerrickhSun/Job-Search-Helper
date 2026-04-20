"""
Job Matcher
Determines match by two gates (both must pass):
  1) Education — candidate's highest degree must be at or above the job's minimum (ordinal scale).
  2) Experience — candidate's estimated years of work experience must be >= the job's minimum.

**Unspecified requirements:** If the posting does not state a minimum for education or for years of
experience, that requirement is treated as **no bar** — any level of education / any amount of
experience satisfies the gate (including zero years when nothing is stated).

Scores are 1.0 (match) or 0.0 (no match). Jobs below --min-score are skipped (use default 0.6).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import dspy

log = logging.getLogger(__name__)

# Higher = more education. Compare with >= for "meets or exceeds".
EDU_ORDER = ("none", "high_school", "associate", "bachelor", "master", "doctorate")
EDU_RANK: dict[str, int] = {name: i for i, name in enumerate(EDU_ORDER)}

REQUIREMENTS_INSTRUCTION = """You extract MINIMUM stated qualifications from a job posting text.

minimum_education — must be EXACTLY one of these strings:
  none | high_school | associate | bachelor | master | doctorate | unspecified

Rules:
- Use "unspecified" if the posting does not clearly state a minimum degree level. Unspecified means
  there is **no minimum** — any degree level (or none listed on the resume) is acceptable for this gate.
- Map PhD, Ph.D., DPhil, Doctorate, doctoral degree → doctorate
- Map Master's, MS, MA, MBA, M.S., MSc → master
- Map Bachelor's, BS, BA, B.S., B.A., undergraduate degree → bachelor
- Map Associate's, AA, AS → associate
- Map high school diploma / GED → high_school

minimum_years_experience — a non-negative number: the smallest years-of-experience requirement stated
(e.g. "3+ years of experience" → 3, "at least 5 years" → 5).
If the posting does not state any minimum years of professional/work experience, use -1. (-1 means
**no minimum years** — any amount of experience, including zero, passes this gate.)

Do not infer stricter requirements than written. If multiple numbers appear, use the minimum years
required for the role as a whole (not preferred/nice-to-have) when possible; if unclear, use -1."""


class JobRequirementsModule(dspy.Module):
    def __init__(self):
        super().__init__()
        self.extract = dspy.ChainOfThought(
            "instruction, job_title, job_company, job_description "
            "-> minimum_education: str, minimum_years_experience: float, rationale: str"
        )

    def forward(
        self,
        job_title: str,
        job_company: str,
        job_description: str,
    ):
        return self.extract(
            instruction=REQUIREMENTS_INSTRUCTION,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
        )


class JobMatcher:
    def __init__(self):
        self._req_module = JobRequirementsModule()

    def score(self, resume: dict, job: dict) -> float:
        """
        Returns 1.0 if education and experience gates both pass, else 0.0.
        Unstated minima (education ``unspecified`` / years ``-1`` or missing) impose no requirement.
        Falls back to heuristic requirement parsing if DSPy fails.
        """
        try:
            return self._score_gates(resume, job)
        except Exception as e:
            log.warning("Match scoring failed (%s), using fallback heuristics", e)
            return self._score_fallback_gates(resume, job)

    def _score_gates(self, resume: dict, job: dict) -> float:
        desc = (job.get("description") or "")[:8000]
        result = self._req_module(
            job_title=job.get("title", ""),
            job_company=job.get("company", ""),
            job_description=desc,
        )

        req_edu = _normalize_education_token(getattr(result, "minimum_education", None))
        req_years = _parse_years_requirement(getattr(result, "minimum_years_experience", None))

        cand_edu = _highest_education_rank(resume)
        cand_years = _estimate_years_experience(resume)

        ok_edu = _education_gate(cand_edu, req_edu)
        ok_exp = _experience_gate(cand_years, req_years)

        r = _as_str_list(getattr(result, "rationale", None))
        log.debug(
            "Gates: candidate edu_rank=%s years≈%.1f | required edu=%s years=%s | edu_ok=%s exp_ok=%s | %s",
            cand_edu,
            cand_years,
            req_edu,
            req_years,
            ok_edu,
            ok_exp,
            r,
        )

        if ok_edu and ok_exp:
            return 1.0
        need_y = (
            f"{req_years:g}"
            if req_years is not None and req_years >= 0
            else "unspecified"
        )
        log.info(
            "Gates failed (edu_ok=%s exp_ok=%s) — need min edu %s, min yrs %s; "
            "have edu %s (rank %s), yrs≈%.1f",
            ok_edu,
            ok_exp,
            req_edu,
            need_y,
            EDU_ORDER[cand_edu],
            cand_edu,
            cand_years,
        )
        return 0.0

    def _score_fallback_gates(self, resume: dict, job: dict) -> float:
        desc = job.get("description") or ""
        req_edu, req_years = _extract_job_requirements_regex(desc)
        cand_edu = _highest_education_rank(resume)
        cand_years = _estimate_years_experience(resume)
        ok_edu = _education_gate(cand_edu, req_edu)
        ok_exp = _experience_gate(cand_years, req_years)
        log.debug(
            "Fallback gates: cand edu=%s yrs≈%.1f req edu=%s yrs=%s -> edu_ok=%s exp_ok=%s",
            cand_edu,
            cand_years,
            req_edu,
            req_years,
            ok_edu,
            ok_exp,
        )
        if ok_edu and ok_exp:
            return 1.0
        need_y = (
            f"{req_years:g}"
            if req_years is not None and req_years >= 0
            else "unspecified"
        )
        log.info(
            "Gates failed (fallback) (edu_ok=%s exp_ok=%s) — need min edu %s, min yrs %s; "
            "have edu %s (rank %s), yrs≈%.1f",
            ok_edu,
            ok_exp,
            req_edu,
            need_y,
            EDU_ORDER[cand_edu],
            cand_edu,
            cand_years,
        )
        return 0.0


def _education_gate(candidate_rank: int, required_token: str) -> bool:
    """If the job does not state a minimum degree, ``unspecified`` — any candidate level is OK."""
    if required_token == "unspecified":
        return True
    need = EDU_RANK.get(required_token, 0)
    return candidate_rank >= need


def _experience_gate(candidate_years: float, required_years: float | None) -> bool:
    """If the job does not state minimum years (``None`` or negative), any experience level is OK."""
    if required_years is None or required_years < 0:
        return True
    return candidate_years >= float(required_years)


def _normalize_education_token(raw: Any) -> str:
    if raw is None:
        return "unspecified"
    s = str(raw).strip().lower()
    if not s or s in ("unspecified", "unknown", "n/a", "na", "none stated", "not stated"):
        return "unspecified"
    s = s.replace("'", "").replace(".", " ")
    if "doctorate" in s or "doctoral" in s or "phd" in s or "ph d" in s or "dphil" in s:
        return "doctorate"
    if "master" in s or " mba " in f" {s} " or s.strip() in ("ms", "ma", "mba", "msc", "m s", "m a"):
        return "master"
    if "bachelor" in s or "undergraduate" in s or s.strip() in ("bs", "ba", "b s", "b a"):
        return "bachelor"
    if "associate" in s or s.strip() in ("aa", "as"):
        return "associate"
    if "high school" in s or s.strip() == "ged":
        return "high_school"
    if s.strip() == "none":
        return "none"
    for name in reversed(EDU_ORDER):  # match most specific token first
        if name != "none" and name in s.replace("_", " "):
            return name
    return "unspecified"


def _degree_string_to_rank(text: str) -> int:
    t = (text or "").lower()
    if any(k in t for k in ("ph.d", "phd", "d.phil", "doctorate", "doctoral")):
        return EDU_RANK["doctorate"]
    if any(
        k in t
        for k in (
            "master",
            " m.s",
            " m.a",
            "mba",
            "msc",
            "m.s.",
            "m.a.",
        )
    ):
        return EDU_RANK["master"]
    if any(k in t for k in ("bachelor", " b.s", " b.a", "bs ", "ba ", "b.s.", "b.a.", "undergraduate")):
        return EDU_RANK["bachelor"]
    if "associate" in t or re.search(r"\baa\b|\bas\b", t):
        return EDU_RANK["associate"]
    if any(k in t for k in ("high school", "ged", "diploma")):
        return EDU_RANK["high_school"]
    return EDU_RANK["none"]


def _highest_education_rank(resume: dict) -> int:
    best = EDU_RANK["none"]
    for e in resume.get("education") or []:
        deg = e.get("degree") if isinstance(e, dict) else ""
        best = max(best, _degree_string_to_rank(str(deg)))
    return best


def _parse_years_requirement(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        y = float(raw)
        return None if y < 0 else y
    s = str(raw).strip()
    if not s:
        return None
    try:
        y = float(s.replace(",", ""))
        return None if y < 0 else y
    except ValueError:
        return None


def _estimate_years_experience(resume: dict) -> float:
    """
    Approximate professional years: career span from year mentions in experience lines,
    else a lower bound from the number of listed roles.
    """
    chunks: list[str] = []
    for r in resume.get("experience") or []:
        if not isinstance(r, dict):
            continue
        chunks.append(str(r.get("dates") or ""))
        chunks.extend(str(b) for b in (r.get("bullets") or []))
    text = " ".join(chunks)
    if not text.strip():
        text = resume.get("raw_text") or ""

    years_found = [int(m.group(0)) for m in re.finditer(r"\b(19|20)\d{2}\b", text)]
    if len(years_found) >= 2:
        return float(max(years_found) - min(years_found))
    if len(years_found) == 1:
        return 2.0

    roles = [r for r in (resume.get("experience") or []) if isinstance(r, dict)]
    if roles:
        return float(max(1, len(roles)))

    return 0.0


def _extract_job_requirements_regex(description: str) -> tuple[str, float | None]:
    """Rough fallback: infer minimum education and years from keywords."""
    t = (description or "").lower()
    req_edu = "unspecified"
    if re.search(r"\b(ph\.?d|doctorate|doctoral)\b", t):
        req_edu = "doctorate"
    elif re.search(r"\bmaster'?s?\b|\bmba\b|\bms\b|\bm\.s\.\b", t):
        req_edu = "master"
    elif re.search(r"\bbachelor'?s?\b|\bundergraduate\b|\bbs\b|\bba\b|\bb\.s\.\b", t):
        req_edu = "bachelor"
    elif "associate" in t:
        req_edu = "associate"

    req_years: float | None = None
    nums: list[float] = []
    for m in re.finditer(
        r"(?:at least|minimum|min\.?|over)\s+(\d+)\s*\+?\s*years?\s+(?:of\s+)?(?:work|professional|relevant)?",
        t,
    ):
        nums.append(float(m.group(1)))
    for m in re.finditer(r"(\d+)\s*\+\s*years?\s+of\s+experience", t):
        nums.append(float(m.group(1)))
    for m in re.finditer(r"(\d+)\s*[-–]\s*(\d+)\s*years?\s+of\s+experience", t):
        nums.append(float(m.group(1)))
    if nums:
        req_years_f = min(nums)
    else:
        req_years_f = None

    return req_edu, req_years_f


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]
