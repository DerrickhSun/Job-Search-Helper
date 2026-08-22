"""
Sync the local ``output/`` tree with S3 (see docs/s3_outputs.md).

``coverletters/`` and ``form_fill_rules/`` are handled entirely separately, by
``utils/s3_log_sync.py`` (an operation log each device replays, rather than a plain directory
diff) — this module never lists, downloads, or uploads anything under those two top-level dirs
(``_LOG_MANAGED_TOP_DIRS``). Everything else under ``output/`` (tracking CSVs,
``consulting_companies.json``, etc.) still syncs the plain way below.

Local-only trees (never uploaded or downloaded): ``output/screenshots/`` (error diagnostics).

Cross-device locking: when more than one device shares the same ``S3_OUTPUT_PREFIX``, a naive
download -> merge-in-memory -> upload sequence (used for the tracking CSVs) is a classic
read-modify-write race — two devices can each read a consistent snapshot, merge their own
changes into it, and upload, with the second upload silently discarding the first device's
additions (S3 has no built-in arbitration between two writes to the same key; whichever commits
last simply wins). :func:`acquire_sync_lock` guards against that for this module's scope; it's
also reused (with a different ``lock_name``) by ``utils/s3_log_sync.py`` for its own, much
shorter-held, per-resource locks. See :func:`sync_download_output_coordinated` and
:func:`sync_upload_output_coordinated`.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import platform
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .display_utils import print_s3_progress
from .output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    COVERLETTERS_DIR,
    FORM_FILL_RULES_DIR,
    OUTPUT_DIR,
)

if TYPE_CHECKING:  # avoids a circular import — s3_log_sync.py imports several names from here
    from .s3_log_sync import PendingChangeTracker

log = logging.getLogger(__name__)

# Top-level dirs under ``output/`` that stay machine-local (not synced to/from S3).
_SYNC_EXCLUDED_TOP_DIRS = frozenset({"screenshots"})

# Top-level dirs handled entirely by utils/s3_log_sync.py instead of this module's plain
# list-and-diff sync (see module docstring).
_LOG_MANAGED_TOP_DIRS = frozenset({COVERLETTERS_DIR.name, FORM_FILL_RULES_DIR.name})

# (memory_csv, archive_csv) pairs — memory is cleaned against its archive after each sync.
_MEMORY_ARCHIVE_PAIRS: tuple[tuple[Path, Path], ...] = (
    (APPLICATIONS_CSV, APPLICATIONS_ARCHIVE_CSV),
    (ASSISTED_APPLICATIONS_CSV, ASSISTED_APPLICATIONS_HISTORY_CSV),
)

# How long a lock can sit unreleased before another device treats it as abandoned (e.g. the
# holder crashed or was killed mid-sync) and reclaims it, rather than waiting on it forever.
_SYNC_LOCK_STALE_SECONDS = 300.0

# How long to wait for a lock actively held by another (non-stale) device before giving up and
# skipping this run's locked-scope sync rather than blocking indefinitely.
_SYNC_LOCK_WAIT_TIMEOUT_SECONDS = 60.0
_SYNC_LOCK_POLL_INTERVAL_SECONDS = 3.0


def _sync_progress(msg: str, *args) -> None:
    """
    User-visible sync progress.

    Uses the module logger (picked up by ``main.py``'s handlers). Scripts that never call
    ``logging.basicConfig`` still get stdout so a long sync does not look hung.
    """
    text = msg % args if args else msg
    log.info("%s", text)
    if not logging.root.handlers:
        print(text, flush=True)


def s3_output_bucket() -> str:
    return (os.environ.get("S3_OUTPUT_BUCKET") or "").strip()


def s3_output_prefix() -> str:
    prefix = (os.environ.get("S3_OUTPUT_PREFIX") or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def s3_output_sync_enabled() -> bool:
    return bool(s3_output_bucket())


def resolve_output_dir(local_dir: Path | str) -> Path:
    root = Path(local_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def s3_list_prefix_for_dir(local_dir: Path) -> str:
    """S3 key prefix for all objects under this local directory (includes trailing slash)."""
    return f"{s3_output_prefix()}{local_dir.name}/"


def s3_key_for_file(local_dir: Path, path: Path) -> str:
    rel = path.relative_to(local_dir).as_posix()
    return f"{s3_output_prefix()}{local_dir.name}/{rel}"


def _skip_local_only_rel(rel: str) -> bool:
    """True for paths under sync-excluded top-level dirs (e.g. ``screenshots/…``)."""
    parts = rel.replace("\\", "/").split("/")
    return bool(parts) and parts[0] in _SYNC_EXCLUDED_TOP_DIRS


def _skip_log_managed_rel(rel: str) -> bool:
    """True for paths under ``coverletters/``/``form_fill_rules/`` — synced by
    ``utils/s3_log_sync.py`` instead, never by this module's plain list-and-diff sync."""
    parts = rel.replace("\\", "/").split("/")
    return bool(parts) and parts[0] in _LOG_MANAGED_TOP_DIRS


def _sync_lock_key(lock_name: str) -> str:
    return f"{s3_output_prefix()}{lock_name}"


def _sync_lock_owner() -> str:
    return f"{platform.node() or 'unknown-host'}:{os.getpid()}"


def _try_create_sync_lock(s3, bucket: str, key: str, owner: str) -> str | None:
    """Atomically create the lock object (fails if it already exists). Returns its ETag on success."""
    from botocore.exceptions import ClientError

    body = json.dumps({"owner": owner, "acquired_at": time.time()}).encode("utf-8")
    try:
        resp = s3.put_object(
            Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ContentType="application/json"
        )
        return resp.get("ETag")
    except ClientError as e:
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = e.response.get("Error", {}).get("Code", "")
        if status == 412 or code in ("PreconditionFailed", "ConditionalRequestConflict"):
            return None
        raise


def acquire_sync_lock(
    *,
    lock_name: str = "sync.lock",
    wait_timeout_seconds: float = _SYNC_LOCK_WAIT_TIMEOUT_SECONDS,
    stale_after_seconds: float = _SYNC_LOCK_STALE_SECONDS,
) -> str | None:
    """
    Acquire a cross-device S3 lock object (``{prefix}{lock_name}``). ``lock_name`` defaults to the
    general ``sync.lock`` (the ``locked`` scope of a sync — everything except cover letters and
    form-fill rules, see module docstring); the coverletters/form_fill_rules log-sync protocol
    (``utils/s3_log_sync.py``) uses this same function with ``lock_name="coverletters.lock"`` /
    ``"form_fill_rules.lock"`` instead, held only briefly around a log-check-and-mutate step rather
    than a whole program run.

    Returns an opaque token (the lock object's ETag) to pass to :func:`release_sync_lock` (with the
    same ``lock_name``) once the caller's critical section has finished — for the ``sync.lock``
    case, that's the whole download -> merge -> upload sequence, not just around the download or
    upload individually, or two devices could each read a consistent pre-lock snapshot and still
    clobber each other on upload.

    Returns ``None`` (never raises) when the lock can't be acquired: S3 sync isn't configured,
    boto3 is missing, another device holds a live (non-stale) lock and ``wait_timeout_seconds``
    elapses, or any S3 error occurs. Callers should skip their locked-scope work for this run
    rather than proceed without the lock.
    """
    if not s3_output_sync_enabled():
        return None
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = _s3_client()
    except ImportError:
        log.warning("S3 sync lock skipped: install boto3")
        return None

    bucket = s3_output_bucket()
    key = _sync_lock_key(lock_name)
    owner = _sync_lock_owner()

    try:
        deadline = time.time() + wait_timeout_seconds
        while True:
            etag = _try_create_sync_lock(s3, bucket, key, owner)
            if etag is not None:
                log.debug("Acquired S3 sync lock s3://%s/%s (owner=%s)", bucket, key, owner)
                return etag

            try:
                head = s3.head_object(Bucket=bucket, Key=key)
            except (ClientError, BotoCoreError):
                # Raced with the holder releasing it between our failed create and this head —
                # loop around and try to create it again immediately.
                continue

            age = time.time() - head["LastModified"].timestamp()
            if age > stale_after_seconds:
                log.warning(
                    "S3 sync lock s3://%s/%s is %.0fs old (> %.0fs) — treating as abandoned "
                    "(holder likely crashed mid-sync) and reclaiming it.",
                    bucket, key, age, stale_after_seconds,
                )
                try:
                    s3.delete_object(Bucket=bucket, Key=key, IfMatch=head["ETag"])
                except (ClientError, BotoCoreError):
                    pass  # another device reclaimed it first — loop and race to create it again
                continue

            if time.time() >= deadline:
                log.warning(
                    "S3 sync lock s3://%s/%s is held by another device (age %.0fs) — giving up "
                    "after %.0fs and skipping this run's locked-scope sync.",
                    bucket, key, age, wait_timeout_seconds,
                )
                return None
            time.sleep(_SYNC_LOCK_POLL_INTERVAL_SECONDS)
    except (ClientError, BotoCoreError, OSError) as e:
        log.warning("Could not acquire S3 sync lock s3://%s/%s: %s", bucket, key, e)
        return None


def release_sync_lock(token: str, *, lock_name: str = "sync.lock") -> None:
    """Release a lock acquired via :func:`acquire_sync_lock` (same ``lock_name``). Logs and returns
    on any failure."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = _s3_client()
    except ImportError:
        return

    bucket = s3_output_bucket()
    key = _sync_lock_key(lock_name)
    try:
        s3.delete_object(Bucket=bucket, Key=key, IfMatch=token)
        log.debug("Released S3 sync lock s3://%s/%s", bucket, key)
    except (ClientError, BotoCoreError, OSError) as e:
        log.warning(
            "Could not release S3 sync lock s3://%s/%s (it will self-clear once stale): %s",
            bucket, key, e,
        )


def iter_local_files(root: Path) -> list[Path]:
    """
    Local files under *root* that participate in this module's plain S3 sync — excludes
    ``screenshots/`` (never synced) and ``coverletters/``/``form_fill_rules/`` (synced separately
    by ``utils/s3_log_sync.py``).
    """
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            if "__pycache__" in p.parts or p.name.endswith(".tmp"):
                continue
            rel = p.relative_to(root).as_posix()
            if _skip_local_only_rel(rel) or _skip_log_managed_rel(rel):
                continue
            out.append(p)
    return sorted(out)


def _s3_client():
    import boto3

    return boto3.session.Session().client("s3")


def _merge_s3_csv(s3, bucket: str, key: str, local_path: Path) -> bool:
    """
    Download a sheet-layout CSV from S3 and union-merge it with the local copy.

    Local rows are passed first so local data wins on URL collisions. Returns True if
    the S3 key existed (False means no S3 object — local file is left untouched).
    """
    import csv
    import io

    from botocore.exceptions import ClientError

    from .apply_sheets import _is_applications_sheet_header_row
    from .sheet_csv import SHEET_HEADER, read_sheet_csv, sort_sheet_rows_by_date, union_sheet_rows, write_sheet_csv

    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        text = resp["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return False
        raise

    s3_header: list[str] | None = None
    s3_rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not any((c or "").strip() for c in row):
            continue
        if _is_applications_sheet_header_row(row):
            s3_header = row
        else:
            s3_rows.append(row)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_header, local_rows = read_sheet_csv(local_path)
    merged = union_sheet_rows(local_rows, s3_rows)
    if len(merged) > len(local_rows):
        merged = sort_sheet_rows_by_date(merged)
    write_sheet_csv(local_path, local_header or s3_header or list(SHEET_HEADER), merged)
    return True


def _clean_memory_against_archive(memory_csv: Path, archive_csv: Path) -> int:
    """
    Remove rows from memory_csv whose URL key already appears in archive_csv.

    Returns the number of rows removed. Archive is the authoritative long-term record;
    memory should never contain duplicates of archived jobs.
    """
    from .sheet_csv import read_sheet_csv, sheet_row_key, write_sheet_csv

    _, archive_rows = read_sheet_csv(archive_csv)
    archive_keys = {k for r in archive_rows if (k := sheet_row_key(r))}
    if not archive_keys:
        return 0

    header, rows = read_sheet_csv(memory_csv)
    kept = [r for r in rows if sheet_row_key(r) not in archive_keys]
    removed = len(rows) - len(kept)
    if removed:
        write_sheet_csv(memory_csv, header, kept)
    return removed


def sync_download_output(local_dir: Path | str = OUTPUT_DIR) -> int:
    """
    Download objects from S3 into *local_dir*, excluding ``coverletters/``/``form_fill_rules/``
    (synced separately by ``utils/s3_log_sync.py``) and local-only dirs like ``screenshots/``.
    """
    bucket = s3_output_bucket()
    if not bucket:
        return 0

    root = resolve_output_dir(local_dir)
    list_prefix = s3_list_prefix_for_dir(root)

    try:
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = _s3_client()
    except ImportError:
        log.warning("S3 download skipped: install boto3 (pip install boto3)")
        return 0

    # Resolve CSV paths for merge-not-overwrite detection.
    merge_csv_paths = frozenset(
        p.resolve()
        for pair in _MEMORY_ARCHIVE_PAIRS
        for p in pair
    )

    downloaded = 0
    skipped_local_only = 0
    to_sync: list[tuple[str, str, dict]] = []
    try:
        _sync_progress("S3: listing objects under s3://%s/%s …", bucket, list_prefix)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                if not key or key.endswith("/"):
                    continue
                if not key.startswith(list_prefix):
                    continue
                rel = key[len(list_prefix):]
                if not rel:
                    continue
                if _skip_local_only_rel(rel) or _skip_log_managed_rel(rel):
                    skipped_local_only += 1
                    continue
                to_sync.append((key, rel, obj))

        work: list[tuple[str, str, dict, str]] = [
            (key, rel, obj, "merging" if (root / rel).resolve() in merge_csv_paths else "downloading")
            for key, rel, obj in to_sync
        ]

        total = len(work)
        if total:
            _sync_progress(
                "S3: syncing %d object(s) from s3://%s/%s -> %s",
                total,
                bucket,
                list_prefix,
                root,
            )

        for i, (key, rel, obj, action) in enumerate(work, 1):
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Log before the transfer so a stall names the current object.
            print_s3_progress("download", i, total, rel, action=action)
            if action == "merging":
                # Merge instead of overwrite: union local + S3 records.
                _merge_s3_csv(s3, bucket, key, dest)
            else:
                s3.download_file(bucket, key, str(dest))
            downloaded += 1
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 download failed (s3://%s/%s): %s", bucket, list_prefix, e)
        return downloaded

    # Strip memory records that are already in the (now-merged) archive.
    for mem_csv, arch_csv in _MEMORY_ARCHIVE_PAIRS:
        removed = _clean_memory_against_archive(mem_csv, arch_csv)
        if removed:
            _sync_progress(
                "Removed %d record(s) from %s already present in archive",
                removed,
                mem_csv.name,
            )

    if skipped_local_only:
        _sync_progress(
            "S3: skipped %d object(s) handled elsewhere or local-only",
            skipped_local_only,
        )
    if downloaded:
        _sync_progress(
            "S3: downloaded/merged %d file(s) from s3://%s/%s -> %s",
            downloaded,
            bucket,
            list_prefix,
            root,
        )
    elif not to_sync:
        _sync_progress(
            "S3: no objects under s3://%s/%s (starting with empty or local-only output/)",
            bucket,
            list_prefix,
        )
    return downloaded


def sync_upload_output(local_dir: Path | str = OUTPUT_DIR) -> int:
    """
    Upload files under *local_dir* to S3, excluding ``coverletters/``/``form_fill_rules/`` (synced
    separately by ``utils/s3_log_sync.py``) and local-only dirs like ``screenshots/`` — see
    :func:`iter_local_files`.
    """
    bucket = s3_output_bucket()
    if not bucket:
        return 0

    root = resolve_output_dir(local_dir)
    if not root.is_dir():
        log.debug("S3 upload skipped: not a directory: %s", root)
        return 0

    files = iter_local_files(root)
    if not files:
        log.debug("S3 upload skipped: no files under %s", root)
        return 0

    try:
        from botocore.exceptions import BotoCoreError, ClientError

        s3 = _s3_client()
    except ImportError:
        log.warning("S3 upload skipped: install boto3 (pip install boto3)")
        return 0

    uploaded = 0
    to_upload: list[tuple[Path, str]] = [(path, path.relative_to(root).as_posix()) for path in files]

    skipped_local_only = 0
    for name in _SYNC_EXCLUDED_TOP_DIRS:
        excluded = root / name
        if excluded.is_dir():
            skipped_local_only += sum(1 for p in excluded.rglob("*") if p.is_file())
    if skipped_local_only:
        _sync_progress(
            "S3: skipping %d local-only file(s) (%s)",
            skipped_local_only,
            ", ".join(sorted(_SYNC_EXCLUDED_TOP_DIRS)),
        )

    total = len(to_upload)
    if total:
        _sync_progress(
            "S3: uploading %d file(s) from %s -> s3://%s/%s",
            total,
            root,
            bucket,
            s3_list_prefix_for_dir(root),
        )

    try:
        for i, (path, rel) in enumerate(to_upload, 1):
            key = s3_key_for_file(root, path)
            # Log before the transfer so a stall names the current object.
            print_s3_progress("upload", i, total, rel)
            ctype, _ = mimetypes.guess_type(path.name)
            extra = {"ContentType": ctype} if ctype else {}
            if extra:
                s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
            else:
                s3.upload_file(str(path), bucket, key)
            uploaded += 1
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 upload failed after %d file(s) (s3://%s/): %s", uploaded, bucket, e)
        return uploaded

    _sync_progress(
        "S3: uploaded %d file(s) from %s -> s3://%s/%s",
        uploaded,
        root,
        bucket,
        s3_list_prefix_for_dir(root),
    )
    if uploaded > 0:
        try:
            from .job_records import cleanup_listings_log_sidecars

            cleanup_listings_log_sidecars()
        except Exception:
            log.debug("Listings log sidecar cleanup after S3 upload failed", exc_info=True)
    return uploaded


def sync_download_output_coordinated(
    local_dir: Path | str = OUTPUT_DIR,
    *,
    cover_letter_modes: tuple[str, ...] | None = None,
) -> str | None:
    """
    Download from S3: cover letters and form-fill rules catch up via their own operation logs
    (``utils/s3_log_sync.py``, imported lazily here to avoid a circular import — that module
    imports back into this one), each with its own short-held lock; then the general
    ``sync.lock`` is acquired and everything else (tracking CSVs, consulting-company memory,
    etc.) downloads only if it's held.

    Returns the ``sync.lock`` token — hold onto it and pass it to
    :func:`sync_upload_output_coordinated` at the end of the run, *after* whatever
    merge/processing this run does with the downloaded data. The lock must span that whole
    round trip, not just this download call, or two devices can still each read a consistent
    pre-lock snapshot and clobber each other on upload regardless. ``None`` means the lock
    wasn't acquired (see :func:`acquire_sync_lock`) — this run's ``sync.lock``-scope download was
    skipped, and the caller should skip its ``sync.lock``-scope upload too. Cover letters and
    form-fill rules are unaffected by this particular token either way — they coordinate through
    their own separate locks.
    """
    from .s3_log_sync import RESOURCE_COVERLETTERS, RESOURCE_FORM_FILL_RULES, sync_log_download

    root = resolve_output_dir(local_dir)
    sync_log_download(RESOURCE_COVERLETTERS, root=root / COVERLETTERS_DIR.name, mode_filter=cover_letter_modes)
    sync_log_download(RESOURCE_FORM_FILL_RULES, root=root / FORM_FILL_RULES_DIR.name)

    token = acquire_sync_lock()
    if token is not None:
        sync_download_output(local_dir)
    else:
        _sync_progress(
            "S3: sync lock unavailable — skipping this run's download of applications/consulting "
            "memory/etc. (cover letters and form-fill rules still synced normally)."
        )
    return token


def sync_upload_output_coordinated(
    local_dir: Path | str = OUTPUT_DIR,
    *,
    cover_letter_modes: tuple[str, ...] | None = None,
    lock_token: str | None,
    cover_letter_changes: "PendingChangeTracker | None" = None,
    form_fill_rule_changes: "PendingChangeTracker | None" = None,
) -> None:
    """
    Upload to S3, the upload-side counterpart to :func:`sync_download_output_coordinated`.

    ``lock_token`` is whatever that function returned at the start of the run — everything except
    cover letters/form-fill rules only uploads (and the ``sync.lock`` is released) if it's not
    ``None``. ``cover_letter_changes``/``form_fill_rule_changes`` are each resource's
    :class:`utils.s3_log_sync.PendingChangeTracker` accumulated over the run (``None`` or empty is
    a no-op for that resource) — pushed via their own log-sync protocol regardless of whether
    ``lock_token`` was acquired, since they coordinate through their own separate locks.
    """
    from .s3_log_sync import PendingChangeTracker, RESOURCE_COVERLETTERS, RESOURCE_FORM_FILL_RULES, sync_log_upload

    root = resolve_output_dir(local_dir)
    if cover_letter_changes is not None:
        sync_log_upload(RESOURCE_COVERLETTERS, root=root / COVERLETTERS_DIR.name, pending=cover_letter_changes)
    if form_fill_rule_changes is not None:
        sync_log_upload(RESOURCE_FORM_FILL_RULES, root=root / FORM_FILL_RULES_DIR.name, pending=form_fill_rule_changes)

    if lock_token is not None:
        sync_upload_output(local_dir)
        release_sync_lock(lock_token)
    else:
        _sync_progress(
            "S3: sync lock was not held this run — skipping upload of applications/consulting "
            "memory/etc. (cover letters and form-fill rules still uploaded normally)."
        )
