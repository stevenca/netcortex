"""Catalyst Center (DNAC) webhook handler.

Catalyst Center can be configured to POST event notifications to an HTTP
endpoint.  Authentication is via a shared token passed in the X-Auth-Token
header (configured in CC → Platform → Developer Toolkit → Event Notifications).

Shared secret storage path:
  netcortex/webhooks/catalyst_center/<instance_name>  → {"shared_secret": "..."}

Reference: https://developer.cisco.com/docs/dna-center/event-management/
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException, status

from netcortex.webhooks.auth import reject_if_unsigned, verify_shared_token

log = structlog.get_logger(__name__)

_SECRET_CACHE: dict[str, str] = {}


async def _get_shared_secret(instance_name: str) -> str | None:
    """Fetch the Catalyst Center webhook shared token from the backend.

    See ``netcortex/webhooks/meraki.py::_get_shared_secret`` for the
    full story on the pre-0.8.0-dev9 ``AttributeError``-silently-
    swallowed-by-bare-except bug that this function shared.
    """
    if instance_name in _SECRET_CACHE:
        return _SECRET_CACHE[instance_name]
    path = f"netcortex/webhooks/catalyst_center/{instance_name}"
    try:
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()
        data = await backend.get(path, required=False)
    except Exception as exc:
        log.warning(
            "webhook.catc.secret_fetch_failed",
            instance=instance_name,
            path=path,
            error=str(exc),
        )
        return None
    if not data:
        return None
    secret = data.get("shared_secret")
    if secret:
        _SECRET_CACHE[instance_name] = secret
    return secret


async def handle_catalyst_center_webhook(
    *,
    instance_name: str,
    body: bytes,
    auth_token: str | None,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Validate and enqueue a Catalyst Center event notification."""
    shared_secret = await _get_shared_secret(instance_name)

    if shared_secret is not None:
        # Constant-time comparison (0.8.0-dev10, F5): the previous `!=`
        # leaked the token length/prefix through a timing side channel.
        if not verify_shared_token(auth_token, shared_secret):
            log.warning(
                "webhook.catc.invalid_token",
                instance=instance_name,
                has_token=auth_token is not None,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Catalyst Center webhook token",
            )
    else:
        # Fail closed (0.8.0-dev10, F1): reject when no token is configured.
        reject_if_unsigned(kind="catalyst_center", instance_name=instance_name)

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    # CC event envelopes look like:
    # {"eventId": "...", "type": "NETWORK-EVENT", "category": "WARN",
    #  "domain": "CONNECTIVITY", "subDomain": "PHYSICAL_LINK",
    #  "details": {"device": "...", "message": "..."}}
    event_id = payload.get("eventId") or payload.get("instanceId")
    event_type = payload.get("type") or payload.get("eventType")
    domain = payload.get("domain")
    sub_domain = payload.get("subDomain")
    device = (payload.get("details") or {}).get("device")

    log.info(
        "webhook.catc.accepted",
        instance=instance_name,
        event_id=event_id,
        event_type=event_type,
        domain=domain,
        sub_domain=sub_domain,
        device=device,
    )

    # Coalesced sync (0.8.0-dev10, F4) — single-flight, rate-limited.
    from netcortex.webhooks.sync_coalesce import schedule_sync
    schedule_sync(
        f"catalyst_center/{instance_name}",
        lambda: _sync_catalyst_center(
            instance_name=instance_name,
            event_type=event_type,
            domain=domain,
            device=device,
            payload=payload,
        ),
    )

    return {
        "status": "queued",
        "adapter": f"catalyst_center/{instance_name}",
        "event_id": event_id or "",
        "event_type": event_type or "",
    }


async def _sync_catalyst_center(
    *,
    instance_name: str,
    event_type: str | None,
    domain: str | None,
    device: str | None,
    payload: dict[str, Any],
) -> None:
    """Trigger a targeted sync after a Catalyst Center event."""
    instance_id = f"catalyst_center/{instance_name}"
    try:
        from netcortex.adapters import get_instances
        adapter = get_instances().get(instance_id)
        if adapter is None:
            log.warning("webhook.catc.adapter_not_found", instance_id=instance_id)
            return

        # For physical link / connectivity events, try a targeted device refresh.
        if device and hasattr(adapter, "discover_device"):
            log.info("webhook.catc.targeted_sync", instance_id=instance_id, device=device)
            await adapter.discover_device(device)
        else:
            log.info("webhook.catc.full_sync", instance_id=instance_id, domain=domain)
            await adapter.discover()
    except Exception as exc:
        log.error("webhook.catc.sync_failed", instance_id=instance_id, error=str(exc))
