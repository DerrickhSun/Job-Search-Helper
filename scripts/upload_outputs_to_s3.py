#!/usr/bin/env python3
"""
Upload local bot outputs to an S3 bucket (mirror of a directory tree).

Prerequisites: pip install boto3 (see requirements.txt), and in .env (or the environment):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION,
  S3_OUTPUT_BUCKET, optional S3_OUTPUT_PREFIX

Usage (from repo root):
  python scripts/upload_outputs_to_s3.py
  python scripts/upload_outputs_to_s3.py --dry-run
  python scripts/upload_outputs_to_s3.py --local-dir data
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env() -> None:
    if load_dotenv:
        load_dotenv(_repo_root() / ".env")


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            if "__pycache__" in p.parts or p.name.endswith(".tmp"):
                continue
            out.append(p)
    return sorted(out)


def main() -> int:
    _load_env()

    parser = argparse.ArgumentParser(description="Upload a local directory to S3.")
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=Path("output"),
        help="Directory to upload (default: output). Relative paths are from repo root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print S3 keys only; do not upload.",
    )
    args = parser.parse_args()

    bucket = (os.environ.get("S3_OUTPUT_BUCKET") or "").strip()
    if not bucket:
        print(
            "error: set S3_OUTPUT_BUCKET in the environment or .env (see docs/s3_outputs.md)",
            file=sys.stderr,
        )
        return 1

    prefix = (os.environ.get("S3_OUTPUT_PREFIX") or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    root = args.local_dir
    if not root.is_absolute():
        root = _repo_root() / root
    root = root.resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("error: install boto3: pip install boto3", file=sys.stderr)
        return 1

    session = boto3.session.Session()
    s3 = session.client("s3")

    files = _iter_files(root)
    if not files:
        print(f"no files under {root}")
        return 0

    uploaded = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        key = f"{prefix}{root.name}/{rel}" if prefix else f"{root.name}/{rel}"

        ctype, _ = mimetypes.guess_type(path.name)
        extra = {"ContentType": ctype} if ctype else {}

        if args.dry_run:
            print(f"DRY-RUN  s3://{bucket}/{key}")
            continue

        try:
            if extra:
                s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
            else:
                s3.upload_file(str(path), bucket, key)
        except (ClientError, BotoCoreError, OSError) as e:
            print(f"error uploading {path} -> s3://{bucket}/{key}: {e}", file=sys.stderr)
            return 1
        uploaded += 1
        print(f"uploaded  s3://{bucket}/{key}")

    if args.dry_run:
        print(f"DRY-RUN: {len(files)} file(s) would upload to s3://{bucket}/")
    else:
        print(f"done: {uploaded} file(s) -> s3://{bucket}/{prefix or ''}{root.name}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
