"""Unit tests for the shared webhook auth/hardening helpers (0.8.0-dev10)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from netcortex.webhooks import auth


def _hexsig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── HMAC verification ──────────────────────────────────────────────────────


def test_verify_hmac_valid() -> None:
    body = b'{"a":1}'
    sig = _hexsig(body, "sek")
    assert auth.verify_hmac_sha256(body, sig, "sek") is True


def test_verify_hmac_accepts_sha256_prefix() -> None:
    body = b'{"a":1}'
    sig = "sha256=" + _hexsig(body, "sek")
    assert auth.verify_hmac_sha256(body, sig, "sek") is True


def test_verify_hmac_rejects_wrong_secret_and_missing_header() -> None:
    body = b'{"a":1}'
    assert auth.verify_hmac_sha256(body, _hexsig(body, "other"), "sek") is False
    assert auth.verify_hmac_sha256(body, None, "sek") is False


# ── Shared-token comparison ────────────────────────────────────────────────


def test_verify_shared_token() -> None:
    assert auth.verify_shared_token("tok", "tok") is True
    assert auth.verify_shared_token("nope", "tok") is False
    assert auth.verify_shared_token(None, "tok") is False
    assert auth.verify_shared_token("tok", "") is False


# ── Body-size guards (F3) ──────────────────────────────────────────────────


def test_enforce_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_max_body_bytes", lambda: 100)
    auth.enforce_content_length("50")          # under cap → no raise
    auth.enforce_content_length(None)           # absent → no raise
    auth.enforce_content_length("not-a-number")  # unparseable → no raise
    with pytest.raises(HTTPException) as ei:
        auth.enforce_content_length("101")
    assert ei.value.status_code == 413


def test_enforce_body_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_max_body_bytes", lambda: 10)
    auth.enforce_body_size(b"x" * 10)
    with pytest.raises(HTTPException) as ei:
        auth.enforce_body_size(b"x" * 11)
    assert ei.value.status_code == 413


# ── Fail-closed gate (F1/F7) ───────────────────────────────────────────────


def test_reject_if_unsigned_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_allow_unsigned", lambda: False)
    with pytest.raises(HTTPException) as ei:
        auth.reject_if_unsigned(kind="meraki", instance_name="t1")
    assert ei.value.status_code == 503


def test_reject_if_unsigned_allows_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_allow_unsigned", lambda: True)
    auth.reject_if_unsigned(kind="meraki", instance_name="t1")  # no raise


# ── Admin / telemetry gates ────────────────────────────────────────────────


def test_require_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_setting", lambda name, default="": "adm" if name == "api_secret" else default)
    auth.require_admin_token("adm")  # no raise
    with pytest.raises(HTTPException) as ei:
        auth.require_admin_token("wrong")
    assert ei.value.status_code == 401


def test_require_admin_token_disabled_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_setting", lambda name, default="": default)
    monkeypatch.setattr(auth, "_allow_unsigned", lambda: False)
    with pytest.raises(HTTPException) as ei:
        auth.require_admin_token("anything")
    assert ei.value.status_code == 503


def test_require_telemetry_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_setting", lambda name, default="": "tel" if name == "telemetry_secret" else default)
    auth.require_telemetry_token("tel")
    with pytest.raises(HTTPException) as ei:
        auth.require_telemetry_token(None)
    assert ei.value.status_code == 401


# ── Replay / freshness (F9) ────────────────────────────────────────────────


def test_is_timestamp_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_replay_window_seconds", lambda: 300)
    now = datetime.now(tz=timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert auth.is_timestamp_fresh(fresh) is True
    assert auth.is_timestamp_fresh(stale) is False
    assert auth.is_timestamp_fresh(None) is True          # nothing to judge
    assert auth.is_timestamp_fresh("garbage") is True       # unparseable → defer to HMAC


def test_is_timestamp_fresh_window_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_replay_window_seconds", lambda: 0)
    stale = (datetime.now(tz=timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert auth.is_timestamp_fresh(stale) is True
