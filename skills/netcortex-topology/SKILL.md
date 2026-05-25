---
name: netcortex-topology
description: >-
  Explore network topology using the NetCortex MCP server. Traverses the
  physical and logical graph around a device, finds paths between two endpoints,
  lists links with health and utilisation data, and retrieves flap history.
  Use when the user asks about device connectivity, neighbors, cabling, path
  tracing, L2/L3 topology, "how does X reach Y", "what connects to this switch",
  "show me the topology around", or link history.
---

# NetCortex Topology

Uses the **netcortex** MCP server.

## Quick start

1. `topology_get(device)` — full neighborhood of one device.
2. `paths_find(src, dst)` — end-to-end path.
3. `links_list(device=...)` — filtered edge list with telemetry.
4. `history_get(element)` — flap timeline.

## Tool guide

### `topology_get(device, hops)`
Returns the subgraph centered on a device.

```
Response shape:
{
  "device":        {name, role, site, mgmt_ip, ...},
  "neighbors":     [{name, iface_local, iface_remote, edge_type, ...}],
  "interfaces":    [{name, speed, oper_status, vlans, ip, ...}],
  "vlans":         [{vlan_id, name}],
  "vrfs":          [{name, rd}],
  "bgp_peers":     [{peer_ip, remote_as, state, ...}],
  "sdwan_tunnels": [{remote, status, ...}],
  "graph":         {nodes: [...], edges: [...]}  ← Cytoscape format
}
```

- `hops=1` (default) — immediate neighbors only.
- `hops=2` — two cable-hops; use for understanding uplink topology.
- `hops=3–4` — campus-wide view; can be large.
- Device name is matched loosely — "cat8k1" will match "cpn-ful-cat8k1".

### `paths_find(src_device, dst_device, max_hops)`
Shortest hop-count path between two devices.

```
Response: {
  "source": "...", "destination": "...", "hops": N,
  "path": [{"node": "...", "via": "PHYSICAL_LINK|SDWAN_TUNNEL|..."}, ...]
}
```

- If no path found within `max_hops` (default 10): returns `{"error": "..."}`.
- Use to verify reachability before a CLI session or trace an outage path.

### `links_list(device, site, status, edge_type, flap_state, min_util, limit)`
Transit edges with live telemetry. Preferred over `topology_get` when you need
health data for a set of links rather than the full neighbor graph.

Edge types: `PHYSICAL_LINK`, `WAN_UPLINK`, `SDWAN_TUNNEL`, `VXLAN_TUNNEL`.

Key response fields per link:
- `oper_status` / `oper_status_changed_at` — current state and when it changed.
- `oper_status_flap_state` — `stable | unstable | flapping`.
- `util_pct_avg_1h`, `util_in_pct_avg_1h`, `util_out_pct_avg_1h` — bandwidth.
- `error_rate_per_s_avg_1h` — interface errors.
- `breaches.util_warn/hot/critical` — pre-computed threshold flags.
- `iface_a` / `iface_b` — interface names at each end.
- `l3_prefix_v4` / `l3_prefix_v6` — prefix on the link.

### `history_get(element_name, field, target)`
Flap timeline for a device, link, or peer.

| Target | `element_name` format | Example |
|---|---|---|
| Device | hostname | `"cpn-ful-cat8k1"` |
| Link | `"A--B"` | `"cat8k1--cat9k1"` |
| Peer | `"device:peer_ip"` | `"cat8k1:10.0.0.1"` |

Response includes `history` (list of `[epoch_ms, new_state]` transitions),
`flap_state`, `flap_count_1h`, `flap_count_24h`, `flap_score_1h`.

## Workflow patterns

**"What does cat9k1 connect to?"**
```
topology_get("cat9k1", hops=1)
→ summarise neighbors table: name, iface_local, iface_remote, edge_type
```

**"How does server A reach the firewall?"**
```
paths_find("server-a", "fw1")
→ list hop sequence; flag any hop where links_list shows oper_status=down
```

**"Show me the campus uplink topology"**
```
topology_get("core-sw1", hops=2)
→ highlight all PHYSICAL_LINK neighbors; note oper_status on each
```

**"Which links to cat8k1 had bounces in the last 24h?"**
```
links_list(device="cpn-ful-cat8k1")
→ filter flap_count_24h > 0
→ history_get("cat8k1--cat8k2") for each unstable link
```

**"Is the path from leaf1 to spine2 healthy?"**
```
paths_find("leaf1", "spine2")
→ for each hop pair, links_list(device="leafX") to check utilisation
```

## Interpreting utilisation

| `util_pct_avg_1h` | Meaning | Action |
|---|---|---|
| < 75 % | Healthy | — |
| 75–85 % | Warning | Monitor; plan capacity |
| 85–95 % | Hot | Identify top talkers; schedule upgrade |
| ≥ 95 % | Critical | Immediate action; consider re-routing |
