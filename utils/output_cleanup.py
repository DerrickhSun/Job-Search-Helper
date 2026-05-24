"""
Prune stale files under ``output/`` (cover letters by default) to keep S3 sync fast.

Age is based on the file's local modification time (``Path.stat().st_mtime``), or for S3
objects the object's ``LastModified`` timestamp.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .output_paths import COVERLETTERS_DIR, OUTPUT_DIR

log = logging.getLogger(__name__)

DEFAULT_COVER_LETTER_MAX_AGE_DAYS = 7.0


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


def file_is_older_than_days(path: Path, max_age_days: float, *, now: float | None = None) -> bool:
    """True if ``path`` exists and its mtime is older than ``max_age_days``."""
    if not path.is_file():
        return False
    if max_age_days <= 0:
        return False
    ref = now if now is not None else time.time()
    age_seconds = ref - path.stat().st_mtime
    return age_seconds > max_age_days * 86400.0


def prune_local_cover_letters(
    *,
    max_age_days: float | None = None,
    cover_dir: Path | str = COVERLETTERS_DIR,
    dry_run: bool = False,
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


def prune_s3_cover_letters(
    *,
    max_age_days: float | None = None,
    dry_run: bool = False,
) -> int:
    """
    Delete S3 objects under ``{prefix}output/coverletters/`` older than ``max_age_days``.

    Requires ``S3_OUTPUT_BUCKET``. Returns count deleted (or would delete).
    """
    from .s3_outputs import s3_list_prefix_for_dir, s3_output_bucket, s3_output_sync_enabled

    days = max_age_days if max_age_days is not None else cover_letter_max_age_days()
    if days is None or not s3_output_sync_enabled():
        return 0

    bucket = s3_output_bucket()
    list_prefix = s3_list_prefix_for_dir(OUTPUT_DIR.resolve())
    cover_prefix = f"{list_prefix}coverletters/"

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


def prune_cover_letters_for_sync(*, dry_run: bool = False) -> tuple[int, int]:
    """
    Local + S3 prune when configured. Call before upload and after download.

    Returns ``(local_removed, s3_removed)``.
    """
    local = prune_local_cover_letters(dry_run=dry_run)
    remote = prune_s3_cover_letters(dry_run=dry_run)
    return local, remote
