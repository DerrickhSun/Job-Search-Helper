"""
Job Matcher
Two-stage evaluation:

1) **Hard gates** (``gates_pass``) — all must pass or the job is skipped without a fit score:
   - Education: candidate's highest degree at or above the job's minimum (ordinal scale).
   - Experience: candidate's estimated years >= the job's minimum when the posting states one.
     If the posting asks for **senior-level** experience, or the **job title** contains **Senior**, **Lead**, or
     **Manager** as a role level, but gives **no numeric** years floor, the gate assumes **5 years** (regex
     backstop + LLM instruction).
   - **Clearance (regex):** evaluation is **per line** (split on newlines in description + title) so patterns
     cannot span unrelated sentences. If **any one line** contains ``active`` … ``clearance`` (both words on
     that same line) and that **same line** does **not** contain ``eligible`` or ``valid`` as whole words, the
     job is skipped (treated as requiring an already-held clearance with no eligible/valid-clearance wording
     on that line).

   Unstated minima (education ``unspecified`` / years ``-1`` or missing) impose no bar, except the
   title/level-without-number case above (Senior / Lead / Manager in title, or senior-level experience in copy).

2) **Fit rating** (``fit_score``) — a float in ``[0, 1]`` from an LLM (skills, roles, domain vs the
   posting). Compared to ``--min-score`` after gates pass. ``score()`` returns this fit when gates pass,
   else ``0.0`` (backward-compatible for callers that expect one number).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import dspy

from .resume_parser import experience_entry_description

log = logging.getLogger(__name__)


def print_job_fit_debug(
    company: str | None,
    title: str | None,
    fit: float | None,
    *,
    note: str = "",
) -> None:
    """
    Print one line to stdout (company, title, fit) for terminal debugging. Independent of log level.
    Use ``fit=None`` when the job was not fit-scored (e.g. hard gates failed).
    """
    c = (company or "").replace("\r", " ").replace("\n", " ").strip() or "(no company)"
    ti = (title or "").replace("\r", " ").replace("\n", " ").strip() or "(no title)"
    if fit is None:
        fs = "(not scored)"
    else:
        fs = f"{float(fit):.4f}"
    suffix = f" | {note}" if note else ""
    print(f"[job-fit] company={c!r} | title={ti!r} | fit={fs}{suffix}", flush=True)


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
(e.g. "3+ years of experience" → 3, "at least 5 years" → 5, "3-5 years of professional experience" → 3,
"5–7 years of work experience" → 5). Treat the left number in an M–N range as the stated minimum floor.
If the posting does not state any minimum years of professional/work experience, use -1. (-1 means
**no minimum years** — any amount of experience, including zero, passes this gate.)

If the posting lists several explicit year minima (for example per-skill lines like "3+ years of work
experience with Node.js" and "5+ years of work experience with TypeScript"), use the **largest**
such number as minimum_years_experience — the candidate must meet each stated floor, so the strictest
single-year bar is the max, not the smallest line item.

Senior level without a number: if the posting requires **senior-level** experience (phrases like
"senior level experience", "senior-level experience", "experience at the senior level") and **does not**
anywhere state a numeric minimum years of experience (no "3+ years", "at least 5 years", etc.), set
minimum_years_experience to **5**.

Title role level without a number: the same **5** applies when the **job title** contains any of these
as **whole words** (case-insensitive): **Senior**, **Lead**, or **Manager** — e.g. "Senior Software Engineer",
"Team Lead", "Engineering Manager", "Product Manager" — and there is still **no** numeric years floor in the
title or description. If numeric minima are stated anywhere, use the **largest** such number only — do not
add 5 on top when explicit year floors already exist.

Do not infer stricter requirements than written. If numbers are ambiguous or clearly only
nice-to-have, use -1."""

FIT_INSTRUCTION = """You rate how strong a fit the candidate is for this job on a scale from 0.0 to 1.0.

Use the resume summary (skills, roles, domain) against the job title, company, and description.
Consider technology overlap, similar past responsibilities, and domain alignment.

Guidance: 0.85+ excellent match; 0.65–0.84 solid match; 0.45–0.64 possible but weaker; below 0.45 poor fit.

Do NOT downgrade the score for degree level, minimum years of experience, or clearance wording —
those are evaluated in hard gates separately.

Return fit_score as a single number between 0.0 and 1.0 inclusive."""


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


class JobFitRatingModule(dspy.Module):
    def __init__(self):
        super().__init__()
        self.rate = dspy.ChainOfThought(
            "instruction, resume_summary, job_title, job_company, job_description "
            "-> fit_score: float, rationale: str"
        )

    def forward(
        self,
        resume_summary: str,
        job_title: str,
        job_company: str,
        job_description: str,
    ):
        return self.rate(
            instruction=FIT_INSTRUCTION,
            resume_summary=resume_summary,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
        )


class JobMatcher:
    def __init__(self):
        self._req_module = JobRequirementsModule()
        self._fit_module = JobFitRatingModule()

    def gates_pass(self, resume: dict, job: dict) -> bool:
        """
        True when education, experience, and clearance heuristics from the posting are satisfied (or unstated).
        On LLM failure, uses regex heuristics on the description and title (same as historical fallback).
        """
        try:
            return self._gates_pass_llm(resume, job)
        except Exception as e:
            log.warning("Gate check failed (%s), using fallback heuristics", e)
            return self._gates_pass_regex(resume, job)

    def fit_score(self, resume: dict, job: dict) -> float:
        """
        Semantic fit in ``[0, 1]`` after hard gates. Call only when ``gates_pass`` is true to save
        an extra LLM call; this method does not re-check gates.
        """
        desc = (job.get("description") or "")[:8000]
        digest = _resume_digest(resume)
        try:
            result = self._fit_module(
                resume_summary=digest,
                job_title=job.get("title", ""),
                job_company=job.get("company", ""),
                job_description=desc,
            )
            parsed = _parse_float_0_1(getattr(result, "fit_score", None))
            if parsed is not None:
                r = _as_str_list(getattr(result, "rationale", None))
                log.debug("Fit score LLM: %.3f | %s", parsed, r)
                return parsed
        except Exception as e:
            log.warning("Fit score LLM failed (%s), using skill overlap heuristic", e)
        return _fit_score_heuristic(resume, job)

    def score(self, resume: dict, job: dict) -> float:
        """``0.0`` if hard gates fail; otherwise same as ``fit_score`` (for tracker / helper one-shot)."""
        if not self.gates_pass(resume, job):
            return 0.0
        return self.fit_score(resume, job)

    def _gates_pass_llm(self, resume: dict, job: dict) -> bool:
        desc_full = job.get("description") or ""
        desc = desc_full[:8000]
        result = self._req_module(
            job_title=job.get("title", ""),
            job_company=job.get("company", ""),
            job_description=desc,
        )

        req_edu_llm = _normalize_education_token(getattr(result, "minimum_education", None))
        req_years_llm = _parse_years_requirement(getattr(result, "minimum_years_experience", None))
        req_edu_rx, req_years_rx = _extract_job_requirements_regex(
            desc_full, str(job.get("title") or "")
        )

        req_edu = _merge_education_requirements(req_edu_llm, req_edu_rx)
        req_years = _merge_years_requirements(req_years_llm, req_years_rx)

        if req_years != req_years_llm or req_edu != req_edu_llm:
            log.info(
                "Gate requirements merged (LLM + regex backstop on full description): "
                "years llm=%s rx=%s → eff=%s | edu llm=%s rx=%s → eff=%s",
                req_years_llm,
                req_years_rx,
                req_years,
                req_edu_llm,
                req_edu_rx,
                req_edu,
            )

        cand_edu = _highest_education_rank(resume)
        cand_years = _estimate_years_experience(resume)

        ok_edu = _education_gate(cand_edu, req_edu)
        ok_exp = _experience_gate(cand_years, req_years)
        ok_clear = _clearance_eligibility_gate_passes(job)

        r = _as_str_list(getattr(result, "rationale", None))
        log.debug(
            "Gates: candidate edu_rank=%s years≈%.1f | required edu=%s years=%s | "
            "edu_ok=%s exp_ok=%s clear_ok=%s | %s",
            cand_edu,
            cand_years,
            req_edu,
            req_years,
            ok_edu,
            ok_exp,
            ok_clear,
            r,
        )

        jid = job.get("id", "")
        jlabel = f"id={jid} " if jid else ""
        need_y = (
            f"{req_years:g}"
            if req_years is not None and req_years >= 0
            else "unspecified"
        )
        if ok_edu and ok_exp and ok_clear:
            log.info(
                "Gates passed [llm+regex] %s%r at %r | eff min yrs=%s min edu=%s | "
                "candidate yrs≈%.1f (date-span/roles heuristic; see _estimate_years_experience) "
                "edu=%s (rank %d) | clearance_gate=ok",
                jlabel,
                job.get("title", ""),
                job.get("company", ""),
                need_y,
                req_edu,
                cand_years,
                EDU_ORDER[cand_edu],
                cand_edu,
            )
            return True
        log.info(
            "Gates failed [llm+regex] %s%r at %r | edu_ok=%s exp_ok=%s clear_ok=%s | "
            "need min edu %s min yrs %s | have edu %s (rank %s) yrs≈%.1f",
            jlabel,
            job.get("title", ""),
            job.get("company", ""),
            ok_edu,
            ok_exp,
            ok_clear,
            req_edu,
            need_y,
            EDU_ORDER[cand_edu],
            cand_edu,
            cand_years,
        )
        return False

    def _gates_pass_regex(self, resume: dict, job: dict) -> bool:
        desc = job.get("description") or ""
        title = str(job.get("title") or "")
        req_edu, req_years = _extract_job_requirements_regex(desc, title)
        cand_edu = _highest_education_rank(resume)
        cand_years = _estimate_years_experience(resume)
        ok_edu = _education_gate(cand_edu, req_edu)
        ok_exp = _experience_gate(cand_years, req_years)
        ok_clear = _clearance_eligibility_gate_passes(job)
        log.debug(
            "Fallback gates: cand edu=%s yrs≈%.1f req edu=%s yrs=%s -> edu_ok=%s exp_ok=%s clear_ok=%s",
            cand_edu,
            cand_years,
            req_edu,
            req_years,
            ok_edu,
            ok_exp,
            ok_clear,
        )
        jid = job.get("id", "")
        jlabel = f"id={jid} " if jid else ""
        need_y = (
            f"{req_years:g}"
            if req_years is not None and req_years >= 0
            else "unspecified"
        )
        if ok_edu and ok_exp and ok_clear:
            log.info(
                "Gates passed [regex_fallback] %s%r at %r | min yrs=%s min edu=%s | "
                "candidate yrs≈%.1f edu=%s (rank %d) | clearance_gate=ok",
                jlabel,
                job.get("title", ""),
                job.get("company", ""),
                need_y,
                req_edu,
                cand_years,
                EDU_ORDER[cand_edu],
                cand_edu,
            )
            return True
        log.info(
            "Gates failed [regex_fallback] %s%r at %r | edu_ok=%s exp_ok=%s clear_ok=%s | "
            "need min edu %s min yrs %s | have edu %s (rank %s) yrs≈%.1f",
            jlabel,
            job.get("title", ""),
            job.get("company", ""),
            ok_edu,
            ok_exp,
            ok_clear,
            req_edu,
            need_y,
            EDU_ORDER[cand_edu],
            cand_edu,
            cand_years,
        )
        return False


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


_RE_ACTIVE_THEN_CLEARANCE_SAME_LINE = re.compile(r"\bactive\b.*\bclearance\b", re.IGNORECASE)
_RE_ELIGIBLE_OR_VALID_SAME_LINE = re.compile(r"\b(eligible|valid)\b", re.IGNORECASE)


def _clearance_eligibility_gate_passes(job: dict) -> bool:
    """
    False (skip job) when **one line** (newline-delimited slice of title + description) contains
    ``active`` … ``clearance`` on that line **and** that **same line** contains **neither** whole-word
    ``eligible`` nor whole-word ``valid`` (either word on that line is enough to pass the gate for that line).

    Matching never spans lines, so a clearance phrase on one line and ``eligible`` on another does not rescue
    the line — only wording on the **same** line counts.
    """
    blob = f"{job.get('description') or ''}\n{job.get('title') or ''}"
    for raw_line in blob.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not _RE_ACTIVE_THEN_CLEARANCE_SAME_LINE.search(line):
            continue
        if _RE_ELIGIBLE_OR_VALID_SAME_LINE.search(line):
            continue
        log.info(
            "Clearance gate: skip — same line has active…clearance without 'eligible' or 'valid': %s",
            line[:240] + ("…" if len(line) > 240 else ""),
        )
        return False
    return True


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
    Approximate professional years for gate comparison.

    Uses the **calendar span** ``max(year) - min(year)`` from four-digit years found in experience
    descriptions/dates (or ``raw_text`` if no experience text). That can **overestimate** overlapping roles
    or long education-adjacent spans — if gates pass unexpectedly, check INFO logs for
    ``candidate yrs≈`` vs ``eff min yrs``.

    Optional ``resume["experience_years_cap"]`` (number): for gates only, the estimate is
    ``min(heuristic, cap)`` so calendar span does not imply more seniority than you want to claim.
    """
    chunks: list[str] = []
    for r in resume.get("experience") or []:
        if not isinstance(r, dict):
            continue
        chunks.append(str(r.get("dates") or ""))
        desc = experience_entry_description(r)
        if desc:
            chunks.append(desc)
    text = " ".join(chunks)
    if not text.strip():
        text = resume.get("raw_text") or ""

    years_found = [int(m.group(0)) for m in re.finditer(r"\b(19|20)\d{2}\b", text)]
    if len(years_found) >= 2:
        estimate = float(max(years_found) - min(years_found))
    elif len(years_found) == 1:
        estimate = 2.0
    else:
        roles = [r for r in (resume.get("experience") or []) if isinstance(r, dict)]
        if roles:
            estimate = float(max(1, len(roles)))
        else:
            estimate = 0.0

    cap = resume.get("experience_years_cap")
    if cap is not None:
        try:
            c = float(cap)
            if c >= 0.0:
                estimate = min(estimate, c)
        except (TypeError, ValueError):
            pass
    return estimate


def _extract_job_requirements_regex(description: str, title: str | None = None) -> tuple[str, float | None]:
    """
    Rough fallback: infer minimum education and years from keywords.

    ``title`` and ``description`` are combined so numeric floors and **Senior**-in-title heuristics apply
    consistently (LinkedIn, Greenhouse, and regex-only fallback).

    Years patterns include ``N(+)? years of experience`` (with optional *professional / work / relevant*
    before *experience*), ``M–N years of … experience`` (minimum ``M``), ``N(+)? years of work experience``,
    ``N(+)? years of <phrase> experience`` (domain-specific tenure implies at least ``N`` years overall),
    and a few ``minimum/over`` forms.
    """
    parts: list[str] = []
    if (title or "").strip():
        parts.append(str(title).strip())
    if (description or "").strip():
        parts.append(str(description).strip())
    t = " ".join(parts).lower() if parts else ""
    req_edu = "unspecified"
    if re.search(r"\b(ph\.?d|doctorate|doctoral)\b", t):
        req_edu = "doctorate"
    elif re.search(r"\bmaster'?s?\b|\bmba\b|\bms\b|\bm\.s\.\b", t):
        req_edu = "master"
    elif re.search(r"\bbachelor'?s?\b|\bundergraduate\b|\bbs\b|\bba\b|\bb\.s\.\b", t):
        req_edu = "bachelor"
    elif "associate" in t:
        req_edu = "associate"

    # Optional words between "of" and "experience" (e.g. "professional", "hands-on") so we still catch
    # "5 years of professional experience" and "3-5 years of professional experience".
    _of_exp = r"of\s+(?:professional\s+|work\s+|relevant\s+|hands-on\s+)?experience\b"
    # Avoid treating the second bound in "3-5 years …" as a separate "5 years …" floor.
    _yr_lead = r"(?:^|[^0-9\-–])(\d+)\s*\+?\s*years?"
    nums: list[float] = []
    for m in re.finditer(
        r"(?:at least|minimum|min\.?|over)\s+(\d+)\s*\+?\s*years?\s+(?:of\s+)?(?:work|professional|relevant)?",
        t,
    ):
        nums.append(float(m.group(1)))
    # "N years of experience" / "N+ years of experience" / "N years of professional experience"
    for m in re.finditer(rf"{_yr_lead}\s+{_of_exp}", t):
        nums.append(float(m.group(1)))
    # LinkedIn / poster lines: "N+ years of work experience with …" (before "with" clause)
    for m in re.finditer(rf"{_yr_lead}\s+of\s+work\s+experience\b", t):
        nums.append(float(m.group(1)))
    # "N years of <domain> experience" — domain-specific tenure implies at least N years overall
    for m in re.finditer(
        rf"{_yr_lead}\s+of\s+(?!experience\b)(.+?)\s+experience\b",
        t,
    ):
        nums.append(float(m.group(1)))
    for m in re.finditer(rf"(\d+)\s*[-–]\s*(\d+)\s*years?\s+{_of_exp}", t):
        nums.append(float(m.group(1)))
    for m in re.finditer(
        r"(\d+)\s*[-–]\s*(\d+)\s*years?\s+of\s+(?!experience\b)(.+?)\s+experience\b",
        t,
    ):
        nums.append(float(m.group(1)))
    if nums:
        # Use the strictest (largest) detected floor so we do not under-read the posting when
        # multiple phrases mention different year counts.
        req_years_f = max(nums)
    else:
        req_years_f = None
        # Posting asks for senior-level experience but states no numeric floor → assume 5 years.
        if _regex_implied_years_senior_level_no_numeric(t):
            req_years_f = 5.0
        elif _title_implies_five_years_role_level_no_numeric_floor(title):
            req_years_f = 5.0

    return req_edu, req_years_f


def _title_implies_five_years_role_level_no_numeric_floor(title: str | None) -> bool:
    """
    True when the job **title** suggests mid/senior role level via whole-word **Senior**, **Lead**, or
    **Manager** (case-insensitive). Used only when no numeric year minima exist in title + description.
    """
    if not (title or "").strip():
        return False
    t = title.strip()
    return any(
        re.search(p, t, re.IGNORECASE)
        for p in (
            r"\bsenior\b",
            r"\blead\b",
            r"\bmanager\b",
        )
    )


def _regex_implied_years_senior_level_no_numeric(text_lower: str) -> bool:
    """
    True when copy ties **senior level** to **experience** in the posting body (not satisfied by title alone).
    Used only when no numeric year minima were found in the combined title + description text.
    """
    if not text_lower or "senior" not in text_lower or "experience" not in text_lower:
        return False
    patterns = (
        r"senior[-\s]+level(?:\s+of)?\s+(?:professional\s+|work\s+)?experience\b",
        r"(?:professional\s+|work\s+)?experience\s+at\s+(?:a\s+)?(?:the\s+)?senior[-\s]+level\b",
        r"senior[-\s]+level.{0,80}\b(?:professional\s+|work\s+)?experience\b",
        r"\b(?:professional\s+|work\s+)?experience\b.{0,80}senior[-\s]+level\b",
    )
    return any(re.search(p, text_lower) for p in patterns)


def _merge_years_requirements(y_llm: float | None, y_rx: float | None) -> float | None:
    """Largest stated minimum wins (stricter posting bar). ``None`` if neither source states a floor."""
    vals = [float(y) for y in (y_llm, y_rx) if y is not None and float(y) >= 0.0]
    return max(vals) if vals else None


def _merge_education_requirements(a: str, b: str) -> str:
    """Stricter degree requirement wins; ``unspecified`` only when neither source states a bar."""
    ra = EDU_RANK.get(a, -1) if a != "unspecified" else -1
    rb = EDU_RANK.get(b, -1) if b != "unspecified" else -1
    m = max(ra, rb)
    if m < 0:
        return "unspecified"
    return EDU_ORDER[m]


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


def _clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


def _parse_float_0_1(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            s = raw.strip().replace("`", "").replace("*", "")
            if not s:
                return None
            s = s.split()[0] if s.split() else s
            return _clamp01(float(s.replace(",", "")))
        return _clamp01(float(raw))
    except (TypeError, ValueError):
        return None


def _resume_digest(resume: dict, *, max_chars: int = 6000) -> str:
    parts: list[str] = []
    skills = resume.get("skills") or []
    if skills:
        parts.append("Skills: " + ", ".join(str(s) for s in skills[:100]))
    for r in (resume.get("experience") or [])[:8]:
        if isinstance(r, dict):
            line = f"Role: {r.get('title', '')} at {r.get('company', '')} | {r.get('dates', '')}"
            desc = experience_entry_description(r)
            if desc:
                line += "\n" + desc
            parts.append(line)
    for e in (resume.get("education") or [])[:5]:
        if isinstance(e, dict):
            parts.append(
                f"Education: {e.get('degree', '')} — {e.get('institution', '')} ({e.get('year', '')})"
            )
    blob = "\n\n".join(parts).strip()
    if not blob:
        blob = (resume.get("raw_text") or "")[:max_chars]
    return blob[:max_chars]


def _fit_score_heuristic(resume: dict, job: dict) -> float:
    blob = ((job.get("description") or "") + " " + (job.get("title") or "")).lower()
    if not blob.strip():
        return 0.45
    skills = [
        str(s).lower().strip()
        for s in (resume.get("skills") or [])
        if str(s).strip() and len(str(s).strip()) > 1
    ]
    if not skills:
        return 0.45
    hits = sum(1 for s in skills if s in blob)
    ratio = hits / len(skills)
    return _clamp01(0.2 + 0.75 * ratio)
