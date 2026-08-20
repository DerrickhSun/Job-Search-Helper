"""
Centralized console output formatting.

Code that prints results to the terminal should call these functions instead of formatting
and printing inline, so changing how output looks later (colors, a progress bar, JSON mode,
whatever) only touches this file rather than the logic that produces the results.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

_S3_BAR_WIDTH = 24
_S3_DETAIL_MAX_LEN = 40  # upper bound only — print_s3_progress shrinks this to fit narrower terminals
_S3_LINE_PAD = 100  # upper bound only — clamped to the actual terminal width at print time (see below)

# Persistent "where we are" status line pinned to the bottom of an interactive console (see
# set_status_line). Guarded by an RLock (not a plain Lock) because redrawing it happens from
# inside _status_paused(), which every other console-output function in this module also enters —
# a plain Lock would deadlock the first time one of those functions triggered a nested call.
_status_lock = threading.RLock()
_status_text = ""
_status_drawn = False


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
    with _status_paused():
        print()
    for handler in logging.root.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                handler.stream.write("\n")
                handler.stream.flush()
            except Exception:
                pass


def _erase_lines(n: int) -> None:
    """
    Move the cursor up ``n`` lines and clear from there to the end of the screen.

    ``\\r`` alone (used for the single-line case) can only return to the start of the *current*
    line — it can't reach lines already scrolled past. Multi-line erase needs the ANSI cursor-up
    (``\\033[{n}A``) and erase-to-end-of-screen (``\\033[0J``) sequences instead, which require an
    ANSI-capable terminal (Windows Terminal, VS Code's integrated terminal, and modern
    conhost/PowerShell all qualify; legacy cmd.exe without VT processing enabled would print the
    raw escape codes instead of erasing — a display glitch, not a crash).
    """
    if n <= 0:
        return
    _safe_print(f"\033[{n}A\033[0J", end="")


def _status_erase_locked() -> None:
    """Erase the status line from the screen if currently drawn. Caller must hold ``_status_lock``."""
    global _status_drawn
    if _status_drawn and _status_text:
        _erase_lines(_status_text.count("\n") + 1)
    _status_drawn = False


def _status_draw_locked() -> None:
    """(Re)draw the status line, if any, on an interactive console. Caller must hold ``_status_lock``."""
    global _status_drawn
    if _status_text and sys.stdout.isatty():
        _safe_print(_status_text, end="\n")
        _status_drawn = True


@contextmanager
def _status_paused() -> Iterator[None]:
    """
    Erase the persistent status line (:func:`set_status_line`) for the duration of the block,
    then redraw it — so whatever the block prints lands above the status line instead of
    colliding with it. Every console-output function in this module, plus the console log
    handler (:class:`StatusAwareStreamHandler`), wraps its actual output in this. A no-op when no
    status line is currently set.
    """
    with _status_lock:
        _status_erase_locked()
        try:
            yield
        finally:
            _status_draw_locked()


def set_status_line(text: str) -> None:
    """
    Show ``text`` as a persistent status line pinned to the bottom of an interactive console
    (e.g. ``"Keyword: software engineer | Processed 12/143"``), replacing any previous status
    line. Every other console-output function in this module erases this line before printing
    and redraws it after, via :func:`_status_paused`, so it always ends up back at the bottom
    instead of interleaved with other output. A no-op on a non-interactive stream (piped/
    redirected output, headless/CI) — there's no fixed "bottom" to pin a line to there.
    """
    global _status_text
    if not sys.stdout.isatty():
        return
    with _status_lock:
        _status_erase_locked()
        _status_text = text
        _status_draw_locked()


def clear_status_line() -> None:
    """Remove the persistent status line. Call at the end of a run so it doesn't linger."""
    global _status_text
    with _status_lock:
        _status_erase_locked()
        _status_text = ""


class StatusAwareStreamHandler(logging.StreamHandler):
    """
    Console ``StreamHandler`` that keeps the persistent status line (:func:`set_status_line`)
    pinned to the bottom of the terminal: every record is emitted with the status line erased
    first and redrawn after, instead of leaving a stale or interleaved copy behind. Pass this
    (instead of a plain ``logging.StreamHandler``) as the console handler in
    ``logging.basicConfig`` for status-line support to cover every ``log.info``/``log.warning``/
    etc. call across the codebase automatically.
    """

    def emit(self, record: logging.LogRecord) -> None:
        with _status_paused():
            super().emit(record)


@contextmanager
def waiting_message(text: str) -> Iterator[None]:
    """
    Show ``text`` on an interactive console for the duration of the ``with`` block, then erase
    it — a "please wait" notice is only useful while the wait is actually happening; leaving it
    sitting in scrollback afterward just adds noise. ``text`` may contain ``\\n`` for a multi-line
    message; all of its lines are erased together.

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
    line_count = text.count("\n") + 1
    with _status_paused():
        if line_count == 1:
            _safe_print(f"\r{text}", end="")
        else:
            _safe_print(text, end="\n")
        try:
            yield
        finally:
            if line_count == 1:
                _safe_print(f"\r{' ' * len(text)}\r", end="")
            else:
                _erase_lines(line_count)


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
    bar = "█" * filled + "░" * (_S3_BAR_WIDTH - filled)

    # The "\r"-in-place trick only works if the whole padded line fits in one terminal row — a
    # line that's wider than the terminal wraps onto a second row, and the next tick's "\r" then
    # only rewinds to the start of *that* wrapped row, not back up to the bar's true start. Once
    # that happens the bar grows a new line on every subsequent tick instead of overwriting in
    # place. _S3_LINE_PAD (100) assumed a wide terminal; an 80-column console/panel (a common
    # Windows default) is already narrower than that even before the filename detail is added. So
    # clamp everything to the terminal's actual width, with a 1-column margin — some terminals
    # wrap as soon as the cursor reaches the last column even without a newline being printed.
    try:
        term_width = shutil.get_terminal_size(fallback=(_S3_LINE_PAD, 24)).columns
    except OSError:
        term_width = _S3_LINE_PAD
    line_width = max(20, min(_S3_LINE_PAD, term_width - 1))

    prefix = f"S3 {verb} [{bar}] {current}/{total} "
    detail_budget = max(4, min(_S3_DETAIL_MAX_LEN, line_width - len(prefix)))
    if len(detail) > detail_budget:
        detail = "…" + detail[-(detail_budget - 1) :]
    line = f"\r{(prefix + detail):<{line_width}}"

    # The whole tick (erase status -> write the bar -> optionally finish it off) has to happen as
    # one atomic unit under _status_lock, not just the erase. print()'s own internal lock only
    # keeps a single call from tearing; it does nothing to stop a *different* thread's log record
    # (StatusAwareStreamHandler.emit(), which also takes _status_lock) from landing its own
    # newline-terminated write in the gap between this bar's erase and its "\r"-prefixed write.
    # Once that happens the next "\r" only returns to the start of *that* line, not back up to
    # where the bar was, so the bar starts a fresh line on every subsequent tick instead of
    # overwriting in place — e.g. background company-lookup-worker log lines racing the bar during
    # a shutdown-time S3 upload.
    with _status_lock:
        _status_erase_locked()
        try:
            print(line, end="", flush=True)
        except UnicodeEncodeError:
            # Legacy (non-UTF-8) console codepage can't render the block characters or ellipsis —
            # fall back to plain ASCII rather than crashing a sync mid-run over cosmetics.
            bar_ascii = "#" * filled + "-" * (_S3_BAR_WIDTH - filled)
            detail_ascii = detail.replace("…", "...")
            prefix_ascii = f"S3 {verb} [{bar_ascii}] {current}/{total} "
            line = f"\r{(prefix_ascii + detail_ascii):<{line_width}}"
            print(line.encode("ascii", "replace").decode("ascii"), end="", flush=True)
        if current >= total:
            print()  # move past the bar so subsequent output starts on a fresh line
            log.info("S3: %s complete — %d file(s)", verb, total)
            _status_draw_locked()


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
    with _status_paused():
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
    with _status_paused():
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
