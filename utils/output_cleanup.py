"""
Prune stale files under ``output/`` (cover letters by default) to keep S3 sync fast.

Age is based on the file's local modification time (``Path.stat().st_mtime``), or for S3
objects the object's ``LastModified`` timestamp.

When S3 downloads cover letters, :func:`utils.s3_outputs.sync_download_output` sets local
mtime from S3 ``LastModified`` so age- and count-based pruning stay meaningful across machines.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .output_paths import COVERLETTERS_DIR, OUTPUT_DIR

log = logging.getLogger(__name__)

DEFAULT_COVER_LETTER_MAX_AGE_DAYS = 7.0
DEFAULT_COVER_LETTER_MAX_COUNT = 100


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


def cover_letter_max_count() -> int | None:
    """
    Max number of ``.docx`` cover letters to retain from env ``COVER_LETTER_MAX_COUNT`` (default 100).

    Returns ``None`` when count-based pruning is disabled (0 or negative).
    """
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


def prune_local_cover_letters_by_count(
    *,
    max_count: int | None = None,
    cover_dir: Path | str = COVERLETTERS_DIR,
    dry_run: bool = False,
) -> int:
    """
    When more than ``max_count`` ``.docx`` files exist under ``output/coverletters/``, delete the
    oldest by local modification time until at most ``max_count`` remain.
    """
    limit = max_count if max_count is not None else cover_letter_max_count()
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


def prune_s3_cover_letters_by_count(
    *,
    max_count: int | None = None,
    dry_run: bool = False,
) -> int:
    """
    When more than ``max_count`` cover-letter objects exist in S3, delete the oldest by
    ``LastModified`` until at most ``max_count`` remain.
    """
    from .s3_outputs import s3_list_prefix_for_dir, s3_output_bucket, s3_output_sync_enabled

    limit = max_count if max_count is not None else cover_letter_max_count()
    if limit is None or not s3_output_sync_enabled():
        return 0

    bucket = s3_output_bucket()
    list_prefix = s3_list_prefix_for_dir(OUTPUT_DIR.resolve())
    cover_prefix = f"{list_prefix}coverletters/"

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


def prune_cover_letters_for_sync(*, dry_run: bool = False) -> tuple[int, int]:
    """
    Local + S3 prune when configured. Call before upload and after download.

    Applies age-based pruning first, then count-based pruning on what remains.

    Returns ``(local_removed, s3_removed)`` (combined totals from both passes).
    """
    local = prune_local_cover_letters(dry_run=dry_run)
    local += prune_local_cover_letters_by_count(dry_run=dry_run)
    remote = prune_s3_cover_letters(dry_run=dry_run)
    remote += prune_s3_cover_letters_by_count(dry_run=dry_run)
    return local, remote
