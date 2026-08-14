"""
Centralized console output formatting.

Code that prints results to the terminal should call these functions instead of formatting
and printing inline, so changing how output looks later (colors, a progress bar, JSON mode,
whatever) only touches this file rather than the logic that produces the results.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

_S3_BAR_WIDTH = 24
_S3_DETAIL_MAX_LEN = 40
_S3_LINE_PAD = 100  # wide enough to blank out any shorter previous line when overwritten


def _safe_print(text: str, *, end: str = "\n") -> None:
    """``print`` that degrades to ASCII instead of crashing on a legacy console codepage."""
    try:
        print(text, end=end, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), end=end, flush=True)


def _log_to_file_handlers_only(text: str) -> None:
    """
    Write ``text`` at INFO to any ``FileHandler`` already attached to the root logger, without
    also reaching console handlers (``StreamHandler``, which ``logging.basicConfig`` defaults to
    ``sys.stderr``). A plain ``log.info(text)`` would print a second, permanent, timestamped
    copy via that console handler — independent of and invisible to our own raw stdout prints —
    which is exactly what made ``waiting_message`` leave a stray line behind: the erase below
    only ever touched its own print, never the logger's separate console copy.
    """
    record = log.makeRecord(log.name, logging.INFO, __file__, 0, text, (), None)
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.handle(record)


def log_only(text: str, *args: object) -> None:
    """
    Record ``text`` in the log file only — never on the console.

    For internal/mechanical detail that's genuinely useful when reconstructing what happened
    after the fact (DOM scraping internals: card counts, which CSS selector matched, pagination
    bookkeeping) but is just repetitive noise scrolling past during a live run. Accepts %-style
    args like ``log.info``: ``log_only("Matched %d link(s) via %r", n, selector)``.

    Falls back to a normal ``log.info`` (reaching the console too) when nothing has configured
    logging yet / no file handler is attached, so a bare script doesn't silently lose the line.
    """
    formatted = text % args if args else text
    if any(isinstance(h, logging.FileHandler) for h in logging.root.handlers):
        _log_to_file_handlers_only(formatted)
    else:
        log.info("%s", formatted)


def print_job_separator() -> None:
    """
    A genuinely blank line (no timestamp/level prefix) before each new job's block of output —
    separates one job's evaluation lines from the previous job's when scanning the console or
    the log file. Written to stdout and directly to any file handler's underlying stream
    (bypassing the log Formatter, which would otherwise stamp even an empty message).
    """
    print()
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                handler.stream.write("\n")
                handler.stream.flush()
            except Exception:
                pass


@contextmanager
def waiting_message(text: str) -> Iterator[None]:
    """
    Show ``text`` on an interactive console for the duration of the ``with`` block, then erase
    it — a "please wait" notice is only useful while the wait is actually happening; leaving it
    sitting in scrollback afterward just adds noise.

    On a non-interactive stream (piped/redirected output, headless/CI), just logs normally
    (``log.info``, reaching both the console and the log file) since there's no in-place line to
    manage. On an interactive terminal, the message still lands in the log file — via the file
    handler(s) directly, deliberately bypassing any console handler — while the console itself
    only ever shows the one ephemeral, erasable copy.

    The block itself is whatever the caller was already doing to actually wait (``time.sleep``,
    ``interruptible_sleep``, a poll loop, ...) — this only wraps the display around it, so
    e.g. ``interruptible_sleep``'s early-exit / return value is untouched::

        with waiting_message(f"Waiting {secs:.1f}s for job list to render"):
            return interruptible_sleep(secs, driver)
    """
    interactive = sys.stdout.isatty()
    if not interactive:
        log.info("%s", text)
        yield
        return

    _log_to_file_handlers_only(text)
    _safe_print(f"\r{text}", end="")
    try:
        yield
    finally:
        _safe_print(f"\r{' ' * len(text)}\r", end="")


def print_s3_progress(verb: str, current: int, total: int, name: str, *, action: str = "") -> None:
    """
    S3 sync progress for one file out of ``total``.

    On an interactive terminal, renders a single in-place progress bar (overwritten via ``\\r``)
    instead of one line per file, so a large sync doesn't push everything else off screen. Falls
    back to one log line per file (via the module logger, so it also lands in the log file) when
    stdout is not a terminal — piped/redirected output, or a headless/CI run — where an in-place
    bar would just show up as a wall of literal ``\\r`` characters instead of updating in place.

    ``action`` names what's happening to this specific file when it differs from ``verb`` (e.g. a
    download whose per-file action is "merging" vs "downloading"); omitted when not given.
    """
    detail = f"{action} {name}" if action else name

    if not sys.stdout.isatty():
        text = f"S3: {verb} {current}/{total} ({detail})"
        log.info("%s", text)
        if not logging.root.handlers:
            print(text, flush=True)
        return

    total_display = max(total, 1)
    frac = min(1.0, current / total_display)
    filled = int(round(_S3_BAR_WIDTH * frac))
    if len(detail) > _S3_DETAIL_MAX_LEN:
        detail = "…" + detail[-(_S3_DETAIL_MAX_LEN - 1) :]
    bar = "█" * filled + "░" * (_S3_BAR_WIDTH - filled)
    line = f"\r{f'S3 {verb} [{bar}] {current}/{total} {detail}':<{_S3_LINE_PAD}}"
    try:
        print(line, end="", flush=True)
    except UnicodeEncodeError:
        # Legacy (non-UTF-8) console codepage can't render the block characters or ellipsis —
        # fall back to plain ASCII rather than crashing a sync mid-run over cosmetics.
        bar = "#" * filled + "-" * (_S3_BAR_WIDTH - filled)
        detail_ascii = detail.replace("…", "...")
        line = f"\r{f'S3 {verb} [{bar}] {current}/{total} {detail_ascii}':<{_S3_LINE_PAD}}"
        print(line.encode("ascii", "replace").decode("ascii"), end="", flush=True)
    if current >= total:
        print()  # move past the bar so subsequent output starts on a fresh line
        log.info("S3: %s complete — %d file(s)", verb, total)


def print_job_outcome(
    title: str | None,
    company: str | None,
    *,
    outcome: str,
    reason: str = "",
    job_id: str = "",
    dismissed: bool | None = None,
) -> None:
    """
    One line summarizing a job evaluation outcome (skip/blacklist/consulting/gates-failed/etc.).

    Replaces what used to be 2-4 separate lines for the same decision: a "Skipping (...)"
    message, a tracker-log call (silent), the dismiss helper's own internal confirmation, and a
    caller-side "-> Dismissed on LinkedIn..." follow-up. Callers still do the actual work
    (``_tracker_log``, ``dismiss_current_job``) — this only reports the combined result, so it
    stays a pure display concern. Pass ``dismissed=None`` when no dismiss was attempted (e.g.
    fit-below-threshold, or a "no dismiss" skip reason) rather than True/False.

    Examples::

        print_job_outcome("SWE", "Foo Inc", outcome="consulting",
                           reason="remembered consulting company", job_id="123", dismissed=True)
        -> "CONSULTING: SWE at Foo Inc — remembered consulting company [id=123] (dismissed)"

        print_job_outcome("SWE", "Foo Inc", outcome="skipped", reason="below fit threshold (62%)")
        -> "SKIPPED: SWE at Foo Inc — below fit threshold (62%)"
    """
    ti = (title or "").replace("\r", " ").replace("\n", " ").strip() or "(no title)"
    co = (company or "").replace("\r", " ").replace("\n", " ").strip() or "(no company)"
    bits = [f"{outcome.upper()}: {ti} at {co}"]
    if reason:
        bits.append(f"— {reason}")
    if job_id:
        bits.append(f"[id={job_id}]")
    if dismissed is True:
        bits.append("(dismissed)")
    elif dismissed is False:
        bits.append("(dismiss failed)")
    log.info(" ".join(bits))


def print_job_fit_debug(
    company: str | None,
    title: str | None,
    fit: float | None,
    *,
    note: str = "",
) -> None:
    """
    Print one line to stdout (company, title, fit) for terminal debugging. Independent of log
    level. Use ``fit=None`` when the job was not fit-scored (e.g. hard gates failed).
    """
    c = (company or "").replace("\r", " ").replace("\n", " ").strip() or "(no company)"
    ti = (title or "").replace("\r", " ").replace("\n", " ").strip() or "(no title)"
    fs = "(not scored)" if fit is None else f"{float(fit):.4f}"
    suffix = f" | {note}" if note else ""
    print(f"[job-fit] company={c!r} | title={ti!r} | fit={fs}{suffix}", flush=True)


def print_saved_jobs_summary(
    *,
    source_label: str,
    total_parsed: int,
    invalid: list[str],
    dest_label: str,
    added: int,
    skipped: int,
    skipped_jobs: list[dict[str, Any]],
    dry_run: bool,
    wrote_path: str | None = None,
) -> None:
    """Summary block after importing saved jobs (``saved_jobs.txt``) into the assisted CSV."""
    print("=== Jobs import summary ===")
    print(f"Parsed {total_parsed} job(s) from {source_label}")
    if invalid:
        print(f"Skipped {len(invalid)} unparseable line(s):")
        for line in invalid:
            print(f"  • {line}")
    if dry_run:
        print("Dry run — no CSV changes written.")
    print(f"Added {added} row(s) to {dest_label}")
    print(f"Skipped {skipped} duplicate(s) already in assisted CSV or archive")
    if skipped_jobs:
        for job in skipped_jobs:
            print(f"  • {job.get('title')} at {job.get('company')} ({job.get('url')})")
    if added and not dry_run and wrote_path:
        print(f"Wrote: {wrote_path}")
    print()
