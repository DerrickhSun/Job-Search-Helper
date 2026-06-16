#!/usr/bin/env python3
"""
Delete cover letter ``.docx`` files older than N days (local and optionally S3).

Uses ``COVER_LETTER_MAX_AGE_DAYS`` from ``.env`` (default 7; set to 0 to disable).

Usage (from repo root):
  python scripts/prune_old_cover_letters.py
  python scripts/prune_old_cover_letters.py --dry-run
  python scripts/prune_old_cover_letters.py --days 14 --local-only
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

from utils.output_cleanup import (  # noqa: E402
    prune_local_cover_letters,
    prune_local_cover_letters_by_count,
    prune_s3_cover_letters,
    prune_s3_cover_letters_by_count,
)


def main() -> int:
    if load_dotenv:
        load_dotenv(_REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Prune old cover letter DOCX files.")
    parser.add_argument(
        "--days",
        type=float,
        default=None,
        metavar="N",
        help="Delete files older than N days (default: COVER_LETTER_MAX_AGE_DAYS or 7).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions only.")
    parser.add_argument("--local-only", action="store_true", help="Do not delete from S3.")
    parser.add_argument("--s3-only", action="store_true", help="Do not delete local files.")
    args = parser.parse_args()

    if args.days is not None and args.days <= 0:
        print("Pruning disabled (--days <= 0).", file=sys.stderr)
        return 0

    local = 0
    remote = 0
    if not args.s3_only:
        local = prune_local_cover_letters(max_age_days=args.days, dry_run=args.dry_run)
        local += prune_local_cover_letters_by_count(dry_run=args.dry_run)
    if not args.local_only:
        remote = prune_s3_cover_letters(max_age_days=args.days, dry_run=args.dry_run)
        remote += prune_s3_cover_letters_by_count(dry_run=args.dry_run)

    print(f"local: {local}  s3: {remote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
