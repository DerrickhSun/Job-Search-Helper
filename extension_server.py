"""
Local HTTP API that lets the browser extension request a tailored cover letter, or answers for
a page's form fields, on demand — instead of only relying on cover letters pre-generated during
a ``main.py --filter`` run, or the Selenium auto-apply flow.

Run from repo root::

    python extension_server.py
    python extension_server.py --port 8743 --resume resume.pdf

Endpoints (all require ``Authorization: Bearer <token>``; see AUTH below)::

    GET  /health
        -> {"status": "ok"}

    POST /cover-letter
        body: {"title": str, "company": str, "description": str,
                "url": str?, "job_id": str?, "save_docx": bool? (default true)}
        -> {"cover_letter": str, "docx_path": str | null}

    POST /answer-fields
        body: {"fields": [{"label": str, "type": "text"|"textarea"|"select"|"radio"|"checkbox_group"}]}
        -> {"answers": [{"value": str | null, "values": [str], "flag": null | "discard"}]}
           (index-aligned with fields)

        Answers come from the same ``FormFillRulesEngine`` (resume/rule lookups) the Selenium
        auto-apply flow uses — there's no LLM fallback, so an unmatched label just comes back as
        ``{"value": null, "values": []}``. ``values`` is the full priority-ordered candidate list
        (a rule's ``answer``/``choose_label`` may list several acceptable answers, e.g.
        ``["No", "Not applicable"]``); ``value`` is just ``values[0]`` for callers that only want
        one. The caller (extension) is responsible for matching a candidate against the right DOM
        option/radio/checkbox — try ``values`` in order and stop at the first that matches an
        actual option, leaving the field untouched if none do; this endpoint only returns label ->
        candidates, the same division of labor ``form_filler.py`` already has internally. A
        ``"radio"`` field is resolved via ``screening_yes_no`` (mirrors how the Selenium flow
        answers Yes/No radios). ``"text"``/``"textarea"`` fields only ever have one candidate —
        free text has no "does the field offer this option" check, so there's nothing to fall
        back from. If the rules engine's internal disqualifying-question sentinel comes back
        (as any candidate), the answer is reported as ``{"value": null, "values": [], "flag":
        "discard"}`` instead of leaking that sentinel string as if it were literal fill text —
        the caller should surface this as "needs manual review", not fill anything.

    When ``save_docx`` is true (default), the .docx is written straight to the user's Downloads
    folder (override with ``--downloads-dir``) so it's already sitting where a file-upload dialog
    on the job application page opens — no need to dig through ``output/`` to attach it. Unlike
    cover letters generated during a ``main.py --filter`` run, these are not auto-deleted by
    ``process_extension.py`` when the job is later marked applied; Downloads is a folder the user
    manages themselves.

    Before calling the LLM, ``/cover-letter`` looks under ``output/coverletters/`` (linkedin /
    filter / greenhouse) for an existing ``.docx`` for the same job id. If one is found, its text
    is reused and the file is copied into Downloads when ``save_docx`` is true — no OpenAI call.
    Downloads itself is never scanned as a source.

    POST /process-extension
        body: {"request_id": str, "saved_jobs_text": str?, "saved_questions_text": str?,
                "dry_run": bool? (default false)}
        -> {"type": "extension_processed", "request_id": str, "summary": {...}}
           | {"type": "process_conflicts", "request_id": str, "server_request_id": str,
               "conflicts": [{"conflict_id": str,
                               "kind": "rule_conflict"|"blank_new_rule"|"reprioritize",
                               "question": str, "job": str|null, "url": str|null,
                               "extension_answer": str?, "existing_rule_answer": str?,
                               "existing_rule_file": str?, "existing_rule_id": str?,
                               "options": [{"choice": int, "label": str}, ...]}, ...]}

        "reprioritize" is the softer case where the extension's answer is already one of the
        rule's accepted fallback answers, just not the top-priority one — asking only whether it
        should move to the front (choice 1 = keep order, 2 = move to top), not a full
        keep/replace/combine decision (see ``utils/extension_rules.py::classify_extension_questions``).

        HTTP equivalent of running ``process_extension.py`` by hand: imports ``saved_jobs_text``
        (same line format as ``saved_jobs.txt``) into ``output/assisted_applications.csv``,
        classifies ``saved_questions_text`` (same block format as
        ``saved_jobs_application_questions.txt``) against existing form-fill rules, and
        auto-adds any that don't conflict. Unlike the CLI, rule conflicts and blank-answer
        confirmations never block on a terminal prompt — if there are none, the response above
        *is* the final result; otherwise a ``server_request_id`` (never the caller's own
        ``request_id`` — see ``utils/extension_process_service.py``) is minted and the conflicts
        are returned for the caller to resolve via ``/process-extension/resolve``. Pending
        conflicts expire after 10 minutes of no resolution.

    POST /process-extension/resolve
        body: {"request_id": str, "server_request_id": str,
                "resolutions": [{"conflict_id": str, "choice": int}, ...]}
        -> {"type": "extension_processed", "request_id": str, "summary": {...}, "unresolved": [...]}
           | {"type": "conflict_resolution_timeout", "request_id": str, "server_request_id": str}

        Both ids from the ``process_conflicts`` message must be echoed back — an unknown/expired
        ``server_request_id``, or one paired with the wrong ``request_id``, gets
        ``conflict_resolution_timeout`` and commits nothing (no partial application of whichever
        resolutions were sent). ``unresolved`` lists any conflict whose underlying rule changed or
        was removed since it was first reported (a live edit from another device, or another
        process on this one) — that one specific resolution is skipped, not the whole batch.

AUTH:
    This server binds to 127.0.0.1 only, but any web page open in the browser can still attempt
    to ``fetch()`` a localhost port — without a check, a page other than our own extension could
    silently trigger LLM calls or read resume content back out of the response. A shared-secret
    token gates every request instead. Set ``COVER_LETTER_SERVER_TOKEN`` in ``.env`` once and
    configure the same value in the extension. If it's unset, one is generated on first run and
    appended to ``.env`` — copy the printed value into the extension's settings.

    Call this API from the extension's background/service-worker script (not a content script)
    with ``http://127.0.0.1:<port>/*`` in ``host_permissions``. Chrome's extension fetch bypasses
    CORS outright when the origin is covered by host_permissions, but Firefox still sends a real
    CORS preflight (``OPTIONS``) for non-"simple" requests — a JSON body and a custom
    ``Authorization`` header each independently trigger one — even from a privileged background
    script. ``do_OPTIONS`` below answers that preflight, and every response carries
    ``Access-Control-Allow-Origin`` so both browsers can read it.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
import unicodedata
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from utils.cover_letter import (
    _MAX_COVER_LETTER_FILENAME_STEM_CHARS,
    _sanitize_cover_letter_filename_segment,
    CoverLetterGenerator,
    find_cover_letter_docx_for_job_id,
    read_cover_letter_docx,
    unique_docx_path,
    write_cover_letter_docx,
)
from utils.dspy_lm import configure_dspy
from utils.extension_process_service import process_extension_request, resolve_conflicts
from utils.form_fill_rules import DISCARD_APPLY, FormFillRulesEngine
from utils.output_paths import COVERLETTERS_DIR, FORM_FILL_RULES_DIR, OUTPUT_DIR
from utils.resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE, load_or_build_resume
from utils.s3_log_sync import RESOURCE_COVERLETTERS, RESOURCE_FORM_FILL_RULES, sync_log_download
from utils.s3_outputs import resolve_output_dir

log = logging.getLogger(__name__)

_TOKEN_ENV_VAR = "COVER_LETTER_SERVER_TOKEN"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8743
_MAX_BODY_BYTES = 200_000
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)", re.IGNORECASE)


def _load_or_create_token() -> str:
    token = (os.environ.get(_TOKEN_ENV_VAR) or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    env_path = Path(".env")
    with env_path.open("a", encoding="utf-8") as f:
        f.write(f"\n{_TOKEN_ENV_VAR}={token}\n")
    print(
        f"Generated a new extension server token and saved it to {env_path.resolve()}.\n"
        f"Configure the browser extension with this token:\n\n    {token}\n"
    )
    return token


def _default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _coverletters_root() -> Path:
    return resolve_output_dir(OUTPUT_DIR) / COVERLETTERS_DIR.name


def _form_fill_rules_root() -> Path:
    return resolve_output_dir(OUTPUT_DIR) / FORM_FILL_RULES_DIR.name


def _job_id_for(*, company: str, title: str, url: str, explicit: str) -> str:
    """Deterministic docx-filename id: explicit id > LinkedIn job id from URL > hash of url/company+title."""
    if explicit:
        return explicit
    m = _LINKEDIN_JOB_ID_RE.search(url)
    if m:
        return m.group(1)
    basis = url or f"{company}|{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def _normalize_whitespace_for_match(text: str) -> str:
    # NFKC folds non-breaking spaces (U+00A0, common in LinkedIn's rendered DOM
    # text) and similar compatibility characters down to plain ASCII space, so
    # a company name scraped from the DOM still matches one embedded in a
    # title pulled from document.title even if their whitespace differs.
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _drop_redundant_company_mentions(company: str, title: str) -> str:
    """Some postings (e.g. "General Interest Application" listings) title themselves with the
    company name already leading and/or trailing the title — filenaming that verbatim repeats
    the company name after it's already been used as the company segment. Strips a leading
    and/or trailing company-name mention (looping, in case both are present); keeps the original
    title if nothing meaningful would remain."""
    norm_company = _normalize_whitespace_for_match(company).casefold()
    if not norm_company:
        return title

    working = _normalize_whitespace_for_match(title)
    changed = True
    while changed:
        changed = False
        lower = working.casefold()
        if lower.startswith(norm_company):
            working = working[len(norm_company):].lstrip(" \t-–—:").strip()
            changed = True
        lower = working.casefold()
        if lower.endswith(norm_company):
            working = working[: len(working) - len(norm_company)].rstrip(" \t-–—:").strip()
            changed = True

    return working or title


def _extension_cover_letter_stem(*, company: str, title: str, job_id: str) -> str:
    """Filename stem for extension-generated cover letters: ``{company}_{title}_{job_id}``.

    Unlike ``cover_letter_docx_stem`` (used by the Selenium auto-apply flow and ``main.py
    --filter``), this has no ``{site}_`` prefix — these already live in the user's Downloads
    folder rather than a site-organized output directory, so the label isn't useful there.
    """
    deduped_title = _drop_redundant_company_mentions(company, title)
    co = _sanitize_cover_letter_filename_segment(company or "Company", 55)
    ti = _sanitize_cover_letter_filename_segment(deduped_title or "Position", 75)
    raw_id = str(job_id or "job").strip()
    jid = re.sub(r"[^\w\-.]+", "_", raw_id).strip("_")
    jid = (jid or "job")[:48]
    stem = "_".join((co, ti, jid))
    stem = re.sub(r"_+", "_", stem)
    return stem[:_MAX_COVER_LETTER_FILENAME_STEM_CHARS].rstrip("._-")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    resume: dict[str, Any]
    cover_gen: CoverLetterGenerator
    rules_engine: FormFillRulesEngine
    token: str
    downloads_dir: Path

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except OSError as e:
            # The client (browser tab) went away before we could respond -- e.g. the user
            # navigated away or closed the tab mid-request. Whatever work this response was
            # reporting on already happened; there's just no one left to deliver it to. Not worth
            # an unhandled traceback in the console.
            log.info("Client disconnected before response could be sent: %s", e)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix):
            return False
        return hmac.compare_digest(auth[len(prefix):].strip(), self.token)

    def do_OPTIONS(self) -> None:
        # CORS preflight is unauthenticated by design — the browser is only
        # asking permission to send the real request's headers, it hasn't
        # attached them yet, so gating this on _authorized() would make every
        # preflight fail and the real GET/POST would never be sent. Auth is
        # still enforced there.
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path not in (
            "/cover-letter", "/answer-fields", "/process-extension", "/process-extension/resolve",
        ):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing or oversized body"})
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
            return

        if self.path == "/cover-letter":
            self._handle_cover_letter(data)
        elif self.path == "/answer-fields":
            self._handle_answer_fields(data)
        elif self.path == "/process-extension":
            self._handle_process_extension(data)
        else:
            self._handle_process_extension_resolve(data)

    def _handle_cover_letter(self, data: dict[str, Any]) -> None:
        title = str(data.get("title") or "").strip()
        company = str(data.get("company") or "").strip()
        description = str(data.get("description") or "").strip()
        if not title or not company:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "title and company are required"})
            return

        job_id = _job_id_for(
            company=company,
            title=title,
            url=str(data.get("url") or ""),
            explicit=str(data.get("job_id") or "").strip(),
        )
        try:
            sync_log_download(RESOURCE_COVERLETTERS, root=_coverletters_root())
        except Exception:
            log.warning("Could not sync cover letters — serving from local state.", exc_info=True)
        existing = find_cover_letter_docx_for_job_id(job_id)
        reused = False
        if existing is not None:
            try:
                cover_letter = read_cover_letter_docx(existing)
            except Exception:
                log.warning(
                    "Could not read existing cover letter %s — generating a new one",
                    existing,
                    exc_info=True,
                )
                cover_letter = ""
            else:
                if cover_letter.strip():
                    reused = True
                    log.info(
                        "Reusing existing cover letter for job_id=%s from %s (skipping OpenAI)",
                        job_id,
                        existing,
                    )
                else:
                    log.warning(
                        "Existing cover letter %s was empty — generating a new one",
                        existing,
                    )

        if not reused:
            job = {"title": title, "company": company, "description": description}
            cover_letter = self.cover_gen.generate(self.resume, job)

        docx_path: str | None = None
        if data.get("save_docx", True):
            stem = _extension_cover_letter_stem(company=company, title=title, job_id=job_id)
            path = unique_docx_path(self.downloads_dir, stem)
            if reused and existing is not None:
                try:
                    shutil.copy2(existing, path)
                except OSError:
                    log.warning(
                        "Could not copy %s to Downloads — writing text instead",
                        existing,
                        exc_info=True,
                    )
                    write_cover_letter_docx(cover_letter, path)
            else:
                write_cover_letter_docx(cover_letter, path)
            docx_path = str(path.resolve())

        self._send_json(HTTPStatus.OK, {"cover_letter": cover_letter, "docx_path": docx_path})

    def _answer_one_field(self, label: str, field_type: str) -> dict[str, Any]:
        """Dispatch to the FormFillRulesEngine method matching this field type.

        Matching a candidate answer back to the right DOM option/radio/checkbox is the caller's
        job (same division of labor form_filler.py already has) — the engine only ever deals in
        label strings. ``"select"``/``"radio"``/``"checkbox_group"`` return the full priority-
        ordered candidate list (a rule may offer several acceptable answers); ``"text"``/
        ``"textarea"`` return at most one — free text has no "does the field offer this option"
        check for a fallback to be meaningful against.
        """
        if field_type == "text":
            v = self.rules_engine.answer_text_field(label, self.resume)
            values = [v] if v is not None else []
        elif field_type == "textarea":
            # No cover-letter text: this endpoint has no job/company context
            # on an arbitrary page, so cover_letter_* result-type rules just
            # yield nothing here rather than erroring.
            v = self.rules_engine.answer_textarea(label, "")
            values = [v] if v is not None else []
        elif field_type == "select":
            values = self.rules_engine.answer_select_candidates(label)
        elif field_type == "radio":
            values = self.rules_engine.screening_yes_no_candidates(label)
        elif field_type == "checkbox_group":
            values = self.rules_engine.checkbox_group_choice_candidates(label)
        else:
            values = []

        if DISCARD_APPLY in values:
            return {"value": None, "values": [], "flag": "discard"}
        return {"value": values[0] if values else None, "values": values, "flag": None}

    def _handle_answer_fields(self, data: dict[str, Any]) -> None:
        fields = data.get("fields")
        if not isinstance(fields, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "fields must be a list"})
            return

        try:
            sync_log_download(RESOURCE_FORM_FILL_RULES, root=_form_fill_rules_root())
            self.__class__.rules_engine = FormFillRulesEngine(apply_source=None)
        except Exception:
            log.warning("Could not sync/reload form-fill rules — serving existing rules.", exc_info=True)

        answers: list[dict[str, Any]] = []
        for field in fields:
            label = str((field or {}).get("label") or "").strip() if isinstance(field, dict) else ""
            field_type = str((field or {}).get("type") or "").strip() if isinstance(field, dict) else ""
            if not label:
                answers.append({"value": None, "flag": None})
                continue
            answers.append(self._answer_one_field(label, field_type))

        self._send_json(HTTPStatus.OK, {"answers": answers})

    def _handle_process_extension(self, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id") or "").strip()
        if not request_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_id is required"})
            return
        try:
            result = process_extension_request(
                request_id=request_id,
                saved_jobs_text=str(data.get("saved_jobs_text") or ""),
                saved_questions_text=str(data.get("saved_questions_text") or ""),
                dry_run=bool(data.get("dry_run", False)),
            )
        except Exception:
            log.exception("process-extension request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "processing failed"})
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_process_extension_resolve(self, data: dict[str, Any]) -> None:
        request_id = str(data.get("request_id") or "").strip()
        server_request_id = str(data.get("server_request_id") or "").strip()
        resolutions = data.get("resolutions")
        if not request_id or not server_request_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_id and server_request_id are required"})
            return
        if not isinstance(resolutions, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "resolutions must be a list"})
            return
        try:
            result = resolve_conflicts(
                request_id=request_id,
                server_request_id=server_request_id,
                resolutions=[r for r in resolutions if isinstance(r, dict)],
            )
        except Exception:
            log.exception("process-extension/resolve request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "processing failed"})
            return
        self._send_json(HTTPStatus.OK, result)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local API server exposing the cover letter generator and form-fill answers to the browser extension."
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Bind address (default: {_DEFAULT_HOST}). Must stay loopback-only.",
    )
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--resume", type=Path, default=DEFAULT_RESUME_FILE)
    parser.add_argument("--resume-cache", type=Path, default=DEFAULT_RESUME_CACHE_PATH)
    parser.add_argument("--force-resume-parse", action="store_true")
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Where to save generated .docx cover letters (default: your Downloads folder).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    load_dotenv()

    if args.host not in _LOOPBACK_HOSTS:
        print(
            "Refusing to bind to a non-loopback host — this server has no business being "
            "reachable off this machine.",
            file=sys.stderr,
        )
        return 1

    token = _load_or_create_token()
    configure_dspy()
    resume = load_or_build_resume(args.resume, args.resume_cache, force_reparse=args.force_resume_parse)
    log.info(
        "Loaded resume profile: %d skills, %d roles",
        len(resume.get("skills") or []),
        len(resume.get("experience") or []),
    )

    downloads_dir = (args.downloads_dir or _default_downloads_dir()).expanduser().resolve()
    log.info("Generated cover letters will be saved to: %s", downloads_dir)

    _Handler.resume = resume
    _Handler.cover_gen = CoverLetterGenerator()
    # apply_source=None: /answer-fields isn't LinkedIn/Greenhouse-specific,
    # it's meant to run on any page, so the two site-specific rule types
    # (literal_from_apply_source / choose_label_from_apply_source) are unused.
    _Handler.rules_engine = FormFillRulesEngine(apply_source=None)
    _Handler.token = token
    _Handler.downloads_dir = downloads_dir

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    log.info("Extension server listening on http://%s:%d (Ctrl+C to stop)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
