"""
Read and update the cached resume profile (``data/resume_profile.json``) without running
main.py or extension_server.py.

Why this exists: re-parsing a resume file (main.py's / extension_server.py's
``--force-resume-parse``) fully overwrites the cache with only the fields
``utils.resume_parser.ResumeParser`` produces -- ``name``, ``email``, ``phone``, ``skills``,
``experience``, ``projects``, ``education``, ``summary``, ``raw_text`` -- silently dropping any
hand-maintained fields it doesn't know how to extract: ``website``, ``linkedin``, ``location``,
``example_cover_letter`` (a past cover letter the generator may echo the tone/phrasing of), and
``experience_years_cap`` (caps the years-of-experience gate heuristic in ``eval_utils.matcher``).
``--reparse`` here merges instead of replacing by default, so those survive, and ``--set`` lets
you edit them directly without hand-editing the JSON.

Run from inside server/::

    python manage_resume.py                                 # print a summary of the cached profile
    python manage_resume.py --get example_cover_letter       # print one field's raw value
    python manage_resume.py --set location "San Diego, CA"   # update one hand-maintained field
    python manage_resume.py --set experience_years_cap 3
    python manage_resume.py --reparse resume.pdf             # re-parse a resume file, merging the
                                                              # result into the cache (keeps website/
                                                              # linkedin/location/example_cover_letter/
                                                              # experience_years_cap from before)
    python manage_resume.py --reparse resume.pdf --overwrite # re-parse and replace the cache entirely
                                                              # (old --force-resume-parse behavior)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Windows consoles default to cp1252, which can't render some characters used below.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from utils.resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE
from utils.resume_parser import ResumeParser

# Fields ResumeParser.parse() produces -- a --reparse (without --overwrite) replaces exactly
# these keys in the cache and leaves everything else untouched.
_PARSER_FIELDS = {
    "raw_text", "name", "email", "phone", "skills", "experience", "projects", "education", "summary",
}

# Hand-maintained fields --set is allowed to touch (not produced by ResumeParser). website/linkedin/
# github/portfolio are also read generically by utils/form_fill_rules.py to autofill application
# form fields asking for those links.
_SETTABLE_FIELDS = {
    "website", "linkedin", "github", "portfolio", "location", "example_cover_letter",
    "experience_years_cap",
}


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Resume cache must be a JSON object: {path}")
    return data


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _coerce_set_value(key: str, raw: str) -> Any:
    if key == "experience_years_cap":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"experience_years_cap must be a number, got {raw!r}") from None
    return raw


def print_summary(data: dict[str, Any]) -> None:
    if not data:
        print("No resume cache found yet -- run with --reparse resume.pdf to create one.")
        return

    def _line(label: str, value: Any) -> None:
        print(f"{label:<22}: {value}")

    _line("name", data.get("name") or "(not set)")
    _line("email", data.get("email") or "(not set)")
    _line("phone", data.get("phone") or "(not set)")
    _line("location", data.get("location") or "(not set)")
    _line("website", data.get("website") or "(not set)")
    _line("linkedin", data.get("linkedin") or "(not set)")
    _line("github", data.get("github") or "(not set)")
    _line("portfolio", data.get("portfolio") or "(not set)")
    _line("experience_years_cap", data.get("experience_years_cap", "(not set)"))

    summary = (data.get("summary") or "").strip()
    _line("summary", (summary[:150] + "...") if len(summary) > 150 else (summary or "(not set)"))

    skills = data.get("skills") or []
    skills_preview = f": {', '.join(skills[:8])}{', ...' if len(skills) > 8 else ''}" if skills else ""
    _line("skills", f"{len(skills)} listed{skills_preview}")

    experience = data.get("experience") or []
    print(f"{'experience':<22}: {len(experience)} entr{'y' if len(experience) == 1 else 'ies'}")
    for e in experience:
        if isinstance(e, dict):
            print(f"    - {e.get('title', '?')} | {e.get('company', '?')} | {e.get('dates', '?')}")

    projects = data.get("projects") or []
    print(f"{'projects':<22}: {len(projects)} entr{'y' if len(projects) == 1 else 'ies'}")
    for p in projects:
        if isinstance(p, dict):
            print(f"    - {p.get('title', '?')} | {p.get('dates', '?')}")

    education = data.get("education") or []
    print(f"{'education':<22}: {len(education)} entr{'y' if len(education) == 1 else 'ies'}")
    for ed in education:
        if isinstance(ed, dict):
            print(f"    - {ed.get('degree', '?')} | {ed.get('institution', '?')} | {ed.get('year', '?')}")

    example = (data.get("example_cover_letter") or "").strip()
    _line("example_cover_letter", f"{len(example)} chars set" if example else "(not set)")


def reparse(resume_file: Path, cache_path: Path, *, overwrite: bool) -> dict[str, Any]:
    if not resume_file.is_file():
        raise FileNotFoundError(f"Resume file not found: {resume_file}")
    parsed = ResumeParser().parse(str(resume_file))

    if overwrite:
        merged = parsed
    else:
        existing = _read_cache(cache_path)
        merged = {**existing, **{k: v for k, v in parsed.items() if k in _PARSER_FIELDS}}

    _write_cache(cache_path, merged)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read and update the cached resume profile (data/resume_profile.json)."
    )
    parser.add_argument(
        "--cache-path", type=Path, default=DEFAULT_RESUME_CACHE_PATH,
        help=f"Resume cache JSON path (default: {DEFAULT_RESUME_CACHE_PATH}).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--reparse", nargs="?", const=str(DEFAULT_RESUME_FILE), default=None, metavar="PATH",
        help=f"Re-parse a resume PDF/DOCX (default: {DEFAULT_RESUME_FILE}) and merge the result "
             "into the cache, keeping website/linkedin/location/example_cover_letter/"
             "experience_years_cap from the existing cache. Pass --overwrite to replace the "
             "cache entirely instead.",
    )
    group.add_argument("--get", metavar="FIELD", help="Print one field's raw JSON value and exit.")
    group.add_argument(
        "--set", nargs=2, metavar=("FIELD", "VALUE"),
        help=f"Set one hand-maintained field and save. Allowed fields: {', '.join(sorted(_SETTABLE_FIELDS))}.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="With --reparse, replace the cache entirely instead of merging (old --force-resume-parse behavior).",
    )
    args = parser.parse_args()

    cache_path = args.cache_path

    try:
        if args.reparse is not None:
            merged = reparse(Path(args.reparse), cache_path, overwrite=args.overwrite)
            mode = "Replaced" if args.overwrite else "Merged into"
            print(f"{mode} cache: {cache_path.resolve()}\n")
            print_summary(merged)
            return 0

        if args.get is not None:
            data = _read_cache(cache_path)
            if args.get not in data:
                print(f"Field {args.get!r} not present in cache.")
                return 1
            print(json.dumps(data[args.get], indent=2, ensure_ascii=False))
            return 0

        if args.set is not None:
            key, raw_value = args.set
            if key not in _SETTABLE_FIELDS:
                print(f"Error: --set can only touch {sorted(_SETTABLE_FIELDS)}, not {key!r}.")
                print("(Other fields come from parsing the resume file -- use --reparse instead.)")
                return 1
            data = _read_cache(cache_path)
            data[key] = _coerce_set_value(key, raw_value)
            _write_cache(cache_path, data)
            print(f"Set {key!r} in {cache_path.resolve()}")
            return 0

        print_summary(_read_cache(cache_path))
        return 0
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
