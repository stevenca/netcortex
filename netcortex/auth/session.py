"""Server-side session store for SAML-authenticated UI sessions.

Design follows the session-management hardening guidance:

* **Opaque, server-issued ids.** The cookie holds only a CSPRNG token
  (``secrets.token_urlsafe(32)`` → 256 bits). No PII or privileges live
  in the cookie; the authenticated subject/email/groups are kept in
  Redis, keyed by the token.
* **Secure cookie flags.** ``Secure`` + ``HttpOnly`` + ``SameSite=Lax``,
  ``Path=/``, non-persistent (no ``Max-Age`` → cleared on browser close).
* **Idle + absolute timeouts.** Idle TTL is enforced via Redis key
  expiry and slid forward on each request; the absolute lifetime is
  checked against a stored ``created_at`` and cannot be extended.
* **No raw ids in logs.** Lifecycle events log a short salted hash of
  the session id, never the token itself.

Redis is already a hard dependency (job-queue coordination); we reuse a
per-event-loop client, mirroring ``netcortex/ingest/queue.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_SESSION_PREFIX = "netcortex:session:"

_clients: dict[int, Any] = {}
_clients_lock = asyncio.Lock()


async def _redis() -> Any:
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover - redis is a base dependency
        raise RuntimeError("redis package not installed — pip install redis>=5") from None

    loop_id = id(asyncio.get_running_loop())
    async with _clients_lock:
        cli = _clients.get(loop_id)
        if cli is not None:
            return cli
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        cli = aioredis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        await cli.ping()
        _clients[loop_id] = cli
        return cli


def _hashed(sid: str) -> str:
    """Short, non-reversible tag for logging (never log the raw id)."""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]


def _idle_ttl() -> int:
    try:
        from netcortex.config import get_settings
        return int(get_settings().session_idle_timeout_seconds)
    except Exception:
        return 1800


def _absolute_ttl() -> int:
    try:
        from netcortex.config import get_settings
        return int(get_settings().session_absolute_timeout_seconds)
    except Exception:
        return 28800


async def create_session(
    *,
    subject: str,
    email: str = "",
    groups: list[str] | None = None,
    name_id: str = "",
    session_index: str = "",
) -> str:
    """Create a server-side session and return its opaque id."""
    sid = secrets.token_urlsafe(32)
    record = {
        "subject": subject,
        "email": email,
        "groups": groups or [],
        "name_id": name_id,
        "session_index": session_index,
        "created_at": int(time.time()),
    }
    cli = await _redis()
    await cli.set(_SESSION_PREFIX + sid, json.dumps(record), ex=_idle_ttl())
    log.info("session.created", sid=_hashed(sid), subject=subject)
    return sid


async def load_session(sid: str | None) -> dict[str, Any] | None:
    """Return the session record for ``sid``, or None if missing/expired.

    Enforces the absolute lifetime and slides the idle TTL forward on a
    valid hit.
    """
    if not sid:
        return None
    cli = await _redis()
    raw = await cli.get(_SESSION_PREFIX + sid)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        await cli.delete(_SESSION_PREFIX + sid)
        return None

    created = int(record.get("created_at", 0))
    if created and (time.time() - created) > _absolute_ttl():
        await cli.delete(_SESSION_PREFIX + sid)
        log.info("session.expired_absolute", sid=_hashed(sid))
        return None

    # Slide the idle window forward.
    await cli.expire(_SESSION_PREFIX + sid, _idle_ttl())
    return record


async def destroy_session(sid: str | None) -> None:
    if not sid:
        return
    cli = await _redis()
    await cli.delete(_SESSION_PREFIX + sid)
    log.info("session.destroyed", sid=_hashed(sid))


# ── Cookie helpers ─────────────────────────────────────────────────────────


def _cookie_name() -> str:
    try:
        from netcortex.config import get_settings
        return get_settings().session_cookie_name or "nc_session"
    except Exception:
        return "nc_session"


def _cookie_secure() -> bool:
    try:
        from netcortex.config import get_settings
        return bool(get_settings().session_cookie_secure)
    except Exception:
        return True


def read_session_id(request: Any) -> str | None:
    return request.cookies.get(_cookie_name())


def set_session_cookie(response: Any, sid: str) -> None:
    """Set the session cookie with hardened flags (non-persistent)."""
    response.set_cookie(
        key=_cookie_name(),
        value=sid,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(key=_cookie_name(), path="/")


def _reset_clients_for_tests() -> None:
    _clients.clear()


__all__ = [
    "create_session",
    "load_session",
    "destroy_session",
    "read_session_id",
    "set_session_cookie",
    "clear_session_cookie",
]
