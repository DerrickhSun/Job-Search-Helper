"""
LinkedIn Easy Apply label → answer rules loaded from ``data/form_fill_rules.json``.

Edit that file to add or change matching behavior without changing Python code.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "data" / "form_fill_rules.json"


class FormFillRulesEngine:
    """Loads JSON rules and resolves answers for text inputs, textareas, selects, and Yes/No screening."""

    def __init__(self, rules_path: Path | str | None = None) -> None:
        self._path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            raise FileNotFoundError(
                f"Form fill rules not found: {self._path} — add data/form_fill_rules.json or pass rules_path."
            )
        with self._path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("form_fill_rules.json must be a JSON object")
        log.info("Loaded form fill rules: %s", self._path.resolve())
        return data

    @staticmethod
    def normalize_label(label: str) -> str:
        return re.sub(r"\s+", " ", (label or "").lower()).strip()

    def _matches(self, normalized_label: str, spec: dict[str, Any]) -> bool:
        if not spec:
            return False
        n = normalized_label
        if spec.get("all_substrings"):
            for s in spec["all_substrings"]:
                if s not in n:
                    return False
        if spec.get("any_substrings"):
            if not any(s in n for s in spec["any_substrings"]):
                return False
        if spec.get("all_of_any"):
            for group in spec["all_of_any"]:
                if not group:
                    return False
                if not any(s in n for s in group):
                    return False
        if spec.get("not_substrings"):
            if any(s in n for s in spec["not_substrings"]):
                return False
        if spec.get("starts_with") is not None:
            sw = str(spec["starts_with"]).lower()
            if not n.startswith(sw):
                return False
        if spec.get("regex"):
            flags = re.I if spec.get("regex_ignore_case", True) else 0
            try:
                if not re.search(spec["regex"], n, flags):
                    return False
            except re.error as e:
                log.warning("Invalid regex in form_fill_rules: %s (%s)", spec.get("regex"), e)
                return False
        return True

    def screening_yes_no(self, label: str) -> str | None:
        """Returns ``\"Yes\"``, ``\"No\"``, or ``None`` if no screening rule matches."""
        n = self.normalize_label(label)
        if not n:
            return None
        for rule in self._data.get("screening_yes_no", []):
            if self._matches(n, rule.get("match", {})):
                ans = rule.get("answer")
                if ans is not None:
                    return str(ans).strip()
        return None

    def _apply_literal(self, result: dict[str, Any]) -> str:
        return str(result.get("value", ""))

    def _apply_text_result(self, result: dict[str, Any], resume: dict[str, Any]) -> str | None:
        t = (result.get("type") or "").strip()
        if t == "literal":
            return self._apply_literal(result)
        if t == "name_part":
            part = (result.get("part") or "").lower()
            parts = (resume.get("name") or "").split()
            if part == "first":
                return parts[0] if parts else ""
            if part == "last":
                return parts[-1] if len(parts) > 1 else ""
            return None
        if t == "resume_key_list":
            for k in result.get("keys") or []:
                v = (resume.get(k) or "").strip()
                if v:
                    return v
            return None
        if t == "years_experience_total":
            return str(max(1, len(resume.get("experience", [])) * 2))
        log.warning("Unknown text_inputs result type: %s", t)
        return None

    def _apply_textarea_result(self, result: dict[str, Any], cover_letter: str) -> str | None:
        t = (result.get("type") or "").strip()
        if t == "cover_letter_full":
            return cover_letter
        if t == "cover_letter_truncated":
            n = int(result.get("max_length", 500))
            return cover_letter[:n] if cover_letter else ""
        if t == "literal":
            return self._apply_literal(result)
        log.warning("Unknown textareas result type: %s", t)
        return None

    def answer_text_field(self, label: str, resume: dict[str, Any]) -> str | None:
        s = self.screening_yes_no(label)
        if s is not None:
            return s
        n = self.normalize_label(label)
        if not n:
            return None
        for rule in self._data.get("text_inputs", []):
            if self._matches(n, rule.get("match", {})):
                return self._apply_text_result(rule.get("result", {}), resume)
        return None

    def answer_textarea(self, label: str, cover_letter: str) -> str | None:
        s = self.screening_yes_no(label)
        if s is not None:
            return s
        n = self.normalize_label(label)
        if not n:
            return None
        for rule in self._data.get("textareas", []):
            if self._matches(n, rule.get("match", {})):
                return self._apply_textarea_result(rule.get("result", {}), cover_letter)
        return None

    def answer_select(self, label: str) -> str | None:
        s = self.screening_yes_no(label)
        if s is not None:
            return s
        n = self.normalize_label(label)
        if not n:
            return None
        for rule in self._data.get("selects", []):
            if self._matches(n, rule.get("match", {})):
                r = rule.get("result", {})
                if (r.get("type") or "").strip() == "literal":
                    return self._apply_literal(r)
                log.warning("Unknown selects result: %s", r)
                return None
        return None
