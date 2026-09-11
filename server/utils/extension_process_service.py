"""
HTTP-driven equivalent of ``process_extension.py``'s import flow, for ``extension_server.py``.

Jobs import has no interactive step and always completes in one request
(:func:`process_extension_request`). Rule-conflict resolution does — instead of blocking on
``input()`` like the CLI, a pending conflict list is stored here (keyed by a server-minted id,
never the caller-supplied one — see :func:`process_extension_request`) and applied later via
:func:`resolve_conflicts`, once the browser extension's popup has collected a choice for each one.

Two independent short-held sync transactions, not one held across the wait for the extension to
respond (which could be arbitrarily long — a backgrounded tab, a closed popup): phase 1 downloads,
imports jobs, auto-adds non-conflicting rules, and uploads whatever was decided; phase 2 downloads
fresh, applies the resolved conflicts, and uploads again. See the approved plan for the full
rationale.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import process_extension

from .extension_rules import (
    ExtensionQuestion,
    QuestionConflict,
    RuleIndex,
    RuleRef,
    _is_blank_answer,
    _load_resume_for_rule_resolution,
    append_rule_to_auto_rules,
    apply_blank_rule_resolution,
    apply_conflict_resolution,
    apply_reprioritize_resolution,
    classify_extension_questions,
    parse_saved_questions_text,
    remove_empty_auto_rules,
)
from .form_fill_rules import FormFillRulesEngine
from .output_paths import FORM_FILL_RULES_DIR
from .s3_log_sync import PendingChangeTracker
from .s3_outputs import sync_download_output_coordinated, sync_upload_output_coordinated

_PENDING_TTL_SECONDS = 600  # 10 minutes


@dataclass
class _PendingConflict:
    conflict_id: str
    kind: str  # "rule_conflict" | "blank_new_rule" | "reprioritize"
    conflict: QuestionConflict | None = None  # "rule_conflict" and "reprioritize" both use this
    blank: tuple[ExtensionQuestion, str, dict[str, Any]] | None = None


@dataclass
class _PendingRequest:
    server_request_id: str
    client_request_id: str
    created_at: float
    phase1_summary: dict[str, Any]
    pending: list[_PendingConflict]
    rules_dir: Path | None = None
    dry_run: bool = False


_PENDING: dict[str, _PendingRequest] = {}
_PENDING_LOCK = threading.Lock()


def _sweep_expired() -> None:
    now = time.time()
    with _PENDING_LOCK:
        expired = [k for k, v in _PENDING.items() if now - v.created_at > _PENDING_TTL_SECONDS]
        for k in expired:
            del _PENDING[k]


def _build_pending_items(
    conflicts: list[QuestionConflict],
    blank_new_rules: list[tuple[ExtensionQuestion, str, dict[str, Any]]],
    reprioritize: list[QuestionConflict],
) -> list[_PendingConflict]:
    items: list[_PendingConflict] = []
    i = 0
    for c in conflicts:
        items.append(_PendingConflict(conflict_id=str(i), kind="rule_conflict", conflict=c))
        i += 1
    for b in blank_new_rules:
        items.append(_PendingConflict(conflict_id=str(i), kind="blank_new_rule", blank=b))
        i += 1
    for r in reprioritize:
        items.append(_PendingConflict(conflict_id=str(i), kind="reprioritize", conflict=r))
        i += 1
    return items


def _conflict_options(can_combine: bool) -> list[dict[str, Any]]:
    options = [
        {"choice": 1, "label": "Keep existing rule"},
        {"choice": 2, "label": "Replace with extension answer"},
    ]
    if can_combine:
        options += [
            {"choice": 3, "label": "Keep both — existing first, extension as fallback"},
            {"choice": 4, "label": "Keep both — extension first, existing as fallback"},
        ]
    return options


def _item_to_json(item: _PendingConflict) -> dict[str, Any]:
    if item.kind == "rule_conflict":
        c = item.conflict
        assert c is not None
        can_combine = c.rule_ref.category != "textareas"
        label2 = (
            "Replace with blank extension answer" if _is_blank_answer(c.question.answer)
            else "Replace with extension answer"
        )
        options = _conflict_options(can_combine)
        options[1]["label"] = label2
        return {
            "conflict_id": item.conflict_id,
            "kind": "rule_conflict",
            "question": c.question.question,
            "job": c.question.company_title,
            "url": c.question.url,
            "extension_answer": c.question.answer,
            "existing_rule_answer": c.rule_answer,
            "existing_rule_file": c.rule_ref.file.name,
            "existing_rule_id": c.rule_ref.rule.get("id"),
            "options": options,
        }
    if item.kind == "reprioritize":
        c = item.conflict
        assert c is not None
        return {
            "conflict_id": item.conflict_id,
            "kind": "reprioritize",
            "question": c.question.question,
            "job": c.question.company_title,
            "url": c.question.url,
            "extension_answer": c.question.answer,
            "existing_rule_answer": c.rule_answer,
            "existing_rule_file": c.rule_ref.file.name,
            "existing_rule_id": c.rule_ref.rule.get("id"),
            "options": [
                {"choice": 1, "label": "Keep current order"},
                {"choice": 2, "label": "Move extension answer to top priority"},
            ],
        }
    assert item.blank is not None
    q, _category, _rule = item.blank
    return {
        "conflict_id": item.conflict_id,
        "kind": "blank_new_rule",
        "question": q.question,
        "job": q.company_title,
        "url": q.url,
        "options": [{"choice": 1, "label": "Skip"}, {"choice": 2, "label": "Save as empty rule"}],
    }


def process_extension_request(
    *,
    request_id: str,
    saved_jobs_text: str = "",
    saved_questions_text: str = "",
    dry_run: bool = False,
    rules_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Phase 1: import jobs (always completes in this call — no interactive step on that side),
    classify questions against existing rules, auto-add non-conflicting new rules, and upload
    everything decided so far. Returns an ``extension_processed`` payload immediately if nothing
    needs a decision, otherwise stores the pending conflicts/blank-new-rules under a freshly-minted
    server id and returns a ``process_conflicts`` payload.
    """
    lock_token = sync_download_output_coordinated()
    cover_letter_changes = PendingChangeTracker()
    form_fill_rule_changes = PendingChangeTracker()

    jobs, invalid_job_lines = process_extension.parse_saved_jobs_text(saved_jobs_text or "")
    jobs_added = jobs_skipped = 0
    cover_letters_deleted = 0
    if jobs:
        jobs_added, jobs_skipped, _skipped_jobs = process_extension.import_saved_jobs_to_assisted(
            jobs, dry_run=dry_run
        )
        cover_letters_deleted = process_extension.delete_cover_letters_for_applied_jobs(
            jobs, dry_run=dry_run, tracker=cover_letter_changes
        )

    questions, invalid_question_blocks = parse_saved_questions_text(saved_questions_text or "")
    empty_rules_removed = remove_empty_auto_rules(
        rules_dir=rules_dir, dry_run=dry_run, tracker=form_fill_rule_changes
    )

    rules_added = 0
    conflicts: list[QuestionConflict] = []
    blank_new_rules: list[tuple[ExtensionQuestion, str, dict[str, Any]]] = []
    reprioritize: list[QuestionConflict] = []
    if questions:
        _skipped, conflicts, new_rules, blank_new_rules, reprioritize = classify_extension_questions(
            questions, rules_dir=rules_dir
        )
        for _item, category, rule in new_rules:
            append_rule_to_auto_rules(
                category, rule, rules_dir=rules_dir, dry_run=dry_run, tracker=form_fill_rule_changes
            )
            rules_added += 1

    summary: dict[str, Any] = {
        "jobs_added": jobs_added,
        "jobs_skipped": jobs_skipped,
        "cover_letters_deleted": cover_letters_deleted,
        "rules_added": rules_added,
        "rules_replaced": 0,
        "rules_kept": 0,
        "rules_combined": 0,
        "blank_saved": 0,
        "blank_skipped": 0,
        "rules_reprioritized": 0,
        "rules_kept_priority": 0,
        "empty_rules_removed": empty_rules_removed,
        "invalid_job_lines": invalid_job_lines,
        "invalid_question_blocks": invalid_question_blocks,
    }

    if lock_token is not None:
        sync_upload_output_coordinated(
            lock_token=lock_token,
            cover_letter_changes=cover_letter_changes,
            form_fill_rule_changes=form_fill_rule_changes,
        )

    pending_items = _build_pending_items(conflicts, blank_new_rules, reprioritize)
    if not pending_items:
        return {"type": "extension_processed", "request_id": request_id, "summary": summary}

    _sweep_expired()
    server_request_id = secrets.token_urlsafe(16)
    with _PENDING_LOCK:
        _PENDING[server_request_id] = _PendingRequest(
            server_request_id=server_request_id,
            client_request_id=request_id,
            created_at=time.time(),
            phase1_summary=summary,
            pending=pending_items,
            rules_dir=rules_dir,
            dry_run=dry_run,
        )

    return {
        "type": "process_conflicts",
        "request_id": request_id,
        "server_request_id": server_request_id,
        "conflicts": [_item_to_json(item) for item in pending_items],
    }


def _refind_rule_ref(fresh_index: RuleIndex, old_ref: RuleRef) -> RuleRef | None:
    """
    Re-locate ``old_ref`` in a freshly-rebuilt index, since arbitrary time may have passed since
    phase 1. Matches by rule ``id`` when the original rule had one (the normal case — every
    extension-auto and hand-curated rule seen in this codebase carries one); otherwise falls back
    to same-position-with-identical-content, treating anything else as "changed since reported".
    """
    bucket: list[RuleRef] = getattr(fresh_index, old_ref.category, [])
    old_id = old_ref.rule.get("id")
    if old_id:
        for ref in bucket:
            if ref.rule.get("id") == old_id:
                return ref
        return None
    if old_ref.index < len(bucket) and bucket[old_ref.index].rule == old_ref.rule:
        return bucket[old_ref.index]
    return None


def resolve_conflicts(
    *, request_id: str, server_request_id: str, resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Phase 2: apply a batch of resolution choices for a pending request. Sweeps expired entries
    first, then looks up ``server_request_id`` and verifies ``request_id`` matches what was stored
    for it — either failing that returns ``conflict_resolution_timeout`` and commits nothing.
    """
    _sweep_expired()
    with _PENDING_LOCK:
        pending_req = _PENDING.get(server_request_id)
        if pending_req is not None and pending_req.client_request_id == request_id:
            del _PENDING[server_request_id]
        else:
            pending_req = None

    if pending_req is None:
        return {
            "type": "conflict_resolution_timeout",
            "request_id": request_id,
            "server_request_id": server_request_id,
        }

    rules_dir = pending_req.rules_dir
    dry_run = pending_req.dry_run
    choice_by_id = {str(r.get("conflict_id")): r.get("choice") for r in resolutions}

    lock_token = sync_download_output_coordinated()
    form_fill_rule_changes = PendingChangeTracker()
    engine = FormFillRulesEngine(rules_path=rules_dir or FORM_FILL_RULES_DIR, apply_source="linkedin")
    resume = _load_resume_for_rule_resolution()
    fresh_index = RuleIndex(rules_dir)

    replaced = kept = combined = blank_saved = blank_skipped = 0
    reprioritized = kept_priority = 0
    unresolved: list[dict[str, Any]] = []

    for item in pending_req.pending:
        choice = choice_by_id.get(item.conflict_id)
        if not isinstance(choice, int):
            unresolved.append({"conflict_id": item.conflict_id, "reason": "no resolution supplied"})
            continue
        if item.kind in ("rule_conflict", "reprioritize"):
            assert item.conflict is not None
            fresh_ref = _refind_rule_ref(fresh_index, item.conflict.rule_ref)
            if fresh_ref is None:
                unresolved.append({
                    "conflict_id": item.conflict_id,
                    "reason": "underlying rule changed or was removed since this conflict was reported",
                })
                continue
            conflict = replace(item.conflict, rule_ref=fresh_ref)
            if item.kind == "rule_conflict":
                outcome = apply_conflict_resolution(
                    conflict, choice, engine=engine, resume=resume,
                    rules_dir=rules_dir, dry_run=dry_run, tracker=form_fill_rule_changes,
                )
                if outcome is None:
                    unresolved.append({"conflict_id": item.conflict_id, "reason": "invalid choice for this conflict"})
                    continue
                kind, _rule = outcome
                if kind == "kept":
                    kept += 1
                elif kind == "replaced":
                    replaced += 1
                else:
                    combined += 1
            else:
                outcome = apply_reprioritize_resolution(
                    conflict, choice, engine=engine, resume=resume,
                    rules_dir=rules_dir, dry_run=dry_run, tracker=form_fill_rule_changes,
                )
                if outcome == "reprioritized":
                    reprioritized += 1
                else:
                    kept_priority += 1
        else:
            assert item.blank is not None
            _q, category, rule = item.blank
            outcome = apply_blank_rule_resolution(
                category, rule, choice, rules_dir=rules_dir, dry_run=dry_run, tracker=form_fill_rule_changes
            )
            if outcome == "saved":
                blank_saved += 1
            else:
                blank_skipped += 1

    if lock_token is not None:
        sync_upload_output_coordinated(lock_token=lock_token, form_fill_rule_changes=form_fill_rule_changes)

    summary = dict(pending_req.phase1_summary)
    summary["rules_replaced"] = replaced
    summary["rules_kept"] = kept
    summary["rules_combined"] = combined
    summary["blank_saved"] = blank_saved
    summary["blank_skipped"] = blank_skipped
    summary["rules_reprioritized"] = reprioritized
    summary["rules_kept_priority"] = kept_priority

    return {
        "type": "extension_processed",
        "request_id": request_id,
        "summary": summary,
        "unresolved": unresolved,
    }
