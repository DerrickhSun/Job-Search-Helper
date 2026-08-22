"""
Operation-log-based S3 sync for ``output/coverletters/`` and ``output/form_fill_rules/``.

See the approved plan at the top of this feature's implementation for full rationale. Short
version: unlike the tracking CSVs (which merge safely via ``union_sheet_rows``), these two
resources have no merge step, so a plain "list S3, download what's missing" sync can't tell
"never synced" apart from "already synced and then deleted" — a file correctly pruned on one
device can resurface later just because another device's S3 copy looks fresh. This module
replaces that with an append-only log of ``add``/``delete`` operations that any device can
*replay* to compute the authoritative current file set, plus a short-held per-resource lock
(reusing :func:`utils.s3_outputs.acquire_sync_lock`) around the moments the log itself changes.

Each resource gets its own log, sequence counter, and lock — entirely independent of the other
and of the general ``sync.lock``-protected scope (CSVs, ``consulting_companies.json``, etc.),
which is untouched by this module.

Log entry shape (one JSON object per log, not a bare array — see :func:`_empty_log_doc`)::

    {
      "version": 1,
      "next_sequence": 9,
      "entries": [
        {
          "sequence": 6, "type": "add", "status": "complete",
          "owner": "HOST:1234", "created_at": ..., "updated_at": ...,
          "files": [{"path": "linkedin/linkedin_Acme_SWE_12345.docx", "before_etag": null}]
        },
        {
          "sequence": 7, "type": "delete", "status": "complete", ...,
          "files": [{"path": "linkedin/linkedin_OldCo_SWE_999.docx"}]
        }
      ]
    }

``type`` is a property of the whole entry (never mixed): ``"add"`` entries should be downloaded
by everyone; ``"delete"`` entries should be removed by everyone, wherever they still exist
locally. ``before_etag`` (add entries only) is the target key's S3 ETag at the moment the entry
was written as in-progress (``null`` if new) — see :func:`_reap_stale_entries` for why this is
needed for whole-file-rewrite resources like ``form_fill_rules``, where plain existence can't
tell "this crashed entry's rewrite landed" apart from "the pre-crash version is still there".

Only ``local_log.json`` is ever written to disk and kept around; the remote log is read/written
directly as S3 object bytes (get_object/put_object) and never materialized as a separate local
file — simpler than shuttling an actual transient file to disk and back, and "never exists
locally for long" is satisfied at least as well by "never exists locally as its own file at all".
"""

from __future__ import annotations

import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .output_paths import cover_letter_mode_names
from .s3_outputs import (
    _SYNC_LOCK_STALE_SECONDS,
    acquire_sync_lock,
    release_sync_lock,
    resolve_output_dir,
    s3_key_for_file,
    s3_output_bucket,
    s3_output_sync_enabled,
)

log = logging.getLogger(__name__)

RESOURCE_COVERLETTERS = "coverletters"
RESOURCE_FORM_FILL_RULES = "form_fill_rules"

# Cap on how many entries a log keeps — otherwise it grows forever. Trimmed to the most recent
# this-many entries (by sequence) every time the log is uploaded (see _trim_log_doc). A device
# whose last-applied sequence falls before the oldest surviving entry can no longer replay via
# simulate() (the entries it needs are gone) and falls back to a full resync instead (see
# _log_has_gap / _resolve_download_delete_sets) — not "more than this many entries have passed
# since I last synced" (fine, simulate() handles that), but specifically "the entries I need have
# actually been evicted." May be adjusted later.
_MAX_LOG_ENTRIES = 100

_COVER_LETTER_MODE_NAMES = cover_letter_mode_names()


@dataclass(frozen=True)
class _ResourceConfig:
    content_dir_name: str
    local_log_rel: str  # relative to output/, where local_log.json persists
    s3_log_rel: str  # relative to output/, virtual path used only for S3 key computation
    lock_name: str
    # Cover letters are unique-per-job and never rewritten in place, so "the local file already
    # exists" reliably means "already correct" — skip re-downloading it. form_fill_rules files
    # (auto_rules.json etc.) are the opposite: the same filename gets rewritten repeatedly, so an
    # "add" entry for one *always* means "download the fresh version", existing or not — an
    # existence-only check would mean a device with a local auto_rules.json (i.e. every device,
    # almost always) could never pick up anyone else's changes to it at all.
    mutable_content: bool


_RESOURCE_CONFIGS: dict[str, _ResourceConfig] = {
    RESOURCE_COVERLETTERS: _ResourceConfig(
        content_dir_name="coverletters",
        local_log_rel="coverletters/local_log.json",
        s3_log_rel="coverletters/s3_log.json",
        lock_name="coverletters.lock",
        mutable_content=False,
    ),
    RESOURCE_FORM_FILL_RULES: _ResourceConfig(
        content_dir_name="form_fill_rules",
        # Siblings of output/form_fill_rules/, not inside it — that directory is glob-loaded
        # directly (*.json) by FormFillRulesEngine and RuleIndex, exactly like the existing
        # rule recycle-bin file already has to stay outside it for the same reason.
        local_log_rel="form_fill_rules_local_log.json",
        s3_log_rel="form_fill_rules_s3_log.json",
        lock_name="form_fill_rules.lock",
        mutable_content=True,
    ),
}


class PendingChangeTracker:
    """
    Accumulates one resource's pending adds/deletes across a single program run, from whenever it
    last caught up (via :func:`sync_log_download` or :func:`sync_log_upload`) to whenever it next
    calls :func:`sync_log_upload`. Writing then deleting (or vice versa) the same path within one
    run collapses to just the later action, since only the end-of-run state matters.
    """

    def __init__(self) -> None:
        self._adds: list[str] = []
        self._deletes: list[str] = []

    def record_write(self, rel_path: str) -> None:
        rel_path = rel_path.replace("\\", "/")
        if rel_path in self._deletes:
            self._deletes.remove(rel_path)
        if rel_path not in self._adds:
            self._adds.append(rel_path)

    def record_delete(self, rel_path: str) -> None:
        rel_path = rel_path.replace("\\", "/")
        if rel_path in self._adds:
            self._adds.remove(rel_path)
        if rel_path not in self._deletes:
            self._deletes.append(rel_path)

    def is_empty(self) -> bool:
        return not self._adds and not self._deletes

    @property
    def pending_adds(self) -> list[str]:
        return list(self._adds)

    @property
    def pending_deletes(self) -> list[str]:
        return list(self._deletes)

    def clear(self) -> None:
        self._adds.clear()
        self._deletes.clear()


def _empty_log_doc() -> dict[str, Any]:
    return {"version": 1, "next_sequence": 1, "entries": []}


def _read_local_log_doc(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_log_doc()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s (%s) — treating as empty", path, e)
        return _empty_log_doc()


def _write_local_log_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _last_applied_sequence(local_doc: dict[str, Any]) -> int:
    entries = local_doc.get("entries") or []
    return max((int(e["sequence"]) for e in entries), default=0)


def simulate(entries: list[dict[str, Any]], last_applied_sequence: int) -> tuple[set[str], set[str]]:
    """
    Replay every ``complete`` entry newer than ``last_applied_sequence``, in sequence order, into
    the final (to_download, to_delete) sets. Later entries override earlier ones for the same
    path (e.g. added then later deleted within the same catch-up window ends up in to_delete).
    """
    to_download: set[str] = set()
    to_delete: set[str] = set()
    for entry in sorted(entries, key=lambda e: int(e["sequence"])):
        if int(entry["sequence"]) <= last_applied_sequence or entry.get("status") != "complete":
            continue
        paths = {f["path"] for f in entry.get("files") or []}
        if entry.get("type") == "add":
            to_download |= paths
            to_delete -= paths
        else:
            to_delete |= paths
            to_download -= paths
    return to_download, to_delete


def _log_has_gap(entries: list[dict[str, Any]], last_applied_sequence: int) -> bool:
    """
    True when ``last_applied_sequence`` is far enough behind that :func:`simulate` can't safely
    reconstruct the current state: the oldest entry the (trimmed) log still has is *newer* than
    what this device would need to resume from, meaning whatever happened in between has been
    evicted and is now unknown. A device merely behind — even by a lot, as long as everything it
    still needs is still in the log — has no gap and uses ``simulate`` normally.
    """
    if not entries:
        return False
    oldest = min(int(e["sequence"]) for e in entries)
    return last_applied_sequence < oldest - 1


def _compute_full_resync_sets(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig
) -> tuple[set[str], set[str]]:
    """
    Full alternative to :func:`simulate` for a device whose gap can't be bridged from the log
    alone: lists every object currently under this resource's content prefix in S3 (the log's
    entries are pruned, but S3 itself always reflects the current authoritative set, since every
    add/delete entry's own upload/delete already happened against these same keys) and diffs that
    against what's locally present. Returns ``(to_download, to_delete)`` in the exact same shape
    :func:`simulate` produces, so callers apply it identically either way (including mode
    filtering and the mutable-content-always-overwrites rule in :func:`_apply_add_delete`, and the
    ``to_download``-based conflict detection in :func:`sync_log_upload`).
    """
    content_root = output_dir / cfg.content_dir_name
    content_prefix = s3_key_for_file(output_dir, content_root) + "/"

    remote_paths: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=content_prefix):
        for obj in page.get("Contents") or []:
            key = obj.get("Key") or ""
            if not key or key.endswith("/") or not key.startswith(content_prefix):
                continue
            remote_paths.add(key[len(content_prefix):])

    if cfg.mutable_content:
        local_paths = (
            {p.name for p in content_root.glob("*.json") if p.is_file()} if content_root.is_dir() else set()
        )
        # Always re-pull every remote file for mutable content, existing locally or not — same
        # reasoning as _apply_add_delete's mutable_content check: existence doesn't mean current.
        to_download = set(remote_paths)
    else:
        local_paths = (
            {p.relative_to(content_root).as_posix() for p in content_root.rglob("*.docx") if p.is_file()}
            if content_root.is_dir()
            else set()
        )
        to_download = remote_paths - local_paths

    to_delete = local_paths - remote_paths
    return to_download, to_delete


def _resolve_download_delete_sets(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig,
    entries: list[dict[str, Any]], last_applied_sequence: int,
) -> tuple[set[str], set[str]]:
    """``simulate()``, or a full resync if this device's gap can no longer be bridged that way."""
    if _log_has_gap(entries, last_applied_sequence):
        log.info(
            "%s: local state is too far behind the trimmed log (needed entries have been "
            "evicted) — doing a full resync instead of incremental catch-up.",
            cfg.content_dir_name,
        )
        return _compute_full_resync_sets(s3, bucket, output_dir, cfg)
    return simulate(entries, last_applied_sequence)


def _get_s3_client_or_none():
    try:
        from .s3_outputs import _s3_client

        return _s3_client()
    except ImportError:
        log.warning("S3 log-sync skipped: install boto3")
        return None


def _s3_log_key(output_dir: Path, cfg: _ResourceConfig) -> str:
    return s3_key_for_file(output_dir, output_dir / cfg.s3_log_rel)


def _download_remote_log(s3, bucket: str, output_dir: Path, cfg: _ResourceConfig) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    key = _s3_log_key(output_dir, cfg)
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in ("NoSuchKey", "404"):
            return _empty_log_doc()
        raise


def _trim_log_doc(doc: dict[str, Any]) -> None:
    """Keep only the most recent ``_MAX_LOG_ENTRIES`` entries (by sequence), in place. Called from
    every place that uploads the log, so it's the one choke point that keeps it bounded."""
    entries = doc.get("entries") or []
    if len(entries) > _MAX_LOG_ENTRIES:
        entries = sorted(entries, key=lambda e: int(e["sequence"]))
        doc["entries"] = entries[-_MAX_LOG_ENTRIES:]


def _upload_remote_log(s3, bucket: str, output_dir: Path, cfg: _ResourceConfig, doc: dict[str, Any]) -> None:
    _trim_log_doc(doc)
    key = _s3_log_key(output_dir, cfg)
    body = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def _content_key(output_dir: Path, cfg: _ResourceConfig, rel_path: str) -> str:
    return s3_key_for_file(output_dir, output_dir / cfg.content_dir_name / rel_path)


def _head_object_etag(s3, bucket: str, key: str) -> tuple[bool, str | None]:
    from botocore.exceptions import ClientError

    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
        return True, resp.get("ETag")
    except ClientError as e:
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = e.response.get("Error", {}).get("Code", "")
        if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
            return False, None
        raise


def _next_sequence(doc: dict[str, Any]) -> int:
    seq = int(doc.get("next_sequence", 1))
    doc["next_sequence"] = seq + 1
    return seq


def _reap_stale_entries(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig, doc: dict[str, Any],
    *, stale_after_seconds: float,
) -> bool:
    """
    Split any long-stale ``in-progress`` entry into a ``complete`` entry (files that actually made
    it to S3) plus, if some files never did, a new ``cancelled`` entry for the rest. Mutates
    ``doc`` in place. Returns True if anything changed (caller must re-upload the log).
    """
    now = time.time()
    changed = False
    new_entries: list[dict[str, Any]] = []
    for entry in doc.get("entries") or []:
        age = now - float(entry.get("updated_at", now))
        if entry.get("status") != "in-progress" or age <= stale_after_seconds:
            new_entries.append(entry)
            continue

        completed: list[dict[str, Any]] = []
        uncompleted: list[dict[str, Any]] = []
        for f in entry.get("files") or []:
            key = _content_key(output_dir, cfg, f["path"])
            exists, etag = _head_object_etag(s3, bucket, key)
            if entry.get("type") == "add":
                before = f.get("before_etag")
                succeeded = exists if before is None else (exists and etag != before)
            else:  # delete
                succeeded = not exists
            (completed if succeeded else uncompleted).append(f)

        changed = True
        log.warning(
            "%s log: entry %s has been in-progress for %.0fs — treating as abandoned "
            "(%d file(s) completed, %d did not).",
            cfg.content_dir_name, entry.get("sequence"), age, len(completed), len(uncompleted),
        )
        if completed:
            entry["files"] = completed
            entry["status"] = "complete"
            entry["updated_at"] = now
            new_entries.append(entry)
        # else: nothing completed — just fold it straight into the cancelled entry below instead
        # of keeping a pointless empty-files "complete" entry.
        if uncompleted:
            new_entries.append(
                {
                    "sequence": _next_sequence(doc),
                    "type": entry.get("type"),
                    "status": "cancelled",
                    "owner": entry.get("owner"),
                    "created_at": entry.get("created_at"),
                    "updated_at": now,
                    "files": uncompleted,
                }
            )
        elif not completed:
            # Wholly-uncompleted entry with no split needed — still must land *somewhere* in the
            # log as cancelled (not just silently dropped) so it's visible it never finished.
            entry["status"] = "cancelled"
            entry["updated_at"] = now
            new_entries.append(entry)

    doc["entries"] = new_entries
    return changed


def _lock_still_ours(s3, bucket: str, cfg: _ResourceConfig, token: str) -> bool:
    """True if our lock token is still the live lock object's ETag (see module docstring on the
    staleness-during-a-very-slow-upload edge case this guards against)."""
    from .s3_outputs import _sync_lock_key

    key = _sync_lock_key(cfg.lock_name)
    exists, etag = _head_object_etag(s3, bucket, key)
    return exists and etag == token


def _cover_letter_rel_in_modes(rel: str, modes: tuple[str, ...]) -> bool:
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] in _COVER_LETTER_MODE_NAMES:
        return parts[0] in modes
    return "linkedin" in modes  # legacy flat-file convention


def _apply_add_delete(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig,
    to_download: set[str], to_delete: set[str], *, mode_filter: tuple[str, ...] | None,
) -> None:
    content_root = output_dir / cfg.content_dir_name
    for rel in sorted(to_download):
        if mode_filter is not None and cfg.content_dir_name == RESOURCE_COVERLETTERS:
            if not _cover_letter_rel_in_modes(rel, mode_filter):
                continue
        dest = content_root / rel
        if dest.is_file() and not cfg.mutable_content:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        key = _content_key(output_dir, cfg, rel)
        try:
            s3.download_file(bucket, key, str(dest))
        except Exception as e:
            log.warning("Could not download s3://%s/%s: %s", bucket, key, e)
    for rel in sorted(to_delete):
        dest = content_root / rel
        if dest.is_file():
            try:
                dest.unlink()
            except OSError as e:
                log.warning("Could not delete local %s: %s", dest, e)


def backfill_existing_files(resource: str, *, root: Path) -> None:
    """
    One-time migration for files that already existed locally before this log-based protocol did.

    Those files have no log entry anywhere — they were uploaded (if at all) by the old,
    unconditional-every-run sync this module replaced, so a *different* device (or a fresh one)
    would never learn about them via :func:`sync_log_download`, since that only ever pulls files
    referenced by a logged entry, never "whatever happens to already be in S3." This scans what's
    currently on disk and backfills whichever of those files have genuinely never been logged.

    Deliberately narrower than "scan and re-add everything local": any file already mentioned in
    *any* existing entry (add or delete, regardless of status) is skipped, not re-backfilled. A
    device that's been offline a long time can still have a stale local copy of something another
    device already correctly deleted (a cover letter) or rewrote to remove a rule from (a
    form-fill-rules file) — with no such check, this function would re-add exactly that stale
    copy as a *new*, higher-sequence entry, and since :func:`simulate` lets later entries override
    earlier ones for the same path, that resurrects the deleted/superseded content for every
    device, not just this one. Only genuinely never-logged files are safe to backfill; a file
    that already has *some* history is trusted to that history instead, even if this device
    hasn't caught up on it yet (the :func:`sync_log_download` call right after this one does that).

    The tradeoff, accepted deliberately: once a file has any history at all, a second device's
    independently-accumulated local edits to that same file (e.g. rules learned entirely offline
    before ever syncing) won't get merged in automatically — plain union-by-id merging can't tell
    "genuinely new" apart from "stale, already deleted elsewhere" without tracking deletions
    explicitly (tombstones), which this does not do. Files with zero history anywhere are
    unaffected and still merge normally.

    Idempotent and safe to call on every run: no-ops immediately once this device has a
    ``local_log.json`` for ``resource`` — from a prior backfill, or from ever having completed a
    normal sync cycle — since that means it has already participated in the protocol at least once.
    """
    if not s3_output_sync_enabled():
        return
    cfg = _RESOURCE_CONFIGS[resource]
    output_dir = resolve_output_dir(root).parent
    local_log_path = output_dir / cfg.local_log_rel
    if local_log_path.is_file():
        return

    content_root = output_dir / cfg.content_dir_name
    if not content_root.is_dir():
        return

    s3 = _get_s3_client_or_none()
    if s3 is None:
        return
    bucket = s3_output_bucket()
    try:
        remote_doc = _download_remote_log(s3, bucket, output_dir, cfg)
    except Exception as e:
        # Can't safely tell what's already logged — skip backfilling this run rather than risk
        # resurrecting something. Retried next run.
        log.warning("Could not check existing %s log before backfill: %s", resource, e)
        return
    already_known: set[str] = {
        f["path"] for entry in (remote_doc.get("entries") or []) for f in (entry.get("files") or [])
    }

    if resource == RESOURCE_COVERLETTERS:
        candidates = [
            p.relative_to(content_root).as_posix()
            for p in sorted(content_root.rglob("*.docx"))
            if p.is_file()
        ]
    else:
        # Matches FormFillRulesEngine/RuleIndex's own glob scope exactly, so a fresh device ends
        # up with the complete rule set (hand-curated files included), not just auto_rules.json.
        candidates = [p.name for p in sorted(content_root.glob("*.json")) if p.is_file()]

    tracker = PendingChangeTracker()
    skipped = 0
    for rel in candidates:
        if rel in already_known:
            skipped += 1
            continue
        tracker.record_write(rel)
    if skipped:
        log.info(
            "%s: skipping backfill of %d file(s) that already have log history — trusting the "
            "log over a possibly-stale local copy.", resource, skipped,
        )

    if tracker.is_empty():
        # Nothing to backfill — write an empty local_log so this scan doesn't repeat every run
        # forever. Any real remote history still gets picked up normally by the
        # sync_log_download() call that follows this one.
        _write_local_log_doc(local_log_path, _empty_log_doc())
        return

    log.info(
        "%s: backfilling %d pre-existing local file(s) into the sync log (one-time).",
        resource, len(tracker.pending_adds),
    )
    sync_log_upload(resource, root=root, pending=tracker)


def sync_log_download(
    resource: str, *, root: Path, mode_filter: tuple[str, ...] | None = None
) -> None:
    """Catch this device up on ``resource``'s log: download new ``add`` files, delete new
    ``delete`` files, advance ``local_log``. No-op if S3 sync is disabled or nothing is new."""
    if not s3_output_sync_enabled():
        return
    cfg = _RESOURCE_CONFIGS[resource]
    output_dir = resolve_output_dir(root).parent
    local_log_path = output_dir / cfg.local_log_rel

    s3 = _get_s3_client_or_none()
    if s3 is None:
        return
    bucket = s3_output_bucket()

    try:
        remote_doc = _download_remote_log(s3, bucket, output_dir, cfg)
    except Exception as e:
        log.warning("Could not download %s log: %s", resource, e)
        return

    local_doc = _read_local_log_doc(local_log_path)
    last_applied = _last_applied_sequence(local_doc)
    remote_max = max((int(e["sequence"]) for e in remote_doc.get("entries") or []), default=0)
    if remote_max <= last_applied:
        return  # nothing new — no lock needed for the common case

    token = acquire_sync_lock(lock_name=cfg.lock_name)
    if token is None:
        log.warning(
            "Could not acquire %s — skipping this run's %s download.", cfg.lock_name, resource
        )
        return
    try:
        remote_doc = _download_remote_log(s3, bucket, output_dir, cfg)  # definitive, lock held
        if _reap_stale_entries(s3, bucket, output_dir, cfg, remote_doc, stale_after_seconds=_SYNC_LOCK_STALE_SECONDS):
            _upload_remote_log(s3, bucket, output_dir, cfg, remote_doc)
        to_download, to_delete = _resolve_download_delete_sets(
            s3, bucket, output_dir, cfg, remote_doc.get("entries") or [], last_applied
        )
        _apply_add_delete(s3, bucket, output_dir, cfg, to_download, to_delete, mode_filter=mode_filter)
        _write_local_log_doc(local_log_path, remote_doc)
    finally:
        release_sync_lock(token, lock_name=cfg.lock_name)


def sync_log_upload(resource: str, *, root: Path, pending: PendingChangeTracker) -> None:
    """Push ``pending``'s accumulated adds/deletes for ``resource``. No-op if there's nothing
    pending or S3 sync is disabled."""
    if pending.is_empty() or not s3_output_sync_enabled():
        return
    cfg = _RESOURCE_CONFIGS[resource]
    output_dir = resolve_output_dir(root).parent
    local_log_path = output_dir / cfg.local_log_rel
    content_root = output_dir / cfg.content_dir_name

    s3 = _get_s3_client_or_none()
    if s3 is None:
        return
    bucket = s3_output_bucket()

    token = acquire_sync_lock(lock_name=cfg.lock_name)
    if token is None:
        log.warning(
            "Could not acquire %s — skipping this run's %s upload.", cfg.lock_name, resource
        )
        return
    try:
        doc = _download_remote_log(s3, bucket, output_dir, cfg)
        if _reap_stale_entries(s3, bucket, output_dir, cfg, doc, stale_after_seconds=_SYNC_LOCK_STALE_SECONDS):
            _upload_remote_log(s3, bucket, output_dir, cfg, doc)

        local_doc = _read_local_log_doc(local_log_path)
        last_applied = _last_applied_sequence(local_doc)
        to_download, to_delete = _resolve_download_delete_sets(
            s3, bucket, output_dir, cfg, doc.get("entries") or [], last_applied
        )

        pending_adds = set(pending.pending_adds)
        pending_deletes = set(pending.pending_deletes)

        if resource == RESOURCE_FORM_FILL_RULES:
            conflicted = to_download & pending_adds
            for rel in conflicted:
                _merge_rule_file_before_overwrite(content_root, rel, output_dir, bucket, s3, cfg)
            # Already resolved above (merged in place) — don't let the plain overwrite pass
            # below clobber the merge with the unmerged remote copy.
            to_download -= conflicted
        else:
            # Cover letters: remote's completed version wins outright on a same-name conflict.
            pending_adds -= to_download

        _apply_add_delete(s3, bucket, output_dir, cfg, to_download, to_delete, mode_filter=None)

        if pending_adds:
            doc = _append_add_entry(s3, bucket, output_dir, cfg, doc, content_root, pending_adds)
        if pending_deletes:
            doc = _append_delete_entry(s3, bucket, output_dir, cfg, doc, pending_deletes)

        _write_local_log_doc(local_log_path, doc)
        pending.clear()
    finally:
        release_sync_lock(token, lock_name=cfg.lock_name)


def _merge_rule_file_before_overwrite(content_root: Path, rel: str, output_dir: Path, bucket: str, s3, cfg: _ResourceConfig) -> None:
    """
    Before the remote's completed version of ``rel`` overwrites this device's local copy (via the
    upcoming ``_apply_add_delete`` download), snapshot the local copy's rule ids into a temp
    location, let the download proceed, then union the two by rule id.
    """
    from .extension_rules import union_rule_file

    if not (content_root / rel).is_file():
        return
    try:
        local_data = json.loads((content_root / rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    key = _content_key(output_dir, cfg, rel)
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        remote_data = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:
        return  # remote fetch failed — leave the plain download/overwrite to happen as-is
    merged = union_rule_file(local_data, remote_data)
    (content_root / rel).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # The subsequent _apply_add_delete download would otherwise clobber this merge with the plain
    # remote copy — since dest.is_file() is now True, _apply_add_delete's "skip if already
    # present" check leaves it alone. Nothing further to do here.


def _snapshot_before_etags(s3, bucket: str, output_dir: Path, cfg: _ResourceConfig, rels: list[str]) -> list[dict[str, Any]]:
    files = []
    for rel in rels:
        key = _content_key(output_dir, cfg, rel)
        exists, etag = _head_object_etag(s3, bucket, key)
        files.append({"path": rel, "before_etag": etag if exists else None})
    return files


def _append_add_entry(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig, doc: dict[str, Any],
    content_root: Path, rels: set[str],
) -> dict[str, Any]:
    from .s3_outputs import _sync_lock_owner

    rel_list = sorted(rels)
    files = _snapshot_before_etags(s3, bucket, output_dir, cfg, rel_list)
    now = time.time()
    seq = _next_sequence(doc)
    entry = {
        "sequence": seq, "type": "add", "status": "in-progress",
        "owner": _sync_lock_owner(), "created_at": now, "updated_at": now, "files": files,
    }
    doc.setdefault("entries", []).append(entry)
    _upload_remote_log(s3, bucket, output_dir, cfg, doc)

    for rel in rel_list:
        src = content_root / rel
        if not src.is_file():
            continue
        key = _content_key(output_dir, cfg, rel)
        ctype, _ = mimetypes.guess_type(src.name)
        extra = {"ContentType": ctype} if ctype else {}
        try:
            if extra:
                s3.upload_file(str(src), bucket, key, ExtraArgs=extra)
            else:
                s3.upload_file(str(src), bucket, key)
        except Exception as e:
            log.warning("Could not upload s3://%s/%s: %s", bucket, key, e)

    from .s3_outputs import _sync_lock_key as _lk

    token_key = _lk(cfg.lock_name)
    _, live_etag = _head_object_etag(s3, bucket, token_key)
    # Re-fetch the log too, in case a very slow upload let another device judge our lock stale
    # and take over (see _lock_still_ours docstring) — if so, don't claim completion over
    # whatever that device already did; leave our entry for it to have reaped.
    doc2 = _download_remote_log(s3, bucket, output_dir, cfg)
    ours = next((e for e in doc2.get("entries") or [] if e.get("sequence") == seq), None)
    if ours is not None and ours.get("status") == "in-progress":
        ours["status"] = "complete"
        ours["updated_at"] = time.time()
        _upload_remote_log(s3, bucket, output_dir, cfg, doc2)
        return doc2
    log.warning(
        "%s log: entry %s was no longer in-progress when finishing upload (lock may have been "
        "reclaimed) — leaving its resolution to whoever reaped it.", cfg.content_dir_name, seq,
    )
    return doc2


def _append_delete_entry(
    s3, bucket: str, output_dir: Path, cfg: _ResourceConfig, doc: dict[str, Any], rels: set[str],
) -> dict[str, Any]:
    from .s3_outputs import _sync_lock_owner

    rel_list = sorted(rels)
    now = time.time()
    seq = _next_sequence(doc)
    entry = {
        "sequence": seq, "type": "delete", "status": "in-progress",
        "owner": _sync_lock_owner(), "created_at": now, "updated_at": now,
        "files": [{"path": rel} for rel in rel_list],
    }
    doc.setdefault("entries", []).append(entry)
    _upload_remote_log(s3, bucket, output_dir, cfg, doc)

    from botocore.exceptions import ClientError

    for rel in rel_list:
        key = _content_key(output_dir, cfg, rel)
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except ClientError as e:
            log.warning("Could not delete s3://%s/%s: %s", bucket, key, e)

    doc2 = _download_remote_log(s3, bucket, output_dir, cfg)
    ours = next((e for e in doc2.get("entries") or [] if e.get("sequence") == seq), None)
    if ours is not None and ours.get("status") == "in-progress":
        ours["status"] = "complete"
        ours["updated_at"] = time.time()
        _upload_remote_log(s3, bucket, output_dir, cfg, doc2)
        return doc2
    log.warning(
        "%s log: entry %s was no longer in-progress when finishing delete (lock may have been "
        "reclaimed) — leaving its resolution to whoever reaped it.", cfg.content_dir_name, seq,
    )
    return doc2
