"""
Sync the local ``output/`` tree with S3 (see docs/s3_outputs.md).

Used by ``main.py`` at run start/end and by ``scripts/upload_outputs_to_s3.py``.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from .output_paths import OUTPUT_DIR

log = logging.getLogger(__name__)


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


def sync_download_output(local_dir: Path | str = OUTPUT_DIR) -> int:
    """
    Download objects from S3 into *local_dir* (creates parents as needed).

    Returns the number of files written. Skips when ``S3_OUTPUT_BUCKET`` is unset.
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

    downloaded = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                if not key or key.endswith("/"):
                    continue
                if not key.startswith(list_prefix):
                    continue
                rel = key[len(list_prefix) :]
                if not rel:
                    continue
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, key, str(dest))
                # Preserve S3 object age on cover letters so local prune by mtime stays meaningful.
                if "coverletters" in dest.parts:
                    last_mod = obj.get("LastModified")
                    if last_mod is not None:
                        ts = last_mod.timestamp()
                        os.utime(dest, (ts, ts))
                downloaded += 1
    except (ClientError, BotoCoreError, OSError) as e:
        log.error("S3 download failed (s3://%s/%s): %s", bucket, list_prefix, e)
        return downloaded

    if downloaded:
        log.info(
            "S3: downloaded %d file(s) from s3://%s/%s -> %s",
            downloaded,
            bucket,
            list_prefix,
            root,
        )
    else:
        log.info("S3: no objects under s3://%s/%s (starting with empty or local-only output/)", bucket, list_prefix)
    return downloaded


def sync_upload_output(local_dir: Path | str = OUTPUT_DIR) -> int:
    """
    Upload all files under *local_dir* to S3.

    Returns the number of files uploaded. Skips when ``S3_OUTPUT_BUCKET`` is unset.
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
    try:
        for path in files:
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

    log.info(
        "S3: uploaded %d file(s) from %s -> s3://%s/%s",
        uploaded,
        root,
        bucket,
        s3_list_prefix_for_dir(root),
    )
    return uploaded
