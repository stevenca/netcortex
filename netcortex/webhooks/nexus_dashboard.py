"""Nexus Dashboard / NDFC webhook handler.

Nexus Dashboard can POST event notifications to an external endpoint with
a shared API key in the ``X-ND-API-Key`` header.

Shared secret storage path:
  netcortex/webhooks/nexus_dashboard/<instance_name>  → {"api_key": "..."}

Prior to 0.8.0-dev10 the Nexus Dashboard route accepted *any* request —
the ``X-ND-API-Key`` header was declared but never checked (F7). This
handler verifies it in constant time and fails closed when no key is
provisioned.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException, status

from netcortex.webhooks.auth import reject_if_unsigned, verify_shared_token

log = structlog.get_logger(__name__)

_SECRET_CACHE: dict[str, str] = {}


async def _get_api_key(instance_name: str) -> str | None:
    """Fetch the Nexus Dashboard webhook API key from the secret backend.

    Accepts either ``api_key`` or ``shared_secret`` in the stored blob so
    operators can use whichever convention matches the rest of the chart.
    """
    if instance_name in _SECRET_CACHE:
        return _SECRET_CACHE[instance_name]
    path = f"netcortex/webhooks/nexus_dashboard/{instance_name}"
    try:
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()
        data = await backend.get(path, required=False)
    except Exception as exc:
        log.warning(
            "webhook.nd.secret_fetch_failed",
            instance=instance_name,
            path=path,
            error=str(exc),
        )
        return None
    if not data:
        return None
    key = data.get("api_key") or data.get("shared_secret")
    if key:
        _SECRET_CACHE[instance_name] = key
    return key


async def handle_nexus_dashboard_webhook(
    *,
    instance_name: str,
    body: bytes,
    api_key: str | None,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Validate and enqueue a Nexus Dashboard event notification."""
    expected = await _get_api_key(instance_name)

    if expected is not None:
        if not verify_shared_token(api_key, expected):
            log.warning(
                "webhook.nd.invalid_key",
                instance=instance_name,
                has_key=api_key is not None,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Nexus Dashboard API key",
            )
    else:
        reject_if_unsigned(kind="nexus_dashboard", instance_name=instance_name)

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body"
        )

    event_type = payload.get("eventType") or payload.get("type")
    log.info(
        "webhook.nd.accepted",
        instance=instance_name,
        event_type=event_type,
    )

    from netcortex.webhooks.sync_coalesce import schedule_sync
    schedule_sync(
        f"nexus_dashboard/{instance_name}",
        lambda: _sync_nexus_dashboard(instance_name=instance_name, payload=payload),
    )

    return {
        "status": "queued",
        "adapter": f"nexus_dashboard/{instance_name}",
        "event_type": event_type or "",
    }


async def _sync_nexus_dashboard(
    *, instance_name: str, payload: dict[str, Any]
) -> None:
    """Trigger a sync of the Nexus Dashboard adapter after an event."""
    instance_id = f"nexus_dashboard/{instance_name}"
    try:
        from netcortex.adapters import get_instances
        adapter = get_instances().get(instance_id)
        if adapter is None:
            log.warning("webhook.nd.adapter_not_found", instance_id=instance_id)
            return
        log.info("webhook.nd.full_sync", instance_id=instance_id)
        await adapter.discover()
    except Exception as exc:
        log.error("webhook.nd.sync_failed", instance_id=instance_id, error=str(exc))
