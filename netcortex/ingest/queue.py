"""Redis Streams transport for GraphData payloads.

Producer side (adapter loop):  publish_graph_data(data)
Consumer side (ingest worker): async for batch in consume_graph_data(...): ...

Stream layout:
  Key:    netcortex:ingest
  Group:  ingest-workers
  Entry:  { "adapter": str, "payload": <gzipped JSON of GraphData.model_dump()> }

We gzip the payload so very large GraphData (10k+ nodes) stays under Redis
entry size limits and roundtrip latency stays low.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import structlog

from netcortex.graph.models import GraphData

log = structlog.get_logger(__name__)

_STREAM_KEY = "netcortex:ingest"
_GROUP = "ingest-workers"
_DEFAULT_MAXLEN = 5000  # entries, approximate (XADD MAXLEN ~)


# Cache the redis client per-event-loop so we don't reconnect on every publish.
_clients: dict[int, Any] = {}
_clients_lock = asyncio.Lock()


async def _client():
    """Return a connected redis.asyncio client (lazily created per loop)."""
    try:
        import redis.asyncio as aioredis
    except ImportError:
        raise RuntimeError("redis package not installed — pip install redis>=5") from None

    loop_id = id(asyncio.get_running_loop())
    async with _clients_lock:
        cli = _clients.get(loop_id)
        if cli is not None:
            return cli
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        cli = aioredis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        # Verify it's actually live
        await cli.ping()
        _clients[loop_id] = cli
        return cli


def _encode(data: GraphData) -> str:
    """Serialize GraphData to a compact, transport-safe string."""
    raw = data.model_dump(mode="json")
    blob = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(blob, compresslevel=6)).decode("ascii")


def _decode(s: str | bytes) -> GraphData:
    """Reverse of _encode."""
    if isinstance(s, bytes):
        s = s.decode("ascii")
    blob = gzip.decompress(base64.b64decode(s))
    return GraphData.model_validate_json(blob)


async def publish_graph_data(data: GraphData, maxlen: int = _DEFAULT_MAXLEN) -> str | None:
    """Publish a GraphData payload onto the ingest stream.

    Returns the new Redis stream entry id on success, or None on failure.
    Failure is intentionally non-raising so the adapter loop can fall back
    to direct ingest.
    """
    try:
        cli = await _client()
    except Exception as exc:
        log.warning("ingest.publish.no_redis", error=str(exc))
        return None

    try:
        payload = _encode(data)
        # MAXLEN ~ caps stream length so unread entries don't grow unbounded.
        entry_id = await cli.xadd(
            _STREAM_KEY,
            {"adapter": data.adapter_id, "payload": payload,
             "nodes": str(len(data.nodes)), "edges": str(len(data.edges))},
            maxlen=maxlen,
            approximate=True,
        )
        log.debug("ingest.publish.ok", adapter=data.adapter_id,
                  entry_id=entry_id if isinstance(entry_id, str) else entry_id.decode("utf-8"),
                  nodes=len(data.nodes), edges=len(data.edges))
        return entry_id if isinstance(entry_id, str) else entry_id.decode("utf-8")
    except Exception as exc:
        log.warning("ingest.publish.failed", adapter=data.adapter_id, error=str(exc))
        return None


async def _ensure_group(cli) -> None:
    """Create the consumer group if it does not already exist (idempotent)."""
    try:
        # MKSTREAM creates the stream if it doesn't exist yet.
        await cli.xgroup_create(_STREAM_KEY, _GROUP, id="$", mkstream=True)
        log.info("ingest.consume.group_created", stream=_STREAM_KEY, group=_GROUP)
    except Exception as exc:
        # BUSYGROUP means the group already exists — fine.
        if "BUSYGROUP" not in str(exc):
            raise


async def consume_graph_data(
    consumer_name: str,
    block_ms: int = 5000,
    batch_size: int = 16,
) -> AsyncIterator[tuple[str, GraphData]]:
    """Yield (entry_id, GraphData) tuples from the stream.

    The caller is responsible for XACKing each entry after successful ingest:
        async for entry_id, data in consume_graph_data("worker-1"):
            await ingest_graph_data(data)
            await ack_entry(entry_id)

    `consumer_name` should be unique per process so pending-list bookkeeping
    works correctly when one worker dies mid-batch.
    """
    cli = await _client()
    await _ensure_group(cli)

    while True:
        try:
            resp = await cli.xreadgroup(
                _GROUP,
                consumer_name,
                {_STREAM_KEY: ">"},
                count=batch_size,
                block=block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The redis client raises a "Timeout reading from redis" exception
            # when XREADGROUP's block period expires with no new entries.
            # That's the normal idle path — log at debug, not warning.
            err = str(exc)
            if "Timeout reading from" in err:
                log.debug("ingest.consume.idle_block", error=err)
            else:
                log.warning("ingest.consume.read_failed", error=err)
            await asyncio.sleep(0.5)
            continue

        if not resp:
            continue  # block timed out — loop and try again

        for _stream_key, entries in resp:
            for entry_id, fields in entries:
                eid = entry_id if isinstance(entry_id, str) else entry_id.decode("utf-8")
                try:
                    raw = fields.get(b"payload") or fields.get("payload")
                    data = _decode(raw)
                except Exception as exc:
                    log.warning("ingest.consume.decode_failed",
                                entry_id=eid, error=str(exc))
                    # ACK garbage entries so we don't loop on them forever
                    try:
                        await cli.xack(_STREAM_KEY, _GROUP, entry_id)
                    except Exception:
                        pass
                    continue
                yield eid, data


async def ack_entry(entry_id: str) -> None:
    """Acknowledge successful processing of a stream entry."""
    cli = await _client()
    try:
        await cli.xack(_STREAM_KEY, _GROUP, entry_id)
    except Exception as exc:
        log.warning("ingest.consume.ack_failed", entry_id=entry_id, error=str(exc))


async def stream_stats() -> dict[str, Any]:
    """Return current stream depth and pending counts for observability."""
    try:
        cli = await _client()
        info = await cli.xinfo_stream(_STREAM_KEY)
        try:
            pending = await cli.xpending(_STREAM_KEY, _GROUP)
        except Exception:
            pending = None
        return {
            "length": info.get(b"length") or info.get("length") if isinstance(info, dict) else None,
            "first_id": info.get(b"first-entry") if isinstance(info, dict) else None,
            "pending": pending,
        }
    except Exception as exc:
        return {"error": str(exc)}
