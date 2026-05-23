"""
Form fill rules loaded from ``data/form_fill_rules.json``.

Used for LinkedIn Easy Apply (text inputs, textareas, selects, screening yes/no) and for Greenhouse
application pages (``checkbox_groups``: fieldset legend → option label to select). Pass ``apply_source``
(``\"linkedin\"`` vs ``\"greenhouse\"``) when constructing the engine so ``choose_label_from_apply_source``
can pick the right **How did you hear** option. ``text_inputs`` may use ``literal_fallbacks`` (``values[]``)
or ``literal_from_apply_source`` (same ``when`` map as checkbox_groups). Edit the JSON to change behavior
without changing Python code.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "form_fill_rules.json"


def label_matches(normalized_label: str, spec: dict[str, Any]) -> bool:
    """
    Return whether a normalized form label/question string satisfies ``spec``.

    Used by :class:`FormFillRulesEngine` for LinkedIn and Greenhouse rule matching.
    """
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
            log.warning("Invalid regex in form rules: %s (%s)", spec.get("regex"), e)
            return False
    return True


class FormFillRulesEngine:
    """Loads JSON rules and resolves answers for text inputs, textareas, selects, and Yes/No screening."""

    def __init__(self, rules_path: Path | str | None = None, *, apply_source: str | None = None) -> None:
        self._path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
        self._apply_source = (apply_source or "").strip().lower() or None
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
        return label_matches(normalized_label, spec)

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
        if t == "literal_fallbacks":
            for v in result.get("values") or []:
                s = str(v).strip()
                if s:
                    return s
            return None
        if t == "literal_from_apply_source":
            m = result.get("when") or result.get("by_apply_source") or {}
            if not isinstance(m, dict) or not m:
                return None
            src = self._apply_source or "linkedin"
            if src not in ("greenhouse", "linkedin"):
                src = "linkedin"
            raw = m.get(src)
            if raw is None or not str(raw).strip():
                raw = m.get("default") or m.get("linkedin")
            if raw is None:
                return None
            return str(raw).strip()
        log.warning("Unknown text_inputs result type: %s", t)
        return None

    def _text_result_candidates(self, result: dict[str, Any], resume: dict[str, Any]) -> list[str]:
        """All values to try for a ``text_inputs`` rule, in order (used for ``literal_fallbacks``)."""
        t = (result.get("type") or "").strip()
        if t == "literal_fallbacks":
            out: list[str] = []
            for v in result.get("values") or []:
                s = str(v).strip()
                if s:
                    out.append(s)
            return out
        one = self._apply_text_result(result, resume)
        if one is None:
            return []
        s = str(one).strip()
        return [s] if s else []

    def _first_matching_text_input_rule(self, label: str) -> dict[str, Any] | None:
        n = self.normalize_label(label)
        if not n:
            return None
        for rule in self._data.get("text_inputs", []):
            if self._matches(n, rule.get("match", {})):
                return rule
        return None

    def text_input_press_enter_after_fill(self, label: str) -> bool:
        """True when the first matching ``text_inputs`` rule sets ``press_enter_after_fill`` (autocomplete commit)."""
        rule = self._first_matching_text_input_rule(label)
        if not rule:
            return False
        result = rule.get("result") or {}
        return bool(result.get("press_enter_after_fill"))

    def text_input_fill_candidates(self, label: str, resume: dict[str, Any]) -> list[str]:
        """
        Ordered strings to type for this label. Screening yes/no resolves to a single candidate; otherwise
        the first matching ``text_inputs`` rule supplies one or more values (``literal_fallbacks``).
        """
        s = self.screening_yes_no(label)
        if s is not None:
            return [s]
        rule = self._first_matching_text_input_rule(label)
        if rule:
            return self._text_result_candidates(rule.get("result", {}), resume)
        return []

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
        c = self.text_input_fill_candidates(label, resume)
        return c[0] if c else None

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

    def has_checkbox_groups(self) -> bool:
        """True when the JSON defines at least one ``checkbox_groups`` entry (Greenhouse fieldsets)."""
        return bool(self._data.get("checkbox_groups"))

    def checkbox_group_choice(self, fieldset_legend_text: str) -> str | None:
        """
        Greenhouse ``fieldset.checkbox``: first matching ``checkbox_groups`` rule wins.

        ``match`` is evaluated on the fieldset ``legend`` text (normalized like other rules).
        Returns ``choose_label`` / ``option_label``, or a label from ``choose_label_from_apply_source``
        (keys ``greenhouse`` | ``linkedin``, plus optional ``default``) when the engine was constructed
        with ``apply_source=…`` (``linkedin`` is the default when unset).
        """
        n = self.normalize_label(fieldset_legend_text)
        if not n:
            return None
        for rule in self._data.get("checkbox_groups", []):
            if label_matches(n, rule.get("match", {})):
                src_map = rule.get("choose_label_from_apply_source")
                if isinstance(src_map, dict) and src_map:
                    src = self._apply_source or "linkedin"
                    if src not in ("greenhouse", "linkedin"):
                        src = "linkedin"
                    raw = src_map.get(src)
                    if raw is None or not str(raw).strip():
                        raw = src_map.get("default") or src_map.get("linkedin")
                    ch = str(raw or "").strip()
                    return ch or None
                raw = rule.get("choose_label") or rule.get("option_label") or ""
                ch = str(raw).strip()
                return ch or None
        return None
