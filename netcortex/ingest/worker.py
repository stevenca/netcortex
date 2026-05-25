"""Ingest worker — consumes GraphData payloads from Redis Streams.

Run with:
    python -m netcortex.ingest.worker

Multiple instances can run in parallel (different consumer names) — Redis
Streams' consumer group semantics distribute entries across them.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time

import structlog

from netcortex import __version__

log = structlog.get_logger(__name__)

_shutdown = asyncio.Event()


async def _ingest_one(entry_id: str, data) -> bool:
    """Ingest a single GraphData payload.  Returns True on success."""
    from netcortex.graph.ingest import ingest_graph_data

    t0 = time.monotonic()
    try:
        await ingest_graph_data(data)
        elapsed = round(time.monotonic() - t0, 2)
        log.info(
            "ingest_worker.entry_done",
            entry_id=entry_id,
            adapter=data.adapter_id,
            nodes=len(data.nodes),
            edges=len(data.edges),
            elapsed_s=elapsed,
        )
        return True
    except Exception as exc:
        log.error("ingest_worker.entry_failed",
                  entry_id=entry_id,
                  adapter=getattr(data, "adapter_id", "unknown"),
                  error=str(exc))
        return False


async def _consume_loop(consumer_name: str) -> None:
    """Main consumer loop — pulls from the stream and ingests."""
    from netcortex.ingest.queue import consume_graph_data, ack_entry

    log.info("ingest_worker.loop_start", consumer=consumer_name)
    async for entry_id, data in consume_graph_data(consumer_name):
        if _shutdown.is_set():
            break
        ok = await _ingest_one(entry_id, data)
        if ok:
            await ack_entry(entry_id)
        # Failed entries stay in the pending list — they'll be retried by
        # a future XCLAIM (Phase E will add a reaper for stuck entries).
    log.info("ingest_worker.loop_stop", consumer=consumer_name)


async def _main() -> None:
    import netcortex.config as cfg_module
    from netcortex.graph import client as neo4j_client
    from netcortex.graph.schema import setup_schema

    log.info("ingest_worker.startup", version=__version__)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)

    # Bootstrap config + Neo4j
    retry_delay = 15
    while not _shutdown.is_set():
        try:
            await cfg_module.init_settings()
            break
        except Exception as exc:
            log.error("ingest_worker.config_failed",
                      error=str(exc), retry_in=retry_delay)
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=retry_delay)
                return
            except asyncio.TimeoutError:
                pass
    else:
        return

    cfg = cfg_module.get_settings()
    try:
        await neo4j_client.init_client(cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password)
        await setup_schema()
        log.info("ingest_worker.neo4j_ready")
    except Exception as exc:
        log.error("ingest_worker.neo4j_failed", error=str(exc))
        return

    # Unique-per-process consumer name so Redis can track pending entries
    consumer_name = os.environ.get(
        "INGEST_CONSUMER_NAME",
        f"{socket.gethostname()}-{os.getpid()}",
    )

    try:
        await _consume_loop(consumer_name)
    except asyncio.CancelledError:
        pass
    finally:
        await neo4j_client.close()
        log.info("ingest_worker.shutdown")


if __name__ == "__main__":
    asyncio.run(_main())
