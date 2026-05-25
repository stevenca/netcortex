---
name: netcortex-health
description: >-
  Run a ranked network health check using the NetCortex MCP server. Surfaces
  device outages, flapping links, down BGP/OSPF peers, utilisation hotspots,
  and SNMP coverage gaps across the entire network or a specific site. Use when
  the user asks "what's wrong?", "give me a network health report", "any alerts?",
  "what's down?", "show me the top problems", or asks about network status at a
  site.
---

# NetCortex Network Health

Uses the **netcortex** MCP server. All tools below are on that server.

## Quick start

1. Call `top_problems` first — no arguments needed.  
2. Drill into the highest-severity bucket with `links_list`, `peers_list`, or
   `inventory_list` using the filters suggested by the problem evidence.
3. For timeline context, call `history_get` on any flapping element.

## Tool guide

### `top_problems(limit, severity)`
The single best starting point. Runs all health checks and returns problems
ranked `critical → warning → info`.

- No arguments required; results are pre-sorted by urgency.
- `severity="critical"` to see only service-affecting issues.
- Problem types: `device_down`, `link_down`, `link_flapping`, `peer_down`,
  `peer_flapping`, `high_utilisation`, `high_errors`, `snmp_unreachable`,
  `snmp_restricted`, `snmp_unpolled`.
- Each problem carries `suggested_action` — quote it directly.

### `inventory_list(site, role, status, flap_state, snmp_health, limit)`
Device-level filter.

| Goal | Args |
|---|---|
| Devices at a site | `site="cpn-ful"` |
| Down devices | `status="offline"` or `status="down"` |
| Currently flapping | `flap_state="flapping"` |
| SNMP coverage gaps | `snmp_health="unreachable"` or `"restricted"` |

### `links_list(status, edge_type, flap_state, min_flap_score, min_util, site, device, limit)`
Transit edge filter.

| Goal | Args |
|---|---|
| Down links | `status="down"` |
| Flapping cables | `flap_state="flapping"` |
| Saturated links | `min_util=75` |
| Links at a site | `site="cpn-ful"` |
| WAN only | `edge_type="WAN_UPLINK"` |
| SD-WAN tunnels | `edge_type="SDWAN_TUNNEL"` |

Results include `breaches` flags (`util_warn`, `util_hot`, `util_critical`,
`err_warn`, `err_critical`) — reference them directly in summaries.

### `peers_list(state, flap_state, protocol, device, limit)`
Routing adjacency filter.

| Goal | Args |
|---|---|
| Down BGP | `protocol="BGP"`, `state="idle"` |
| Flapping peers | `flap_state="flapping"` |
| Peers on a device | `device="cpn-ful-cat8k1"` |

### `history_get(element_name, field, target)`
Transition timeline for any device, link, or peer. Use after identifying
an unstable element.

- `element_name`: device hostname, `"dev-a--dev-b"` for a link, or
  `"device:peer_ip"` for a peer.
- `target="auto"` tries device → link → peer in order.

## Response format

For health reports, structure output as:

```
## Network health — <timestamp>

**Critical** (N)
- <problem_type>: <summary>
  Evidence: <key fields>
  Action: <suggested_action>

**Warning** (N)
...

**Info** (N)
...

Scanned: X devices, Y links, Z peers
```

## Examples

**"What's wrong on the network right now?"**
→ `top_problems()` → group by severity, quote `suggested_action` per item.

**"Any link issues at cpn-ful?"**
→ `links_list(site="cpn-ful", status="down")` then
  `links_list(site="cpn-ful", flap_state="flapping")`

**"Is BGP healthy?"**
→ `peers_list(protocol="BGP")` → filter `oper_status == "down"` or
  `flap_state != "stable"`.

**"Which sites have SNMP gaps?"**
→ `inventory_list(snmp_health="unreachable", limit=100)` then
  `inventory_list(snmp_health="restricted", limit=100)`.
