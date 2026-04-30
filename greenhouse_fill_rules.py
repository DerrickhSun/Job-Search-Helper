"""
Greenhouse application form rules loaded from ``data/greenhouse_fill_rules.json``.

Edit that file to add checkbox groups and (later) other control types without changing Python code.
Matching reuses the same label ``match`` objects as LinkedIn ``data/form_fill_rules.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from form_fill_rules import FormFillRulesEngine, label_matches

log = logging.getLogger(__name__)

DEFAULT_GREENHOUSE_RULES_PATH = Path(__file__).resolve().parent / "data" / "greenhouse_fill_rules.json"


class GreenhouseFillRulesEngine:
    """Loads ``greenhouse_fill_rules.json`` and resolves answers for Greenhouse-specific widgets."""

    def __init__(self, rules_path: Path | str | None = None) -> None:
        self._path = Path(rules_path) if rules_path else DEFAULT_GREENHOUSE_RULES_PATH
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            log.warning(
                "Greenhouse fill rules not found at %s — skipping rule-driven Greenhouse fields.",
                self._path.resolve(),
            )
            return {}
        with self._path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("greenhouse_fill_rules.json must be a JSON object")
        log.info("Loaded Greenhouse fill rules: %s", self._path.resolve())
        return data

    def checkbox_group_choice(self, fieldset_legend_text: str) -> str | None:
        """
        Return the option label to select for a checkbox ``fieldset`` (e.g. ``Job Board``), or ``None``.

        First matching entry in ``checkbox_groups`` wins.
        """
        n = FormFillRulesEngine.normalize_label(fieldset_legend_text)
        if not n:
            return None
        for rule in self._data.get("checkbox_groups", []):
            if label_matches(n, rule.get("match", {})):
                raw = rule.get("choose_label") or rule.get("option_label") or ""
                ch = str(raw).strip()
                return ch or None
        return None

    def has_checkbox_rules(self) -> bool:
        return bool(self._data.get("checkbox_groups"))
