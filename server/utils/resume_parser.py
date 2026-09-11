"""
Resume Parser
Extracts structured data from PDF or DOCX resumes using PyMuPDF and regex/spaCy.
"""

import re
from pathlib import Path
from typing import Any


def project_entry_description(project: dict[str, Any]) -> str:
    """
    Body text for one ``projects`` item: ``description`` when present, otherwise legacy ``bullets``
    joined with newlines.
    """
    if not isinstance(project, dict):
        return ""
    d = str(project.get("description") or "").strip()
    if d:
        return d
    bullets = project.get("bullets")
    if isinstance(bullets, (list, tuple)):
        return "\n".join(str(b).strip() for b in bullets if str(b).strip())
    return ""


def experience_entry_description(role: dict[str, Any]) -> str:
    """
    Body text for one ``experience`` item: ``description`` when present, otherwise legacy ``bullets``
    joined with newlines (older ``resume_profile.json`` from before the description field).
    """
    if not isinstance(role, dict):
        return ""
    d = str(role.get("description") or "").strip()
    if d:
        return d
    bullets = role.get("bullets")
    if isinstance(bullets, (list, tuple)):
        return "\n".join(str(b).strip() for b in bullets if str(b).strip())
    return ""


def first_name_from_resume(resume: dict) -> str | None:
    """First name token from parsed ``resume`` (``name`` field). Used e.g. to detect LinkedIn login UI."""
    name = (resume.get("name") or "").strip()
    if not name:
        return None
    parts = name.split()
    return parts[0] if parts else None


class ResumeParser:
    """
    Parses a resume file (PDF or DOCX) into structured data.

    Returns a dict with:
      - raw_text: full text of the resume
      - name: candidate name (best-effort)
      - email: email address
      - phone: phone number
      - skills: list of skill strings
      - experience: list of {title, company, dates, description}
      - projects: list of {title, dates, description}
      - education: list of {degree, institution, year}
      - summary: optional summary/objective section
    """

    # Common skill keywords to look for (extend this list)
    SKILL_KEYWORDS = [
        "python", "javascript", "typescript", "java", "c++", "c#", "go", "rust",
        "react", "vue", "angular", "node.js", "django", "fastapi", "flask",
        "sql", "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
        "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
        "machine learning", "deep learning", "tensorflow", "pytorch",
        "git", "ci/cd", "rest api", "graphql", "linux",
    ]

    def parse(self, path: str) -> dict:
        path = Path(path)
        suffix = path.suffix.lower()

        if suffix == ".pdf":
            text = self._read_pdf(path)
        elif suffix in (".docx", ".doc"):
            text = self._read_docx(path)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        return {
            "raw_text": text,
            "name": self._extract_name(text),
            "email": self._extract_email(text),
            "phone": self._extract_phone(text),
            "skills": self._extract_skills(text),
            "experience": self._extract_experience(text),
            "projects": self._extract_projects(text),
            "education": self._extract_education(text),
            "summary": self._extract_summary(text),
        }

    # ------------------------------------------------------------------
    # File readers
    # ------------------------------------------------------------------

    def _read_pdf(self, path: Path) -> str:
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(path))
            return "\n".join(page.get_text() for page in doc)
        except ImportError:
            raise ImportError("Install PyMuPDF: pip install pymupdf")

    def _read_docx(self, path: Path) -> str:
        try:
            from docx import Document
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            raise ImportError("Install python-docx: pip install python-docx")

    # ------------------------------------------------------------------
    # Extractors
    # ------------------------------------------------------------------

    def _extract_email(self, text: str) -> str:
        match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text)
        return match.group(0) if match else ""

    def _extract_phone(self, text: str) -> str:
        match = re.search(r"(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})", text)
        return match.group(0).strip() if match else ""

    def _extract_name(self, text: str) -> str:
        # Heuristic: first non-empty line is usually the name
        for line in text.splitlines():
            line = line.strip()
            if line and len(line.split()) in (2, 3) and line[0].isupper():
                return line
        return ""

    def _extract_skills(self, text: str) -> list[str]:
        text_lower = text.lower()
        found = []

        # Match from known keyword list
        for skill in self.SKILL_KEYWORDS:
            if skill in text_lower:
                found.append(skill)

        # Also try to extract a "Skills" section explicitly
        skills_match = re.search(
            r"skills[:\s]+(.*?)(?:\n\n|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if skills_match:
            raw = skills_match.group(1)
            extras = [s.strip() for s in re.split(r"[,|•·\n]", raw) if s.strip()]
            for e in extras:
                if e.lower() not in [f.lower() for f in found] and len(e) < 40:
                    found.append(e)

        return list(dict.fromkeys(found))  # preserve order, dedupe

    def _extract_experience(self, text: str) -> list[dict]:
        """
        Basic heuristic: look for sections like "Experience", "Work History".
        Returns a list of role dicts. For production, consider using an LLM
        extraction call here for better accuracy.
        """
        section_match = re.search(
            r"(?:experience|work history|employment)[:\s]*(.*?)(?=education|skills|projects|certifications|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            return []

        section = section_match.group(1)
        blocks = re.split(r"\n{2,}", section.strip())
        roles = []

        for block in blocks[:10]:  # cap at 10 roles
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue
            roles.append({
                "title": lines[0] if lines else "",
                "company": lines[1] if len(lines) > 1 else "",
                "dates": lines[2] if len(lines) > 2 else "",
                "description": "\n".join(lines[3:]).strip(),
            })

        return roles

    def _extract_projects(self, text: str) -> list[dict]:
        """
        Heuristic: ``Projects`` / ``Personal projects`` section, split into entries like experience.
        """
        section_match = re.search(
            r"(?:projects|personal projects|selected projects)[:\s]*(.*?)"
            r"(?=(?:^|\n)\s*(?:education|experience|work history|employment)\b"
            r"|(?:^|\n)\s*programming skills\b"
            r"|(?:^|\n)\s*skills\s*[:\s]"
            r"|(?:^|\n)\s*(?:certifications|summary|objective|references)\b"
            r"|(?:^|\n)\s*research and publications\b"
            r"|\Z)",
            text,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if not section_match:
            return []

        section = section_match.group(1)
        blocks = re.split(r"\n{2,}", section.strip())
        out: list[dict] = []

        for block in blocks[:15]:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue
            out.append({
                "title": lines[0] if lines else "",
                "dates": lines[1] if len(lines) > 1 else "",
                "description": "\n".join(lines[2:]).strip(),
            })

        return out

    def _extract_education(self, text: str) -> list[dict]:
        section_match = re.search(
            r"education[:\s]*(.*?)(?=experience|skills|projects|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            return []

        section = section_match.group(1)
        blocks = re.split(r"\n{2,}", section.strip())
        edu = []

        for block in blocks[:5]:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if lines:
                edu.append({
                    "degree": lines[0],
                    "institution": lines[1] if len(lines) > 1 else "",
                    "year": lines[2] if len(lines) > 2 else "",
                })

        return edu

    def _extract_summary(self, text: str) -> str:
        match = re.search(
            r"(?:summary|objective|profile)[:\s]*(.*?)(?=\n\n|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()[:500]
        return ""
