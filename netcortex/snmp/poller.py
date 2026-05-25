"""Standalone, shardable SNMP poller process.

Each instance:
  1. Boots config + Neo4j + Redis.
  2. Registers itself in the shard registry (heartbeat every 10s).
  3. Loads the same SnmpAdapter instances as the main worker.
  4. On every cycle, polls *only* the targets it owns according to
     consistent-hash partitioning across the live poller cohort.
  5. Publishes results to the Redis ingest stream (Phase C) instead of
     writing Neo4j directly, so the ingest worker pool absorbs them.

This lets you scale SNMP polling horizontally:

    docker compose --profile snmp-poller up -d \
        --scale netcortex-snmp-poller=4

The shard registry handles join/leave: a dying poller's targets are
automatically picked up by the rest of the cohort on the next cycle.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

import structlog

from netcortex import __version__

log = structlog.get_logger(__name__)

_shutdown = asyncio.Event()


async def _poll_one_adapter(adapter, registry, cycle_no: int) -> None:
    """Run discover on one adapter, but filter its target list by our shard."""
    from netcortex.graph.models import GraphData
    from netcortex.ingest.queue import publish_graph_data

    # Save original target list and replace with our shard's slice so the
    # adapter only polls hosts we own.
    original_targets = getattr(adapter, "_targets", None)
    sharded = None
    if isinstance(original_targets, list) and original_targets:
        sharded = [t for t in original_targets if registry.owns(t)]
        adapter._targets = sharded

    log.info(
        "snmp_poller.cycle_start",
        cycle=cycle_no,
        poller=registry.poller_id,
        cohort_size=registry.total,
        original_targets=len(original_targets) if original_targets else 0,
        my_targets=len(sharded) if sharded is not None else "n/a",
    )

    t0 = time.monotonic()
    try:
        data: GraphData = await adapter.discover()
    finally:
        # Always restore so re-shard on next cycle is clean
        if original_targets is not None:
            adapter._targets = original_targets

    elapsed = round(time.monotonic() - t0, 2)
    entry_id = await publish_graph_data(data)
    log.info(
        "snmp_poller.cycle_done",
        cycle=cycle_no,
        poller=registry.poller_id,
        nodes=len(data.nodes),
        edges=len(data.edges),
        elapsed_s=elapsed,
        published=bool(entry_id),
        entry_id=entry_id,
    )


async def _poller_loop(adapter, registry, interval: int) -> None:
    log.info("snmp_poller.loop_start",
             instance=adapter.instance_id, interval=interval,
             poller=registry.poller_id)
    cycle = 0
    while not _shutdown.is_set():
        cycle += 1
        try:
            await _poll_one_adapter(adapter, registry, cycle)
        except Exception as exc:
            log.error("snmp_poller.cycle_failed",
                       instance=adapter.instance_id, error=str(exc))
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
    log.info("snmp_poller.loop_stop", instance=adapter.instance_id)


async def _main() -> None:
    import netcortex.config as cfg_module
    from netcortex.graph import client as neo4j_client
    from netcortex.adapters import load_instances, get_instances
    from netcortex.snmp.shard import ShardRegistry

    log.info("snmp_poller.startup", version=__version__)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)

    # Bootstrap config + Neo4j (we still need Neo4j read-access to enumerate
    # graph-derived targets in adapters that read from Neo4j).
    while not _shutdown.is_set():
        try:
            await cfg_module.init_settings()
            break
        except Exception as exc:
            log.error("snmp_poller.config_failed", error=str(exc), retry_in=15)
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=15)
                return
            except asyncio.TimeoutError:
                pass
    else:
        return

    cfg = cfg_module.get_settings()
    try:
        await neo4j_client.init_client(cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password)
        log.info("snmp_poller.neo4j_ready")
    except Exception as exc:
        log.warning("snmp_poller.neo4j_failed", error=str(exc))

    # Register in shard cohort
    registry = ShardRegistry()
    await registry.start()
    log.info("snmp_poller.registered",
             poller=registry.poller_id, cohort_size=registry.total)

    # Load adapter instances and only run SNMP ones
    try:
        await load_instances()
    except Exception as exc:
        log.error("snmp_poller.adapters_load_failed", error=str(exc))

    instances = get_instances()
    snmp_instances = [a for a in instances.values() if a.name == "snmp"]
    if not snmp_instances:
        log.warning("snmp_poller.no_snmp_adapters")
        await _shutdown.wait()
        await registry.stop()
        return

    interval = (
        cfg.sync_interval_snmp
        or cfg.sync_interval
        or 300
    )

    tasks = [
        asyncio.create_task(_poller_loop(a, registry, interval),
                             name=f"snmp-poller-{a.instance_id}")
        for a in snmp_instances
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await registry.stop()
        await neo4j_client.close()
        log.info("snmp_poller.shutdown")


if __name__ == "__main__":
    asyncio.run(_main())
