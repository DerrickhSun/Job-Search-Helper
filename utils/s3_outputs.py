"""
Sync the local ``output/`` tree with S3 (see docs/s3_outputs.md).

Cover letters live under ``output/coverletters/{linkedin,filter,greenhouse}/``. Per-run
``cover_letter_modes`` limits which subfolders are downloaded/uploaded/pruned; other
``output/`` files always sync.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from .output_paths import (
    APPLICATIONS_ARCHIVE_CSV,
    APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_CSV,
    ASSISTED_APPLICATIONS_HISTORY_CSV,
    COVERLETTERS_DIR,
    OUTPUT_DIR,
    cover_letter_mode_names,
)

log = logging.getLogger(__name__)

_COVER_MODES = cover_letter_mode_names()

# (memory_csv, archive_csv) pairs — memory is cleaned against its archive after each sync.
_MEMORY_ARCHIVE_PAIRS: tuple[tuple[Path, Path], ...] = (
    (APPLICATIONS_CSV, APPLICATIONS_ARCHIVE_CSV),
    (ASSISTED_APPLICATIONS_CSV, ASSISTED_APPLICATIONS_HISTORY_CSV),
)


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


def _cover_letter_mode_from_rel(rel: str) -> str | None:
    """
    Mode for ``coverletters/{mode}/…`` keys, or ``linkedin`` for legacy flat ``coverletters/*.docx``.
    """
    parts = rel.replace("\\", "/").split("/")
    if not parts or parts[0] != COVERLETTERS_DIR.name:
        return None
    if len(parts) >= 3 and parts[1] in _COVER_MODES:
        return parts[1]
    if len(parts) == 2 and parts[1].lower().endswith(".docx"):
        return "linkedin"
    return None


def _skip_cover_letter_rel(rel: str, cover_letter_modes: tuple[str, ...] | None) -> bool:
    if cover_letter_modes is None:
        return False
    mode = _cover_letter_mode_from_rel(rel)
    if mode is None:
        return False
    return mode not in cover_letter_modes


def _preserve_cover_letter_mtime(dest: Path) -> bool:
    try:
        rel = dest.relative_to(resolve_output_dir(OUTPUT_DIR))
    except ValueError:
        return False
    return _cover_letter_mode_from_rel(rel.as_posix()) is not None


def iter_local_files(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            if "__pycache__" in p.parts or p.name.endswith(".tmp"):
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


def sync_download_output(
    local_dir: Path | str = OUTPUT_DIR,
    *,
    cover_letter_modes: tuple[str, ...] | None = None,
    cover_letter_subdirs: tuple[str, ...] | None = None,
) -> int:
    """
    Download objects from S3 into *local_dir*.

    When ``cover_letter_modes`` is set (e.g. ``("filter",)``), skip other
    ``coverletters/{mode}/`` prefixes. ``None`` downloads every mode subfolder.
    """
    if cover_letter_modes is None and cover_letter_subdirs is not None:
        cover_letter_modes = cover_letter_subdirs

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
    skipped_cover = 0
    try:
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
                if _skip_cover_letter_rel(rel, cover_letter_modes):
                    skipped_cover += 1
                    continue
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.resolve() in merge_csv_paths:
                    # Merge instead of overwrite: union local + S3 records.
                    _merge_s3_csv(s3, bucket, key, dest)
                elif _preserve_cover_letter_mtime(dest) and dest.is_file():
                    # Never overwrite an existing local cover letter.
                    continue
                else:
                    s3.download_file(bucket, key, str(dest))
                    if _preserve_cover_letter_mtime(dest):
                        last_mod = obj.get("LastModified")
                        if last_mod is not None:
                            ts = last_mod.timestamp()
                            os.utime(dest, (ts, ts))
                downloaded += 1
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 download failed (s3://%s/%s): %s", bucket, list_prefix, e)
        return downloaded

    # Strip memory records that are already in the (now-merged) archive.
    for mem_csv, arch_csv in _MEMORY_ARCHIVE_PAIRS:
        removed = _clean_memory_against_archive(mem_csv, arch_csv)
        if removed:
            log.info(
                "Removed %d record(s) from %s already present in archive",
                removed,
                mem_csv.name,
            )

    if cover_letter_modes and skipped_cover:
        log.info(
            "S3: skipped %d cover-letter object(s) outside active mode(s) %s",
            skipped_cover,
            cover_letter_modes,
        )
    if downloaded:
        log.info(
            "S3: downloaded/merged %d file(s) from s3://%s/%s -> %s",
            downloaded,
            bucket,
            list_prefix,
            root,
        )
    else:
        log.info("S3: no objects under s3://%s/%s (starting with empty or local-only output/)", bucket, list_prefix)
    return downloaded


def sync_upload_output(
    local_dir: Path | str = OUTPUT_DIR,
    *,
    cover_letter_modes: tuple[str, ...] | None = None,
    cover_letter_subdirs: tuple[str, ...] | None = None,
) -> int:
    """Upload files under *local_dir* to S3 (same cover-letter mode filtering as download)."""
    if cover_letter_modes is None and cover_letter_subdirs is not None:
        cover_letter_modes = cover_letter_subdirs

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
    skipped_cover = 0
    try:
        for path in files:
            rel = path.relative_to(root).as_posix()
            if _skip_cover_letter_rel(rel, cover_letter_modes):
                skipped_cover += 1
                continue
            key = s3_key_for_file(root, path)
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

    if cover_letter_modes and skipped_cover:
        log.info(
            "S3: skipped uploading %d local cover-letter file(s) outside active mode(s) %s",
            skipped_cover,
            cover_letter_modes,
        )
    log.info(
        "S3: uploaded %d file(s) from %s -> s3://%s/%s",
        uploaded,
        root,
        bucket,
        s3_list_prefix_for_dir(root),
    )
    return uploaded
