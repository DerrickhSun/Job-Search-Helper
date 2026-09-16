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
"""

from __future__ import annotations

import json
import logging
import secrets
import time
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


def connect_profile(*, profile_id: str | None, site: str, cookies: list[dict[str, Any]]) -> str:
    """
    Store `cookies` for `site` under `profile_id` if it's a profile we recognize, otherwise mint a
    fresh one -- an unrecognized/absent id is treated as "first connect", not an error, since a
    brand-new device has no id yet by definition. Returns the authoritative profile id (only ever
    server-minted, never whatever the caller proposed) the caller must store from now on.
    """
    doc = _read_profile(profile_id) if profile_id else None
    if doc is None:
        profile_id = secrets.token_urlsafe(24)
        doc = {"profile_id": profile_id, "created_at": time.time(), "sites": {}}

    doc.setdefault("sites", {})[site] = {"cookies": cookies, "updated_at": time.time()}
    _write_profile(profile_id, doc)
    return profile_id


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
