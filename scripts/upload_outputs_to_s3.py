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
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

from utils.s3_outputs import (  # noqa: E402
    iter_local_files,
    resolve_output_dir,
    s3_key_for_file,
    s3_output_bucket,
    s3_output_sync_enabled,
    sync_upload_output,
)


def _load_env() -> None:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")


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

    if not s3_output_sync_enabled():
        print(
            "error: set S3_OUTPUT_BUCKET in the environment or .env (see docs/s3_outputs.md)",
            file=sys.stderr,
        )
        return 1

    bucket = s3_output_bucket()
    root = resolve_output_dir(args.local_dir if args.local_dir.is_absolute() else _REPO_ROOT / args.local_dir)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    files = iter_local_files(root)
    if not files:
        print(f"no files under {root}")
        return 0

    if args.dry_run:
        for path in files:
            print(f"DRY-RUN  s3://{bucket}/{s3_key_for_file(root, path)}")
        print(f"DRY-RUN: {len(files)} file(s) would upload to s3://{bucket}/")
        return 0

    uploaded = sync_upload_output(root)
    if uploaded != len(files):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
