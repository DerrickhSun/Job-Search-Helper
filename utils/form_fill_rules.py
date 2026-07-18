"""
Form fill rules loaded from ``output/form_fill_rules/`` (S3-synced with ``output/``), seeded from
``defaults/form_fill_rules/`` on first run. Legacy paths ``data/form_fill_rules/`` and
``data/form_fill_rules.json`` are still supported for ``--form-fill-rules`` overrides.

Used for LinkedIn Easy Apply (text inputs, textareas, selects, screening yes/no, checkbox questions) and
for Greenhouse application pages (``checkbox_groups``: fieldset legend → option label to select). Pass
``apply_source`` (``\"linkedin\"`` vs ``\"greenhouse\"``) when constructing the engine so
``choose_label_from_apply_source`` can pick the right **How did you hear** option. ``text_inputs`` and
``selects`` may use ``literal_fallbacks`` (``values[]``) or ``literal_from_apply_source`` (same ``when``
map as checkbox_groups). Edit the JSON to change behavior without changing Python code.

Priority-ordered answers: ``screening_yes_no``'s ``answer`` and ``checkbox_groups``'s
``choose_label``/``option_label`` accept either a single string or a list of strings. A list is a
priority order — the field-filler tries the first value, and if the field doesn't offer that option
(e.g. a radio group with no "No" choice), tries the next, and so on, stopping at the first that matches
an actual option. A plain string is still a valid one-item list (fully backward compatible). See
:meth:`FormFillRulesEngine.screening_yes_no_candidates` and
:meth:`FormFillRulesEngine.checkbox_group_choice_candidates`.

Directory mode: every ``*.json`` file in the directory is loaded in **case-insensitive filename order**
and merged. List-valued keys (``screening_yes_no``, ``text_inputs``, ``textareas``, ``selects``,
``checkbox_groups``) are **concatenated** in that order; scalar keys (``schema_version``,
``documentation``) take the value from the first file that defines them. Because the FIRST matching
rule within a category wins, split a category across files using numeric filename prefixes (e.g.
``screening_10_*.json`` before ``screening_20_*.json``) to control precedence.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from .output_paths import FORM_FILL_RULES_DIR, LEGACY_FORM_FILL_RULES_DIR, LEGACY_FORM_FILL_RULES_FILE

DEFAULT_RULES_DIR = FORM_FILL_RULES_DIR
DEFAULT_RULES_FILE = LEGACY_FORM_FILL_RULES_FILE
DEFAULT_RULES_PATH = (
    DEFAULT_RULES_DIR
    if DEFAULT_RULES_DIR.is_dir() and any(DEFAULT_RULES_DIR.glob("*.json"))
    else LEGACY_FORM_FILL_RULES_DIR
    if LEGACY_FORM_FILL_RULES_DIR.is_dir()
    else DEFAULT_RULES_FILE
    if DEFAULT_RULES_FILE.is_file()
    else DEFAULT_RULES_DIR
)

# Special screening answer: close the apply flow and choose Discard (not Save) on the draft dialog.
DISCARD_APPLY = "__discard_apply__"

# List-valued top-level keys are concatenated across files; everything else is treated as a scalar.
_MERGEABLE_LIST_KEYS = (
    "screening_yes_no",
    "text_inputs",
    "textareas",
    "selects",
    "checkbox_groups",
)


def normalize_label_for_exact(label: str) -> str:
    """Normalized label for ``match.exact`` comparison (case/whitespace; trailing ``?`` / ``*``)."""
    return FormFillRulesEngine.normalize_label(label).rstrip("?").strip()


def label_matches(normalized_label: str, spec: dict[str, Any]) -> bool:
    """
    Return whether a normalized form label/question string satisfies ``spec``.

    Used by :class:`FormFillRulesEngine` for LinkedIn and Greenhouse rule matching.

    When ``exact`` is set, the label must equal that string after :func:`normalize_label_for_exact`
    (other match keys are ignored). Hand-written rules may still use substring/regex matchers.
    """
    if not spec:
        return False
    if spec.get("exact") is not None:
        want = normalize_label_for_exact(str(spec["exact"]))
        got = normalize_label_for_exact(normalized_label)
        return got == want
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
        if self._path.is_dir():
            return self._load_dir(self._path)
        if self._path.is_file():
            return self._load_file(self._path)
        raise FileNotFoundError(
            f"Form fill rules not found: {self._path} — add JSON files under output/form_fill_rules/ "
            "(synced via S3), or pass rules_path."
        )

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in form fill rules file {path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"Form fill rules file must be a JSON object: {path}")
        return data

    def _load_file(self, path: Path) -> dict[str, Any]:
        data = self._read_json_object(path)
        log.info("Loaded form fill rules: %s", path.resolve())
        return data

    def _load_dir(self, path: Path) -> dict[str, Any]:
        files = sorted(path.glob("*.json"), key=lambda p: p.name.lower())
        if not files:
            raise FileNotFoundError(
                f"No *.json rule files found in form fill rules directory: {path}"
            )
        merged: dict[str, Any] = {}
        for fp in files:
            part = self._read_json_object(fp)
            for key, val in part.items():
                if key in _MERGEABLE_LIST_KEYS:
                    if not isinstance(val, list):
                        raise ValueError(
                            f"Key {key!r} in {fp} must be a list (got {type(val).__name__})."
                        )
                    merged.setdefault(key, []).extend(val)
                else:
                    merged.setdefault(key, val)
        log.info(
            "Loaded form fill rules: merged %d file(s) from %s",
            len(files),
            path.resolve(),
        )
        return merged

    @staticmethod
    def normalize_label(label: str) -> str:
        n = re.sub(r"\s+", " ", (label or "").lower()).strip()
        # Trailing asterisks (and any space before them) are common "required field" markers — drop them
        # so "Question?*" / "Question *" match the same rules as "Question?".
        return re.sub(r"[\s*]+$", "", n)

    def _matches(self, normalized_label: str, spec: dict[str, Any]) -> bool:
        return label_matches(normalized_label, spec)

    @staticmethod
    def _coerce_screening_answer(raw: Any) -> str | None:
        if raw is None:
            return None
        s = str(raw).strip()
        if s == DISCARD_APPLY:
            return DISCARD_APPLY
        return s

    @classmethod
    def _coerce_answer_list(cls, raw: Any) -> list[str]:
        """
        Normalize an ``answer`` (or ``choose_label``) value into an ordered, deduplicated list of
        candidates. Accepts a single string (backward compatible) or a list of strings — the list is
        a **priority order**: the caller tries the first; if the field doesn't offer that option, it
        tries the next, and so on.
        """
        if raw is None:
            return []
        raw_list = raw if isinstance(raw, list) else [raw]
        out: list[str] = []
        seen: set[str] = set()
        for v in raw_list:
            s = cls._coerce_screening_answer(v)
            if s is None or s == "":
                continue
            key = s.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    def screening_yes_no_candidates(self, label: str) -> list[str]:
        """
        Ordered candidate answers (priority order) from the first matching ``screening_yes_no`` rule.
        Empty list means no rule matched (or a matched region rule deliberately leaves the field empty).

        A screening rule may use a fixed ``answer`` (string or list — see :meth:`_coerce_answer_list`) or,
        for label-dependent answers, ``answer_by_region`` (see :meth:`_resolve_region_answer`). A region
        rule that matches is **authoritative**: it stops the scan and may intentionally return no
        candidates (leave the field empty for the user to fill).
        """
        n = self.normalize_label(label)
        if not n:
            return []
        for rule in self._data.get("screening_yes_no", []):
            if not self._matches(n, rule.get("match", {})):
                continue
            region_cfg = rule.get("answer_by_region")
            if region_cfg:
                ans, decided = self._resolve_region_answer(n, region_cfg)
                if decided:
                    return [ans] if ans is not None else []
                continue
            cands = self._coerce_answer_list(rule.get("answer"))
            if cands:
                return cands
        return []

    def screening_yes_no(self, label: str) -> str | None:
        """Highest-priority candidate answer — see :meth:`screening_yes_no_candidates` for the full list."""
        c = self.screening_yes_no_candidates(label)
        return c[0] if c else None

    @staticmethod
    def _region_condition_matches(region_lower: str, cond: dict[str, Any]) -> bool:
        """
        True when ``region_lower`` satisfies a region condition. A condition matches if **any** listed term
        matches across these operators:
        - ``contains_any``: plain substring (e.g. ``\"computer science\"``).
        - ``contains_word_any``: whole-word via ``\\b`` (e.g. the state abbreviation ``\"ca\"`` without
          matching inside ``\"california\"``).
        - ``contains_token_any``: token bounded by anything **except** alphanumerics, ``+`` or ``#`` — so
          ``\"c\"`` matches ``c`` / ``c,`` but **not** ``c++`` or ``c#``, and ``\"sql\"`` matches standalone
          ``sql`` but not ``mysql``. Use for ambiguous short language names.
        """
        for sub in cond.get("contains_any") or []:
            s = str(sub).lower().strip()
            if s and s in region_lower:
                return True
        for word in cond.get("contains_word_any") or []:
            w = str(word).lower().strip()
            if w and re.search(rf"\b{re.escape(w)}\b", region_lower):
                return True
        for tok in cond.get("contains_token_any") or []:
            t = str(tok).lower().strip()
            if t and re.search(rf"(?<![a-z0-9+#]){re.escape(t)}(?![a-z0-9+#])", region_lower):
                return True
        return False

    def _resolve_region_answer(
        self, normalized_label: str, cfg: dict[str, Any]
    ) -> tuple[str | None, bool]:
        """
        Resolve a label-dependent Yes/No answer.

        ``cfg`` keys:
        - ``region_regex``: a regex run on the normalized label; group 1 (or the whole match) is the region.
        - ``conditions``: ordered list; the first whose terms appear in the region wins (see
          :meth:`_region_condition_matches`). Each has an ``answer`` (``\"Yes\"``/``\"No\"``/``null``).
        - ``default_answer``: used when no condition matches (``null`` ⇒ leave empty).

        Returns ``(answer, decided)``. ``decided`` is ``False`` only when ``region_regex`` does not capture a
        region (so the caller can fall through to other rules); otherwise the region rule is authoritative,
        and ``answer`` may be ``None`` to deliberately leave the field empty.
        """
        rx = cfg.get("region_regex")
        if not rx:
            return None, False
        try:
            m = re.search(rx, normalized_label, re.I)
        except re.error as e:
            log.warning("Invalid region_regex in form rules: %s (%s)", rx, e)
            return None, False
        if not m:
            return None, False
        region = (m.group(1) if m.groups() else m.group(0)) or ""
        region_lower = region.strip().lower()
        for cond in cfg.get("conditions") or []:
            if self._region_condition_matches(region_lower, cond):
                return self._coerce_screening_answer(cond.get("answer")), True
        if "default_answer" in cfg:
            return self._coerce_screening_answer(cfg.get("default_answer")), True
        return None, True

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

    def answer_select_candidates(self, label: str) -> list[str]:
        """
        Ordered candidate values (priority order) for a ``select``: try the first; if the dropdown
        doesn't offer that option, try the next. ``screening_yes_no`` candidates take priority (a
        Yes/No question rendered as a dropdown), then the first matching ``selects`` rule — ``literal``
        (single value) or ``literal_fallbacks`` (``values: [...]``, same convention as ``text_inputs``).
        """
        s = self.screening_yes_no_candidates(label)
        if s:
            return s
        n = self.normalize_label(label)
        if not n:
            return []
        for rule in self._data.get("selects", []):
            if self._matches(n, rule.get("match", {})):
                r = rule.get("result", {})
                t = (r.get("type") or "").strip()
                if t == "literal":
                    v = self._apply_literal(r)
                    return [v] if v else []
                if t == "literal_fallbacks":
                    return [str(v).strip() for v in (r.get("values") or []) if str(v).strip()]
                log.warning("Unknown selects result: %s", r)
                return []
        return []

    def answer_select(self, label: str) -> str | None:
        c = self.answer_select_candidates(label)
        return c[0] if c else None

    def has_checkbox_groups(self) -> bool:
        """True when the JSON defines at least one ``checkbox_groups`` entry (Greenhouse and LinkedIn fieldsets)."""
        return bool(self._data.get("checkbox_groups"))

    def checkbox_group_choice_candidates(self, fieldset_legend_text: str) -> list[str]:
        """
        Ordered candidate option labels (priority order) from the first matching ``checkbox_groups``
        rule — Greenhouse ``fieldset.checkbox`` or a LinkedIn checkbox fieldset with multiple options.

        ``match`` is evaluated on the fieldset ``legend`` text (normalized like other rules).
        ``choose_label`` / ``option_label`` may be a string or a list (priority order — see
        :meth:`_coerce_answer_list`), or a label from ``choose_label_from_apply_source`` (keys
        ``greenhouse`` | ``linkedin``, plus optional ``default``) when the engine was constructed with
        ``apply_source=…`` (``linkedin`` is the default when unset).
        """
        n = self.normalize_label(fieldset_legend_text)
        if not n:
            return []
        for rule in self._data.get("checkbox_groups", []):
            if label_matches(n, rule.get("match", {})):
                src_map = rule.get("choose_label_from_apply_source")
                if isinstance(src_map, dict) and src_map:
                    src = self._apply_source or "linkedin"
                    if src not in ("greenhouse", "linkedin"):
                        src = "linkedin"
                    raw = src_map.get(src)
                    if raw is None or (isinstance(raw, str) and not raw.strip()):
                        raw = src_map.get("default") or src_map.get("linkedin")
                    return self._coerce_answer_list(raw)
                raw = rule.get("choose_label") or rule.get("option_label")
                cands = self._coerce_answer_list(raw)
                if cands:
                    return cands
        return []

    def checkbox_group_choice(self, fieldset_legend_text: str) -> str | None:
        """Highest-priority option label — see :meth:`checkbox_group_choice_candidates` for the full list."""
        c = self.checkbox_group_choice_candidates(fieldset_legend_text)
        return c[0] if c else None
