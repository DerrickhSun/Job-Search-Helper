"""
Bot behavior toggles (``data/behavior.json``) -- consulting/greenhouse/student-job/unpaid-job
switches read by main.py. Split out from main.py (rather than defined inline there) so other
entry points, like extension_server.py's ``/config`` endpoint, can load and save the same file
without importing main.py itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BEHAVIOR_PATH = Path("data/behavior.json")

BEHAVIOR_DEFAULTS: dict[str, Any] = {
    "skip_consulting": True,
    "consulting_companies_memory": True,
    "greenhouse_manual_next_listing": True,
    "greenhouse_prefetch": True,
    "greenhouse_prompt_before_close": True,
    "greenhouse_date_posted": None,
    "greenhouse_gate_probe_max_listings": 0,
    "student_job_mode": "both",
    "unpaid_job_mode": "include",
}


def load_behavior_config(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_BEHAVIOR_PATH
    if p.is_file():
        try:
            overrides = json.loads(p.read_text(encoding="utf-8"))
            return {**BEHAVIOR_DEFAULTS, **{k: v for k, v in overrides.items() if k in BEHAVIOR_DEFAULTS}}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not read %s: %s -- using built-in defaults", p, e)
    return dict(BEHAVIOR_DEFAULTS)


def save_behavior_config(config: dict[str, Any], path: Path | str | None = None) -> None:
    p = Path(path) if path else DEFAULT_BEHAVIOR_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
