"""NetCortex background worker — graph discovery, correlation, and NetBox sync.

Discovery cycle per adapter:
  1. adapter.discover() → GraphData
  2. ingest_graph_data(GraphData) → Neo4j
  3. After ALL adapters complete one round: run correlation engine

Invoked via: python -m netcortex.worker
"""

from __future__ import annotations

import asyncio
import signal
import time

import structlog

from netcortex import __version__

log = structlog.get_logger(__name__)

_shutdown = asyncio.Event()

# Default discovery interval per adapter type (seconds)
_DEFAULT_INTERVAL = 300

# Correlation runs once per correlation_interval seconds (regardless of adapters)
_CORRELATION_INTERVAL = 120

# Housekeeping (orphan/stub cleanup) cadence
_HOUSEKEEPING_INTERVAL = 600

# Shared event: set each time any adapter completes a discovery cycle
_discovery_done = asyncio.Event()
_ingest_lock = asyncio.Lock()


async def _discover_and_ingest(adapter, interval_override: int | None = None) -> None:
    """Run one discover → ingest cycle for a single adapter.

    Ingest path is selected at runtime by INGEST_MODE env var:
      direct (default): write to Neo4j inline
      stream:           publish to Redis Stream; an ingest worker handles writes

    Stream mode automatically falls back to direct ingest if Redis is down,
    so the system stays online during Redis outages.
    """
    import os as _os
    from netcortex.graph.ingest import ingest_graph_data

    instance_id = adapter.instance_id
    mode = _os.environ.get("INGEST_MODE", "direct").lower()
    t0 = time.monotonic()
    try:
        log.info("worker.discover_start", instance=instance_id, ingest_mode=mode)
        data = await adapter.discover()
        published = False
        if mode == "stream":
            from netcortex.ingest.queue import publish_graph_data
            entry_id = await publish_graph_data(data)
            if entry_id:
                published = True
                log.info("worker.discover_published",
                         instance=instance_id, entry_id=entry_id,
                         nodes=len(data.nodes), edges=len(data.edges))
        if not published:
            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    async with _ingest_lock:
                        await ingest_graph_data(data)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if "DeadlockDetected" not in str(exc):
                        raise
                    backoff = 0.2 * attempt
                    log.warning(
                        "worker.ingest_deadlock_retry",
                        instance=instance_id,
                        attempt=attempt,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
            if last_exc is not None:
                raise last_exc
        elapsed = round(time.monotonic() - t0, 2)
        log.info("worker.discover_done", instance=instance_id,
                 nodes=len(data.nodes), edges=len(data.edges),
                 elapsed_s=elapsed, mode=("stream" if published else "direct"))
        _discovery_done.set()
    except NotImplementedError:
        log.debug("worker.discover_not_implemented", instance=instance_id)
    except Exception as exc:
        log.error("worker.discover_failed", instance=instance_id, error=str(exc))


async def _adapter_loop(adapter, interval: int) -> None:
    """Run discover → ingest in a loop for one adapter instance."""
    instance_id = adapter.instance_id
    log.info("worker.adapter_loop_start", instance=instance_id, interval=interval)
    while not _shutdown.is_set():
        await _discover_and_ingest(adapter)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass  # normal interval expiry, loop again
    log.info("worker.adapter_loop_stop", instance=instance_id)


async def _correlation_loop(interval: int = _CORRELATION_INTERVAL) -> None:
    """Run the topology + site correlation engines periodically.

    Waits for at least one discovery event, then runs both correlation passes.
    Re-runs every `interval` seconds or whenever new discovery data arrives.
    """
    from netcortex.graph.correlate import run_correlation
    from netcortex.graph.site_correlate import run_site_correlation

    log.info("worker.correlation_loop_start", interval_s=interval)
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_discovery_done.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

        if _shutdown.is_set():
            break

        _discovery_done.clear()

        # MAC/ARP topology correlation
        try:
            t0 = time.monotonic()
            stats = await run_correlation()
            elapsed = round(time.monotonic() - t0, 2)
            log.info("worker.correlation_done", elapsed_s=elapsed, **stats)
        except Exception as exc:
            log.error("worker.correlation_failed", error=str(exc))

        # Site correlation — link PlatformSite → canonical Site
        try:
            cfg = _get_cfg_safe()
            explicit = (cfg or {}).get("site_mappings") if cfg else None
            t0 = time.monotonic()
            sc_stats = await run_site_correlation(explicit_mappings=explicit)
            elapsed = round(time.monotonic() - t0, 2)
            log.info("worker.site_correlate_done", elapsed_s=elapsed, **sc_stats)
        except Exception as exc:
            log.error("worker.site_correlate_failed", error=str(exc))

    log.info("worker.correlation_loop_stop")


async def _housekeeping_loop(interval: int = _HOUSEKEEPING_INTERVAL) -> None:
    """Periodically prune orphaned stub nodes that no real adapter touches anymore.

    A `stub` Device is one inferred from LLDP/CDP discovery — typically a name we
    saw on a neighbour MIB but never confirmed as a real device.  Adapters
    occasionally fail to clear these when the neighbour vanishes (edge purge
    removes edges but not orphan nodes), so we GC any stub Device with no
    remaining relationships every `interval` seconds.

    We also evict orphan RoutingPeer / MACAddress / ARPEntry / IPAddress nodes
    once they lose all incoming edges, since those are pure satellites of a
    parent device and have no standalone meaning.
    """
    from netcortex.graph.client import get_driver

    log.info("worker.housekeeping_loop_start", interval_s=interval)
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass

        try:
            driver = get_driver()
            async with driver.session() as session:
                # Orphan stubs (any node with stub=true and no relationships)
                r1 = await session.run(
                    "MATCH (n) WHERE n.stub = true AND NOT (n)--() "
                    "WITH n LIMIT 5000 DELETE n RETURN count(*) AS n"
                )
                rec = await r1.single()
                stubs = rec["n"] if rec else 0

                # Orphan satellites — never created standalone, only via parent
                deleted_extras = 0
                for label in ("RoutingPeer", "MACAddress", "ARPEntry",
                              "IPAddress", "Prefix"):
                    res = await session.run(
                        f"MATCH (n:{label}) WHERE NOT (n)--() "
                        f"WITH n LIMIT 2000 DELETE n RETURN count(*) AS n"
                    )
                    r = await res.single()
                    deleted_extras += r["n"] if r else 0

                # Per-device / per-network VLAN stubs that never got
                # canonicalised. These are usually Meraki org-level
                # VLANs with no device member, or first-cycle artefacts
                # before LOCATED_AT was set. Canonical VLANs (id starts
                # with 'vlan:') are kept regardless.
                res = await session.run(
                    """
                    MATCH (v:VLAN)
                    WHERE NOT v.id STARTS WITH 'vlan:'
                      AND NOT (v)--()
                    WITH v LIMIT 2000
                    DELETE v
                    RETURN count(*) AS n
                    """
                )
                r = await res.single()
                deleted_extras += r["n"] if r else 0

                # Legacy canonical VLAN nodes with no platform_site_id
                # (e.g. the rogue `vlan:1` / `vlan:20` left over from
                # the pre-fabric-scope MERGE).  A proper canonical VLAN
                # always carries the PlatformSite it lives in; any node
                # missing that property is a leftover and can't be
                # correlated with prefixes or SVIs.  DETACH so any stray
                # HAS_PREFIX edges that survived the correlator pre-step
                # sweep go with them.
                res = await session.run(
                    """
                    MATCH (v:VLAN)
                    WHERE v.id STARTS WITH 'vlan:'
                      AND (v.platform_site_id IS NULL OR v.platform_site_id = '')
                    WITH v LIMIT 2000
                    DETACH DELETE v
                    RETURN count(*) AS n
                    """
                )
                r = await res.single()
                deleted_extras += r["n"] if r else 0

                # Legacy SNMP STP/CAM/ARP/IP poller artefacts.  Before
                # the canonical-id fix, those polls keyed Interface
                # nodes as ``snmp-if:<host_ip>:<ifname>`` (using the
                # SNMP session host) instead of the canonical
                # ``snmp-if:<dev_node_id>:<ifname>`` produced by the
                # counter poll.  The result was an orphan Interface
                # node that no Device pointed at via HAS_INTERFACE, so
                # the ``_decorate_physical_links_stp`` join (and the
                # health-enrichment pass) silently produced zero
                # matches.  Detach-delete every orphan keyed by a bare
                # IPv4 (so the next adapter cycle recreates it with
                # the canonical id).  Keep ``snmp-if:meraki:*`` and
                # ``snmp-if:netbox-device:*`` untouched — those are
                # the correctly-keyed survivors.
                res = await session.run(
                    """
                    MATCH (i:Interface)
                    WHERE i.id =~ '^snmp-if:[0-9]+\\\\.[0-9]+\\\\.[0-9]+\\\\.[0-9]+:.*'
                      AND NOT (()-[:HAS_INTERFACE]->(i))
                    WITH i LIMIT 2000
                    DETACH DELETE i
                    RETURN count(*) AS n
                    """
                )
                r = await res.single()
                deleted_extras += r["n"] if r else 0

                # Legacy STP-poller artefacts. Before Phase 3 the STP
                # walker used dot1dBasePortNumber as if it were ifIndex,
                # which produced Interface nodes named "port-<basePort>"
                # (e.g. "port-181") that never match any LLDP/CDP cable.
                # These pollute the topology with garbage mac_correlation
                # edges. Now that the walker resolves the real ifName via
                # dot1dBasePortIfIndex they will never be re-created;
                # detach-delete every existing instance.
                # We deliberately keep "port-channel*" since those are
                # legitimate Cisco port-channel names.
                res = await session.run(
                    """
                    MATCH (i:Interface)
                    WHERE i.name =~ '^port-[0-9]+$'
                    WITH i LIMIT 2000
                    DETACH DELETE i
                    RETURN count(*) AS n
                    """
                )
                r = await res.single()
                deleted_extras += r["n"] if r else 0

                # Legacy PHYSICAL_LINK edges that carried the same
                # "port-<basePort>" interface name as a property even
                # though the corresponding Interface node is long gone
                # (the MAC-correlation pass minted these from the
                # bogus port names, then the Interface delete above
                # orphaned them).  They show up as one-sided edges
                # (interface_a='port-181', interface_b=null) and have
                # no diagnostic value.  Targeted DELETE keeps the
                # housekeeping budget small.
                res = await session.run(
                    """
                    MATCH ()-[r:PHYSICAL_LINK]-()
                    WHERE r.interface_a =~ '^port-[0-9]+$'
                       OR r.interface_b =~ '^port-[0-9]+$'
                    WITH r LIMIT 2000
                    DELETE r
                    RETURN count(*) AS n
                    """
                )
                r = await res.single()
                deleted_extras += r["n"] if r else 0

                # Collapse reversed PHYSICAL_LINK edges left over from before
                # the ingest layer started canonicalizing direction. We only
                # delete a reverse-direction edge when the SAME (interface_a,
                # interface_b) pair already exists in canonical direction
                # — otherwise we'd wipe legitimate parallel cables whose
                # opposite end happens to have its own (different-port)
                # reverse edge from a stale prior cycle.
                rdup = await session.run(
                    """
                    MATCH (a)-[r1:PHYSICAL_LINK]->(b)
                    WHERE a.id < b.id
                    WITH a, b, collect({
                        rid: id(r1),
                        ia: coalesce(r1.interface_a, ''),
                        ib: coalesce(r1.interface_b, '')
                    }) AS forward
                    MATCH (b)-[r2:PHYSICAL_LINK]->(a)
                    WITH a, b, forward, r2,
                         coalesce(r2.interface_a, '') AS r2_ia,
                         coalesce(r2.interface_b, '') AS r2_ib
                    WHERE any(f IN forward WHERE
                                (f.ia = r2_ib AND f.ib = r2_ia)
                             OR (f.ia = r2_ia AND f.ib = r2_ib))
                    WITH r2 LIMIT 5000
                    DELETE r2
                    RETURN count(*) AS n
                    """
                )
                rdup_rec = await rdup.single()
                collapsed_links = rdup_rec["n"] if rdup_rec else 0

                # Re-sync stale mgmt_ip on Devices where the stored value has
                # drifted from candidate_ips[0]. This can happen when an old
                # discovery cycle wrote mgmt_ip='' (because the platform API
                # was rate-limited or transient-failed and returned no
                # candidate IPs) and the content_hash hasn't changed since
                # — the ingest layer's hash-skip optimization keeps the bad
                # value pinned.
                #
                # dev16: direct heal — set mgmt_ip := candidate_ips[0] in
                # place rather than waiting for the next adapter ingest to
                # rewrite the whole node. The adapter itself now omits
                # empty IP fields entirely (so a partial cycle never
                # clobbers known-good values), but this housekeeping pass
                # repairs nodes that were already corrupted by older runs.
                # _content_hash is cleared too so the next legitimate
                # adapter write isn't hash-skipped against the patched-up
                # value.
                # ── mgmt_ip repair: two-tier strategy ─────────────────────
                #
                # Tier 1 (Meraki appliances): re-apply the operator-mandated
                # MX rule strictly from currently-stored fields. This is
                # idempotent and rule-faithful — it won't promote a public
                # WAN IP over a LAN SVI on a non-SDWAN MX, and won't pick a
                # carrier-side public address over an AutoVPN address on an
                # SDWAN MX. The rule, expressed in Cypher:
                #
                #   if on_sdwan: vpn_ip > wan1_ip > wan2_ip
                #   else (not on_sdwan): wan1_ip > wan2_ip
                #
                # Tier 2 (everything else): fall back to candidate_ips[0],
                # which is what the originating adapter ranked as "best
                # reachable address" for this device.
                #
                # Both tiers only WRITE when the result differs from what's
                # currently stored, so they're free to run every cycle.
                mx_fix = await session.run(
                    """
                    MATCH (d:Device)
                    WHERE d.platform = 'meraki' AND d.role = 'firewall'
                    WITH d,
                         CASE
                           WHEN d.on_sdwan = true THEN
                             coalesce(
                               CASE WHEN d.vpn_ip  IS NOT NULL AND d.vpn_ip  <> '' THEN d.vpn_ip  END,
                               CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                               CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                             )
                           ELSE
                             coalesce(
                               CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                               CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                             )
                         END AS rule_ip
                    WHERE rule_ip IS NOT NULL
                      AND coalesce(d.mgmt_ip, '') <> rule_ip
                    WITH d, rule_ip LIMIT 5000
                    SET d.mgmt_ip = rule_ip
                    REMOVE d._content_hash
                    RETURN count(d) AS n
                    """
                )
                mx_rec = await mx_fix.single()
                mx_resets = mx_rec["n"] if mx_rec else 0

                # Tier 2: generic candidate_ips[0] heal for non-MX devices
                # whose mgmt_ip got stale or empty. Excludes Meraki
                # appliances so we don't fight the strict-rule pass above.
                fix_res = await session.run(
                    """
                    MATCH (d:Device)
                    WHERE NOT (d.platform = 'meraki' AND d.role = 'firewall')
                      AND d.candidate_ips IS NOT NULL
                      AND size(d.candidate_ips) > 0
                      AND (
                            d.mgmt_ip IS NULL
                         OR d.mgmt_ip = ''
                         OR d.mgmt_ip <> d.candidate_ips[0]
                      )
                    WITH d LIMIT 5000
                    SET d.mgmt_ip = d.candidate_ips[0]
                    REMOVE d._content_hash
                    RETURN count(d) AS n
                    """
                )
                fix_rec = await fix_res.single()
                mgmt_ip_resets = (fix_rec["n"] if fix_rec else 0) + mx_resets

                # ── Stamp ``vlans_configured`` (sorted list of VLAN IDs)
                # directly on each Device. Denormalized from
                # LOGICAL_MEMBER → VLAN edges so the UI can render the
                # per-device VLAN footprint without traversing the graph
                # on every hover. Covers VLANs from ANY source adapter
                # (SNMP, NDFC, Meraki, NetBox) since we aggregate over
                # whatever LOGICAL_MEMBER edges the correlator left
                # pointing at the device.
                #
                # Aggregation strategy: we union VLANs across every
                # Device node sharing the same ``name`` and stamp the
                # union on ALL variants. The graph has multiple Device
                # nodes per physical box (one per adapter — meraki:,
                # cdp-neighbor:, snmp-if:, ndfc:, etc.) and only some
                # of those variants ever get the LOGICAL_MEMBER edges
                # because only some adapters poll VLAN inventory. From
                # the operator's perspective there's ONE
                # cpn-ful-cat9k1 with 9 VLANs, so every PHYSICAL_LINK
                # endpoint that resolves to a cat9k1 variant should see
                # the same VLAN footprint regardless of which adapter
                # owns the link anchor.
                #
                # Sorting happens in Python because Neo4j 5 Community
                # doesn't ship ``apoc.coll.sort`` and the pure-Cypher
                # equivalent is painful. Device counts are bounded
                # (~hundreds) so the extra round-trip is cheap.
                vlan_query = await session.run(
                    """
                    MATCH (d:Device) WHERE d.name IS NOT NULL
                    OPTIONAL MATCH (d)-[:LOGICAL_MEMBER]->(v:VLAN)
                    WITH d.name AS name,
                         elementId(d) AS id,
                         d.platform   AS platform,
                         d.vlans_source AS vlans_source,
                         [x IN collect(DISTINCT v.vid)
                          WHERE x IS NOT NULL AND x > 0 AND x < 4095] AS vids,
                         coalesce(d.vlans_configured, []) AS existing
                    RETURN name,
                           collect({id: id,
                                    platform: platform,
                                    vlans_source: vlans_source,
                                    vids: vids,
                                    existing: existing}) AS variants
                    """
                )
                updates: list[dict] = []
                async for row in vlan_query:
                    # Union across all Device variants sharing this name.
                    # For Meraki hardware we ALSO union in whatever the
                    # SNMP adapter stamped directly on the Device as
                    # ``vlans_configured`` (vlans_source='snmp_meraki') —
                    # those devices intentionally do NOT emit per-VID
                    # LOGICAL_MEMBER edges (the MS VLAN table is a
                    # mirror of the org-wide config and would explode
                    # the topology with hundreds of diamonds), so the
                    # only place that footprint lives is on the node
                    # itself.  Without this carry-over the next
                    # housekeeping pass would blank vlans_configured
                    # back to the empty LOGICAL_MEMBER aggregate.
                    union: set[int] = set()
                    for v in row["variants"]:
                        for vid in (v["vids"] or []):
                            if vid is not None:
                                union.add(int(vid))
                        if v.get("vlans_source") == "snmp_meraki":
                            for vid in (v["existing"] or []):
                                if vid is not None:
                                    union.add(int(vid))
                    sorted_vids = sorted(union)
                    for v in row["variants"]:
                        if list(v["existing"] or []) == sorted_vids:
                            continue
                        updates.append({"id": v["id"], "vids": sorted_vids})
                vlans_stamped = len(updates)
                if updates:
                    await session.run(
                        """
                        UNWIND $rows AS row
                        MATCH (d) WHERE elementId(d) = row.id
                        SET d.vlans_configured = row.vids,
                            d.vlan_count = size(row.vids)
                        """,
                        rows=updates,
                    )

            log.info("worker.housekeeping_done",
                     stubs=stubs, orphan_satellites=deleted_extras,
                     collapsed_reverse_links=collapsed_links,
                     mgmt_ip_resets=mgmt_ip_resets,
                     vlans_stamped=vlans_stamped)
        except Exception as exc:
            log.warning("worker.housekeeping_failed", error=str(exc))
    log.info("worker.housekeeping_loop_stop")


async def _netbox_enrich_loop(cfg, interval: int = 300) -> None:
    """Enrich Device + Prefix nodes with NetBox info.

    Runs two enrichment passes per cycle:
      1. Devices — matches graph Devices to NetBox by serial (then name)
         and stamps ``netbox_site_slug`` / ``netbox_id`` so the UI can
         group cross-platform copies of the same device into one
         NetBox-site container.
      2. Prefixes — annotates Prefix nodes with their NetBox VLAN vid
         and site slug.  The correlator uses these to link prefixes to
         the right canonical ``vlan:nb:<slug>:<vid>`` even when on-the-
         wire discovery (SNMP routes, SVI walks) didn't surface the
         VLAN→Prefix association directly.
    """
    from netcortex.sync.netbox_enrich import (
        enrich_devices_from_netbox,
        enrich_prefixes_from_netbox_ipam,
    )

    log.info("worker.netbox_enrich_loop_start", interval_s=interval)
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_discovery_done.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

        if _shutdown.is_set():
            break

        try:
            counts = await enrich_devices_from_netbox(
                cfg.netbox_url,
                cfg.netbox_token,
                verify_ssl=cfg.netbox_verify_ssl,
            )
            log.info("worker.netbox_enrich_done", **counts)
        except Exception as exc:
            log.error("worker.netbox_enrich_failed", error=str(exc))

        try:
            pcounts = await enrich_prefixes_from_netbox_ipam(
                cfg.netbox_url,
                cfg.netbox_token,
                verify_ssl=cfg.netbox_verify_ssl,
            )
            log.info("worker.netbox_prefix_enrich_done", **pcounts)
        except Exception as exc:
            log.error("worker.netbox_prefix_enrich_failed", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
    log.info("worker.netbox_enrich_loop_stop")


async def _netbox_sync_loop(cfg, interval: int = 300) -> None:
    """Sync NetBox Sites and Locations every `interval` seconds (default 15 min)."""
    from netcortex.sync.netbox_sites import sync_netbox_sites

    log.info("worker.netbox_sync_loop_start", interval_s=interval)
    while not _shutdown.is_set():
        try:
            counts = await sync_netbox_sites(cfg.netbox_url, cfg.netbox_token)
            log.info("worker.netbox_sync_done", **counts)
            _discovery_done.set()  # trigger site correlation
        except Exception as exc:
            log.error("worker.netbox_sync_failed", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
    log.info("worker.netbox_sync_loop_stop")


def _get_cfg_safe() -> dict | None:
    """Return the raw core secret dict for optional correlation config, or None."""
    try:
        from netcortex.config import get_settings  # noqa: PLC0415
        get_settings()  # raises if not init'd
        return None  # settings object doesn't expose raw core dict; mappings come via env
    except Exception:
        return None


async def _main() -> None:
    import netcortex.config as cfg_module
    from netcortex.graph import client as neo4j_client
    from netcortex.graph.schema import setup_schema
    from netcortex.adapters import load_instances, get_instances

    log.info("worker.startup", version=__version__)

    # Register OS signals for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)

    # Bootstrap configuration — retry until successful
    retry_delay = 15
    while not _shutdown.is_set():
        try:
            await cfg_module.init_settings()
            log.info("worker.config_loaded")
            break
        except Exception as exc:
            log.error("worker.config_failed", error=str(exc), retry_in=retry_delay)
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=retry_delay)
                return
            except asyncio.TimeoutError:
                pass
    else:
        return

    cfg = cfg_module.get_settings()

    # Neo4j connection
    try:
        await neo4j_client.init_client(cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password)
        await setup_schema()
        log.info("worker.neo4j_ready")
    except Exception as exc:
        log.error("worker.neo4j_failed", error=str(exc))
        # Continue — will retry on next cycle

    # Load adapter instances
    try:
        await load_instances()
    except Exception as exc:
        log.error("worker.adapters_load_failed", error=str(exc))

    instances = get_instances()
    if not instances:
        log.warning("worker.no_adapters",
                    hint="Configure adapter instances in your secret backend")

    # Build per-type intervals from config as the final fallback.
    # Resolution order (most specific wins):
    #   1. adapter._interval  — set by load_instances() from instance or type secret
    #   2. cfg.sync_interval_<type>  — from Settings (populated from secrets/env)
    #   3. cfg.sync_interval  — global default (netcortex/core sync_interval or 300s)
    type_interval_map: dict[str, int] = {
        "meraki":           cfg.sync_interval_meraki,
        "catalyst_center":  cfg.sync_interval_catalyst_center,
        "nexus_dashboard":  cfg.sync_interval_nexus_dashboard,
        "intersight":       cfg.sync_interval_intersight,
        "snmp":             cfg.sync_interval_snmp,
        "generic_rest":     cfg.sync_interval_generic_rest,
    }

    # Launch one discovery loop per adapter + the correlation loop
    tasks: list[asyncio.Task] = []
    for instance_id, adapter in instances.items():
        interval = (
            adapter._interval                              # instance / type / global from secrets
            or type_interval_map.get(adapter.name)        # type-level from Settings
            or cfg.sync_interval                          # global default
        )
        task = asyncio.create_task(
            _adapter_loop(adapter, interval),
            name=f"discover-{instance_id}",
        )
        tasks.append(task)

    # NetBox site/location sync is disabled — canonical Site/Location nodes
    # are no longer shown in the topology view.  Re-enable here if needed.
    # tasks.append(asyncio.create_task(
    #     _netbox_sync_loop(cfg, interval=cfg.sync_interval_netbox_sites),
    #     name="netbox-sites",
    # ))

    # NetBox device enrichment — enriches Device nodes with site info from NetBox
    # and marks cross-platform duplicates (same serial from multiple adapters).
    tasks.append(asyncio.create_task(
        _netbox_enrich_loop(cfg, interval=cfg.sync_interval),
        name="netbox-enrich",
    ))

    # Correlation loop runs regardless of whether adapters are configured
    tasks.append(asyncio.create_task(_correlation_loop(), name="correlation"))

    # Periodic GC of orphan stubs and satellite nodes
    tasks.append(asyncio.create_task(_housekeeping_loop(), name="housekeeping"))

    if len(tasks) == 1:  # only correlation, no adapters
        log.info("worker.idle", reason="no adapters configured")
        await _shutdown.wait()
    else:
        await asyncio.gather(*tasks, return_exceptions=True)

    await neo4j_client.close()
    log.info("worker.shutdown")


if __name__ == "__main__":
    asyncio.run(_main())
