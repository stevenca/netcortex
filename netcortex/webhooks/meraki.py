"""Meraki webhook handler.

Meraki signs each webhook POST body with HMAC-SHA256 using the shared secret
configured in Dashboard → Webhooks.  The signature arrives in the header:
  X-Cisco-Meraki-Signature: <hex-digest>

Shared secret storage path:  netcortex/webhooks/meraki  → {"shared_secret": "..."}

Reference: https://developer.cisco.com/meraki/webhooks/
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import BackgroundTasks, HTTPException, status

from netcortex.webhooks.auth import (
    is_timestamp_fresh,
    reject_if_unsigned,
    verify_hmac_sha256,
)

if TYPE_CHECKING:
    from netcortex.thalamus import SensoryPublisher

log = structlog.get_logger(__name__)

_SECRET_CACHE: dict[str, str] = {}  # instance_name → shared_secret


async def _get_shared_secret(instance_name: str) -> str | None:
    """Fetch the Meraki webhook shared secret from the secret backend.

    Returns ``None`` when the secret is not configured for this tenant.
    Note: returning ``None`` causes the route to accept the webhook
    with a loud warning (see :func:`handle_meraki_webhook`). That is
    intentional for bootstrapping (operators can validate the receive
    path before plumbing secrets), **but** it means a misconfigured
    backend silently disables HMAC verification — so this function
    distinguishes "secret not configured" (return None) from "backend
    unreachable / wrong API" (log loudly + return None — same outcome
    so we don't crash the request, but operators can spot the
    mismatch in logs).

    Historical note: prior to 0.8.0-dev9 this function called
    ``backend.get_secret(...)`` which is not a real method on
    :class:`AwsSecretsManagerBackend` — the bare ``except Exception``
    swallowed the resulting ``AttributeError`` and HMAC verification
    silently fell open for every tenant. Fixed in dev9 to use the
    correct ``backend.get(path, required=False)`` API.
    """
    if instance_name in _SECRET_CACHE:
        return _SECRET_CACHE[instance_name]
    path = f"netcortex/webhooks/meraki/{instance_name}"
    try:
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()
        data = await backend.get(path, required=False)
    except Exception as exc:
        log.warning(
            "webhook.meraki.secret_fetch_failed",
            instance=instance_name,
            path=path,
            error=str(exc),
        )
        return None
    if not data:
        # No secret stored for this tenant. Distinct from a backend
        # error — the path is well-formed, the secret just isn't set.
        return None
    secret = data.get("shared_secret")
    if secret:
        _SECRET_CACHE[instance_name] = secret
    return secret


def _verify_signature(body: bytes, signature_header: str | None, shared_secret: str) -> bool:
    """Return True if the Meraki HMAC-SHA256 signature is valid.

    Thin wrapper over the shared :func:`netcortex.webhooks.auth.verify_hmac_sha256`
    so all vendors use one constant-time implementation.
    """
    return verify_hmac_sha256(body, signature_header, shared_secret)


async def handle_meraki_webhook(
    *,
    instance_name: str,
    body: bytes,
    signature_header: str | None,
    background_tasks: BackgroundTasks,
    publisher: "SensoryPublisher | None" = None,
) -> dict[str, str]:
    """Validate and process a Meraki webhook event.

    Two side effects, both best-effort and independent:

    1. **Publish sensory events to NATS** (0.8.0-dev8 and later) so
       reflex handlers and the episodic memory layer see the event in
       seconds, not minutes. Driven by the
       :mod:`netcortex.webhooks.meraki_events` mapper.
    2. **Trigger a targeted adapter sync** (pre-dev8 behavior) so the
       Neo4j live-state graph reflects the change. This stays even
       when the sensory side is wired in: graph sync re-derives
       authoritative state, sensory events feed the reflex layer.

    The two paths are intentionally decoupled: a NATS hiccup must not
    block the sync, and a misbehaving adapter must not block the
    publish. Both run inside the request handler (publish is async +
    fast; sync is in BackgroundTasks).
    """
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
        # Fail closed (0.8.0-dev10): a tenant with no configured secret is
        # rejected (503) unless the operator opted into the explicit
        # bootstrap switch. Previously this branch accepted the webhook.
        reject_if_unsigned(kind="meraki", instance_name=instance_name)

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    # Replay resistance (0.8.0-dev10): Meraki stamps each alert with an ISO
    # `sentAt`. Reject stale deliveries (captured-and-replayed payloads)
    # outside the configured freshness window. Only enforced when a secret
    # is configured — an unsigned bootstrap request has no integrity anyway.
    if shared_secret is not None and not is_timestamp_fresh(payload.get("sentAt")):
        log.warning(
            "webhook.meraki.stale_timestamp",
            instance=instance_name,
            sent_at=payload.get("sentAt"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook timestamp outside the accepted freshness window",
        )

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

    # ── Publish sensory events ────────────────────────────────────────────
    # Pure mapper from vendor dialect to our subjects taxonomy. The
    # publisher itself is best-effort; per-event publish failures are
    # logged inside :class:`SensoryPublisher` and don't propagate.
    sensory_published = 0
    if publisher is not None:
        try:
            from netcortex.webhooks.meraki_events import map_meraki_payload
            events = map_meraki_payload(payload)
            for ev in events:
                await publisher.publish(
                    ev.event_class, *ev.target_parts, payload=ev.payload
                )
            sensory_published = len(events)
        except Exception as exc:
            # Mapper bugs or malformed payloads should not 5xx the
            # webhook — they should log and let the sync trigger fire
            # so live state still reconciles.
            log.warning(
                "webhook.meraki.publish_failed",
                instance=instance_name,
                event_type=event_type,
                error=str(exc),
            )

    # ── Targeted adapter sync (pre-dev8 behavior, retained) ───────────────
    # Funneled through the coalescing scheduler (0.8.0-dev10, F4) so a
    # webhook flood can't spawn unbounded concurrent discoveries.
    from netcortex.webhooks.sync_coalesce import schedule_sync
    schedule_sync(
        f"meraki/{instance_name}",
        lambda: _sync_meraki_network(
            instance_name=instance_name,
            event_type=event_type,
            network_id=network_id,
            payload=payload,
        ),
    )

    return {
        "status": "queued",
        "adapter": f"meraki/{instance_name}",
        "event_type": event_type,
        "sensory_events_published": str(sensory_published),
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
