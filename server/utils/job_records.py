"""Append parsed job listings to a JSON Lines file for auditing."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_LISTINGS_LOG = Path("data/listings_log.jsonl")

# OneDrive / Windows often raises these while a sync placeholder is mid-hydration.
_RETRYABLE_ERRNOS = {5, 13, 22}  # EIO, EACCES, EINVAL


def listings_log_sidecar_paths(path: Path | str | None = None) -> list[Path]:
    """
    Rotated / overflow listings logs next to the main JSONL (never commit these — can be huge).

    Matches ``listings_log_*.jsonl``, ``*.bak``, and ``listings_log_overflow.jsonl``.
    """
    p = Path(path) if path else DEFAULT_LISTINGS_LOG
    parent = p.parent
    if not parent.is_dir():
        return []
    stem = p.stem  # listings_log
    suffix = p.suffix  # .jsonl
    found: list[Path] = []
    for child in parent.iterdir():
        if not child.is_file():
            continue
        name = child.name
        if name == p.name:
            continue
        if name == f"{stem}_overflow{suffix}":
            found.append(child)
            continue
        if name.startswith(stem + "_") and (
            name.endswith(suffix) or name.endswith(suffix + ".bak") or name.endswith(".bak")
        ):
            found.append(child)
            continue
        if name.startswith(stem) and name.endswith(".bak"):
            found.append(child)
    return sorted(found)


def warn_if_listings_log_sidecars(path: Path | str | None = None) -> list[Path]:
    """Log a warning when rotated/overflow listings logs are present; return those paths."""
    sidecars = listings_log_sidecar_paths(path)
    if not sidecars:
        return []
    parts = []
    for s in sidecars:
        try:
            mb = s.stat().st_size / (1024 * 1024)
            parts.append(f"{s.name} ({mb:.1f} MiB)")
        except OSError:
            parts.append(s.name)
    log.warning(
        "Listings log sidecar(s) present under %s — do not commit these (GitHub 100 MiB limit): %s. "
        "They are deleted after a successful S3 output upload, or you can delete them manually.",
        (Path(path) if path else DEFAULT_LISTINGS_LOG).parent,
        "; ".join(parts),
    )
    return sidecars


def cleanup_listings_log_sidecars(path: Path | str | None = None) -> int:
    """Delete rotated/overflow listings logs. Returns how many files were removed."""
    removed = 0
    for s in listings_log_sidecar_paths(path):
        try:
            s.unlink(missing_ok=True)
            log.info("Deleted listings log sidecar %s", s)
            removed += 1
        except OSError as e:
            log.warning("Could not delete listings log sidecar %s: %s", s, e)
    return removed


def append_listing_record(
    path: Path | str,
    job: dict[str, Any],
    *,
    phase: str = "parsed",
    extra: dict[str, Any] | None = None,
    retries: int = 5,
) -> bool:
    """
    Append one JSON object per line: timestamp, phase, job payload, optional extra fields.

    Returns True on success. Retries transient Windows/OneDrive open failures (``Errno 22``
    Invalid argument on cloud ``ReparsePoint`` files is common). On persistent failure, tries an
    overflow sidecar next to the main log so the apply pipeline is not aborted.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Listings log: could not create parent dir %s: %s", p.parent, e)
        return False

    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "job": job,
    }
    if extra:
        row.update(extra)
    line = json.dumps(row, ensure_ascii=False) + "\n"

    last_err: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
            return True
        except OSError as e:
            last_err = e
            errno = getattr(e, "errno", None)
            if attempt + 1 < retries and errno in _RETRYABLE_ERRNOS:
                time.sleep(0.25 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = e
            break

    overflow = p.with_name(p.stem + "_overflow" + p.suffix)
    try:
        with overflow.open("a", encoding="utf-8") as f:
            f.write(line)
        log.warning(
            "Listings log: could not append to %s (%s); wrote to %s instead. "
            "Large OneDrive-synced logs often cause Errno 22 — keep the file local or rotate it.",
            p,
            last_err,
            overflow,
        )
        return True
    except Exception as e2:
        log.warning(
            "Listings log: append failed for %s (%s) and overflow %s (%s) — continuing without log row.",
            p,
            last_err,
            overflow,
            e2,
        )
        return False
