"""
Import screening / text answers captured by the browser extension into form fill rules.

Parses ``saved_jobs_application_questions.txt``, compares each Q/A pair to existing rules in
``output/form_fill_rules/``, appends new rules to ``auto_rules.json``, and interactively resolves
conflicts when an existing rule disagrees with the extension answer.

Blank extension answers are not auto-saved: each is confirmed interactively (same style as
conflicts). Empty literal rules already in ``auto_rules.json`` are purged on import.
"""

from __future__ import annotations

import codecs
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.form_fill_rules import FormFillRulesEngine, label_matches, normalize_label_for_exact

log = logging.getLogger(__name__)
from utils.output_paths import FORM_FILL_RULES_DIR

AUTO_RULES_FILENAME = "auto_rules.json"
RULE_CATEGORIES = ("screening_yes_no", "text_inputs", "textareas", "selects")

# Deleted rules are moved here instead of discarded outright, so a rule dropped for conflicting
# with an extension answer can still be reviewed, fixed, and restored. Deliberately a sibling
# *file* of FORM_FILL_RULES_DIR (not a file inside it) — FormFillRulesEngine/RuleIndex only glob
# *.json directly inside that directory, so recycled rules can never accidentally get loaded and
# used to fill fields.
RECYCLE_BIN_FILENAME_SUFFIX = "_recycle_bin.json"

QUESTION_EXPORT_ALIASES = (
    "saved_jobs_application_questions.txt",
    "saved_job_application_questions.txt",
)
SAVED_JOBS_QUESTIONS_FILENAME = QUESTION_EXPORT_ALIASES[0]


@dataclass
class RuleRef:
    file: Path
    category: str
    index: int
    rule: dict[str, Any]


@dataclass
class ExtensionQuestion:
    question: str
    answer: str
    url: str | None = None
    company_title: str | None = None


@dataclass
class QuestionConflict:
    question: ExtensionQuestion
    rule_ref: RuleRef
    rule_answer: str


def _is_blank_answer(value: str | None) -> bool:
    return not (value or "").strip()


def _rule_is_blank(rule: dict[str, Any]) -> bool:
    """True when a rule only stores an empty literal / empty answer list (no useful fill value)."""
    if "answer" in rule:
        ans = rule.get("answer")
        if isinstance(ans, list):
            return not ans or all(_is_blank_answer(str(x)) for x in ans)
        return _is_blank_answer(str(ans) if ans is not None else "")
    result = rule.get("result")
    if not isinstance(result, dict):
        return False
    t = (result.get("type") or "").strip()
    if t == "literal":
        return _is_blank_answer(str(result.get("value") or ""))
    if t == "literal_fallbacks":
        values = result.get("values") or []
        return not values or all(_is_blank_answer(str(x)) for x in values)
    return False


class RuleIndex:
    """Merged rule lists with file provenance (same merge order as :class:`FormFillRulesEngine`)."""

    def __init__(self, rules_dir: Path | None = None) -> None:
        self.rules_dir = Path(rules_dir or FORM_FILL_RULES_DIR)
        self.screening_yes_no: list[RuleRef] = []
        self.text_inputs: list[RuleRef] = []
        self.textareas: list[RuleRef] = []
        self.selects: list[RuleRef] = []
        self._build()

    def _build(self) -> None:
        self.screening_yes_no = []
        self.text_inputs = []
        self.textareas = []
        self.selects = []
        if not self.rules_dir.is_dir():
            return
        files = sorted(self.rules_dir.glob("*.json"), key=lambda p: p.name.lower())
        for fp in files:
            data = _read_json_object(fp)
            for category in RULE_CATEGORIES:
                rules = data.get(category)
                if not isinstance(rules, list):
                    continue
                bucket: list[RuleRef] = getattr(self, category)
                for idx, rule in enumerate(rules):
                    if isinstance(rule, dict):
                        bucket.append(RuleRef(file=fp, category=category, index=idx, rule=rule))

    def reload(self) -> None:
        self._build()

    def find_matching_rule(self, question: str) -> RuleRef | None:
        n = FormFillRulesEngine.normalize_label(question)
        if not n:
            return None
        for category in RULE_CATEGORIES:
            for ref in getattr(self, category):
                if label_matches(n, ref.rule.get("match", {})):
                    return ref
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _write_json_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json_array(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_json_array(path: Path, data: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def recycle_bin_path(rules_dir: Path | None = None) -> Path:
    base = Path(rules_dir or FORM_FILL_RULES_DIR)
    return base.parent / (base.name + RECYCLE_BIN_FILENAME_SUFFIX)


def list_recycled_rules(*, rules_dir: Path | None = None) -> list[dict[str, Any]]:
    """All recycled entries, oldest first — each has ``id`` (its position, stable for one session)."""
    entries = _read_json_array(recycle_bin_path(rules_dir))
    out = []
    for i, entry in enumerate(entries):
        if isinstance(entry, dict):
            out.append({"id": i, **entry})
    return out


def _recycle_rule(
    ref: RuleRef,
    *,
    reason: str,
    context: dict[str, Any] | None = None,
    rules_dir: Path | None = None,
) -> None:
    """Append ``ref``'s rule to the recycle bin. Does not touch the rule's source file."""
    entry: dict[str, Any] = {
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "category": ref.category,
        "source_file": ref.file.name,
        "rule": ref.rule,
    }
    if context:
        entry["context"] = context
    path = recycle_bin_path(rules_dir)
    bin_entries = _read_json_array(path)
    bin_entries.append(entry)
    _write_json_array(path, bin_entries)


def restore_recycled_rule(
    entry_id: int, *, rules_dir: Path | None = None, dry_run: bool = False
) -> dict[str, Any] | None:
    """
    Move recycle-bin entry ``entry_id`` (see :func:`list_recycled_rules`) back into
    ``auto_rules.json`` under its original category, and remove it from the bin.

    Intended for after hand-editing the entry's ``rule.match`` in the recycle-bin JSON file to
    fix whatever caused the conflict. Returns the restored entry, or ``None`` if ``entry_id``
    does not exist.
    """
    path = recycle_bin_path(rules_dir)
    entries = _read_json_array(path)
    if entry_id < 0 or entry_id >= len(entries):
        return None
    entry = entries[entry_id]
    if not isinstance(entry, dict) or not isinstance(entry.get("rule"), dict):
        return None
    if not dry_run:
        remaining = entries[:entry_id] + entries[entry_id + 1 :]
        _write_json_array(path, remaining)
        append_rule_to_auto_rules(entry.get("category") or "screening_yes_no", entry["rule"], rules_dir=rules_dir)
    return entry


def _dedupe_repeated_question(question: str) -> str:
    q = question.strip()
    if not q:
        return q
    if "?" in q:
        parts = [p.strip() for p in q.split("?") if p.strip()]
        if len(parts) >= 2 and parts[0] == parts[1]:
            return parts[0] + "?"
    half = len(q) // 2
    if half > 0 and q[:half].strip() == q[half:].strip():
        return q[:half].strip()
    return q


def parse_saved_questions_text(text: str) -> tuple[list[ExtensionQuestion], list[str]]:
    """Parse extension question blocks. Returns ``(questions, invalid_blocks)``."""
    entries: list[ExtensionQuestion] = []
    invalid: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        question: str | None = None
        answer: str | None = None
        url: str | None = None
        company_title: str | None = None
        for line in lines:
            if line.startswith("Q:"):
                question = _dedupe_repeated_question(line[2:].strip())
            elif line.startswith("A:"):
                answer = line[2:].strip()
            elif line.upper().startswith("URL:"):
                url = line.split(":", 1)[1].strip()
            elif company_title is None:
                company_title = line
        if question and answer is not None:
            entries.append(
                ExtensionQuestion(
                    question=question,
                    answer=answer,
                    url=url,
                    company_title=company_title,
                )
            )
        else:
            invalid.append(block)
    return _unique_questions(entries), invalid


def _unique_questions(entries: list[ExtensionQuestion]) -> list[ExtensionQuestion]:
    out: list[ExtensionQuestion] = []
    seen: set[str] = set()
    for entry in entries:
        key = FormFillRulesEngine.normalize_label(entry.question)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def parse_saved_questions_file(path: Path) -> tuple[list[ExtensionQuestion], list[str]]:
    if not path.is_file():
        return [], []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return parse_saved_questions_text(text)


def resolve_questions_file_path(downloads_dir: Path) -> Path | None:
    for name in QUESTION_EXPORT_ALIASES:
        candidate = downloads_dir / name
        if candidate.is_file():
            return candidate
    return downloads_dir / QUESTION_EXPORT_ALIASES[0]


def _load_resume_for_rule_resolution() -> dict[str, Any]:
    cache = Path("data/resume_profile.json")
    if not cache.is_file():
        return {}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_answer(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _answers_match(rule_answer: str | None, extension_answer: str) -> bool:
    if rule_answer is None:
        return False
    return _normalize_answer(rule_answer) == _normalize_answer(extension_answer)


def resolve_rule_answer(
    ref: RuleRef,
    question: str,
    *,
    engine: FormFillRulesEngine,
    resume: dict[str, Any],
) -> str | None:
    candidates = resolve_rule_answer_candidates(ref, question, engine=engine, resume=resume)
    return candidates[0] if candidates else None


def resolve_rule_answer_candidates(
    ref: RuleRef,
    question: str,
    *,
    engine: FormFillRulesEngine,
    resume: dict[str, Any],
) -> list[str]:
    """Full priority-ordered candidate list for the matched rule (used by conflict combining)."""
    if ref.category == "screening_yes_no":
        return engine.screening_yes_no_candidates(question)
    if ref.category == "text_inputs":
        return engine.text_input_fill_candidates(question, resume)
    if ref.category == "textareas":
        # Free text — no well-defined "try next" fallback, so at most one candidate.
        v = engine.answer_textarea(question, cover_letter="")
        return [v] if v else []
    if ref.category == "selects":
        return engine.answer_select_candidates(question)
    return []


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = (it or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def combined_rule_for_conflict(
    conflict: QuestionConflict,
    *,
    engine: FormFillRulesEngine,
    resume: dict[str, Any],
    prioritize_existing: bool,
) -> tuple[str, dict[str, Any]] | None:
    """
    Build a new rule combining the existing rule's candidate answer(s) with the extension's answer, in
    priority order. Returns ``None`` when this rule category has no well-defined combined form (free-text
    ``textareas``) or the extension answer is empty.
    """
    category = conflict.rule_ref.category
    if category == "textareas":
        return None
    question = conflict.question.question
    extension_answer = (conflict.question.answer or "").strip()
    if not extension_answer:
        return None
    existing = resolve_rule_answer_candidates(conflict.rule_ref, question, engine=engine, resume=resume)
    if prioritize_existing:
        combined = _dedupe_preserve_order(existing + [extension_answer])
    else:
        combined = _dedupe_preserve_order([extension_answer] + existing)
    if not combined:
        return None

    match = question_to_match(question)
    slug = _slug_from_question(question)
    priority_note = "existing priority" if prioritize_existing else "extension priority"
    comment = f"Auto-added from browser extension (combined with prior rule, {priority_note})"
    rule_id = f"extension_auto_{slug}"

    if category == "screening_yes_no":
        return category, {
            "id": rule_id,
            "comment": comment,
            "match": match,
            "answer": combined,
        }
    if category in ("text_inputs", "selects"):
        return category, {
            "id": rule_id,
            "comment": comment,
            "match": match,
            "result": {"type": "literal_fallbacks", "values": combined},
        }
    return None


def question_to_match(question: str) -> dict[str, Any]:
    """Extension-derived rules match the full normalized question text only (not substrings)."""
    return {"exact": normalize_label_for_exact(question)}


def upgrade_extension_auto_rule_match(match: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """
    Convert legacy extension ``all_substrings`` / ``regex`` matchers to ``exact``.

    Returns ``(new_match, changed)``.
    """
    if match.get("exact") is not None:
        return match, False
    subs = match.get("all_substrings")
    if isinstance(subs, list) and len(subs) == 1:
        s = str(subs[0] or "").strip()
        if s:
            return {"exact": normalize_label_for_exact(s)}, True
    rx = match.get("regex")
    if isinstance(rx, str) and rx.strip() and len(match) == 1:
        try:
            literal = codecs.decode(rx, "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            return match, False
        if literal.strip():
            return {"exact": normalize_label_for_exact(literal)}, True
    return match, False


def migrate_extension_auto_rules_to_exact(
    *,
    rules_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    """
    Rewrite ``extension_auto_*`` rules in ``auto_rules.json`` to use ``match.exact``.

    Returns the number of rules updated.
    """
    path = auto_rules_path(rules_dir)
    data = _read_json_object(path)
    if not data:
        return 0
    changed = 0
    for category in RULE_CATEGORIES:
        rules = data.get(category)
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if not str(rule.get("id", "")).startswith("extension_auto_"):
                continue
            old = rule.get("match") or {}
            if not isinstance(old, dict):
                continue
            new, did = upgrade_extension_auto_rule_match(old)
            if did:
                rule["match"] = new
                changed += 1
    if changed and not dry_run:
        _write_json_object(path, data)
        log.info("Migrated %d extension auto rule(s) to exact match in %s", changed, path)
    return changed


def _slug_from_question(question: str) -> str:
    base = FormFillRulesEngine.normalize_label(question)[:60]
    slug = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return slug or "question"


def new_rule_for_question(question: str, answer: str) -> tuple[str, dict[str, Any]]:
    """Return ``(category, rule_dict)`` for a new extension-derived rule."""
    ans = answer.strip()
    match = question_to_match(question)
    slug = _slug_from_question(question)
    if ans.lower() in ("yes", "no"):
        return (
            "screening_yes_no",
            {
                "id": f"extension_auto_{slug}",
                "comment": "Auto-added from browser extension",
                "match": match,
                "answer": "Yes" if ans.lower() == "yes" else "No",
            },
        )
    return (
        "text_inputs",
        {
            "id": f"extension_auto_{slug}",
            "comment": "Auto-added from browser extension",
            "match": match,
            "result": {"type": "literal", "value": ans},
        },
    )


def auto_rules_path(rules_dir: Path | None = None) -> Path:
    base = Path(rules_dir or FORM_FILL_RULES_DIR)
    return base / AUTO_RULES_FILENAME


def remove_empty_auto_rules(*, rules_dir: Path | None = None, dry_run: bool = False) -> int:
    """
    Delete blank literal / empty-answer rules from ``auto_rules.json``.

    Returns the number of rules removed.
    """
    path = auto_rules_path(rules_dir)
    data = _read_json_object(path)
    removed = 0
    for category in RULE_CATEGORIES:
        rules = data.get(category)
        if not isinstance(rules, list):
            continue
        kept: list[Any] = []
        for rule in rules:
            if isinstance(rule, dict) and _rule_is_blank(rule):
                removed += 1
                continue
            kept.append(rule)
        data[category] = kept
    if removed and not dry_run:
        _write_json_object(path, data)
        log.info("Removed %d empty rule(s) from %s", removed, path)
    return removed


def append_rule_to_auto_rules(
    category: str,
    rule: dict[str, Any],
    *,
    rules_dir: Path | None = None,
    dry_run: bool = False,
) -> Path:
    path = auto_rules_path(rules_dir)
    data = _read_json_object(path)
    rules = data.setdefault(category, [])
    if not isinstance(rules, list):
        rules = []
        data[category] = rules
    rule_id = rule.get("id")
    if rule_id and any(isinstance(r, dict) and r.get("id") == rule_id for r in rules):
        return path
    rules.append(rule)
    if not dry_run:
        _write_json_object(path, data)
    return path


def delete_rule(
    ref: RuleRef,
    *,
    reason: str = "deleted",
    context: dict[str, Any] | None = None,
    rules_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Move ``ref``'s rule to the recycle bin (see :func:`_recycle_rule`), then remove it."""
    data = _read_json_object(ref.file)
    rules = data.get(ref.category)
    if not isinstance(rules, list) or ref.index >= len(rules):
        return
    if not dry_run:
        _recycle_rule(ref, reason=reason, context=context, rules_dir=rules_dir)
    del rules[ref.index]
    data[ref.category] = rules
    if not dry_run:
        _write_json_object(ref.file, data)


@dataclass
class QuestionProcessResult:
    matched: int = 0
    added: int = 0
    conflicts: list[QuestionConflict] | None = None
    blank_new: list[ExtensionQuestion] | None = None
    blank_saved: int = 0
    blank_skipped: int = 0
    resolved_replaced: int = 0
    resolved_kept: int = 0
    resolved_combined: int = 0
    invalid_blocks: list[str] | None = None
    empty_rules_removed: int = 0


def classify_extension_questions(
    questions: list[ExtensionQuestion],
    *,
    rules_dir: Path | None = None,
    resume: dict[str, Any] | None = None,
) -> tuple[
    list[ExtensionQuestion],
    list[QuestionConflict],
    list[tuple[ExtensionQuestion, str, dict[str, Any]]],
    list[tuple[ExtensionQuestion, str, dict[str, Any]]],
]:
    """
    Return ``(skipped_same_answer, conflicts, new_rules, blank_new_rules)``.

    ``new_rules`` / ``blank_new_rules`` are ``(question, category, rule_dict)`` tuples.
    Blank extension answers with no matching rule go in ``blank_new_rules`` (need confirmation).
    """
    index = RuleIndex(rules_dir)
    engine = FormFillRulesEngine(rules_path=rules_dir or FORM_FILL_RULES_DIR, apply_source="linkedin")
    resume = resume if resume is not None else _load_resume_for_rule_resolution()

    skipped: list[ExtensionQuestion] = []
    conflicts: list[QuestionConflict] = []
    new_rules: list[tuple[ExtensionQuestion, str, dict[str, Any]]] = []
    blank_new_rules: list[tuple[ExtensionQuestion, str, dict[str, Any]]] = []

    for item in questions:
        ref = index.find_matching_rule(item.question)
        if ref is None:
            category, rule = new_rule_for_question(item.question, item.answer)
            if _is_blank_answer(item.answer):
                blank_new_rules.append((item, category, rule))
            else:
                new_rules.append((item, category, rule))
            continue
        rule_answer = resolve_rule_answer(ref, item.question, engine=engine, resume=resume)
        # Empty stored literals resolve to None; treat blank-vs-blank as a match.
        if rule_answer is None and _rule_is_blank(ref.rule) and _is_blank_answer(item.answer):
            skipped.append(item)
            continue
        if _answers_match(rule_answer, item.answer):
            skipped.append(item)
            continue
        if rule_answer is None:
            if _rule_is_blank(ref.rule):
                display = "(empty rule)"
            else:
                display = "(non-literal rule — could not compare)"
            conflicts.append(
                QuestionConflict(
                    question=item,
                    rule_ref=ref,
                    rule_answer=display,
                )
            )
            continue
        conflicts.append(
            QuestionConflict(
                question=item,
                rule_ref=ref,
                rule_answer=rule_answer,
            )
        )
    return skipped, conflicts, new_rules, blank_new_rules


def resolve_conflicts_interactively(
    conflicts: list[QuestionConflict],
    *,
    rules_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Prompt the user for each conflict. Returns ``(replaced_count, kept_count, combined_count)``."""
    replaced = 0
    kept = 0
    combined_total = 0
    index = RuleIndex(rules_dir)
    engine = FormFillRulesEngine(rules_path=rules_dir or FORM_FILL_RULES_DIR, apply_source="linkedin")
    resume = _load_resume_for_rule_resolution()

    for conflict in conflicts:
        q = conflict.question
        can_combine = conflict.rule_ref.category != "textareas"
        # Attached to any recycle-bin entry created below, so a recycled rule shows why it
        # conflicted (the question/answers involved) — useful context for refining it by hand.
        recycle_context = {
            "question": q.question,
            "extension_answer": q.answer,
            "rule_answer": conflict.rule_answer,
            "job": q.company_title,
            "url": q.url,
        }
        print()
        print("Conflict — existing rule disagrees with extension answer")
        print(f"  Question: {q.question}")
        if q.company_title:
            print(f"  Job: {q.company_title}")
        if q.url:
            print(f"  URL: {q.url}")
        if _is_blank_answer(q.answer):
            print("  Extension answer: (blank)")
        else:
            print(f"  Extension answer: {q.answer}")
        print(
            f"  Existing rule: {conflict.rule_ref.file.name} "
            f"[{conflict.rule_ref.rule.get('id') or conflict.rule_ref.index}]"
        )
        print(f"  Rule answer: {conflict.rule_answer}")
        print("  [1] Keep existing rule (skip)")
        if _is_blank_answer(q.answer):
            print("  [2] Replace with blank extension answer (delete old rule, save empty rule)")
        else:
            print("  [2] Replace with extension answer (delete old rule, add to auto_rules.json)")
        if can_combine:
            print("  [3] Keep both — try the existing answer first, extension answer as fallback")
            print("  [4] Keep both — try the extension answer first, existing answer as fallback")
        else:
            print("  (Free-text answers can't be combined — choose 1 or 2 for this one.)")
        valid = "1/2/3/4" if can_combine else "1/2"
        while True:
            choice = input(f"  Choice [{valid}]: ").strip().lower()
            if choice in ("1", "keep", "k", ""):
                kept += 1
                print("  → Keeping existing rule.")
                break
            if choice in ("2", "replace", "r", "extension"):
                if not dry_run:
                    delete_rule(
                        conflict.rule_ref,
                        reason="conflict_replaced",
                        context=recycle_context,
                        rules_dir=rules_dir,
                    )
                    index.reload()
                    category, rule = new_rule_for_question(q.question, q.answer)
                    append_rule_to_auto_rules(category, rule, rules_dir=rules_dir)
                    index.reload()
                replaced += 1
                print("  → Replaced with extension answer in auto_rules.json.")
                break
            if can_combine and choice in ("3", "existing", "combine-existing"):
                result = combined_rule_for_conflict(
                    conflict, engine=engine, resume=resume, prioritize_existing=True
                )
                if result is None:
                    print("  Could not build a combined rule for this question — try 1 or 2.")
                    continue
                category, rule = result
                if not dry_run:
                    delete_rule(
                        conflict.rule_ref,
                        reason="conflict_combined_existing_priority",
                        context=recycle_context,
                        rules_dir=rules_dir,
                    )
                    index.reload()
                    append_rule_to_auto_rules(category, rule, rules_dir=rules_dir)
                    index.reload()
                combined_total += 1
                print(f"  → Combined (existing priority): {_rule_answer_preview(rule)}")
                break
            if can_combine and choice in ("4", "new", "combine-new"):
                result = combined_rule_for_conflict(
                    conflict, engine=engine, resume=resume, prioritize_existing=False
                )
                if result is None:
                    print("  Could not build a combined rule for this question — try 1 or 2.")
                    continue
                category, rule = result
                if not dry_run:
                    delete_rule(
                        conflict.rule_ref,
                        reason="conflict_combined_extension_priority",
                        context=recycle_context,
                        rules_dir=rules_dir,
                    )
                    index.reload()
                    append_rule_to_auto_rules(category, rule, rules_dir=rules_dir)
                    index.reload()
                combined_total += 1
                print(f"  → Combined (extension priority): {_rule_answer_preview(rule)}")
                break
            print(f"  Enter {valid.replace('/', ', ')}.")
    return replaced, kept, combined_total


def _rule_answer_preview(rule: dict[str, Any]) -> list[str]:
    if "answer" in rule:
        return rule["answer"] if isinstance(rule["answer"], list) else [rule["answer"]]
    result = rule.get("result") or {}
    if "value" in result and not result.get("values"):
        return [str(result.get("value") or "")]
    return list(result.get("values") or [])


def confirm_blank_rules_interactively(
    blank_new_rules: list[tuple[ExtensionQuestion, str, dict[str, Any]]],
    *,
    rules_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Prompt before saving extension answers that are blank.

    Returns ``(saved_count, skipped_count)``.
    """
    saved = 0
    skipped = 0
    for item, category, rule in blank_new_rules:
        print()
        print("Blank answer from extension — save an empty rule?")
        print(f"  Question: {item.question}")
        if item.company_title:
            print(f"  Job: {item.company_title}")
        if item.url:
            print(f"  URL: {item.url}")
        print("  Extension answer: (blank)")
        print("  [1] Skip (do not save)")
        print(f"  [2] Save blank rule to {AUTO_RULES_FILENAME}")
        while True:
            choice = input("  Choice [1/2]: ").strip().lower()
            if choice in ("1", "skip", "s", ""):
                skipped += 1
                print("  → Skipped (no rule saved).")
                break
            if choice in ("2", "save", "yes", "y"):
                if not dry_run:
                    append_rule_to_auto_rules(category, rule, rules_dir=rules_dir)
                saved += 1
                print(f"  → Saved blank rule to {AUTO_RULES_FILENAME}.")
                break
            print("  Enter 1 or 2.")
    return saved, skipped


def process_extension_questions(
    path: Path,
    *,
    rules_dir: Path | None = None,
    dry_run: bool = False,
    interactive: bool = True,
) -> QuestionProcessResult:
    questions, invalid = parse_saved_questions_file(path)
    result = QuestionProcessResult(invalid_blocks=invalid)
    if not questions:
        # Still purge empty auto rules even when the export has no parseable Q/A.
        result.empty_rules_removed = remove_empty_auto_rules(rules_dir=rules_dir, dry_run=dry_run)
        return result

    result.empty_rules_removed = remove_empty_auto_rules(rules_dir=rules_dir, dry_run=dry_run)

    skipped, conflicts, new_rules, blank_new_rules = classify_extension_questions(
        questions, rules_dir=rules_dir
    )

    result.matched = len(skipped)
    result.conflicts = conflicts
    result.blank_new = [item for item, _cat, _rule in blank_new_rules]

    for _item, category, rule in new_rules:
        append_rule_to_auto_rules(category, rule, rules_dir=rules_dir, dry_run=dry_run)
        result.added += 1

    if blank_new_rules and interactive and not dry_run:
        saved, skipped_blank = confirm_blank_rules_interactively(
            blank_new_rules, rules_dir=rules_dir, dry_run=dry_run
        )
        result.blank_saved = saved
        result.blank_skipped = skipped_blank
        result.added += saved
    elif blank_new_rules:
        # Dry-run / --no-interactive: never auto-save blanks.
        result.blank_skipped = len(blank_new_rules)

    if conflicts and interactive and not dry_run:
        replaced, kept, combined_total = resolve_conflicts_interactively(
            conflicts, rules_dir=rules_dir, dry_run=dry_run
        )
        result.resolved_replaced = replaced
        result.resolved_kept = kept
        result.resolved_combined = combined_total
    elif conflicts and (dry_run or not interactive):
        result.resolved_kept = len(conflicts)

    return result


def print_questions_summary(path: Path, result: QuestionProcessResult, *, dry_run: bool) -> None:
    print("=== Extension questions import ===")
    print(f"Path: {path.resolve()}")
    if result.invalid_blocks:
        print(f"Skipped {len(result.invalid_blocks)} unparseable block(s):")
        for blk in result.invalid_blocks:
            preview = blk.strip().replace("\n", " | ")[:200]
            print(f"  • {preview}")
    if result.empty_rules_removed:
        print(f"Removed {result.empty_rules_removed} empty rule(s) from {AUTO_RULES_FILENAME}")
    print(f"Already matched by existing rules (same answer): {result.matched}")
    print(f"New rules added to {AUTO_RULES_FILENAME}: {result.added}")
    if result.blank_new:
        print(
            f"Blank extension answers: {len(result.blank_new)} "
            f"(saved: {result.blank_saved}, skipped: {result.blank_skipped})"
        )
        pending_blank = len(result.blank_new) - result.blank_saved - result.blank_skipped
        if pending_blank > 0 or (result.blank_skipped and dry_run):
            print("  Blank questions:")
            for item in result.blank_new:
                print(f"    • {item.question[:80]}")
    if dry_run:
        print("Dry run — no rule files written.")
    if result.conflicts:
        pending = (
            len(result.conflicts)
            - result.resolved_replaced
            - result.resolved_kept
            - result.resolved_combined
        )
        print(f"Conflicts: {len(result.conflicts)}")
        if result.resolved_replaced or result.resolved_kept or result.resolved_combined:
            print(
                f"  Resolved — replaced: {result.resolved_replaced}, "
                f"kept: {result.resolved_kept}, combined: {result.resolved_combined}"
            )
        if pending > 0:
            print("  Unresolved conflicts:")
            for conflict in result.conflicts:
                print(f"    • {conflict.question.question[:80]}…")
                print(f"      extension={conflict.question.answer!r} rule={conflict.rule_answer!r}")
    print()
