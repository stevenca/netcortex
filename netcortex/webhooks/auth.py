"""Shared webhook authentication and request-hardening helpers.

Every inbound webhook receiver (Meraki, Catalyst Center, Nexus Dashboard,
the generic catch-all, and HTTP telemetry push) shares the same security
requirements:

* **Fail closed.** A receiver for a tenant whose signing secret is not
  configured must *reject* the request, not silently accept it. The only
  exception is an explicit, default-off bootstrap switch
  (``webhook_allow_unsigned``) for standing up a brand-new integration
  before its secret is provisioned.
* **Bounded body size.** Reject oversized requests (413) before parsing,
  so an unauthenticated caller cannot exhaust memory.
* **Constant-time secret comparison.** Never leak secret bytes through a
  timing side channel.
* **Replay resistance.** When the payload carries a trustworthy
  timestamp, reject stale requests.

Centralizing these here means a new receiver gets them by calling a
couple of helpers, and a security fix lands in one place for every
vendor at once (the dev9 incident — where one wrong backend-method call
silently disabled HMAC verification — is exactly the class of bug this
consolidation prevents from recurring per-vendor).

All settings are read defensively: if :func:`get_settings` has not run
yet (e.g. in a unit test that imports a handler directly), we fall back
to the **secure** defaults (fail closed, 1 MiB cap, 300 s replay window).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import structlog
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

# Secure fallbacks used when Settings is not initialized (unit tests that
# import a handler in isolation). These mirror the config.py defaults.
_DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
_DEFAULT_REPLAY_WINDOW_S = 300


def _allow_unsigned() -> bool:
    try:
        from netcortex.config import get_settings
        return bool(get_settings().webhook_allow_unsigned)
    except Exception:
        # Fail closed when config is unavailable.
        return False


def _max_body_bytes() -> int:
    try:
        from netcortex.config import get_settings
        val = int(get_settings().webhook_max_body_bytes)
        return val if val > 0 else _DEFAULT_MAX_BODY_BYTES
    except Exception:
        return _DEFAULT_MAX_BODY_BYTES


def _replay_window_seconds() -> int:
    try:
        from netcortex.config import get_settings
        return int(get_settings().webhook_replay_window_seconds)
    except Exception:
        return _DEFAULT_REPLAY_WINDOW_S


# ---------------------------------------------------------------------------
# Body-size enforcement (F3)
# ---------------------------------------------------------------------------


def enforce_content_length(content_length: str | int | None) -> None:
    """Reject (413) based on the ``Content-Length`` header before reading
    the body. This is the cheap first gate — it lets us refuse a large
    upload without buffering it. ``await request.body()`` is still bounded
    by :func:`enforce_body_size` afterwards in case the header lies or is
    absent (the ingress ``proxy-body-size`` is the third, outermost gate).
    """
    if content_length is None:
        return
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        return
    cap = _max_body_bytes()
    if length > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body exceeds {cap} bytes",
        )


def enforce_body_size(body: bytes) -> None:
    """Reject (413) after reading the body — defends against a missing or
    dishonest ``Content-Length`` header."""
    cap = _max_body_bytes()
    if len(body) > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body exceeds {cap} bytes",
        )


# ---------------------------------------------------------------------------
# Fail-closed gate for unconfigured secrets (F1, F7)
# ---------------------------------------------------------------------------


def reject_if_unsigned(*, kind: str, instance_name: str) -> None:
    """Enforce fail-closed behavior when no signing secret is configured.

    Called by a handler when its secret lookup returned ``None``. Raises
    503 unless the operator has explicitly enabled the
    ``webhook_allow_unsigned`` bootstrap switch, in which case it logs a
    loud warning and returns (allowing the request through unverified).
    """
    if _allow_unsigned():
        log.warning(
            "webhook.unsigned_accepted",
            kind=kind,
            instance=instance_name,
            hint="webhook_allow_unsigned is ENABLED — this request was not "
                 "authenticated. Provision the secret and disable the flag.",
        )
        return
    log.warning(
        "webhook.rejected_no_secret",
        kind=kind,
        instance=instance_name,
        hint=f"No signing secret configured at netcortex/webhooks/{kind}/"
             f"{instance_name}. Store it, or set webhook_allow_unsigned=true "
             f"to bootstrap (NOT for production).",
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Webhook receiver not provisioned (no signing secret configured)",
    )


# ---------------------------------------------------------------------------
# Constant-time secret comparison (F1, F5, F7)
# ---------------------------------------------------------------------------


def verify_hmac_sha256(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify an HMAC-SHA256 hex-digest signature in constant time.

    Tolerates an optional ``sha256=`` prefix (newer Meraki firmware).
    """
    if not signature_header:
        return False
    received = signature_header.removeprefix("sha256=").strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def verify_shared_token(provided: str | None, expected: str) -> bool:
    """Constant-time equality for a shared bearer/header token."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _setting(name: str, default: str = "") -> str:
    try:
        from netcortex.config import get_settings
        return getattr(get_settings(), name, default) or default
    except Exception:
        return default


def require_admin_token(provided: str | None) -> None:
    """Gate an administrative / high-privilege endpoint behind ``api_secret``.

    Used for the generic webhook catch-all (which can trigger discovery on
    *any* adapter — F4 amplification) and the telemetry SSE monitor (which
    streams operational data — F6). Fails closed: if no ``api_secret`` is
    configured the endpoint is disabled (503) unless the bootstrap switch
    is set.
    """
    secret = _setting("api_secret")
    if not secret:
        if _allow_unsigned():
            log.warning(
                "webhook.admin_unsigned_accepted",
                hint="api_secret unset and webhook_allow_unsigned enabled — "
                     "administrative endpoint is OPEN.",
            )
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Administrative endpoint disabled (no api_secret configured)",
        )
    if not verify_shared_token(provided, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing administrative token",
        )


def require_telemetry_token(provided: str | None) -> None:
    """Gate the HTTP telemetry-push ingest behind ``telemetry_secret`` (F6).

    Network devices send a shared ``X-Telemetry-Token`` header. Fails closed
    unless the bootstrap switch is set.
    """
    secret = _setting("telemetry_secret")
    if not secret:
        if _allow_unsigned():
            log.warning(
                "telemetry.unsigned_accepted",
                hint="telemetry_secret unset and webhook_allow_unsigned enabled "
                     "— telemetry ingest is OPEN.",
            )
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry ingest not provisioned (no telemetry_secret configured)",
        )
    if not verify_shared_token(provided, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing telemetry token",
        )


# ---------------------------------------------------------------------------
# Replay / freshness (F9)
# ---------------------------------------------------------------------------


def is_timestamp_fresh(ts_iso: str | None) -> bool:
    """Return True if ``ts_iso`` is within the configured replay window.

    Returns True when:
      * the replay window is disabled (0), or
      * no timestamp is supplied (we can't judge freshness; the HMAC
        signature is still the primary control), or
      * the timestamp parses and is within ``+window`` / ``-window``.

    Returns False only when a timestamp is present, parses, and falls
    outside the window. A small positive skew is tolerated for clock
    drift between the vendor cloud and this host.
    """
    window = _replay_window_seconds()
    if window <= 0 or not ts_iso:
        return True
    parsed = _parse_iso8601(ts_iso)
    if parsed is None:
        # Unparseable timestamp — don't hard-fail on a format we don't
        # recognize; the signature remains the authority.
        return True
    now = datetime.now(tz=timezone.utc)
    age = (now - parsed).total_seconds()
    # Reject if older than the window, or implausibly far in the future.
    return -window <= age <= window


def _parse_iso8601(value: str) -> datetime | None:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "enforce_content_length",
    "enforce_body_size",
    "reject_if_unsigned",
    "require_admin_token",
    "require_telemetry_token",
    "verify_hmac_sha256",
    "verify_shared_token",
    "is_timestamp_fresh",
]
