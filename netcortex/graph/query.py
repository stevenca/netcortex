"""Named graph queries — used by the REST API and MCP tools.

All functions return plain dicts/lists suitable for JSON serialisation
and for feeding directly to Cytoscape.js or an LLM context window.

Neo4j driver note: Node/Relationship objects support `dict(obj)` only when
the driver implements Mapping.  To guarantee compatibility across driver
versions we always use the Cypher `properties()` function in RETURN clauses,
which gives us plain Python dicts with no driver-object coercion needed.
"""

from __future__ import annotations

from typing import Any

import structlog

from netcortex.graph.client import get_driver
from netcortex.graph.models import Dimension, EdgeType, NodeType

log = structlog.get_logger(__name__)

# Structural edge types that define the container hierarchy.
# These are NEVER returned as Cytoscape edges — instead they populate the
# `parent` field on child nodes so Cytoscape renders compound containers.
_STRUCTURAL_RELS = frozenset({
    EdgeType.LOCATED_AT.value,
    EdgeType.WITHIN_LOCATION.value,
    EdgeType.MAPS_TO_SITE.value,
})

# Overlay name → relationship types to include.
#
# Overlays are multi-selectable in the UI (multiple can be enabled at once;
# the backend unions their edge-type lists). The previous "one dimension
# at a time" model is still supported via the legacy ``dimension`` query
# parameter and the ``_DIMENSION_RELS`` map below.
#
# Naming follows the OSI/network-operator mental model rather than the
# more abstract internal "dimension" enum:
#   physical  — cables and ports
#   l2        — switched broadcast domain: VLANs + spanning tree
#   l3        — routed reachability: prefixes, IPs, routing peers
#   sdwan     — encrypted overlay tunnels and policy assignments
#   fabric    — EVPN/VXLAN underlay + overlay (separate from L2/L3 because
#               not every fleet has fabric)
#   virtual   — hypervisor + VM topology (vSphere etc.)
_OVERLAY_RELS: dict[str, list[str]] = {
    # In the post-Phase 5 model, PHYSICAL_LINK is the single canonical
    # L1/L2 edge between two devices.  L2 overlays decorate that link
    # (vlans_carried / stp_* properties) or add a small set of
    # overlay-specific edges (canonical VLAN membership, HAS_SVI).
    # L3 routing adjacencies are NOT folded onto PHYSICAL_LINK — they
    # render as their own dashed ROUTING_PEER edges (0.6.0-dev6).
    "physical": [
        EdgeType.PHYSICAL_LINK.value,
        EdgeType.HAS_INTERFACE.value,
    ],
    "l2": [
        # PHYSICAL_LINK is included so the UI can color / label cables
        # by vlans_carried / native_vlan / trunk_mode when L2 is on.
        EdgeType.PHYSICAL_LINK.value,
        EdgeType.LOGICAL_MEMBER.value,
        EdgeType.HAS_SVI.value,
        # HAS_PREFIX is deliberately omitted: the canonical VLAN now
        # carries its associated CIDR(s) as `prefix_v4` / `prefix_v6`
        # properties (set by correlate._decorate_vlan_labels_with_prefixes),
        # so the UI renders one combined VLAN node instead of a VLAN
        # plus a dangling Prefix.  Prefix nodes are still reachable from
        # the L3 overlay via ROUTES_TO.
        # STP context is also "L2" — kept here while STPDomain still
        # exists. Phase 6 retires STPDomain in favor of inline link
        # decoration (stp_state_a/b/etc.) so these will be removed.
        EdgeType.STP_MEMBER.value,
        EdgeType.STP_ROOT.value,
        EdgeType.STP_LINK.value,
    ],
    "l3": [
        # Same logic as L2: physical cables show up so the operator can
        # see what the routing adjacencies overlay on. ROUTING_PEER
        # edges are now the canonical routing-adjacency visualisation
        # (rendered as dashed lines coloured by oper_status).
        EdgeType.PHYSICAL_LINK.value,
        EdgeType.ROUTES_TO.value,
        EdgeType.BGP_PEER.value,
        EdgeType.VRF_MEMBER.value,
        EdgeType.ROUTING_PEER.value,
        EdgeType.ASSIGNED_IP.value,
    ],
    "sdwan": [
        EdgeType.SDWAN_TUNNEL.value,
        EdgeType.POLICY_APPLIES.value,
    ],
    "fabric": [
        EdgeType.VNI_EXTENDS.value,
        EdgeType.FABRIC_PEER.value,
        EdgeType.VNI_MEMBER.value,
    ],
    "virtual": [
        EdgeType.HAS_VM.value,
        EdgeType.VM_NETWORK.value,
    ],
    # WAN — site uplink to the public Internet. Includes the Device →
    # AutonomousSystem and Device → Internet edges that the WAN
    # correlator stamps based on Meraki MX uplink IPs, eBGP-to-public-AS
    # neighbors, and (eventually) default-route next-hops.
    "wan": [
        EdgeType.WAN_UPLINK.value,
        EdgeType.TRANSITS.value,
        # AS_PEER was emitted in dev2 to model the home AS ↔ external
        # AS boundary; dev3 retired it because the WAN_UPLINK edge
        # from the border device to the external AS already conveys
        # the boundary, and the home AS hexagon was visual noise on
        # top of the per-device local_asn halo.  The EdgeType still
        # exists for back-compat but is no longer included in any
        # overlay.
    ],
}

# Legacy single-dimension map (kept for back-compat with anyone hitting
# /api/graph?dimension=X). New callers should prefer ?overlay=X[&overlay=Y].
_DIMENSION_RELS: dict[str, list[str]] = {
    Dimension.PHYSICAL.value: _OVERLAY_RELS["physical"],
    Dimension.LOGICAL.value: [
        EdgeType.LOGICAL_MEMBER.value,
        EdgeType.HAS_SVI.value,
        EdgeType.ASSIGNED_IP.value,
        EdgeType.VRF_MEMBER.value,
    ],
    Dimension.ROUTING.value: _OVERLAY_RELS["l3"],
    Dimension.FABRIC.value: _OVERLAY_RELS["fabric"],
    Dimension.SDWAN.value: _OVERLAY_RELS["sdwan"],
    Dimension.VIRTUAL.value: [
        EdgeType.HAS_VM.value,
        EdgeType.VM_NETWORK.value,
        EdgeType.LOGICAL_MEMBER.value,
        EdgeType.HAS_SVI.value,
        EdgeType.VNI_MEMBER.value,
    ],
    Dimension.STP.value: [
        EdgeType.STP_MEMBER.value,
        EdgeType.STP_ROOT.value,
        EdgeType.STP_LINK.value,
    ],
    Dimension.WAN.value: _OVERLAY_RELS["wan"],
}


def list_overlays() -> list[dict[str, list[str]]]:
    """Return the overlay catalog (used by the UI to auto-render buttons)."""
    return [{"id": name, "rel_types": rels} for name, rels in _OVERLAY_RELS.items()]

# Node types hidden by default in the topology view.
# Interface nodes clutter the view; expose them only via device detail.
# MACAddress and ARPEntry are used internally for correlation only.
# STPDomain carries no information that isn't already projected onto
# the participating Device nodes (stp_is_root, stp_priority,
# stp_domain_name, stp_root_bridge_name — see
# ``correlate._decorate_devices_with_stp_membership``) — leaving the
# standalone node visible just clutters every site with a red dot
# whose only outgoing edge is "ms1 is root", which the gold root
# border on the switch already conveys.  The STP toggle in the UI
# lights up the projected fields directly.
_HIDDEN_NODE_TYPES_DEFAULT = {"Interface", "MACAddress", "ARPEntry", "STPDomain"}


def _node_type(labels: list[str]) -> str:
    return labels[0] if labels else "Unknown"


async def get_full_graph(
    dimension: str | None = None,
    overlays: list[str] | None = None,
    strict_overlays: bool = False,
    collapse_l3_on_physical: bool = False,
    site: str | None = None,
    limit: int = 2000,
    include_interfaces: bool = False,
    include_mac_nodes: bool = False,
) -> dict[str, list[dict]]:
    """Return the graph in Cytoscape.js compound-node format.

    Container hierarchy (Site → Location → PlatformSite → Device) is expressed
    via the `parent` field on each node's data rather than as edges, so
    Cytoscape.js renders them as nested compound nodes automatically.

    Args:
        dimension:          [legacy] Filter to one dimension (physical/logical/
                            routing/sdwan/fabric/stp/virtual). Mutually exclusive
                            with ``overlays`` — if both are passed, ``overlays``
                            wins.
        overlays:           [preferred] List of overlay names — the returned edge
                            set is the UNION of all selected overlays. Valid
                            names: physical, l2, l3, sdwan, fabric, virtual.
                            Without ``strict_overlays`` an empty list falls back
                            to every non-structural rel; with ``strict_overlays``
                            an empty list returns ZERO edges (nodes only).
        strict_overlays:    When True, the response is bounded EXACTLY by the
                            ``overlays`` selection (empty selection → no edges).
                            The UI uses this so toggling overlays is purely
                            additive.
        collapse_l3_on_physical:
                            When True, fold routing/BGP adjacencies onto the
                            PHYSICAL_LINK edge that carries them. The routing
                            peer edge is dropped from the returned set and the
                            physical edge gains a ``carries`` array of
                            ``{protocol, peer_ip, remote_as, state, peer_device}``
                            entries. Only applies when the peer's IP can be
                            matched to a Device in the graph and a physical
                            link exists between the two devices — multi-hop
                            iBGP, SVI-only adjacencies, and tunnels are left
                            as standalone edges. The UI sets this whenever
                            both ``physical`` and ``l3`` overlays are active.
        site:               Filter by canonical site slug (nb-site:<slug>).
        limit:              Max number of non-structural relationships to return.
        include_interfaces: Expose Interface port-marker nodes (hidden by default).
        include_mac_nodes:  Expose MACAddress/ARPEntry nodes (hidden by default).
    """
    driver = get_driver()

    # Cytoscape.js reserves these field names on edge data objects.
    # Rename any Neo4j property that collides to avoid silent overwrites.
    _CY_RESERVED = frozenset({"id", "source", "target"})

    def _safe_props(props: dict, prefix: str = "neo4j_") -> dict:
        return {
            (f"{prefix}{k}" if k in _CY_RESERVED else k): v
            for k, v in props.items()
        }

    async with driver.session() as session:
        # Determine which node types to hide — needed before structural queries
        # so we can filter child_nodes consistently.
        hidden_types: set[str] = set()
        hidden_types.update({NodeType.SITE.value, NodeType.LOCATION.value})  # never show NetBox nodes
        # STPDomain is permanently hidden: every fact it carries
        # (root device, priority, name) is already projected onto the
        # participating Device nodes by
        # ``correlate._decorate_devices_with_stp_membership``.  Leaving
        # the standalone node visible just adds a red dot at every
        # site whose only outgoing edge is "<root> is root", which the
        # gold border on the root switch already communicates.
        hidden_types.add(NodeType.STP_DOMAIN.value)
        if not include_interfaces:
            hidden_types.add(NodeType.INTERFACE.value)
        if not include_mac_nodes:
            hidden_types.update({NodeType.MAC_ADDRESS.value, NodeType.ARP_ENTRY.value})

        # ── Step 1: Build parent map ──────────────────────────────────────────
        #
        # Only PlatformSite nodes are used as visual containers — canonical
        # NetBox Site/Location nodes are intentionally excluded from the UI
        # (they exist in the graph for enrichment / site-correlation only).
        #
        # Visual hierarchy:  PlatformSite > Device/Interface/…
        # Floating nodes: devices with no LOCATED_AT appear without a parent.

        parent_map: dict[str, str] = {}
        container_nodes: dict[str, dict] = {}
        child_nodes: dict[str, dict] = {}  # all nodes that live inside a container

        def _register_container(nid: str, nprops: dict, nlabels: list) -> None:
            if nid and nid not in container_nodes:
                nt = _node_type(nlabels)
                container_nodes[nid] = {
                    "data": {
                        "id": nid,
                        "label": nprops.get("name", nid),
                        "type": nt,
                        **_safe_props(nprops),
                    }
                }

        # 1a. PlatformSite nodes that have at least one child → containers.
        # Empty site containers (no LOCATED_AT children) are intentionally
        # hidden to avoid clutter from platforms that have many empty sites.
        r_ps = await session.run(
            "MATCH (ps:PlatformSite) WHERE (ps)<-[:LOCATED_AT]-() "
            "RETURN properties(ps) AS pp, labels(ps) AS pl"
        )
        for rec in await r_ps.data():
            pp = rec["pp"] or {}
            pid = pp.get("id", "")
            _register_container(pid, pp, rec["pl"])

        # 1a2. PlatformSite → PlatformSite nesting (e.g. Chassis inside UCS Domain).
        # The standard step 1b only maps non-PlatformSite children; here we capture
        # the parent assignment for PlatformSite nodes that themselves have a
        # LOCATED_AT to another PlatformSite (like chassis inside a UCS domain).
        r_ps_nest = await session.run(
            "MATCH (child:PlatformSite)-[:LOCATED_AT]->(parent:PlatformSite) "
            "RETURN child.id AS child_id, parent.id AS parent_id"
        )
        for rec in await r_ps_nest.data():
            child_id = rec["child_id"] or ""
            par_id = rec["parent_id"] or ""
            if child_id and par_id:
                parent_map[child_id] = par_id
                if child_id in container_nodes:
                    container_nodes[child_id]["data"]["parent"] = par_id

        # 1a.5  Build absorbed_prefixes — Prefix nodes whose CIDR is
        # already visible as an annotation on a containing element (a
        # VLAN's label or a PHYSICAL_LINK's l3_prefix). These are
        # hidden from the topology so the user doesn't see "VLAN 14 +
        # floating Prefix node + ROUTES_TO cloud" all carrying the
        # same CIDR.  Edges that touch an absorbed prefix are dropped
        # at edge-emit time below, and child re-parenting also skips
        # absorbed prefixes so they don't reappear as isolated
        # container children.
        r_abs = await session.run(
            "MATCH (p:Prefix) WHERE p.absorbed = true RETURN p.id AS pid"
        )
        absorbed_prefixes: set[str] = set()
        for rec in await r_abs.data():
            if rec["pid"]:
                absorbed_prefixes.add(rec["pid"])

        # 1b. All LOCATED_AT → PlatformSite edges → parent_map + child_nodes.
        # We use the PlatformSite directly as the visual parent regardless of
        # any MAPS_TO_SITE correlation (which is used for enrichment only).
        # Duplicate nodes (canonical_id IS NOT NULL) are excluded here — they
        # will be hidden from the graph in favour of their canonical counterpart.
        r_loc = await session.run(
            "MATCH (child)-[:LOCATED_AT]->(ps:PlatformSite) "
            "WHERE NOT child:PlatformSite AND NOT child:Site AND NOT child:Location "
            "AND (child.canonical_id IS NULL) "
            "RETURN properties(child) AS cp, labels(child) AS cl, ps.id AS parent_id"
        )
        for rec in await r_loc.data():
            cp = rec["cp"] or {}
            cid = cp.get("id", "")
            pid = rec.get("parent_id", "")
            if not cid:
                continue
            # Don't re-introduce absorbed Prefix nodes here either —
            # they would otherwise appear as isolated children of the
            # NetBox-site container with no edges (because the edge
            # path above already drops them).
            if cid in absorbed_prefixes:
                continue
            if pid:
                parent_map[cid] = pid
            cl = rec["cl"] or []
            nt = _node_type(cl)
            if nt not in hidden_types and cid not in container_nodes:
                el: dict = {
                    "data": {
                        "id": cid,
                        "label": cp.get("name", cid),
                        "type": nt,
                        **_safe_props(cp),
                    }
                }
                if pid:
                    el["data"]["parent"] = pid
                child_nodes[cid] = el

        # 1c. NetBox site override — for devices enriched with a NetBox site,
        # replace the PlatformSite visual parent with a NetBox-site container.
        # The original platform container is retained in device.platform_container.
        # Only canonical nodes (canonical_id IS NULL) get a NetBox site container.
        #
        # We also gather (PlatformSite → slug) statistics here so the next
        # step can either re-parent the PlatformSite under the NetBox site
        # (when only one slug dominates) or hide it entirely (when the
        # PlatformSite's name collides with that NetBox site's name).
        r_nb_sites = await session.run(
            "MATCH (d:Device) "
            "WHERE d.netbox_site_slug IS NOT NULL AND d.netbox_site_slug <> '' "
            "AND (d.canonical_id IS NULL) "
            "OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite) "
            "RETURN d.id AS did, "
            "       d.netbox_site_slug AS slug, "
            "       d.netbox_site_name AS site_name, "
            "       ps.id   AS ps_id, "
            "       ps.name AS ps_name"
        )
        # ps_id → {slug: count}
        ps_slug_counts: dict[str, dict[str, int]] = {}
        ps_name_by_id: dict[str, str] = {}
        nb_site_name_by_slug: dict[str, str] = {}

        for rec in await r_nb_sites.data():
            did = rec["did"]
            slug = rec["slug"] or ""
            site_name = rec["site_name"] or slug
            if not slug:
                continue
            container_id = f"nb-site:{slug}"
            nb_site_name_by_slug[slug] = site_name

            # Register the NetBox site as a container (PlatformSite-styled)
            if container_id not in container_nodes:
                container_nodes[container_id] = {
                    "data": {
                        "id": container_id,
                        "label": site_name,
                        "type": "PlatformSite",
                        "source": "netbox",
                        "name": site_name,
                        "slug": slug,
                    }
                }

            # Override parent assignment to NetBox site
            parent_map[did] = container_id
            if did in child_nodes:
                child_nodes[did]["data"]["parent"] = container_id

            ps_id = rec.get("ps_id") or ""
            ps_name = rec.get("ps_name") or ""
            if ps_id:
                bucket = ps_slug_counts.setdefault(ps_id, {})
                bucket[slug] = bucket.get(slug, 0) + 1
                if ps_name and ps_id not in ps_name_by_id:
                    ps_name_by_id[ps_id] = ps_name

        # 1c.5. Re-parent or hide PlatformSite containers that all map to a
        # single NetBox site.
        #
        # Two cases:
        #   A. PlatformSite name == NetBox site name (e.g. Meraki network
        #      "cpn-ful" + NetBox site "cpn-ful") — completely hide the
        #      PlatformSite so the UI doesn't render two identically-
        #      labeled boxes side-by-side.  Every child that pointed at
        #      this PlatformSite (Devices, nested PSes) is re-parented to
        #      the NetBox site directly.
        #   B. PlatformSite name differs (e.g. "Intersight CPN" or
        #      "Chassis-1") — keep the PlatformSite but make it a child of
        #      the NetBox site, so the structure becomes
        #      ``nb-site:cpn-ful → Intersight CPN → Chassis-1 → blades``.
        hidden_platform_sites: set[str] = set()
        for ps_id, slug_counts in ps_slug_counts.items():
            if not slug_counts:
                continue
            # Pick the dominant slug (most enriched children of this PS).
            dominant_slug = max(slug_counts.items(), key=lambda kv: kv[1])[0]
            container_id = f"nb-site:{dominant_slug}"
            if container_id not in container_nodes:
                continue
            ps_name = ps_name_by_id.get(ps_id, "")
            nb_name = nb_site_name_by_slug.get(dominant_slug, "")

            if ps_name and nb_name and ps_name == nb_name:
                # Case A: hide the PlatformSite entirely.  Re-parent all
                # of its children (including non-enriched ones like CDP
                # stubs and nested PSes) onto the NetBox site directly.
                hidden_platform_sites.add(ps_id)
                for child_id, parent_id in list(parent_map.items()):
                    if parent_id == ps_id:
                        parent_map[child_id] = container_id
                        if child_id in child_nodes:
                            child_nodes[child_id]["data"]["parent"] = container_id
                        if child_id in container_nodes:
                            container_nodes[child_id]["data"]["parent"] = container_id
                # Remove the PlatformSite container from the rendered set
                container_nodes.pop(ps_id, None)
            else:
                # Case B: nest the PlatformSite under the NetBox site.
                parent_map[ps_id] = container_id
                if ps_id in container_nodes:
                    container_nodes[ps_id]["data"]["parent"] = container_id

        # 1d. Build canonical_map — used to skip duplicate nodes and reroute edges.
        # Nodes with canonical_id set are non-canonical duplicates; they are hidden
        # from the topology and any edges they carry are dropped (the canonical node
        # carries the authoritative connections via the correlation engine).
        r_dupes = await session.run(
            "MATCH (d:Device) WHERE d.canonical_id IS NOT NULL "
            "RETURN d.id AS dupe_id, d.canonical_id AS canon_id"
        )
        canonical_map: dict[str, str] = {}
        for rec in await r_dupes.data():
            if rec["dupe_id"] and rec["canon_id"]:
                canonical_map[rec["dupe_id"]] = rec["canon_id"]

        # ── Step 2: Main topology query ───────────────────────────────────────
        #
        # Resolution order:
        #   1. ``overlays`` (preferred, multi-select)  → union of rel types
        #   2. ``dimension`` (legacy, single-select)   → one dimension's rels
        #   3. neither set + strict_overlays           → NO edges (nodes only)
        #   4. neither set                             → every non-structural rel
        active_overlays = [o for o in (overlays or []) if o in _OVERLAY_RELS]
        skip_edges = False
        rel_pattern = ""
        if active_overlays:
            rel_set: set[str] = set()
            for o in active_overlays:
                rel_set.update(_OVERLAY_RELS[o])
            rel_pattern = f"[r:{'|'.join(sorted(rel_set))}]"
        elif dimension and dimension in _DIMENSION_RELS:
            rel_types = "|".join(_DIMENSION_RELS[dimension])
            rel_pattern = f"[r:{rel_types}]"
        elif strict_overlays:
            # Caller wants overlays-respected-exactly behavior: zero overlays
            # means zero edges. We still want to surface the device fleet so
            # the user can see what they have — that happens in the canonical-
            # device backfill below.
            skip_edges = True
        else:
            excluded = set(_STRUCTURAL_RELS)
            if not include_mac_nodes:
                excluded |= {
                    EdgeType.LEARNED_MAC.value,
                    EdgeType.OWNS_MAC.value,
                    EdgeType.HAS_ARP.value,
                }
            all_rels = [e.value for e in EdgeType if e.value not in excluded]
            rel_pattern = f"[r:{'|'.join(all_rels)}]"

        site_filter = (
            "WHERE (src)-[:LOCATED_AT*1..2]->(:PlatformSite {slug: $site})"
        ) if site else ""

        records: list[dict] = []
        if not skip_edges:
            cypher = (
                f"MATCH (src)-{rel_pattern}->(dst) "
                f"{site_filter} "
                f"RETURN properties(src) AS src_props, "
                f"       properties(dst) AS dst_props, "
                f"       properties(r)   AS rel_props, "
                f"       labels(src)     AS src_labels, "
                f"       labels(dst)     AS dst_labels, "
                f"       type(r)         AS rel_type, "
                f"       id(r)           AS rel_id "
                f"LIMIT $limit"
            )
            result = await session.run(cypher, site=site, limit=limit)
            records = await result.data()

            # ── Spine guarantee: ensure PHYSICAL_LINK is never truncated ──
            #
            # The single MATCH-all-edges query above splits the LIMIT budget
            # across every requested rel type. When the union spans many
            # overlays (e.g. UI fetching physical+l2+l3+sdwan+fabric+virtual)
            # heavy edge classes like SDWAN_TUNNEL/STP_ROOT can squeeze the
            # PHYSICAL_LINK cable map out of the result — even though the
            # cable map is the topological spine every overlay rides on top
            # of. Backfill any missing PHYSICAL_LINK edges so the cable map
            # is always complete regardless of the per-edge-type mix.
            if "PHYSICAL_LINK" in rel_pattern and rel_pattern != "[r:PHYSICAL_LINK]":
                have_phys_ids = {
                    rec["rel_id"] for rec in records
                    if rec.get("rel_type") == "PHYSICAL_LINK"
                }
                spine_cypher = (
                    f"MATCH (src)-[r:PHYSICAL_LINK]->(dst) "
                    f"{site_filter} "
                    f"RETURN properties(src) AS src_props, "
                    f"       properties(dst) AS dst_props, "
                    f"       properties(r)   AS rel_props, "
                    f"       labels(src)     AS src_labels, "
                    f"       labels(dst)     AS dst_labels, "
                    f"       type(r)         AS rel_type, "
                    f"       id(r)           AS rel_id"
                )
                spine_result = await session.run(spine_cypher, site=site)
                for rec in await spine_result.data():
                    if rec.get("rel_id") not in have_phys_ids:
                        records.append(rec)

        seen_nodes: dict[str, dict] = {}
        edges: list[dict] = []

        for rec in records:
            src: dict = rec["src_props"] or {}
            dst: dict = rec["dst_props"] or {}
            rel: dict = rec["rel_props"] or {}
            src_id: str = src.get("id", "")
            dst_id: str = dst.get("id", "")
            src_type = _node_type(rec["src_labels"])
            dst_type = _node_type(rec["dst_labels"])

            if src_type in hidden_types or dst_type in hidden_types:
                continue

            # Skip edges that touch non-canonical (duplicate) nodes entirely.
            # The canonical node carries the authoritative connections.
            if src_id in canonical_map or dst_id in canonical_map:
                continue

            # Skip edges that touch a Prefix that has already been
            # absorbed into a VLAN label or a physical link annotation —
            # rendering them would produce duplicate visual signal
            # (a floating Prefix node + ROUTES_TO/HAS_PREFIX cloud
            # alongside the VLAN/cable that already shows the CIDR).
            if src_id in absorbed_prefixes or dst_id in absorbed_prefixes:
                continue

            for nid, nprops, ntype in [(src_id, src, src_type), (dst_id, dst, dst_type)]:
                if nid and nid not in seen_nodes:
                    el: dict[str, Any] = {
                        "data": {
                            "id": nid,
                            "label": nprops.get("name", nid),
                            "type": ntype,
                            **_safe_props(nprops),
                        }
                    }
                    # Inject Cytoscape compound parent (NetBox site takes priority
                    # via parent_map which was overridden in step 1c above).
                    if nid in parent_map:
                        el["data"]["parent"] = parent_map[nid]
                    seen_nodes[nid] = el

            if src_id and dst_id:
                # PHYSICAL_LINK can have multiple parallel edges between
                # the same pair (one per cable). Include the Neo4j-internal
                # relationship id in the Cytoscape edge id so Cytoscape
                # doesn't collapse them or throw "duplicate id" errors.
                edge_id = f"{src_id}-{rec['rel_type']}-{dst_id}-{rec['rel_id']}"
                edges.append({
                    "data": {
                        "id": edge_id,
                        "source": src_id,
                        "target": dst_id,
                        "type": rec["rel_type"],
                        **_safe_props(rel),
                    }
                })

        # ── Step 3 (optional): Hide stub-peer dupes for resolved sessions ──
        #
        # The correlator (`_collapse_routing_peers` → emit-adjacency model)
        # creates a direct Device↔Device ROUTING_PEER edge whenever a
        # stub peer's IP resolves to a known device.  The original
        # Device→RoutingPeer stub edge from the adapter is still present
        # in Neo4j (so the routing tables / detail view can find it), but
        # rendering both would shadow the canonical dashed adjacency line
        # the UI now draws between the two devices.
        #
        # When ``collapse_l3_on_physical`` is set we drop the
        # Device→RoutingPeer stub edge whenever a Device→Device
        # ROUTING_PEER adjacency exists for the same (protocol, peer_ip)
        # pair, and we drop the orphan RoutingPeer node that loses its
        # only edge.  Stub edges for unresolved (external/transit) peers
        # are left intact so the operator can still see "this device
        # has a BGP session to 4.2.2.1 / AS3356" on the canvas.
        if collapse_l3_on_physical and edges and not skip_edges:
            # 3a. Index Device→Device ROUTING_PEER adjacencies by
            # (sorted device-id pair, protocol, lowercased peer ip).
            # We key on the peer ip because the canonical direction
            # is "src.id < dst.id" — the stub edge's owner device may
            # be either endpoint.
            adj_keys: set[tuple[str, str, str, str]] = set()
            adj_devices: set[str] = set()
            for e in edges:
                d = e["data"]
                if d.get("type") != "ROUTING_PEER":
                    continue
                src = d.get("source", "")
                dst = d.get("target", "")
                # device-to-device adjacency: both endpoints are
                # Devices (no "routing-peer:" prefix).
                if src.startswith("routing-peer:") or dst.startswith("routing-peer:"):
                    continue
                proto = (d.get("protocol") or "").lower()
                lip = (d.get("local_ip") or "").lower()
                rip = (d.get("remote_ip") or "").lower()
                pair = tuple(sorted([src, dst]))
                if lip:
                    adj_keys.add((pair[0], pair[1], proto, lip))
                if rip:
                    adj_keys.add((pair[0], pair[1], proto, rip))
                adj_devices.add(src)
                adj_devices.add(dst)

            # 3b. RoutingPeer id → (peer_ip, protocol) — needed to match
            # a stub edge against the device-to-device adjacency set.
            peer_meta: dict[str, tuple[str, str]] = {}
            r_rp = await session.run(
                "MATCH (rp:RoutingPeer) "
                "RETURN rp.id AS id, rp.peer_ip AS ip, rp.protocol AS protocol"
            )
            async for rec in r_rp:
                rid = rec["id"]
                if not rid:
                    continue
                peer_meta[rid] = (
                    (rec["ip"] or "").lower(),
                    (rec["protocol"] or "").lower(),
                )

            # 3c. Drop stub Device→RoutingPeer edges whose adjacency is
            # already covered by a Device↔Device ROUTING_PEER. Keep
            # everything else.
            collapsed = 0
            kept_edges: list[dict] = []
            for e in edges:
                d = e["data"]
                if d.get("type") not in ("ROUTING_PEER", "BGP_PEER"):
                    kept_edges.append(e)
                    continue
                src = d.get("source", "")
                dst = d.get("target", "")
                # Device-to-device adjacencies always survive.
                if (not src.startswith("routing-peer:")
                        and not dst.startswith("routing-peer:")):
                    kept_edges.append(e)
                    continue
                # Identify which endpoint is the stub and which is the device.
                if src.startswith("routing-peer:"):
                    rp_id, dev_id = src, dst
                else:
                    rp_id, dev_id = dst, src
                meta = peer_meta.get(rp_id)
                if not meta or not meta[0]:
                    kept_edges.append(e)
                    continue
                peer_ip, proto = meta
                # If any device-to-device adjacency for this device covers
                # this (protocol, peer_ip), drop the stub edge.
                covered = False
                for adev in adj_devices:
                    if adev == dev_id:
                        continue
                    pair = tuple(sorted([dev_id, adev]))
                    if (pair[0], pair[1], proto, peer_ip) in adj_keys:
                        covered = True
                        break
                if covered:
                    collapsed += 1
                    continue
                kept_edges.append(e)
            edges = kept_edges
            if collapsed:
                # Refresh seen_nodes to drop any RoutingPeer nodes that
                # lost their only incoming edge — they'd otherwise render
                # as orphan dots.
                live_nodes: set[str] = set()
                for e in edges:
                    live_nodes.add(e["data"]["source"])
                    live_nodes.add(e["data"]["target"])
                for nid in list(seen_nodes.keys()):
                    nt = seen_nodes[nid]["data"].get("type")
                    if nt == "RoutingPeer" and nid not in live_nodes:
                        del seen_nodes[nid]

        # When the caller asked for "nodes only" (strict_overlays + no
        # overlay/dimension), explicitly surface every canonical Device that
        # isn't already captured via LOCATED_AT child resolution. Without
        # this, devices without a PlatformSite would silently disappear.
        if skip_edges:
            r_dev_all = await session.run(
                "MATCH (d:Device) "
                "WHERE d.canonical_id IS NULL "
                "RETURN properties(d) AS props, labels(d) AS lbls"
            )
            for rec in await r_dev_all.data():
                p = rec["props"] or {}
                did = p.get("id", "")
                if not did or did in child_nodes:
                    continue
                el: dict[str, Any] = {
                    "data": {
                        "id": did,
                        "label": p.get("name", did),
                        "type": _node_type(rec["lbls"]),
                        **_safe_props(p),
                    }
                }
                if did in parent_map:
                    el["data"]["parent"] = parent_map[did]
                child_nodes[did] = el

        # Merge: containers + all children (isolated or not) + topology endpoints.
        # Priority: seen_nodes (have full edge context) > child_nodes > container_nodes.
        # Duplicate nodes excluded from child_nodes (step 1b filter) and from
        # seen_nodes (edge skip above), so they never reach the final output.
        merged: dict[str, dict] = {**container_nodes, **child_nodes, **seen_nodes}

        # Prune empty containers — two passes to handle cascading emptiness.
        #
        # Pass 1: remove PlatformSite nodes that have no child nodes at all.
        #   Example: a chassis whose blades were moved to a NetBox site container.
        # Pass 2: remove PlatformSite nodes whose only children were themselves
        #   pruned in pass 1.  Example: a UCS domain that only contained the
        #   chassis just removed.
        #
        # Non-PlatformSite nodes (Device, VLAN, etc.) are always kept.

        def _prune_pass(nodes_dict: dict[str, dict]) -> dict[str, dict]:
            parents_in_use = {
                n["data"]["parent"]
                for n in nodes_dict.values()
                if "parent" in n["data"]
            }
            return {
                nid: node
                for nid, node in nodes_dict.items()
                if node["data"].get("type") not in ("PlatformSite",)
                or nid in parents_in_use
            }

        all_nodes = _prune_pass(_prune_pass(merged))

        return {"nodes": list(all_nodes.values()), "edges": edges}


async def get_device_context(device_name: str, hops: int = 2) -> dict[str, Any]:
    """Return a device and its neighbourhood up to `hops` away."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (d:Device) WHERE d.name = $name "
            "RETURN properties(d) AS props, labels(d) AS lbls LIMIT 1",
            name=device_name,
        )
        rec = await result.single()
        if not rec:
            return {"error": f"Device '{device_name}' not found in graph"}
        device: dict = rec["props"] or {}

        # Neighbourhood subgraph.
        # Path-length bounds CANNOT be parameterised — Cypher requires them
        # as literals in the variable-length pattern, so we clamp the int
        # and interpolate it directly.  We also accept ``hops=1`` so the
        # data explorer can ask for a tight 1-hop neighbourhood.
        hop_n = max(1, min(int(hops or 2), 4))
        result = await session.run(
            "MATCH path = (d:Device {name: $name})-[*1.." + str(hop_n) + "]-(n) "
            "RETURN "
            "  [nd IN nodes(path) | {props: properties(nd), labels: labels(nd)}] AS path_nodes, "
            "  [rr IN relationships(path) | {props: properties(rr), type: type(rr), "
            "    src_id: startNode(rr).id, dst_id: endNode(rr).id, "
            "    rel_id: id(rr)}]                                                AS path_rels",
            name=device_name,
        )
        path_records = await result.data()

        seen_nodes: dict[str, dict] = {}
        seen_edges: dict[str, dict] = {}
        neighbors: list[dict] = []
        interfaces: list[dict] = []
        vlans: list[dict] = []
        vrfs: list[dict] = []
        bgp_peers: list[dict] = []
        sdwan_tunnels: list[dict] = []
        mac_addresses: list[dict] = []

        _CY_RESERVED = frozenset({"id", "source", "target"})

        def _safe_props(props: dict, prefix: str = "neo4j_") -> dict:
            return {
                (f"{prefix}{k}" if k in _CY_RESERVED else k): v
                for k, v in props.items()
            }

        for prec in path_records:
            for node_data in prec.get("path_nodes", []):
                n: dict = node_data.get("props") or {}
                labels: list = node_data.get("labels") or []
                nid = n.get("id", "")
                if not nid or nid in seen_nodes:
                    continue
                label = labels[0] if labels else "Unknown"
                seen_nodes[nid] = {"data": {"id": nid, "label": n.get("name", nid), "type": label, **_safe_props(n)}}
                if label == "Device" and nid != device.get("id"):
                    neighbors.append(n)
                elif label == "Interface":
                    interfaces.append(n)
                elif label == "VLAN":
                    vlans.append(n)
                elif label == "VRF":
                    vrfs.append(n)
                elif label == "BGPSession":
                    bgp_peers.append(n)
                elif label == "SDWANTunnel":
                    sdwan_tunnels.append(n)
                elif label == "MACAddress":
                    mac_addresses.append(n)

            for rel_data in prec.get("path_rels", []):
                rel_props: dict = rel_data.get("props") or {}
                rel_type = rel_data.get("type", "RELATED")
                src_id = rel_data.get("src_id", "")
                dst_id = rel_data.get("dst_id", "")
                # Include the Neo4j relationship id so PHYSICAL_LINK
                # parallel edges (multiple cables between same pair) each
                # get a unique Cytoscape id rather than colliding.
                rel_id = rel_data.get("rel_id")
                eid = f"{src_id}-{rel_type}-{dst_id}-{rel_id}"
                if eid not in seen_edges:
                    seen_edges[eid] = {
                        "data": {"id": eid, "source": src_id, "target": dst_id, "type": rel_type, **_safe_props(rel_props)}
                    }

        return {
            "device": device,
            "neighbors": neighbors,
            "interfaces": interfaces,
            "vlans": vlans,
            "vrfs": vrfs,
            "bgp_peers": bgp_peers,
            "sdwan_tunnels": sdwan_tunnels,
            "mac_addresses": mac_addresses,
            "graph": {
                "nodes": list(seen_nodes.values()),
                "edges": list(seen_edges.values()),
            },
        }


async def find_path(src_name: str, dst_name: str, max_hops: int = 10) -> dict[str, Any]:
    """Find the shortest path between two devices in the graph."""
    driver = get_driver()
    async with driver.session() as session:
        # Path-length bounds CANNOT be parameterised — interpolate the
        # clamped integer directly (Cypher requires it as a literal).
        hop_n = max(1, min(int(max_hops or 10), 15))
        result = await session.run(
            "MATCH (src:Device {name: $src}), (dst:Device {name: $dst}) "
            "MATCH path = shortestPath((src)-[*1.." + str(hop_n) + "]-(dst)) "
            "RETURN [n IN nodes(path) | properties(n)] AS path_nodes, "
            "       [r IN relationships(path) | type(r)] AS path_rels, "
            "       length(path) AS hops",
            src=src_name,
            dst=dst_name,
        )
        rec = await result.single()
        if not rec:
            return {"error": f"No path found between '{src_name}' and '{dst_name}' within {max_hops} hops"}

        return {
            "source": src_name,
            "destination": dst_name,
            "hops": rec["hops"],
            "path": [
                {"node": node, "via": rel}
                for node, rel in zip(rec["path_nodes"], rec["path_rels"] + [None])
            ],
        }


async def get_graph_stats() -> dict[str, Any]:
    """Return node/edge counts per type — used for the status page."""
    driver = get_driver()
    async with driver.session() as session:
        counts: dict[str, Any] = {"nodes": {}, "relationships": {}}
        for label in ["Device", "Interface", "VLAN", "VNI", "VRF",
                      "BGPSession", "SDWANTunnel", "SDWANPolicy", "Prefix",
                      "Site", "Location", "PlatformSite",
                      "MACAddress", "ARPEntry"]:
            r = await session.run(f"MATCH (n:{label}) RETURN count(n) AS c")
            row = await r.single()
            counts["nodes"][label] = row["c"] if row else 0
        for rel in ["PHYSICAL_LINK", "BGP_PEER", "FABRIC_PEER", "SDWAN_TUNNEL",
                    "LOGICAL_MEMBER", "VNI_EXTENDS", "VRF_MEMBER",
                    "LOCATED_AT", "WITHIN_LOCATION", "MAPS_TO_SITE",
                    "HAS_INTERFACE", "ASSIGNED_IP",
                    "LEARNED_MAC", "OWNS_MAC", "HAS_ARP"]:
            r = await session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")
            row = await r.single()
            counts["relationships"][rel] = row["c"] if row else 0
        return counts


async def get_vlan_members(vid: int) -> dict[str, Any]:
    """Return all devices and interfaces that are members of a VLAN."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (v:VLAN {vid: $vid})<-[:LOGICAL_MEMBER]-(n) "
            "RETURN properties(n) AS props, labels(n) AS lbls",
            vid=vid,
        )
        records = await result.data()
        members = []
        for rec in records:
            props = rec["props"] or {}
            labels = rec["lbls"] or []
            members.append({"type": labels[0] if labels else "Unknown", **props})
        return {"vid": vid, "members": members, "count": len(members)}


async def get_mac_table(device_name: str | None = None, mac: str | None = None) -> dict[str, Any]:
    """Return MAC address table entries — optionally filtered by device or MAC address."""
    driver = get_driver()
    async with driver.session() as session:
        if device_name:
            result = await session.run(
                "MATCH (d:Device {name: $name})-[:HAS_INTERFACE]->(i:Interface)-[:LEARNED_MAC]->(m:MACAddress) "
                "RETURN properties(i) AS iface, properties(m) AS mac_props",
                name=device_name,
            )
        elif mac:
            result = await session.run(
                "MATCH (i:Interface)-[:LEARNED_MAC]->(m:MACAddress {mac: $mac}) "
                "MATCH (d:Device)-[:HAS_INTERFACE]->(i) "
                "RETURN properties(i) AS iface, properties(m) AS mac_props, properties(d) AS dev",
                mac=mac,
            )
        else:
            result = await session.run(
                "MATCH (i:Interface)-[:LEARNED_MAC]->(m:MACAddress) "
                "MATCH (d:Device)-[:HAS_INTERFACE]->(i) "
                "RETURN properties(i) AS iface, properties(m) AS mac_props, properties(d) AS dev "
                "LIMIT 500",
            )
        records = await result.data()
        entries = []
        for rec in records:
            iface = rec.get("iface") or {}
            mac_p = rec.get("mac_props") or {}
            dev = rec.get("dev") or {}
            entries.append({
                "mac": mac_p.get("mac"),
                "vendor": mac_p.get("vendor"),
                "interface": iface.get("name"),
                "device": dev.get("name"),
                "ip": mac_p.get("ip"),
                "vlan": mac_p.get("vlan"),
                "source": mac_p.get("source"),
            })
        return {"entries": entries, "count": len(entries)}


async def get_aggregated_topology(
    level: str = "site",
    dimension: str | None = None,
) -> dict[str, Any]:
    """Aggregated graph view — rolls up many devices into container summary nodes.

    Returns a small (<=200 element) graph regardless of how many devices the
    graph holds.  Designed as the default for the topology view so that
    browser renderers never see >3000 elements on initial load.

    Levels:
        site      — one bubble per PlatformSite (with device_count, error_count, etc.)
                    Edges between bubbles indicate inter-site links (any
                    PHYSICAL_LINK / SDWAN_TUNNEL crossing a site boundary).
        adapter   — one bubble per adapter source (meraki/CPN, intersight/CPN, …).
        dimension — one bubble per Dimension a device participates in.

    The returned shape mirrors /api/graph (nodes/edges) for drop-in UI use.
    """
    driver = get_driver()
    if level not in ("site", "adapter", "dimension"):
        level = "site"

    async with driver.session() as session:
        if level == "site":
            r = await session.run(
                """
                MATCH (d:Device)
                WHERE d.canonical_id IS NULL
                  AND (d.stub IS NULL OR d.stub = false)
                OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps)
                WITH coalesce(ps.id, 'unassigned') AS sid,
                     coalesce(ps.name, '(unassigned)') AS sname,
                     labels(ps) AS slabels,
                     d
                RETURN sid, sname, slabels,
                       count(d) AS total,
                       sum(CASE WHEN d.snmp_polled = true THEN 1 ELSE 0 END) AS polled_ok,
                       sum(CASE WHEN d.snmp_polled = false THEN 1 ELSE 0 END) AS polled_bad,
                       collect(DISTINCT d.source_adapter)[..10] AS adapters,
                       collect(DISTINCT d.role)[..10] AS roles
                ORDER BY total DESC
                """
            )
            buckets = await r.data()
            nodes: list[dict] = []
            for b in buckets:
                slabels = b.get("slabels") or []
                kind = "PlatformSite" if "PlatformSite" in slabels else (
                    "Site" if "Site" in slabels else "Unassigned"
                )
                nodes.append({
                    "data": {
                        "id": f"agg:site:{b['sid']}",
                        "label": f"{b['sname']}",
                        "subtitle": f"{b['total']} devices",
                        "type": "AggregateSite",
                        "container_kind": kind,
                        "container_id": b["sid"],
                        "device_count": b["total"],
                        "snmp_polled_ok": b["polled_ok"],
                        "snmp_polled_failed": b["polled_bad"],
                        "adapters": [a for a in (b.get("adapters") or []) if a],
                        "roles": [r for r in (b.get("roles") or []) if r],
                    }
                })

            # Edges between sites = any link where the two endpoints live in
            # different containers.  Apply the same stub/canonical filters as
            # the node query so we never reference a node that wasn't emitted.
            r2 = await session.run(
                """
                MATCH (a:Device)-[r:PHYSICAL_LINK|SDWAN_TUNNEL|VXLAN_TUNNEL|BGP_PEER]->(b:Device)
                WHERE a.canonical_id IS NULL AND b.canonical_id IS NULL
                  AND (a.stub IS NULL OR a.stub = false)
                  AND (b.stub IS NULL OR b.stub = false)
                OPTIONAL MATCH (a)-[:LOCATED_AT]->(psa)
                OPTIONAL MATCH (b)-[:LOCATED_AT]->(psb)
                WITH coalesce(psa.id, 'unassigned') AS sa,
                     coalesce(psb.id, 'unassigned') AS sb,
                     type(r) AS rel
                WHERE sa <> sb
                RETURN sa, sb, rel, count(*) AS weight
                """
            )

            # Build a set of valid node IDs for the safety-net filter below.
            node_ids = {n["data"]["id"] for n in nodes}

            edges = []
            async for r in r2:
                src = f"agg:site:{r['sa']}"
                tgt = f"agg:site:{r['sb']}"
                # Drop any edge whose endpoint wasn't emitted as a node
                # (guards against future query drift or unexpected graph state).
                if src not in node_ids or tgt not in node_ids:
                    continue
                edges.append({
                    "data": {
                        "id": f"agg:{r['rel']}:{r['sa']}->{r['sb']}",
                        "source": src,
                        "target": tgt,
                        "rel_type": r["rel"],
                        "weight": r["weight"],
                        "label": f"{r['weight']} {r['rel']}",
                    }
                })
            return {"level": level, "nodes": nodes, "edges": edges,
                    "node_count": len(nodes), "edge_count": len(edges),
                    "truncated": False}

        if level == "adapter":
            r = await session.run(
                """
                MATCH (d:Device)
                WHERE d.canonical_id IS NULL
                  AND (d.stub IS NULL OR d.stub = false)
                  AND d.source_adapter IS NOT NULL
                RETURN d.source_adapter AS adapter,
                       count(d) AS total,
                       sum(CASE WHEN d.snmp_polled = true THEN 1 ELSE 0 END) AS polled_ok
                ORDER BY total DESC
                """
            )
            buckets = await r.data()
            nodes = [
                {
                    "data": {
                        "id": f"agg:adapter:{b['adapter']}",
                        "label": b["adapter"],
                        "subtitle": f"{b['total']} devices",
                        "type": "AggregateAdapter",
                        "device_count": b["total"],
                        "snmp_polled_ok": b["polled_ok"],
                    }
                }
                for b in buckets
            ]
            return {"level": level, "nodes": nodes, "edges": [],
                    "node_count": len(nodes), "edge_count": 0,
                    "truncated": False}

        # dimension level
        r = await session.run(
            """
            MATCH (d:Device)
            WHERE d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
              AND d.dimensions IS NOT NULL
            UNWIND d.dimensions AS dim
            WITH dim, count(d) AS total
            RETURN dim, total ORDER BY total DESC
            """
        )
        buckets = await r.data()
        nodes = [
            {
                "data": {
                    "id": f"agg:dim:{b['dim']}",
                    "label": b["dim"],
                    "subtitle": f"{b['total']} devices",
                    "type": "AggregateDimension",
                    "device_count": b["total"],
                }
            }
            for b in buckets
        ]
        return {"level": level, "nodes": nodes, "edges": [],
                "node_count": len(nodes), "edge_count": 0,
                "truncated": False}


async def get_filter_catalog() -> dict[str, Any]:
    """Return a slim catalog of selectable filter targets (sites + devices).

    Powers the UI's reusable chip-filter component (see Topology,
    Inventory, MAC/ARP, STP, Routing views).  Each entry is a small
    ``{id, label, kind, site?}`` record — just enough to render the
    autocomplete dropdown and the chip pill, no Cytoscape data.

    Sites come from canonical ``Site`` nodes (preferred) and, where
    those don't exist yet, are derived from Device.netbox_site_slug
    so brand-new deployments still get a populated picker.  We never
    expose ``PlatformSite`` here — operators think in terms of NetBox
    sites, not adapter-specific containers.

    Devices include ``netbox_site_slug`` so the chip filter can
    auto-include a device's site when the device chip is selected
    (and vice versa) — keeps cross-view semantics consistent.

    Filters out tombstoned devices, non-canonical duplicates, and
    truly orphaned stubs.  Capped at 5000 devices for response size;
    typical deployments are well below that.
    """
    driver = get_driver()
    async with driver.session() as session:
        site_rows = await (await session.run(
            """
            MATCH (s:Site)
            WHERE s.tombstoned IS NULL OR s.tombstoned = false
            RETURN s.slug AS slug, s.name AS name
            UNION
            MATCH (d:Device)
            WHERE d.netbox_site_slug IS NOT NULL
              AND d.netbox_site_slug <> ''
              AND (d.tombstoned IS NULL OR d.tombstoned = false)
              AND NOT EXISTS {
                MATCH (s:Site) WHERE s.slug = d.netbox_site_slug
              }
            RETURN DISTINCT d.netbox_site_slug AS slug,
                            coalesce(d.netbox_site_name, d.netbox_site_slug) AS name
            """
        )).data()

        device_rows = await (await session.run(
            """
            MATCH (d:Device)
            WHERE d.canonical_id IS NULL
              AND (d.tombstoned IS NULL OR d.tombstoned = false)
              AND (d.stub IS NULL OR d.stub = false OR (d)--())
              AND d.name IS NOT NULL AND d.name <> ''
            RETURN d.id AS id,
                   d.name AS name,
                   coalesce(d.netbox_site_slug, '') AS site_slug,
                   coalesce(d.netbox_site_name, d.platform_container, '') AS site_label
            ORDER BY d.name
            LIMIT 5000
            """
        )).data()

    # De-duplicate sites by slug (the UNION above can yield 1 row per
    # source).  Sort by display name so the dropdown reads naturally.
    sites_by_slug: dict[str, dict[str, str]] = {}
    for row in site_rows:
        slug = (row.get("slug") or "").strip()
        if not slug:
            continue
        if slug not in sites_by_slug:
            sites_by_slug[slug] = {
                "id":    f"site:{slug}",
                "label": row.get("name") or slug,
                "slug":  slug,
                "kind":  "site",
            }
    sites = sorted(sites_by_slug.values(), key=lambda s: s["label"].lower())

    # De-duplicate devices by name (we collapse multi-adapter
    # duplicates here too — pick the first occurrence per name).  The
    # chip filter only needs one entry per logical device.
    seen_names: set[str] = set()
    devices: list[dict[str, str]] = []
    for row in device_rows:
        name = row.get("name") or ""
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        devices.append({
            "id":         row.get("id") or f"device:{name}",
            "label":      name,
            "name":       name,
            "site_slug":  row.get("site_slug") or "",
            "site_label": row.get("site_label") or "",
            "kind":       "device",
        })

    return {
        "sites":   sites,
        "devices": devices,
        "counts":  {"sites": len(sites), "devices": len(devices)},
    }


async def get_inventory() -> dict[str, Any]:
    """Return a flat inventory list of all Device nodes for the inventory table view.

    Includes site/location context, platform, role, serial, model, and IP.

    Stub Devices (LLDP/CDP-only discoveries that haven't been matched to a
    real platform-managed device) ARE included if they have at least one
    relationship to a real device — they show up flagged as
    ``discovery_only`` so operators can spot gaps in coverage (unmanaged
    devices, missing SNMP creds, or rogues). Truly orphaned stubs (no
    relationships) are skipped because they'd be GC'd by housekeeping
    anyway.

    Excludes:
      - Non-canonical duplicate nodes (canonical_id set)
      - Orphan stubs (stub=true with no relationships)
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (d:Device) "
            "WHERE d.canonical_id IS NULL "
            "  AND (d.stub IS NULL OR d.stub = false OR (d)--()) "
            "OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps) "
            "RETURN properties(d) AS dev, "
            "       labels(ps) AS ps_labels, "
            "       ps.name AS ps_name "
            "ORDER BY d.name "
            "LIMIT 2000",
        )
        records = await result.data()

    devices = []
    for rec in records:
        d = rec.get("dev") or {}
        source_adapter = d.get("source_adapter", "")
        # Adapter type is the part before the first "/" (e.g. "meraki" from "meraki/CPN")
        adapter_type = source_adapter.split("/")[0] if source_adapter else ""
        snmp_polled = bool(d.get("snmp_polled"))
        # snmp_sources is the modern multi-source list; fall back to the
        # legacy snmp_source single-string for older Device nodes.
        snmp_sources = d.get("snmp_sources") or []
        if not snmp_sources and snmp_polled:
            legacy = d.get("snmp_source")
            if legacy == "meraki_cloud":
                snmp_sources = ["meraki_cloud"]
            elif legacy:
                snmp_sources = [s.strip() for s in legacy.split("+") if s.strip()]
            else:
                snmp_sources = ["direct"]
        snmp_source = (d.get("snmp_source")
                       or ("+".join(snmp_sources) if snmp_sources
                           else ("direct" if snmp_polled else None)))

        # Build ordered list of data source pills. Direct SNMP and Meraki
        # cloud SNMP appear as separate pills so a device polled both ways
        # shows BOTH (one isn't hidden by the other).
        is_stub = bool(d.get("stub"))
        discovered_via = d.get("discovered_via") or ""

        data_sources: list[str] = []
        if is_stub:
            # Discovery-only entries: the only "source" is LLDP or CDP.
            # Tag with the discovering adapter so operators can trace it.
            tag = discovered_via or "lldp"
            data_sources.append(tag)
        elif adapter_type:
            data_sources.append(adapter_type)

        for src in snmp_sources:
            if src == "meraki_cloud":
                if "snmp:cloud" not in data_sources:
                    data_sources.append("snmp:cloud")
            elif src == "direct":
                if "snmp" not in data_sources:
                    data_sources.append("snmp")
            else:
                tag = f"snmp:{src}"
                if tag not in data_sources:
                    data_sources.append(tag)

        # SNMP MIB coverage diagnostics — these tell the user *why* certain
        # data is missing from a device (view restriction vs not instrumented
        # vs simply not polled yet) so they can fix the upstream device
        # config instead of treating it as a NetCortex bug.
        snmp_health = d.get("snmp_health") or (
            "full" if (snmp_polled and bool(d.get("snmp_direct"))) else
            ("cloud_only" if snmp_polled else "unpolled")
        )
        snmp_missing_mibs = list(d.get("snmp_missing_mibs") or [])
        snmp_restricted_mibs = list(d.get("snmp_restricted_mibs") or [])

        devices.append({
            "name":             d.get("name", ""),
            "role":             d.get("role", ""),
            "platform":         d.get("platform", ""),
            "model":            d.get("model") or d.get("platform_metadata_model", ""),
            "serial":           d.get("serial", ""),
            "mgmt_ip":          d.get("mgmt_ip", ""),
            "site":             d.get("netbox_site_name") or d.get("platform_container") or rec.get("ps_name") or "",
            "site_slug":        d.get("netbox_site_slug") or "",
            "platform_site":    d.get("platform_container") or rec.get("ps_name") or "",
            "source_adapter":   source_adapter,
            "adapter_type":     adapter_type,
            "snmp_polled":      snmp_polled,
            "snmp_source":      snmp_source,
            "snmp_sources":     snmp_sources,
            "snmp_direct":      bool(d.get("snmp_direct")),
            "snmp_cloud":       bool(d.get("snmp_cloud")),
            "snmp_health":      snmp_health,
            "snmp_missing_mibs": snmp_missing_mibs,
            "snmp_restricted_mibs": snmp_restricted_mibs,
            "snmp_mib_coverage_at": d.get("snmp_mib_coverage_at"),
            "data_sources":     data_sources,
            "discovery_only":   is_stub,
            "discovered_via":   discovered_via,
            "discovered_by":    d.get("discovered_by") or "",
            "status":           (d.get("oper_state") or d.get("status")
                                 or d.get("reachabilityStatus") or d.get("admin_state") or ""),
            "status_history":   d.get("status_history"),
            "status_changed_at": d.get("status_changed_at"),
            "status_flap_state": d.get("status_flap_state") or "stable",
            "status_flap_count_1h": d.get("status_flap_count_1h") or 0,
            "status_flap_count_24h": d.get("status_flap_count_24h") or 0,
            "vendor":           d.get("vendor", ""),
            "os_version":       (d.get("os_version") or d.get("softwareVersion")
                                 or d.get("firmware") or d.get("release")
                                 or d.get("running_software_version") or ""),
            # Cloud-managed "last reported" timestamp (epoch ms).  Today
            # only Meraki stamps this; other adapters will be added as
            # they grow equivalent semantics.  Surfaced here so
            # top_problems (and any external consumer of the inventory
            # API) can demote or filter problems whose source-of-truth
            # has not refreshed in a long time — distinguishing genuine
            # outages from abandoned/never-deployed inventory.
            "meraki_last_reported_at":     d.get("meraki_last_reported_at"),
            "meraki_last_reported_at_iso": d.get("meraki_last_reported_at_iso") or "",
        })

    return {"devices": devices, "count": len(devices)}


async def get_cam_correlated() -> dict[str, Any]:
    """Return a fully correlated MAC/ARP table.

    Each row tracks one MAC address with:
    - Where it was *learned* (CAM table entry: switch + interface + VLAN)
    - What device *owns* it (NIC ownership)
    - All IP addresses associated with it (ARP entries)

    This is built from three graph patterns:
      LEARNED_MAC:  Interface → MACAddress   (switch CAM entry)
      OWNS_MAC:     Device    → MACAddress   (server/endpoint NIC)
      HAS_ARP:      Interface → ARPEntry     (IP↔MAC binding)
    """
    driver = get_driver()
    async with driver.session() as session:
        # Collect all learned-MAC entries (switch CAM table data)
        r_learned = await session.run(
            "MATCH (sw:Device)-[:HAS_INTERFACE]->(i:Interface)-[r:LEARNED_MAC]->(m:MACAddress) "
            "RETURN m.mac AS mac, "
            "       sw.name AS learned_on_device, "
            "       i.name AS learned_on_port, "
            "       coalesce(r.vlan, m.vlan) AS vlan, "
            "       m.vendor AS vendor, "
            "       m.source AS source "
            "ORDER BY mac "
            "LIMIT 5000",
        )
        learned_records = await r_learned.data()

        # Collect all device-owns-MAC entries
        r_owns = await session.run(
            "MATCH (d:Device)-[r:OWNS_MAC]->(m:MACAddress) "
            "RETURN m.mac AS mac, d.name AS owner_device, "
            "       r.nic_name AS nic_name, d.role AS owner_role",
        )
        owns_records = await r_owns.data()

        # Collect ARP entries (IP↔MAC).
        # Three sources:
        #   (a) MACAddress -[:HAS_ARP]-> ARPEntry  (Meraki, CATC, NDFC)
        #   (b) Interface  -[:HAS_ARP]-> ARPEntry  (older CATC path)
        #   (c) MACAddress.ip property              (legacy inline storage)
        r_arp = await session.run(
            "MATCH (m:MACAddress)-[:HAS_ARP]->(a:ARPEntry) "
            "RETURN m.mac AS mac, a.ip AS ip, a.vrf AS vrf, "
            "       a.source AS arp_device, NULL AS arp_iface "
            "UNION ALL "
            "MATCH (i:Interface)-[:HAS_ARP]->(a:ARPEntry) "
            "RETURN a.mac AS mac, a.ip AS ip, a.vrf AS vrf, "
            "       a.device AS arp_device, a.interface AS arp_iface "
            "UNION ALL "
            "MATCH (m:MACAddress) WHERE m.ip IS NOT NULL AND m.ip <> '' "
            "AND NOT (m)-[:HAS_ARP]->() "
            "RETURN m.mac AS mac, m.ip AS ip, NULL AS vrf, "
            "       NULL AS arp_device, NULL AS arp_iface "
            "LIMIT 20000",
        )
        arp_records = await r_arp.data()

    # Index ownership and ARP by MAC
    owners: dict[str, dict] = {}
    for rec in owns_records:
        mac = (rec.get("mac") or "").lower()
        if mac and mac not in owners:
            owners[mac] = {
                "owner": rec.get("owner_device", ""),
                "nic": rec.get("nic_name", ""),
                "role": rec.get("owner_role", ""),
            }

    ips_by_mac: dict[str, list[str]] = {}
    for rec in arp_records:
        mac = (rec.get("mac") or "").lower()
        ip = rec.get("ip") or ""
        if mac and ip:
            ips_by_mac.setdefault(mac, [])
            if ip not in ips_by_mac[mac]:
                ips_by_mac[mac].append(ip)

    # Build correlated rows — one row per (mac, learned_on_device, learned_on_port) triplet
    rows = []
    seen_macs: set[str] = set()
    for rec in learned_records:
        mac = (rec.get("mac") or "").lower()
        if not mac:
            continue
        owner = owners.get(mac, {})
        ips = ips_by_mac.get(mac, [])
        rows.append({
            "mac":              mac,
            "vendor":           rec.get("vendor") or "",
            "learned_device":   rec.get("learned_on_device") or "",
            "learned_port":     rec.get("learned_on_port") or "",
            "vlan":             rec.get("vlan") or "",
            "owner_device":     owner.get("owner", ""),
            "owner_nic":        owner.get("nic", ""),
            "owner_role":       owner.get("role", ""),
            "ip_addresses":     ", ".join(sorted(ips)),
            "source":           rec.get("source") or "",
        })
        seen_macs.add(mac)

    # Add MACs that are owned but not in any CAM table.
    for mac, owner in owners.items():
        if mac not in seen_macs:
            ips = ips_by_mac.get(mac, [])
            rows.append({
                "mac":              mac,
                "vendor":           "",
                "learned_device":   "",
                "learned_port":     "",
                "vlan":             "",
                "owner_device":     owner.get("owner", ""),
                "owner_nic":        owner.get("nic", ""),
                "owner_role":       owner.get("role", ""),
                "ip_addresses":     ", ".join(sorted(ips)),
                "source":           "owned_only",
            })

    # Add MACs that only exist in ARP data (no owner and not currently learned).
    for mac, ips in ips_by_mac.items():
        if mac in seen_macs or mac in owners:
            continue
        rows.append({
            "mac":              mac,
            "vendor":           "",
            "learned_device":   "",
            "learned_port":     "",
            "vlan":             "",
            "owner_device":     "",
            "owner_nic":        "",
            "owner_role":       "",
            "ip_addresses":     ", ".join(sorted(ips)),
            "source":           "arp_only",
        })

    rows.sort(key=lambda r: (r["mac"], r["learned_device"], r["learned_port"]))
    return {"entries": rows, "count": len(rows)}


async def get_mac_lookup(mac: str, limit: int = 50) -> dict[str, Any]:
    """Return correlated MAC rows for one specific MAC address.

    This is the targeted/efficient variant of ``get_cam_correlated`` used by
    MCP ``mac_lookup``. It keeps functionality identical (same output shape)
    while avoiding a full CAM-table scan in Python for single-MAC lookups.
    """
    driver = get_driver()
    async with driver.session() as session:
        r_rows = await session.run(
            """
            MATCH (m:MACAddress {mac: $mac})
            OPTIONAL MATCH (sw:Device)-[:HAS_INTERFACE]->(i:Interface)
                           -[lm:LEARNED_MAC]->(m)
            OPTIONAL MATCH (owner:Device)-[om:OWNS_MAC]->(m)
            OPTIONAL MATCH (m)-[:HAS_ARP]->(a:ARPEntry)
            WITH m, sw, i, lm, owner, om,
                 [x IN collect(DISTINCT a.ip) WHERE x IS NOT NULL AND x <> ''] AS ips
            RETURN
                m.mac AS mac,
                coalesce(m.vendor, '') AS vendor,
                coalesce(sw.name, '') AS learned_device,
                coalesce(i.name, '') AS learned_port,
                coalesce(lm.vlan, m.vlan, '') AS vlan,
                coalesce(owner.name, '') AS owner_device,
                coalesce(om.nic_name, '') AS owner_nic,
                coalesce(owner.role, '') AS owner_role,
                ips AS ip_addresses,
                coalesce(m.source, CASE WHEN owner IS NOT NULL THEN 'owned_only' ELSE '' END) AS source
            ORDER BY learned_device, learned_port, owner_device
            LIMIT $limit
            """,
            mac=mac,
            limit=max(1, int(limit)),
        )
        rows = await r_rows.data()
    for row in rows:
        ips = row.get("ip_addresses") or []
        row["ip_addresses"] = ", ".join(sorted(ips))
    return {"entries": rows, "count": len(rows)}


async def get_top_problems_inventory() -> list[dict[str, Any]]:
    """Return just the device fields required by MCP ``top_problems``."""
    driver = get_driver()
    async with driver.session() as session:
        res = await session.run(
            """
            MATCH (d:Device)
            WHERE d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false OR (d)--())
            OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps)
            RETURN
                d.name AS name,
                d.mgmt_ip AS mgmt_ip,
                coalesce(d.netbox_site_name, d.platform_container, ps.name, '') AS site,
                toLower(coalesce(d.oper_state, d.status, d.reachabilityStatus, d.admin_state, '')) AS status,
                toLower(coalesce(d.snmp_health,
                    CASE
                      WHEN coalesce(d.snmp_polled, false) = true
                       AND coalesce(d.snmp_direct, false) = true THEN 'full'
                      WHEN coalesce(d.snmp_polled, false) = true THEN 'cloud_only'
                      ELSE 'unpolled'
                    END
                )) AS snmp_health,
                coalesce(d.snmp_restricted_mibs, []) AS snmp_restricted_mibs,
                d.meraki_last_reported_at AS meraki_last_reported_at,
                coalesce(d.meraki_last_reported_at_iso, '') AS meraki_last_reported_at_iso
            ORDER BY d.name
            LIMIT 5000
            """
        )
        return await res.data()


async def get_top_problems_links() -> list[dict[str, Any]]:
    """Return just the transit-edge fields required by MCP ``top_problems``."""
    driver = get_driver()
    types = ["PHYSICAL_LINK", "WAN_UPLINK", "SDWAN_TUNNEL", "VXLAN_TUNNEL"]
    async with driver.session() as session:
        rows: list[dict[str, Any]] = []
        for rel in types:
            res = await session.run(
                f"""
                MATCH (a)-[r:{rel}]->(b)
                RETURN
                    type(r) AS edge_type,
                    coalesce(a.name, a.label, a.id) AS a_name,
                    coalesce(b.name, b.label, b.id) AS b_name,
                    coalesce(r.interface_a_active, r.interface_a, r.wan_slot) AS iface_a,
                    coalesce(r.interface_b_active, r.interface_b) AS iface_b,
                    toLower(coalesce(r.oper_status, r.state, r.status, '')) AS oper_status,
                    r.oper_status_changed_at AS oper_status_changed_at,
                    coalesce(r.oper_status_flap_state, 'stable') AS oper_status_flap_state,
                    coalesce(r.oper_status_flap_count_1h, 0) AS oper_status_flap_count_1h,
                    coalesce(r.oper_status_flap_count_24h, 0) AS oper_status_flap_count_24h,
                    coalesce(r.oper_status_flap_score_1h, 0.0) AS oper_status_flap_score_1h,
                    r.health_score AS health_score,
                    coalesce(r.util_pct, 0.0) AS util_pct,
                    coalesce(r.util_pct_avg_1h, r.util_pct, 0.0) AS util_pct_avg_1h,
                    coalesce(r.error_rate_per_s, 0.0) AS error_rate_per_s,
                    coalesce(r.error_rate_per_s_avg_1h, r.error_rate_per_s, 0.0) AS error_rate_per_s_avg_1h
                """
            )
            rows.extend(await res.data())
    return rows


def _synthesize_stp_name(domain_id: str, vlan: Any, root_mac: str) -> str:
    """Build a human-friendly STP domain name when one is not stored.

    Preference order:
        1. "VLAN <id>" if vlan is set
        2. "Root <mac>" if root_mac is set
        3. The trailing component of the domain id (after the last ':')
        4. The full domain id
    """
    if vlan:
        try:
            return f"VLAN {int(vlan)}"
        except (TypeError, ValueError):
            return f"VLAN {vlan}"
    if root_mac:
        return f"Root {root_mac}"
    if domain_id and ":" in domain_id:
        return domain_id.rsplit(":", 1)[-1]
    return domain_id or "(unnamed)"


async def get_stp_topology() -> dict[str, Any]:
    """Return STP topology data structured for tree visualization.

    Each domain contains:
      - id, name (synthesized if needed), root_mac, protocol, vlan
      - root_device  : {name, id, model} or None
      - members      : [{name, id, priority, root_path_cost, ports: [...]}]
      - port_count   : total ports across all members
      - links        : flat list of all STP_LINK entries (for compatibility)
    """
    driver = get_driver()
    async with driver.session() as session:
        r_domains = await session.run(
            "MATCH (dom:STPDomain) "
            "RETURN dom.id AS id, dom.name AS name, dom.root_bridge_mac AS root_mac, "
            "       dom.bridge_protocol AS protocol, dom.vlan AS vlan, "
            "       dom.source_adapter AS source "
            "LIMIT 1000"
        )
        domains_raw = await r_domains.data()

        r_roots = await session.run(
            "MATCH (d:Device)-[:STP_ROOT]->(dom:STPDomain) "
            "RETURN dom.id AS domain_id, d.name AS root_name, "
            "       d.id AS root_id, d.model AS model "
            "LIMIT 1000"
        )
        roots_by_domain: dict[str, dict] = {
            r["domain_id"]: {"name": r["root_name"], "id": r["root_id"],
                              "model": r.get("model", "")}
            async for r in r_roots
        }

        r_members = await session.run(
            "MATCH (d:Device)-[r:STP_MEMBER]->(dom:STPDomain) "
            "RETURN dom.id AS domain_id, d.name AS dev_name, d.id AS dev_id, "
            "       r.bridge_priority AS priority, r.root_path_cost AS path_cost "
            "LIMIT 5000"
        )
        members_by_domain: dict[str, dict[str, dict]] = {}
        async for m in r_members:
            did = m["domain_id"]
            members_by_domain.setdefault(did, {})
            members_by_domain[did][m["dev_id"]] = {
                "name": m["dev_name"], "id": m["dev_id"],
                "priority": m.get("priority"),
                "root_path_cost": m.get("path_cost"),
                "ports": [],
            }

        # Walk STP_LINK edges and attach each port to its device's member entry
        r_links = await session.run(
            "MATCH (i:Interface)-[r:STP_LINK]->(dom:STPDomain) "
            "OPTIONAL MATCH (d:Device)-[:HAS_INTERFACE]->(i) "
            "RETURN dom.id AS domain_id, "
            "       d.name AS device_name, d.id AS device_id, "
            "       i.name AS port_name, i.id AS port_id, "
            "       r.port_state AS state, r.port_role AS role, "
            "       r.path_cost AS cost, r.priority AS prio "
            "LIMIT 20000"
        )
        all_links: list[dict] = []
        async for lk in r_links:
            did = lk["domain_id"]
            entry = {
                "device": lk.get("device_name") or "",
                "device_id": lk.get("device_id") or "",
                "port": lk.get("port_name") or "",
                "port_id": lk.get("port_id") or "",
                "state": lk.get("state") or "",
                "role": lk.get("role") or "",
                "cost": lk.get("cost"),
                "priority": lk.get("prio"),
            }
            all_links.append({"domain_id": did, **entry})
            # Attach to its member device
            dev_id = lk.get("device_id")
            if dev_id and did in members_by_domain:
                members_by_domain[did].setdefault(dev_id, {
                    "name": lk.get("device_name") or dev_id,
                    "id": dev_id, "priority": None, "root_path_cost": None,
                    "ports": [],
                })
                members_by_domain[did][dev_id]["ports"].append(entry)

    domains = []
    links_by_domain: dict[str, list[dict]] = {}
    for lk in all_links:
        links_by_domain.setdefault(lk["domain_id"], []).append(lk)

    for dom in domains_raw:
        did = dom["id"]
        name = dom.get("name") or _synthesize_stp_name(
            did, dom.get("vlan"), dom.get("root_mac") or ""
        )
        members = list(members_by_domain.get(did, {}).values())
        members.sort(key=lambda m: (m.get("root_path_cost") or 0, m.get("name") or ""))
        for m in members:
            m["ports"].sort(key=lambda p: p.get("port") or "")
        port_count = sum(len(m["ports"]) for m in members)
        domains.append({
            "id": did,
            "name": name,
            "root_mac": dom.get("root_mac") or "",
            "protocol": dom.get("protocol") or "stp",
            "vlan": dom.get("vlan"),
            "source": dom.get("source"),
            "root_device": roots_by_domain.get(did),
            "members": members,
            "port_count": port_count,
            "links": links_by_domain.get(did, []),
        })

    domains.sort(key=lambda d: (0 if d["root_device"] else 1,
                                 -len(d["members"]),
                                 d["name"]))
    return {"domains": domains, "count": len(domains)}


async def get_routing_topology() -> dict[str, Any]:
    """Return L3 routing topology data — devices, prefixes, and routing peers.

    Returns:
      - devices: list of devices with their assigned IP addresses and subnets
      - prefixes: list of subnets with the devices attached to them
      - peers: list of OSPF/BGP/EIGRP routing peer relationships
    """
    driver = get_driver()
    async with driver.session() as session:
        # Devices with their ROUTES_TO (subnet) relationships
        r_prefixes = await session.run(
            "MATCH (d:Device)-[r:ROUTES_TO]->(p:Prefix) "
            "WHERE d.stub IS NULL OR d.stub = false "
            "RETURN d.name AS device, d.id AS device_id, "
            "       coalesce(p.prefix, p.cidr) AS prefix, p.version AS ip_version, "
            "       r.interface AS interface, r.ip AS ip "
            "LIMIT 5000"
        )
        prefix_rows = await r_prefixes.data()

        # Routing peer relationships
        r_peers = await session.run(
            "MATCH (a:Device)-[r:ROUTING_PEER]->(b) "
            "WHERE (a.stub IS NULL OR a.stub = false) "
            "RETURN a.name AS from_device, a.id AS from_id, "
            "       CASE WHEN b:Device THEN b.name ELSE b.name END AS to_name, "
            "       b.id AS to_id, labels(b) AS to_labels, "
            "       r.protocol AS protocol, r.state AS state, "
            "       r.peer_ip AS peer_ip, r.router_id AS router_id, "
            "       r.remote_as AS remote_as "
            "LIMIT 2000"
        )
        peer_rows = await r_peers.data()

    # Build prefix → devices map
    prefix_devices: dict[str, dict] = {}
    for row in prefix_rows:
        pfx = row.get("prefix", "")
        if not pfx:
            continue
        if pfx not in prefix_devices:
            prefix_devices[pfx] = {
                "prefix": pfx,
                "version": row.get("ip_version", "4"),
                "devices": [],
            }
        prefix_devices[pfx]["devices"].append({
            "name": row.get("device", ""),
            "id": row.get("device_id", ""),
            "interface": row.get("interface", ""),
            "ip": row.get("ip", ""),
        })

    peers = [
        {
            "from_device": r.get("from_device", ""),
            "from_id": r.get("from_id", ""),
            "to_name": r.get("to_name", ""),
            "to_id": r.get("to_id", ""),
            "protocol": r.get("protocol", ""),
            "state": r.get("state", ""),
            "peer_ip": r.get("peer_ip", ""),
            "router_id": r.get("router_id", ""),
            "remote_as": r.get("remote_as"),
        }
        for r in peer_rows
    ]

    return {
        "prefixes": list(prefix_devices.values()),
        "peers": peers,
        "prefix_count": len(prefix_devices),
        "peer_count": len(peers),
    }


async def get_vlans(site: str | None = None, device: str | None = None) -> dict[str, Any]:
    """Return VLAN inventory table data with optional site/device filters."""
    driver = get_driver()
    site_q = (site or "").strip().lower() or None
    dev_q = (device or "").strip().lower() or None
    async with driver.session() as session:
        res = await session.run(
            """
            MATCH (v:VLAN)
            OPTIONAL MATCH (d:Device)-[:LOGICAL_MEMBER]->(v)
            WHERE d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps)
            WITH v,
                 [x IN collect(DISTINCT d.name) WHERE x IS NOT NULL AND x <> ''] AS devices,
                 [x IN collect(DISTINCT coalesce(d.netbox_site_name, d.platform_container, ps.name, ''))
                  WHERE x IS NOT NULL AND x <> ''] AS sites
            WHERE (
                $site IS NULL OR
                any(s IN sites WHERE toLower(s) CONTAINS $site)
            )
            AND (
                $device IS NULL OR
                any(n IN devices WHERE toLower(n) CONTAINS $device)
            )
            RETURN
                v.id AS id,
                v.vid AS vid,
                coalesce(v.name, '') AS name,
                coalesce(v.source_adapter, '') AS source_adapter,
                coalesce(v.source, '') AS source,
                coalesce(v.platform_site_id, '') AS platform_site_id,
                devices,
                sites,
                size(devices) AS member_count
            ORDER BY vid ASC, name ASC
            LIMIT 5000
            """,
            site=site_q,
            device=dev_q,
        )
        rows = await res.data()
    return {"vlans": rows, "count": len(rows)}


async def get_links() -> dict[str, Any]:
    """Return a flat list of every "transit" edge for the Links table view.

    A "transit" edge is anything that carries traffic between two
    endpoints: physical cables, WAN uplinks (Internet/AS), SD-WAN
    tunnels, and VXLAN tunnels.  We deliberately do NOT include
    ``ROUTING_PEER`` (that's a control-plane adjacency, not a data
    path) or ``LOGICAL_MEMBER`` (semantic membership, not transit).

    Each row carries the everything an operator or an agent needs to
    answer "is this link healthy?" and "is it flapping?":

      * Endpoints (A side, Z side) with device names and interfaces.
      * Edge type + discovery method + confidence.
      * Health metrics (``health_score``, ``util_pct``, ``error_rate_per_s``).
      * Operational state + ``oper_status_changed_at`` so the UI can
        render "down since 14m ago".
      * Flap stats (``flap_state``, ``flap_count_24h``, ``flap_score_1h``,
        ``oper_status_history``) so the UI can sort by stability and
        render the connectivity strip.
      * Site context on both sides for chip-filter cross-referencing.

    Sorted server-side by ``oper_status_flap_score_1h DESC,
    oper_status_changed_at DESC`` so the most operationally interesting
    rows (currently flapping, then recently changed) land at the top.

    Capped at 5000 rows — way above the typical fleet's transit-edge
    count but enough headroom to handle large multi-site deployments
    without truncating mid-page.
    """
    driver = get_driver()
    types = ["PHYSICAL_LINK", "WAN_UPLINK", "SDWAN_TUNNEL", "VXLAN_TUNNEL"]

    async with driver.session() as session:
        # One MATCH per edge type so we can use the specific relationship
        # filter (faster than a big OR on r:TYPE1|TYPE2|...).  Each
        # branch projects the same column set so the post-processing
        # below is type-agnostic.
        all_rows: list[dict[str, Any]] = []
        for t in types:
            res = await session.run(
                f"""
                MATCH (a)-[r:{t}]->(b)
                RETURN
                    type(r)                            AS edge_type,
                    elementId(r)                       AS edge_id,
                    coalesce(a.name, a.label, a.id)    AS a_name,
                    coalesce(b.name, b.label, b.id)    AS b_name,
                    a.id                                AS a_id,
                    b.id                                AS b_id,
                    labels(a)                           AS a_labels,
                    labels(b)                           AS b_labels,
                    a.netbox_site_name                  AS a_site,
                    b.netbox_site_name                  AS b_site,
                    a.netbox_site_slug                  AS a_site_slug,
                    b.netbox_site_slug                  AS b_site_slug,
                    coalesce(r.interface_a_active, r.interface_a, r.wan_slot) AS iface_a,
                    coalesce(r.interface_b_active, r.interface_b) AS iface_b,
                    coalesce(r.oper_status, r.state, r.status) AS oper_status,
                    r.oper_status_changed_at            AS oper_status_changed_at,
                    r.oper_status_history               AS oper_status_history,
                    r.oper_status_flap_state            AS oper_status_flap_state,
                    r.oper_status_flap_count_1h         AS oper_status_flap_count_1h,
                    r.oper_status_flap_count_24h        AS oper_status_flap_count_24h,
                    r.oper_status_flap_score_1h         AS oper_status_flap_score_1h,
                    r.health_score                      AS health_score,
                    r.util_pct                          AS util_pct,
                    r.util_in_pct                       AS util_in_pct,
                    r.util_out_pct                      AS util_out_pct,
                    r.util_pct_avg_1h                   AS util_pct_avg_1h,
                    r.util_in_pct_avg_1h                AS util_in_pct_avg_1h,
                    r.util_out_pct_avg_1h               AS util_out_pct_avg_1h,
                    r.error_rate_per_s                  AS error_rate_per_s,
                    r.error_rate_per_s_avg_1h           AS error_rate_per_s_avg_1h,
                    r.util_in_pct_history_7d            AS util_in_pct_history_7d,
                    r.util_out_pct_history_7d           AS util_out_pct_history_7d,
                    r.error_rate_per_s_history_7d       AS error_rate_per_s_history_7d,
                    r.speed_mbps                        AS speed_mbps,
                    r.speed_bps                         AS speed_bps,
                    r.media_type                        AS media_type,
                    r.discovery_proto                   AS discovery_proto,
                    r.source                            AS source,
                    r.source_adapter                    AS source_adapter,
                    coalesce(r.confidence, 0.0)         AS confidence,
                    r.l3_prefix_v4                      AS l3_prefix_v4,
                    r.l3_prefix_v6                      AS l3_prefix_v6,
                    r.via                               AS via,
                    r.wan_slot                          AS wan_slot,
                    r.asn                               AS asn,
                    r.public_ip                         AS public_ip,
                    r.peer_ip                           AS peer_ip,
                    r.first_seen                        AS first_seen,
                    r.last_seen                         AS last_seen
                """
            )
            all_rows.extend(await res.data())

    # Server-side sort: flapping first, then most-recently-changed,
    # then by health score.  Coalesce NULLs to neutral defaults so
    # the comparison stays total-ordered.  Doing this in Python keeps
    # the Cypher simple and lets us combine results across types.
    def _sort_key(row: dict[str, Any]) -> tuple:
        flap_score = row.get("oper_status_flap_score_1h") or 0.0
        changed    = row.get("oper_status_changed_at") or 0
        health     = row.get("health_score") or 0
        return (-flap_score, -changed, -health)

    all_rows.sort(key=_sort_key)
    all_rows = all_rows[:5000]

    # Trim labels lists down to the primary type for compact UI
    # rendering and drop None values from the L3 prefix arrays
    # (Neo4j returns ``None`` rather than ``[]`` for unset list props).
    out: list[dict[str, Any]] = []
    for r in all_rows:
        a_labels = r.get("a_labels") or []
        b_labels = r.get("b_labels") or []
        out.append({
            "edge_id":        r.get("edge_id"),
            "edge_type":      r.get("edge_type"),
            "a_name":         r.get("a_name") or "",
            "b_name":         r.get("b_name") or "",
            "a_id":           r.get("a_id") or "",
            "b_id":           r.get("b_id") or "",
            "a_kind":         a_labels[0] if a_labels else "",
            "b_kind":         b_labels[0] if b_labels else "",
            "a_site":         r.get("a_site") or "",
            "b_site":         r.get("b_site") or "",
            "a_site_slug":    r.get("a_site_slug") or "",
            "b_site_slug":    r.get("b_site_slug") or "",
            "iface_a":        r.get("iface_a") or "",
            "iface_b":        r.get("iface_b") or "",
            "oper_status":    r.get("oper_status") or "",
            "oper_status_changed_at":     r.get("oper_status_changed_at"),
            "oper_status_history":        r.get("oper_status_history"),
            "oper_status_flap_state":     r.get("oper_status_flap_state") or "stable",
            "oper_status_flap_count_1h":  r.get("oper_status_flap_count_1h") or 0,
            "oper_status_flap_count_24h": r.get("oper_status_flap_count_24h") or 0,
            "oper_status_flap_score_1h":  r.get("oper_status_flap_score_1h") or 0.0,
            "health_score":     r.get("health_score"),
            "util_pct":         r.get("util_pct"),
            "util_in_pct":      r.get("util_in_pct"),
            "util_out_pct":     r.get("util_out_pct"),
            "util_pct_avg_1h":  r.get("util_pct_avg_1h"),
            "util_in_pct_avg_1h": r.get("util_in_pct_avg_1h"),
            "util_out_pct_avg_1h": r.get("util_out_pct_avg_1h"),
            "error_rate_per_s": r.get("error_rate_per_s"),
            "error_rate_per_s_avg_1h": r.get("error_rate_per_s_avg_1h"),
            "util_in_pct_history_7d": r.get("util_in_pct_history_7d"),
            "util_out_pct_history_7d": r.get("util_out_pct_history_7d"),
            "error_rate_per_s_history_7d": r.get("error_rate_per_s_history_7d"),
            "speed_mbps":       r.get("speed_mbps"),
            "speed_bps":        r.get("speed_bps"),
            "media_type":       r.get("media_type") or "",
            "discovery_proto":  r.get("discovery_proto") or "",
            "source":           r.get("source") or "",
            "source_adapter":   r.get("source_adapter") or "",
            "confidence":       r.get("confidence") or 0.0,
            "l3_prefix_v4":     [p for p in (r.get("l3_prefix_v4") or []) if p],
            "l3_prefix_v6":     [p for p in (r.get("l3_prefix_v6") or []) if p],
            "via":              r.get("via") or "",
            "wan_slot":         r.get("wan_slot") or "",
            "asn":              r.get("asn"),
            "public_ip":        r.get("public_ip") or "",
            "peer_ip":          r.get("peer_ip") or "",
            "first_seen":       r.get("first_seen"),
            "last_seen":        r.get("last_seen"),
        })

    # Type-distribution counts make great quick-summary footer text.
    type_counts: dict[str, int] = {}
    for row in out:
        type_counts[row["edge_type"]] = type_counts.get(row["edge_type"], 0) + 1

    return {
        "links": out,
        "count": len(out),
        "type_counts": type_counts,
    }


async def get_graph_summary() -> dict[str, Any]:
    """Return a text summary of the graph — used as LLM context."""
    stats = await get_graph_stats()
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (d:Device) RETURN d.role AS role, count(d) AS c"
        )
        role_counts = {rec["role"]: rec["c"] async for rec in result}

        result = await session.run(
            "MATCH (s:Site) RETURN properties(s) AS props LIMIT 20"
        )
        site_records = await result.data()
        sites = [r["props"].get("name", "") for r in site_records if r["props"]]

    return {
        "total_devices": stats["nodes"].get("Device", 0),
        "total_interfaces": stats["nodes"].get("Interface", 0),
        "total_vlans": stats["nodes"].get("VLAN", 0),
        "total_vnIs": stats["nodes"].get("VNI", 0),
        "total_vrfs": stats["nodes"].get("VRF", 0),
        "total_mac_addresses": stats["nodes"].get("MACAddress", 0),
        "device_roles": role_counts,
        "sites": sites,
        "relationships": stats["relationships"],
    }


async def get_device_explorer(
    device_key: str,
) -> dict[str, Any]:
    """Return EVERYTHING the graph knows about one device — for debug UI.

    ``device_key`` may be a Device.id (e.g. ``meraki:Q5TY-EB22-LPZG``) or a
    name (case-insensitive, first DNS label).  If multiple devices share a
    name (shouldn't happen after the stub-merger runs but possible during
    a transient cycle) the first match by id, then by name is returned.

    Sections returned (all best-effort — missing data is just an empty list):
      * ``device``       — full property bag
      * ``interfaces``   — list of HAS_INTERFACE-attached Interface nodes
                           with neighbors / IPs / MACs / ARP entries learned
      * ``neighbors``    — PHYSICAL_LINK summary (peer name + interfaces)
      * ``routing``      — ROUTING_PEER edges
      * ``stp``          — STP_MEMBER / STP_ROOT relationships
      * ``vlans``        — VLAN memberships
      * ``prefixes``     — Owned IPv4/IPv6 prefixes
      * ``macs``         — OWNS_MAC entries
      * ``snmp``         — Source coverage details + timestamps
    """
    driver = get_driver()
    async with driver.session() as session:
        # ── Locate the device ────────────────────────────────────────────
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.id = $key
               OR toLower(coalesce(d.name, '')) = toLower($key)
               OR toLower(split(coalesce(d.name, ''), '.')[0]) = toLower($key)
            RETURN properties(d) AS dev
            ORDER BY CASE WHEN d.id = $key THEN 0 ELSE 1 END,
                     coalesce(d.stub, false) ASC
            LIMIT 1
            """,
            key=device_key,
        )
        rec = await result.single()
        if not rec or not rec["dev"]:
            return {"error": f"device not found: {device_key}"}
        dev = dict(rec["dev"])
        dev_id = dev.get("id")

        # ── Interfaces (+ assigned IPs, learned MACs, ARP entries) ───────
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[:HAS_INTERFACE]->(i:Interface)
            OPTIONAL MATCH (i)-[:ASSIGNED_IP]->(ip:IPAddress)
            OPTIONAL MATCH (i)-[:LEARNED_MAC]->(m:MACAddress)
            OPTIONAL MATCH (i)-[:HAS_ARP]->(arp:ARPEntry)
            WITH i,
                 collect(DISTINCT ip.address) AS ips,
                 collect(DISTINCT m.mac)       AS learned_macs,
                 collect(DISTINCT arp {ip: arp.ip, mac: arp.mac, vlan: arp.vlan})
                   AS arps
            RETURN properties(i) AS iface, ips, learned_macs, arps
            ORDER BY i.name LIMIT 1000
            """,
            id=dev_id,
        )
        ifaces = []
        async for row in r:
            iface = dict(row["iface"]) if row["iface"] else {}
            iface["assigned_ips"] = [v for v in (row["ips"] or []) if v]
            iface["learned_macs"] = [v for v in (row["learned_macs"] or []) if v]
            iface["arp_entries"] = [
                e for e in (row["arps"] or []) if e and e.get("ip")
            ]
            ifaces.append(iface)

        # ── Physical neighbors (both directions, undirected collapse) ────
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[r:PHYSICAL_LINK]-(n:Device)
            RETURN n.name AS name, n.id AS id,
                   r.interface_a AS interface_a,
                   r.interface_b AS interface_b,
                   r.discovery_proto AS proto,
                   r.source AS source,
                   r.confidence AS confidence,
                   startNode(r).id = d.id AS d_is_a
            ORDER BY n.name
            """,
            id=dev_id,
        )
        neighbors = []
        async for row in r:
            d_is_a = bool(row["d_is_a"])
            neighbors.append({
                "name": row["name"],
                "id": row["id"],
                "local_interface":
                    row["interface_a"] if d_is_a else row["interface_b"],
                "remote_interface":
                    row["interface_b"] if d_is_a else row["interface_a"],
                "discovery_proto": row["proto"] or "",
                "source": row["source"] or "adapter",
                "confidence": row["confidence"],
            })

        # ── Routing peers ────────────────────────────────────────────────
        # Peers can land on either a real Device node (when the neighbor
        # was discovered separately) or on a RoutingPeer stub (when only
        # this device sees it). Include both so the explorer surfaces
        # everything the routing dimension knows about us.
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[r:ROUTING_PEER]-(p)
            WHERE p:Device OR p:RoutingPeer
            RETURN coalesce(p.name, p.peer_ip, p.router_id, p.id) AS peer,
                   p.id AS peer_id,
                   labels(p) AS peer_labels,
                   r.protocol AS proto, r.state AS state,
                   r.peer_ip AS peer_ip,
                   r.router_id AS router_id, r.remote_as AS remote_as,
                   r.local_as AS local_as, r.address_family AS afi
            ORDER BY r.protocol, peer
            LIMIT 500
            """,
            id=dev_id,
        )
        routing = []
        async for row in r:
            entry = dict(row)
            labels = entry.pop("peer_labels", None) or []
            entry["peer_type"] = "Device" if "Device" in labels else (
                "RoutingPeer" if "RoutingPeer" in labels else "unknown")
            routing.append(entry)

        # ── STP domain memberships ───────────────────────────────────────
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[m:STP_MEMBER|STP_ROOT]->(dom:STPDomain)
            RETURN dom.id AS domain_id, type(m) AS role,
                   m.bridge_priority AS priority,
                   m.root_path_cost  AS path_cost,
                   dom.root_bridge_mac AS root_mac
            """,
            id=dev_id,
        )
        stp = [dict(row) async for row in r]

        # ── VLAN memberships ─────────────────────────────────────────────
        # Different adapters model VLAN membership differently. NDFC/Meraki
        # attach VLANs directly to Devices (LOGICAL_MEMBER); IOS/IOS-XE
        # exposes per-port trunk/access membership via interfaces. Take
        # the union of both patterns and dedupe by vid.
        r = await session.run(
            """
            CALL {
                WITH $id AS id
                MATCH (d:Device {id: id})-[m:LOGICAL_MEMBER|TAGGED|UNTAGGED]->(v:VLAN)
                RETURN v.vid AS vid, v.name AS name, type(m) AS membership
              UNION
                WITH $id AS id
                MATCH (d:Device {id: id})-[:HAS_INTERFACE]->(:Interface)
                      -[m:LOGICAL_MEMBER|TAGGED|UNTAGGED]->(v:VLAN)
                RETURN v.vid AS vid, v.name AS name, type(m) AS membership
              UNION
                // Inventory fallback: devices whose VLAN footprint is
                // stamped on the Device node itself (vlans_configured)
                // instead of emitted as one VLAN node + edge per VID.
                // Meraki MS switches use this path — see
                // _poll_vlans_summary in netcortex/adapters/snmp.py.
                // We synthesize VLAN-style rows from the integer list
                // so the Explorer still sees the full footprint even
                // though no VLAN graph nodes exist for them.
                WITH $id AS id
                MATCH (d:Device {id: id})
                WHERE d.vlans_configured IS NOT NULL
                  AND size(d.vlans_configured) > 0
                UNWIND d.vlans_configured AS vid
                RETURN vid AS vid,
                       'VLAN' + toString(vid) AS name,
                       'INVENTORY' AS membership
            }
            WITH vid, head(collect(name)) AS name,
                 collect(DISTINCT membership) AS memberships
            RETURN vid, name, memberships
            ORDER BY vid LIMIT 1000
            """,
            id=dev_id,
        )
        vlans = [dict(row) async for row in r]

        # ── Prefixes attached to the device ──────────────────────────────
        # Adapters use several relation names: HAS_PREFIX (assigned),
        # ANNOUNCES (BGP/IGP origin), ROUTES_TO (route table entry).
        # Different adapters also pick different scalar names for the CIDR
        # (``cidr`` in newer adapters, ``prefix`` in legacy / SNMP).
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[r:HAS_PREFIX|ANNOUNCES|ROUTES_TO]->(p:Prefix)
            RETURN coalesce(p.cidr, p.prefix) AS cidr,
                   p.scope AS scope,
                   p.name AS name,
                   p.version AS version,
                   type(r) AS via,
                   r.protocol AS protocol,
                   r.next_hop AS next_hop,
                   r.metric AS metric
            ORDER BY cidr LIMIT 500
            """,
            id=dev_id,
        )
        prefixes = [dict(row) async for row in r]

        # ── Owned MACs (NIC MAC addresses) ───────────────────────────────
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[o:OWNS_MAC]->(m:MACAddress)
            RETURN m.mac AS mac, o.nic_name AS nic_name
            LIMIT 100
            """,
            id=dev_id,
        )
        macs = [dict(row) async for row in r]

        # ── Located-at chain (site, location, network) ───────────────────
        r = await session.run(
            """
            MATCH (d:Device {id: $id})-[:LOCATED_AT]->(loc)
            RETURN labels(loc) AS labels, properties(loc) AS props
            LIMIT 10
            """,
            id=dev_id,
        )
        locations = []
        async for row in r:
            locations.append({
                "labels": list(row["labels"] or []),
                "name": (row["props"] or {}).get("name", ""),
                "id":   (row["props"] or {}).get("id", ""),
            })

    # Per-MIB-family coverage map.  Stored as a JSON string on the Device
    # node (Neo4j cannot persist nested dicts as a single property) so we
    # parse it back into a dict for the API response.  When the JSON is
    # missing or unparseable we degrade gracefully to an empty map — the
    # UI handles ``coverage == {}`` by showing "coverage not yet measured".
    import json as _json
    coverage_raw = dev.get("snmp_mib_coverage_json") or ""
    coverage_map: dict[str, dict[str, Any]] = {}
    if coverage_raw:
        try:
            parsed = _json.loads(coverage_raw)
            if isinstance(parsed, dict):
                coverage_map = parsed
        except (ValueError, TypeError):
            coverage_map = {}

    snmp_polled = bool(dev.get("snmp_polled"))
    snmp_direct = bool(dev.get("snmp_direct"))
    health = dev.get("snmp_health") or (
        "full" if (snmp_polled and snmp_direct) else
        ("cloud_only" if snmp_polled else "unpolled")
    )

    snmp = {
        "polled": snmp_polled,
        "source": dev.get("snmp_source"),
        "sources": dev.get("snmp_sources") or [],
        "direct": snmp_direct,
        "cloud": bool(dev.get("snmp_cloud")),
        "last_status": dev.get("snmp_last_status"),
        "polled_at": dev.get("snmp_polled_at"),
        "direct_at": dev.get("snmp_direct_at"),
        "cloud_at": dev.get("snmp_cloud_at"),
        # Coverage diagnostics — used by the device detail panel to show
        # exactly which MIB families the agent let us walk this cycle.
        "health": health,
        "missing_mibs": list(dev.get("snmp_missing_mibs") or []),
        "restricted_mibs": list(dev.get("snmp_restricted_mibs") or []),
        "coverage": coverage_map,
        "coverage_at": dev.get("snmp_mib_coverage_at"),
    }

    return {
        "device": dev,
        "snmp": snmp,
        "interfaces": ifaces,
        "neighbors": neighbors,
        "routing_peers": routing,
        "stp": stp,
        "vlans": vlans,
        "prefixes": prefixes,
        "owned_macs": macs,
        "locations": locations,
        "counts": {
            "interfaces": len(ifaces),
            "neighbors": len(neighbors),
            "routing_peers": len(routing),
            "stp_domains": len(stp),
            "vlans": len(vlans),
            "prefixes": len(prefixes),
            "owned_macs": len(macs),
        },
    }
