"""SAML SSO endpoints (0.8.0-dev11).

All routes here are PUBLIC (excluded from the API-auth gate in main.py),
because they are the login flow itself. Routes 404 when SAML is disabled
so a non-SSO deployment exposes no extra surface.

Flow (SP-initiated, the only mode we allow):
  GET  /saml/login?next=/path   → redirect to Okta with a signed-ish
                                   RelayState carrying the local return path
  POST /saml/acs                → validate the assertion, create the
                                   server-side session, set the cookie,
                                   redirect to the validated return path
  GET  /saml/metadata           → SP metadata XML (paste into Okta)
  GET  /saml/logout             → destroy local session + (optional) SLO
  GET/POST /saml/sls            → single-logout service
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from netcortex.auth import saml as saml_mod
from netcortex.auth import session as session_mod

log = structlog.get_logger(__name__)

router = APIRouter(tags=["auth"])


def _cfg() -> Any:
    from netcortex.config import get_settings
    try:
        cfg = get_settings()
    except RuntimeError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready")
    if not getattr(cfg, "saml_enabled", False):
        # SAML disabled → these endpoints do not exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return cfg


def _safe_local_path(candidate: str | None) -> str:
    """Return a safe same-origin path for post-login redirect.

    Prevents open-redirect: only accepts a path beginning with a single
    "/" (rejects "//host" network-path and absolute URLs). Falls back to
    "/".
    """
    if not candidate:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


@router.get("/saml/login", include_in_schema=False)
async def saml_login(request: Request) -> RedirectResponse:
    cfg = _cfg()
    next_path = _safe_local_path(request.query_params.get("next"))
    req_data = saml_mod.prepare_request(cfg, request)
    auth = saml_mod.build_auth(cfg, req_data)
    # return_to becomes the RelayState round-tripped back to /saml/acs.
    redirect_url = auth.login(return_to=next_path)
    log.info("saml.login.initiated", next=next_path)
    return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/saml/acs", include_in_schema=False)
async def saml_acs(request: Request) -> Response:
    cfg = _cfg()
    form = await request.form()
    post_data = {k: v for k, v in form.items()}
    req_data = saml_mod.prepare_request(cfg, request, post_data=post_data)
    auth = saml_mod.build_auth(cfg, req_data)

    auth.process_response()
    errors = auth.get_errors()
    if errors:
        log.warning(
            "saml.acs.invalid_response",
            errors=errors,
            reason=auth.get_last_error_reason(),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SAML authentication failed")
    if not auth.is_authenticated():
        log.warning("saml.acs.not_authenticated")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="SAML authentication failed")

    name_id = auth.get_nameid() or ""
    attributes = auth.get_attributes() or {}
    groups = saml_mod.extract_groups(cfg, attributes)
    # Prefer an explicit email attribute, else the NameID (email format).
    email = ""
    for key in ("email", "Email", "user.email", "mail"):
        val = attributes.get(key)
        if val:
            email = val[0] if isinstance(val, (list, tuple)) else str(val)
            break
    if not email and "@" in name_id:
        email = name_id

    if not saml_mod.is_user_allowed(cfg, email=email, groups=groups):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for NetCortex")

    sid = await session_mod.create_session(
        subject=name_id or email,
        email=email,
        groups=groups,
        name_id=name_id,
        session_index=auth.get_session_index() or "",
    )

    return_to = _safe_local_path(post_data.get("RelayState"))
    response = RedirectResponse(return_to, status_code=status.HTTP_303_SEE_OTHER)
    session_mod.set_session_cookie(response, sid)
    # Sensitive auth response — never cache.
    response.headers["Cache-Control"] = "no-store"
    log.info("saml.acs.session_established", email=email, next=return_to)
    return response


@router.get("/saml/metadata", include_in_schema=False)
async def saml_metadata(request: Request) -> Response:
    cfg = _cfg()
    xml, errors = saml_mod.sp_metadata(cfg)
    if errors:
        log.error("saml.metadata.invalid", errors=errors)
        raise HTTPException(status_code=500, detail="Invalid SP metadata")
    return Response(content=xml, media_type="application/xml")


@router.get("/saml/logout", include_in_schema=False)
async def saml_logout(request: Request) -> Response:
    cfg = _cfg()
    sid = session_mod.read_session_id(request)
    await session_mod.destroy_session(sid)
    # Local logout always; we redirect home and clear the cookie. (IdP-side
    # SLO can be wired via auth.logout() later; local invalidation is the
    # security-critical part.)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    session_mod.clear_session_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.api_route("/saml/sls", methods=["GET", "POST"], include_in_schema=False)
async def saml_sls(request: Request) -> Response:
    cfg = _cfg()
    # Best-effort single-logout endpoint: invalidate the local session.
    sid = session_mod.read_session_id(request)
    await session_mod.destroy_session(sid)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    session_mod.clear_session_cookie(response)
    return response
