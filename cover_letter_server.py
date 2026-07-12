"""
Local HTTP API that lets the browser extension request a tailored cover letter on demand,
instead of only relying on cover letters pre-generated during a ``main.py --filter`` run.

Run from repo root::

    python cover_letter_server.py
    python cover_letter_server.py --port 8743 --resume resume.pdf

Endpoints (all require ``Authorization: Bearer <token>``; see AUTH below)::

    GET  /health
        -> {"status": "ok"}

    POST /cover-letter
        body: {"title": str, "company": str, "description": str,
                "url": str?, "job_id": str?, "save_docx": bool? (default true)}
        -> {"cover_letter": str, "docx_path": str | null}

    When ``save_docx`` is true (default), the .docx is written straight to the user's Downloads
    folder (override with ``--downloads-dir``) so it's already sitting where a file-upload dialog
    on the job application page opens — no need to dig through ``output/`` to attach it. Unlike
    cover letters generated during a ``main.py --filter`` run, these are not auto-deleted by
    ``process_extension.py`` when the job is later marked applied; Downloads is a folder the user
    manages themselves.

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
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from utils.cover_letter import (
    CoverLetterGenerator,
    cover_letter_docx_path_unique,
    write_cover_letter_docx,
)
from utils.dspy_lm import configure_dspy
from utils.resume_cache import DEFAULT_RESUME_CACHE_PATH, DEFAULT_RESUME_FILE, load_or_build_resume

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
        f"Generated a new cover letter server token and saved it to {env_path.resolve()}.\n"
        f"Configure the browser extension with this token:\n\n    {token}\n"
    )
    return token


def _default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _job_id_for(*, company: str, title: str, url: str, explicit: str) -> str:
    """Deterministic docx-filename id: explicit id > LinkedIn job id from URL > hash of url/company+title."""
    if explicit:
        return explicit
    m = _LINKEDIN_JOB_ID_RE.search(url)
    if m:
        return m.group(1)
    basis = url or f"{company}|{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    resume: dict[str, Any]
    cover_gen: CoverLetterGenerator
    token: str
    downloads_dir: Path

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
        if self.path != "/cover-letter":
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

        title = str(data.get("title") or "").strip()
        company = str(data.get("company") or "").strip()
        description = str(data.get("description") or "").strip()
        if not title or not company:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "title and company are required"})
            return

        job = {"title": title, "company": company, "description": description}
        cover_letter = self.cover_gen.generate(self.resume, job)

        docx_path: str | None = None
        if data.get("save_docx", True):
            job_id = _job_id_for(
                company=company,
                title=title,
                url=str(data.get("url") or ""),
                explicit=str(data.get("job_id") or "").strip(),
            )
            path = cover_letter_docx_path_unique(
                self.downloads_dir, site="filter", company=company, title=title, job_id=job_id
            )
            write_cover_letter_docx(cover_letter, path)
            docx_path = str(path.resolve())

        self._send_json(HTTPStatus.OK, {"cover_letter": cover_letter, "docx_path": docx_path})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local API server exposing the cover letter generator to the browser extension."
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
    _Handler.token = token
    _Handler.downloads_dir = downloads_dir

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    log.info("Cover letter server listening on http://%s:%d (Ctrl+C to stop)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
