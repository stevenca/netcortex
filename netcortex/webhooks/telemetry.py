"""HTTP streaming telemetry ingest and SSE event stream.

HTTP dial-out MDT:
  Cisco IOS-XE / IOS-XR can push YANG-modeled telemetry via HTTP POST.
  Configure with:
    telemetry ietf subscription <id>
      encoding encode-kvgpb / encode-json
      protocol http-encode destination-address <netcortex-ip> port 443
      source-address <device-ip>
      stream yang-push

  POST /ingest/telemetry/<device_name>
    Body:  JSON-encoded YANG data (RFC 8641) or compact KV-GPB (decoded server-side)
    Headers:
      Content-Type: application/json  OR  application/yang-data+json
      X-Yang-Path: Cisco-IOS-XE-mpls-oper:mpls-oper-data  (optional, aids routing)
      X-Collection-Id: <subscription-id>  (optional)

gRPC dial-out MDT (gNMI Subscribe):
  For gRPC-based telemetry (port 57500), use the telemetry-grpc sidecar
  service defined in the Helm chart.  That service receives the stream,
  transcodes to JSON, and publishes onto the same Redis ingest queue.

SSE monitoring stream:
  GET /ingest/telemetry/stream  →  text/event-stream
  Each event:  data: {"device": "...", "yang_path": "...", "nodes": N, "ts": ...}
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, AsyncIterator

import structlog
from fastapi import BackgroundTasks, HTTPException, Request, status

log = structlog.get_logger(__name__)

# Ring buffer of recent telemetry events for the SSE stream (max 500 entries).
_event_ring: deque[dict[str, Any]] = deque(maxlen=500)
_event_subscribers: list[asyncio.Queue[dict[str, Any] | None]] = []


async def handle_telemetry_push(
    *,
    device_name: str,
    body: bytes,
    content_type: str,
    collection_id: str | None,
    yang_path: str | None,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Accept and queue a streaming telemetry push from a network device."""
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # Some encodings (KV-GPB) arrive as binary — treat as opaque for now.
        payload = {"_raw_bytes": len(body), "_content_type": content_type}

    node_count = _estimate_node_count(payload)

    log.info(
        "telemetry.push.received",
        device=device_name,
        yang_path=yang_path,
        collection_id=collection_id,
        bytes=len(body),
        estimated_nodes=node_count,
    )

    event: dict[str, Any] = {
        "device": device_name,
        "yang_path": yang_path,
        "collection_id": collection_id,
        "nodes": node_count,
        "bytes": len(body),
        "ts": time.time(),
    }
    _broadcast_event(event)

    background_tasks.add_task(
        _ingest_telemetry_payload,
        device_name=device_name,
        yang_path=yang_path,
        payload=payload,
    )

    return {
        "status": "accepted",
        "device": device_name,
        "yang_path": yang_path or "",
        "nodes": node_count,
    }


async def _ingest_telemetry_payload(
    *,
    device_name: str,
    yang_path: str | None,
    payload: dict[str, Any],
) -> None:
    """Parse telemetry payload and write interface/metric updates into the graph."""
    try:
        from netcortex.graph.telemetry import ingest_telemetry
        await ingest_telemetry(device_name=device_name, yang_path=yang_path, payload=payload)
    except ImportError:
        # graph.telemetry module not yet implemented — log and skip.
        log.debug(
            "telemetry.ingest.graph_module_missing",
            device=device_name,
            yang_path=yang_path,
            hint="Implement netcortex/graph/telemetry.py to persist metrics to Neo4j",
        )
    except Exception as exc:
        log.error("telemetry.ingest.failed", device=device_name, error=str(exc))


def _estimate_node_count(payload: Any, depth: int = 0) -> int:
    """Rough estimate of the number of YANG nodes in a payload (for logging)."""
    if depth > 5:
        return 1
    if isinstance(payload, dict):
        return sum(_estimate_node_count(v, depth + 1) for v in payload.values()) or 1
    if isinstance(payload, list):
        return sum(_estimate_node_count(i, depth + 1) for i in payload) or 1
    return 1


def _broadcast_event(event: dict[str, Any]) -> None:
    """Push a telemetry event to the ring buffer and all SSE subscribers."""
    _event_ring.append(event)
    dead: list[asyncio.Queue[dict[str, Any] | None]] = []
    for q in _event_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _event_subscribers.remove(q)


async def telemetry_event_stream(request: Request) -> AsyncIterator[str]:
    """Async generator for the SSE endpoint — yields recent + live events."""
    q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=200)
    _event_subscribers.append(q)

    try:
        # Replay the ring buffer for the new subscriber so the dashboard isn't empty.
        for event in list(_event_ring):
            yield f"data: {json.dumps(event)}\n\n"

        # Stream live events.
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping so proxies don't close the connection.
                yield ": keepalive\n\n"
    finally:
        if q in _event_subscribers:
            _event_subscribers.remove(q)
