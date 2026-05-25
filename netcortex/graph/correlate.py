"""Topology correlation engine.

After every adapter discovery cycle this module runs Cypher-based correlation
queries that infer PHYSICAL_LINK edges from:

  1. MAC correlation  — a device's NIC MAC (OWNS_MAC) matches a MAC learned on a
                         switch port (LEARNED_MAC).  Confidence: 0.85
  2. ARP correlation  — an ARP entry (HAS_ARP) resolves to an IP address that is
                         assigned to another device's interface (ASSIGNED_IP).
                         Confidence: 0.65

Inferred edges carry `source: "correlated"` and a `confidence` score so they
can be distinguished from topology discovered directly by an adapter.
"""

from __future__ import annotations

import ipaddress
import re

import structlog

from netcortex.graph.client import get_driver

log = structlog.get_logger(__name__)


# ── Interface-name port-tail helpers (dev17) ─────────────────────────────────
#
# Cisco multi-rate ports expose multiple IF-MIB rows for the same physical
# port — e.g. a 1/1/5 SFP+/SFP28 cage shows up as both
# ``TenGigabitEthernet1/1/5`` (10G logical port) and ``TwentyFiveGigE1/1/5``
# (25G logical port). Exactly one is the active variant at a time; the
# other is an inactive shadow whose SNMP vmVlan defaults to access-VLAN-1
# (the documented MIB default for any port not in an access VLAN, including
# routed L3 ports).
#
# LLDP/CDP can advertise either variant in the connected-port TLV
# depending on negotiated speed, so the PHYSICAL_LINK can end up anchored
# to the shadow. Without sibling-aware decoration the link's L2 facts
# reflect the shadow ("access vlan 1") instead of the active trunk.
#
# ``_port_tail`` extracts the chassis/slot/port digits at the end of an
# interface name; two interfaces on the same Device that share the same
# tail (e.g. ``1/1/5``) are considered speed-variant siblings.

_PORT_TAIL_RE = re.compile(r"[0-9].*$")


def _port_tail(name: str | None) -> str:
    """Return the numeric port-id suffix of a Cisco-style interface name.

    Examples:
      * ``TenGigabitEthernet1/1/5`` → ``1/1/5``
      * ``TwentyFiveGigE1/1/5``     → ``1/1/5``
      * ``Ethernet1/46``            → ``1/46``
      * ``Port-channel1``           → ``1``
      * ``Vlan80``                  → ``80``
    """
    if not name:
        return ""
    m = _PORT_TAIL_RE.search(str(name))
    return m.group(0) if m else ""


def _l2_rank(iface: dict | None) -> int:
    """Score how authoritative an Interface's L2 state looks.

    Higher = stronger signal. Used to pick the right sibling when a
    PHYSICAL_LINK could be anchored to either of two speed variants.

    Ranking rationale:
      * 100 — trunk with a populated ``vlans_allowed`` list (gold)
      *  80 — trunk with no allowed list (still authoritative)
      *  60 — access VLAN ≠ 1 (real config, not the vmVlan default)
      *  20 — access VLAN = 1 with no other L2 facts (suspicious — this
              is what Cisco's vmVlan returns for any port that isn't in
              an access VLAN, e.g. a routed L3 port or the inactive
              shadow of a multi-rate port)
      *  10 — has trunk_mode set but doesn't fit above
      *   0 — no L2 facts at all
    """
    if not iface:
        return -1
    tm = iface.get("trunk_mode")
    if not tm:
        return 0
    if tm == "trunk" and iface.get("vlans_allowed"):
        return 100
    if tm == "trunk":
        return 80
    access = iface.get("vlans_access")
    if tm == "access" and access is not None and access != 1:
        return 60
    if tm == "access" and access == 1:
        return 20
    return 10


def _resolve_active_iface(
    link_iface_name: str,
    device_ifaces: list[dict],
) -> dict | None:
    """Pick the most-authoritative Interface entry for one side of a link.

    Considers:
      1. all Interface nodes on the device whose canonical name matches
         the link's anchor name (lower-cased), AND
      2. all Interface nodes whose port_tail matches the anchor's tail
         (sibling speed variants of the same physical port).

    Returns the highest-ranked candidate (see ``_l2_rank``). If no
    candidate exists or none have any L2 facts, returns None.
    """
    if not link_iface_name or not device_ifaces:
        return None
    anchor_lower = link_iface_name.lower()
    anchor_tail = _port_tail(link_iface_name)
    candidates = []
    for iface in device_ifaces:
        nm = (iface.get("name") or "").lower()
        if not nm:
            continue
        if nm == anchor_lower:
            candidates.append(iface)
        elif anchor_tail and _port_tail(iface.get("name")) == anchor_tail:
            candidates.append(iface)
    if not candidates:
        return None
    best = max(candidates, key=_l2_rank)
    if _l2_rank(best) <= 0:
        return None
    return best


def _vlans_set_for(iface: dict | None) -> list[int] | None:
    """Return the carried-VLAN set for one end of a link."""
    if not iface:
        return None
    tm = iface.get("trunk_mode")
    if tm == "trunk" and iface.get("vlans_allowed"):
        return list(iface["vlans_allowed"])
    if tm == "access" and iface.get("vlans_access") is not None:
        return [iface["vlans_access"]]
    return None


async def run_correlation() -> dict[str, int]:
    """Run all correlation passes and return counts of created/merged edges.

    Pass order matters:
      1. Stub→real device merge — drops phantom inventory entries first.
      2. MAC + ARP correlation — only adds edges between pairs that don't
         already have a higher-confidence LLDP/CDP/topology link.
      3. Dedupe PHYSICAL_LINK pairs — collapses the same (a,b) pair when
         multiple discovery_proto rows piled up; keeps the highest-priority
         edge per pair (LLDP/CDP > catc_topology > NDFC > mac/arp).
      4. Normalize interface names on edges (vl80 → Vlan80) so the UI
         doesn't show duplicate-looking labels.
      5. Health enrichment.
    """
    # Stub→real merge runs in three increasingly-fuzzy passes; each one
    # is allowed to consume stubs the previous pass left behind, so by the
    # end any stub still in the graph genuinely has no canonical match.
    #   1. chassis_mac     — deterministic (LLDP chassisIdSubtype=4)
    #   2. mgmt_ip         — deterministic (LLDP mgmtAddr / CDP cacheAddr)
    #   3. hostname        — best effort, the legacy behaviour
    stubs_by_mac  = await _merge_neighbor_stubs_by_chassis_mac()
    stubs_by_ip   = await _merge_neighbor_stubs_by_mgmt_ip()
    stubs_merged  = await _merge_neighbor_stubs_by_name()
    # Pick up any stub that some OTHER pass (notably the NetBox-enrich
    # name-matcher) has already stamped with `canonical_id` but never
    # actually merged.  Those stubs sit in the graph with their edges
    # invisible to the topology view (the canonical_id filter hides
    # them) — running absorb here re-points their PHYSICAL_LINK edges
    # onto the canonical and deletes the stub, restoring visibility.
    stubs_by_canon_id = await _absorb_stubs_with_canonical_id()
    stubs_rehomed = await _rehome_unmerged_stubs_to_peer_site()

    # Collapse per-device / per-network VLAN stubs into one canonical
    # node per (fabric, vid). Runs before any L2 link decoration so the
    # downstream passes always operate on the canonical id.
    vlans_canonicalised = await _canonicalize_vlans_per_fabric()
    vlan_svis_linked    = await _link_vlan_svis_and_prefixes()
    # Fold associated Prefix CIDRs into VLAN.prefix_v4 / prefix_v6 so the
    # UI can render "VLAN 11 · Infrastructure / 192.168.11.0/24" as a
    # single node instead of a VLAN + dangling Prefix pair.  Runs AFTER
    # _link_vlan_svis_and_prefixes so the SVI-derived prefixes are in
    # the HAS_PREFIX set we read from.
    vlan_labels_decorated = await _decorate_vlan_labels_with_prefixes()

    mac_links = await _correlate_via_mac()
    arp_links = await _correlate_via_arp()
    dedup_links = await _dedupe_physical_links_by_pair()
    norm_ifaces = await _normalize_physical_link_interfaces()
    enriched = await _enrich_physical_links_with_health()
    vendors_filled = await _enrich_mac_vendors()

    # Decorate PHYSICAL_LINK edges with L1/L2/L3 attributes so the UI
    # can render one edge per cable and color/label by overlay. This
    # runs AFTER physical-link dedupe/normalization so we don't waste
    # work on edges that are about to be deleted.
    # Decoration queries match Interface.name_canonical to
    # PHYSICAL_LINK.interface_a/b. Make sure every Interface has a
    # canonical name populated FIRST or the JOIN will silently miss.
    canon_iface_names = await _populate_interface_canonical_names()

    decorated_l2  = await _decorate_physical_links_l2()
    decorated_stp = await _decorate_physical_links_stp()
    decorated_l3  = await _decorate_physical_links_l3()

    # Stitch SNMP-discovered STPDomains into the Meraki-discovered
    # ones when both share a root device.  Without this, a Nexus
    # peered into a Meraki MS390 site shows up in a separate
    # unnamed `stp-domain:<bridge-id>` instead of being absorbed
    # into the named `STP <site>` tree.
    stp_domains_merged = await _merge_redundant_stp_domains()

    # Project the per-domain STP role data sitting on STP_ROOT /
    # STP_MEMBER edges directly onto each Device node, so the topology
    # view can light up STP membership without traversing a separate
    # STPDomain node (which we now hide from the UI — it carried zero
    # information that wasn't already implied by the membership edges).
    stp_devices_decorated = await _decorate_devices_with_stp_membership()

    # Stamp STP context onto each PHYSICAL_LINK and onto every
    # Device's "how many in-domain peers can I reach over cable"
    # counter.  These two derived facts are what the UI uses to:
    #   * draw inter-domain trunks differently from intra-domain
    #     spanning-tree cables (without this every cable in the
    #     site looks like the active tree)
    #   * flag orphan STP members — devices that claim membership
    #     in an STP domain but have no PHYSICAL_LINK to any other
    #     member (typical for "phantom" roots that Meraki's cloud
    #     remembers from decommissioned hardware).
    stp_topology_stamped = await _stamp_stp_link_topology()

    # WAN topology — infer site-Internet uplinks for every L3 edge
    # device.  Three discovery rules, in priority order:
    #
    #   1. Meraki MX devices that carry a wan{1,2}_public_ip property
    #      always emit a direct Device → Internet WAN_UPLINK tagged
    #      with the public IP and the wan slot.
    #   2. Devices with at least one established ROUTING_PEER edge whose
    #      remote_as falls outside private ASN space get a
    #      Device → AutonomousSystem → Internet path (the AS sells
    #      transit so we synthesize the TRANSITS edge too).
    #   3. (Future) Devices with an explicit 0.0.0.0/0 ROUTES_TO edge
    #      will route through the next-hop's interface.
    #
    # The pass creates Internet / AutonomousSystem nodes on demand,
    # tags devices as is_wan_edge=true, and clears stale uplinks for
    # devices that no longer satisfy any rule.
    wan_uplinks = await _infer_wan_topology()

    # Attach every Device to every canonical NetBox VLAN whose site
    # slug matches the device's site slug.  This fills in the
    # "infrastructure VLAN is reachable from every device at the
    # site" relationship that no adapter currently emits explicitly
    # (Meraki's per-org VLAN list isn't joined to device membership;
    # Cisco SNMP gives port-level VLANs only for switches).  Lets the
    # UI render VLAN 1 connected to every device at a site instead of
    # floating disconnected.
    site_vlan_members = await _attach_devices_to_site_vlans()

    # After L3 link decoration and VLAN label decoration have run, any
    # Prefix whose CIDR is already shown as part of a VLAN label or a
    # PHYSICAL_LINK annotation can be hidden — its standalone node is
    # noise.  Runs LAST among decoration passes so it sees the final
    # state of both VLAN.prefix_v4/v6 and PHYSICAL_LINK.l3_prefix.
    prefixes_absorbed = await _mark_absorbed_prefixes()

    # Phase 4: emit device-to-device ROUTING_PEER adjacency edges so
    # the UI can render BGP/OSPF/EIGRP sessions as their own dashed
    # lines coloured by oper_status (separate from the L1/L2 cable).
    # Also scrubs the previous design's PHYSICAL_LINK.routing_protocols
    # / ROUTES_OVER_UNKNOWN remnants.
    routing_collapsed = await _collapse_routing_peers()

    # Phase 5a: maintain status-transition history + flap stats on
    # every Device / link / routing peer.  Must run AFTER all the
    # correlators that set oper_status (PHYSICAL_LINK enrichment,
    # WAN topology inference, routing-peer collapse) so we see the
    # final per-cycle value before recording any transition.  Pure
    # decision logic lives in ``netcortex.graph.history``.
    perf_history_stats = await _update_link_performance_history()
    history_stats = await _update_status_history()

    # Phase 5b: stamp universal observability timestamps on every node
    # and edge that doesn't yet have them, so the UI can always render
    # "first seen / last seen / up since X" no matter which adapter or
    # correlator created the object.  Runs LAST so it sees every node
    # and edge written or refreshed in this cycle.
    freshness_stats = await _stamp_freshness()

    total = mac_links + arp_links
    log.info("correlate.done",
             stubs_merged_by_chassis_mac=stubs_by_mac,
             stubs_merged_by_mgmt_ip=stubs_by_ip,
             stubs_merged_by_name=stubs_merged,
             stubs_merged_by_canonical_id=stubs_by_canon_id,
             stubs_rehomed_to_peer_site=stubs_rehomed,
             vlans_canonicalised=vlans_canonicalised,
             vlan_svis_linked=vlan_svis_linked,
             vlan_labels_decorated=vlan_labels_decorated,
             mac_links=mac_links, arp_links=arp_links,
             physical_links_deduped=dedup_links,
             iface_names_normalized=norm_ifaces,
             links_enriched_with_health=enriched,
             iface_canonical_names_set=canon_iface_names,
             links_decorated_l2=decorated_l2,
             links_decorated_stp=decorated_stp,
             links_decorated_l3=decorated_l3,
             stp_domains_merged=stp_domains_merged,
             stp_devices_decorated=stp_devices_decorated,
             stp_topology_stamped=stp_topology_stamped,
             wan_uplinks=wan_uplinks,
             site_vlan_members=site_vlan_members,
             prefixes_absorbed=prefixes_absorbed,
             routing_collapsed=routing_collapsed,
             mac_vendors_filled=vendors_filled,
             freshness_nodes_touched=freshness_stats.get("nodes_touched", 0),
             freshness_edges_touched=freshness_stats.get("edges_touched", 0),
             devices_status_backfilled=freshness_stats.get("devices_status_backfilled", 0),
             history_device_transitions=history_stats.get("device_transitions", 0),
             history_link_transitions=history_stats.get("physical_link_transitions", 0),
             history_peer_transitions=history_stats.get("routing_peer_transitions", 0),
             history_wan_transitions=history_stats.get("wan_uplink_transitions", 0),
             history_sdwan_transitions=history_stats.get("sdwan_tunnel_transitions", 0),
             link_perf_history_updated=perf_history_stats.get("links_history_updated", 0),
             link_health_adjusted_by_util_1h=perf_history_stats.get("links_health_adjusted", 0),
             total=total)
    return {"stubs_merged_by_chassis_mac": stubs_by_mac,
            "stubs_merged_by_mgmt_ip": stubs_by_ip,
            "stubs_merged_by_name": stubs_merged,
            "stubs_merged_by_canonical_id": stubs_by_canon_id,
            "stubs_rehomed_to_peer_site": stubs_rehomed,
            "vlans_canonicalised": vlans_canonicalised,
            "vlan_svis_linked": vlan_svis_linked,
            "vlan_labels_decorated": vlan_labels_decorated,
            "iface_canonical_names_set": canon_iface_names,
            "links_decorated_l2": decorated_l2,
            "links_decorated_stp": decorated_stp,
            "links_decorated_l3": decorated_l3,
            "stp_domains_merged": stp_domains_merged,
            "stp_devices_decorated": stp_devices_decorated,
            "stp_topology_stamped": stp_topology_stamped,
            "wan_uplinks": wan_uplinks,
            "site_vlan_members": site_vlan_members,
            "prefixes_absorbed": prefixes_absorbed,
            "routing_collapsed": routing_collapsed,
            "mac_links": mac_links, "arp_links": arp_links,
            "physical_links_deduped": dedup_links,
            "iface_names_normalized": norm_ifaces,
            "links_enriched_with_health": enriched,
            "mac_vendors_filled": vendors_filled,
            "freshness_nodes_touched": freshness_stats.get("nodes_touched", 0),
            "freshness_edges_touched": freshness_stats.get("edges_touched", 0),
            "devices_status_backfilled": freshness_stats.get("devices_status_backfilled", 0),
            "link_perf_history_updated": perf_history_stats.get("links_history_updated", 0),
            "link_health_adjusted_by_util_1h": perf_history_stats.get("links_health_adjusted", 0),
            "total": total}


# ── Helpers shared by chassis_mac / mgmt_ip / hostname merge passes ──────────

# Cypher fragment that, given $stub_id + $real_id parameters, redirects
# every PHYSICAL_LINK edge touching the stub onto the real device and
# then DETACH DELETEs the stub. The MERGE keys on the interface pair so
# parallel cables (three cables to the same neighbor) each survive as
# their own edge — same reasoning as Step 2 in the legacy name-merge
# below.
# IMPORTANT: after MERGE we re-stamp ``source_adapter = 'correlator'``.
# The per-adapter purge in ``ingest.ingest_graph_data`` deletes every
# relationship whose ``source_adapter`` matches the incoming batch's
# ``adapter_id`` (e.g. ``snmp/default``) at the START of the next
# ingest of that adapter.  Without re-tagging, every redirected cable
# inherits its original ``snmp/default`` adapter id from ``p`` and is
# wiped on the next SNMP poll — causing canonical devices (e.g.
# cpn-ful-n9k1) to flap between "17 visible neighbors" and "3
# visible neighbors" depending on whether stub-merge ran in the
# current correlator cycle.  Tagging the promoted edge with
# ``correlator`` makes it idempotent across SNMP cycles, while
# ``original_source_adapter`` preserves provenance for debugging.
_REDIRECT_INBOUND_CYPHER = """
MATCH (stub:Device {id: $stub_id})
MATCH (real:Device {id: $real_id})
WITH stub, real
OPTIONAL MATCH (peer)-[r:PHYSICAL_LINK]->(stub)
WHERE peer.id <> real.id
WITH stub, real, peer, r, properties(r) AS p
FOREACH (_ IN CASE WHEN peer IS NULL THEN [] ELSE [1] END |
    MERGE (peer)-[nr:PHYSICAL_LINK {
        interface_a: coalesce(p.interface_a, ''),
        interface_b: coalesce(p.interface_b, '')
    }]->(real)
    SET nr += p,
        nr.original_source_adapter = coalesce(
            nr.original_source_adapter,
            p.source_adapter,
            nr.source_adapter
        ),
        nr.source_adapter   = 'correlator',
        nr.merged_from_stub = stub.id
)
WITH r
FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
    DELETE r
)
"""

_REDIRECT_OUTBOUND_CYPHER = """
MATCH (stub:Device {id: $stub_id})
MATCH (real:Device {id: $real_id})
WITH stub, real
OPTIONAL MATCH (stub)-[r:PHYSICAL_LINK]->(peer)
WHERE peer.id <> real.id
WITH stub, real, peer, r, properties(r) AS p
FOREACH (_ IN CASE WHEN peer IS NULL THEN [] ELSE [1] END |
    MERGE (real)-[nr:PHYSICAL_LINK {
        interface_a: coalesce(p.interface_a, ''),
        interface_b: coalesce(p.interface_b, '')
    }]->(peer)
    SET nr += p,
        nr.original_source_adapter = coalesce(
            nr.original_source_adapter,
            p.source_adapter,
            nr.source_adapter
        ),
        nr.source_adapter   = 'correlator',
        nr.merged_from_stub = stub.id
)
WITH r
FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
    DELETE r
)
"""


async def _absorb_stub_into_real(
    session, stub_id: str, real_id: str
) -> None:
    """Redirect every PHYSICAL_LINK edge on ``stub_id`` onto ``real_id``
    and tombstone the stub so its (stub-id → canonical-id) mapping
    persists across SNMP cycles.

    Why we KEEP the stub instead of DETACH-deleting it:

    LLDP/CDP-emitted stubs are MERGE'd by id every SNMP cycle, so a
    stub we deleted is back in the graph 4 minutes later — only now
    its previously-set ``canonical_id`` is gone, and the new SNMP-
    emitted ``(peer)-[PHYSICAL_LINK]->(stub)`` edges have nowhere to
    be promoted until the next NetBox-enrich cycle runs (every
    5 min).  In that window, n9k1 (and every other device with stub
    neighbors) flashes the duplicate "real cable + stub cable" state
    the user reported.

    By leaving the stub in place with ``canonical_id`` set and
    ``tombstoned=true``:
      * The topology query already hides any node with
        ``canonical_id`` (see ``query.py``: ``canonical_map``).
      * Subsequent SNMP cycles re-MERGE the stub but ``+=`` only
        overwrites keys present in the new payload — ``canonical_id``
        and ``tombstoned`` survive.
      * Every correlator cycle's ``_absorb_stubs_with_canonical_id``
        pass finds the re-stamped stub and re-points its freshly-
        emitted edges onto the canonical, in place via the
        ``(interface_a, interface_b)`` MERGE key.  No transient
        duplicates ever escape the cycle they were created in.

    The cost is a fixed-size pool of tombstoned Device nodes (one
    per unique LLDP/CDP neighbor name we've ever seen + canonicalised).
    They never accumulate beyond that — same id ⇒ same node.
    """
    await session.run(
        _REDIRECT_INBOUND_CYPHER, stub_id=stub_id, real_id=real_id
    )
    await session.run(
        _REDIRECT_OUTBOUND_CYPHER, stub_id=stub_id, real_id=real_id
    )
    await session.run(
        """
        MATCH (stub:Device {id: $stub_id})
        SET stub.canonical_id = $real_id,
            stub.tombstoned   = true
        """,
        stub_id=stub_id,
        real_id=real_id,
    )


async def _merge_neighbor_stubs_by_chassis_mac() -> int:
    """Merge LLDP/CDP stubs onto real Devices using the chassis MAC.

    LLDP's ``lldpRemChassisId`` (subtype 4 = macAddress) and CDP's
    ``cdpCacheAddress`` (when interpreted as a chassis MAC) give us a
    deterministic key into our MAC inventory. Most enterprise vendors
    pick a port MAC as the chassis-id, so we look for an exact match
    against any ``MACAddress`` node owned by a real ``Device``.

    The previous hostname-only merge often failed because vendors name
    pairs differently in their inventory (`-A`/`-B`) vs. what they
    advertise via LLDP (`-1`/`-2`). The chassis MAC bypasses naming
    entirely.
    """
    driver = get_driver()
    merged = 0
    async with driver.session() as session:
        pairs_res = await session.run(
            """
            MATCH (stub:Device)
            WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                   OR stub.id STARTS WITH 'cdp-neighbor:')
              AND stub.chassis_mac IS NOT NULL AND stub.chassis_mac <> ''
            MATCH (real:Device)-[:OWNS_MAC]->(m:MACAddress)
            WHERE m.mac = toLower(stub.chassis_mac)
              AND (real.stub IS NULL OR real.stub = false)
              AND real.id <> stub.id
              AND NOT (real.id STARTS WITH 'lldp-neighbor:'
                       OR real.id STARTS WITH 'cdp-neighbor:')
            RETURN DISTINCT stub.id AS stub_id,
                            head(collect(real.id)) AS real_id
            """
        )
        pairs: list[tuple[str, str]] = []
        async for rec in pairs_res:
            if rec["stub_id"] and rec["real_id"]:
                pairs.append((rec["stub_id"], rec["real_id"]))

        for stub_id, real_id in pairs:
            await _absorb_stub_into_real(session, stub_id, real_id)
            merged += 1

    if merged:
        log.info("correlate.stubs_merged_by_chassis_mac", count=merged)
    return merged


async def _merge_neighbor_stubs_by_mgmt_ip() -> int:
    """Merge LLDP/CDP stubs onto real Devices using their management IP.

    LLDP's ``lldpRemManAddr`` and CDP's ``cdpCacheAddress`` give us the
    neighbor's management IP. We resolve that to a real Device using:
      1. ``IPAddress`` nodes (canonical, populated by interface walks).
      2. ``Device.candidate_ips`` (fallback for devices whose IPAddress
         nodes haven't landed yet, e.g. Intersight FIs whose mgmt IPs
         come from Meraki / NDFC inventory data).

    This pass runs AFTER chassis-MAC merge so we only consume stubs the
    deterministic chassis match couldn't claim. Real Devices that own
    the IP via OSPF / BGP / loopback addresses won't accidentally absorb
    a peer's LLDP stub because we restrict the match to addresses
    flagged with ``device_node_id`` (interface-assigned only).
    """
    driver = get_driver()
    merged = 0
    async with driver.session() as session:
        ip_pair_rows = await session.run(
            """
            MATCH (stub:Device)
            WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                   OR stub.id STARTS WITH 'cdp-neighbor:')
              AND stub.mgmt_ip IS NOT NULL AND stub.mgmt_ip <> ''
            OPTIONAL MATCH (ip:IPAddress {address: stub.mgmt_ip})
            WITH stub, ip.device_node_id AS via_ip_node
            OPTIONAL MATCH (cand:Device)
                WHERE cand.candidate_ips IS NOT NULL
                  AND stub.mgmt_ip IN cand.candidate_ips
                  AND (cand.stub IS NULL OR cand.stub = false)
                  AND cand.id <> stub.id
                  AND NOT (cand.id STARTS WITH 'lldp-neighbor:'
                           OR cand.id STARTS WITH 'cdp-neighbor:')
            WITH stub, via_ip_node, head(collect(cand.id)) AS via_cand
            RETURN stub.id AS stub_id,
                   coalesce(via_ip_node, via_cand) AS real_id
            """
        )
        pairs: list[tuple[str, str]] = []
        async for rec in ip_pair_rows:
            sid, rid = rec["stub_id"], rec["real_id"]
            if not (sid and rid) or sid == rid:
                continue
            if rid.startswith(("lldp-neighbor:", "cdp-neighbor:")):
                continue
            pairs.append((sid, rid))

        for stub_id, real_id in pairs:
            await _absorb_stub_into_real(session, stub_id, real_id)
            merged += 1

    if merged:
        log.info("correlate.stubs_merged_by_mgmt_ip", count=merged)
    return merged


async def _absorb_stubs_with_canonical_id() -> int:
    """Absorb any LLDP/CDP stub already stamped with ``canonical_id``.

    Several passes outside the dedicated stub-merge family can set
    ``canonical_id`` on a stub Device without actually re-pointing its
    PHYSICAL_LINK edges onto the canonical:

      * ``netbox_enrich`` used to match stubs by hostname against the
        NetBox device inventory and stamp ``canonical_id`` on the
        loser of each (canonical, stub) pair. (That has been fixed
        upstream — stubs are now excluded from the matcher — but
        this pass remains as the cleanup half of the fix.)
      * Any future correlator pass that wants to mark a stub for
        deletion without writing its own edge-redirection plumbing.

    When ``canonical_id`` is set on a stub, the topology query hides
    the stub AND drops every edge incident to it (the design intent
    is that the canonical already carries those edges).  But when the
    stub's edges were never redirected, they vanish from the rendered
    graph completely — which is how cpn-ful-n9k1 ended up showing
    only 3 of its 17 LLDP/CDP physical neighbors: the other 14 stubs
    had ``canonical_id`` set by netbox_enrich (pointing at the
    matching Intersight FI / NDFC leaf canonical) but no pass ever
    re-pointed their cables onto those canonicals.

    This pass closes the gap: for every stub with ``canonical_id``
    pointing at an existing real Device, redirect its inbound and
    outbound PHYSICAL_LINK edges onto the canonical and DETACH-delete
    the stub.  Same plumbing as the other stub-merge passes via
    ``_absorb_stub_into_real``.
    """
    driver = get_driver()
    merged = 0
    async with driver.session() as session:
        pairs_res = await session.run(
            """
            MATCH (stub:Device)
            WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                   OR stub.id STARTS WITH 'cdp-neighbor:')
              AND stub.canonical_id IS NOT NULL
              AND stub.canonical_id <> ''
              AND stub.canonical_id <> stub.id
            MATCH (real:Device {id: stub.canonical_id})
            WHERE NOT (real.id STARTS WITH 'lldp-neighbor:'
                       OR real.id STARTS WITH 'cdp-neighbor:')
            RETURN stub.id AS stub_id, real.id AS real_id
            """
        )
        pairs: list[tuple[str, str]] = []
        async for rec in pairs_res:
            if rec["stub_id"] and rec["real_id"]:
                pairs.append((rec["stub_id"], rec["real_id"]))

        for stub_id, real_id in pairs:
            await _absorb_stub_into_real(session, stub_id, real_id)
            merged += 1

    if merged:
        log.info("correlate.stubs_merged_by_canonical_id", count=merged)
    return merged


async def _rehome_unmerged_stubs_to_peer_site() -> int:
    """Give surviving LLDP/CDP stubs a visual home in the topology.

    A stub that can't be merged into a real Device (third-party gear,
    AI-pod compute nodes, etc.) has no ``LOCATED_AT`` parent and renders
    as a floating ghost node in the UI. Users mis-read that as "missing
    LLDP data".

    Rule: if the stub has exactly one PHYSICAL_LINK peer (or all of its
    peers share the same PlatformSite), copy that PlatformSite as the
    stub's container so it nests next to the device that detected it.
    Stubs reached from multiple distinct sites are left alone (rare —
    typically only happens during transient discovery races).
    """
    driver = get_driver()
    rehomed = 0
    async with driver.session() as session:
        res = await session.run(
            """
            MATCH (stub:Device)
            WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                   OR stub.id STARTS WITH 'cdp-neighbor:')
              AND coalesce(stub.stub, false) = true
              AND NOT EXISTS { (stub)-[:LOCATED_AT]->(:PlatformSite) }
            MATCH (stub)-[:PHYSICAL_LINK]-(peer:Device)-[:LOCATED_AT]->(ps:PlatformSite)
            WITH stub, collect(DISTINCT ps) AS sites
            WHERE size(sites) = 1
            WITH stub, sites[0] AS site
            MERGE (stub)-[:LOCATED_AT]->(site)
            RETURN count(*) AS n
            """
        )
        rec = await res.single()
        rehomed = rec["n"] if rec else 0

    if rehomed:
        log.info("correlate.stubs_rehomed_to_peer_site", count=rehomed)
    return rehomed


async def _canonicalize_vlans_per_fabric() -> int:
    """Collapse per-device / per-network VLAN nodes into one canonical
    VLAN per (site, vid).

    Background
    ----------
    Different adapters emit VLANs at different granularities:
      * SNMP per device → ``snmp-vlan:<device_id>:<vid>``
      * NDFC per fabric → ``ndfc-vlan:<fabric>:<vid>``
      * Meraki per network → ``meraki-vlan:<network_id>:<vid>``
      * CatC per site → various

    Until consolidated, the L2 overlay sees N copies of "VLAN 11" — one
    per device that reports it — and the user can't tell which links
    actually carry the same broadcast domain.

    Bucket key (preferred → fallback)
    ---------------------------------
    1. **NetBox site slug** — when at least one owning device has been
       NetBox-enriched (``netbox_site_slug`` is set), the canonical id is
       ``vlan:nb:<slug>:<vid>``.  This collapses the same broadcast
       domain across multiple PlatformSites that all map to one physical
       site (e.g. cat9k1 in a Meraki network + n9k1 in an NDFC fabric,
       both at NetBox site ``cpn-ful``).  That is the real-world case
       where one VLAN/STP domain is trunked across heterogeneous fabrics.
    2. **PlatformSite id** — when no NetBox info is available, fall back
       to ``vlan:<platform_site_id>:<vid>`` so behaviour is unchanged
       for un-enriched portions of the graph.

    The canonical VLAN tracks every contributing PlatformSite in the
    ``platform_site_ids`` list property so downstream SVI/Prefix
    scoping can still scope per-fabric when it needs to.

    Two candidate-discovery paths are tried, in order:

    1. **Device join** — for adapters that DO emit ``Device-[:LOGICAL_MEMBER]->VLAN``
       edges (SNMP, NDFC), the PlatformSite + NetBox slug come from the device.
    2. **Id-pattern parse** — for adapters that DON'T (Meraki today),
       the per-network ``L_xxx`` id is embedded in the VLAN's own id
       (``meraki-vlan:L_xxx:<vid>``); the PlatformSite is
       ``meraki-network:L_xxx`` and the NetBox slug is the most common
       slug across that PlatformSite's devices.

    Two follow-up passes complete the picture:

    * **Migration of legacy fabric-scoped canonicals** — earlier runs of
      this function (before NetBox bucketing) created
      ``vlan:<platform_site_id>:<vid>`` nodes.  Once their member devices
      get enriched, those nodes are folded into the new NetBox-scoped
      canonicals.  Their edges (HAS_SVI/HAS_PREFIX/LOGICAL_MEMBER/etc.)
      are re-pointed onto the survivor and the legacy node is deleted.
    """
    driver = get_driver()
    merged_count = 0
    async with driver.session() as session:
        # ── Path A: Device→VLAN→PlatformSite join (SNMP, NDFC).
        #    Skipped for any VLAN that has no LOGICAL_MEMBER edge yet —
        #    those are picked up by Path B (or left for housekeeping).
        #
        #    For every owning device we also pull `netbox_site_slug`.
        #    Multiple members may live in different PlatformSites (the
        #    cross-fabric extension case), so we collect all PlatformSite
        #    ids and the most-common NetBox slug here in Python.
        candidates = await (await session.run(
            """
            MATCH (v:VLAN)
            WHERE NOT v.id STARTS WITH 'vlan:'
            MATCH (d:Device)-[:LOGICAL_MEMBER]->(v)
            OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite)
            WITH v,
                 collect(DISTINCT ps.id) AS site_ids,
                 collect(DISTINCT d.netbox_site_slug) AS slug_list,
                 collect(DISTINCT d.name) AS members
            RETURN v.id   AS src_id,
                   v.vid  AS vid,
                   coalesce(v.name, v.vlan_name, 'VLAN' + toString(v.vid)) AS name,
                   v.description AS description,
                   site_ids,
                   slug_list,
                   members
            """
        )).data()

        # ── Path B: id-pattern fallback for adapters that don't emit
        #    LOGICAL_MEMBER edges. Currently covers Meraki only — every
        #    other adapter already has the Device→VLAN join above.
        #    The source id is `meraki-vlan:L_xxx:<vid>`, the PlatformSite
        #    is `meraki-network:L_xxx`.  NetBox slug comes from any
        #    device located at that PlatformSite (Meraki devices ARE
        #    NetBox-enriched by serial).
        candidates_meraki = await (await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.id STARTS WITH 'meraki-vlan:'
            WITH v, split(v.id, ':') AS parts
            WHERE size(parts) = 3
            WITH v, parts[1] AS network_id, parts[2] AS vid_str
            MATCH (ps:PlatformSite {id: 'meraki-network:' + network_id})
            OPTIONAL MATCH (d:Device)-[:LOCATED_AT]->(ps)
            WITH v, ps, vid_str,
                 collect(DISTINCT d.netbox_site_slug) AS slug_list
            RETURN v.id   AS src_id,
                   coalesce(v.vid, toInteger(vid_str)) AS vid,
                   coalesce(v.name, v.vlan_name, 'VLAN' + toString(toInteger(vid_str))) AS name,
                   v.description AS description,
                   [ps.id] AS site_ids,
                   slug_list,
                   []    AS members
            """
        )).data()

        # Merge the two streams — dedupe on src_id so a Meraki VLAN that
        # somehow has BOTH a LOGICAL_MEMBER edge AND an id-derivable site
        # gets canonicalised exactly once.
        seen_src_ids = {c["src_id"] for c in candidates}
        for row in candidates_meraki:
            if row["src_id"] not in seen_src_ids:
                candidates.append(row)
                seen_src_ids.add(row["src_id"])

        for row in candidates:
            vid = row.get("vid")
            site_ids = [s for s in (row.get("site_ids") or []) if s]
            slug_list = [s for s in (row.get("slug_list") or []) if s]
            if vid is None or not site_ids:
                continue

            # Pick the most-common non-empty NetBox slug.  When two
            # slugs tie we pick the lexicographically smallest so the
            # outcome is stable across runs.
            chosen_slug = ""
            if slug_list:
                counts: dict[str, int] = {}
                for s in slug_list:
                    counts[s] = counts.get(s, 0) + 1
                chosen_slug = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

            if chosen_slug:
                canonical_id = f"vlan:nb:{chosen_slug}:{int(vid)}"
            else:
                # No NetBox info yet: keep the legacy per-fabric scope.
                # Next correlation pass after enrichment lands will fold
                # this canonical into the NetBox-scoped one (see the
                # migration step below).
                canonical_id = f"vlan:{site_ids[0]}:{int(vid)}"

            # Two-phase rewrite (no APOC available):
            #   1. MERGE canonical node, attach to every contributing PlatformSite,
            #      record platform_site_ids[] for downstream scope queries.
            #   2. Move LOGICAL_MEMBER edges from src → canon, then DETACH DELETE src.
            # If we ever start emitting other edge types directly on the
            # raw VLAN node (HAS_SVI, HAS_PREFIX, etc.) they must be
            # added here BEFORE the DETACH DELETE.
            res = await session.run(
                """
                MATCH (src:VLAN {id: $src_id})
                MERGE (canon:VLAN {id: $canonical_id})
                ON CREATE SET canon.created_at = timestamp()
                SET canon.vid              = $vid,
                    canon.vlan_id          = $vid,
                    canon.name             = $name,
                    canon.description      = coalesce(canon.description, $description),
                    canon.platform_site_id = $primary_site_id,
                    canon.platform_site_ids = [s IN coalesce(canon.platform_site_ids, [])
                                               WHERE NOT s IN $site_ids]
                                              + $site_ids,
                    canon.netbox_site_slug = $netbox_site_slug,
                    canon.dimensions       = ['logical','virtual'],
                    canon.source_adapter   = coalesce(canon.source_adapter, 'correlator'),
                    canon.updated_at       = timestamp()
                WITH src, canon
                UNWIND $site_ids AS site_id
                  MATCH (ps:PlatformSite {id: site_id})
                  MERGE (canon)-[:LOCATED_AT]->(ps)
                // Collapse back to one (src, canon) row so the
                // OPTIONAL MATCH below doesn't try to re-bind the
                // same LOGICAL_MEMBER edge once per site_id and
                // double-delete it.
                WITH DISTINCT src, canon
                OPTIONAL MATCH (d:Device)-[m:LOGICAL_MEMBER]->(src)
                FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END |
                    MERGE (d)-[mc:LOGICAL_MEMBER]->(canon)
                    SET mc.source_adapter = coalesce(mc.source_adapter, m.source_adapter, 'correlator')
                    DELETE m
                )
                WITH DISTINCT src
                DETACH DELETE src
                RETURN 1 AS done
                """,
                src_id=row["src_id"],
                canonical_id=canonical_id,
                primary_site_id=site_ids[0],
                site_ids=site_ids,
                vid=int(vid),
                name=row.get("name"),
                description=row.get("description"),
                netbox_site_slug=chosen_slug or None,
            )
            if await res.single():
                merged_count += 1

        # ── Migration: collapse legacy un-NetBox-scoped canonicals into
        # the NetBox-scoped ones.  These are nodes whose id is
        # ``vlan:<vid>`` (the original Catalyst-Center global form) or
        # ``vlan:<platform_site_id>:<vid>`` (an earlier run of this
        # function — before NetBox bucketing — created them).  Now that
        # NetBox enrichment has caught up we can fold their edges onto
        # the matching ``vlan:nb:<slug>:<vid>`` canonical and delete the
        # legacy node.
        #
        # CRITICAL DIFFERENCE FROM THE EARLIER IMPLEMENTATION:
        # The previous version picked a single "chosen_slug" per legacy
        # node and re-pointed *every* member device to that one slug's
        # canonical.  That broke whenever a legacy VLAN had members from
        # more than one NetBox site (the Catalyst-Center global
        # ``vlan:1`` is the canonical example — it had members from
        # cpn-ful, cpn-ash, AND cpn-nashville, and the migration would
        # smush them all into ``vlan:nb:cpn-ful:1``, making it falsely
        # look like the default VLAN extends across three sites.
        #
        # The replacement migrates **per (device, legacy-VLAN) pair**:
        # each device's own ``netbox_site_slug`` chooses its destination
        # canonical.  Devices in cpn-ash → ``vlan:nb:cpn-ash:1``;
        # devices in cpn-ful → ``vlan:nb:cpn-ful:1``; etc.  Outbound
        # legacy edges (HAS_SVI / HAS_PREFIX / LOCATED_AT) are dropped
        # entirely because they're inherently un-scoped — the normal
        # ``_link_vlan_svis_and_prefixes`` pass will re-discover them
        # for each per-slug canonical.
        per_device_rows = await (await session.run(
            """
            MATCH (d:Device)-[m:LOGICAL_MEMBER]->(legacy:VLAN)
            WHERE legacy.id STARTS WITH 'vlan:'
              AND NOT legacy.id STARTS WITH 'vlan:nb:'
              AND legacy.vid IS NOT NULL
              AND d.netbox_site_slug IS NOT NULL AND d.netbox_site_slug <> ''
            RETURN d.id AS device_id,
                   d.netbox_site_slug AS slug,
                   legacy.id AS legacy_id,
                   legacy.vid AS vid,
                   coalesce(legacy.name, 'VLAN' + toString(legacy.vid)) AS name,
                   legacy.description AS description,
                   legacy.platform_site_id AS legacy_site_id,
                   coalesce(legacy.platform_site_ids, [legacy.platform_site_id]) AS legacy_site_ids,
                   m.source_adapter AS member_source
            """
        )).data()

        legacy_ids_touched: set[str] = set()
        for row in per_device_rows:
            vid = row.get("vid")
            slug = row.get("slug")
            if vid is None or not slug:
                continue
            new_id = f"vlan:nb:{slug}:{int(vid)}"
            if new_id == row["legacy_id"]:
                continue
            legacy_ids_touched.add(row["legacy_id"])
            res = await session.run(
                """
                MATCH (legacy:VLAN {id: $legacy_id})
                MATCH (d:Device   {id: $device_id})
                MERGE (canon:VLAN {id: $new_id})
                ON CREATE SET canon.created_at = timestamp()
                SET canon.vid              = $vid,
                    canon.vlan_id          = $vid,
                    canon.name             = coalesce(canon.name, $name),
                    canon.description      = coalesce(canon.description, $description),
                    canon.platform_site_ids = [s IN coalesce(canon.platform_site_ids, [])
                                               WHERE NOT s IN $legacy_site_ids]
                                              + [s IN $legacy_site_ids WHERE s IS NOT NULL],
                    canon.platform_site_id = coalesce(canon.platform_site_id, $legacy_site_id),
                    canon.netbox_site_slug = $slug,
                    canon.dimensions       = coalesce(canon.dimensions, ['logical','virtual']),
                    canon.source_adapter   = coalesce(canon.source_adapter, 'correlator'),
                    canon.updated_at       = timestamp()
                WITH d, legacy, canon
                MATCH (d)-[old:LOGICAL_MEMBER]->(legacy)
                MERGE (d)-[nm:LOGICAL_MEMBER]->(canon)
                SET nm.source_adapter = coalesce(nm.source_adapter,
                                                  old.source_adapter,
                                                  $member_source,
                                                  'correlator'),
                    nm.updated_at = timestamp()
                DELETE old
                RETURN 1 AS done
                """,
                legacy_id=row["legacy_id"],
                device_id=row["device_id"],
                new_id=new_id,
                vid=int(vid),
                name=row.get("name"),
                description=row.get("description"),
                slug=slug,
                legacy_site_id=row.get("legacy_site_id"),
                legacy_site_ids=[s for s in (row.get("legacy_site_ids") or []) if s],
                member_source=row.get("member_source"),
            )
            if await res.single():
                merged_count += 1

        # ── Reap legacy nodes that have lost (or never had) any
        # LOGICAL_MEMBER edge after the per-device migration.  We
        # DETACH DELETE them so any orphan HAS_SVI / HAS_PREFIX /
        # LOCATED_AT edges go with them — those were attached at the
        # un-scoped legacy node and would otherwise leak across NetBox
        # sites the same way the LOGICAL_MEMBER edges used to.  The
        # normal `_link_vlan_svis_and_prefixes` pass rebuilds them per
        # canonical.
        if legacy_ids_touched:
            await session.run(
                """
                UNWIND $ids AS lid
                MATCH (legacy:VLAN {id: lid})
                OPTIONAL MATCH (legacy)<-[m:LOGICAL_MEMBER]-(:Device)
                WITH legacy, count(m) AS remaining
                WHERE remaining = 0
                DETACH DELETE legacy
                """,
                ids=list(legacy_ids_touched),
            )

        # Also reap any other un-scoped legacy VLAN nodes that had no
        # member devices to begin with (orphaned by an earlier crash or
        # an adapter cycle where the device disappeared mid-poll).
        await session.run(
            """
            MATCH (legacy:VLAN)
            WHERE legacy.id STARTS WITH 'vlan:'
              AND NOT legacy.id STARTS WITH 'vlan:nb:'
            OPTIONAL MATCH (legacy)<-[m:LOGICAL_MEMBER]-(:Device)
            WITH legacy, count(m) AS members
            WHERE members = 0
            DETACH DELETE legacy
            """
        )

    if merged_count:
        log.info("correlate.vlans_canonicalised", count=merged_count)
    return merged_count


async def _link_vlan_svis_and_prefixes() -> int:
    """Link canonical VLANs to their SVI interface(s) and prefixes.

    An SVI is an Interface whose name matches ``Vlan<vid>`` /
    ``Vl<vid>`` (Cisco) or ``vlan<vid>`` (Meraki). For every SVI on a
    Device whose PlatformSite matches a canonical VLAN we add:

      * (VLAN)-[:HAS_SVI]->(Interface)
      * (VLAN)-[:HAS_PREFIX]->(Prefix)  for any Prefix whose
        ``vlan_id`` property matches this VLAN's vid. (Prefixes already
        carry ``vlan_id`` from the SD-WAN / NDFC discovery — we just
        wire them together explicitly so the UI can render the
        triangle.)

    The two MERGEs are kept in separate queries: the SVI MERGE filters
    on Device→LOCATED_AT scope, but the Prefix MERGE is allowed to be
    global because Prefix nodes don't always carry a site id (they're
    sometimes discovered from routing tables on a different device).
    """
    driver = get_driver()
    svis_linked = 0
    prefixes_linked = 0
    async with driver.session() as session:
        # ── Pre-step: delete stale HAS_PREFIX edges that violate the
        # fabric-scope invariant.  Earlier versions of this function did
        # a vid-only MERGE that linked every Meraki prefix to every
        # other fabric's "VLAN 1" — leaving rogue edges that survive
        # across runs because MERGE never deletes.  This sweep is cheap
        # (only touches existing HAS_PREFIX edges) and keeps the graph
        # consistent with the new fabric-aware MERGE below.
        # NOTE: now that canonical VLANs can span multiple PlatformSites
        # (when those sites all map to the same NetBox site), an edge is
        # considered valid if ANY of the VLAN's contributing
        # `platform_site_ids` matches the Prefix's network_id.  We fall
        # back to the singular `platform_site_id` when the list property
        # isn't populated (un-migrated nodes).
        await session.run(
            """
            MATCH (v:VLAN)-[hp:HAS_PREFIX]->(p:Prefix)
            WHERE v.id STARTS WITH 'vlan:'
              AND p.network_id IS NOT NULL
              AND NOT ANY(s IN coalesce(v.platform_site_ids, [v.platform_site_id])
                          WHERE s IS NOT NULL
                            AND (s ENDS WITH (':' + p.network_id) OR s = p.network_id))
            DELETE hp
            """
        )
        # Drop any HAS_PREFIX edge that points at a canonical VLAN
        # without any platform_site_id information (those are
        # pre-fabric-scope orphans that the housekeeping loop will reap
        # separately).
        await session.run(
            """
            MATCH (v:VLAN)-[hp:HAS_PREFIX]->(:Prefix)
            WHERE v.id STARTS WITH 'vlan:'
              AND (v.platform_site_id IS NULL OR v.platform_site_id = '')
              AND (v.platform_site_ids IS NULL OR size(v.platform_site_ids) = 0)
            DELETE hp
            """
        )

        # ── Pre-step: enforce the "no L2 extension" invariant on
        # NetBox-scoped canonicals.
        #
        # This deployment does not run any VLAN-extension technology
        # (no VXLAN-EVPN, no OTV, no L2VPN, no Q-in-Q tunneling), so a
        # `vlan:nb:<slug>:<vid>` canonical may only be a member of, or
        # have SVIs/prefixes attached to, devices and resources that
        # live in that same NetBox site.  Stale data from prior
        # versions of the correlator (which used PlatformSite as the
        # scope key) can leave edges that violate this invariant —
        # e.g. devices in `catc-site:unassigned:cpn-ful-catc1` would
        # link to every NetBox site's "default" VLAN because their
        # PlatformSite contained devices from cpn-ful, cpn-ash, AND
        # cpn-nashville.  These purges run every cycle and are cheap
        # (they touch only edges, never devices/VLANs themselves) so
        # the graph self-heals as soon as the worker reloads.
        purge_member = await session.run(
            """
            MATCH (d:Device)-[m:LOGICAL_MEMBER]->(v:VLAN)
            WHERE v.id STARTS WITH 'vlan:nb:'
              AND v.netbox_site_slug IS NOT NULL AND v.netbox_site_slug <> ''
              AND (
                d.netbox_site_slug IS NULL OR d.netbox_site_slug = ''
                OR d.netbox_site_slug <> v.netbox_site_slug
              )
            DELETE m
            RETURN count(m) AS purged
            """
        )
        rec = await purge_member.single()
        purged_members = (rec["purged"] if rec else 0)

        purge_svi = await session.run(
            """
            MATCH (v:VLAN)-[s:HAS_SVI]->(svi:Interface)
            WHERE v.id STARTS WITH 'vlan:nb:'
              AND v.netbox_site_slug IS NOT NULL AND v.netbox_site_slug <> ''
            OPTIONAL MATCH (od:Device)-[:HAS_INTERFACE]->(svi)
            WITH v, s, svi, head(collect(od)) AS owner
            WHERE owner IS NULL
               OR owner.netbox_site_slug IS NULL
               OR owner.netbox_site_slug = ''
               OR owner.netbox_site_slug <> v.netbox_site_slug
            DELETE s
            RETURN count(s) AS purged
            """
        )
        rec = await purge_svi.single()
        purged_svis = (rec["purged"] if rec else 0)

        if purged_members or purged_svis:
            log.info(
                "correlate.vlan_scope_invariant_enforced",
                purged_member_edges=purged_members,
                purged_svi_edges=purged_svis,
            )

        # SVI linkage: a device's Vlan<vid> interface contributes to
        # the canonical VLAN of the same vid IFF the device and VLAN
        # share a site.  "Share a site" means one of:
        #   * For NetBox-scoped canonicals (`vlan:nb:<slug>:<vid>`):
        #     the device's `netbox_site_slug` equals the canonical's
        #     `netbox_site_slug`.  This is the strict, correct scope —
        #     we cannot use `platform_site_ids` here because a single
        #     PlatformSite (e.g. Catalyst-Center's "unassigned" bucket
        #     for instance ``cpn-ful-catc1``) can contain devices from
        #     several NetBox sites, and using it would falsely extend
        #     every "default" VLAN across every NetBox site that
        #     instance manages.
        #   * For pre-NetBox legacy canonicals (`vlan:<ps>:<vid>`):
        #     the device must be `LOCATED_AT` one of the VLAN's
        #     contributing PlatformSites.  These are short-lived —
        #     the migration step below upgrades them to `vlan:nb:` as
        #     soon as enrichment provides a slug.
        # The same scope rule applies to the LOGICAL_MEMBER edge we
        # MERGE here so visually every L3 gateway serving the VLAN
        # appears connected, without leaking across sites.
        res = await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.id STARTS WITH 'vlan:'
            WITH v, coalesce(v.platform_site_ids, [v.platform_site_id]) AS site_ids
            MATCH (d:Device)-[:HAS_INTERFACE]->(svi:Interface)
            WHERE (
                toLower(svi.name) = 'vlan' + toString(v.vid)
                OR toLower(svi.name) = 'vl' + toString(v.vid)
                OR toLower(svi.name) = 'vlan ' + toString(v.vid)
            )
            // Site-scope guard.  For NetBox-scoped canonicals require
            // an exact slug match on the device; for legacy canonicals
            // fall back to platform-site containment.
            AND (
                (v.id STARTS WITH 'vlan:nb:'
                  AND v.netbox_site_slug IS NOT NULL
                  AND v.netbox_site_slug <> ''
                  AND d.netbox_site_slug = v.netbox_site_slug)
                OR
                (NOT v.id STARTS WITH 'vlan:nb:'
                  AND EXISTS {
                    MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite)
                    WHERE ps.id IN site_ids
                  })
            )
            MERGE (v)-[s:HAS_SVI]->(svi)
            SET s.source_adapter = 'correlator',
                s.updated_at = timestamp()
            MERGE (d)-[m:LOGICAL_MEMBER]->(v)
            SET m.source_adapter = coalesce(m.source_adapter, 'correlator'),
                m.via_svi        = true,
                m.updated_at     = timestamp()
            RETURN count(DISTINCT svi) AS svis
            """
        )
        rec = await res.single()
        svis_linked = rec["svis"] if rec else 0

        # Prefix → VLAN via Prefix.vlan_id (already populated by Meraki/NDFC adapters).
        # CRITICAL: scope by fabric, not just by vid.  Without the fabric scope, a
        # Meraki prefix with `vlan_id=1, network_id=L_xxx` would link to every
        # other fabric's "VLAN 1" too (CATC default, NDFC default, every other
        # Meraki network's VLAN 1, …), turning the "default" VLAN into a
        # collect-all for the entire deployment.
        #
        # Fabric correlation rules:
        #   * If Prefix has `network_id` (Meraki path), require it to match
        #     ANY of the VLAN's contributing platform_site_ids (canonical id
        #     format: meraki-network:L_xxx).
        #   * Otherwise (NDFC, raw SNMP, etc.), the prefix is global / shared
        #     across fabrics — link by vid alone.  This preserves the existing
        #     behaviour for fabric-internal VLANs that aren't ambiguous because
        #     their vids don't collide across separate fabrics.
        # Meraki uses `cidr`, SNMP uses `prefix`; both share the same id format
        # `prefix:<cidr>` so the MERGE is keyed off the existing node.
        res2 = await session.run(
            """
            MATCH (v:VLAN) WHERE v.id STARTS WITH 'vlan:'
            WITH v, coalesce(v.platform_site_ids, [v.platform_site_id]) AS site_ids
            MATCH (p:Prefix) WHERE p.vlan_id = v.vid
              AND (
                p.network_id IS NULL
                OR ANY(s IN site_ids
                       WHERE s IS NOT NULL
                         AND (s ENDS WITH (':' + p.network_id) OR s = p.network_id))
              )
            MERGE (v)-[hp:HAS_PREFIX]->(p)
            SET hp.source_adapter = 'correlator',
                hp.updated_at = timestamp()
            RETURN count(DISTINCT p) AS prefixes
            """
        )
        rec2 = await res2.single()
        prefixes_via_tag = rec2["prefixes"] if rec2 else 0

        # Prefix → VLAN via SVI IP.  This is the SNMP path: when an SVI
        # has an ASSIGNED_IP whose IPAddress.subnet equals an existing
        # Prefix.prefix / Prefix.cidr, the prefix demonstrably lives on
        # that VLAN even though the SNMP adapter didn't tag it with
        # vlan_id (the SVI walk doesn't expose that mapping directly).
        # We use coalesce() because Meraki Prefix nodes store the CIDR
        # in `cidr` while SNMP Prefix nodes store it in `prefix`.
        res3 = await session.run(
            """
            MATCH (v:VLAN) WHERE v.id STARTS WITH 'vlan:'
            MATCH (v)-[:HAS_SVI]->(:Interface)-[:ASSIGNED_IP]->(ip:IPAddress)
            WHERE ip.subnet IS NOT NULL AND ip.subnet <> ''
            MATCH (p:Prefix)
            WHERE coalesce(p.cidr, p.prefix) = ip.subnet
            MERGE (v)-[hp:HAS_PREFIX]->(p)
            SET hp.source_adapter = coalesce(hp.source_adapter, 'correlator'),
                hp.via_svi        = true,
                hp.updated_at     = timestamp()
            RETURN count(DISTINCT p) AS prefixes
            """
        )
        rec3 = await res3.single()
        prefixes_via_svi = rec3["prefixes"] if rec3 else 0

        # Prefix → VLAN via ROUTES_TO + SVI-style interface name.
        #
        # Some L3 gateways (notably IOS-XE catalysts polled via Meraki
        # rather than direct SNMP) don't get ASSIGNED_IP edges from
        # their SVI walk because the SNMP-derived Interface ids don't
        # collide with the Meraki-emitted ones — the ASSIGNED_IP edge
        # ends up dangling.  But the ROUTES_TO edge from the device to
        # the prefix DOES carry the interface name (e.g. ``Vl11``,
        # ``Vlan11``, ``Vlan-11``), giving us a clean (device, vid,
        # prefix) triple.
        #
        # Scope rule (identical to the SVI path above): a ROUTES_TO
        # via Vlan<vid> can only link to a canonical VLAN that lives
        # in the device's own NetBox site (or, for legacy canonicals,
        # is LOCATED_AT one of the device's PlatformSites).  Without
        # this guard, when cat9k1.ciscops.net in cpn-ash has a route
        # via Vl1, it would link to **every** VLAN-1 canonical in the
        # graph (cpn-ash, cpn-ful, cpn-nashville, …) because every
        # fabric has a vid=1 entry.  Since this deployment has no L2
        # extension technology, that's never correct.
        res4 = await session.run(
            """
            MATCH (d:Device)-[r:ROUTES_TO]->(p:Prefix)
            WHERE r.interface IS NOT NULL
              AND r.interface =~ '(?i)(vlan-|vlan|vl)[0-9]+'
            WITH d, r, p,
                 toInteger(
                   replace(replace(replace(
                     toLower(r.interface), 'vlan-', ''), 'vlan', ''), 'vl', '')
                 ) AS svi_vid
            WHERE svi_vid IS NOT NULL AND svi_vid >= 1 AND svi_vid <= 4094
            MATCH (v:VLAN)
            WHERE v.id STARTS WITH 'vlan:' AND v.vid = svi_vid
            WITH d, r, p, v,
                 coalesce(v.platform_site_ids, [v.platform_site_id]) AS site_ids
            WHERE (
                (v.id STARTS WITH 'vlan:nb:'
                  AND v.netbox_site_slug IS NOT NULL
                  AND v.netbox_site_slug <> ''
                  AND d.netbox_site_slug = v.netbox_site_slug)
                OR
                (NOT v.id STARTS WITH 'vlan:nb:'
                  AND EXISTS {
                    MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite)
                    WHERE ps.id IN site_ids
                  })
            )
            MERGE (v)-[hp:HAS_PREFIX]->(p)
            SET hp.source_adapter   = coalesce(hp.source_adapter, 'correlator'),
                hp.via_routes_to    = true,
                hp.routes_to_iface  = r.interface,
                hp.updated_at       = timestamp()
            MERGE (d)-[m:LOGICAL_MEMBER]->(v)
            SET m.source_adapter   = coalesce(m.source_adapter, 'correlator'),
                m.via_routes_to    = true,
                m.routes_to_iface  = r.interface,
                m.updated_at       = timestamp()
            RETURN count(DISTINCT p) AS prefixes
            """
        )
        rec4 = await res4.single()
        prefixes_via_routes_to = rec4["prefixes"] if rec4 else 0

        # Prefix → VLAN via NetBox IPAM tags.
        #
        # NetBox is the source of truth for "this prefix belongs to that
        # VLAN at this site" — discovery data sometimes misses this
        # linkage entirely (e.g. a catalyst whose IPv4 SVI lives on a
        # subnet also reachable via its OOB mgmt port: the route table
        # reports the v4 prefix as routed via Gi0/0, not Vl<n>, so our
        # ROUTES_TO-based path silently loses the VLAN association).
        # The ``enrich_prefixes_from_netbox_ipam`` pass in the worker
        # stamps every Prefix node with ``netbox_vlan_vid`` (and, when
        # NetBox has it, ``netbox_site_slug``) so we can join here.
        #
        # We only link to NetBox-scoped canonical VLANs (``vlan:nb:...``)
        # because the slug-based join only makes sense when the canonical
        # is itself NetBox-scoped.  Per-fabric canonical VLANs without
        # NetBox grouping use the existing tag/SVI/ROUTES_TO paths.
        #
        # Two flavours of join:
        #   * Exact slug match — when the NetBox prefix has its own site
        #     assignment, both vid AND slug must equal the canonical's.
        #   * Site-less fallback — when the NetBox prefix has no site
        #     (the common case in this deployment), we look at which
        #     enriched Device ROUTES_TO the prefix and use that device's
        #     NetBox slug to disambiguate.  Without this fallback
        #     site-less NetBox prefixes would never link, because the
        #     canonical VLAN always carries a slug.
        res5 = await session.run(
            """
            MATCH (v:VLAN) WHERE v.id STARTS WITH 'vlan:nb:'
            MATCH (p:Prefix)
            WHERE p.netbox_vlan_vid = v.vid
              AND p.netbox_site_slug IS NOT NULL
              AND p.netbox_site_slug <> ''
              AND p.netbox_site_slug = v.netbox_site_slug
            MERGE (v)-[hp:HAS_PREFIX]->(p)
            SET hp.source_adapter   = coalesce(hp.source_adapter, 'netbox-ipam'),
                hp.via_netbox_ipam  = true,
                hp.updated_at       = timestamp()
            RETURN count(DISTINCT p) AS prefixes
            """
        )
        rec5 = await res5.single()
        prefixes_via_netbox_exact = rec5["prefixes"] if rec5 else 0

        res5b = await session.run(
            """
            MATCH (p:Prefix)
            WHERE p.netbox_vlan_vid IS NOT NULL
              AND (p.netbox_site_slug IS NULL OR p.netbox_site_slug = '')
            MATCH (d:Device)-[:ROUTES_TO]->(p)
            WHERE d.netbox_site_slug IS NOT NULL AND d.netbox_site_slug <> ''
            MATCH (v:VLAN {
                id: 'vlan:nb:' + d.netbox_site_slug + ':' + toString(p.netbox_vlan_vid)
            })
            MERGE (v)-[hp:HAS_PREFIX]->(p)
            SET hp.source_adapter        = coalesce(hp.source_adapter, 'netbox-ipam'),
                hp.via_netbox_ipam       = true,
                hp.netbox_ipam_via_device = d.name,
                hp.updated_at            = timestamp()
            MERGE (d)-[m:LOGICAL_MEMBER]->(v)
            SET m.source_adapter        = coalesce(m.source_adapter, 'netbox-ipam'),
                m.via_netbox_ipam       = true,
                m.netbox_ipam_via_device = d.name,
                m.updated_at            = timestamp()
            RETURN count(DISTINCT p) AS prefixes
            """
        )
        rec5b = await res5b.single()
        prefixes_via_netbox_route = rec5b["prefixes"] if rec5b else 0

        # ── Final LOGICAL_MEMBER backfill ────────────────────────────────
        #
        # Any device that ROUTES_TO a Prefix already linked to a
        # NetBox-scoped VLAN, AND whose own NetBox slug matches that
        # VLAN's slug, is by definition a member of that VLAN —
        # even when the route happens to leave via a non-SVI port
        # (e.g. cat9k1's IPv4 path to 192.133.162.0/24 goes through
        # the OOB Gi0/0 mgmt port, not the Vl11 SVI).  Without this
        # backfill those gateways stay visually disconnected from
        # the VLAN even though the prefix-to-VLAN link is correct.
        #
        # The unique-pair count is captured for diagnostics only.
        await session.run(
            """
            MATCH (v:VLAN)-[:HAS_PREFIX]->(p:Prefix)
            WHERE v.id STARTS WITH 'vlan:nb:'
              AND v.netbox_site_slug IS NOT NULL
              AND v.netbox_site_slug <> ''
            MATCH (d:Device)-[:ROUTES_TO]->(p)
            WHERE d.netbox_site_slug = v.netbox_site_slug
            MERGE (d)-[m:LOGICAL_MEMBER]->(v)
            SET m.source_adapter   = coalesce(m.source_adapter, 'correlator'),
                m.via_prefix_route = true,
                m.updated_at       = timestamp()
            """
        )

        prefixes_via_netbox = prefixes_via_netbox_exact + prefixes_via_netbox_route

        prefixes_linked = (
            prefixes_via_tag
            + prefixes_via_svi
            + prefixes_via_routes_to
            + prefixes_via_netbox
        )

    if svis_linked or prefixes_linked:
        log.info("correlate.vlan_svis_linked",
                 svis=svis_linked,
                 prefixes_via_tag=prefixes_via_tag,
                 prefixes_via_svi=prefixes_via_svi,
                 prefixes_via_routes_to=prefixes_via_routes_to,
                 prefixes_via_netbox=prefixes_via_netbox,
                 prefixes_total=prefixes_linked)
    return svis_linked + prefixes_linked


async def _decorate_vlan_labels_with_prefixes() -> int:
    """Fold associated Prefix nodes into the canonical VLAN's own properties.

    Once a VLAN has one or more ``HAS_PREFIX`` edges (set by
    :func:`_link_vlan_svis_and_prefixes`) the Prefix is conceptually
    "part of" the VLAN — the same broadcast domain has both an L2 tag
    and an L3 subnet.  Rather than keep them as two separate nodes that
    the layout engine has to position independently, we stamp the
    prefix CIDR(s) as properties on the VLAN itself:

      * ``prefix_v4``  — IPv4 CIDR (or list, if multiple)
      * ``prefix_v6``  — IPv6 CIDR (or list, if multiple)

    The UI uses these to compose a multi-line label::

        VLAN 11 · Infrastructure
        192.168.11.0/24
        2620:41:1:b::/64

    Detecting v4 vs v6 is done by looking for ``:`` in the CIDR — fast,
    and ambiguous CIDRs (rare) are simply stored under v4 so they never
    silently disappear.  Storing as a Neo4j list keeps things flexible
    for the (uncommon) multi-prefix case (e.g. a VLAN with both a
    primary and an HSRP/VRRP secondary subnet).
    """
    driver = get_driver()
    decorated = 0
    async with driver.session() as session:
        # Pull every (vlan, [prefixes]) tuple in one round-trip so we
        # don't issue O(vlan_count) queries.  CIDR ranking by string is
        # deterministic and the UI doesn't care about ordering anyway.
        rows = await (await session.run(
            """
            MATCH (v:VLAN)-[:HAS_PREFIX]->(p:Prefix)
            WITH v, collect(DISTINCT coalesce(p.cidr, p.prefix)) AS cidrs
            RETURN v.id AS vlan_id, cidrs
            """
        )).data()

        for row in rows:
            cidrs = [c for c in (row.get("cidrs") or []) if c]
            if not cidrs:
                continue
            v4 = sorted({c for c in cidrs if ":" not in c})
            v6 = sorted({c for c in cidrs if ":" in c})

            res = await session.run(
                """
                MATCH (v:VLAN {id: $vid})
                SET v.prefix_v4   = $v4,
                    v.prefix_v6   = $v6,
                    v.has_prefix  = true,
                    v.updated_at  = timestamp()
                RETURN 1 AS ok
                """,
                vid=row["vlan_id"],
                v4=v4,
                v6=v6,
            )
            if await res.single():
                decorated += 1

        # Clear stamps on VLANs that USED to have a prefix but no longer
        # do (e.g. SVI removed).  Keeps the UI from showing stale CIDRs.
        await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.has_prefix = true
              AND NOT (v)-[:HAS_PREFIX]->(:Prefix)
            SET v.prefix_v4 = null,
                v.prefix_v6 = null,
                v.has_prefix = false,
                v.updated_at = timestamp()
            """
        )

    if decorated:
        log.info("correlate.vlan_labels_decorated", count=decorated)
    return decorated


async def _populate_interface_canonical_names() -> int:
    """Stamp every Interface with a ``name_canonical`` property.

    Different adapters and protocols spell the same physical port
    differently — ``Te1/1/5`` (CDP / IOS-XE short form), ``Twe1/1/5``
    (LLDP short form), and ``TwentyFiveGigE1/1/5`` (long form) all
    reference the same NIC. The PHYSICAL_LINK edge already stores its
    interface_a/b in the canonical (long) form, so for Phase 3
    decoration queries to JOIN against Interface nodes we need the same
    canonical form on the Interface side.

    We skip rows that already have an up-to-date canonical (cheap idle
    runs) and we only rewrite the new field — ``name`` stays whatever
    the adapter reported, so the UI's "Inventory" tab shows the
    platform-native value.
    """
    from netcortex.util.ifname import normalize_ifname

    driver = get_driver()
    updated = 0
    async with driver.session() as session:
        rows = await (await session.run(
            """
            MATCH (i:Interface)
            WHERE i.name IS NOT NULL
              AND (i.name_canonical IS NULL OR i.name_canonical = '')
            RETURN elementId(i) AS rid, i.name AS name
            LIMIT 10000
            """
        )).data()

        for row in rows:
            canon = normalize_ifname(row["name"])
            if not canon:
                continue
            await session.run(
                "MATCH (i) WHERE elementId(i) = $rid SET i.name_canonical = $c",
                rid=row["rid"], c=canon,
            )
            updated += 1

    if updated:
        log.info("correlate.iface_canonical_names_set", count=updated)
    return updated


async def _decorate_physical_links_l2() -> int:
    """Stamp every PHYSICAL_LINK with the L2 properties of its endpoints.

    For each (a)-[r:PHYSICAL_LINK]->(b), look up the Interface nodes
    matching r.interface_a on `a` and r.interface_b on `b` and copy:

      * trunk_mode_a / trunk_mode_b  ("trunk" / "access" / NULL)
      * vlans_access_a / vlans_access_b  (int VID for access ports)
      * native_vlan_a / native_vlan_b   (int VID for trunks)
      * vlans_carried_a / vlans_carried_b — per-side carried VLAN set
      * vlans_carried — intersection of the two sides' sets (the
                        conservative "what's truly bridged" view)
      * interface_a_active / interface_b_active — if the link is
        anchored to a multi-rate speed-variant shadow and the active
        sibling was resolved instead, this records the active port's
        name so the UI can render both
      * l2_consistent — bool: do both ends agree on trunk_mode / native?

    Sibling-aware resolution (dev17)
    --------------------------------
    Cisco multi-rate ports (e.g. 1G/10G or 10G/25G SFP cages) expose
    two IF-MIB rows for one physical port — one for each speed family.
    Only one is the active variant at a time; the other is an inactive
    shadow whose SNMP vmVlan default-reads as ``access vlan 1``. LLDP
    and CDP can advertise either name in the connected-port TLV, so
    the resulting PHYSICAL_LINK can be anchored to the shadow. Without
    sibling resolution the link's L2 decoration would silently reflect
    the dead shadow ("access vlan 1") instead of the live trunk.

    For each side we score every Interface that either name-matches
    the link anchor OR shares the anchor's numeric port-tail
    (``1/1/5`` matches both ``TenGigabitEthernet1/1/5`` and
    ``TwentyFiveGigE1/1/5``). The highest-ranked candidate wins, and
    when it differs from the anchor we record the active variant on
    ``interface_a_active`` / ``interface_b_active`` so the UI can
    surface the discrepancy.

    Pre-conditions
    --------------
    Phase 1 (_poll_port_vlans) must have populated trunk_mode /
    vlans_access / vlans_allowed / native_vlan on the Interface nodes.
    Links where neither end has any L2 facts are stamped with NULLs
    (preserving the old behaviour of skipping silently).
    """
    driver = get_driver()
    async with driver.session() as session:
        # ── Step 1: build a per-device inventory of L2-bearing interfaces.
        #
        # One round-trip pulls every Interface on every Device that has
        # any L2 state (trunk_mode OR vlans_access set). The volume is
        # bounded (tens of thousands of rows at the upper end) and the
        # subsequent sibling lookup is then pure-Python set/dict work.
        res = await session.run(
            """
            MATCH (d)-[:HAS_INTERFACE]->(i:Interface)
            WHERE i.trunk_mode IS NOT NULL OR i.vlans_access IS NOT NULL
            RETURN d.id AS device_id,
                   coalesce(i.name_canonical, i.name) AS name,
                   i.trunk_mode AS trunk_mode,
                   i.vlans_allowed AS vlans_allowed,
                   i.vlans_access AS vlans_access,
                   i.native_vlan AS native_vlan
            """
        )
        ifaces_by_device: dict[str, list[dict]] = {}
        async for row in res:
            ifaces_by_device.setdefault(row["device_id"], []).append(
                {
                    "name": row["name"],
                    "trunk_mode": row["trunk_mode"],
                    "vlans_allowed": row["vlans_allowed"],
                    "vlans_access": row["vlans_access"],
                    "native_vlan": row["native_vlan"],
                }
            )

        # ── Step 2: walk every PHYSICAL_LINK and resolve the best
        # L2-bearing interface for each side using the sibling-aware
        # ranker.
        res = await session.run(
            """
            MATCH (a)-[r:PHYSICAL_LINK]->(b)
            WHERE r.interface_a IS NOT NULL AND r.interface_b IS NOT NULL
            RETURN elementId(r) AS rid,
                   a.id AS a_id, b.id AS b_id,
                   r.interface_a AS ia_name, r.interface_b AS ib_name
            """
        )
        updates: list[dict] = []
        async for row in res:
            a_inv = ifaces_by_device.get(row["a_id"], [])
            b_inv = ifaces_by_device.get(row["b_id"], [])
            ia = _resolve_active_iface(row["ia_name"], a_inv)
            ib = _resolve_active_iface(row["ib_name"], b_inv)
            if not ia and not ib:
                # No L2 facts on either side — skip but still null out
                # any stale fields the link might be carrying from a
                # previous decoration cycle so we don't lie to the UI.
                updates.append(
                    {
                        "rid": row["rid"],
                        "trunk_mode_a": None, "trunk_mode_b": None,
                        "vlans_access_a": None, "vlans_access_b": None,
                        "native_vlan_a": None, "native_vlan_b": None,
                        "vlans_carried": None,
                        "vlans_carried_a": None, "vlans_carried_b": None,
                        "interface_a_active": None,
                        "interface_b_active": None,
                        "l2_consistent": None,
                    }
                )
                continue

            a_set = _vlans_set_for(ia)
            b_set = _vlans_set_for(ib)
            if a_set is not None and b_set is not None:
                a_lookup = set(a_set)
                carried = [v for v in a_set if v in a_lookup and v in b_set]
                # dedupe while preserving order
                seen = set()
                carried = [v for v in carried if not (v in seen or seen.add(v))]
            elif a_set is not None:
                carried = list(a_set)
            elif b_set is not None:
                carried = list(b_set)
            else:
                carried = None

            # Only stamp interface_*_active when the resolved sibling
            # differs from the link's anchor name (case-insensitive).
            anchor_a = (row["ia_name"] or "").lower()
            anchor_b = (row["ib_name"] or "").lower()
            ia_active = (
                ia["name"] if ia and (ia["name"] or "").lower() != anchor_a else None
            )
            ib_active = (
                ib["name"] if ib and (ib["name"] or "").lower() != anchor_b else None
            )

            # l2_consistent: only meaningful when BOTH sides have a
            # trunk_mode. The hot-path check in the UI is smart enough
            # to recognise that access(N) ↔ trunk(native=N) is
            # operationally fine, so we keep the underlying property
            # as a strict equality and let the UI interpret it.
            tma = ia["trunk_mode"] if ia else None
            tmb = ib["trunk_mode"] if ib else None
            nva = ia["native_vlan"] if ia else None
            nvb = ib["native_vlan"] if ib else None
            if tma is None or tmb is None:
                l2_consistent = None
            else:
                l2_consistent = (tma == tmb) and (
                    (nva if nva is not None else -1) == (nvb if nvb is not None else -1)
                )

            updates.append(
                {
                    "rid": row["rid"],
                    "trunk_mode_a": tma, "trunk_mode_b": tmb,
                    "vlans_access_a": ia["vlans_access"] if ia else None,
                    "vlans_access_b": ib["vlans_access"] if ib else None,
                    "native_vlan_a": nva, "native_vlan_b": nvb,
                    "vlans_carried": carried,
                    "vlans_carried_a": a_set,
                    "vlans_carried_b": b_set,
                    "interface_a_active": ia_active,
                    "interface_b_active": ib_active,
                    "l2_consistent": l2_consistent,
                }
            )

        if not updates:
            return 0

        # ── Step 3: write the updates back in one batch.
        await session.run(
            """
            UNWIND $rows AS row
            MATCH ()-[r:PHYSICAL_LINK]->()
            WHERE elementId(r) = row.rid
            SET r.trunk_mode_a       = row.trunk_mode_a,
                r.trunk_mode_b       = row.trunk_mode_b,
                r.vlans_access_a     = row.vlans_access_a,
                r.vlans_access_b     = row.vlans_access_b,
                r.native_vlan_a      = row.native_vlan_a,
                r.native_vlan_b      = row.native_vlan_b,
                r.vlans_carried      = row.vlans_carried,
                r.vlans_carried_a    = row.vlans_carried_a,
                r.vlans_carried_b    = row.vlans_carried_b,
                r.interface_a_active = row.interface_a_active,
                r.interface_b_active = row.interface_b_active,
                r.l2_consistent      = row.l2_consistent,
                r.l2_updated_at      = timestamp()
            """,
            rows=updates,
        )
        updated = sum(
            1
            for u in updates
            if u["trunk_mode_a"] is not None or u["trunk_mode_b"] is not None
        )

    if updated:
        log.info("correlate.links_decorated_l2", count=updated)
    return updated


async def _decorate_physical_links_stp() -> int:
    """Stamp PHYSICAL_LINK edges with the STP state seen on each end.

    For each link we walk:
      (a)-[:HAS_INTERFACE]->(ia)-[:STP_LINK]->(stp_a)
      (b)-[:HAS_INTERFACE]->(ib)-[:STP_LINK]->(stp_b)

    and copy:
      * stp_state_a / stp_state_b  (forwarding/blocking/learning/...)
      * stp_role_a  / stp_role_b   (root/designated/alternate/...)
      * stp_root                   (root bridge id if both ends agree)
      * stp_vlan                   (vid if STPDomain is per-VLAN)
      * stp_instance               (STP instance id for traceability)

    When neither end has STP data we leave the fields unset. When the
    two ends disagree (different root bridges) we still write both
    values — the UI flags that as a topology bug.
    """
    driver = get_driver()
    updated = 0
    async with driver.session() as session:
        res = await session.run(
            """
            MATCH (a)-[r:PHYSICAL_LINK]->(b)
            WHERE r.interface_a IS NOT NULL AND r.interface_b IS NOT NULL
            OPTIONAL MATCH (a)-[:HAS_INTERFACE]->(ia:Interface)
                -[stp_a:STP_LINK]->(sa:STPDomain)
                WHERE toLower(coalesce(ia.name_canonical, ia.name))
                    = toLower(r.interface_a)
            OPTIONAL MATCH (b)-[:HAS_INTERFACE]->(ib:Interface)
                -[stp_b:STP_LINK]->(sb:STPDomain)
                WHERE toLower(coalesce(ib.name_canonical, ib.name))
                    = toLower(r.interface_b)
            WITH r, stp_a, stp_b, sa, sb
            WHERE stp_a IS NOT NULL OR stp_b IS NOT NULL
            SET r.stp_state_a  = stp_a.port_state,
                r.stp_state_b  = stp_b.port_state,
                r.stp_role_a   = stp_a.port_role,
                r.stp_role_b   = stp_b.port_role,
                r.stp_root     = coalesce(sa.root_bridge, sb.root_bridge),
                r.stp_vlan     = coalesce(sa.vlan_id, sb.vlan_id),
                r.stp_instance = coalesce(sa.id, sb.id),
                r.stp_updated_at = timestamp()
            RETURN count(DISTINCT r) AS n
            """
        )
        rec = await res.single()
        updated = rec["n"] if rec else 0

    if updated:
        log.info("correlate.links_decorated_stp", count=updated)
    return updated


async def _merge_redundant_stp_domains() -> int:
    """Collapse SNMP-discovered STPDomains into Meraki-discovered ones.

    Meraki creates one ``STPDomain`` per Meraki *network*
    (``stp:meraki/...`` id, has a human-readable ``name`` like
    ``STP cpn-ful``).  SNMP independently creates one ``STPDomain``
    per *IEEE bridge id* observed on a polled switch
    (``stp-domain:<root-mac>`` id, no ``name`` because the bridge
    protocol carries no domain label).  When a single physical
    spanning tree is visible from both sources — e.g. a Nexus polled
    by SNMP reports ``ccc01-sw1``'s bridge id as its root, while
    Meraki's cloud reports that same ``ccc01-sw1`` as the root of
    ``STP cpn-ful`` — we end up with two STPDomain nodes for one
    real tree.  The unnamed one then projects ``stp_domain_name=NULL``
    onto every device that only joined via SNMP, which leaks into
    the UI badge as a blank domain.

    For every pair where:

      * a named ``stp:*`` domain D1 has a Device R as its
        ``STP_ROOT`` member, and
      * an unnamed ``stp-domain:*`` domain D2 also has R as its
        ``STP_ROOT`` member,

    we redirect every membership edge from D2 onto D1 (MERGEing to
    avoid duplicates) and ``DETACH DELETE`` D2.  Stamps a
    ``merged_from`` list on the survivor so the provenance is
    queryable.
    """
    driver = get_driver()
    async with driver.session() as session:
        # Phase 1: redirect every membership edge from each
        # unnamed-but-named-paired STP domain onto the named
        # survivor, then delete the unnamed domain.
        result = await session.run(
            """
            // Find named/unnamed domain pairs sharing the same root.
            MATCH (named:STPDomain)<-[:STP_ROOT]-(root:Device)-[:STP_ROOT]->(unnamed:STPDomain)
            WHERE named.name IS NOT NULL AND named.name <> ''
              AND (unnamed.name IS NULL OR unnamed.name = '')
              AND named.id <> unnamed.id
            // Pick one named survivor per unnamed domain (a Meraki
            // tree might be claimed by SNMP from multiple polled
            // members — we still only want a single survivor).
            WITH unnamed, head(collect(DISTINCT named)) AS survivor
            // Redirect every membership edge from the unnamed
            // domain onto the survivor.  MERGE keeps the
            // STP_ROOT/STP_MEMBER distinction intact; ON CREATE
            // copies stp_priority from the old edge so the
            // membership context isn't lost.
            MATCH (d:Device)-[old:STP_ROOT|STP_MEMBER]->(unnamed)
            WITH unnamed, survivor, d, old, type(old) AS old_type,
                 old.stp_priority AS old_prio
            CALL (d, survivor, old_type, old_prio) {
              FOREACH (_ IN CASE WHEN old_type = 'STP_ROOT' THEN [1] ELSE [] END |
                MERGE (d)-[mr:STP_ROOT]->(survivor)
                ON CREATE SET mr.stp_priority = old_prio,
                              mr.merged_from_snmp = true
              )
              FOREACH (_ IN CASE WHEN old_type = 'STP_MEMBER' THEN [1] ELSE [] END |
                MERGE (d)-[mm:STP_MEMBER]->(survivor)
                ON CREATE SET mm.stp_priority = old_prio,
                              mm.merged_from_snmp = true
              )
            }
            // Track provenance on the survivor and delete the
            // redundant domain (DETACH DELETE also removes any
            // remaining old edges).
            WITH DISTINCT unnamed, survivor
            SET survivor.merged_from = coalesce(survivor.merged_from, []) + [unnamed.id]
            DETACH DELETE unnamed
            RETURN count(*) AS n
            """
        )
        rec = await result.single()
        merged = rec["n"] if rec else 0

        # Phase 2: STP_ROOT trumps STP_MEMBER for the same
        # (device, domain) pair.  The SNMP adapter sometimes emits
        # both -- a root bridge is technically also a participating
        # member of its own tree -- and our merge faithfully copied
        # the duplicates.  Drop the STP_MEMBER side so the
        # downstream projection (and the topology view) sees one
        # unambiguous role per (device, domain) edge.
        result = await session.run(
            """
            MATCH (d:Device)-[member:STP_MEMBER]->(dom:STPDomain)
            WHERE EXISTS { MATCH (d)-[:STP_ROOT]->(dom) }
            DELETE member
            RETURN count(member) AS n
            """
        )
        rec = await result.single()
        deduped = rec["n"] if rec else 0

    if merged or deduped:
        log.info("correlate.stp_domains_merged",
                 merged=merged, role_dedup=deduped)
    return merged + deduped


async def _decorate_devices_with_stp_membership() -> int:
    """Project STP role/priority/domain onto each Device node.

    Reads the STP_ROOT / STP_MEMBER edges already produced by the
    Meraki and SNMP adapters and copies the relevant facts directly
    onto the participating Device, so the topology renderer can
    color / badge / dim devices by STP membership without traversing
    a separate STPDomain node.

    Fields stamped on the Device (all set to NULL when no membership
    edge exists, so devices that were briefly in an STP domain and
    later removed lose their decoration cleanly):

      * stp_is_root           bool   — true if device owns STP_ROOT edge
      * stp_priority          int    — bridge priority (lower = root)
      * stp_domain_id         str    — STPDomain.id this device is in
      * stp_domain_name       str    — STPDomain.name (e.g. "STP <site>")
      * stp_root_bridge_id    str    — Device.id of the root in this domain
      * stp_root_bridge_name  str    — Device.name of the root in this domain

    A device that belongs to multiple STP domains (rare — happens for
    a switch peered into two unrelated Meraki networks) keeps the
    lowest-priority (root-most) domain.
    """
    driver = get_driver()
    async with driver.session() as session:
        # Phase 1: clear stale STP membership on devices that no longer
        # have any STP edge.  Without this, dropping a switch out of an
        # STP domain would leave a stale badge floating.
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.stp_domain_id IS NOT NULL
              AND NOT (d)-[:STP_ROOT|STP_MEMBER]->(:STPDomain)
            SET d.stp_is_root = NULL,
                d.stp_priority = NULL,
                d.stp_domain_id = NULL,
                d.stp_domain_name = NULL,
                d.stp_root_bridge_id = NULL,
                d.stp_root_bridge_name = NULL
            RETURN count(d) AS n
            """
        )
        rec = await result.single()
        cleared = rec["n"] if rec else 0
        if cleared:
            log.info("correlate.stp_devices_cleared", count=cleared)

        # Phase 2: for each Device that DOES have an STP edge, write
        # the per-domain facts.  Pick the lowest-priority (root-most)
        # domain when the device belongs to several.  We compute the
        # root bridge name per domain in the same query so the badge
        # text doesn't need a second hop.
        result = await session.run(
            """
            // root bridge per domain (lowest stp_priority wins; on
            // ties prefer the explicit STP_ROOT-tagged device, then
            // any deterministic id).
            MATCH (root_d:Device)-[r:STP_ROOT|STP_MEMBER]->(dom:STPDomain)
            WITH dom, root_d, r,
                 CASE WHEN type(r) = 'STP_ROOT' THEN 0 ELSE 1 END AS root_rank,
                 coalesce(r.stp_priority, 32768) AS prio
            ORDER BY dom.id, root_rank, prio, root_d.id
            WITH dom, head(collect({d: root_d, prio: prio})) AS root_pick
            WITH dom, root_pick.d AS root_dev, root_pick.prio AS root_prio
            // for each membership, write the chosen root's facts to
            // the member device.  We pick the lowest-priority domain
            // per device when there are multiples.
            MATCH (d:Device)-[r:STP_ROOT|STP_MEMBER]->(dom)
            WITH d, dom, root_dev, root_prio, r,
                 coalesce(r.stp_priority, 32768) AS my_prio,
                 CASE WHEN type(r) = 'STP_ROOT' THEN true ELSE false END AS is_root
            // When a switch belongs to multiple STP domains (e.g.
            // peered into two separate Meraki networks, or
            // simultaneously visible via SNMP + Meraki), pick:
            //   1. the domain where THIS device is the root
            //      (`is_root DESC`), then
            //   2. the lowest STP priority (`my_prio`), then
            //   3. a domain that has a human-readable name —
            //      the SNMP-discovered STPDomain has no `name`
            //      property, so without this we'd silently pick
            //      the unnamed one over `STP cpn-ful` on every
            //      tie and leave the badge blank, then
            //   4. deterministic id as a final stable tiebreak.
            ORDER BY d.id, is_root DESC, my_prio,
                     CASE WHEN dom.name IS NULL OR dom.name = ''
                          THEN 1 ELSE 0 END,
                     dom.id
            WITH d, head(collect({
                dom_id: dom.id, dom_name: dom.name,
                root_id: root_dev.id, root_name: root_dev.name,
                prio: my_prio, is_root: is_root
            })) AS pick
            SET d.stp_is_root = pick.is_root,
                d.stp_priority = pick.prio,
                d.stp_domain_id = pick.dom_id,
                d.stp_domain_name = pick.dom_name,
                d.stp_root_bridge_id = pick.root_id,
                d.stp_root_bridge_name = pick.root_name
            RETURN count(d) AS n
            """
        )
        rec = await result.single()
        updated = rec["n"] if rec else 0

    if updated:
        log.info("correlate.stp_devices_decorated", count=updated, cleared=cleared)
    return updated


async def _stamp_stp_link_topology() -> int:
    """Stamp STP-relative facts onto cables and devices.

    Two derived projections are written here so the topology view
    can render the right thing without joining ``STP*`` edges at
    request time:

    1. ``PHYSICAL_LINK.stp_inter_domain`` (bool)
       Set to ``true`` when both endpoints carry an
       ``stp_domain_id`` but the IDs differ.  These cables are
       L2 trunks that cross STP scope boundaries (e.g. a Meraki
       MS390 in one Meraki "network" trunked to an MS390 in
       another Meraki "network" at the same physical site) —
       they are NOT part of either spanning tree's active path
       and should be rendered distinctly from intra-domain
       backbone cables.  Cleared (set to NULL) when the endpoints
       share a domain or one lacks STP context.

    2. ``Device.stp_peers_in_domain`` (int)
       The count of distinct PHYSICAL_LINK peers that share this
       device's ``stp_domain_id``.  Zero means: "Meraki/SNMP says
       I'm in this STP tree but I can't see any other member over
       cable" — typical for phantom roots Meraki's cloud
       remembers from decommissioned hardware, or for STP members
       whose physical uplink discovery has gaps.  The UI uses
       this to dash-border such devices and call out the
       membership as unverified.
    """
    driver = get_driver()
    async with driver.session() as session:
        # ── (1a) clear stale stp_inter_domain on cables that are no
        # longer inter-domain (endpoints changed or one side dropped
        # out of all STP domains).
        result = await session.run(
            """
            MATCH (a:Device)-[r:PHYSICAL_LINK]->(b:Device)
            WHERE r.stp_inter_domain = true
              AND (a.stp_domain_id IS NULL
                   OR b.stp_domain_id IS NULL
                   OR a.stp_domain_id = b.stp_domain_id)
            SET r.stp_inter_domain = NULL
            RETURN count(r) AS n
            """
        )
        rec = await result.single()
        inter_cleared = rec["n"] if rec else 0

        # ── (1b) flag inter-domain cables.
        result = await session.run(
            """
            MATCH (a:Device)-[r:PHYSICAL_LINK]->(b:Device)
            WHERE a.stp_domain_id IS NOT NULL
              AND b.stp_domain_id IS NOT NULL
              AND a.stp_domain_id <> b.stp_domain_id
            SET r.stp_inter_domain = true
            RETURN count(r) AS n
            """
        )
        rec = await result.single()
        inter_flagged = rec["n"] if rec else 0

        # ── (2a) clear the peer/size counts on devices that left STP.
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE (d.stp_peers_in_domain IS NOT NULL
                   OR d.stp_domain_size IS NOT NULL)
              AND d.stp_domain_id IS NULL
            SET d.stp_peers_in_domain = NULL,
                d.stp_domain_size = NULL
            RETURN count(d) AS n
            """
        )
        rec = await result.single()
        peers_cleared = rec["n"] if rec else 0

        # ── (2b) for every Device with an stp_domain_id, count
        # both:
        #   * stp_domain_size  — how many devices total claim
        #     membership in this STP domain (the device counts
        #     itself).  A "single-switch domain" has size 1 — the
        #     root is alone and zero peers is expected, NOT an
        #     anomaly.
        #   * stp_peers_in_domain — how many of those peers we
        #     can actually reach over a PHYSICAL_LINK.  Bidirectional
        #     match (Neo4j stores direction but PHYSICAL_LINK is
        #     conceptually undirected).
        #
        # The UI flags a device as "orphan / unverified" only when
        # stp_domain_size > 1 AND stp_peers_in_domain = 0 — i.e.
        # other members exist on paper but we can't see any cable
        # to them.  This avoids over-flagging the 50+ legitimate
        # single-switch sites in this fleet.
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.stp_domain_id IS NOT NULL
            OPTIONAL MATCH (d)-[:PHYSICAL_LINK]-(peer:Device)
            WHERE peer.stp_domain_id = d.stp_domain_id
              AND peer <> d
            WITH d, count(DISTINCT peer) AS peer_count
            // Count how many devices claim this domain (including d).
            OPTIONAL MATCH (other:Device)
            WHERE other.stp_domain_id = d.stp_domain_id
            WITH d, peer_count, count(DISTINCT other) AS domain_size
            SET d.stp_peers_in_domain = peer_count,
                d.stp_domain_size = domain_size
            RETURN count(d) AS n
            """
        )
        rec = await result.single()
        peers_stamped = rec["n"] if rec else 0

    total = inter_flagged + peers_stamped
    if total or inter_cleared or peers_cleared:
        log.info("correlate.stp_topology_stamped",
                 inter_flagged=inter_flagged,
                 inter_cleared=inter_cleared,
                 peers_stamped=peers_stamped,
                 peers_cleared=peers_cleared)
    return total


# ── WAN / Internet uplink inference ──────────────────────────────────────────
#
# Reserved / private ASN ranges per IANA / RFC 6996:
#   * 64512-65534  : 16-bit private use
#   * 65535         : reserved sentinel
#   * 4200000000-4294967294 : 32-bit private use
#   * 4294967295    : reserved sentinel
#
# Anything outside these ranges (and outside the documentation block
# 64496-64511 / 65536-65551 / 65552 + 4200000000) is treated as a real
# routable upstream ASN.  Picked deliberately liberal so an ISP's
# private-AS edge that happens to leak past us still shows up rather
# than disappearing silently.
def _is_public_asn(asn: int | None) -> bool:
    if asn is None:
        return False
    try:
        n = int(asn)
    except (TypeError, ValueError):
        return False
    if n <= 0 or n >= 4294967295:
        return False
    if 64496 <= n <= 64511:           # documentation
        return False
    if 64512 <= n <= 65534:           # 16-bit private
        return False
    if n == 65535:                    # 16-bit reserved
        return False
    if 65536 <= n <= 65551:           # documentation
        return False
    if 4200000000 <= n <= 4294967294: # 32-bit private
        return False
    return True


async def _detect_home_asn() -> int | None:
    """Auto-detect the operator's "home" Autonomous System.

    A naïve scan of every device's BGP peers can't tell the difference
    between an iBGP route-reflector adjacency (same AS) and a real eBGP
    uplink to a transit provider.  Without that distinction we'd treat
    every internal route reflector as a separate "upstream Internet
    provider" — exactly the misread the operator caught at AS11017.

    Heuristic, ordered by signal strength:

      1.  Public ASNs only — private ranges (RFC 6996) can't be a home
          AS for the public Internet.
      2.  Score each candidate AS by:
              * distinct devices that peer to it
              * distinct sites those devices live in
              * total BGP peer endpoints to that AS
          Multi-site peering is the strongest signal; a real transit
          provider almost always peers from exactly one border device
          at one site, while an iBGP home AS peers from every router
          at every site.
      3.  Require the winner to satisfy at least one of:
              * peers from ≥2 sites, OR
              * peers from ≥3 devices
          to avoid mis-flagging a single-site stub deployment.
      4.  Return the winner's ASN; otherwise None (treat everything as
          eBGP, same as today).
    """
    driver = get_driver()
    async with driver.session() as session:
        rows = await (await session.run(
            """
            MATCH (d:Device)-[r:ROUTING_PEER]->()
            WHERE r.remote_as IS NOT NULL
              AND d.tombstoned IS NULL
            RETURN r.remote_as AS asn,
                   count(DISTINCT d) AS dev_count,
                   count(DISTINCT d.netbox_site_slug) AS site_count,
                   count(r) AS peer_count
            """
        )).data()

    best_asn: int | None = None
    best_score = (0, 0, 0)
    for row in rows:
        try:
            asn = int(row["asn"])
        except (TypeError, ValueError):
            continue
        if not _is_public_asn(asn):
            continue
        score = (row["site_count"] or 0,
                 row["dev_count"]  or 0,
                 row["peer_count"] or 0)
        if score > best_score:
            best_score = score
            best_asn = asn

    site_count, dev_count, _ = best_score
    if site_count >= 2 or dev_count >= 3:
        return best_asn
    return None


async def _infer_wan_topology() -> int:
    """Stitch every WAN-edge Device to the public Internet.

    Discovery model:

      0. Auto-detect the operator's home AS (see ``_detect_home_asn``).
         All subsequent BGP-based logic treats peers in that AS as
         iBGP — they do not count as Internet uplinks.

      1. **Meraki MX uplinks**: every Device with ``wan{1,2}_public_ip``
         populated emits a direct Device → Internet WAN_UPLINK
         tagged with the slot, public IP, and (when present) private
         IP.  These devices uplink through whatever ISP NATs them; we
         don't yet do reverse IP→ASN lookups so they bypass the AS
         layer.

      2. **eBGP-to-external-AS**: a Device with an established
         ROUTING_PEER edge whose ``remote_as`` is public *and* not the
         home AS emits:
             * Device → AutonomousSystem(as:<asn>) WAN_UPLINK
             * AutonomousSystem → Internet TRANSITS edge

         The WAN_UPLINK edge IS the AS boundary marker — earlier
         iterations also materialized a separate home-AS hexagon and
         AS_PEER edge, but the home-AS node and its AS_PEER
         relationships were visually redundant on top of the
         per-device ``local_asn`` halo.  Dev3 dropped them; the AS
         boundary is now read straight off the eBGP WAN_UPLINK lines.

    Every Device that participates in at least one rule is stamped
    ``is_wan_edge=true``.  Devices that have BGP evidence (iBGP or
    eBGP) of being inside the home AS get ``local_asn=<home_asn>`` so
    the UI can draw an AS-membership halo around the home network.

    Returns the count of WAN_UPLINK edges created or refreshed.
    """
    driver = get_driver()
    created = 0
    home_asn = await _detect_home_asn()

    async with driver.session() as session:
        # ── Ensure the singleton Internet node exists.
        await session.run(
            """
            MERGE (n:Internet {id: 'internet:0'})
            ON CREATE SET
                n.name = 'Internet',
                n.type = 'Internet',
                n.created_at = timestamp(),
                n.source_adapter = 'correlator'
            SET n.dimensions = ['wan']
            """
        )

        # ── Snapshot status-history off the WAN_UPLINK edges we're
        # about to DELETE+rebuild so flap-detection survives the
        # destructive cleanup.  The history correlator stores the
        # connectivity strip + flap stats AS PROPERTIES on the edge,
        # and we delete-recreate the edge every cycle below — which
        # would otherwise reset history to a fresh seed every cycle
        # and make every WAN uplink look stable forever.
        #
        # We key the snapshot on (src_id, dst_id, via, wan_slot|asn)
        # because that's what the Rule 1 / Rule 2 MERGE statements
        # below use to identify each uplink uniquely.  After the
        # rebuild we replay these properties onto whichever
        # recreated edge matches.
        # NOTE: we also capture r.oper_status itself.  Without this,
        # _enrich_wan_uplinks_with_health below would see prev_oper=NULL
        # on every freshly recreated edge and re-stamp
        # oper_status_changed_at on every correlator cycle — the exact
        # bug that caused the misleading 30-ms cluster of "20 WAN
        # uplinks just went down" alerts in top_problems.  Restoring
        # oper_status lets the transition detector see the genuine
        # before-state and only stamp _changed_at on real changes.
        prev_wan_history = await (await session.run(
            """
            MATCH (s)-[r:WAN_UPLINK]->(t)
            WHERE r.source = 'correlator'
              AND (r.oper_status_history IS NOT NULL
                   OR r.oper_status IS NOT NULL)
            RETURN elementId(s) AS src_id, elementId(t) AS dst_id,
                   r.via AS via,
                   coalesce(r.wan_slot, '') AS wan_slot,
                   coalesce(r.asn, -1)      AS asn,
                   r.oper_status            AS oper_status,
                   r.oper_status_history    AS hist,
                   r.oper_status_changed_at AS changed_at,
                   r.oper_status_flap_count_1h  AS flap_1h,
                   r.oper_status_flap_count_24h AS flap_24h,
                   r.oper_status_flap_score_1h  AS flap_score,
                   r.oper_status_flap_state     AS flap_state,
                   r.first_seen AS first_seen
            """
        )).data()

        # ── Clear stale WAN context before re-stamping.  Correlator-
        # owned WAN_UPLINK edges and per-device home-AS flags all get
        # rebuilt from current data below.  The AS_PEER edge type
        # was retired in dev3 but we sweep any leftovers so an
        # upgrade-in-place doesn't leave dangling boundary lines.
        await session.run(
            """
            MATCH ()-[r:WAN_UPLINK]->()
            WHERE r.source = 'correlator'
            DELETE r
            """
        )
        await session.run("MATCH ()-[r:AS_PEER]->() DELETE r")
        await session.run(
            """
            MATCH (d:Device)
            WHERE d.is_wan_edge IS NOT NULL OR d.local_asn IS NOT NULL
            REMOVE d.is_wan_edge, d.wan_edge_reason, d.local_asn
            """
        )

        # ── Sweep stale home-AS nodes left over from dev2.
        # The home AS used to be materialized as a hexagon + AS_PEER
        # edges; dev3 dropped that visual.  Deleting any prior
        # is_home=true node here is a one-time cleanup that becomes a
        # no-op after the first run.
        if home_asn is not None:
            await session.run(
                """
                MATCH (a:AutonomousSystem)
                WHERE coalesce(a.is_home, false) = true
                   OR a.id = $home_id
                DETACH DELETE a
                """,
                home_id=f"as:{home_asn}",
            )

        # ── Rule 1: Meraki MX uplinks.  One MERGE per slot so dual-WAN
        # devices show both edges and properties stay slot-isolated.
        for slot, ip_prop in (("wan1", "wan1_public_ip"),
                              ("wan2", "wan2_public_ip")):
            res = await session.run(
                f"""
                MATCH (d:Device)
                WHERE d.{ip_prop} IS NOT NULL
                  AND d.{ip_prop} <> ''
                  AND d.tombstoned IS NULL
                MATCH (i:Internet {{id: 'internet:0'}})
                MERGE (d)-[r:WAN_UPLINK {{
                    source: 'correlator',
                    via: 'mx_uplink',
                    wan_slot: '{slot}'
                }}]->(i)
                SET r.public_ip   = d.{ip_prop},
                    r.private_ip  = coalesce(d.{slot}_ip, ''),
                    r.dimension   = 'wan',
                    r.updated_at  = timestamp()
                SET d.is_wan_edge      = true,
                    d.wan_edge_reason  = 'mx_uplink'
                RETURN count(r) AS n
                """
            )
            rec = await res.single()
            created += rec["n"] if rec else 0

        # ── Pull every (device, remote_as) pair once and split them
        # into iBGP (home AS) vs eBGP (everything else public).
        rows = await (await session.run(
            """
            MATCH (d:Device)-[r:ROUTING_PEER]->(p:RoutingPeer)
            WHERE r.remote_as IS NOT NULL
              AND coalesce(r.state, '') IN ['', 'established', 'idle',
                                              'active', 'connect',
                                              'opensent', 'openconfirm']
              AND d.tombstoned IS NULL
            RETURN d.id   AS dev_id,
                   d.name AS dev_name,
                   r.remote_as AS asn,
                   collect(DISTINCT p.peer_ip) AS peers
            """
        )).data()

        ibgp_devices: set[str] = set()
        ebgp_devices: set[str] = set()
        as_to_create: dict[int, dict] = {}
        uplinks_to_create: list[dict] = []

        for row in rows:
            try:
                asn = int(row["asn"])
            except (TypeError, ValueError):
                continue
            if not _is_public_asn(asn):
                continue
            peers = sorted(p for p in (row["peers"] or []) if p)
            if home_asn is not None and asn == home_asn:
                # iBGP — internal to the home AS, not a WAN uplink.
                ibgp_devices.add(row["dev_id"])
                continue
            # eBGP — real external peer.
            ebgp_devices.add(row["dev_id"])
            as_to_create.setdefault(asn, {"asn": asn})
            uplinks_to_create.append({
                "dev_id":     row["dev_id"],
                "dev_name":   row["dev_name"],
                "asn":        asn,
                "peer_ip":    peers[0] if peers else None,
                "peer_count": len(peers),
            })

        # ── Materialise each external AS, wire TRANSITS to Internet,
        # and (when we know the home AS) wire AS_PEER to the home AS.
        for asn_data in as_to_create.values():
            await session.run(
                """
                MERGE (a:AutonomousSystem {id: $id})
                ON CREATE SET
                    a.created_at = timestamp(),
                    a.source_adapter = 'correlator'
                SET a.asn = $asn,
                    a.name = 'AS' + toString($asn),
                    a.type = 'AutonomousSystem',
                    a.is_home = false,
                    a.dimensions = ['wan']
                WITH a
                MATCH (i:Internet {id: 'internet:0'})
                MERGE (a)-[t:TRANSITS {source: 'correlator'}]->(i)
                SET t.dimension = 'wan',
                    t.updated_at = timestamp()
                """,
                id=f"as:{asn_data['asn']}", asn=asn_data["asn"],
            )

        # ── Border-router WAN_UPLINK edges + AS_PEER boundary edges.
        for up in uplinks_to_create:
            res = await session.run(
                """
                MATCH (d:Device {id: $dev_id})
                MATCH (a:AutonomousSystem {id: $as_id})
                MERGE (d)-[r:WAN_UPLINK {
                    source: 'correlator',
                    via: 'ebgp',
                    asn: $asn
                }]->(a)
                SET r.peer_ip    = $peer_ip,
                    r.peer_count = $peer_count,
                    r.dimension  = 'wan',
                    r.updated_at = timestamp()
                SET d.is_wan_edge     = true,
                    d.wan_edge_reason = coalesce(d.wan_edge_reason, 'ebgp_public')
                RETURN count(r) AS n
                """,
                dev_id=up["dev_id"], as_id=f"as:{up['asn']}",
                asn=up["asn"], peer_ip=up["peer_ip"],
                peer_count=up["peer_count"],
            )
            rec = await res.single()
            created += rec["n"] if rec else 0

        # ── Stamp local_asn on every device that has BGP evidence of
        # being inside the home AS.  Two sources of evidence:
        #
        #   1. iBGP peer to the home AS — device clearly inside.
        #   2. eBGP peer to any external AS — device is one *side* of
        #      a boundary, and the home-AS side is ours by definition.
        #      Without this rule the cat8k border routers
        #      (`cpn-ful-cat8k1`, `cpn-ash-cat8k1`) — whose only
        #      collected BGP session is the eBGP one to Lumen / HE —
        #      would be missing from the home-AS halo despite plainly
        #      being inside AS11017.
        #
        # We're still deliberately conservative on the "no BGP at
        # all" case: devices we have no routing evidence for stay
        # unstamped rather than be assumed inside the home AS.
        # That keeps the UI ring honest as "we KNOW this is in our
        # AS" instead of bleeding into "we assume so".
        in_home_as = ibgp_devices | ebgp_devices
        if home_asn is not None and in_home_as:
            await session.run(
                """
                MATCH (d:Device)
                WHERE d.id IN $ids
                SET d.local_asn = $asn
                """,
                ids=list(in_home_as), asn=home_asn,
            )

        # ── Sweep orphan AutonomousSystem nodes — an upstream AS that
        # nobody uplinks to any more should disappear so the WAN
        # overlay stays tidy.  The Internet node is intentionally
        # left even when nothing uplinks to it (it's a stable
        # landmark).
        await session.run(
            """
            MATCH (a:AutonomousSystem)
            WHERE NOT (a)<-[:WAN_UPLINK]-()
              AND coalesce(a.source_adapter, '') = 'correlator'
            DETACH DELETE a
            """
        )

    if created or home_asn is not None:
        log.info("correlate.wan_uplinks_inferred",
                 edges=created,
                 home_asn=home_asn,
                 ibgp_devices=len(ibgp_devices) if 'ibgp_devices' in locals() else 0,
                 ebgp_devices=len(ebgp_devices) if 'ebgp_devices' in locals() else 0,
                 external_ases=len(as_to_create) if 'as_to_create' in locals() else 0)

    # ── Replay the snapshotted status-history onto the freshly
    # rebuilt edges so flap-detection survives the destructive
    # rebuild above.  Match by the same identity tuple we used to
    # capture: (src_id, dst_id, via, wan_slot|asn).  If a particular
    # uplink is no longer emitted by either rule (truly retired),
    # its snapshot simply has nowhere to land and is dropped — the
    # uplink no longer exists, so losing its history is correct.
    if prev_wan_history:
        restored = 0
        async with driver.session() as session:
            for snap in prev_wan_history:
                res = await session.run(
                    """
                    MATCH (s)-[r:WAN_UPLINK]->(t)
                    WHERE elementId(s) = $src_id
                      AND elementId(t) = $dst_id
                      AND r.source     = 'correlator'
                      AND r.via        = $via
                      AND coalesce(r.wan_slot, '') = $wan_slot
                      AND coalesce(r.asn, -1)      = $asn
                    SET r.oper_status                 = coalesce(r.oper_status, $oper_status),
                        r.oper_status_history         = coalesce($hist, r.oper_status_history),
                        r.oper_status_changed_at      = coalesce(r.oper_status_changed_at, $changed_at),
                        r.oper_status_flap_count_1h   = coalesce($flap_1h, r.oper_status_flap_count_1h),
                        r.oper_status_flap_count_24h  = coalesce($flap_24h, r.oper_status_flap_count_24h),
                        r.oper_status_flap_score_1h   = coalesce($flap_score, r.oper_status_flap_score_1h),
                        r.oper_status_flap_state      = coalesce($flap_state, r.oper_status_flap_state),
                        r.first_seen                  = coalesce(r.first_seen, $first_seen)
                    RETURN count(r) AS n
                    """,
                    src_id=snap["src_id"], dst_id=snap["dst_id"],
                    via=snap["via"], wan_slot=snap["wan_slot"], asn=snap["asn"],
                    oper_status=snap["oper_status"],
                    hist=snap["hist"], changed_at=snap["changed_at"],
                    flap_1h=snap["flap_1h"], flap_24h=snap["flap_24h"],
                    flap_score=snap["flap_score"], flap_state=snap["flap_state"],
                    first_seen=snap["first_seen"],
                )
                rec = await res.single()
                restored += (rec["n"] if rec else 0) or 0
        if restored:
            log.info("correlate.wan_uplink_history_restored", restored=restored,
                     captured=len(prev_wan_history))

    # ── Stamp link-health onto every WAN_UPLINK so the UI can
    # color / size it the same way it does PHYSICAL_LINK.  Runs
    # separately so it can be called on its own from tests.
    health_stamped = await _enrich_wan_uplinks_with_health()
    if health_stamped:
        log.info("correlate.wan_uplinks_health_stamped", edges=health_stamped)

    return created


async def _enrich_wan_uplinks_with_health() -> int:
    """Annotate every WAN_UPLINK with port-state / errors / utilization.

    Two sources of health, picked per-edge based on the discovery
    rule that emitted the WAN_UPLINK:

    1. **eBGP uplinks** (``via='ebgp'``): the egress interface is the
       one on the border device whose assigned IP shares a subnet
       with the eBGP peer IP.  In Python we walk every interface IP
       on the device and find the smallest /N (try /31, /30, /29,
       …, /24) that contains both the peer IP and the interface IP;
       the matching interface is the egress port.  Health
       (``oper_status``, ``util_in_pct``, ``health_score``,
       ``error_rate_per_s``, ``speed_mbps``) is copied onto the
       WAN_UPLINK edge.

    2. **MX uplinks** (``via='mx_uplink'``): Meraki MX devices don't
       reliably expose WAN1/WAN2 ports in IF-MIB so SNMP-discovered
       Interface nodes are missing.  Fall back to device-level
       signals: ``oper_status`` derives from ``Device.status``
       (`active` / `online` → up, otherwise down), and a coarse
       ``health_score`` from ``Device.snmp_health`` (`cloud_only`
       → 30 yellow, `unreachable` → 80 red, anything else → 0).

    Both rules emit the same set of properties on the WAN_UPLINK
    edge so the UI styling code can treat them uniformly.

    Returns the number of WAN_UPLINK edges updated.
    """
    import ipaddress
    driver = get_driver()
    updated = 0

    async with driver.session() as session:
        # ── Rule 1: eBGP uplinks — find egress interface by IP-prefix
        # intersection with the BGP peer IP.
        ebgp_rows = await (await session.run(
            """
            MATCH (d:Device)-[r:WAN_UPLINK {via: 'ebgp', source: 'correlator'}]->()
            WHERE r.peer_ip IS NOT NULL AND r.peer_ip <> ''
            OPTIONAL MATCH (d)-[:HAS_INTERFACE]->(i:Interface)
                          -[:ASSIGNED_IP]->(ip:IPAddress)
            WITH d, r, elementId(r) AS rid,
                 collect(DISTINCT {
                    iface: i.name,
                    ip:    ip.address,
                    oper:  i.oper_status,
                    util:  i.util_in_pct,
                    err:   coalesce(i.error_rate_per_s, i.error_rate),
                    speed: i.speed_mbps,
                    health: i.health_score
                 }) AS ifaces
            RETURN rid, r.peer_ip AS peer, ifaces
            """
        )).data()

        for row in ebgp_rows:
            peer_str = row["peer"]
            try:
                peer = ipaddress.ip_address(peer_str)
            except (ValueError, TypeError):
                continue

            # Walk /31 → /24 and find every interface IP that shares
            # a subnet with the peer.  In practice eBGP peerings use
            # /30 or /31 transit nets, but we try a range to cover
            # misconfigurations and IPv6 ULAs.
            #
            # When multiple Interface records on the same Device
            # share the same IP (a *very* common artifact of running
            # both CATC and SNMP adapters against the same physical
            # box — they each create their own Interface node with
            # the long vs. short ifName), score them by:
            #   1. longest prefix that contains both IPs (best match)
            #   2. within tie: prefer entries with a non-null
            #      oper_status, then non-null health_score / util
            # so we land on the SNMP-populated record (`Te0/0/4`)
            # rather than the CATC stub (`TenGigabitEthernet0/0/4`,
            # which exists but never gets per-port stats polled).
            best: dict | None = None
            best_mask = -1
            for entry in (row.get("ifaces") or []):
                if not entry.get("ip") or not entry.get("iface"):
                    continue
                try:
                    iface_ip = ipaddress.ip_address(entry["ip"])
                except (ValueError, TypeError):
                    continue
                if iface_ip.version != peer.version:
                    continue
                masks = (range(31, 23, -1) if peer.version == 4
                         else range(127, 63, -1))
                matched_mask = -1
                for mask in masks:
                    try:
                        net = ipaddress.ip_network(
                            f"{iface_ip}/{mask}", strict=False)
                    except ValueError:
                        continue
                    if peer in net and iface_ip in net:
                        matched_mask = mask
                        break
                if matched_mask < 0:
                    continue
                # Score: (mask, has_oper, has_health, has_util) — bigger wins.
                def _score(e: dict, mask: int) -> tuple[int, int, int, int]:
                    return (
                        mask,
                        1 if e.get("oper") else 0,
                        1 if e.get("health") is not None else 0,
                        1 if e.get("util") is not None else 0,
                    )
                if best is None:
                    best, best_mask = entry, matched_mask
                else:
                    if _score(entry, matched_mask) > _score(best, best_mask):
                        best, best_mask = entry, matched_mask

            if not best:
                continue

            # Stamp ``oper_status_changed_at`` only when the value
            # actually transitions (including null→value).  Lets the
            # UI render "down since 3h ago" instead of just showing
            # the current state.
            await session.run(
                """
                MATCH ()-[r:WAN_UPLINK]->()
                WHERE elementId(r) = $rid
                WITH r, r.oper_status AS prev_oper
                SET r.egress_iface  = $iface,
                    r.egress_ip     = $ip,
                    r.oper_status   = $oper,
                    r.util_in_pct   = $util,
                    r.util_out_pct  = $util,
                    r.util_pct      = $util,
                    r.error_rate    = $err,
                    r.error_rate_per_s = $err,
                    r.speed_mbps    = $speed,
                    r.health_score  = $health,
                    r.health_source = 'interface'
                FOREACH (_ IN CASE
                         WHEN coalesce($oper,'') <> coalesce(prev_oper,'')
                          AND $oper IS NOT NULL
                         THEN [1] ELSE [] END |
                  SET r.oper_status_changed_at = $now)
                """,
                rid=row["rid"],
                iface=best.get("iface"),
                ip=best.get("ip"),
                oper=best.get("oper"),
                util=best.get("util"),
                err=best.get("err"),
                speed=best.get("speed"),
                health=best.get("health"),
                now=_now_ms(),
            )
            updated += 1

        # ── Rule 2: MX uplinks — prefer per-uplink Meraki Dashboard
        # status (``Device.mx_wan1_status`` / ``mx_wan2_status``) when
        # available, since that's per-WAN-port accuracy.  Fall back to
        # device-wide ``Device.status`` if the Meraki uplink endpoint
        # hasn't been polled yet.
        #
        # Bucketing: oper='down' → red (health 80), Meraki cloud_only
        # → mild yellow (25), unreachable → 70, otherwise 0 (green).
        mx_rows = await (await session.run(
            """
            MATCH (d:Device)-[r:WAN_UPLINK {via: 'mx_uplink', source: 'correlator'}]->()
            RETURN elementId(r) AS rid,
                   coalesce(d.status, '')         AS status,
                   coalesce(d.snmp_health, '')    AS snmp_health,
                   coalesce(r.wan_slot, '')       AS slot,
                   d.mx_wan1_status                AS wan1_status,
                   d.mx_wan2_status                AS wan2_status,
                   d.mx_wan1_status_raw            AS wan1_raw,
                   d.mx_wan2_status_raw            AS wan2_raw
            """
        )).data()

        for row in mx_rows:
            slot = (row["slot"] or "").lower()  # 'wan1' or 'wan2'
            # Prefer per-uplink status when we have it
            per_uplink_status = None
            per_uplink_raw    = None
            if slot == "wan1":
                per_uplink_status = (row.get("wan1_status") or "").lower() or None
                per_uplink_raw    = row.get("wan1_raw")
            elif slot == "wan2":
                per_uplink_status = (row.get("wan2_status") or "").lower() or None
                per_uplink_raw    = row.get("wan2_raw")

            if per_uplink_status in ("up", "down", "unknown", "disabled"):
                oper = per_uplink_status
                health_source = "mx_uplink"
            else:
                status = (row["status"] or "").lower()
                if status in ("active", "online", "up"):
                    oper = "up"
                elif status in ("alerting", "dormant", "offline", "down"):
                    oper = "down"
                else:
                    oper = "unknown"
                health_source = "mx_device"

            snmp_h = (row["snmp_health"] or "").lower()
            if oper == "down":
                health = 80
            elif snmp_h == "unreachable":
                health = 70
            elif snmp_h in ("cloud_only", "stale"):
                health = 25
            else:
                health = 0

            await session.run(
                """
                MATCH ()-[r:WAN_UPLINK]->()
                WHERE elementId(r) = $rid
                WITH r, r.oper_status AS prev_oper
                SET r.egress_iface     = $slot,
                    r.oper_status      = $oper,
                    r.oper_status_raw  = $oper_raw,
                    r.util_in_pct      = coalesce(r.util_in_pct, 0),
                    r.util_out_pct     = coalesce(r.util_out_pct, 0),
                    r.health_score     = $health,
                    r.health_source    = $health_source
                FOREACH (_ IN CASE
                         WHEN coalesce($oper,'') <> coalesce(prev_oper,'')
                          AND $oper IS NOT NULL
                         THEN [1] ELSE [] END |
                  SET r.oper_status_changed_at = $now)
                """,
                rid=row["rid"],
                slot=row["slot"].upper() if row["slot"] else None,
                oper=oper,
                oper_raw=per_uplink_raw,
                health=health,
                health_source=health_source,
                now=_now_ms(),
            )
            updated += 1

        # ── Rule 3: Derive Meraki MX device oper_state from uplinks.
        #
        # Device.status from /organizations/{org}/devices is frequently
        # inventory-oriented ("active") and can stay "up" even when both
        # WAN circuits are down. Use per-uplink WAN state + recency to
        # produce a more operationally meaningful node state.
        now_ms = _now_ms()
        stale_cutoff_ms = now_ms - 86_400_000  # 24h
        dev_res = await session.run(
            """
            MATCH (d:Device)
            WHERE toLower(coalesce(d.platform, '')) = 'meraki'
              AND (
                toLower(coalesce(d.model, '')) STARTS WITH 'mx'
                OR toLower(coalesce(d.role, '')) = 'firewall'
              )
            WITH d,
                 [s IN [toLower(d.mx_wan1_status), toLower(d.mx_wan2_status)]
                  WHERE s IS NOT NULL AND s <> ''] AS wan_states
            SET d.oper_state = CASE
                  WHEN size(wan_states) > 0
                   AND all(s IN wan_states WHERE s IN ['down', 'disabled'])
                    THEN 'down'
                  WHEN d.meraki_last_reported_at IS NOT NULL
                   AND d.meraki_last_reported_at < $stale_cutoff_ms
                    THEN 'alerting'
                  WHEN size(wan_states) > 0
                   AND any(s IN wan_states WHERE s = 'up')
                    THEN 'up'
                  WHEN size(wan_states) > 0
                    THEN 'alerting'
                  ELSE coalesce(d.oper_state, d.status, 'unknown')
                END,
                d.oper_state_source = 'mx_uplink_rollup',
                d.oper_state_updated_at = $now_ms
            RETURN count(d) AS c
            """,
            now_ms=now_ms,
            stale_cutoff_ms=stale_cutoff_ms,
        )
        rec = await dev_res.single()
        if rec and (rec.get("c") or 0):
            log.info("correlate.wan_uplinks.mx_device_state_rollup", devices=int(rec.get("c") or 0))

    return updated


def _now_ms() -> int:
    """Local timestamp helper — epoch ms, matches util.timestamps.epoch_ms.

    Inlined here to avoid the cross-module import overhead in a
    correlator hot path that runs every cycle.
    """
    import time as _t
    return int(_t.time() * 1000)


async def _attach_devices_to_site_vlans() -> int:
    """Wire up LOGICAL_MEMBER for site-scoped NetBox VLANs.

    No adapter currently writes a device→VLAN membership edge for
    NetBox-modeled VLANs at the site level: the Meraki adapter
    creates per-org VLAN nodes but doesn't join them to device
    membership, and the SNMP adapter only knows port-level VLANs on
    a switch (not on phones, APs, MX firewalls, etc.).  The result
    in the topology view is a floating VLAN 1 node at every Meraki
    site that has no edges, even though "every device at the site
    is reachable on VLAN 1" is the operator's actual mental model.

    For every Device d and canonical NetBox VLAN v where:

      * v.id starts with 'vlan:nb:'  (NetBox-sourced, not a stub)
      * d.netbox_site_slug = v.netbox_site_slug   (same site)
      * no LOGICAL_MEMBER edge already exists between them

    we MERGE a LOGICAL_MEMBER edge tagged with ``source='correlator'``
    and ``inferred_via='site_slug'`` so adapter-emitted edges (with
    real per-port membership) always win over this site-default
    fallback during dedup.
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.id STARTS WITH 'vlan:nb:'
              AND v.netbox_site_slug IS NOT NULL
              AND v.netbox_site_slug <> ''
            MATCH (d:Device)
            WHERE d.netbox_site_slug = v.netbox_site_slug
              AND d.tombstoned IS NULL
              AND NOT (d)-[:LOGICAL_MEMBER]->(v)
            MERGE (d)-[m:LOGICAL_MEMBER {
                source: 'correlator',
                inferred_via: 'site_slug'
            }]->(v)
            ON CREATE SET m.created_at = timestamp(),
                          m.role = 'site-default'
            RETURN count(m) AS n
            """
        )
        rec = await result.single()
        added = rec["n"] if rec else 0

    if added:
        log.info("correlate.site_vlan_members_attached", count=added)
    return added


async def _decorate_physical_links_l3() -> int:
    """Stamp PHYSICAL_LINK edges with the L3 prefixes routed across them.

    Two complementary strategies, both run, results merged per-edge:

    Strategy A — ROUTES_TO + interface (primary)
    --------------------------------------------
    The richest evidence: every directly-connected route an adapter
    discovered carries an interface name on the originating device.
    For example SNMP polling cat9k1 yields::

        (cpn-ful-cat9k1)-[:ROUTES_TO {interface:'Twe1/1/1'}]->(:Prefix {cidr:'192.133.161.128/30'})

    If a PHYSICAL_LINK already exists out of that interface (regardless
    of who polled the OTHER side), the prefix demonstrably lives on
    that cable.  We use this single-sided evidence to stamp the cable
    even when the peer wasn't SNMP-polled (Meraki MX, partner router,
    ARP-correlated cable, etc.) — the previous both-sides-or-skip rule
    missed those entirely.

    SVI interface names (``Vl<vid>``, ``Vlan<vid>``, ``BVI*``,
    ``BDI*``) are excluded here on purpose — those prefixes belong on
    the VLAN node (via ``_link_vlan_svis_and_prefixes`` path 4), not
    on any single cable.

    Strategy B — both-sides ASSIGNED_IP intersection (fallback)
    ----------------------------------------------------------
    Original behaviour, preserved for cables where neither side has a
    ROUTES_TO but both sides have ASSIGNED_IP records that happen to
    share a Prefix CIDR.  Rare in practice but harmless to keep.

    Properties set on the PHYSICAL_LINK
    -----------------------------------
    * ``l3_prefix``       — first discovered CIDR (kept for backwards
      compat with UI code that reads the singular field).
    * ``l3_prefix_v4``    — list of all IPv4 CIDRs the cable carries.
    * ``l3_prefix_v6``    — list of all IPv6 CIDRs the cable carries.
      Both lists are deduplicated and sorted for stable rendering.
    * ``is_routed``       — true iff any prefix attached.
    * ``l3_updated_at``   — epoch-ms; lets the worker prune stale data.
    """
    import ipaddress
    driver = get_driver()
    updated = 0
    async with driver.session() as session:
        # ── Strategy A: ROUTES_TO + interface ─────────────────────────
        #
        # Returns one row per (PHYSICAL_LINK, prefix) pair.  We then
        # aggregate per-link in Python so we can split v4/v6 cleanly
        # and dedupe identical CIDRs that come in via multiple
        # ROUTES_TO observations (the same /30 typically appears as
        # both v4 and v6 next-hop rows from a dual-stack catalyst).
        #
        # Important: the ROUTES_TO and the PHYSICAL_LINK don't always
        # hang off the same Device node.  Many real graphs still have
        # duplicate Device nodes for the same hostname (e.g.
        # `meraki:Q5TY-…`, `cdp-neighbor:cpn-ful-cat9k1`,
        # `lldp-neighbor:cpn-ful-cat9k1`) that haven't been
        # canonically merged yet — each adapter creates its own.
        # SNMP-derived ROUTES_TO lands on one node, LLDP-derived
        # PHYSICAL_LINK lands on another, and a strict same-node join
        # silently drops every prefix that crosses this boundary.
        #
        # Joining on `Device.name` instead bridges the duplicates
        # without requiring full canonicalization to land first.  The
        # name is also case-insensitively compared because some
        # adapters normalize hostnames to lower-case.
        rows_a = await (await session.run(
            """
            MATCH (d_route:Device)-[rt:ROUTES_TO]->(p:Prefix)
            WHERE rt.interface IS NOT NULL
              AND coalesce(p.cidr, p.prefix) IS NOT NULL
              AND d_route.name IS NOT NULL
              AND d_route.name <> ''
              // Exclude SVI / virtual L3 interfaces — their prefixes
              // belong on the VLAN, not on a physical cable.
              AND NOT toLower(rt.interface) STARTS WITH 'vlan'
              AND NOT toLower(rt.interface) STARTS WITH 'vl'
              AND NOT toLower(rt.interface) STARTS WITH 'bvi'
              AND NOT toLower(rt.interface) STARTS WITH 'bdi'
              AND NOT toLower(rt.interface) STARTS WITH 'loopback'
              AND NOT toLower(rt.interface) STARTS WITH 'tunnel'
            MATCH (d_link:Device)-[pl:PHYSICAL_LINK]-(:Device)
            WHERE d_link.name IS NOT NULL
              AND toLower(d_link.name) = toLower(d_route.name)
              AND (
                (startNode(pl) = d_link AND
                   toLower(coalesce(pl.interface_a_active, pl.interface_a, ''))
                     = toLower(rt.interface))
                OR
                (endNode(pl) = d_link AND
                   toLower(coalesce(pl.interface_b_active, pl.interface_b, ''))
                     = toLower(rt.interface))
              )
            RETURN DISTINCT
              elementId(pl)              AS rid,
              coalesce(p.cidr, p.prefix) AS cidr,
              p.version                  AS version
            """
        )).data()

        # Aggregate per-link.  We compute version from the CIDR if the
        # node's `version` field is missing (some adapters don't set it).
        per_link: dict[str, dict[str, set[str]]] = {}
        for r in rows_a:
            rid = r["rid"]
            cidr = r["cidr"]
            v = r["version"]
            if v is None:
                try:
                    v = ipaddress.ip_network(cidr, strict=False).version
                except (ValueError, TypeError):
                    continue
            slot = "v4" if int(v) == 4 else "v6" if int(v) == 6 else None
            if not slot:
                continue
            per_link.setdefault(rid, {"v4": set(), "v6": set()})[slot].add(cidr)

        # ── Strategy B: both-sided ASSIGNED_IP intersection ───────────
        rows_b = await (await session.run(
            """
            MATCH (a)-[r:PHYSICAL_LINK]->(b)
            WHERE r.interface_a IS NOT NULL AND r.interface_b IS NOT NULL
            OPTIONAL MATCH (a)-[:HAS_INTERFACE]->(ia:Interface)
                -[:ASSIGNED_IP]->(ip_a:IPAddress)
                WHERE toLower(coalesce(ia.name_canonical, ia.name))
                    = toLower(coalesce(r.interface_a_active, r.interface_a))
            OPTIONAL MATCH (b)-[:HAS_INTERFACE]->(ib:Interface)
                -[:ASSIGNED_IP]->(ip_b:IPAddress)
                WHERE toLower(coalesce(ib.name_canonical, ib.name))
                    = toLower(coalesce(r.interface_b_active, r.interface_b))
            WITH r, collect(DISTINCT ip_a.address) AS ips_a,
                    collect(DISTINCT ip_b.address) AS ips_b
            WHERE size(ips_a) > 0 AND size(ips_b) > 0
            RETURN elementId(r) AS rid, ips_a, ips_b
            """
        )).data()

        if rows_b:
            prefix_rows = await (await session.run(
                "MATCH (p:Prefix) "
                "WHERE coalesce(p.cidr, p.prefix) IS NOT NULL "
                "RETURN coalesce(p.cidr, p.prefix) AS cidr"
            )).data()
            prefixes: list[
                ipaddress.IPv4Network | ipaddress.IPv6Network
            ] = []
            for prow in prefix_rows:
                try:
                    prefixes.append(
                        ipaddress.ip_network(prow["cidr"], strict=False)
                    )
                except (ValueError, TypeError):
                    continue

            for row in rows_b:
                try:
                    a_ips = [
                        ipaddress.ip_address(x) for x in row["ips_a"] if x
                    ]
                    b_ips = [
                        ipaddress.ip_address(x) for x in row["ips_b"] if x
                    ]
                except (ValueError, TypeError):
                    continue
                for net in prefixes:
                    if any(ip in net for ip in a_ips) and any(
                        ip in net for ip in b_ips
                    ):
                        slot = "v4" if net.version == 4 else "v6"
                        per_link.setdefault(
                            row["rid"], {"v4": set(), "v6": set()}
                        )[slot].add(str(net))

        # ── Single write pass: stamp each cable with its aggregated
        # v4/v6 CIDR lists.  Use the first non-empty CIDR as the
        # legacy singular `l3_prefix` so older UI code keeps working.
        write_rows = []
        for rid, slots in per_link.items():
            v4 = sorted(slots["v4"])
            v6 = sorted(slots["v6"])
            first = (v4 + v6)[0] if (v4 or v6) else None
            if not first:
                continue
            write_rows.append({
                "rid": rid, "v4": v4, "v6": v6, "first": first,
            })

        if write_rows:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH ()-[r]->() WHERE elementId(r) = row.rid
                SET r.l3_prefix      = row.first,
                    r.l3_prefix_v4   = row.v4,
                    r.l3_prefix_v6   = row.v6,
                    r.is_routed      = true,
                    r.l3_updated_at  = timestamp()
                """,
                rows=write_rows,
            )
            updated = len(write_rows)

        # ── Clear stale L3 stamps on cables that no longer carry any
        # routed prefix (e.g. interface re-purposed from no-switchport
        # to switchport, or peer device removed).  Touches only edges
        # that were previously decorated AND are NOT in this pass.
        stamped_rids = {row["rid"] for row in write_rows}
        if stamped_rids:
            await session.run(
                """
                MATCH ()-[r:PHYSICAL_LINK]->()
                WHERE r.is_routed = true
                  AND NOT elementId(r) IN $keep
                REMOVE r.l3_prefix, r.l3_prefix_v4, r.l3_prefix_v6,
                       r.is_routed, r.l3_updated_at
                """,
                keep=list(stamped_rids),
            )
        else:
            await session.run(
                """
                MATCH ()-[r:PHYSICAL_LINK]->()
                WHERE r.is_routed = true
                REMOVE r.l3_prefix, r.l3_prefix_v4, r.l3_prefix_v6,
                       r.is_routed, r.l3_updated_at
                """
            )

    if updated:
        log.info("correlate.links_decorated_l3", count=updated)
    return updated


async def _mark_absorbed_prefixes() -> int:
    """Stamp ``Prefix.absorbed=true`` on prefixes already folded into a
    visual element (a VLAN's label, or a PHYSICAL_LINK's ``l3_prefix``
    annotation), so the topology renderer can hide them.

    Without this, the UI shows duplicated information: e.g. a
    ``VLAN 14 · cpn-ful-ai1`` node whose label already includes
    ``192.133.164.0/24`` *and* a separate floating ``prefix:192.133.164.0/24``
    node with its own ROUTES_TO edges back to the same devices.

    A prefix is considered absorbed when ANY of the following are true:
      * Some VLAN ``v`` has ``v.has_prefix=true`` and the prefix's CIDR
        appears in ``v.prefix_v4`` or ``v.prefix_v6``.  The VLAN label
        already shows the CIDR, so the standalone Prefix node and its
        HAS_PREFIX / ROUTES_TO edges add no information.
      * Some PHYSICAL_LINK ``r`` has ``r.l3_prefix`` equal to the
        prefix's CIDR.  The cable already shows the subnet inline.

    We also clear the stamp on prefixes that USED to be absorbed but no
    longer are (e.g. a VLAN's SVI was removed), so the prefix can
    reappear as a standalone node next correlation pass.
    """
    driver = get_driver()
    marked = 0
    cleared = 0
    async with driver.session() as session:
        # Pass A — absorbed by a VLAN that already lists the CIDR.
        res_a = await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.has_prefix = true
            UNWIND coalesce(v.prefix_v4, []) + coalesce(v.prefix_v6, []) AS cidr
            WITH DISTINCT cidr, v
            MATCH (p:Prefix)
            WHERE coalesce(p.cidr, p.prefix) = cidr
            SET p.absorbed         = true,
                p.absorbed_by_kind = 'vlan',
                p.absorbed_by_id   = v.id,
                p.absorbed_at      = timestamp()
            RETURN count(DISTINCT p) AS n
            """
        )
        rec_a = await res_a.single()
        marked += int(rec_a["n"]) if rec_a else 0

        # Pass B — absorbed by a PHYSICAL_LINK that lists the CIDR
        # inline.  We only stamp if the prefix wasn't already absorbed
        # by a VLAN (VLAN takes precedence — the link is a transit, the
        # VLAN is the broadcast domain).
        #
        # A cable can absorb multiple prefixes (dual-stack /30 + /127,
        # parent of several sub-interfaces, etc.) so we union the
        # legacy singular ``l3_prefix`` with the v4/v6 lists added by
        # the rewritten decorator.
        res_b = await session.run(
            """
            MATCH ()-[r:PHYSICAL_LINK]->()
            WHERE r.l3_prefix IS NOT NULL
               OR (r.l3_prefix_v4 IS NOT NULL AND size(r.l3_prefix_v4) > 0)
               OR (r.l3_prefix_v6 IS NOT NULL AND size(r.l3_prefix_v6) > 0)
            WITH elementId(r) AS rid,
                 [x IN coalesce(r.l3_prefix_v4, []) WHERE x IS NOT NULL] AS v4,
                 [x IN coalesce(r.l3_prefix_v6, []) WHERE x IS NOT NULL] AS v6,
                 CASE WHEN r.l3_prefix IS NULL THEN []
                      ELSE [r.l3_prefix] END AS singleton
            UNWIND singleton + v4 + v6 AS cidr
            WITH DISTINCT rid, cidr
            MATCH (p:Prefix)
            WHERE coalesce(p.cidr, p.prefix) = cidr
              AND (p.absorbed IS NULL OR p.absorbed = false)
            SET p.absorbed         = true,
                p.absorbed_by_kind = 'physical_link',
                p.absorbed_by_id   = rid,
                p.absorbed_at      = timestamp()
            RETURN count(DISTINCT p) AS n
            """
        )
        rec_b = await res_b.single()
        marked += int(rec_b["n"]) if rec_b else 0

        # Clear stamps that no longer apply — e.g. the VLAN that had
        # this prefix was deleted, or the SVI moved.  We re-check the
        # same two conditions in the WHERE: if neither still holds, the
        # prefix is once again a standalone node.
        res_c = await session.run(
            """
            MATCH (p:Prefix)
            WHERE p.absorbed = true
            WITH p, coalesce(p.cidr, p.prefix) AS cidr
            WHERE NOT EXISTS {
                MATCH (v:VLAN)
                WHERE v.has_prefix = true
                  AND ( cidr IN coalesce(v.prefix_v4, [])
                     OR cidr IN coalesce(v.prefix_v6, []) )
              }
              AND NOT EXISTS {
                MATCH ()-[r:PHYSICAL_LINK]->()
                WHERE r.l3_prefix = cidr
                   OR cidr IN coalesce(r.l3_prefix_v4, [])
                   OR cidr IN coalesce(r.l3_prefix_v6, [])
              }
            SET p.absorbed         = false,
                p.absorbed_by_kind = null,
                p.absorbed_by_id   = null
            RETURN count(p) AS n
            """
        )
        rec_c = await res_c.single()
        cleared = int(rec_c["n"]) if rec_c else 0

    if marked or cleared:
        log.info("correlate.prefixes_absorbed",
                 marked=marked, cleared=cleared)
    return marked


async def _collapse_routing_peers() -> int:
    """Emit device-to-device ROUTING_PEER adjacency edges so the UI can
    render routing sessions as their own dashed lines (separate from the
    physical cable that may or may not carry them).

    Why this design (rewritten in 0.6.0-dev6)
    -----------------------------------------
    The previous implementation decorated ``PHYSICAL_LINK`` edges with a
    ``routing_protocols`` list and asked the UI to render the routing
    context inline on the cable.  This was confusing in practice:

      * Routing adjacencies don't always follow the cable topology
        (iBGP over loopbacks, OSPF over SVIs, eBGP over a transit
        link that crosses multiple cables).  Merging the two layers
        loses information about which session is up vs down.
      * Up/down state was buried; "this cable carries BGP" said
        nothing about whether the BGP session was actually
        established.
      * Operators want to see, e.g., the active BGP sessions across
        a site without having to read tiny labels on cables.

    The new model:

      * For every ``(Device A)-[:ROUTING_PEER]->(RoutingPeer{peer_ip=X})``
        stub edge from the adapter, resolve peer Device B by IP.
      * If resolved: MERGE a ``(A)-[:ROUTING_PEER]->(B)`` adjacency edge
        with full session metadata (protocol, local_ip, remote_ip,
        local_as, remote_as, address_family, state, oper_status,
        oper_status_changed_at).  The UI renders this as a dashed line.
      * If not resolved (transit / external peer): leave the
        ``(A)-[:ROUTING_PEER]->(RoutingPeer)`` stub edge alone — the
        RoutingPeer node and its dashed edge to the stub still render.
      * **Never** touch PHYSICAL_LINK or VLAN properties.  Old
        ``routing_protocols`` / ``routing_updated_at`` properties on
        PHYSICAL_LINK and VLAN nodes are scrubbed every cycle so a
        rolling upgrade doesn't leave stale decorations.

    Return value is the count of adjacency edges merged.
    """
    driver = get_driver()
    handled = 0

    # state → oper_status normalization (raw strings from SNMP MIBs).
    # 'up' means the session is in steady-state forwarding; everything
    # else collapses to 'down' so the UI's red/green coloring works.
    _UP_STATES = {"full", "established"}
    _DOWN_STATES = {
        "down", "init", "attempt", "exstart", "exchange", "loading",
        "2way", "idle", "active", "connect", "opensent", "openconfirm",
    }

    async with driver.session() as session:
        # ── Housekeeping: scrub the previous design's decorations ──────
        # PHYSICAL_LINK edges used to carry ``routing_protocols`` and
        # ``routing_updated_at``.  Now that routing lives on its own
        # ROUTING_PEER edges, those properties are noise that breaks
        # the UI's "the cable is a clean L1 object" assumption.
        # Same for VLAN nodes.  Cheap to run every cycle and idempotent.
        await session.run(
            """
            MATCH ()-[r:PHYSICAL_LINK]->()
            WHERE r.routing_protocols IS NOT NULL
               OR r.routing_updated_at IS NOT NULL
            REMOVE r.routing_protocols, r.routing_updated_at
            """
        )
        await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.routing_protocols IS NOT NULL
               OR v.routing_updated_at IS NOT NULL
            REMOVE v.routing_protocols, v.routing_updated_at
            """
        )
        # The old design also created ``ROUTES_OVER_UNKNOWN`` edges as a
        # "faded unknown-carrier arc" when no PHYSICAL_LINK was found.
        # The new model uses a device-to-device ROUTING_PEER instead,
        # so the ROUTES_OVER_UNKNOWN edges are now redundant.  Delete
        # them so the UI only sees the canonical adjacency.
        await session.run(
            """
            MATCH ()-[r:ROUTES_OVER_UNKNOWN]->()
            DELETE r
            """
        )

        # ── Walk every Device→RoutingPeer stub edge ────────────────────
        # NB: the stub edge direction is normalised in ingest so the
        # smaller node id is the source.  RoutingPeer stub ids start
        # with ``routing-peer:``, which sorts lexicographically below
        # ``snmp:…``, ``meraki:…``, etc., so the stored direction is
        # often actually ``(RoutingPeer)-[:ROUTING_PEER]->(Device)``.
        # The undirected ``-[]-`` match below covers both.
        #
        # We also pull the observer's ``local_asn`` (set by the WAN
        # correlator on devices that speak eBGP) so we can fill in the
        # *local* AS for the adjacency we're about to MERGE.  Without
        # this the hover would only ever show the remote AS, since
        # SNMP BGP4-MIB reports the neighbor's AS but not the local
        # router's.
        peer_rows = await (await session.run(
            """
            MATCH (a:Device)-[r:ROUTING_PEER]-(rp:RoutingPeer)
            WHERE rp.peer_ip IS NOT NULL
            RETURN a.id AS a_id,
                   a.local_asn AS observer_asn,
                   rp.peer_ip AS peer_ip,
                   coalesce(rp.protocol, r.protocol, 'unknown') AS proto,
                   coalesce(r.state, rp.state, '') AS state,
                   rp.remote_as AS remote_as,
                   rp.router_id AS router_id,
                   rp.id AS rp_id
            """
        )).data()

        for row in peer_rows:
            a_id = row["a_id"]
            observer_asn = row["observer_asn"]
            peer_ip = row["peer_ip"]
            proto = row["proto"]
            state_raw = (row["state"] or "").lower()
            remote_as = row["remote_as"]
            router_id = row["router_id"]
            rp_id = row["rp_id"]

            # Derive oper_status from state. 'up' iff in steady-state
            # forwarding (BGP established, OSPF full).
            if state_raw in _UP_STATES:
                oper_status = "up"
            elif state_raw in _DOWN_STATES or state_raw:
                oper_status = "down"
            else:
                oper_status = "unknown"

            # Derive address_family from the peer IP. Note: SNMP BGP4-MIB
            # / OSPF-MIB walks index by IPv4 today; IPv6-MIB extensions
            # exist but aren't currently polled.  Still, parse defensively
            # so this works if the adapter gains IPv6 support later.
            try:
                ip_obj = ipaddress.ip_address(peer_ip)
                afi = "ipv6" if ip_obj.version == 6 else "ipv4"
            except (ValueError, TypeError):
                afi = "unknown"

            # If the source device A is itself a CDP/LLDP stub that has
            # been canonicalised, redirect to the canonical id so the
            # new adjacency lands on the real device.  We also bring in
            # the canonical device's ``local_asn`` so the local AS is
            # accurate post-redirect.
            res_a_can = await session.run(
                """
                MATCH (a:Device {id: $a_id})
                OPTIONAL MATCH (canon:Device {id: a.canonical_id})
                RETURN coalesce(a.canonical_id, a.id) AS canonical_id,
                       coalesce(canon.local_asn, a.local_asn) AS local_asn
                """,
                a_id=a_id,
            )
            a_can_rec = await res_a_can.single()
            if a_can_rec:
                a_id = a_can_rec["canonical_id"] or a_id
                # Prefer the (possibly canonicalised) device's local_asn
                # over the stub's, if known.
                if a_can_rec["local_asn"] is not None:
                    observer_asn = a_can_rec["local_asn"]

            # Resolve peer Device by IP (interface IP, mgmt_ip, or
            # candidate_ips), preferring canonical (non-stub) devices
            # over CDP/LLDP stubs. The ORDER BY puts:
            #   - non-stub, no canonical pointer → first (true canonical)
            #   - stub Devices (id starts with cdp-/lldp-neighbor:) last
            # so we never bind a routing adjacency to a stub when a real
            # device for the same IP exists.  Also returns the resolved
            # B's local_asn so we can fill in B's AS as the remote AS
            # when the observer is acting as the local side.
            res_b = await session.run(
                """
                MATCH (b:Device)
                WHERE (
                    EXISTS {
                        (b)-[:HAS_INTERFACE]->(:Interface)
                          -[:ASSIGNED_IP]->(:IPAddress {address: $peer_ip})
                    }
                    OR b.mgmt_ip = $peer_ip
                    OR ($peer_ip IN coalesce(b.candidate_ips, []))
                )
                AND b.id <> $a_id
                WITH b,
                     CASE WHEN b.canonical_id IS NOT NULL THEN 1 ELSE 0 END AS is_stub_target,
                     CASE WHEN b.id STARTS WITH 'cdp-neighbor:'
                            OR b.id STARTS WITH 'lldp-neighbor:' THEN 1 ELSE 0 END AS is_stub_id
                ORDER BY is_stub_id ASC, is_stub_target ASC
                OPTIONAL MATCH (canon_b:Device {id: b.canonical_id})
                RETURN coalesce(b.canonical_id, b.id) AS b_id,
                       coalesce(canon_b.local_asn, b.local_asn) AS b_local_asn
                LIMIT 1
                """,
                a_id=a_id, peer_ip=peer_ip,
            )
            b_rec = await res_b.single()
            if not b_rec:
                # External / transit peer — render the Device→RoutingPeer
                # stub edge as-is. Nothing to do here, the stub already
                # exists.
                continue
            b_id = b_rec["b_id"]
            b_local_asn = b_rec["b_local_asn"]
            # Guard against self-loop after canonical redirection.
            if b_id == a_id:
                continue
            # If we don't have the stub's remote_as (BGP4-MIB didn't
            # report it for the observer), fall back to B's own
            # local_asn from the WAN correlator. This is exact when B
            # itself was polled and its local AS was recorded.
            if remote_as is None and b_local_asn is not None:
                remote_as = b_local_asn

            # Find Device A's local IP that sits on the same subnet as
            # peer_ip (best heuristic for "which interface is hosting
            # this session"). Falls back to A.mgmt_ip if no shared subnet
            # is found.  For routing-peer rendering this is what the
            # operator wants to see in the hover ("session between
            # 10.0.0.1 and 10.0.0.2").
            res_local = await session.run(
                """
                MATCH (a:Device {id: $a_id})-[:HAS_INTERFACE]
                      ->(:Interface)-[:ASSIGNED_IP]->(ip:IPAddress)
                WHERE ip.address IS NOT NULL
                RETURN ip.address AS ip LIMIT 50
                """,
                a_id=a_id,
            )
            local_ip = None
            a_ips = [r["ip"] async for r in res_local]
            try:
                peer_net_v4 = ipaddress.ip_network(f"{peer_ip}/24", strict=False)
                peer_net_v6 = (
                    ipaddress.ip_network(f"{peer_ip}/64", strict=False)
                    if afi == "ipv6" else None
                )
            except (ValueError, TypeError):
                peer_net_v4 = None
                peer_net_v6 = None
            for ip_str in a_ips:
                try:
                    ip = ipaddress.ip_address(ip_str)
                except (ValueError, TypeError):
                    continue
                if afi == "ipv4" and peer_net_v4 and ip.version == 4 and ip in peer_net_v4:
                    local_ip = ip_str
                    break
                if afi == "ipv6" and peer_net_v6 and ip.version == 6 and ip in peer_net_v6:
                    local_ip = ip_str
                    break
            # Fallback to mgmt IP if no subnet match.
            if local_ip is None:
                res_mgmt = await session.run(
                    "MATCH (a:Device {id: $a_id}) RETURN a.mgmt_ip AS mgmt",
                    a_id=a_id,
                )
                mgmt_rec = await res_mgmt.single()
                if mgmt_rec and mgmt_rec["mgmt"]:
                    local_ip = mgmt_rec["mgmt"]

            # Canonicalize direction (lex-smaller id is the source) so
            # bidirectional reports (A polled B, B polled A) collapse
            # onto the same edge.  ``local_*`` and ``remote_*`` are
            # always relative to the canonical src.
            #
            # Observer A polls neighbor B:
            #   * observer's IP → src_ip   (if A==src)  or dst_ip  (if A==dst)
            #   * observer's AS → src_as   ditto       or dst_as
            #   * peer_ip       → dst_ip   ditto       or src_ip
            #   * stub remote_as → dst_as  ditto       or src_as
            # So whichever direction we're processing, *one side*'s AS
            # comes from observer's local_asn and the *other side*'s
            # comes from the stub's remote_as.  Both directions fill in
            # complementary fields, so the second cycle completes the
            # picture.
            if a_id < b_id:
                src_id, dst_id = a_id, b_id
                src_ip, dst_ip = local_ip, peer_ip
                src_as, dst_as = observer_asn, remote_as
            else:
                src_id, dst_id = b_id, a_id
                src_ip, dst_ip = peer_ip, local_ip
                src_as, dst_as = remote_as, observer_asn

            # MERGE the adjacency.  Key on (src_id, dst_id, protocol,
            # address_family, local_ip, remote_ip) — all six are
            # invariant across both poll directions (canonical IPs are
            # determined by the device-id ordering, not the observer),
            # so symmetric observations converge on a single edge.
            #
            # Use ``coalesce`` on local_as/remote_as so a second
            # observation that contributes only one AS doesn't wipe out
            # the AS the previous observation already filled in.
            #
            # Transition detection: stamp ``oper_status_changed_at`` only
            # when oper_status actually changes value, so the UI can
            # render "up for 3d" / "down since 12m ago".
            await session.run(
                """
                MATCH (src:Device {id: $src_id}), (dst:Device {id: $dst_id})
                MERGE (src)-[r:ROUTING_PEER {
                    protocol:       $proto,
                    address_family: $afi,
                    local_ip:       $src_ip,
                    remote_ip:      $dst_ip
                }]->(dst)
                ON CREATE SET
                    r.first_seen             = timestamp(),
                    r.oper_status_changed_at = timestamp()
                WITH r, r.oper_status AS prev
                SET r.local_as              = coalesce($src_as, r.local_as),
                    r.remote_as             = coalesce($dst_as, r.remote_as),
                    r.state                 = $state,
                    r.oper_status           = $oper_status,
                    r.last_seen             = timestamp(),
                    r.source                = 'correlated',
                    r.source_adapter        = 'correlator',
                    r.peer_node_id          = coalesce(r.peer_node_id, $rp_id),
                    r.router_id             = coalesce(r.router_id, $router_id)
                FOREACH (_ IN CASE
                         WHEN prev IS NOT NULL AND prev <> $oper_status
                         THEN [1] ELSE [] END |
                    SET r.oper_status_changed_at = timestamp())
                """,
                src_id=src_id, dst_id=dst_id,
                proto=proto, afi=afi,
                src_ip=src_ip, dst_ip=dst_ip,
                src_as=src_as, dst_as=dst_as,
                state=state_raw, oper_status=oper_status,
                rp_id=rp_id, router_id=router_id,
            )
            handled += 1

        # ── Tail purge ─────────────────────────────────────────────────
        # 1. Drop device-to-device ROUTING_PEER edges that point at
        #    devices which were stubs and have since been canonicalised
        #    (their canonical_id is now non-null, so the edge is
        #    shadowed by an equivalent edge on the canonical id).
        await session.run(
            """
            MATCH (a:Device)-[r:ROUTING_PEER]->(b:Device)
            WHERE r.source = 'correlated'
              AND (
                a.canonical_id IS NOT NULL
                OR b.canonical_id IS NOT NULL
              )
            DELETE r
            """
        )
        # 2. Drop device-to-device ROUTING_PEER edges whose underlying
        #    adapter stub (RoutingPeer with same peer_ip+protocol)
        #    disappeared, so we don't keep stale adjacencies forever
        #    when an adapter stops reporting a session.
        await session.run(
            """
            MATCH (a:Device)-[r:ROUTING_PEER]->(b:Device)
            WHERE r.source = 'correlated'
              AND NOT EXISTS {
                MATCH (:Device)-[:ROUTING_PEER]-(rp:RoutingPeer)
                WHERE rp.peer_ip = r.remote_ip
                  AND coalesce(rp.protocol, '') = coalesce(r.protocol, '')
              }
              AND NOT EXISTS {
                MATCH (:Device)-[:ROUTING_PEER]-(rp:RoutingPeer)
                WHERE rp.peer_ip = r.local_ip
                  AND coalesce(rp.protocol, '') = coalesce(r.protocol, '')
              }
            DELETE r
            """
        )

    if handled:
        log.info("correlate.routing_adjacencies_emitted", count=handled)
    return handled


async def _merge_neighbor_stubs_by_name() -> int:
    """Merge LLDP/CDP stub Device nodes into their real platform-discovered
    counterparts, matched by hostname (case-insensitive, trailing DNS domain
    stripped).

    Background
    ----------
    When SNMP polling sees an LLDP/CDP neighbor on a switch port, the
    adapter has nothing to identify the remote device with except its
    chassis ID/sysName.  We materialize that as a stub Device with id
    ``lldp-neighbor:<name>`` or ``cdp-neighbor:<name>`` so the
    PHYSICAL_LINK edge has a valid target.  When another adapter
    (Meraki, CatC, etc.) already knows about the same device under a
    real id (e.g. ``meraki:Q5TY-EB22-LPZG``), the stub becomes a
    duplicate inventory entry.  This pass redirects every edge from the
    stub onto the real device, then deletes the stub.

    Matching rule
    -------------
    A stub merges into a real Device if both:
      1. Their normalized names match (lowercased; the first DNS label
         only, so ``cpn-ful-cat9k1.ciscops.net`` and ``cpn-ful-cat9k1``
         are the same).
      2. The real Device has ``stub`` IS NULL or false.

    The name match is intentionally strict (no fuzzy / Levenshtein) to
    avoid collapsing distinct devices that happen to share a prefix.
    """
    driver = get_driver()
    merged = 0
    async with driver.session() as session:
        # LLDP/CDP stubs only appear as the *target* of PHYSICAL_LINK edges
        # (they're materialized when an SNMP poll observes a neighbor we
        # don't already know about). So we only need to redirect inbound
        # PHYSICAL_LINK edges to the real device, then delete the stub.
        # No APOC required — relationship type is hardcoded.
        # Step 1: find stub→real pairs (case-insensitive hostname match,
        # short-name only so 'cpn-ful-cat9k1' matches 'cpn-ful-cat9k1.example').
        # Two-pass match — both clauses produce a (stub, real) pair:
        #
        #   (a) Stub short-name matches the real Device's primary
        #       ``name`` (the historical behaviour).
        #   (b) Stub short-name matches any string an adapter has
        #       published in ``candidate_names`` on the real Device.
        #       Intersight uses this to surface ``SwitchId`` (``A``/
        #       ``B``), the DN, and the friendly ``Name`` so an FI's
        #       LLDP sysName — whatever firmware decides to advertise
        #       — can resolve to the canonical FI node without going
        #       through NetBox.
        pairs_res = await session.run(
            """
            CALL {
                MATCH (stub:Device)
                WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                       OR stub.id STARTS WITH 'cdp-neighbor:')
                  AND stub.name IS NOT NULL AND stub.name <> ''
                WITH stub,
                     toLower(split(stub.name, '.')[0]) AS sname
                MATCH (real:Device)
                WHERE real.id <> stub.id
                  AND NOT (real.id STARTS WITH 'lldp-neighbor:'
                           OR real.id STARTS WITH 'cdp-neighbor:')
                  AND toLower(split(coalesce(real.name, ''), '.')[0]) = sname
                RETURN stub.id AS stub_id, real.id AS real_id
            UNION
                MATCH (stub:Device)
                WHERE (stub.id STARTS WITH 'lldp-neighbor:'
                       OR stub.id STARTS WITH 'cdp-neighbor:')
                  AND stub.name IS NOT NULL AND stub.name <> ''
                WITH stub,
                     toLower(split(stub.name, '.')[0]) AS sname
                MATCH (real:Device)
                WHERE real.id <> stub.id
                  AND NOT (real.id STARTS WITH 'lldp-neighbor:'
                           OR real.id STARTS WITH 'cdp-neighbor:')
                  AND real.candidate_names IS NOT NULL
                  AND any(c IN real.candidate_names
                           WHERE toLower(split(c, '.')[0]) = sname)
                RETURN stub.id AS stub_id, real.id AS real_id
            }
            RETURN DISTINCT stub_id, real_id
            """
        )
        pairs: list[tuple[str, str]] = []
        async for rec in pairs_res:
            pairs.append((rec["stub_id"], rec["real_id"]))

        # Step 2: per-pair, delegate to the shared `_absorb_stub_into_real`
        # helper.  This intentionally replaces the previous in-place
        # MERGE+DELETE Cypher so name-merged stubs benefit from the same
        # ``source_adapter = 'correlator'`` re-tag and tombstoned-stub
        # caching as the chassis_mac and mgmt_ip merge passes — without
        # those, name-merged cables would still get wiped by the next
        # SNMP ingest's per-adapter purge.
        merged = 0
        for stub_id, real_id in pairs:
            await _absorb_stub_into_real(session, stub_id, real_id)
            merged += 1

        # Step 3: collapse multiple stubs that share a hostname (e.g.
        # `lldp-neighbor:foo` and `cdp-neighbor:foo` for the same physical
        # device). Without this, the inventory shows duplicate "via lldp"
        # and "via cdp" rows for the same node. We pick a canonical stub
        # per name — preferring the one with the richest metadata (a non-
        # null discovered_via), then deterministically by id — and merge
        # the rest into it.
        stub_pairs_res = await session.run(
            """
            MATCH (s:Device)
            WHERE (s.id STARTS WITH 'lldp-neighbor:'
                   OR s.id STARTS WITH 'cdp-neighbor:')
              AND coalesce(s.stub, false) = true
              AND s.name IS NOT NULL AND s.name <> ''
            WITH toLower(split(s.name, '.')[0]) AS sname,
                 collect(s) AS stubs
            WHERE size(stubs) > 1
            RETURN sname, stubs
            """
        )
        stub_groups: list[tuple[str, list[dict]]] = []
        async for rec in stub_pairs_res:
            stubs_list: list[dict] = []
            for st in rec["stubs"]:
                stubs_list.append({
                    "id": st["id"],
                    "discovered_via": st.get("discovered_via"),
                })
            stub_groups.append((rec["sname"], stubs_list))

        stub_stub_merged = 0
        for _sname, stubs_list in stub_groups:
            # Prefer the stub with richer metadata; ties broken by id.
            stubs_list.sort(
                key=lambda s: (
                    0 if s.get("discovered_via") else 1,
                    s["id"],
                ),
            )
            canonical = stubs_list[0]["id"]
            losers = [s["id"] for s in stubs_list[1:]]
            for loser_id in losers:
                # Stub-to-stub merge: same shared helper as Step 2.
                # The winner stub stays canonical (no canonical_id on it
                # because the winner has no further canonical to point
                # at), and the loser stubs get their edges redirected
                # AND get tombstoned with canonical_id pointing at the
                # winner — so the next SNMP cycle's re-emitted loser
                # edges absorb idempotently into the winner.
                await _absorb_stub_into_real(session, loser_id, canonical)
                stub_stub_merged += 1
    if merged or stub_stub_merged:
        log.info("correlate.stubs_merged",
                 count=merged, stub_to_stub_merged=stub_stub_merged)
    return merged + stub_stub_merged


async def _enrich_physical_links_with_health() -> int:
    """Push per-interface health metrics onto PHYSICAL_LINK edges.

    For each PHYSICAL_LINK edge that has at least one endpoint Interface
    with a `health_score`, copy:
      - util_pct (max of the two sides)
      - error_rate_per_s (sum of the two sides)
      - health_score (max of the two sides)
      - single_sided (true if only one endpoint reports health)
      - oper_status (derived: "down" if any side is down, else "up")
      - oper_status_changed_at (stamped only on transition)
    onto the edge so the UI can color/thicken it AND show "down since X"
    without joining nodes.

    Scoping
    -------
    Health is computed from the **specific interface** each side of the
    cable terminates on, identified by ``link.interface_a`` /
    ``link.interface_b`` (or their ``_active`` siblings for Cisco
    multi-rate SFP cages where the anchor name points at the inactive
    sibling).  Earlier versions joined *every* HAS_INTERFACE on each
    device, which meant a single unused / down port anywhere on a
    chassis caused EVERY cable on that chassis to inherit
    ``oper_status='down'`` and ``health_score=80`` — i.e. the entire
    switch's topology lit up red even though all uplinks were fine.
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (a:Device)-[link:PHYSICAL_LINK]->(b:Device)
            // Resolve each side's specific interface (prefer the
            // active sibling Cisco multi-rate SFP cages may have set,
            // fall back to the anchor name).  If neither field is on
            // the link, skip — we can't safely scope without it.
            WITH a, b, link,
                 coalesce(link.interface_a_active, link.interface_a) AS ia_name,
                 coalesce(link.interface_b_active, link.interface_b) AS ib_name
            WHERE ia_name IS NOT NULL OR ib_name IS NOT NULL
            OPTIONAL MATCH (a)-[:HAS_INTERFACE]->(ia:Interface)
              WHERE ia_name IS NOT NULL
                AND (ia.name = ia_name OR ia.canonical_name = ia_name)
            OPTIONAL MATCH (b)-[:HAS_INTERFACE]->(ib:Interface)
              WHERE ib_name IS NOT NULL
                AND (ib.name = ib_name OR ib.canonical_name = ib_name)
            WITH link, ia, ib,
                 // Per-side reporting flags: true iff that side's
                 // specific interface exists AND has any health datum
                 // we can use.  ``oper_status`` alone counts as
                 // "reporting" — many adapters set oper without
                 // computing a health_score.
                 (ia IS NOT NULL AND
                    (ia.health_score IS NOT NULL OR ia.oper_status IS NOT NULL)) AS a_reports,
                 (ib IS NOT NULL AND
                    (ib.health_score IS NOT NULL OR ib.oper_status IS NOT NULL)) AS b_reports
            WHERE a_reports OR b_reports
            WITH link, ia, ib, a_reports, b_reports,
                 // Per-side oper_status (lowercased, '' when absent).
                 toLower(coalesce(ia.oper_status, '')) AS a_oper,
                 toLower(coalesce(ib.oper_status, '')) AS b_oper,
                 // Per-side health metrics, defaulting to 0 where absent
                 // so MAX/SUM aggregations work without NULL contamination.
                 coalesce(ia.health_score, 0) AS a_hs,
                 coalesce(ib.health_score, 0) AS b_hs,
                 coalesce(ia.util_in_pct, 0) AS a_util_in,
                 coalesce(ia.util_out_pct, 0) AS a_util_out,
                 coalesce(ib.util_in_pct, 0) AS b_util_in,
                 coalesce(ib.util_out_pct, 0) AS b_util_out,
                 coalesce(ia.error_rate_in_per_s, 0)
                   + coalesce(ia.error_rate_out_per_s, 0) AS a_err,
                 coalesce(ib.error_rate_in_per_s, 0)
                   + coalesce(ib.error_rate_out_per_s, 0) AS b_err
            WITH link, a_reports, b_reports,
                 CASE WHEN a_hs > b_hs THEN a_hs ELSE b_hs END AS hscore,
                 CASE WHEN a_util_in > b_util_in THEN a_util_in ELSE b_util_in END AS util_in,
                 CASE WHEN a_util_out > b_util_out THEN a_util_out ELSE b_util_out END AS util_out,
                 a_err + b_err AS err,
                 // Derived link state.  Only count a side's report if
                 // that side actually has an interface match — otherwise
                 // an empty '' from coalesce would be treated as a
                 // signal.  "down" iff any reporting side is down;
                 // "up" iff any reporting side is up; NULL when neither
                 // side has live oper_status.
                 CASE
                   WHEN (a_reports AND a_oper = 'down')
                     OR (b_reports AND b_oper = 'down') THEN 'down'
                   WHEN (a_reports AND a_oper = 'up')
                     OR (b_reports AND b_oper = 'up')   THEN 'up'
                   ELSE NULL
                 END AS new_oper,
                 link.oper_status AS prev_oper
            SET link.health_score      = hscore,
                link.util_in_pct       = util_in,
                link.util_out_pct      = util_out,
                link.util_pct          = CASE WHEN util_in > util_out THEN util_in ELSE util_out END,
                link.error_rate_per_s  = err,
                link.single_sided      = NOT (a_reports AND b_reports),
                link.oper_status       = new_oper
            // Stamp the transition timestamp only on an actual change
            // (including null→value).  Uses Neo4j's native timestamp()
            // so this stays atomic with the SET.
            FOREACH (_ IN CASE
                     WHEN coalesce(new_oper,'') <> coalesce(prev_oper,'')
                      AND new_oper IS NOT NULL
                     THEN [1] ELSE [] END |
              SET link.oper_status_changed_at = timestamp())
            RETURN count(*) AS n
            """
        )
        rec = await result.single()
        return rec["n"] if rec else 0


async def _correlate_via_mac() -> int:
    """Create PHYSICAL_LINK edges where a switch learned a device's own MAC.

    LLDP/CDP/native-topology edges always take precedence: if any
    PHYSICAL_LINK already exists between the two devices in either
    direction with a high-confidence discovery protocol, we skip the
    MAC-based inference. This prevents the "two parallel links between
    cat9k1 and n9k1" duplication the user reported.

    We also DELETE any pre-existing ``mac_correlation`` edge between a
    pair that has since acquired a high-confidence edge.  Without this
    cleanup, a mac_correlation edge created in an early cycle (when no
    LLDP edge existed yet) persists for one full cycle until
    ``_dedupe_physical_links_by_pair`` collapses it — long enough for an
    operator screenshot to capture the transient duplicate.  Doing the
    cleanup here keeps the graph self-consistent at every read point.

    Pattern:
        (switch:Device)-[:HAS_INTERFACE]->(iface:Interface)
                        -[:LEARNED_MAC]->(mac:MACAddress)
                        <-[:OWNS_MAC]-(endpoint:Device)
    """
    driver = get_driver()
    async with driver.session() as session:
        # Cleanup: drop any mac_correlation edge whose pair now also has
        # a higher-confidence discovery_proto.  Run BEFORE the MERGE so
        # the same call always converges in one pass.
        await session.run(
            """
            MATCH (a)-[mac_edge:PHYSICAL_LINK {discovery_proto: 'mac_correlation'}]-(b)
            WHERE EXISTS {
                MATCH (a)-[hc:PHYSICAL_LINK]-(b)
                WHERE hc.discovery_proto IN [
                    'lldp', 'cdp', 'catc_topology', 'meraki_topology',
                    'ndfc_topology', 'intersight'
                ]
            }
            DELETE mac_edge
            """
        )

        result = await session.run(
            """
            MATCH (switch:Device)-[:HAS_INTERFACE]->(iface:Interface)
                  -[:LEARNED_MAC]->(mac:MACAddress)
                  <-[:OWNS_MAC]-(endpoint:Device)
            WHERE switch <> endpoint
              AND NOT EXISTS {
                MATCH (switch)-[ex:PHYSICAL_LINK]-(endpoint)
                WHERE ex.discovery_proto IN [
                    'lldp', 'cdp', 'catc_topology', 'meraki_topology',
                    'ndfc_topology', 'intersight'
                ]
              }
            // Canonicalize direction (source.id < target.id) so MERGE
            // collides with any adapter-written edge for the same pair
            // instead of stacking up a reverse-direction duplicate.
            WITH switch, endpoint, iface, mac,
                 CASE WHEN switch.id < endpoint.id THEN switch ELSE endpoint END AS src,
                 CASE WHEN switch.id < endpoint.id THEN endpoint ELSE switch END AS dst
            MERGE (src)-[link:PHYSICAL_LINK {
                source: 'correlated',
                discovery_proto: 'mac_correlation'
            }]->(dst)
            ON CREATE SET
                link.confidence    = 0.85,
                link.interface_a   = iface.name,
                link.via_mac       = mac.mac,
                link.created_at    = timestamp()
            ON MATCH SET
                link.confidence    = 0.85,
                link.interface_a   = iface.name,
                link.via_mac       = mac.mac,
                link.updated_at    = timestamp()
            RETURN count(link) AS created
            """
        )
        rec = await result.single()
        count = rec["created"] if rec else 0
        log.debug("correlate.mac_done", links=count)
        return count


async def _correlate_via_arp() -> int:
    """Create PHYSICAL_LINK edges where an ARP entry resolves to a known device IP.

    LLDP/CDP/native-topology edges take precedence. We also skip pairs
    that already have a MAC-correlated link, because MAC correlation is
    higher confidence than ARP (a learned MAC means the endpoint actually
    sent a frame through that port, whereas ARP resolution can happen
    across multiple hops via SVIs).

    As with ``_correlate_via_mac``, we DELETE any stale arp_correlation
    edge whose pair has since acquired a higher-confidence edge.

    Pattern:
        (switch_iface:Interface)-[:HAS_ARP]->(arp:ARPEntry)
        AND
        (endpoint_iface:Interface)-[:ASSIGNED_IP]->(ip:IPAddress {address: arp.ip})
        AND
        (switch:Device)-[:HAS_INTERFACE]->(switch_iface)
        AND
        (endpoint:Device)-[:HAS_INTERFACE]->(endpoint_iface)
    """
    driver = get_driver()
    async with driver.session() as session:
        # Cleanup stale arp_correlation edges where a better proto now exists.
        await session.run(
            """
            MATCH (a)-[arp_edge:PHYSICAL_LINK {discovery_proto: 'arp_correlation'}]-(b)
            WHERE EXISTS {
                MATCH (a)-[hc:PHYSICAL_LINK]-(b)
                WHERE hc.discovery_proto IN [
                    'lldp', 'cdp', 'catc_topology', 'meraki_topology',
                    'ndfc_topology', 'intersight', 'mac_correlation'
                ]
            }
            DELETE arp_edge
            """
        )

        result = await session.run(
            """
            MATCH (switch_iface:Interface)-[:HAS_ARP]->(arp:ARPEntry)
            MATCH (endpoint_iface:Interface)-[:ASSIGNED_IP]->(:IPAddress {address: arp.ip})
            MATCH (switch:Device)-[:HAS_INTERFACE]->(switch_iface)
            MATCH (endpoint:Device)-[:HAS_INTERFACE]->(endpoint_iface)
            WHERE switch <> endpoint
              AND NOT EXISTS {
                MATCH (switch)-[ex:PHYSICAL_LINK]-(endpoint)
                WHERE ex.discovery_proto IN [
                    'lldp', 'cdp', 'catc_topology', 'meraki_topology',
                    'ndfc_topology', 'intersight', 'mac_correlation'
                ]
              }
            WITH switch, endpoint, switch_iface, arp,
                 CASE WHEN switch.id < endpoint.id THEN switch ELSE endpoint END AS src,
                 CASE WHEN switch.id < endpoint.id THEN endpoint ELSE switch END AS dst
            MERGE (src)-[link:PHYSICAL_LINK {
                source: 'correlated',
                discovery_proto: 'arp_correlation'
            }]->(dst)
            ON CREATE SET
                link.confidence    = 0.65,
                link.interface_a   = switch_iface.name,
                link.via_arp_ip    = arp.ip,
                link.via_arp_mac   = arp.mac,
                link.created_at    = timestamp()
            ON MATCH SET
                link.confidence    = 0.65,
                link.updated_at    = timestamp()
            RETURN count(link) AS created
            """
        )
        rec = await result.single()
        count = rec["created"] if rec else 0
        log.debug("correlate.arp_done", links=count)
        return count


# Discovery-protocol precedence (higher = more authoritative).
# Used by _dedupe_physical_links_by_pair to pick the winner per pair.
_PROTO_PRIORITY: dict[str, int] = {
    "lldp":             100,
    "cdp":               95,
    "meraki_topology":   90,
    "ndfc_topology":     90,
    "catc_topology":     85,
    # ``intersight`` is a vendor-authoritative cabling source for the
    # UCS world (``ether/HostPorts.AcknowledgedPeerInterface``).  It is
    # ranked between native fabric topologies and MAC inference because
    # the FI peer side comes from Intersight's own port acknowledgement
    # rather than from a wire-side observation.
    "intersight":        80,
    "mac_correlation":   50,
    "arp_correlation":   30,
    # any other / NULL → 0
}


async def _dedupe_physical_links_by_pair() -> int:
    """Collapse REDUNDANT PHYSICAL_LINK edges between the same pair of
    devices, while preserving genuinely-parallel cables.

    The new rule (post multi-edge schema):
      1. Group all edges by undirected pair (a, b) with a.id < b.id.
      2. If any LLDP/CDP/native-topology edge exists for the pair,
         drop every mac/arp/lower-priority edge for that same pair
         (the parallel-link information already comes from the
         high-priority discovery protocol).
      3. Within the remaining edges, sub-group by the canonical
         interface pair (lex-sorted (interface_a, interface_b)) so
         parallel cables on distinct ports survive. Per sub-group,
         keep the highest-priority edge and delete the rest.
      4. Tiebreaker prefers canonical direction (start.id < end.id) so
         the dedupe is stable across cycles.

    Without rule 3, a switch with three cables to the same neighbor
    would surface as a single line in the explorer instead of three.
    """
    driver = get_driver()
    deleted = 0
    async with driver.session() as session:
        # Pull all PHYSICAL_LINK rows along with the interface pair so we
        # can group parallel cables correctly. The per-pair set is small
        # in practice (a few protos × a few cables) so doing the sort in
        # Python keeps the Cypher legible.
        result = await session.run(
            """
            MATCH (a:Device)-[r:PHYSICAL_LINK]-(b:Device)
            WHERE a.id < b.id
            RETURN a.id AS a_id, b.id AS b_id,
                   id(r) AS rid,
                   r.discovery_proto AS proto,
                   r.confidence AS conf,
                   coalesce(r.interface_a, '') AS iface_a,
                   coalesce(r.interface_b, '') AS iface_b,
                   startNode(r).id AS start_id,
                   endNode(r).id AS end_id
            """
        )
        records = await result.data()

    # Group by undirected pair
    pair_groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        pair_groups.setdefault((rec["a_id"], rec["b_id"]), []).append(rec)

    # High-priority discovery protocols: their presence on a pair makes
    # any inferred mac/arp edge redundant for the SAME pair (the
    # interface info is already richer).
    _HIGH_CONF_PROTOS = {
        "lldp", "cdp", "catc_topology", "meraki_topology", "ndfc_topology",
        "intersight",
    }
    _INFERRED_PROTOS = {"mac_correlation", "arp_correlation"}

    rids_to_delete: list[int] = []
    inferred_dropped = 0
    sub_dropped = 0
    shared_iface_dropped = 0
    for edges in pair_groups.values():
        if len(edges) <= 1:
            continue

        # Rule 2: if any high-confidence edge exists, drop inferred ones.
        has_high_conf = any(
            (e.get("proto") or "") in _HIGH_CONF_PROTOS for e in edges
        )
        if has_high_conf:
            survivors = [
                e for e in edges
                if (e.get("proto") or "") not in _INFERRED_PROTOS
            ]
            for e in edges:
                if (e.get("proto") or "") in _INFERRED_PROTOS:
                    rids_to_delete.append(e["rid"])
                    inferred_dropped += 1
            edges = survivors
        if len(edges) <= 1:
            continue

        # Rule 3: sub-group by canonical interface pair so parallel
        # cables on distinct (interface_a, interface_b) tuples each
        # survive. Within a sub-group, pick the highest-priority edge.
        sub_groups: dict[tuple[str, str], list[dict]] = {}
        for e in edges:
            ia = e.get("iface_a") or ""
            ib = e.get("iface_b") or ""
            # Treat (ia, ib) as undirected — two adapters may have
            # reported the same cable with sides swapped.
            key = tuple(sorted((ia, ib)))
            sub_groups.setdefault(key, []).append(e)

        for sub in sub_groups.values():
            if len(sub) <= 1:
                continue
            sub.sort(
                key=lambda e: (
                    _PROTO_PRIORITY.get(e.get("proto") or "", 0),
                    float(e.get("conf") or 0.0),
                    1 if (e.get("start_id") or "") < (e.get("end_id") or "") else 0,
                ),
                reverse=True,
            )
            for losing in sub[1:]:
                rids_to_delete.append(losing["rid"])
                sub_dropped += 1

        # Rule 4: collapse cross-adapter duplicates that survived Rule 3.
        #
        # Different adapters often spell the SAME physical port using
        # different conventions — e.g. on cat9k1↔n9k1 the SNMP/CDP
        # walk reports the cat9k1 side as ``TwentyFiveGigE1/1/5`` while
        # the Meraki adapter reports it as ``Port 1::C9300x-NM-8Y::5``.
        # Both also report the n9k1 side as ``Ethernet1/46``.  Rule 3
        # treats those as different sub-groups (different sorted
        # interface-pair tuples) so neither edge gets dropped, and the
        # graph ends up with two PHYSICAL_LINKs for what is one cable.
        #
        # The physical invariant is simple: a switch port has exactly
        # one cable terminating in it.  So if two surviving edges on
        # the same undirected (a,b) pair share at least one non-empty
        # interface name, they describe the same cable; keep the
        # highest-priority one.  We treat the names as undirected
        # (each non-empty name from each edge becomes a "label", and
        # any shared label glues two edges together — union-find).
        surviving = [
            e for e in edges
            if e["rid"] not in set(rids_to_delete)
        ]
        if len(surviving) <= 1:
            continue

        # Union-find over the surviving edges keyed by interface labels.
        parent: dict[int, int] = {i: i for i in range(len(surviving))}

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(x: int, y: int) -> None:
            rx, ry = _find(x), _find(y)
            if rx != ry:
                parent[rx] = ry

        # Compare labels under ``normalize_ifname`` so two adapters
        # that report the same physical port using different naming
        # conventions ("Te1/0/24" vs "TenGigabitEthernet1/0/24" vs
        # "Gi1/0/1" vs "GigabitEthernet1/0/1") still glue their edges
        # into the same union-find component.  Without this the
        # ``_normalize_physical_link_interfaces`` rewrite that runs
        # AFTER dedup leaves brief windows where the DB holds the
        # un-normalized form, and an adapter cycle in between can
        # MERGE a fresh edge under the canonical key while the old
        # edge sticks around under the short-form key — a duplicate
        # cable that no later dedup pass can collapse because the
        # raw labels share no characters.
        from netcortex.util.ifname import normalize_ifname
        label_to_idx: dict[str, int] = {}
        for idx, e in enumerate(surviving):
            for raw in (e.get("iface_a") or "", e.get("iface_b") or ""):
                if not raw:
                    continue
                label = normalize_ifname(raw)
                if label in label_to_idx:
                    _union(label_to_idx[label], idx)
                else:
                    label_to_idx[label] = idx

        components: dict[int, list[dict]] = {}
        for idx, e in enumerate(surviving):
            components.setdefault(_find(idx), []).append(e)

        for comp in components.values():
            if len(comp) <= 1:
                continue
            comp.sort(
                key=lambda e: (
                    _PROTO_PRIORITY.get(e.get("proto") or "", 0),
                    float(e.get("conf") or 0.0),
                    # Prefer edges whose names look like real IOS-style
                    # port names ("Te1/0/33") over Meraki-style opaque
                    # encodings ("Port 1::Ma-MOD-4X10G::4") so the
                    # surviving edge stays human-readable in the UI.
                    -(
                        ("::" in (e.get("iface_a") or "")) +
                        ("::" in (e.get("iface_b") or ""))
                    ),
                    1 if (e.get("start_id") or "") < (e.get("end_id") or "") else 0,
                ),
                reverse=True,
            )
            for losing in comp[1:]:
                rids_to_delete.append(losing["rid"])
                shared_iface_dropped += 1

    if not rids_to_delete:
        return 0

    # Bulk delete in chunks to keep per-tx work bounded.
    async with driver.session() as session:
        CHUNK = 500
        for i in range(0, len(rids_to_delete), CHUNK):
            batch = rids_to_delete[i:i + CHUNK]
            await session.run(
                """
                MATCH ()-[r:PHYSICAL_LINK]-()
                WHERE id(r) IN $rids
                DELETE r
                """,
                rids=batch,
            )
            deleted += len(batch)

    if deleted:
        log.info("correlate.physical_links_deduped",
                 deleted=deleted,
                 inferred_dropped=inferred_dropped,
                 sub_dropped=sub_dropped,
                 shared_iface_dropped=shared_iface_dropped,
                 pairs_with_dupes=len([g for g in pair_groups.values() if len(g) > 1]))
    return deleted


async def _normalize_physical_link_interfaces() -> int:
    """Rewrite ``interface_a`` / ``interface_b`` on PHYSICAL_LINK edges
    through ``normalize_ifname()`` so short and long Cisco names collapse
    to the same canonical form (``Vl80`` → ``Vlan80``, ``Twe1/1/5`` →
    ``TwentyFiveGigE1/1/5``).

    The raw values (``interface_a_raw``/``interface_b_raw``) are
    preserved by adapters that report them; this pass only rewrites the
    normalized field.

    Adapters call ``normalize_ifname()`` at creation time, so this pass
    is mainly a safety net for legacy edges or for adapters that
    bypassed normalization. Because the multi-edge MERGE keys on
    ``(interface_a, interface_b)``, a rename here may leave two edges
    sharing the post-rename key — dedupe (called next in the pipeline)
    cleans those up.

    Returns the number of edges updated.
    """
    from netcortex.util.ifname import normalize_ifname

    driver = get_driver()
    updated = 0
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH ()-[r:PHYSICAL_LINK]->()
            WHERE r.interface_a IS NOT NULL OR r.interface_b IS NOT NULL
            RETURN id(r) AS rid,
                   r.interface_a AS ia,
                   r.interface_b AS ib
            """
        )
        rows = await result.data()

        fixes: list[dict] = []
        for row in rows:
            ia = row.get("ia") or ""
            ib = row.get("ib") or ""
            new_ia = normalize_ifname(ia) if ia else ""
            new_ib = normalize_ifname(ib) if ib else ""
            if (ia and new_ia != ia) or (ib and new_ib != ib):
                fixes.append({
                    "rid": row["rid"],
                    "ia": new_ia or None,
                    "ib": new_ib or None,
                })

        if not fixes:
            return 0

        CHUNK = 500
        for i in range(0, len(fixes), CHUNK):
            batch = fixes[i:i + CHUNK]
            await session.run(
                """
                UNWIND $fixes AS f
                MATCH ()-[r:PHYSICAL_LINK]->()
                WHERE id(r) = f.rid
                SET r.interface_a = coalesce(f.ia, r.interface_a),
                    r.interface_b = coalesce(f.ib, r.interface_b)
                """,
                fixes=batch,
            )
            updated += len(batch)
    if updated:
        log.info("correlate.iface_names_normalized", count=updated)
    return updated


async def _enrich_mac_vendors() -> int:
    """Fill ``MACAddress.vendor`` from the IEEE OUI table for any MAC
    that doesn't already carry a vendor string.

    The lookup is in-memory (``mac_vendor_lookup``) so we can iterate
    every MAC in a single pass — the typical fleet has a few thousand
    MACs, well within an instant pass. We only WRITE to nodes whose
    vendor we successfully resolved, so unresolved MACs don't accumulate
    null writes on every cycle.
    """
    from netcortex.util.oui import lookup_vendor

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (m:MACAddress)
            WHERE m.mac IS NOT NULL
              AND (m.vendor IS NULL OR m.vendor = '')
            RETURN m.mac AS mac
            LIMIT 50000
            """
        )
        macs = [rec["mac"] async for rec in result if rec.get("mac")]

        updates: list[dict] = []
        for mac in macs:
            v = lookup_vendor(mac)
            if v:
                updates.append({"mac": mac, "vendor": v})

        if not updates:
            return 0

        CHUNK = 500
        written = 0
        for i in range(0, len(updates), CHUNK):
            batch = updates[i : i + CHUNK]
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (m:MACAddress {mac: row.mac})
                SET m.vendor = row.vendor
                """,
                rows=batch,
            )
            written += len(batch)

    if written:
        log.info("correlate.mac_vendors_filled",
                 count=written, candidates=len(macs))
    return written


async def _update_link_performance_history() -> dict[str, int]:
    """Maintain 7-day hourly histories for link utilization and errors.

    For every transit edge (PHYSICAL_LINK / WAN_UPLINK / SDWAN_TUNNEL /
    VXLAN_TUNNEL), we maintain three rolling hourly series:

      * ``util_in_pct_history_7d``
      * ``util_out_pct_history_7d``
      * ``error_rate_per_s_history_7d``

    Storage shape:
      ``[[bucket_start_ms, avg_value, sample_count], ...]``

    Derived scalars written each cycle:
      * ``util_in_pct_avg_1h``, ``util_out_pct_avg_1h``
      * ``util_in_pct_avg_24h``, ``util_out_pct_avg_24h``
      * ``util_pct_avg_1h`` = max(in_1h, out_1h)
      * ``error_rate_per_s_avg_1h``, ``error_rate_per_s_avg_24h``

    Health adjustment:
      ``health_score`` is re-evaluated using ``util_pct_avg_1h`` (not
      the instantaneous util sample). We retain the stronger of the
      existing health and the utilization penalty bucket:
        avg util >= 95% -> penalty 80
        avg util >= 85% -> penalty 60
        avg util >= 75% -> penalty 40
        avg util >= 65% -> penalty 25
        else           -> penalty 0
    """
    from netcortex.graph import history as H

    driver = get_driver()
    now_ms = _now_ms()
    stats = {
        "links_observed": 0,
        "links_history_updated": 0,
        "links_health_adjusted": 0,
    }

    read_q = """
        MATCH ()-[r:PHYSICAL_LINK|WAN_UPLINK|SDWAN_TUNNEL|VXLAN_TUNNEL]->()
        RETURN elementId(r) AS id,
               coalesce(r.util_in_pct, r.util_pct) AS util_in,
               coalesce(r.util_out_pct, r.util_pct) AS util_out,
               coalesce(r.error_rate_per_s, r.error_rate) AS err,
               r.util_in_pct_history_7d AS util_in_hist,
               r.util_out_pct_history_7d AS util_out_hist,
               r.error_rate_per_s_history_7d AS err_hist,
               r.health_score AS health_score
    """
    write_q = """
        MATCH ()-[r]->()
        WHERE elementId(r) = $id
        SET r += $u
    """

    async with driver.session() as session:
        rows = await (await session.run(read_q)).data()
        for row in rows:
            stats["links_observed"] += 1
            util_in = H.apply_hourly_metric_sample(
                row.get("util_in_hist"),
                row.get("util_in"),
                now_ms,
            )
            util_out = H.apply_hourly_metric_sample(
                row.get("util_out_hist"),
                row.get("util_out"),
                now_ms,
            )
            err = H.apply_hourly_metric_sample(
                row.get("err_hist"),
                row.get("err"),
                now_ms,
            )
            util_1h_in = util_in.get("avg_1h")
            util_1h_out = util_out.get("avg_1h")
            util_24h_in = util_in.get("avg_24h")
            util_24h_out = util_out.get("avg_24h")
            util_candidates = [v for v in (util_1h_in, util_1h_out) if v is not None]
            util_1h = max(util_candidates) if util_candidates else None

            updates: dict[str, Any] = {
                "util_in_pct_history_7d": util_in["history_json"],
                "util_out_pct_history_7d": util_out["history_json"],
                "error_rate_per_s_history_7d": err["history_json"],
                "util_in_pct_avg_1h": util_1h_in,
                "util_out_pct_avg_1h": util_1h_out,
                "util_in_pct_avg_24h": util_24h_in,
                "util_out_pct_avg_24h": util_24h_out,
                "util_pct_avg_1h": util_1h,
                "error_rate_per_s_avg_1h": err.get("avg_1h"),
                "error_rate_per_s_avg_24h": err.get("avg_24h"),
            }

            base_health = row.get("health_score") or 0
            util_penalty = 0
            if util_1h is not None:
                if util_1h >= 95:
                    util_penalty = 80
                elif util_1h >= 85:
                    util_penalty = 60
                elif util_1h >= 75:
                    util_penalty = 40
                elif util_1h >= 65:
                    util_penalty = 25
            new_health = max(float(base_health), float(util_penalty))
            if new_health != float(base_health):
                stats["links_health_adjusted"] += 1
            updates["health_score"] = round(new_health, 2)

            await session.run(write_q, id=row["id"], u=updates)
            stats["links_history_updated"] += 1

    log.info("correlate.link_performance_history_updated", **stats)
    return stats


async def _update_status_history() -> dict[str, int]:
    """Record state transitions and compute flap stats for every
    object that has an operational status field worth tracking.

    Targets and their status fields (kept small on purpose — adding
    a new target is one extra entry in ``_HISTORY_TARGETS``):

      * ``Device.status``                — up/down/active/alerting/etc.
      * ``PHYSICAL_LINK.oper_status``    — set by ``_enrich_physical_links_with_health``
      * ``WAN_UPLINK.oper_status``       — set by ``_infer_wan_topology``
      * ``SDWAN_TUNNEL.oper_status``     — adapter-emitted
      * ``ROUTING_PEER.oper_status``     — set by ``_collapse_routing_peers``

    For each (element, field), we:

      1. Read the current value AND the prior ``<field>_history`` JSON
         (one Cypher round-trip per target type).
      2. Diff in Python via ``history.apply_transition`` — appends a
         new transition only when the value actually changed, but
         always refreshes the flap-state scalar so 25-hour-old
         clusters age out of "unstable" cleanly.
      3. Write back the (small) updates dict via ``SET r += $u``.

    The pure decision logic lives in ``netcortex.graph.history``;
    this function is the only place that touches Neo4j on its
    behalf.  Keeps the schema definition and the I/O cleanly
    separated, which keeps the unit tests honest.

    Runs every correlation cycle.  Idempotent — re-running it on a
    stable graph produces no transitions and only refreshes the
    flap scalars, which is intentionally cheap.
    """
    from netcortex.graph import history as H

    driver = get_driver()
    now_ms = _now_ms()
    stats: dict[str, int] = {
        "device_transitions":         0,
        "device_observations":        0,
        "physical_link_transitions":  0,
        "physical_link_observations": 0,
        "wan_uplink_transitions":     0,
        "wan_uplink_observations":    0,
        "sdwan_tunnel_transitions":   0,
        "sdwan_tunnel_observations":  0,
        "routing_peer_transitions":   0,
        "routing_peer_observations":  0,
    }

    # Target definitions: (label_or_edge_type, is_edge, field, query).
    # Each query MUST return ``{id, current, history_json}`` rows; the
    # writeback uses ``elementId(x) = $id`` so any element kind works.
    #
    # We pull current+history in a single MATCH and write transitions
    # back element-by-element.  At a typical fleet scale (≤500 devices,
    # ≤1500 cables, ≤200 routing peers) this is sub-second and we
    # avoid the complexity of a batched APOC update.
    targets: list[tuple[str, str, str, str, str]] = [
        # (stat_prefix, kind, field, read_query, write_match_clause)
        (
            "device", "node", "status",
            """
            MATCH (d:Device)
            WHERE d.canonical_id IS NULL
            RETURN elementId(d)     AS id,
                   coalesce(d.oper_state, d.status, d.reachabilityStatus, 'unknown') AS current,
                   d.status_history AS history_json
            """,
            "MATCH (d) WHERE elementId(d) = $id SET d += $u",
        ),
        (
            "physical_link", "edge", "oper_status",
            """
            MATCH ()-[r:PHYSICAL_LINK]->()
            WHERE r.oper_status IS NOT NULL
            RETURN elementId(r)            AS id,
                   r.oper_status           AS current,
                   r.oper_status_history   AS history_json
            """,
            "MATCH ()-[r:PHYSICAL_LINK]->() WHERE elementId(r) = $id SET r += $u",
        ),
        (
            "wan_uplink", "edge", "oper_status",
            """
            MATCH ()-[r:WAN_UPLINK]->()
            WHERE r.oper_status IS NOT NULL
            RETURN elementId(r)            AS id,
                   r.oper_status           AS current,
                   r.oper_status_history   AS history_json
            """,
            "MATCH ()-[r:WAN_UPLINK]->() WHERE elementId(r) = $id SET r += $u",
        ),
        (
            "sdwan_tunnel", "edge", "oper_status",
            """
            MATCH ()-[r:SDWAN_TUNNEL]->()
            RETURN elementId(r)            AS id,
                   coalesce(r.oper_status, r.state, r.status, 'unknown') AS current,
                   r.oper_status_history   AS history_json
            """,
            "MATCH ()-[r:SDWAN_TUNNEL]->() WHERE elementId(r) = $id SET r += $u",
        ),
        (
            "routing_peer", "edge", "oper_status",
            """
            MATCH ()-[r:ROUTING_PEER]->()
            WHERE r.oper_status IS NOT NULL
            RETURN elementId(r)            AS id,
                   r.oper_status           AS current,
                   r.oper_status_history   AS history_json
            """,
            "MATCH ()-[r:ROUTING_PEER]->() WHERE elementId(r) = $id SET r += $u",
        ),
    ]

    async with driver.session() as session:
        for stat_prefix, _kind, field, read_q, write_q in targets:
            rows = await (await session.run(read_q)).data()
            for row in rows:
                stats[f"{stat_prefix}_observations"] += 1
                # Track the prior history-string identity so we only
                # count a transition when the history actually grew
                # (or first appeared).  Comparing the full string is
                # cheap and avoids relying on a key-presence heuristic
                # that gets fooled by the scalars-refresh path.
                prior_hist_str = row.get("history_json")
                updates = H.apply_transition(
                    field=field,
                    current_value=row.get("current"),
                    new_value=row.get("current"),
                    history_json=prior_hist_str,
                    now_ms=now_ms,
                )
                if not updates:
                    continue
                new_hist_str = updates.get(f"{field}_history")
                if new_hist_str is not None and new_hist_str != prior_hist_str:
                    stats[f"{stat_prefix}_transitions"] += 1
                await session.run(write_q, id=row["id"], u=updates)

    log.info("correlate.status_history_updated", **stats)
    return stats


async def _stamp_freshness() -> dict[str, int]:
    """Stamp observability timestamps on graph elements that need it.

    Three universal properties give the operator a consistent "how
    fresh is this fact?" answer regardless of which adapter or
    correlator produced an object:

    * ``first_seen`` — set ONCE on creation; never updated.
    * ``last_seen``  — refreshed on correlator-owned facts each cycle
      and initialised on any legacy row missing it.
    * ``status_changed_at`` (Devices only) — backfilled to
      ``first_seen`` for devices that have a status but no
      transition has been observed yet, so the UI can render
      "up since X" from the very first observation instead of
      showing a blank.

    This runs as the FINAL correlation step so it sees every node and
    edge produced by the adapters, the correlators, AND any
    intermediate cleanups. Adapter-emitted objects already get
    ``last_seen`` from ingest; this pass focuses on:
      1) Backfilling legacy rows missing first/last_seen.
      2) Refreshing correlator-owned rows (no source_adapter) without
         rewriting the entire graph on every cycle.

    Uses Neo4j's native ``timestamp()`` so the value matches the
    epoch-millisecond convention established by ``epoch_ms()``.
    """
    driver = get_driver()
    stats = {"nodes_touched": 0, "edges_touched": 0,
             "devices_status_backfilled": 0,
             "edges_oper_status_backfilled": 0}

    async with driver.session() as session:
        # Touch only rows that need initialization OR are correlator-owned.
        # Adapter-owned rows are refreshed in ingest and don't need a
        # full-graph rewrite every correlation cycle.
        res = await session.run(
            """
            MATCH (n)
            WHERE n.first_seen IS NULL
               OR n.last_seen IS NULL
               OR coalesce(n.source_adapter, '') = ''
            SET n.last_seen = timestamp()
            FOREACH (_ IN CASE WHEN n.first_seen IS NULL THEN [1] ELSE [] END |
                SET n.first_seen = timestamp())
            RETURN count(n) AS c
            """
        )
        rec = await res.single()
        stats["nodes_touched"] = (rec["c"] if rec else 0) or 0

        # Same strategy for relationships.
        res = await session.run(
            """
            MATCH ()-[r]->()
            WHERE r.first_seen IS NULL
               OR r.last_seen IS NULL
               OR coalesce(r.source_adapter, '') = ''
            SET r.last_seen = timestamp()
            FOREACH (_ IN CASE WHEN r.first_seen IS NULL THEN [1] ELSE [] END |
                SET r.first_seen = timestamp())
            RETURN count(r) AS c
            """
        )
        rec = await res.single()
        stats["edges_touched"] = (rec["c"] if rec else 0) or 0

        # Backfill Device.status_changed_at when missing so the UI
        # can render "up since X" / "down since X" from the very
        # first observation. We only backfill when the device has
        # a status set (otherwise there's nothing to mark a change
        # against). The value we use is first_seen — the earliest
        # point in time we can attribute the current status to.
        res = await session.run(
            """
            MATCH (d:Device)
            WHERE d.status IS NOT NULL
              AND d.status_changed_at IS NULL
              AND d.first_seen IS NOT NULL
            SET d.status_changed_at = d.first_seen
            RETURN count(d) AS c
            """
        )
        rec = await res.single()
        stats["devices_status_backfilled"] = (rec["c"] if rec else 0) or 0

        # Same backfill for every edge type that participates in the
        # status-history feature.  The history module's seed branch
        # deliberately does NOT stamp ``<field>_changed_at`` (because
        # seeding only proves "first time we observed this edge", not
        # an actual state transition we witnessed).  This backfill
        # provides the honest answer: ``first_seen`` — the earliest
        # point in time we can attribute the current operational
        # state to.
        #
        # Idempotent: only touches edges where ``oper_status_changed_at``
        # is still NULL, so the periodic correlator pass costs nothing
        # in steady state.
        edges_backfilled = 0
        for edge_type in ("PHYSICAL_LINK", "WAN_UPLINK",
                          "SDWAN_TUNNEL", "ROUTING_PEER"):
            res = await session.run(
                f"""
                MATCH ()-[r:{edge_type}]->()
                WHERE r.oper_status IS NOT NULL
                  AND r.oper_status_changed_at IS NULL
                  AND r.first_seen IS NOT NULL
                SET r.oper_status_changed_at = r.first_seen
                RETURN count(r) AS c
                """
            )
            rec = await res.single()
            edges_backfilled += (rec["c"] if rec else 0) or 0
        stats["edges_oper_status_backfilled"] = edges_backfilled

    log.info("correlate.freshness_stamped",
             nodes_touched=stats["nodes_touched"],
             edges_touched=stats["edges_touched"],
             devices_status_backfilled=stats["devices_status_backfilled"],
             edges_oper_status_backfilled=stats["edges_oper_status_backfilled"])
    return stats


async def get_correlation_stats() -> dict[str, int]:
    """Return counts of correlated vs adapter-discovered PHYSICAL_LINK edges."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH ()-[r:PHYSICAL_LINK]->()
            RETURN
                r.source AS src,
                r.discovery_proto AS proto,
                count(r) AS c
            """
        )
        records = await result.data()
        stats: dict[str, int] = {}
        for rec in records:
            key = rec.get("proto") or rec.get("src") or "unknown"
            stats[key] = stats.get(key, 0) + (rec["c"] or 0)
        return stats
