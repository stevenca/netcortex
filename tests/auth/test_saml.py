"""Tests for SAML SP settings + authorization helpers (0.8.0-dev11).

These cover the pure logic (settings dict construction, authz, redirect
safety) without importing python3-saml/xmlsec — the OneLogin import is
lazy and only happens in build_auth/sp_metadata.
"""

from __future__ import annotations

from types import SimpleNamespace

from netcortex.auth import saml as saml_mod
from netcortex.auth.router import _safe_local_path


def _cfg(**over):
    base = dict(
        saml_enabled=True,
        saml_sp_base_url="https://netcortex.example.com",
        saml_sp_entity_id="",
        saml_idp_entity_id="http://www.okta.com/exk123",
        saml_idp_sso_url="https://example.okta.com/app/abc/sso/saml",
        saml_idp_slo_url="https://example.okta.com/app/abc/slo/saml",
        saml_idp_x509_cert="MIIBOGUS...",
        saml_sp_x509_cert="",
        saml_sp_private_key="",
        saml_allowed_email_domains=[],
        saml_allowed_groups=[],
        saml_attr_groups="groups",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_build_settings_is_strict_and_hardened() -> None:
    s = saml_mod.build_settings(_cfg())
    assert s["strict"] is True
    sec = s["security"]
    assert sec["wantAssertionsSigned"] is True
    assert sec["rejectUnsolicitedResponsesWithInResponseTo"] is True
    assert "rsa-sha256" in sec["signatureAlgorithm"]
    assert "sha256" in sec["digestAlgorithm"]
    # ACS URL derived from base URL
    assert s["sp"]["assertionConsumerService"]["url"] == "https://netcortex.example.com/saml/acs"
    # entityId falls back to the metadata URL when unset
    assert s["sp"]["entityId"] == "https://netcortex.example.com/saml/metadata"


def test_build_settings_signs_requests_only_with_keypair() -> None:
    unsigned = saml_mod.build_settings(_cfg())
    assert unsigned["security"]["authnRequestsSigned"] is False
    signed = saml_mod.build_settings(
        _cfg(saml_sp_x509_cert="CERT", saml_sp_private_key="KEY")
    )
    assert signed["security"]["authnRequestsSigned"] is True


def test_authz_allows_when_no_lists() -> None:
    assert saml_mod.is_user_allowed(_cfg(), email="a@x.com", groups=[]) is True


def test_authz_email_domain() -> None:
    cfg = _cfg(saml_allowed_email_domains=["example.com"])
    assert saml_mod.is_user_allowed(cfg, email="a@example.com", groups=[]) is True
    assert saml_mod.is_user_allowed(cfg, email="a@evil.com", groups=[]) is False


def test_authz_groups() -> None:
    cfg = _cfg(saml_allowed_groups=["netops", "admins"])
    assert saml_mod.is_user_allowed(cfg, email="a@x.com", groups=["netops"]) is True
    assert saml_mod.is_user_allowed(cfg, email="a@x.com", groups=["guests"]) is False


def test_extract_groups_handles_scalar_and_list() -> None:
    cfg = _cfg()
    assert saml_mod.extract_groups(cfg, {"groups": ["a", "b"]}) == ["a", "b"]
    assert saml_mod.extract_groups(cfg, {"groups": "solo"}) == ["solo"]
    assert saml_mod.extract_groups(cfg, {}) == []


def test_safe_local_path_blocks_open_redirect() -> None:
    assert _safe_local_path("/dashboard") == "/dashboard"
    assert _safe_local_path("/api/graph?x=1") == "/api/graph?x=1"
    assert _safe_local_path(None) == "/"
    assert _safe_local_path("") == "/"
    assert _safe_local_path("//evil.com") == "/"          # network-path
    assert _safe_local_path("https://evil.com") == "/"     # absolute URL
    assert _safe_local_path("javascript:alert(1)") == "/"   # scheme
