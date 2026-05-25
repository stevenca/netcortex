"""MCP tools for agentic ops — the 9 baseline tools an LLM / agent
needs to answer ~80% of operator-grade network questions.

Design principles (kept short on purpose, per the workspace MCP-security
rule, "no `do_anything` tools"):

* **Single-purpose** — each tool answers one diagnostic question.
* **Bounded output** — every tool caps to a small page (default 50 rows)
  with an explicit ``limit`` arg and a truncation indicator so an agent
  can paginate if it actually needs more.
* **Stable field names** — fields match the REST API surface
  (``oper_status``, ``oper_status_flap_state``, etc.) so the same
  docs describe both surfaces.
* **Self-explaining** — docstrings name the diagnostic question and
  cite the top-20-problems mapping in CHANGELOG so an agent's tool-
  selection prompt picks the right tool.
* **Delegates to existing graph queries** — zero business logic in
  this file; we are the thin MCP layer the workspace rule mandates.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from netcortex.mcp.server import mcp

log = structlog.get_logger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
# Default page size for every tool.  Small enough that an LLM can read
# the whole response without truncation; large enough that a typical
# fleet's worth of "down" or "flapping" links fits in one call.
_DEFAULT_LIMIT = 50
_MAX_LIMIT     = 500
_ALLOWED_HISTORY_FIELDS = frozenset({"status", "oper_status", "stp_state"})


def _clamp_limit(n: int | None) -> int:
    """Bound a caller-supplied limit to [1, _MAX_LIMIT]."""
    if not n or n <= 0:
        return _DEFAULT_LIMIT
    return min(int(n), _MAX_LIMIT)


#: Severities the staleness policy is allowed to emit.  ``"filter"`` is
#: special-cased to drop the problem entirely; everything else is a
#: real severity bucket consumed by the top_problems ranker.
_ALLOWED_STALE_SEVERITIES = frozenset({"critical", "warning", "info", "filter"})


def _apply_staleness_policy(
    severity: str,
    last_reported_at_ms: int | None,
    now_ms: int,
    threshold_seconds: int,
    stale_severity: str,
) -> str | None:
    """Return the adjusted severity for a problem, or ``None`` to drop it.

    The "staleness" policy demotes (or filters) problems whose
    underlying device has not reported to its source-of-truth in a
    long time.  Concretely, an MX appliance whose Meraki Dashboard
    ``lastReportedAt`` is months in the past is almost always
    abandoned inventory, not a live incident — even though the
    Dashboard still answers "wan1 is down" forever.  Without this
    helper such devices show up as ``critical`` link_down events and
    drown out genuinely actionable signal.

    The contract is intentionally pure so it is trivially testable
    and easy to reason about:

      * ``severity``               — the would-be problem severity if
                                     no staleness policy applied.
      * ``last_reported_at_ms``    — when the source-of-truth last
                                     observed the device, in epoch ms.
                                     ``None`` means we have no signal
                                     either way and the policy is a
                                     no-op.
      * ``now_ms``                 — current wall clock, in epoch ms.
                                     Threaded in rather than captured
                                     so tests can pin time.
      * ``threshold_seconds``      — staleness threshold; a problem is
                                     "stale" if ``now_ms -
                                     last_reported_at_ms`` exceeds
                                     this.  ``<= 0`` disables the
                                     policy entirely (passthrough).
      * ``stale_severity``         — severity to assign to stale
                                     problems.  The literal string
                                     ``"filter"`` drops the problem.

    Returns the original severity when the policy does not apply, the
    configured ``stale_severity`` when the problem is stale, or
    ``None`` when the caller should skip emitting the problem
    entirely.
    """
    if last_reported_at_ms is None or threshold_seconds <= 0:
        return severity
    age_ms = now_ms - int(last_reported_at_ms)
    if age_ms <= threshold_seconds * 1000:
        return severity
    if stale_severity == "filter":
        return None
    if stale_severity not in _ALLOWED_STALE_SEVERITIES:
        # Defensive fallback: invalid config should never silently
        # break a check — leave the original severity in place.
        return severity
    return stale_severity


def _truncated(rows: list[Any], limit: int, total: int) -> dict[str, Any]:
    """Standard envelope so every tool reports truncation consistently."""
    return {
        "truncated": len(rows) < total,
        "returned":  len(rows),
        "total":     total,
        "limit":     limit,
    }


def _parse_link_pair(raw: str) -> tuple[str, str] | None:
    """Parse a user-supplied link identity into `(a_name, b_name)`."""
    s = (raw or "").strip()
    if not s:
        return None
    for delim in (" <-> ", " ⇄ ", "|", "->", "<-"):
        if delim in s:
            a, b = [x.strip() for x in s.split(delim, 1)]
            if a and b:
                return a, b
    # Backward-compatible fallback: "a-b". Use a single split so
    # hostnames containing dashes can still be represented as "a-b-c--x-y".
    if "--" in s:
        a, b = [x.strip() for x in s.split("--", 1)]
        if a and b:
            return a, b
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1: inventory.list — "What devices exist and what's their state?"
# Maps to top-20 problems: #5 (device unreachable), #6 (SNMP coverage),
#                          #7 (Wi-Fi at site).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def inventory_list(
    site: str | None = None,
    role: str | None = None,
    status: str | None = None,
    flap_state: str | None = None,
    snmp_health: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    """List devices with state, optionally filtered.

    Use this tool when the agent needs to know: "what's at site X?",
    "which devices are down?", "which devices have unreliable SNMP
    coverage?", "which devices are flapping right now?".

    Args:
        site:        Restrict to a site (matches NetBox site name OR slug).
        role:        Restrict to a role (e.g. 'switch', 'router', 'ap').
        status:      Restrict to a status value ('active', 'offline', ...).
        flap_state:  'stable' | 'unstable' | 'flapping' from Phase A.
        snmp_health: 'full' | 'partial' | 'restricted' | 'unreachable' |
                     'cloud_only' | 'unpolled'.  Surfaces monitoring gaps.
        limit:       Max devices to return (default 50, cap 500).

    Returns:
        ``{"devices": [{name, role, mgmt_ip, site, status,
        status_flap_state, status_flap_count_24h, snmp_health, ...}, ...],
        "truncated": ..., "returned": ..., "total": ..., "limit": ...}``
    """
    try:
        from netcortex.graph.query import get_inventory
        data = await get_inventory()
        rows = data.get("devices") or []

        # Apply filters in Python — small dataset, simpler than
        # building dynamic Cypher and easier for the agent to reason
        # about the filter semantics.
        def _matches(d: dict) -> bool:
            if site:
                s = site.lower()
                if (d.get("site", "").lower() != s
                    and d.get("site_slug", "").lower() != s
                    and d.get("platform_site", "").lower() != s):
                    return False
            if role and d.get("role", "").lower() != role.lower():
                return False
            if status and d.get("status", "").lower() != status.lower():
                return False
            if snmp_health and d.get("snmp_health", "") != snmp_health.lower():
                return False
            if flap_state:
                # Inventory query doesn't carry flap_state today —
                # re-derive from the underlying field if missing.
                if d.get("status_flap_state", "stable") != flap_state.lower():
                    return False
            return True

        filtered = [d for d in rows if _matches(d)]
        total = len(filtered)
        limit = _clamp_limit(limit)
        out = filtered[:limit]

        # Project just the agent-relevant fields.  Full payload is
        # available via /api/inventory if the agent needs to drill in.
        slim = [{
            "name":        d.get("name"),
            "role":        d.get("role"),
            "mgmt_ip":     d.get("mgmt_ip"),
            "site":        d.get("site"),
            "platform":    d.get("platform"),
            "model":       d.get("model"),
            "status":      d.get("status"),
            "snmp_health": d.get("snmp_health"),
            "vendor":      d.get("vendor"),
            "os_version":  d.get("os_version"),
            "adapter":     d.get("source_adapter"),
        } for d in out]
        return {"devices": slim, **_truncated(slim, limit, total)}
    except Exception as exc:
        log.error("mcp.inventory_list.failed", error=str(exc))
        return {"error": f"inventory_list failed: {exc}",
                "devices": [], "returned": 0, "total": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: topology.get — "How is X connected?"
# Maps to top-20 problems: most diagnostic flows start here.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def topology_get(
    device: str,
    hops: int = 1,
) -> dict:
    """Return the subgraph around one device — neighbors, interfaces,
    VLANs, peers, and tunnels — out to ``hops`` cable-distance.

    Use this tool when the agent needs to understand the connectivity
    of a specific device: "what does cat9k1 connect to?", "show me
    the L1/L2 neighborhood of mx1", "what VLANs reach this switch?".

    Args:
        device: Device hostname (exact match preferred; partials
                may match the closest single hit).
        hops:   Number of relationship hops to traverse (1–4, default 1).

    Returns:
        ``{"device": {...}, "neighbors": [...], "interfaces": [...],
        "vlans": [...], "vrfs": [...], "bgp_peers": [...],
        "sdwan_tunnels": [...], "graph": {"nodes": [...], "edges": [...]}}``

        The ``graph`` sub-payload is Cytoscape-shaped and can be
        re-rendered by any compatible UI.
    """
    try:
        from netcortex.graph.query import get_device_context
        return await get_device_context(device, hops=max(1, min(int(hops or 1), 4)))
    except Exception as exc:
        log.error("mcp.topology_get.failed", device=device, error=str(exc))
        return {"error": f"topology_get failed: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3: links.list — "Which cables are flapping / down / over-utilised?"
# Maps to top-20 problems: #1 (link flapping), #3 (high util),
#                          #4 (high errors), #8 (WAN), #9 (SD-WAN).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def links_list(
    status: str | None = None,
    edge_type: str | None = None,
    flap_state: str | None = None,
    min_flap_score: float = 0.0,
    min_util: float = 0.0,
    min_error_rate: float = 0.0,
    site: str | None = None,
    device: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    """List transit edges (cables, WAN uplinks, SD-WAN tunnels) with
    health, flap stats, and operational state.

    Pre-sorted server-side by flap_score → recency → health so the
    most operationally urgent rows surface first WITHOUT additional
    filter args.

    Use this tool when the agent needs to find unhealthy data-plane
    edges: "which cables are flapping?", "any saturated links?",
    "show me down WAN uplinks", "anything with high error rates?".

    Args:
        status:         'up' | 'down' (current oper_status).
        edge_type:      'PHYSICAL_LINK' | 'WAN_UPLINK' | 'SDWAN_TUNNEL' | 'VXLAN_TUNNEL'.
        flap_state:     'stable' | 'unstable' | 'flapping'.
        min_flap_score: 0.0–1.0; ≥0.5 = clearly flapping in last hour.
        min_util:       Min utilisation percent (0–100).
        min_error_rate: Min error rate per second.
        site:           Restrict to links touching a site (either side).
        device:         Restrict to links touching a device (either side).
        limit:          Max links to return (default 50, cap 500).

    Returns:
        ``{"links": [{a_name, iface_a, b_name, iface_b, edge_type,
        oper_status, oper_status_flap_state, oper_status_flap_count_24h,
        oper_status_changed_at, health_score, util_pct, error_rate_per_s,
        a_site, b_site, l3_prefix_v4, l3_prefix_v6, discovery_proto, ...},
        ...], "truncated": ..., ...}``
    """
    try:
        from netcortex.graph.query import get_links
        data = await get_links()
        rows = data.get("links") or []

        def _matches(r: dict) -> bool:
            if status and (r.get("oper_status") or "").lower() != status.lower():
                return False
            if edge_type and r.get("edge_type") != edge_type:
                return False
            if flap_state and r.get("oper_status_flap_state") != flap_state.lower():
                return False
            if (r.get("oper_status_flap_score_1h") or 0.0) < float(min_flap_score):
                return False
            if (r.get("util_pct") or 0.0) < float(min_util):
                return False
            if (r.get("error_rate_per_s") or 0.0) < float(min_error_rate):
                return False
            if site:
                s = site.lower()
                if (r.get("a_site", "").lower() != s
                    and r.get("b_site", "").lower() != s
                    and r.get("a_site_slug", "").lower() != s
                    and r.get("b_site_slug", "").lower() != s):
                    return False
            if device:
                d = device.lower()
                if (r.get("a_name", "").lower() != d
                    and r.get("b_name", "").lower() != d):
                    return False
            return True

        filtered = [r for r in rows if _matches(r)]
        total = len(filtered)
        limit = _clamp_limit(limit)
        out = filtered[:limit]

        slim = [{
            "edge_type":        r.get("edge_type"),
            "a_name":           r.get("a_name"),
            "iface_a":          r.get("iface_a"),
            "b_name":           r.get("b_name"),
            "iface_b":          r.get("iface_b"),
            "oper_status":      r.get("oper_status"),
            "oper_status_changed_at":     r.get("oper_status_changed_at"),
            "oper_status_flap_state":     r.get("oper_status_flap_state"),
            "oper_status_flap_count_1h":  r.get("oper_status_flap_count_1h"),
            "oper_status_flap_count_24h": r.get("oper_status_flap_count_24h"),
            "oper_status_flap_score_1h":  r.get("oper_status_flap_score_1h"),
            "health_score":     r.get("health_score"),
            "util_pct":         r.get("util_pct"),
            "util_pct_avg_1h":  r.get("util_pct_avg_1h"),
            "util_in_pct_avg_1h": r.get("util_in_pct_avg_1h"),
            "util_out_pct_avg_1h": r.get("util_out_pct_avg_1h"),
            "error_rate_per_s": r.get("error_rate_per_s"),
            "error_rate_per_s_avg_1h": r.get("error_rate_per_s_avg_1h"),
            "util_in_pct_history_7d": r.get("util_in_pct_history_7d"),
            "util_out_pct_history_7d": r.get("util_out_pct_history_7d"),
            "error_rate_per_s_history_7d": r.get("error_rate_per_s_history_7d"),
            # Threshold metadata mirrors UI overlays so a model can
            # reason with the same semantics a human sees in-table.
            "thresholds": {
                "util_pct_avg_1h": {"warn": 75.0, "hot": 85.0, "critical": 95.0},
                "error_rate_per_s_avg_1h": {"warn": 1.0, "critical": 5.0},
            },
            "breaches": {
                "util_warn": (r.get("util_pct_avg_1h") or 0.0) >= 75.0,
                "util_hot": (r.get("util_pct_avg_1h") or 0.0) >= 85.0,
                "util_critical": (r.get("util_pct_avg_1h") or 0.0) >= 95.0,
                "err_warn": (r.get("error_rate_per_s_avg_1h") or 0.0) >= 1.0,
                "err_critical": (r.get("error_rate_per_s_avg_1h") or 0.0) >= 5.0,
            },
            "speed_mbps":       r.get("speed_mbps"),
            "a_site":           r.get("a_site"),
            "b_site":           r.get("b_site"),
            "l3_prefix_v4":     r.get("l3_prefix_v4"),
            "l3_prefix_v6":     r.get("l3_prefix_v6"),
            "discovery_proto":  r.get("discovery_proto"),
            # Provenance + WAN-uplink slot fields:
            #   - source_adapter lets agents tell adapter-discovered cables
            #     (meraki, catc, snmp) from correlator-built edges
            #     (WAN_UPLINK to Internet, AS boundary peers).
            #   - wan_slot disambiguates dual-WAN MX uplinks where the
            #     same (Device → Internet) pair is emitted once per slot.
            #   - via is the discovery rule (mx_uplink | ebgp) that
            #     produced a correlator-built uplink.
            "source_adapter":   r.get("source_adapter"),
            "wan_slot":         r.get("wan_slot"),
            "via":              r.get("via"),
        } for r in out]
        return {"links": slim, **_truncated(slim, limit, total)}
    except Exception as exc:
        log.error("mcp.links_list.failed", error=str(exc))
        return {"error": f"links_list failed: {exc}",
                "links": [], "returned": 0, "total": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4: peers.list — "Which routing adjacencies are down or unstable?"
# Maps to top-20 problems: #2 (BGP/OSPF peer down/flapping).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def peers_list(
    state: str | None = None,
    flap_state: str | None = None,
    protocol: str | None = None,
    device: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    """List routing peer adjacencies (BGP, OSPF, etc.) with current
    state and flap stats.

    Use this tool when the agent needs to diagnose control-plane
    issues: "any BGP sessions down?", "which peers are flapping?",
    "show me OSPF adjacencies on this device".

    Args:
        state:      Raw protocol state ('established', 'idle',
                    'active', 'connect', 'full', '2way', ...).
        flap_state: 'stable' | 'unstable' | 'flapping' from Phase A.
        protocol:   'BGP' | 'OSPF' | 'EIGRP' | 'ISIS'.
        device:     Restrict to peers on a specific device.
        limit:      Max peers to return (default 50, cap 500).

    Returns:
        ``{"peers": [{from_device, to_name, protocol, state, peer_ip,
        router_id, remote_as, oper_status, oper_status_flap_state, ...},
        ...], "truncated": ..., ...}``
    """
    try:
        from netcortex.graph.client import get_driver
        driver = get_driver()
        norm_state = (state or "").lower() or None
        norm_flap = (flap_state or "").lower() or None
        norm_proto = (protocol or "").lower() or None
        norm_device = (device or "").lower() or None
        limit = _clamp_limit(limit)
        async with driver.session() as session:
            count_row = await (await session.run(
                """
                MATCH (x)-[r:ROUTING_PEER]-(y)
                WITH CASE WHEN x:Device THEN x ELSE y END AS a,
                     CASE WHEN x:Device THEN y ELSE x END AS b,
                     r
                WHERE a:Device
                  AND (a.stub IS NULL OR a.stub = false)
                  AND ($state IS NULL OR toLower(coalesce(r.state, '')) = $state)
                  AND ($flap_state IS NULL OR coalesce(r.oper_status_flap_state, 'stable') = $flap_state)
                  AND ($protocol IS NULL OR toLower(coalesce(r.protocol, '')) = $protocol)
                  AND (
                        $device IS NULL
                     OR toLower(coalesce(a.name, '')) = $device
                     OR toLower(coalesce(b.name, b.id, '')) = $device
                  )
                RETURN count(*) AS c
                """,
                state=norm_state,
                flap_state=norm_flap,
                protocol=norm_proto,
                device=norm_device,
            )).single()
            total = int((count_row or {}).get("c") or 0)

            rows = await (await session.run(
                """
                MATCH (x)-[r:ROUTING_PEER]-(y)
                WITH CASE WHEN x:Device THEN x ELSE y END AS a,
                     CASE WHEN x:Device THEN y ELSE x END AS b,
                     r
                WHERE a:Device
                  AND (a.stub IS NULL OR a.stub = false)
                  AND ($state IS NULL OR toLower(coalesce(r.state, '')) = $state)
                  AND ($flap_state IS NULL OR coalesce(r.oper_status_flap_state, 'stable') = $flap_state)
                  AND ($protocol IS NULL OR toLower(coalesce(r.protocol, '')) = $protocol)
                  AND (
                        $device IS NULL
                     OR toLower(coalesce(a.name, '')) = $device
                     OR toLower(coalesce(b.name, b.id, '')) = $device
                  )
                RETURN
                    a.name AS from_device,
                    coalesce(b.name, b.id) AS to_name,
                    r.protocol AS protocol,
                    r.address_family AS address_family,
                    r.state AS state,
                    r.oper_status AS oper_status,
                    r.oper_status_changed_at AS oper_status_changed_at,
                    r.oper_status_flap_state AS flap_state,
                    r.oper_status_flap_count_1h AS flap_count_1h,
                    r.oper_status_flap_count_24h AS flap_count_24h,
                    r.local_ip AS local_ip,
                    r.remote_ip AS remote_ip,
                    r.local_as AS local_as,
                    r.remote_as AS remote_as,
                    r.router_id AS router_id
                ORDER BY
                    CASE coalesce(r.oper_status_flap_state, 'stable')
                        WHEN 'flapping' THEN 0
                        WHEN 'unstable' THEN 1
                        ELSE 2
                    END,
                    coalesce(r.oper_status_changed_at, 0) DESC
                LIMIT $limit
                """
                ,
                state=norm_state,
                flap_state=norm_flap,
                protocol=norm_proto,
                device=norm_device,
                limit=limit,
            )).data()
        out = rows
        return {"peers": out, **_truncated(out, limit, total)}
    except Exception as exc:
        log.error("mcp.peers_list.failed", error=str(exc))
        return {"error": f"peers_list failed: {exc}",
                "peers": [], "returned": 0, "total": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5: paths.find — "Show me the path between A and B."
# Maps to top-20 problems: #12 (MTU), #13 (asymmetric routing).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def paths_find(
    src_device: str,
    dst_device: str,
    max_hops: int = 10,
) -> dict:
    """Find the shortest network path between two devices.

    Use this tool when the agent needs to trace connectivity: "how
    does cat9k1 reach mx1?", "what's the path from server A to gateway B?".

    Args:
        src_device: Source device hostname.
        dst_device: Destination device hostname.
        max_hops:   Maximum path length (default 10, cap 15).

    Returns:
        ``{"source": ..., "destination": ..., "hops": N,
        "path": [{node, via}, ...]}`` or ``{"error": "..."}``
        when no path is found within the hop budget.
    """
    try:
        from netcortex.graph.query import find_path
        return await find_path(src_device, dst_device, max_hops=max_hops)
    except Exception as exc:
        log.error("mcp.paths_find.failed",
                  src=src_device, dst=dst_device, error=str(exc))
        return {"error": f"paths_find failed: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 6: history.get — "Show me the 24h / 7d history for this thing."
# Maps to top-20 problems: #1 (flapping), #2 (peer flapping), #10 (STP).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def history_get(
    element_name: str,
    field: str = "oper_status",
    target: str = "auto",
) -> dict:
    """Return the connectivity history (transition list + flap stats)
    for one element.

    Use this tool when the agent has identified a problem and wants
    the full transition timeline: "show me the 24h flap history of
    cat9k1's uplink", "when did mx1's BGP session to AS65000 last
    bounce?".

    Args:
        element_name: For ``target='device'``, the device hostname.
                      For ``target='link'``, a free-text matcher
                      like "cat9k1-mx1" — we'll find the best link
                      matching that pair of device names.
                      For ``target='peer'``, "device:peer_ip" or
                      just the local device name.
        field:        Which tracked field's history to return.
                      Defaults to 'oper_status' for edges, 'status'
                      for devices.  Pure name; we append '_history'.
        target:       'device' | 'link' | 'peer' | 'auto' (default).
                      'auto' tries device first, then link, then peer.

    Returns:
        ``{"element": {...identity props...}, "field": "...",
        "current": "up", "history": [[at, to], ...],
        "flap_state": "stable", "flap_count_1h": 0,
        "flap_count_24h": 0, "flap_score_1h": 0.0,
        "changed_at": <epoch_ms>}``
    """
    try:
        field_norm = (field or "").strip().lower()
        if field_norm not in _ALLOWED_HISTORY_FIELDS:
            return {"error": (
                f"Unsupported field '{field}'. "
                f"Allowed: {', '.join(sorted(_ALLOWED_HISTORY_FIELDS))}"
            )}
        # Device nodes track `status`; links/peers track `oper_status`.
        dev_field = "status" if field_norm in {"status", "oper_status"} else field_norm
        edge_field = "oper_status" if field_norm == "status" else field_norm

        from netcortex.graph.client import get_driver
        driver = get_driver()
        async with driver.session() as session:
            results: list[dict] = []

            # 1. Try Device first — matches the natural agent
            # phrasing of "show me cat9k1's history".
            if target in ("auto", "device"):
                r = await (await session.run(
                    """
                    MATCH (d:Device)
                    WHERE d.name = $name AND d.canonical_id IS NULL
                    RETURN d.name AS name, d.id AS id, 'Device' AS kind,
                           d[$field] AS current,
                           d[$field + '_history'] AS history,
                           d[$field + '_changed_at'] AS changed_at,
                           d[$field + '_flap_state'] AS flap_state,
                           d[$field + '_flap_count_1h'] AS flap_count_1h,
                           d[$field + '_flap_count_24h'] AS flap_count_24h,
                           d[$field + '_flap_score_1h'] AS flap_score_1h
                    LIMIT 1
                    """,
                    name=element_name,
                    field=dev_field,
                )).data()
                results.extend(r)

            # 2. Then try PHYSICAL_LINK matching a "A-B" pair pattern.
            if not results and target in ("auto", "link"):
                pair = _parse_link_pair(element_name)
                if pair:
                    a, b = pair
                    r = await (await session.run(
                        """
                        MATCH (sa:Device)-[r:PHYSICAL_LINK]->(sb:Device)
                        WHERE (sa.name = $a AND sb.name = $b)
                           OR (sa.name = $b AND sb.name = $a)
                        RETURN sa.name + ' <-> ' + sb.name AS name,
                               elementId(r) AS id, 'PHYSICAL_LINK' AS kind,
                               r[$field] AS current,
                               r[$field + '_history'] AS history,
                               r[$field + '_changed_at'] AS changed_at,
                               r[$field + '_flap_state'] AS flap_state,
                               r[$field + '_flap_count_1h'] AS flap_count_1h,
                               r[$field + '_flap_count_24h'] AS flap_count_24h,
                               r[$field + '_flap_score_1h'] AS flap_score_1h
                        LIMIT 1
                        """,
                        a=a, b=b,
                        field=edge_field,
                    )).data()
                    results.extend(r)

            # 3. Then ROUTING_PEER — match by source device name OR by
            # "device:ip" colon-separated syntax.
            if not results and target in ("auto", "peer"):
                dev, _, ip = element_name.partition(":")
                r = await (await session.run(
                    """
                    MATCH (x)-[r:ROUTING_PEER]-(y)
                    WITH CASE WHEN x:Device THEN x ELSE y END AS a,
                         CASE WHEN x:Device THEN y ELSE x END AS b,
                         r
                    WHERE a:Device
                      AND a.name = $dev
                      AND ($ip = '' OR r.remote_ip = $ip OR r.peer_ip = $ip)
                    RETURN a.name + ' -> ' + coalesce(b.name, r.remote_ip, '?') AS name,
                           elementId(r) AS id, 'ROUTING_PEER' AS kind,
                           r[$field] AS current,
                           r[$field + '_history'] AS history,
                           r[$field + '_changed_at'] AS changed_at,
                           r[$field + '_flap_state'] AS flap_state,
                           r[$field + '_flap_count_1h'] AS flap_count_1h,
                           r[$field + '_flap_count_24h'] AS flap_count_24h,
                           r[$field + '_flap_score_1h'] AS flap_score_1h
                    LIMIT 1
                    """,
                    dev=dev, ip=ip,
                    field=edge_field,
                )).data()
                results.extend(r)

            if not results:
                return {"error": f"No element found matching '{element_name}' "
                                 f"(target={target})"}

            row = results[0]
            try:
                hist = json.loads(row.get("history") or "[]")
            except (TypeError, ValueError):
                hist = []

            return {
                "element":         {"name": row["name"], "id": row["id"],
                                    "kind": row["kind"]},
                "field":           dev_field if row["kind"] == "Device" else edge_field,
                "current":         row.get("current"),
                "history":         hist,
                "flap_state":      row.get("flap_state") or "stable",
                "flap_count_1h":   row.get("flap_count_1h") or 0,
                "flap_count_24h":  row.get("flap_count_24h") or 0,
                "flap_score_1h":   row.get("flap_score_1h") or 0.0,
                "changed_at":      row.get("changed_at"),
            }
    except Exception as exc:
        log.error("mcp.history_get.failed",
                  element=element_name, error=str(exc))
        return {"error": f"history_get failed: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 7: mac.lookup — "Where is this MAC learned?"
# Maps to top-20 problems: #14 (duplicate MAC), #15 (default gateway).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def mac_lookup(mac: str, limit: int = _DEFAULT_LIMIT) -> dict:
    """Look up everywhere a MAC address is learned in the network.

    Use this tool when the agent needs to locate a host or
    investigate possible duplicate MACs: "where is mac aa:bb:cc:...
    learned?", "is this MAC flapping between switches?".

    Args:
        mac:   The MAC address (any common format — colons,
               dashes, or no separators).  Case-insensitive.
        limit: Max entries to return (default 50, cap 500).

    Returns:
        ``{"entries": [{mac, learned_device, learned_port, vlan,
        owner_device, owner_nic, ip_addresses, source}, ...],
        "truncated": ..., ...}``
    """
    try:
        # Normalize MAC to colon-lowercase form to match graph storage.
        norm = mac.replace("-", "").replace(":", "").replace(".", "").lower()
        if len(norm) == 12:
            norm = ":".join(norm[i:i+2] for i in range(0, 12, 2))
        limit = _clamp_limit(limit)
        from netcortex.graph.query import get_mac_lookup
        data = await get_mac_lookup(norm, limit=limit)
        out = data.get("entries") or []
        total = data.get("count") or len(out)
        return {"entries": out, **_truncated(out, limit, total)}
    except Exception as exc:
        log.error("mcp.mac_lookup.failed", mac=mac, error=str(exc))
        return {"error": f"mac_lookup failed: {exc}",
                "entries": [], "returned": 0, "total": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 8: ip.lookup — "Where does this IP/prefix live?"
# Maps to top-20 problems: #14 (duplicate IP), #15 (default gateway).
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def ip_lookup(ip: str, limit: int = _DEFAULT_LIMIT) -> dict:
    """Look up an IP address or CIDR prefix in the network — find
    attached devices, interfaces, and the VLAN/cable that carries it.

    Use this tool when the agent has an IP and needs to know where
    it lives: "what device owns 10.0.0.5?", "which VLAN is
    192.168.1.0/24 on?".

    Accepts both a single host IP (``"10.0.0.5"``) and a CIDR
    (``"10.0.0.0/24"``).  Exact match on prefix, exact-or-contains
    match on host IP.

    Returns:
        ``{"prefixes": [{prefix, version, devices: [...]}],
        "addresses": [{ip, device, interface}],
        "links": [{a_name, b_name, l3_prefix_v4, l3_prefix_v6}],
        "truncated": ..., ...}``
    """
    try:
        from netcortex.graph.client import get_driver
        driver = get_driver()
        is_cidr = "/" in ip

        async with driver.session() as session:
            if is_cidr:
                # CIDR: find Prefix node + attached devices + carrying links.
                prefixes = await (await session.run(
                    """
                    MATCH (p:Prefix {prefix: $p})
                    OPTIONAL MATCH (d:Device)-[r:ROUTES_TO]->(p)
                    RETURN p.prefix AS prefix, p.version AS version,
                           collect({name: d.name, id: d.id,
                                    interface: r.interface, ip: r.ip}) AS devices
                    """,
                    p=ip,
                )).data()
                links = await (await session.run(
                    """
                    MATCH ()-[r:PHYSICAL_LINK]->()
                    WHERE $p IN coalesce(r.l3_prefix_v4, [])
                       OR $p IN coalesce(r.l3_prefix_v6, [])
                    RETURN startNode(r).name AS a_name,
                           endNode(r).name AS b_name,
                           r.l3_prefix_v4 AS l3_prefix_v4,
                           r.l3_prefix_v6 AS l3_prefix_v6
                    LIMIT 50
                    """,
                    p=ip,
                )).data()
                return {
                    "prefixes":  prefixes,
                    "addresses": [],
                    "links":     links,
                }

            # Host IP: find IPAddress node + the interface it's on.
            addresses = await (await session.run(
                """
                MATCH (a:IPAddress {address: $ip})
                OPTIONAL MATCH (a)<-[:HAS_IP]-(i:Interface)<-[:HAS_INTERFACE]-(d:Device)
                RETURN a.address AS ip, a.version AS version,
                       collect(DISTINCT {device: d.name,
                                          interface: i.name}) AS endpoints
                LIMIT 20
                """,
                ip=ip,
            )).data()
            # Also surface any ROUTES_TO whose ip matches (common when
            # there's no IPAddress node but the Device.routes_to.ip
            # property has the value).
            routes = await (await session.run(
                """
                MATCH (d:Device)-[r:ROUTES_TO]->(p:Prefix)
                WHERE r.ip = $ip
                RETURN d.name AS device, r.interface AS interface,
                       p.prefix AS prefix
                LIMIT 20
                """,
                ip=ip,
            )).data()
            return {
                "prefixes":  [],
                "addresses": addresses,
                "routes":    routes,
                "links":     [],
            }
    except Exception as exc:
        log.error("mcp.ip_lookup.failed", ip=ip, error=str(exc))
        return {"error": f"ip_lookup failed: {exc}",
                "prefixes": [], "addresses": [], "links": []}


# ─────────────────────────────────────────────────────────────────────────────
# Tool 9: top_problems — "Run all health checks and give me a ranked list."
# Maps to the entire top-20 list.  The hero tool for agentic ops.
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def top_problems(limit: int = 20, severity: str | None = None) -> dict:
    """Run a battery of health checks and return a ranked list of
    issues — the single best tool to ask an agent to call first when
    asked "what's wrong with the network?".

    Each problem is a single small object with stable fields so an
    agent can iterate, filter, and quote-back to the operator
    without further tool calls.

    Severity ladder (ranked highest-first):

      * ``critical`` — service-affecting now: device down, link down,
                       BGP/OSPF peer down, currently flapping.
      * ``warning``  — recent instability or capacity pressure:
                       unstable flap state, high utilisation,
                       elevated error rate, SNMP partially restricted.
      * ``info``     — observability gaps: SNMP unpolled, devices
                       missing required MIBs.

    Args:
        limit:    Max problems to return (default 20, cap 500).
        severity: Restrict to a severity bucket.

    Returns:
        ``{"problems": [{problem_type, severity, summary, evidence,
        suggested_action, related: {kind, name, id}}, ...],
        "checks_run": [...], "scanned": {devices, links, peers},
        "truncated": ..., ...}``

    Each problem's ``problem_type`` is a stable string so an agent
    can group / filter consistently across calls:

      * ``device_down``         — device.status in {down, offline, unreachable}
      * ``link_down``           — transit edge oper_status == down
      * ``link_flapping``       — transit edge flap_state == flapping
      * ``peer_down``           — routing peer oper_status == down
      * ``peer_flapping``       — routing peer flap_state == flapping
      * ``high_utilisation``    — link util_pct ≥ 80
      * ``high_errors``         — link error_rate_per_s ≥ 1
      * ``snmp_restricted``     — device snmp_health == 'restricted'
      * ``snmp_unreachable``    — device snmp_health == 'unreachable'
      * ``snmp_unpolled``       — device snmp_health == 'unpolled'

    Staleness policy
    ----------------
    ``device_down`` and ``link_down`` problems run through the
    staleness policy controlled by two ``netcortex/core`` config keys:

      * ``top_problems_stale_after_seconds`` (default ``86400`` / 24 h)
      * ``top_problems_stale_severity``      (default ``"info"``;
         values: ``critical|warning|info|filter``)

    A problem is "stale" when its underlying device's source-of-truth
    (today: Meraki Dashboard's ``lastReportedAt``) hasn't refreshed
    within the threshold.  Stale problems are re-emitted at the
    configured severity (``"filter"`` drops them entirely), tagged
    with ``evidence.stale = true`` and
    ``evidence.last_reported_at_ms``, and their summary appended with
    ``"(stale source data)"`` so an agent or UI can render them as
    housekeeping rather than active incidents.
    """
    from netcortex.config import get_settings
    from netcortex.graph.query import get_top_problems_inventory, get_top_problems_links
    from netcortex.util.timestamps import epoch_ms

    sev = (severity or "").lower() or None
    problems: list[dict] = []
    checks_run: list[str] = []

    # ── Staleness policy (see netcortex.config) ───────────────────────
    # Pulled once per call so every check below uses the same knobs.
    # If settings haven't been initialised (e.g. in a unit test that
    # exercises top_problems directly) we degrade to "policy disabled"
    # — passthrough behaviour, no demote, no filter.
    try:
        _settings = get_settings()
        _stale_after_s = int(_settings.top_problems_stale_after_seconds)
        _stale_sev     = str(_settings.top_problems_stale_severity)
    except Exception:
        _stale_after_s = 0
        _stale_sev     = "info"
    _now_ms = epoch_ms()

    # ── Inventory checks ──────────────────────────────────────────────
    try:
        inv = await get_top_problems_inventory()
        checks_run.append("inventory_state")
        # name → last_reported_at lookup so the link-state checks below
        # can apply the same staleness policy without re-querying the
        # graph for each row.  Built only from MX-style cloud-managed
        # devices that actually carry the timestamp; other devices fall
        # through to no-op staleness handling.
        _last_reported_by_name: dict[str, int] = {}
        for _d in inv:
            _ts = _d.get("meraki_last_reported_at")
            _nm = _d.get("name") or ""
            if _nm and isinstance(_ts, (int, float)):
                _last_reported_by_name[_nm] = int(_ts)

        for d in inv:
            name   = d.get("name", "")
            status = (d.get("status") or "").lower()
            health = (d.get("snmp_health") or "").lower()
            site   = d.get("site") or ""

            if status in ("down", "offline", "unreachable", "alerting", "dormant"):
                _last_seen_src = d.get("meraki_last_reported_at")
                _sev = _apply_staleness_policy(
                    "critical", _last_seen_src, _now_ms,
                    _stale_after_s, _stale_sev,
                )
                if _sev is None:
                    continue  # filter out: caller asked us to drop stale
                _is_stale = (_sev != "critical")
                problems.append({
                    "problem_type": "device_down",
                    "severity":     _sev,
                    "summary":      f"Device {name} is {status}"
                                    + (" (stale source data)" if _is_stale else ""),
                    "evidence":     {"status": status, "site": site,
                                     "mgmt_ip": d.get("mgmt_ip"),
                                     "stale": _is_stale,
                                     "last_reported_at_ms": _last_seen_src,
                                     "last_reported_at_iso": d.get(
                                         "meraki_last_reported_at_iso") or ""},
                    "suggested_action": (
                        "Source-of-truth has not refreshed this device "
                        "in over " f"{_stale_after_s // 3600}h"
                        " — confirm it is still deployed before treating "
                        "this as a live outage."
                        if _is_stale else
                        "Verify physical power, console reachability, "
                        "and the latest entry in the connectivity "
                        "history (use history.get) to see if this is "
                        "a fresh outage or a longstanding one."),
                    "related": {"kind": "Device", "name": name,
                                "id": d.get("mgmt_ip") or name},
                })
            if health == "unreachable":
                problems.append({
                    "problem_type": "snmp_unreachable",
                    "severity":     "warning",
                    "summary":      f"SNMP polls to {name} are failing",
                    "evidence":     {"snmp_health": health, "site": site},
                    "suggested_action": (
                        "Check SNMP credentials, ACLs, and reachability "
                        "from the poller. Without SNMP we lose interface "
                        "counters, MAC tables, and route tables."),
                    "related": {"kind": "Device", "name": name,
                                "id": d.get("mgmt_ip") or name},
                })
            elif health == "restricted":
                missing = d.get("snmp_restricted_mibs") or []
                problems.append({
                    "problem_type": "snmp_restricted",
                    "severity":     "warning",
                    "summary":      f"Device {name} has SNMP view restrictions",
                    "evidence":     {"snmp_health": health, "site": site,
                                     "restricted_mibs": missing},
                    "suggested_action": (
                        "Widen the device's 'snmp-server view' to cover "
                        "the missing MIB families: " + ", ".join(missing)),
                    "related": {"kind": "Device", "name": name,
                                "id": d.get("mgmt_ip") or name},
                })
            elif health == "unpolled":
                problems.append({
                    "problem_type": "snmp_unpolled",
                    "severity":     "info",
                    "summary":      f"Device {name} has never been SNMP-polled",
                    "evidence":     {"snmp_health": health, "site": site},
                    "suggested_action": (
                        "Add SNMP credentials in NetBox Secrets for this "
                        "device so the poller can pick it up next cycle."),
                    "related": {"kind": "Device", "name": name,
                                "id": d.get("mgmt_ip") or name},
                })
    except Exception as exc:
        log.warning("mcp.top_problems.inventory_check_failed", error=str(exc))

    # ── Link checks ───────────────────────────────────────────────────
    try:
        links = await get_top_problems_links()
        checks_run.append("link_state")
        for r in links:
            oper  = (r.get("oper_status") or "").lower()
            flap  = r.get("oper_status_flap_state") or "stable"
            util  = r.get("util_pct") or 0.0
            util_avg_1h = r.get("util_pct_avg_1h")
            util_for_alert = util_avg_1h if util_avg_1h is not None else util
            err   = r.get("error_rate_per_s") or 0.0
            err_avg_1h = r.get("error_rate_per_s_avg_1h")
            err_for_alert = err_avg_1h if err_avg_1h is not None else err
            pair  = f"{r.get('a_name','?')} ⇄ {r.get('b_name','?')}"
            etype = r.get("edge_type", "EDGE")

            if oper == "down":
                # Cross-reference the inventory map built above so a
                # WAN_UPLINK on a long-abandoned MX (months without a
                # check-in) is demoted/filtered alongside the
                # corresponding device_down event. For links where both
                # sides are real devices, use the freshest source-of-truth
                # timestamp across both endpoints.
                _src_name = r.get("a_name") or ""
                _dst_name = r.get("b_name") or ""
                _last_seen_src = _last_reported_by_name.get(_src_name) \
                    if "_last_reported_by_name" in locals() else None
                _last_seen_dst = _last_reported_by_name.get(_dst_name) \
                    if "_last_reported_by_name" in locals() else None
                if _last_seen_src is not None and _last_seen_dst is not None:
                    _last_seen = max(_last_seen_src, _last_seen_dst)
                else:
                    _last_seen = _last_seen_src if _last_seen_src is not None else _last_seen_dst
                _sev = _apply_staleness_policy(
                    "critical", _last_seen, _now_ms,
                    _stale_after_s, _stale_sev,
                )
                if _sev is None:
                    continue
                _is_stale = (_sev != "critical")
                problems.append({
                    "problem_type": "link_down",
                    "severity":     _sev,
                    "summary":      f"{etype} {pair} is DOWN"
                                    + (" (stale source data)" if _is_stale else ""),
                    "evidence":     {
                        "edge_type": etype,
                        "iface_a": r.get("iface_a"),
                        "iface_b": r.get("iface_b"),
                        "oper_status_changed_at": r.get("oper_status_changed_at"),
                        "health_score": r.get("health_score"),
                        "stale": _is_stale,
                        "last_reported_at_ms": _last_seen,
                    },
                    "suggested_action": (
                        "Source-of-truth for " f"{_src_name}"
                        " has not refreshed in over "
                        f"{_stale_after_s // 3600}h — confirm the "
                        "device is still deployed before treating "
                        "this as a live outage."
                        if _is_stale else
                        "Check both endpoints' interface state; verify "
                        "cabling / SFP / fiber loss; correlate with the "
                        "connectivity strip (history.get) to see if this "
                        "is the first down event or a flap."),
                    "related": {"kind": etype, "name": pair,
                                "id": f"{r.get('a_name')}|{r.get('b_name')}"},
                })
            if flap == "flapping":
                problems.append({
                    "problem_type": "link_flapping",
                    "severity":     "critical",
                    "summary":      f"{etype} {pair} is flapping",
                    "evidence":     {
                        "flap_count_1h":  r.get("oper_status_flap_count_1h"),
                        "flap_count_24h": r.get("oper_status_flap_count_24h"),
                        "flap_score_1h":  r.get("oper_status_flap_score_1h"),
                        "current_state":  oper,
                    },
                    "suggested_action": (
                        "Inspect both sides' interface error counters; "
                        "swap the cable / SFP; check power on the remote "
                        "side; consider damping if BGP."),
                    "related": {"kind": etype, "name": pair,
                                "id": f"{r.get('a_name')}|{r.get('b_name')}"},
                })
            elif flap == "unstable":
                problems.append({
                    "problem_type": "link_flapping",
                    "severity":     "warning",
                    "summary":      f"{etype} {pair} is unstable",
                    "evidence":     {
                        "flap_count_24h": r.get("oper_status_flap_count_24h"),
                        "current_state":  oper,
                    },
                    "suggested_action": (
                        "Recent bouncing detected but not currently "
                        "flapping. Review history.get for the pattern; "
                        "schedule a maintenance window to investigate."),
                    "related": {"kind": etype, "name": pair,
                                "id": f"{r.get('a_name')}|{r.get('b_name')}"},
                })
            if util_for_alert >= 80:
                problems.append({
                    "problem_type": "high_utilisation",
                    "severity":     "warning" if util_for_alert < 95 else "critical",
                    "summary":      f"{etype} {pair} at {util_for_alert:.0f}% utilisation (1h avg)",
                    "evidence":     {"util_pct": util,
                                     "util_pct_avg_1h": util_avg_1h,
                                     "speed_mbps": r.get("speed_mbps")},
                    "suggested_action": (
                        "Identify top talkers; consider link aggregation "
                        "or upgrade if sustained."),
                    "related": {"kind": etype, "name": pair,
                                "id": f"{r.get('a_name')}|{r.get('b_name')}"},
                })
            if err_for_alert >= 1.0:
                problems.append({
                    "problem_type": "high_errors",
                    "severity":     "warning" if err_for_alert < 5.0 else "critical",
                    "summary":      f"{etype} {pair} reporting {err_for_alert:.2f} err/s (1h avg)",
                    "evidence":     {"error_rate_per_s": err,
                                     "error_rate_per_s_avg_1h": err_avg_1h,
                                     "util_pct_avg_1h": util_avg_1h},
                    "suggested_action": (
                        "Physical-layer issue likely — check fiber loss "
                        "/ SFP, swap if needed; verify duplex mismatch."),
                    "related": {"kind": etype, "name": pair,
                                "id": f"{r.get('a_name')}|{r.get('b_name')}"},
                })
    except Exception as exc:
        log.warning("mcp.top_problems.link_check_failed", error=str(exc))

    # ── Routing peer checks ───────────────────────────────────────────
    try:
        peers = (await peers_list(limit=_MAX_LIMIT)).get("peers") or []
        checks_run.append("peer_state")
        for p in peers:
            oper = (p.get("oper_status") or "").lower()
            flap = p.get("flap_state") or "stable"
            proto = p.get("protocol") or "L3"
            pair  = f"{p.get('from_device','?')} → {p.get('to_name','?')}"

            if oper == "down":
                problems.append({
                    "problem_type": "peer_down",
                    "severity":     "critical",
                    "summary":      f"{proto} peer {pair} is DOWN",
                    "evidence":     {
                        "protocol": proto,
                        "state":    p.get("state"),
                        "remote_as": p.get("remote_as"),
                        "remote_ip": p.get("remote_ip"),
                    },
                    "suggested_action": (
                        "Check underlying L3 reachability between the "
                        "endpoints; verify password/keychain; correlate "
                        "with link state (links.list)."),
                    "related": {"kind": "ROUTING_PEER", "name": pair,
                                "id": pair},
                })
            if flap == "flapping":
                problems.append({
                    "problem_type": "peer_flapping",
                    "severity":     "critical",
                    "summary":      f"{proto} peer {pair} is flapping",
                    "evidence":     {
                        "flap_count_1h":  p.get("flap_count_1h"),
                        "flap_count_24h": p.get("flap_count_24h"),
                        "state":          p.get("state"),
                    },
                    "suggested_action": (
                        "Check hold/keepalive timers; verify the "
                        "underlying transport is stable (links.list); "
                        "consider BGP graceful-restart if appropriate."),
                    "related": {"kind": "ROUTING_PEER", "name": pair,
                                "id": pair},
                })
            elif flap == "unstable":
                problems.append({
                    "problem_type": "peer_flapping",
                    "severity":     "warning",
                    "summary":      f"{proto} peer {pair} is unstable",
                    "evidence":     {
                        "flap_count_24h": p.get("flap_count_24h"),
                        "state":          p.get("state"),
                    },
                    "suggested_action": (
                        "Recent bouncing in the last 24h. Review "
                        "history.get for the pattern."),
                    "related": {"kind": "ROUTING_PEER", "name": pair,
                                "id": pair},
                })
    except Exception as exc:
        log.warning("mcp.top_problems.peer_check_failed", error=str(exc))

    # ── Rank: critical → warning → info, then by stability score
    # within each bucket so the most "actively bad" things float up.
    _SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
    problems.sort(key=lambda p: (_SEV_RANK.get(p["severity"], 99),
                                  p.get("problem_type", "")))
    if sev:
        problems = [p for p in problems if p["severity"] == sev]

    total = len(problems)
    limit = _clamp_limit(limit)
    out = problems[:limit]

    return {
        "problems":   out,
        "checks_run": checks_run,
        "scanned":    {"devices": len(inv) if "inv" in locals() else 0,
                       "links":   len(links) if "links" in locals() else 0,
                       "peers":   len(peers) if "peers" in locals() else 0},
        **_truncated(out, limit, total),
    }
