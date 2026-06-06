"""HTTP route tests for the non-Meraki receivers and telemetry ingest
after the 0.8.0-dev10 security hardening (fail-closed auth, const-time
token compare, Nexus Dashboard key verification, admin/telemetry gates,
body-size caps)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from netcortex.webhooks import auth as auth_module


# ── Catalyst Center (F1 fail-closed, F5 const-time) ─────────────────────────


@pytest.fixture
def catc_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    from netcortex.webhooks import catalyst_center as cc
    secret = "catc-token-test"  # noqa: S105 — test fixture
    monkeypatch.setitem(cc._SECRET_CACHE, "T1", secret)
    return secret


def test_catc_valid_token_200(webhook_client: TestClient, catc_secret: str) -> None:
    body = json.dumps({"eventId": "e1", "type": "NETWORK-EVENT"}).encode()
    resp = webhook_client.post(
        "/webhooks/catalyst_center/T1",
        content=body,
        headers={"Content-Type": "application/json", "X-Auth-Token": catc_secret},
    )
    assert resp.status_code == 200
    assert resp.json()["adapter"] == "catalyst_center/T1"


def test_catc_invalid_token_401(webhook_client: TestClient, catc_secret: str) -> None:
    body = json.dumps({"eventId": "e1"}).encode()
    resp = webhook_client.post(
        "/webhooks/catalyst_center/T1",
        content=body,
        headers={"Content-Type": "application/json", "X-Auth-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_catc_no_secret_fails_closed_503(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netcortex.webhooks import catalyst_center as cc

    async def _none(_name: str) -> None:
        return None

    monkeypatch.setattr(cc, "_get_shared_secret", _none)
    monkeypatch.setattr(auth_module, "_allow_unsigned", lambda: False)
    resp = webhook_client.post(
        "/webhooks/catalyst_center/T1",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503


# ── Nexus Dashboard (F7 verify key, fail-closed) ────────────────────────────


@pytest.fixture
def nd_key(monkeypatch: pytest.MonkeyPatch) -> str:
    from netcortex.webhooks import nexus_dashboard as nd
    key = "nd-api-key-test"  # noqa: S105 — test fixture
    monkeypatch.setitem(nd._SECRET_CACHE, "N1", key)
    return key


def test_nd_valid_key_200(webhook_client: TestClient, nd_key: str) -> None:
    body = json.dumps({"eventType": "fabric.event"}).encode()
    resp = webhook_client.post(
        "/webhooks/nexus_dashboard/N1",
        content=body,
        headers={"Content-Type": "application/json", "X-ND-API-Key": nd_key},
    )
    assert resp.status_code == 200
    assert resp.json()["adapter"] == "nexus_dashboard/N1"


def test_nd_invalid_key_401(webhook_client: TestClient, nd_key: str) -> None:
    resp = webhook_client.post(
        "/webhooks/nexus_dashboard/N1",
        content=b"{}",
        headers={"Content-Type": "application/json", "X-ND-API-Key": "nope"},
    )
    assert resp.status_code == 401


def test_nd_no_key_fails_closed_503(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netcortex.webhooks import nexus_dashboard as nd

    async def _none(_name: str) -> None:
        return None

    monkeypatch.setattr(nd, "_get_api_key", _none)
    monkeypatch.setattr(auth_module, "_allow_unsigned", lambda: False)
    resp = webhook_client.post(
        "/webhooks/nexus_dashboard/N1",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 503


# ── Generic catch-all (F4 admin-gated) ──────────────────────────────────────


def test_generic_requires_admin_token(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_module, "_setting",
        lambda name, default="": "adm" if name == "api_secret" else default,
    )
    # No token → 401
    r1 = webhook_client.post("/webhooks/generic/foo/bar", content=b"{}")
    assert r1.status_code == 401
    # Correct token → 200
    r2 = webhook_client.post(
        "/webhooks/generic/foo/bar",
        content=b"{}",
        headers={"X-NetCortex-Token": "adm"},
    )
    assert r2.status_code == 200


def test_generic_disabled_without_api_secret(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_module, "_setting", lambda name, default="": default)
    monkeypatch.setattr(auth_module, "_allow_unsigned", lambda: False)
    resp = webhook_client.post("/webhooks/generic/foo/bar", content=b"{}")
    assert resp.status_code == 503


# ── Telemetry ingest + SSE (F6) ─────────────────────────────────────────────


def test_telemetry_requires_token(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_module, "_setting",
        lambda name, default="": "tel" if name == "telemetry_secret" else default,
    )
    # Missing token → 401
    r1 = webhook_client.post(
        "/ingest/telemetry/dev1",
        content=json.dumps({"x": 1}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert r1.status_code == 401
    # Correct token → 202 accepted
    r2 = webhook_client.post(
        "/ingest/telemetry/dev1",
        content=json.dumps({"x": 1}).encode(),
        headers={"Content-Type": "application/json", "X-Telemetry-Token": "tel"},
    )
    assert r2.status_code == 202


def test_telemetry_sse_admin_gated(
    webhook_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_module, "_setting",
        lambda name, default="": "adm" if name == "api_secret" else default,
    )
    resp = webhook_client.get("/ingest/telemetry/stream")
    assert resp.status_code == 401
