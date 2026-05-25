"""Graph ingest — write adapter-discovered data into Neo4j.

Strategy:
  - Nodes: MERGE by id (idempotent, never deletes nodes).
  - Edges: **diff-based** purge.  We MERGE the new payload's edges
    first, then DELETE per-rel-type edges tagged with this adapter
    that are NOT present in the new payload's identity set.  The
    diff key is ``(source_id, target_id)`` for single-edge rel types
    and ``(source_id, target_id, interface_a, interface_b)`` for
    multi-edge types (``PHYSICAL_LINK``) so parallel cables each
    survive on their own identity.

    Why diff-based instead of the older "purge-then-rewrite":
        The previous design deleted every edge owned by this adapter
        before rewriting them in the same transaction-window.  In the
        interval between the DELETE and the re-MERGE, other readers
        (notably the correlator's ``_correlate_via_mac`` /
        ``_correlate_via_arp`` passes) saw the graph in a stale state.
        For example, with the LLDP-reported cat9k1↔fabric-cat9k cable
        momentarily absent, the MAC correlator's
        ``NOT EXISTS { ... discovery_proto IN ['lldp', ...] }`` guard
        would pass and a spurious ``mac_correlation`` PHYSICAL_LINK
        would be MERGEd.  The dedup pass then deleted it, and the
        cycle repeated every ingest tick — leading to the dashed
        "duplicate cable" the operator saw flickering in the topology.
        Diff-based purge never makes an existing edge disappear, so
        the correlator's predicates stay accurate at all times.
  - Incremental writes (Phase C3): each node/edge carries a sha1
    content hash; rows whose hash already matches what's in Neo4j are
    skipped entirely.  This makes a steady-state "nothing changed" cycle
    nearly free for Neo4j.
  - Undirected edge types (PHYSICAL_LINK, STP_LINK, ROUTING_PEER, etc.)
    are canonicalized so two adapters reporting the same link from
    opposite ends collapse to one Neo4j edge. The canonical direction
    is the lexicographic minimum of (source_id, target_id).
"""

from __future__ import annotations

import structlog

from netcortex.graph.client import get_driver
from netcortex.graph.models import GraphData, GraphNode, GraphEdge
from netcortex.ingest.hash import node_hash, edge_hash
from netcortex.util.timestamps import epoch_ms

# Relationship types that are inherently undirected. Edges of these types
# are normalized so source_id <= target_id lexicographically before being
# MERGEd, which prevents two opposite-direction edges between the same
# pair (e.g. cat9k1 -> n9k1 from CDP and n9k1 -> cat9k1 from LLDP).
_UNDIRECTED_REL_TYPES: frozenset[str] = frozenset({
    "PHYSICAL_LINK",
    "STP_LINK",
    "ROUTING_PEER",
})

# Relationship types where MULTIPLE parallel edges between the same
# (source, target) pair are legitimate and must be preserved (e.g. a
# switch with 3 cables to the same neighbor). For these, we MERGE on the
# interface pair as well, so each parallel link survives as its own
# edge. For everything else, MERGE collapses on (src, dst, rel) which is
# the desired behavior (one HAS_INTERFACE per Device→Interface pair,
# one ASSIGNED_IP per Interface→IPAddress pair, etc.).
_MULTI_EDGE_REL_TYPES: frozenset[str] = frozenset({
    "PHYSICAL_LINK",
})

log = structlog.get_logger(__name__)

# How many nodes/edges to write per Cypher transaction
_BATCH_SIZE = 200


def _edge_identity(e: GraphEdge) -> tuple:
    """Identity tuple used to compute and look up an edge's content hash.

    For multi-edge types (PHYSICAL_LINK) the interface pair is part of
    identity, so parallel cables between the same two devices get
    distinct hashes and don't clobber each other in the hash dict.
    """
    rel = e.type.value
    if rel in _MULTI_EDGE_REL_TYPES:
        return (
            e.source_id,
            e.target_id,
            rel,
            str(e.properties.get("interface_a") or ""),
            str(e.properties.get("interface_b") or ""),
        )
    return (e.source_id, e.target_id, rel)


async def ingest_graph_data(data: GraphData) -> None:
    """Ingest all nodes and edges from a GraphData object into Neo4j.

    Order (diff-based purge — see module docstring for the rationale):
        1. Look up content hashes (Phase C3 incremental writes).
        2. MERGE nodes (idempotent upserts by id).
        3. MERGE edges from the new payload (idempotent — keyed by
           ``(src, dst)`` or ``(src, dst, interface_a, interface_b)``
           for multi-edge types so existing edges are updated in place).
        4. DELETE the per-adapter, per-rel-type tail: edges tagged with
           this adapter that are NOT in the new payload's identity set.

    The previous order (purge first, then re-MERGE) left a window during
    which adapter-owned edges did not exist in the graph; the correlator
    could observe that stale state and synthesize duplicate inferred
    edges (mac/arp_correlation) that the dedup pass then had to undo on
    the next tick.  The diff-based pattern eliminates the window because
    existing edges are always observable to other queries while the new
    payload is being applied.
    """
    driver = get_driver()

    # ── Pre-compute content hashes (Phase C3) ────────────────────────────
    node_hashes = {n.id: node_hash(n) for n in data.nodes}

    # Canonicalize undirected edges (so opposite-direction reports of the
    # same link collapse) and compute identity. For multi-edge types
    # (PHYSICAL_LINK) the identity also includes the interface pair so
    # parallel cables between the same pair get distinct hashes.
    _canonicalize_undirected_edges(data.edges)
    edge_keys = [_edge_identity(e) for e in data.edges]
    edge_hashes = {k: edge_hash(e) for k, e in zip(edge_keys, data.edges, strict=False)}

    log.info("graph.ingest_start", adapter=data.adapter_id,
             nodes=len(data.nodes), edges=len(data.edges))

    # Collect which relationship types this adapter is producing this cycle
    rel_types_in_data: set[str] = {e.type.value for e in data.edges}

    skipped_nodes = 0
    written_nodes = 0
    skipped_edges = 0
    written_edges = 0
    purged_edges = 0

    # Single "now" for the entire ingest cycle so all first_seen/last_seen
    # stamps written by this call share the same clock reading. This is
    # important for observability: an operator should see "this batch of
    # 200 LLDP edges all came in at the same instant" rather than each
    # row having a slightly different timestamp.
    now_ms = epoch_ms()

    async with driver.session() as session:
        # ── Step 1: Look up existing content hashes so we can skip no-ops ───
        existing_node_hashes: dict[str, str] = {}
        existing_edge_hashes: dict[tuple, str] = {}
        if node_hashes:
            ids = list(node_hashes.keys())
            for i in range(0, len(ids), 1000):
                batch_ids = ids[i:i+1000]
                res = await session.run(
                    "MATCH (n) WHERE n.id IN $ids "
                    "RETURN n.id AS id, n._content_hash AS h",
                    ids=batch_ids,
                )
                async for rec in res:
                    if rec["h"]:
                        existing_node_hashes[rec["id"]] = rec["h"]
        if data.edges:
            edge_rows_by_rel: dict[str, list[dict[str, str]]] = {}
            for e in data.edges:
                rel = e.type.value
                row: dict[str, str] = {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                }
                if rel in _MULTI_EDGE_REL_TYPES:
                    row["interface_a"] = str(e.properties.get("interface_a") or "")
                    row["interface_b"] = str(e.properties.get("interface_b") or "")
                edge_rows_by_rel.setdefault(rel, []).append(row)

            for rel, rows in edge_rows_by_rel.items():
                for i in range(0, len(rows), 1000):
                    batch_rows = rows[i:i+1000]
                    if rel in _MULTI_EDGE_REL_TYPES:
                        res = await session.run(
                            f"""
                            UNWIND $rows AS row
                            MATCH (src {{id: row.source_id}})
                                  -[r:{rel} {{
                                      interface_a: row.interface_a,
                                      interface_b: row.interface_b
                                  }}]->
                                  (dst {{id: row.target_id}})
                            RETURN row.source_id AS source_id,
                                   row.target_id AS target_id,
                                   row.interface_a AS interface_a,
                                   row.interface_b AS interface_b,
                                   r._content_hash AS h
                            """,
                            rows=batch_rows,
                        )
                        async for rec in res:
                            if rec["h"]:
                                k = (
                                    rec["source_id"],
                                    rec["target_id"],
                                    rel,
                                    rec["interface_a"],
                                    rec["interface_b"],
                                )
                                existing_edge_hashes[k] = rec["h"]
                    else:
                        res = await session.run(
                            f"""
                            UNWIND $rows AS row
                            MATCH (src {{id: row.source_id}})-[r:{rel}]->
                                  (dst {{id: row.target_id}})
                            RETURN row.source_id AS source_id,
                                   row.target_id AS target_id,
                                   r._content_hash AS h
                            """,
                            rows=batch_rows,
                        )
                        async for rec in res:
                            if rec["h"]:
                                k = (rec["source_id"], rec["target_id"], rel)
                                existing_edge_hashes[k] = rec["h"]

        # ── Step 2: MERGE nodes (skip rows whose hash matches) ───────────────
        nodes_to_write = [
            n for n in data.nodes
            if existing_node_hashes.get(n.id) != node_hashes[n.id]
        ]
        nodes_to_touch_only = [
            n for n in data.nodes
            if existing_node_hashes.get(n.id) == node_hashes[n.id]
        ]
        skipped_nodes = len(nodes_to_touch_only)
        written_nodes = len(nodes_to_write)
        for i in range(0, len(nodes_to_write), _BATCH_SIZE):
            batch = nodes_to_write[i : i + _BATCH_SIZE]
            await _merge_nodes(session, batch, node_hashes, now_ms=now_ms)
        # For nodes we DID NOT rewrite (hash matched), still bump
        # last_seen so the operator can see the freshness independently
        # of whether the content changed. Without this we can't tell
        # "no change since last cycle" from "node disappeared from this
        # adapter's view".
        if nodes_to_touch_only:
            for i in range(0, len(nodes_to_touch_only), _BATCH_SIZE):
                batch = nodes_to_touch_only[i : i + _BATCH_SIZE]
                await _touch_last_seen_nodes(session, batch, now_ms=now_ms)

        # ── Step 3: MERGE edges from this payload ───────────────────────────
        # MERGE keys on (src, dst) for normal rel types and on the
        # interface pair as well for multi-edge types, so re-running with
        # the same payload is a no-op (each edge is updated in place
        # rather than recreated).  Existing edges from a previous cycle
        # that still appear in the new payload are touched up here; the
        # ones that disappeared get cleaned up in Step 4.
        edges_to_write = [
            e for e in data.edges
            if existing_edge_hashes.get(_edge_identity(e)) != edge_hashes[_edge_identity(e)]
        ]
        edges_to_touch_only = [
            e for e in data.edges
            if existing_edge_hashes.get(_edge_identity(e)) == edge_hashes[_edge_identity(e)]
        ]
        written_edges = len(edges_to_write)
        skipped_edges = len(edges_to_touch_only)
        for i in range(0, len(edges_to_write), _BATCH_SIZE):
            batch = edges_to_write[i : i + _BATCH_SIZE]
            await _merge_edges(session, batch, edge_hashes, now_ms=now_ms)
        if edges_to_touch_only:
            for i in range(0, len(edges_to_touch_only), _BATCH_SIZE):
                batch = edges_to_touch_only[i : i + _BATCH_SIZE]
                await _touch_last_seen_edges(session, batch, now_ms=now_ms)

        # ── Step 4: Diff-based purge of the per-(adapter, rel) tail ─────────
        # For each rel_type in the new payload, delete edges tagged with
        # this adapter that are NOT in the payload's identity set.
        # Match by full instance id (e.g. "meraki/CPN") OR by adapter
        # family (e.g. "snmp") because some legacy helpers emit edges
        # tagged with just the family name.  This guarantees stale edges
        # from prior discovery cycles are wiped no matter which form was
        # used while never deleting an edge that is still current.
        adapter_family = data.adapter_id.split("/", 1)[0]
        keep_keys_per_rel = _build_keep_keys(data.edges)
        for rel_type in rel_types_in_data:
            keep = keep_keys_per_rel.get(rel_type, [])
            if rel_type in _MULTI_EDGE_REL_TYPES:
                # Identity = (src, dst, interface_a, interface_b).
                # Use coalesce on the interface props to mirror how
                # _merge_edges normalises NULL → "" before MERGE.
                cypher = (
                    f"MATCH (a)-[r:{rel_type}]->(b) "
                    "WHERE (r.source_adapter = $adapter "
                    "       OR r.source_adapter = $family) "
                    "  AND NOT [a.id, b.id, "
                    "           coalesce(r.interface_a, ''), "
                    "           coalesce(r.interface_b, '')] IN $keep "
                    "WITH collect(r) AS rs "
                    "FOREACH (x IN rs | DELETE x) "
                    "RETURN size(rs) AS n"
                )
            else:
                # Identity = (src, dst).
                cypher = (
                    f"MATCH (a)-[r:{rel_type}]->(b) "
                    "WHERE (r.source_adapter = $adapter "
                    "       OR r.source_adapter = $family) "
                    "  AND NOT [a.id, b.id] IN $keep "
                    "WITH collect(r) AS rs "
                    "FOREACH (x IN rs | DELETE x) "
                    "RETURN size(rs) AS n"
                )
            res = await session.run(
                cypher,
                adapter=data.adapter_id,
                family=adapter_family,
                keep=keep,
            )
            rec = await res.single()
            if rec:
                purged_edges += rec["n"] or 0

    log.info(
        "graph.ingest_done",
        adapter=data.adapter_id,
        nodes_written=written_nodes,
        nodes_skipped_unchanged=skipped_nodes,
        edges_written=written_edges,
        edges_skipped_unchanged=skipped_edges,
        edges_purged_stale=purged_edges,
    )


def _build_keep_keys(edges: list[GraphEdge]) -> dict[str, list[list]]:
    """Build per-rel-type "keep" lists used by the diff-based purge.

    For each rel type, returns a list of identity tuples (as plain
    Python lists, since Neo4j parameter encoding prefers lists over
    tuples) — ``[src_id, dst_id]`` for normal rel types and
    ``[src_id, dst_id, interface_a, interface_b]`` for multi-edge types.

    The interface values default to ``""`` to match how ``_merge_edges``
    normalises missing/None interface props before MERGE, so the diff
    query's ``coalesce(r.interface_a, '') IN $keep`` lookup hits the
    same key the MERGE used to write the edge.
    """
    keep: dict[str, list[list]] = {}
    for e in edges:
        rel = e.type.value
        if rel in _MULTI_EDGE_REL_TYPES:
            ia = e.properties.get("interface_a") or ""
            ib = e.properties.get("interface_b") or ""
            keep.setdefault(rel, []).append(
                [e.source_id, e.target_id, ia, ib]
            )
        else:
            keep.setdefault(rel, []).append([e.source_id, e.target_id])
    return keep


def _canonicalize_undirected_edges(edges: list[GraphEdge]) -> None:
    """In-place: orient undirected edges so source_id <= target_id.

    Cisco-family L2 links commonly get reported from BOTH ends:
      * cat9k1 LLDP says   cat9k1  -> n9k1  via Twe1/1/5
      * n9k1   CDP  says   n9k1    -> cat9k1 via TwentyFiveGigE1/1/5
    Storing both as directed edges in Neo4j produces "duplicate" links in
    the UI. By flipping every undirected edge so the lexicographically
    smaller node id is always the source, both reports MERGE onto the
    same Neo4j edge. We also swap the interface_a/interface_b properties
    (and *_raw variants) so the stored values stay consistent with the
    new direction.
    """
    swap_keys = {
        "interface_a": "interface_b",
        "interface_a_raw": "interface_b_raw",
        "port_a": "port_b",
        "side_a": "side_b",
    }
    for e in edges:
        if e.type.value not in _UNDIRECTED_REL_TYPES:
            continue
        if e.source_id <= e.target_id:
            continue
        e.source_id, e.target_id = e.target_id, e.source_id
        for ka, kb in swap_keys.items():
            if ka in e.properties or kb in e.properties:
                va = e.properties.pop(ka, None)
                vb = e.properties.pop(kb, None)
                if va is not None:
                    e.properties[kb] = va
                if vb is not None:
                    e.properties[ka] = vb


async def _merge_nodes(
    session,
    nodes: list[GraphNode],
    hashes: dict[str, str],
    *,
    now_ms: int,
) -> None:
    """MERGE nodes by id, SET all properties, stamp first_seen / last_seen.

    ``first_seen`` is set only on CREATE so it remains the moment the
    object first entered the graph.  ``last_seen`` is refreshed on every
    write so the operator can age out objects that haven't been observed
    in a while.
    """
    by_type: dict[str, list[dict]] = {}
    for node in nodes:
        label = node.type.value
        by_type.setdefault(label, [])
        props = {
            "id": node.id,
            **node.properties,
            "dimensions": [d.value for d in node.dimensions],
            "source_adapter": node.source_adapter,
            "_content_hash": hashes.get(node.id, ""),
        }
        if node.netbox_id is not None:
            props["netbox_id"] = node.netbox_id
        if node.netbox_type is not None:
            props["netbox_type"] = node.netbox_type
        by_type[label].append(props)

    for label, rows in by_type.items():
        # For Device nodes specifically, also track when ``status``
        # transitions so the UI can render "down since X".  We capture
        # the pre-merge value via WITH and then conditionally stamp
        # ``status_changed_at``.  Doing this in one MERGE-and-SET
        # pipeline keeps the change atomic and avoids a second
        # round-trip for the comparison.
        if label == "Device":
            cypher = (
                "UNWIND $rows AS row "
                f"MERGE (n:{label} {{id: row.id}}) "
                "WITH n, row, n.status AS prev_status "
                "SET n += row, n.last_seen = $now "
                "FOREACH (_ IN CASE WHEN n.first_seen IS NULL THEN [1] ELSE [] END | "
                "  SET n.first_seen = $now) "
                "FOREACH (_ IN CASE "
                "         WHEN coalesce(row.status,'') <> coalesce(prev_status,'') "
                "          AND row.status IS NOT NULL "
                "         THEN [1] ELSE [] END | "
                "  SET n.status_changed_at = $now)"
            )
        else:
            cypher = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{label} {{id: row.id}}) "
                f"ON CREATE SET n.first_seen = $now "
                f"SET n += row, n.last_seen = $now"
            )
        await session.run(cypher, rows=rows, now=now_ms)


async def _touch_last_seen_nodes(
    session,
    nodes: list[GraphNode],
    *,
    now_ms: int,
) -> None:
    """Refresh ``last_seen`` on nodes whose content hash already matched.

    These nodes are still being observed by the adapter; we just don't
    need to rewrite all their properties.  Without this touch the node
    would look as stale as one the adapter never returned.

    Also sets ``first_seen`` for legacy nodes that pre-date this
    refactor — they'll get ``first_seen = $now`` once, which is the
    best we can do (we don't know when they actually appeared, only
    that we've seen them by now).
    """
    ids = [n.id for n in nodes]
    if not ids:
        return
    cypher = (
        "UNWIND $ids AS nid "
        "MATCH (n {id: nid}) "
        "SET n.last_seen = $now "
        # Legacy nodes from before the timestamp refactor don't have
        # a first_seen; backfill it with $now so the property is
        # always present (operator can see "we've been tracking this
        # since at least <now>" instead of NULL).
        "FOREACH (_ IN CASE WHEN n.first_seen IS NULL THEN [1] ELSE [] END | "
        "  SET n.first_seen = $now)"
    )
    await session.run(cypher, ids=ids, now=now_ms)


async def _merge_edges(
    session,
    edges: list[GraphEdge],
    hashes: dict[tuple, str],
    *,
    now_ms: int,
) -> None:
    """MERGE edges by (source.id, target.id, relationship type) — plus
    the interface pair for multi-edge types so parallel cables survive.

    Stamps ``first_seen`` on create and ``last_seen`` on every observation
    so the UI can answer "how long has this cable been in the graph?"
    and the worker can age out edges that disappear.
    """
    by_type: dict[str, list[dict]] = {}
    for edge in edges:
        rel = edge.type.value
        by_type.setdefault(rel, [])
        props = {
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            **edge.properties,
            "_content_hash": hashes.get(_edge_identity(edge), ""),
        }
        if edge.dimension:
            props["dimension"] = edge.dimension.value
        if edge.source_adapter:
            props["source_adapter"] = edge.source_adapter
        by_type[rel].append(props)

    for rel, rows in by_type.items():
        if rel in _MULTI_EDGE_REL_TYPES:
            # MERGE on the (canonicalized) interface pair so parallel
            # cables between the same two devices each become a distinct
            # edge. interface_a/interface_b are normalized by the
            # adapters and re-oriented by _canonicalize_undirected_edges
            # so they're stable keys. Empty strings replace nulls so
            # MERGE keys never resolve to NULL (which would prevent the
            # pattern from matching even an identical row on the next
            # cycle).
            for row in rows:
                row.setdefault("interface_a", "")
                row.setdefault("interface_b", "")
                if row["interface_a"] is None:
                    row["interface_a"] = ""
                if row["interface_b"] is None:
                    row["interface_b"] = ""
            cypher = (
                f"UNWIND $rows AS row "
                f"MATCH (src {{id: row.source_id}}) "
                f"MATCH (dst {{id: row.target_id}}) "
                f"MERGE (src)-[r:{rel} {{"
                f"  interface_a: row.interface_a, "
                f"  interface_b: row.interface_b"
                f"}}]->(dst) "
                f"ON CREATE SET r.first_seen = $now "
                f"SET r += row, r.last_seen = $now"
            )
        else:
            cypher = (
                f"UNWIND $rows AS row "
                f"MATCH (src {{id: row.source_id}}) "
                f"MATCH (dst {{id: row.target_id}}) "
                f"MERGE (src)-[r:{rel}]->(dst) "
                f"ON CREATE SET r.first_seen = $now "
                f"SET r += row, r.last_seen = $now"
            )
        await session.run(cypher, rows=rows, now=now_ms)


async def _touch_last_seen_edges(
    session,
    edges: list[GraphEdge],
    *,
    now_ms: int,
) -> None:
    """Refresh ``last_seen`` on unchanged edges whose hashes matched."""
    by_type: dict[str, list[dict[str, str]]] = {}
    for edge in edges:
        rel = edge.type.value
        row: dict[str, str] = {
            "source_id": edge.source_id,
            "target_id": edge.target_id,
        }
        if rel in _MULTI_EDGE_REL_TYPES:
            row["interface_a"] = str(edge.properties.get("interface_a") or "")
            row["interface_b"] = str(edge.properties.get("interface_b") or "")
        by_type.setdefault(rel, []).append(row)

    for rel, rows in by_type.items():
        if rel in _MULTI_EDGE_REL_TYPES:
            cypher = (
                f"UNWIND $rows AS row "
                f"MATCH (src {{id: row.source_id}})-[r:{rel} {{"
                f"  interface_a: row.interface_a, "
                f"  interface_b: row.interface_b"
                f"}}]->(dst {{id: row.target_id}}) "
                "SET r.last_seen = $now "
                "FOREACH (_ IN CASE WHEN r.first_seen IS NULL THEN [1] ELSE [] END | "
                "  SET r.first_seen = $now)"
            )
        else:
            cypher = (
                f"UNWIND $rows AS row "
                f"MATCH (src {{id: row.source_id}})-[r:{rel}]->(dst {{id: row.target_id}}) "
                "SET r.last_seen = $now "
                "FOREACH (_ IN CASE WHEN r.first_seen IS NULL THEN [1] ELSE [] END | "
                "  SET r.first_seen = $now)"
            )
        await session.run(cypher, rows=rows, now=now_ms)
