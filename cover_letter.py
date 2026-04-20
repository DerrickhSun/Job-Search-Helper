"""
Cover Letter Generator
Uses a DSPy ChainOfThought module to write a tailored 3-paragraph cover letter for each job.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import dspy

log = logging.getLogger(__name__)


def normalize_cover_letter_dashes(text: str) -> str:
    """Replace em/en dashes and similar with ASCII hyphen-minus (forms often reject fancy punctuation)."""
    if not text:
        return text
    t = text.replace("\u2014", "-").replace("\u2013", "-").replace("\u2015", "-")
    t = t.replace("\u2212", "-")  # minus sign
    return t


def write_cover_letter_docx(body: str, path: Path | str) -> Path:
    """
    Write cover letter body to a ``.docx`` file (paragraphs split on blank lines).
    Requires ``python-docx``.
    """
    from docx import Document

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = normalize_cover_letter_dashes(body.strip())
    doc = Document()
    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not parts:
        doc.add_paragraph(body or "")
    else:
        for p in parts:
            doc.add_paragraph(p)
    doc.save(str(path.resolve()))
    return path


COVER_INSTRUCTION = """Write a concise, professional cover letter for this job application.

Requirements:
- Exactly 3 short paragraphs, no fluff
- Paragraph 1: Why this role fits and your relevant background
- Paragraph 2: One specific example of relevant past work or impact
- Paragraph 3: Enthusiasm for the company and a clear call to action
- Tone: confident but not arrogant, conversational but professional
- Do NOT include a date, address block, or "Dear Hiring Manager" — only the three body paragraphs
- Keep under 220 words total"""


class CoverLetterModule(dspy.Module):
    def __init__(self):
        super().__init__()
        self.write = dspy.ChainOfThought(
            "instruction, name, email, summary, skills, experience, job_title, job_company, job_description "
            "-> cover_letter: str"
        )

    def forward(
        self,
        name: str,
        email: str,
        summary: str,
        skills: str,
        experience: str,
        job_title: str,
        job_company: str,
        job_description: str,
    ):
        return self.write(
            instruction=COVER_INSTRUCTION,
            name=name,
            email=email,
            summary=summary,
            skills=skills,
            experience=experience,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
        )


class CoverLetterGenerator:
    def __init__(self):
        self._module = CoverLetterModule()

    def generate(self, resume: dict, job: dict) -> str:
        """
        Generates and returns a cover letter string.
        Falls back to a template if the DSPy call fails.
        """
        try:
            return self._generate_with_dspy(resume, job)
        except Exception as e:
            log.warning("Cover letter generation failed (%s), using template fallback", e)
            return self._template_fallback(resume, job)

    def _generate_with_dspy(self, resume: dict, job: dict) -> str:
        experience_summary = "; ".join(
            f"{r['title']} at {r['company']}" for r in resume.get("experience", [])[:3]
        )

        result = self._module(
            name=resume.get("name", ""),
            email=resume.get("email", ""),
            summary=(resume.get("summary", "") or "")[:500],
            skills=", ".join(resume.get("skills", [])[:12]),
            experience=experience_summary or "Not provided",
            job_title=job.get("title", ""),
            job_company=job.get("company", ""),
            job_description=(job.get("description", ""))[:4000],
        )

        return normalize_cover_letter_dashes(result.cover_letter.strip())

    def _template_fallback(self, resume: dict, job: dict) -> str:
        name = resume.get("name", "Candidate")
        skills = ", ".join(resume.get("skills", [])[:5])
        return normalize_cover_letter_dashes(
            f"I am excited to apply for the {job.get('title')} position at {job.get('company')}. "
            f"With expertise in {skills}, I believe I would be a strong addition to your team.\n\n"
            f"Throughout my career, I have consistently delivered results in similar roles and am confident "
            f"I can bring that same dedication to {job.get('company')}.\n\n"
            f"I would welcome the opportunity to discuss how my background aligns with your needs. "
            f"Thank you for considering my application.\n\nSincerely,\n{name}"
        )
