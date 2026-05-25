"""Meraki webhook handler.

Meraki signs each webhook POST body with HMAC-SHA256 using the shared secret
configured in Dashboard → Webhooks.  The signature arrives in the header:
  X-Cisco-Meraki-Signature: <hex-digest>

Shared secret storage path:  netcortex/webhooks/meraki  → {"shared_secret": "..."}

Reference: https://developer.cisco.com/meraki/webhooks/
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException, status

log = structlog.get_logger(__name__)

_SECRET_CACHE: dict[str, str] = {}  # instance_name → shared_secret


async def _get_shared_secret(instance_name: str) -> str | None:
    """Fetch the Meraki webhook shared secret from the secret backend."""
    if instance_name in _SECRET_CACHE:
        return _SECRET_CACHE[instance_name]
    try:
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()
        data = await backend.get_secret(f"netcortex/webhooks/meraki/{instance_name}")
        secret = data.get("shared_secret")
        if secret:
            _SECRET_CACHE[instance_name] = secret
        return secret
    except Exception as exc:
        log.warning("webhook.meraki.secret_fetch_failed", instance=instance_name, error=str(exc))
        return None


def _verify_signature(body: bytes, signature_header: str | None, shared_secret: str) -> bool:
    """Return True if the Meraki HMAC-SHA256 signature is valid."""
    if not signature_header:
        return False
    # Meraki sends the raw hex digest (no "sha256=" prefix in older versions;
    # newer versions may add it — strip it for compatibility).
    received = signature_header.removeprefix("sha256=").strip()
    expected = hmac.new(
        shared_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)


async def handle_meraki_webhook(
    *,
    instance_name: str,
    body: bytes,
    signature_header: str | None,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Validate and enqueue a Meraki webhook event."""
    shared_secret = await _get_shared_secret(instance_name)

    if shared_secret is not None:
        if not _verify_signature(body, signature_header, shared_secret):
            log.warning(
                "webhook.meraki.invalid_signature",
                instance=instance_name,
                has_header=signature_header is not None,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Meraki webhook signature",
            )
    else:
        log.warning(
            "webhook.meraki.no_secret_configured",
            instance=instance_name,
            hint="Store shared_secret at netcortex/webhooks/meraki/<instance_name>",
        )

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    event_type = payload.get("alertType") or payload.get("eventType") or "unknown"
    network_id = payload.get("networkId")
    org_id = payload.get("organizationId")

    log.info(
        "webhook.meraki.accepted",
        instance=instance_name,
        event_type=event_type,
        network_id=network_id,
        org_id=org_id,
    )

    # Targeted refresh: only re-sync the affected network if possible.
    background_tasks.add_task(
        _sync_meraki_network,
        instance_name=instance_name,
        event_type=event_type,
        network_id=network_id,
        payload=payload,
    )

    return {
        "status": "queued",
        "adapter": f"meraki/{instance_name}",
        "event_type": event_type,
    }


async def _sync_meraki_network(
    *,
    instance_name: str,
    event_type: str,
    network_id: str | None,
    payload: dict[str, Any],
) -> None:
    """Trigger a targeted sync of the Meraki adapter after a webhook event."""
    instance_id = f"meraki/{instance_name}"
    try:
        from netcortex.adapters import get_instances
        adapter = get_instances().get(instance_id)
        if adapter is None:
            log.warning("webhook.meraki.adapter_not_found", instance_id=instance_id)
            return

        # If the adapter exposes a targeted network-refresh method, use it;
        # otherwise fall back to a full discover().
        if network_id and hasattr(adapter, "discover_network"):
            log.info("webhook.meraki.targeted_sync", instance_id=instance_id, network_id=network_id)
            await adapter.discover_network(network_id)
        else:
            log.info("webhook.meraki.full_sync", instance_id=instance_id, event_type=event_type)
            await adapter.discover()
    except Exception as exc:
        log.error("webhook.meraki.sync_failed", instance_id=instance_id, error=str(exc))
