"""
Per-device "profile" storage for cookies the browser extension uploads when the user presses a
"Connect to X" button (LinkedIn today; other sites can register into ``_SITE_DOMAINS`` later
without changing the storage shape). See ``extension_server.py``'s ``/profile/connect`` and
``/profile/ping`` docstrings for the wire protocol.

Each profile is one JSON file, ``data/extension_profiles/<profile_id>.json``, keyed by a
server-minted id -- the extension proposes its own previously-stored id back on every connect,
but never gets to invent a *new* one: an unrecognized id is treated as "this device has never
connected before" and a fresh one is minted, same reasoning as
``utils/extension_process_service.py``'s ``server_request_id`` (an id used as a storage-key/lookup
credential has to come from the server, not from whatever the caller happened to propose, or
anyone could just guess/pick another device's id).

Deliberately separate from ``data/selenium_linkedin_cookies.json`` (main.py's own account) and
never synced to S3: unlike ``output/`` (shared application history/config across *one person's*
own devices), these cookies are account-equivalent credentials that identify a single browser's
own session -- replicating them into a shared bucket would turn one leak into many, and syncing a
snapshot to a different device doesn't even help that device (only the server ever reads this
file; the other device's own browser still isn't logged in).

Two devices connecting to the *same* underlying account (e.g. the same LinkedIn login in two
browsers) would otherwise end up as two unrelated profiles -- cookies alone can't tell, since each
device gets its own session cookie values even for one account. For sites in
``SITES_WITH_IDENTITY``, the extension also reports a stable per-account identity (a LinkedIn
profile URL, resolved via LinkedIn's own ``/in/me/`` redirect) alongside its cookies;
``find_conflicting_profile()`` uses it to detect "this identity is already connected under a
*different* profile" and stages the decision (join / merge / cancel) via
``stage_connect_conflict()``/``resolve_connect_conflict()`` rather than silently creating a
duplicate -- see ``extension_server.py``'s ``/profile/connect`` and
``/profile/connect/resolve`` docstrings for the wire protocol.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILES_DIR = Path("data/extension_profiles")

# Sites the extension is allowed to upload cookies for -- a plain allow-list keeps the domain
# check in validate_cookie() meaningful per site instead of accepting any domain string at all.
_SITE_DOMAINS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
}

SUPPORTED_SITES = frozenset(_SITE_DOMAINS)

# Sites for which the extension also reports a stable per-account "identity" (e.g. the account's
# own profile URL) alongside its cookies, so the server can tell "two devices connected to the
# *same* account" apart from "two different accounts" -- see find_conflicting_profile() and
# extension_server.py's /profile/connect docstring. A site absent from this map just never
# conflict-checks (every connect is accepted standalone, today's original behavior) until its own
# identity signal is worked out -- not a blocker for adding a new site.
_SITE_IDENTITY_PREFIXES: dict[str, str] = {
    "linkedin": "https://www.linkedin.com/in/",
}

SITES_WITH_IDENTITY = frozenset(_SITE_IDENTITY_PREFIXES)

_PENDING_CONFLICT_TTL_SECONDS = 600  # 10 minutes, same as utils/extension_process_service.py


def _profile_path(profile_id: str) -> Path:
    return PROFILES_DIR / f"{profile_id}.json"


def _is_valid_profile_id(profile_id: str) -> bool:
    # Matches the alphabet secrets.token_urlsafe() produces. profile_id is used directly in a
    # filename below, so this also guards against path traversal from a malformed client value.
    return bool(profile_id) and all(c.isalnum() or c in "-_" for c in profile_id)


def _read_profile(profile_id: str) -> dict[str, Any] | None:
    if not _is_valid_profile_id(profile_id):
        return None
    path = _profile_path(profile_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read profile %s: %s", profile_id, e)
        return None


def _write_profile(profile_id: str, doc: dict[str, Any]) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _profile_path(profile_id).write_text(json.dumps(doc, indent=2), encoding="utf-8")


def validate_cookie(item: Any, *, site: str) -> dict[str, Any]:
    """Validate/normalize one cookie dict for `site`. Raises ValueError on anything malformed or
    out of scope for that site's domain -- shape matches what
    ``utils/chrome_driver.py::load_cookies()`` reads back."""
    if not isinstance(item, dict):
        raise ValueError("each cookie must be an object")
    name = str(item.get("name") or "").strip()
    value = str(item.get("value") or "")
    domain = str(item.get("domain") or "").strip().lower().lstrip(".")
    if not name or not value:
        raise ValueError("each cookie needs a non-empty name and value")

    allowed = _SITE_DOMAINS.get(site, ())
    if not any(domain == d or domain.endswith("." + d) for d in allowed):
        raise ValueError(f"cookie {name!r} has domain {domain!r}, not valid for site {site!r}")

    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": str(item.get("domain") or "").strip(),
        "path": str(item.get("path") or "/").strip() or "/",
    }
    if item.get("secure") is not None:
        cookie["secure"] = bool(item["secure"])
    expiry = item.get("expiry")
    if expiry is not None:
        try:
            cookie["expiry"] = int(expiry)
        except (TypeError, ValueError):
            raise ValueError(f"cookie {name!r} has a non-numeric expiry")
    return cookie


def validate_identity(identity: Any, *, site: str) -> str:
    """Validate `identity` (e.g. a LinkedIn profile URL) for `site`. Raises ValueError if it
    doesn't look like that site's identity shape -- see _SITE_IDENTITY_PREFIXES."""
    s = str(identity or "").strip()
    prefix = _SITE_IDENTITY_PREFIXES.get(site)
    if not prefix or not s.lower().startswith(prefix):
        raise ValueError(f"identity does not look like a {site} profile URL")
    return s


def _delete_profile(profile_id: str) -> None:
    if not _is_valid_profile_id(profile_id):
        return
    path = _profile_path(profile_id)
    if path.is_file():
        path.unlink()


def find_conflicting_profile(*, site: str, identity: str, exclude_profile_id: str | None) -> str | None:
    """
    The id of some *other* profile that already has `site` connected with this same `identity`,
    or None if there's no such conflict. `exclude_profile_id` is the requesting device's own
    current id (if any) -- its own existing connection for the same account is a normal refresh,
    not a conflict with itself.
    """
    if not identity or not PROFILES_DIR.is_dir():
        return None
    for path in PROFILES_DIR.glob("*.json"):
        candidate_id = path.stem
        if candidate_id == exclude_profile_id:
            continue
        doc = _read_profile(candidate_id)
        if doc is None:
            continue
        site_doc = (doc.get("sites") or {}).get(site)
        if site_doc and site_doc.get("identity") == identity:
            return candidate_id
    return None


def connect_profile(
    *, profile_id: str | None, site: str, cookies: list[dict[str, Any]], identity: str | None = None
) -> str:
    """
    Store `cookies` (and `identity`, if given) for `site` under `profile_id` if it's a profile we
    recognize, otherwise mint a fresh one -- an unrecognized/absent id is treated as "first
    connect", not an error, since a brand-new device has no id yet by definition. Returns the
    authoritative profile id (only ever server-minted, never whatever the caller proposed) the
    caller must store from now on.

    Callers for a site in SITES_WITH_IDENTITY should have already checked
    find_conflicting_profile() and handled any conflict *before* calling this -- it does not
    re-check, so calling it directly would silently let two profiles claim the same identity.
    """
    doc = _read_profile(profile_id) if profile_id else None
    if doc is None:
        profile_id = secrets.token_urlsafe(24)
        doc = {"profile_id": profile_id, "created_at": time.time(), "sites": {}}

    site_entry: dict[str, Any] = {"cookies": cookies, "updated_at": time.time()}
    if identity:
        site_entry["identity"] = identity
    doc.setdefault("sites", {})[site] = site_entry
    _write_profile(profile_id, doc)
    return profile_id


def merge_profiles(*, into_profile_id: str, from_profile_id: str) -> None:
    """
    Copy every site `from_profile_id` has that `into_profile_id` lacks, then delete
    `from_profile_id`. A site already present on `into_profile_id` is left untouched -- it's the
    surviving profile, so its own (necessarily fresher, just-connected) data wins on overlap
    rather than being clobbered by the profile being retired.
    """
    into_doc = _read_profile(into_profile_id)
    from_doc = _read_profile(from_profile_id)
    if into_doc is None or from_doc is None:
        return
    into_sites = into_doc.setdefault("sites", {})
    for site, site_doc in (from_doc.get("sites") or {}).items():
        into_sites.setdefault(site, site_doc)
    _write_profile(into_profile_id, into_doc)
    _delete_profile(from_profile_id)


def disconnect_site(profile_id: str, site: str) -> bool:
    """Remove `site`'s stored cookies from `profile_id`, leaving the profile id itself (and any
    other connected sites) untouched -- disconnecting one site is not the same as forgetting this
    device. Returns whether `site` was actually connected."""
    doc = _read_profile(profile_id)
    if doc is None:
        return False
    sites = doc.get("sites") or {}
    if site not in sites:
        return False
    del sites[site]
    doc["sites"] = sites
    _write_profile(profile_id, doc)
    return True


def profile_sites(profile_id: str) -> list[str] | None:
    """Site names with stored cookies for `profile_id`, or None if the profile is unknown."""
    doc = _read_profile(profile_id)
    if doc is None:
        return None
    return sorted((doc.get("sites") or {}).keys())


def load_site_cookies(profile_id: str, site: str) -> list[dict[str, Any]] | None:
    """Cookies stored for `site` under `profile_id`, or None if not found -- for whenever a future
    consumer wants to act as this profile's account (e.g. ``load_cookies(driver, ...)`` after
    writing this list out, or a helper that does so directly)."""
    doc = _read_profile(profile_id)
    if doc is None:
        return None
    site_doc = (doc.get("sites") or {}).get(site)
    if site_doc is None:
        return None
    return site_doc.get("cookies") or []


# --- Pending connect-conflicts -----------------------------------------------------------------
#
# When a connect attempt's identity matches an *already-connected* profile, the connect is not
# applied yet -- the attempted cookies/identity are stashed here (in-memory only, like
# utils/extension_process_service.py's own `_PENDING` dict) under a freshly-minted id, and the
# extension is expected to show the user a choice (join the existing profile / merge the two /
# stay as-is) before anything is written to disk. This is exactly the same shape as that other
# module's pending-conflict handling, kept separate here since it's specific to profile identity
# rather than form-fill-rule conflicts.


@dataclass
class _PendingConnectConflict:
    site: str
    cookies: list[dict[str, Any]]
    identity: str
    requesting_profile_id: str | None
    existing_profile_id: str
    created_at: float = field(default_factory=time.time)


_PENDING_CONFLICTS: dict[str, _PendingConnectConflict] = {}
_PENDING_CONFLICTS_LOCK = threading.Lock()


def _sweep_expired_conflicts() -> None:
    now = time.time()
    with _PENDING_CONFLICTS_LOCK:
        expired = [
            k for k, v in _PENDING_CONFLICTS.items()
            if now - v.created_at > _PENDING_CONFLICT_TTL_SECONDS
        ]
        for k in expired:
            del _PENDING_CONFLICTS[k]


def stage_connect_conflict(
    *,
    site: str,
    cookies: list[dict[str, Any]],
    identity: str,
    requesting_profile_id: str | None,
    existing_profile_id: str,
) -> str:
    """Stash a detected conflict and return a fresh pending id for the extension to resolve via
    resolve_connect_conflict(). Nothing is written to any profile file yet."""
    _sweep_expired_conflicts()
    pending_id = secrets.token_urlsafe(16)
    with _PENDING_CONFLICTS_LOCK:
        _PENDING_CONFLICTS[pending_id] = _PendingConnectConflict(
            site=site,
            cookies=cookies,
            identity=identity,
            requesting_profile_id=requesting_profile_id,
            existing_profile_id=existing_profile_id,
        )
    return pending_id


def resolve_connect_conflict(pending_id: str, choice: str) -> dict[str, Any]:
    """
    Apply the user's choice for a previously-staged conflict:
      - "join": drop the requesting device's own profile entirely and adopt the existing one.
      - "merge": connect the requester's own profile as normal (minting one if it never had any),
        then absorb every site the existing profile has that the requester's doesn't, and drop
        the existing profile.
      - "cancel": discard the pending conflict; nothing changes on either profile.
    Returns {"status": ..., "profile_id": ...} on success or {"error": ...} for an unknown/expired
    pending_id or choice.
    """
    _sweep_expired_conflicts()
    with _PENDING_CONFLICTS_LOCK:
        pending = _PENDING_CONFLICTS.pop(pending_id, None)
    if pending is None:
        return {"error": "unknown or expired pending_id"}

    if choice == "cancel":
        return {"status": "cancelled"}

    if choice == "join":
        if pending.requesting_profile_id:
            _delete_profile(pending.requesting_profile_id)
        return {"status": "joined", "profile_id": pending.existing_profile_id}

    if choice == "merge":
        current_id = connect_profile(
            profile_id=pending.requesting_profile_id,
            site=pending.site,
            cookies=pending.cookies,
            identity=pending.identity,
        )
        merge_profiles(into_profile_id=current_id, from_profile_id=pending.existing_profile_id)
        return {"status": "merged", "profile_id": current_id}

    return {"error": f"unknown choice: {choice!r}"}
