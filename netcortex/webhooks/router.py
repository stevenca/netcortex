"""FastAPI router for all inbound webhook endpoints.

URL scheme:
  POST /webhooks/meraki/{instance_name}
  POST /webhooks/catalyst_center/{instance_name}
  POST /webhooks/nexus_dashboard/{instance_name}
  POST /webhooks/generic/{platform}/{instance_name}
  POST /ingest/telemetry/{device_name}        ← HTTP-push MDT / streaming telemetry
  GET  /ingest/telemetry/stream               ← SSE stream of ingest events (UI / monitoring)

The router is mounted on the main FastAPI app in main.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from netcortex.webhooks.meraki import handle_meraki_webhook
from netcortex.webhooks.catalyst_center import handle_catalyst_center_webhook
from netcortex.webhooks.telemetry import handle_telemetry_push, telemetry_event_stream

log = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks & telemetry"])


# ── Meraki ────────────────────────────────────────────────────────────────────

@router.post(
    "/webhooks/meraki/{instance_name}",
    summary="Meraki webhook receiver",
    status_code=status.HTTP_200_OK,
)
async def meraki_webhook(
    instance_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_cisco_meraki_signature: str | None = Header(
        None, alias="X-Cisco-Meraki-Signature"
    ),
) -> dict[str, str]:
    """Receive and process Meraki Dashboard webhook alerts.

    Meraki signs each webhook with HMAC-SHA256 of the request body using the
    shared secret configured in Dashboard → Webhooks.  Store that secret at:
      ``netcortex/webhooks/meraki`` → ``{"shared_secret": "..."}``

    The handler validates the signature then queues a targeted sync of the
    affected network so state is refreshed within seconds of the event.
    """
    body = await request.body()
    log.info(
        "webhook.meraki.received",
        instance=instance_name,
        bytes=len(body),
        has_sig=x_cisco_meraki_signature is not None,
    )
    result = await handle_meraki_webhook(
        instance_name=instance_name,
        body=body,
        signature_header=x_cisco_meraki_signature,
        background_tasks=background_tasks,
    )
    return result


# ── Catalyst Center ───────────────────────────────────────────────────────────

@router.post(
    "/webhooks/catalyst_center/{instance_name}",
    summary="Catalyst Center (DNAC) webhook receiver",
    status_code=status.HTTP_200_OK,
)
async def catalyst_center_webhook(
    instance_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
) -> dict[str, str]:
    """Receive Catalyst Center event notifications.

    Configure an event subscription in Catalyst Center to POST to this URL.
    Store the shared token at:
      ``netcortex/webhooks/catalyst_center`` → ``{"shared_secret": "..."}``
    """
    body = await request.body()
    log.info(
        "webhook.catalyst_center.received",
        instance=instance_name,
        bytes=len(body),
        has_token=x_auth_token is not None,
    )
    result = await handle_catalyst_center_webhook(
        instance_name=instance_name,
        body=body,
        auth_token=x_auth_token,
        background_tasks=background_tasks,
    )
    return result


# ── Nexus Dashboard ───────────────────────────────────────────────────────────

@router.post(
    "/webhooks/nexus_dashboard/{instance_name}",
    summary="Nexus Dashboard webhook receiver",
    status_code=status.HTTP_200_OK,
)
async def nexus_dashboard_webhook(
    instance_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_nd_api_key: str | None = Header(None, alias="X-ND-API-Key"),
) -> dict[str, str]:
    """Receive Nexus Dashboard / NDFC event notifications."""
    body = await request.body()
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    log.info(
        "webhook.nexus_dashboard.received",
        instance=instance_name,
        event_type=payload.get("eventType") or payload.get("type"),
    )
    background_tasks.add_task(_trigger_sync, "nexus_dashboard", instance_name, payload)
    return {"status": "queued", "adapter": f"nexus_dashboard/{instance_name}"}


# ── Generic catch-all ─────────────────────────────────────────────────────────

@router.post(
    "/webhooks/generic/{platform}/{instance_name}",
    summary="Generic webhook catch-all",
    status_code=status.HTTP_200_OK,
)
async def generic_webhook(
    platform: str,
    instance_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Accept webhooks from platforms without a dedicated handler.

    The payload is logged and queued for a full adapter sync.
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body.decode("utf-8", errors="replace")}

    log.info(
        "webhook.generic.received",
        platform=platform,
        instance=instance_name,
        keys=list(payload.keys()) if isinstance(payload, dict) else None,
    )
    background_tasks.add_task(_trigger_sync, platform, instance_name, payload)
    return {"status": "queued", "adapter": f"{platform}/{instance_name}"}


# ── HTTP streaming telemetry (MDT push) ───────────────────────────────────────

@router.post(
    "/ingest/telemetry/{device_name}",
    summary="HTTP streaming telemetry receiver",
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_telemetry(
    device_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_collection_id: str | None = Header(None, alias="X-Collection-Id"),
    x_yang_path: str | None = Header(None, alias="X-Yang-Path"),
) -> dict[str, Any]:
    """Accept HTTP-push streaming telemetry from network devices.

    Compatible with:
    - Cisco IOS-XE / IOS-XR HTTP dial-out MDT
    - Catalyst Center telemetry push
    - Any device that can POST YANG-modeled JSON data

    The payload is validated, tagged with the device name, and queued
    onto the Redis ingest stream for asynchronous graph enrichment.

    For gRPC dial-out MDT (gNMI Subscribe), see the telemetry-grpc
    sidecar service (port 57500) in the Helm chart.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")

    result = await handle_telemetry_push(
        device_name=device_name,
        body=body,
        content_type=content_type,
        collection_id=x_collection_id,
        yang_path=x_yang_path,
        background_tasks=background_tasks,
    )
    return result


@router.get(
    "/ingest/telemetry/stream",
    summary="SSE stream of ingest events",
    response_class=StreamingResponse,
)
async def telemetry_sse_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of recent telemetry ingest activity.

    Useful for real-time monitoring of what data is flowing in.
    Connect with:  curl -N https://.../ingest/telemetry/stream
    """
    return StreamingResponse(
        telemetry_event_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _trigger_sync(platform: str, instance_name: str, payload: dict[str, Any]) -> None:
    """Background task: trigger a targeted sync for the adapter that received a webhook."""
    instance_id = f"{platform}/{instance_name}"
    try:
        from netcortex.adapters import get_instances
        instances = get_instances()
        adapter = instances.get(instance_id)
        if adapter is None:
            log.warning("webhook.sync.adapter_not_found", instance_id=instance_id)
            return
        log.info("webhook.sync.triggered", instance_id=instance_id)
        await adapter.discover()
        log.info("webhook.sync.complete", instance_id=instance_id)
    except Exception as exc:
        log.error("webhook.sync.failed", instance_id=instance_id, error=str(exc))
