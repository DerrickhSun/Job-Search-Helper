"""Append parsed job listings to a JSON Lines file for auditing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LISTINGS_LOG = Path("data/listings_log.jsonl")


def append_listing_record(
    path: Path | str,
    job: dict[str, Any],
    *,
    phase: str = "parsed",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON object per line: timestamp, phase, job payload, optional extra fields."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "job": job,
    }
    if extra:
        row.update(extra)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
