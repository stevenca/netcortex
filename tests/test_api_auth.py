"""Tests for the API-authentication middleware (0.8.0-dev10, F2/F8).

Builds a fresh FastAPI app and attaches the production ``_api_auth``
middleware from :mod:`netcortex.main`, so we exercise the real path
classification and bearer-token check without standing up the full app
(which needs Neo4j / Redis / the secret backend).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import netcortex.main as main_module


class _FakeSettings:
    def __init__(self, api_secret: str, saml_enabled: bool = False) -> None:
        self.api_secret = api_secret
        self.saml_enabled = saml_enabled


def _build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(main_module._api_auth)

    @app.get("/api/inventory")
    async def _inv() -> dict:
        return {"ok": True}

    @app.get("/metrics")
    async def _metrics() -> dict:
        return {"ok": True}

    @app.get("/health")
    async def _health() -> dict:
        return {"ok": True}

    @app.post("/webhooks/meraki/x")
    async def _wh() -> dict:
        return {"ok": True}

    @app.get("/")
    async def _root() -> dict:
        return {"ok": True}

    return app


def test_api_blocked_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("s3cret"))
    c = TestClient(_build_app())
    assert c.get("/api/inventory").status_code == 401
    assert c.get("/metrics").status_code == 401
    assert c.get("/").status_code == 401


def test_api_allowed_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("s3cret"))
    c = TestClient(_build_app())
    r = c.get("/api/inventory", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_public_paths_never_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("s3cret"))
    c = TestClient(_build_app())
    assert c.get("/health").status_code == 200
    assert c.post("/webhooks/meraki/x").status_code == 200


def test_noop_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With api_secret unset, the middleware is a no-op (ingress is the
    control in that mode)."""
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings(""))
    c = TestClient(_build_app())
    assert c.get("/api/inventory").status_code == 200


def test_is_api_auth_public_classification() -> None:
    assert main_module._is_api_auth_public("/health") is True
    assert main_module._is_api_auth_public("/webhooks/meraki/x") is True
    assert main_module._is_api_auth_public("/ingest/telemetry/d") is True
    assert main_module._is_api_auth_public("/saml/acs") is True
    assert main_module._is_api_auth_public("/saml/login") is True
    assert main_module._is_api_auth_public("/api/inventory") is False
    assert main_module._is_api_auth_public("/metrics") is False
    assert main_module._is_api_auth_public("/") is False


# ── SAML session path (0.8.0-dev11) ────────────────────────────────────────


def test_saml_browser_redirects_to_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """With SAML on, an unauthenticated browser navigation (Accept: html)
    is 302-redirected to the IdP login, preserving the target as ?next."""
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("", saml_enabled=True))

    async def _no_session(_request):
        return False

    monkeypatch.setattr(main_module, "_has_valid_saml_session", _no_session)
    c = TestClient(_build_app(), follow_redirects=False)
    r = c.get("/api/inventory", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert r.headers["location"].startswith("/saml/login?next=")


def test_saml_xhr_gets_401_not_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("", saml_enabled=True))

    async def _no_session(_request):
        return False

    monkeypatch.setattr(main_module, "_has_valid_saml_session", _no_session)
    c = TestClient(_build_app(), follow_redirects=False)
    r = c.get("/api/inventory", headers={"Accept": "application/json"})
    assert r.status_code == 401


def test_saml_valid_session_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("", saml_enabled=True))

    async def _ok_session(_request):
        return True

    monkeypatch.setattr(main_module, "_has_valid_saml_session", _ok_session)
    c = TestClient(_build_app(), follow_redirects=False)
    r = c.get("/api/inventory", headers={"Accept": "text/html"})
    assert r.status_code == 200


def test_bearer_still_works_with_saml_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine callers use the api_secret bearer even when SAML is on."""
    monkeypatch.setattr(main_module, "get_settings", lambda: _FakeSettings("s3cret", saml_enabled=True))

    async def _no_session(_request):
        return False

    monkeypatch.setattr(main_module, "_has_valid_saml_session", _no_session)
    c = TestClient(_build_app(), follow_redirects=False)
    r = c.get("/api/inventory", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
