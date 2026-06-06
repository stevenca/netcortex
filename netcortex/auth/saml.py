"""SAML 2.0 Service Provider built on python3-saml (OneLogin).

We deliberately do **not** hand-roll SAML/XML handling — python3-saml
delegates signature verification to xmlsec/libxmlsec1 and, in ``strict``
mode with the security flags below, enforces the protections called for
in the SAML hardening guidance:

* **Signed assertions required** (``wantAssertionsSigned``) and verified
  against the IdP's x509 cert — defeats forged/unsigned assertions and
  XML signature-wrapping.
* **Audience / Destination / timestamp validation** (``strict=True``) —
  rejects assertions minted for another SP, replayed, or outside the
  ``NotBefore``/``NotOnOrAfter`` window.
* **InResponseTo enforcement**
  (``rejectUnsolicitedResponsesWithInResponseTo``) — only accepts
  responses to AuthnRequests this SP actually issued (SP-initiated),
  blocking unsolicited-response injection.
* **SHA-256 signatures/digests** — no SHA-1.

The IdP cert and the optional SP private key are pulled from the secret
backend (never env). The OneLogin import is lazy so the dependency stays
an opt-in extra and the module imports fine without xmlsec present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from netcortex.config import Settings

log = structlog.get_logger(__name__)

_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
_HTTP_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_HTTP_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
_NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


def _acs_url(cfg: "Settings") -> str:
    return f"{cfg.saml_sp_base_url.rstrip('/')}/saml/acs"


def _sls_url(cfg: "Settings") -> str:
    return f"{cfg.saml_sp_base_url.rstrip('/')}/saml/sls"


def _metadata_url(cfg: "Settings") -> str:
    return f"{cfg.saml_sp_base_url.rstrip('/')}/saml/metadata"


def build_settings(cfg: "Settings") -> dict[str, Any]:
    """Construct the python3-saml settings dict from runtime config."""
    sp_signing = bool(cfg.saml_sp_x509_cert and cfg.saml_sp_private_key)
    entity_id = cfg.saml_sp_entity_id or _metadata_url(cfg)
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": entity_id,
            "assertionConsumerService": {
                "url": _acs_url(cfg),
                "binding": _HTTP_POST,
            },
            "singleLogoutService": {
                "url": _sls_url(cfg),
                "binding": _HTTP_REDIRECT,
            },
            "NameIDFormat": _NAMEID_EMAIL,
            "x509cert": cfg.saml_sp_x509_cert or "",
            "privateKey": cfg.saml_sp_private_key or "",
        },
        "idp": {
            "entityId": cfg.saml_idp_entity_id,
            "singleSignOnService": {
                "url": cfg.saml_idp_sso_url,
                "binding": _HTTP_REDIRECT,
            },
            "singleLogoutService": {
                "url": cfg.saml_idp_slo_url or cfg.saml_idp_sso_url,
                "binding": _HTTP_REDIRECT,
            },
            "x509cert": cfg.saml_idp_x509_cert,
        },
        "security": {
            "nameIdEncrypted": False,
            "authnRequestsSigned": sp_signing,
            "logoutRequestSigned": sp_signing,
            "logoutResponseSigned": sp_signing,
            "signMetadata": False,
            "wantMessagesSigned": False,
            "wantAssertionsSigned": True,
            "wantNameId": True,
            "wantNameIdEncrypted": False,
            "wantAssertionsEncrypted": False,
            # Do NOT require an <AttributeStatement>: a signed assertion
            # carrying only a NameID is valid for us (we derive the email
            # from an emailAddress-format NameID). Duo sends NameID-only
            # assertions unless attribute release is configured, and the
            # default (True) rejects them. This relaxes ONLY the optional
            # attribute element — signature/audience/Destination/timestamp
            # validation is unaffected.
            "wantAttributeStatement": False,
            "requestedAuthnContext": False,
            "rejectUnsolicitedResponsesWithInResponseTo": True,
            "signatureAlgorithm": _RSA_SHA256,
            "digestAlgorithm": _SHA256,
        },
    }


def prepare_request(cfg: "Settings", request: Any, post_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the request dict python3-saml expects.

    Host/scheme/port are taken from the configured public base URL — not
    the raw request — so Destination/ACS validation matches the
    externally-visible https origin even though the app sits behind a
    TLS-terminating ingress and sees plain http on :8000.
    """
    base = urlparse(cfg.saml_sp_base_url)
    is_https = base.scheme == "https"
    return {
        "https": "on" if is_https else "off",
        "http_host": base.netloc,
        "script_name": request.url.path,
        "server_port": str(base.port or (443 if is_https else 80)),
        "get_data": dict(request.query_params),
        "post_data": dict(post_data or {}),
    }


def build_auth(cfg: "Settings", req_data: dict[str, Any]) -> Any:
    """Instantiate a OneLogin_Saml2_Auth (lazy import of python3-saml)."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "python3-saml not installed — install the 'saml' extra "
            "(pip install '.[saml]') to enable SAML SSO."
        ) from exc
    return OneLogin_Saml2_Auth(req_data, old_settings=build_settings(cfg))


def sp_metadata(cfg: "Settings") -> tuple[str, list[str]]:
    """Return (xml, errors) for the SP metadata document."""
    try:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("python3-saml not installed") from exc
    settings = OneLogin_Saml2_Settings(build_settings(cfg), sp_validation_only=True)
    metadata = settings.get_sp_metadata()
    errors = settings.validate_metadata(metadata)
    xml = metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata
    return xml, list(errors)


def is_user_allowed(
    cfg: "Settings", *, email: str, groups: list[str]
) -> bool:
    """Coarse authorization gate after a valid assertion.

    Empty allow-lists = any authenticated IdP user is permitted.
    """
    domains = cfg.saml_allowed_email_domains
    if domains:
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if domain not in {d.lower() for d in domains}:
            log.warning("saml.authz.denied_domain", email_domain=domain)
            return False
    allowed_groups = cfg.saml_allowed_groups
    if allowed_groups:
        if not (set(groups) & set(allowed_groups)):
            log.warning("saml.authz.denied_group", groups=groups)
            return False
    return True


def extract_groups(cfg: "Settings", attributes: dict[str, Any]) -> list[str]:
    """Pull the group-membership attribute (multi-valued) from an assertion."""
    raw = attributes.get(cfg.saml_attr_groups, [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(v) for v in raw]
    return []


__all__ = [
    "build_settings",
    "prepare_request",
    "build_auth",
    "sp_metadata",
    "is_user_allowed",
    "extract_groups",
]
