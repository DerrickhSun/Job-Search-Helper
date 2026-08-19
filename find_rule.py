"""
Find which form-fill rule (if any) answers a given question/label.

When LinkedIn/Greenhouse auto-fill answers a question incorrectly, use this to find the exact
rule responsible (its file, line, and what it answers) — or confirm that no rule matched at all,
so you know whether to fix an existing rule or add a new one.

Searches the same rules directory the live filler uses (``output/form_fill_rules/``, falling back
to legacy/default locations — see ``utils/form_fill_rules.py``), across all rule categories:
screening_yes_no, text_inputs, selects, textareas, checkbox_groups. Within a category the first
matching rule (in file-name order, then in-file order) is the one actually used; any further
matches are reported as "shadowed" so you can spot duplicate/conflicting rules too.

Usage:
    python find_rule.py
    python find_rule.py "Are you legally authorized to work in the United States?"
    python find_rule.py --rules-path defaults/form_fill_rules "How did you hear about us?"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Windows consoles default to cp1252, which can't render the arrows/dashes used below.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from utils.form_fill_rules import DEFAULT_RULES_PATH, FormFillRulesEngine, label_matches

# Mirrors utils/form_fill_rules.py's _CONDITIONAL_QUESTION_RE — questions starting with "if" are
# never auto-answered by any rule (no inter-question dependency tracking), no matter what matches.
_CONDITIONAL_QUESTION_RE = re.compile(r"^if\b")

# (category key, what kind of form element it drives)
_CATEGORIES: list[tuple[str, str]] = [
    (
        "screening_yes_no",
        "Yes/No screening question (radio buttons or single checkbox). Also takes priority over "
        "text_inputs/selects/textareas rules below when the same label appears as one of those.",
    ),
    ("text_inputs", "Free-text input field"),
    ("selects", "Dropdown / select field"),
    ("textareas", "Textarea field (e.g. cover letter, long-form answer)"),
    ("checkbox_groups", "Checkbox/radio fieldset with multiple options (matched on the fieldset's legend)"),
]


def _load_rule_files(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(path.glob("*.json"), key=lambda p: p.name.lower())
        if not files:
            raise FileNotFoundError(f"No *.json rule files found in {path}")
        return files
    if path.is_file():
        return [path]
    raise FileNotFoundError(f"Form fill rules not found: {path}")


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Form fill rules file must be a JSON object: {path}")
    return data


def _merged_rules(files: list[Path]) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    """category -> ordered (source file, rule) pairs, in the same order the live engine sees them."""
    merged: dict[str, list[tuple[Path, dict[str, Any]]]] = {cat: [] for cat, _ in _CATEGORIES}
    for fp in files:
        data = _read_json_object(fp)
        for cat in merged:
            for rule in data.get(cat) or []:
                merged[cat].append((fp, rule))
    return merged


def find_matches(label: str, rules_path: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    """
    category -> every (file, rule) whose ``match`` matches ``label``, ordered exactly like the live
    engine resolves them: descending ``priority`` (default 0), then original file/list order as a
    tie-break (see ``FormFillRulesEngine._matching_rules_by_priority``). Index 0 is what's actually used.
    """
    files = _load_rule_files(rules_path)
    merged = _merged_rules(files)
    n = FormFillRulesEngine.normalize_label(label)
    out: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for cat, _desc in _CATEGORIES:
        candidates = [
            (i, fp, rule)
            for i, (fp, rule) in enumerate(merged[cat])
            if n and label_matches(n, rule.get("match", {}))
        ]
        candidates.sort(key=lambda t: (-_priority(t[2]), t[0]))
        out[cat] = [(fp, rule) for _, fp, rule in candidates]
    return out


def _priority(rule: dict[str, Any]) -> float:
    try:
        return float(rule.get("priority", 0))
    except (TypeError, ValueError):
        return 0.0


def _coerce_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    seen: set[str] = set()
    for v in items:
        s = str(v).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _describe_rule(category: str, rule: dict[str, Any]) -> str:
    """Human-readable summary of what a matched rule answers."""
    if category == "screening_yes_no":
        region_cfg = rule.get("answer_by_region")
        if region_cfg:
            parts = []
            for cond in region_cfg.get("conditions") or []:
                terms = (
                    (cond.get("contains_any") or [])
                    + (cond.get("contains_word_any") or [])
                    + (cond.get("contains_token_any") or [])
                )
                parts.append(f"{cond.get('answer')!r} if label region matches any of {terms}")
            if "default_answer" in region_cfg:
                parts.append(f"else {region_cfg.get('default_answer')!r}")
            region_regex = region_cfg.get("region_regex", "")
            return f"depends on region extracted via regex {region_regex!r}: " + "; ".join(parts)
        cands = _coerce_list(rule.get("answer"))
        return " → ".join(cands) if cands else "(no answer configured)"

    if category == "checkbox_groups":
        src_map = rule.get("choose_label_from_apply_source")
        if isinstance(src_map, dict) and src_map:
            return "by apply source: " + ", ".join(f"{k}={v!r}" for k, v in src_map.items())
        cands = _coerce_list(rule.get("choose_label") or rule.get("option_label"))
        return " → ".join(cands) if cands else "(no option configured)"

    # text_inputs / selects / textareas all use a "result" block.
    result = rule.get("result") or {}
    t = (result.get("type") or "").strip()
    if t == "literal":
        return repr(result.get("value", ""))
    if t == "literal_fallbacks":
        vals = _coerce_list(result.get("values"))
        return " → ".join(vals) if vals else "(no values configured)"
    if t == "literal_from_apply_source":
        m = result.get("when") or result.get("by_apply_source") or {}
        return "by apply source: " + ", ".join(f"{k}={v!r}" for k, v in m.items())
    if t == "name_part":
        return f"your {result.get('part', '?')} name (from resume)"
    if t == "resume_key_list":
        return "resume field(s): " + ", ".join(result.get("keys") or [])
    if t == "cover_letter_full":
        return "full cover letter text"
    if t == "cover_letter_truncated":
        return f"cover letter, truncated to {result.get('max_length', 500)} characters"
    return f"(unrecognized result type: {t!r})"


def _rule_line_number(file_path: Path, rule: dict[str, Any]) -> int | None:
    """Best-effort line number of a rule's ``id`` field within its file, for a clickable location."""
    rule_id = rule.get("id")
    if not rule_id:
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    needle = json.dumps(str(rule_id))  # JSON-encoded, e.g. "my_rule_id"
    m = re.search(r'"id"\s*:\s*' + re.escape(needle), text)
    return text.count("\n", 0, m.start()) + 1 if m else None


def _github_link(file_path: Path, line: int | None) -> str | None:
    """GitHub blob URL for ``file_path`` at ``line``, if it's tracked by git with a GitHub remote."""
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5,
        )
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5,
        )
        if root.returncode != 0 or remote.returncode != 0 or branch.returncode != 0:
            return None
        root_dir = root.stdout.strip()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(file_path)],
            capture_output=True, text=True, cwd=root_dir, timeout=5,
        )
        if tracked.returncode != 0:
            return None
    except (subprocess.SubprocessError, OSError):
        return None

    remote_url = remote.stdout.strip()
    branch_name = branch.stdout.strip()
    if not remote_url or not branch_name or branch_name == "HEAD":
        return None
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?$", remote_url)
    if not m:
        return None
    rel = file_path.resolve().relative_to(Path(root_dir).resolve()).as_posix()
    url = f"https://github.com/{m.group(1)}/blob/{branch_name}/{rel}"
    return f"{url}#L{line}" if line else url


def report(question: str, rules_path: Path) -> None:
    matches = find_matches(question, rules_path)
    normalized = FormFillRulesEngine.normalize_label(question)

    print(f"\nQuestion : {question!r}")
    print(f"Rules dir: {rules_path}")

    if _CONDITIONAL_QUESTION_RE.match(normalized):
        print(
            "\nNote: this label starts with \"if\" — the live filler treats it as conditional and "
            "never\nauto-answers it, regardless of any matches shown below "
            "(see utils/form_fill_rules.py)."
        )

    any_found = False
    for cat, desc in _CATEGORIES:
        cat_matches = matches[cat]
        if not cat_matches:
            continue
        any_found = True
        print(f"\n[{cat}] {desc}")
        for i, (fp, rule) in enumerate(cat_matches):
            tag = "MATCH — this is what the app actually uses" if i == 0 else "shadowed by the match above"
            line = _rule_line_number(fp, rule)
            location = f"{fp}:{line}" if line else str(fp)
            link = _github_link(fp, line)
            print(f"  - {tag}")
            print(f"    id      : {rule.get('id', '(no id)')}")
            print(f"    priority: {_priority(rule):g}")
            if rule.get("comment"):
                print(f"    comment : {rule['comment']}")
            print(f"    answer  : {_describe_rule(cat, rule)}")
            print(f"    location: {location}")
            if link:
                print(f"    link    : {link}")

    if not any_found:
        print(
            "\nNo rule matches this question in any category — it will be left blank for manual "
            "review\n(or fall through to whatever default behavior the filler has for unmatched fields)."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find which form-fill rule (if any) answers a given question/label."
    )
    parser.add_argument(
        "question", nargs="*", help="The question/label text as it appears on the form."
    )
    parser.add_argument(
        "--rules-path",
        type=Path,
        default=None,
        help=f"Rules file/dir to search (default: whatever the live filler uses, currently {DEFAULT_RULES_PATH}).",
    )
    args = parser.parse_args()

    question = " ".join(args.question) if args.question else input("Enter the form question/label to look up: ").strip()
    if not question:
        print("No question entered.")
        return 0

    rules_path = args.rules_path or DEFAULT_RULES_PATH
    try:
        report(question, rules_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
