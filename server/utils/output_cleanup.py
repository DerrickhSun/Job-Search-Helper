"""
Prune stale files under ``output/`` (cover letters by default) to keep S3 sync fast.

Age is based on the file's local modification time (``Path.stat().st_mtime``), or for S3
objects the object's ``LastModified`` timestamp.

When S3 downloads cover letters, the log-based sync in ``utils/s3_log_sync.py`` sets local mtime
from S3 ``LastModified`` so age- and count-based pruning stay meaningful across machines.

Local pruning (:func:`prune_local_cover_letters`, :func:`prune_local_cover_letters_by_count`)
records each file it removes locally into an optional ``tracker`` (a
:class:`utils.s3_log_sync.PendingChangeTracker`) instead of deleting the matching S3 object
directly — the actual S3-side delete happens later, as a properly logged ``"delete"`` entry when
the caller flushes the tracker via ``sync_log_upload``. This propagates the deletion to every
other device (not just this one), which a direct, unlogged S3 delete never did: a file removed
here for a good reason (aged out, or no longer needed) would otherwise just keep sitting on every
other device that never learns it's gone. The separate ``prune_s3_cover_letters*`` functions below
remain useful as a backstop for objects that exist only in S3 and were never downloaded here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .output_paths import COVERLETTERS_DIR, OUTPUT_DIR, cover_letter_dir_for_mode, cover_letter_output_dirs

if TYPE_CHECKING:
    from .s3_log_sync import PendingChangeTracker

log = logging.getLogger(__name__)

DEFAULT_COVER_LETTER_MAX_AGE_DAYS = 7.0
DEFAULT_COVER_LETTER_MAX_COUNT = 100

_COVER_LETTER_LIMITS_FILE = Path("data/cover_letter_limits.json")


def _load_cover_letter_limits() -> dict[str, int]:
    """Load per-mode count limits from data/cover_letter_limits.json."""
    if not _COVER_LETTER_LIMITS_FILE.is_file():
        return {}
    try:
        raw = json.loads(_COVER_LETTER_LIMITS_FILE.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}
    except Exception as e:
        log.warning("Could not read %s: %s — using global default", _COVER_LETTER_LIMITS_FILE, e)
        return {}


def cover_letter_max_age_days() -> float | None:
    """
    Max age in days from env ``COVER_LETTER_MAX_AGE_DAYS`` (default 7).

    Returns ``None`` when pruning is disabled (0 or negative).
    """
    raw = (os.environ.get("COVER_LETTER_MAX_AGE_DAYS") or "7").strip()
    try:
        days = float(raw)
    except ValueError:
        days = DEFAULT_COVER_LETTER_MAX_AGE_DAYS
    if days <= 0:
        return None
    return days


def cover_letter_max_count(mode: str | None = None) -> int | None:
    """
    Max number of ``.docx`` cover letters to retain for the given mode.

    Reads per-mode limits from ``data/cover_letter_limits.json`` (keys: ``linkedin``,
    ``filter``, ``greenhouse``). Falls back to env ``COVER_LETTER_MAX_COUNT`` (default 100).
    Returns ``None`` when count-based pruning is disabled (0 or negative).
    """
    if mode:
        limits = _load_cover_letter_limits()
        if mode in limits:
            n = limits[mode]
            if n <= 0:
                return None
            return n
    raw = (os.environ.get("COVER_LETTER_MAX_COUNT") or "100").strip()
    try:
        n = int(float(raw))
    except ValueError:
        n = DEFAULT_COVER_LETTER_MAX_COUNT
    if n <= 0:
        return None
    return n


def _iter_local_cover_letter_paths(cover_dir: Path) -> list[Path]:
    if not cover_dir.is_dir():
        return []
    return sorted(p for p in cover_dir.rglob("*.docx") if p.is_file())


def file_is_older_than_days(path: Path, max_age_days: float, *, now: float | None = None) -> bool:
    """True if ``path`` exists and its mtime is older than ``max_age_days``."""
    if not path.is_file():
        return False
    if max_age_days <= 0:
        return False
    ref = now if now is not None else time.time()
    age_seconds = ref - path.stat().st_mtime
    return age_seconds > max_age_days * 86400.0


def _rel_to_coverletters_dir(path: Path) -> str | None:
    """``path``'s location relative to ``output/coverletters/`` (e.g. ``linkedin/x.docx``), for
    :class:`utils.s3_log_sync.PendingChangeTracker`. ``None`` if ``path`` isn't under there."""
    try:
        return path.resolve().relative_to(COVERLETTERS_DIR.resolve()).as_posix()
    except ValueError:
        return None


def prune_local_cover_letters(
    *,
    max_age_days: float | None = None,
    cover_dir: Path | str = COVERLETTERS_DIR,
    dry_run: bool = False,
    tracker: "PendingChangeTracker | None" = None,
) -> int:
    """
    Delete ``.docx`` files under ``output/coverletters/`` older than ``max_age_days``.

    Returns the number of files removed (or that would be removed when ``dry_run``).
    """
    days = max_age_days if max_age_days is not None else cover_letter_max_age_days()
    if days is None:
        return 0

    root = Path(cover_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    if not root.is_dir():
        return 0

    removed = 0
    now = time.time()
    for path in sorted(root.rglob("*.docx")):
        if not path.is_file():
            continue
        if not file_is_older_than_days(path, days, now=now):
            continue
        removed += 1
        if dry_run:
            log.info("DRY-RUN  would delete old cover letter: %s", path)
            continue
        try:
            path.unlink()
            log.debug("Deleted old cover letter: %s", path)
        except OSError as e:
            log.warning("Could not delete %s: %s", path, e)
        else:
            if tracker is not None:
                rel = _rel_to_coverletters_dir(path)
                if rel is not None:
                    tracker.record_delete(rel)

    if removed and not dry_run:
        log.info(
            "Pruned %d cover letter(s) older than %.0f day(s) from %s",
            removed,
            days,
            root,
        )
    elif removed and dry_run:
        log.info(
            "DRY-RUN: %d cover letter(s) older than %.0f day(s) under %s",
            removed,
            days,
            root,
        )
    return removed


def prune_local_cover_letters_by_count(
    *,
    max_count: int | None = None,
    cover_dir: Path | str = COVERLETTERS_DIR,
    mode: str | None = None,
    dry_run: bool = False,
    tracker: "PendingChangeTracker | None" = None,
) -> int:
    """
    When more than ``max_count`` ``.docx`` files exist under ``output/coverletters/``, delete the
    oldest by local modification time until at most ``max_count`` remain.

    ``mode`` (e.g. ``linkedin``, ``filter``, ``greenhouse``) selects the per-mode limit from
    ``data/cover_letter_limits.json``; falls back to the global default when omitted.
    """
    limit = max_count if max_count is not None else cover_letter_max_count(mode)
    if limit is None:
        return 0

    root = Path(cover_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()

    paths = _iter_local_cover_letter_paths(root)
    excess = len(paths) - limit
    if excess <= 0:
        return 0

    paths.sort(key=lambda p: p.stat().st_mtime)
    removed = 0
    for path in paths[:excess]:
        removed += 1
        if dry_run:
            log.info("DRY-RUN  would delete excess cover letter (count cap): %s", path)
            continue
        try:
            path.unlink()
            log.debug("Deleted excess cover letter (count cap): %s", path)
        except OSError as e:
            log.warning("Could not delete %s: %s", path, e)
        else:
            if tracker is not None:
                rel = _rel_to_coverletters_dir(path)
                if rel is not None:
                    tracker.record_delete(rel)

    if removed and not dry_run:
        log.info(
            "Pruned %d excess cover letter(s) (kept newest %d) from %s",
            removed,
            limit,
            root,
        )
    elif removed and dry_run:
        log.info(
            "DRY-RUN: would prune %d excess cover letter(s) (keep newest %d) under %s",
            removed,
            limit,
            root,
        )
    return removed


def prune_s3_cover_letters(
    *,
    max_age_days: float | None = None,
    cover_subdir: str = "coverletters",
    cover_mode: str | None = None,
    dry_run: bool = False,
) -> int:
    """
    Delete S3 objects under ``{prefix}output/coverletters/{mode}/`` older than ``max_age_days``.

    ``cover_mode`` is the subfolder name (``linkedin``, ``filter``, ``greenhouse``). When omitted,
    uses legacy flat ``output/coverletters/`` prefix via ``cover_subdir`` only.
    """
    from .s3_outputs import s3_list_prefix_for_dir, s3_output_bucket, s3_output_sync_enabled

    days = max_age_days if max_age_days is not None else cover_letter_max_age_days()
    if days is None or not s3_output_sync_enabled():
        return 0

    bucket = s3_output_bucket()
    list_prefix = s3_list_prefix_for_dir(OUTPUT_DIR.resolve())
    if cover_mode:
        cover_prefix = f"{list_prefix}{COVERLETTERS_DIR.name}/{cover_mode.strip('/')}/"
    else:
        cover_prefix = f"{list_prefix}{cover_subdir.strip('/')}/"

    try:
        from botocore.exceptions import BotoCoreError, ClientError

        from .s3_outputs import _s3_client
    except ImportError:
        log.warning("S3 cover-letter prune skipped: install boto3")
        return 0

    cutoff = time.time() - days * 86400.0
    deleted = 0
    s3 = _s3_client()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=cover_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                if not key or key.endswith("/"):
                    continue
                if not key.lower().endswith(".docx"):
                    continue
                last_mod = obj.get("LastModified")
                if last_mod is None:
                    continue
                if last_mod.timestamp() > cutoff:
                    continue
                deleted += 1
                if dry_run:
                    log.info("DRY-RUN  would delete s3://%s/%s", bucket, key)
                    continue
                s3.delete_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 cover-letter prune failed (s3://%s/%s): %s", bucket, cover_prefix, e)
        return deleted

    if deleted and not dry_run:
        log.info(
            "S3: deleted %d cover letter object(s) older than %.0f day(s) under s3://%s/%s",
            deleted,
            days,
            bucket,
            cover_prefix,
        )
    return deleted


def prune_s3_cover_letters_by_count(
    *,
    max_count: int | None = None,
    cover_subdir: str = "coverletters",
    cover_mode: str | None = None,
    dry_run: bool = False,
) -> int:
    """
    When more than ``max_count`` cover-letter objects exist in S3 under a mode folder, delete the
    oldest by ``LastModified`` until at most ``max_count`` remain.
    """
    from .s3_outputs import s3_list_prefix_for_dir, s3_output_bucket, s3_output_sync_enabled

    limit = max_count if max_count is not None else cover_letter_max_count(cover_mode)
    if limit is None or not s3_output_sync_enabled():
        return 0

    bucket = s3_output_bucket()
    list_prefix = s3_list_prefix_for_dir(OUTPUT_DIR.resolve())
    if cover_mode:
        cover_prefix = f"{list_prefix}{COVERLETTERS_DIR.name}/{cover_mode.strip('/')}/"
    else:
        cover_prefix = f"{list_prefix}{cover_subdir.strip('/')}/"

    try:
        from botocore.exceptions import BotoCoreError, ClientError

        from .s3_outputs import _s3_client
    except ImportError:
        log.warning("S3 cover-letter count prune skipped: install boto3")
        return 0

    objects: list[tuple[float, str]] = []
    s3 = _s3_client()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=cover_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                if not key or key.endswith("/"):
                    continue
                if not key.lower().endswith(".docx"):
                    continue
                last_mod = obj.get("LastModified")
                if last_mod is None:
                    continue
                objects.append((last_mod.timestamp(), key))
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 cover-letter count prune failed (s3://%s/%s): %s", bucket, cover_prefix, e)
        return 0

    excess = len(objects) - limit
    if excess <= 0:
        return 0

    objects.sort(key=lambda t: t[0])
    deleted = 0
    for _ts, key in objects[:excess]:
        deleted += 1
        if dry_run:
            log.info("DRY-RUN  would delete excess s3://%s/%s", bucket, key)
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError, OSError) as e:
            log.warning("Could not delete s3://%s/%s: %s", bucket, key, e)

    if deleted and not dry_run:
        log.info(
            "S3: deleted %d excess cover letter object(s) (kept newest %d) under s3://%s/%s",
            deleted,
            limit,
            bucket,
            cover_prefix,
        )
    return deleted


def prune_cover_letters_for_sync(
    *,
    cover_subdirs: tuple[str, ...] | None = None,
    cover_letter_modes: tuple[str, ...] | None = None,
    dry_run: bool = False,
    tracker: "PendingChangeTracker | None" = None,
) -> tuple[int, int]:
    """
    Local + S3 prune when configured.

    ``cover_letter_modes`` / ``cover_subdirs``: mode names under ``output/coverletters/``
    (``linkedin``, ``filter``, ``greenhouse``). ``None`` prunes all three.

    ``tracker``, if given, records every locally-pruned file as a pending delete (flushed to S3 as
    a logged ``"delete"`` entry next time the caller flushes it via ``sync_log_upload`` — see
    module docstring); the separate ``prune_s3_cover_letters*`` calls below are independent of
    ``tracker`` and keep running regardless, as a backstop for S3-only stragglers.
    """
    modes = cover_letter_modes if cover_letter_modes is not None else cover_subdirs

    if modes is None:
        mode_list = tuple(d.name for d in cover_letter_output_dirs())
    else:
        mode_list = modes

    local = 0
    remote = 0
    for mode in mode_list:
        cover_dir = cover_letter_dir_for_mode(mode)
        if cover_dir is None:
            log.warning("Unknown cover letter mode for prune: %r", mode)
            continue
        local += prune_local_cover_letters(cover_dir=cover_dir, dry_run=dry_run, tracker=tracker)
        local += prune_local_cover_letters_by_count(
            cover_dir=cover_dir, mode=mode, dry_run=dry_run, tracker=tracker
        )
        remote += prune_s3_cover_letters(cover_mode=mode, dry_run=dry_run)
        remote += prune_s3_cover_letters_by_count(cover_mode=mode, dry_run=dry_run)
    return local, remote
