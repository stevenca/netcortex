# MCP Tools Reference

NetCortex exposes a single MCP server with **27 registered tool names**
(as of 0.6.0-dev20) reachable over the **streamable-http** transport at
`/mcp/` on the same FastAPI listener that serves the status UI.

Operationally, there are two classes of tools today:

- **Production-ready (`agentic_ops`)**: the 9 tools in this section.
- **Placeholder catalog tools**: currently return `not implemented` or
  minimal placeholders while the NetBox/access/sync/document backends
  are being wired.

For the design rationale and how the tools fit together with the
status-history correlator and the Links UI, see
[Agentic Ops](agentic-ops.md).

---

## Quick Start

### 1. Verify the transport is up

```bash
curl -sS http://localhost:8000/api/status | jq .mcp
# {
#   "status": "enabled",
#   "path": "/mcp",
#   "transport": "streamable-http",
#   "tool_count": 27,
#   "message": ""
# }
```

The header of the NetCortex status page (top right) also shows the
"MCP" pill — green when the transport is mounted and reachable.

### 2. Add to Cursor

Add to `~/.cursor/mcp.json` (merge into existing `mcpServers` block):

```json
{
  "mcpServers": {
    "netcortex": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

**The trailing slash matters** — the streamable-http transport
expects requests at the mount root.  Cursor handles this internally;
if you ever test with curl, hit `/mcp/`.

If reaching the container from a different host, replace `localhost`
with the host IP or hostname.

### 3. Disable / move the endpoint

* `NETCORTEX_MCP_ENABLED=0` — disables the transport entirely.
* `NETCORTEX_MCP_PATH=/something` — moves the mount point.

---

## Agentic-Ops Tools (the highest-value surface)

These nine tools were built specifically for LLMs / agents doing
operator-grade network diagnosis.  They are single-purpose, bounded
(default 50 rows, hard cap 500), and use stable field names matching
the REST API surface (`oper_status`, `oper_status_flap_state`, etc.).

When an agent is asked "what's wrong with the network?", the
recommended call sequence is:

1. **`top_problems`** — ranked list of every issue
2. **`links_list`** / **`peers_list`** — drill into the underlying
   data plane / control plane
3. **`history_get`** — pull the 7-day transition timeline for the
   specific element
4. **`topology_get`** — understand connectivity around a device
5. **`paths_find`** — trace end-to-end reachability

### `top_problems` — the hero tool

Runs a battery of health checks across the device inventory, every
transit edge, and every routing peer; returns a ranked problem list.

```json
{
  "limit": "integer (default 20, cap 500)",
  "severity": "string (optional: critical|warning|info)"
}
```

Each problem has stable fields:

| Field              | Purpose                                          |
|--------------------|--------------------------------------------------|
| `problem_type`     | Stable string for grouping/filtering             |
| `severity`         | `critical` / `warning` / `info`                  |
| `summary`          | Human-readable one-liner                         |
| `evidence`         | The underlying data that triggered the check     |
| `suggested_action` | Actionable next step                             |
| `related`          | `{kind, name, id}` reference to the element      |

Severity ladder:

* **critical** — service-affecting now: device down, link down,
  peer down, currently flapping (≥5 transitions in last hour)
* **warning**  — recent instability or capacity pressure: unstable
  flap state, util ≥ 80%, error rate ≥ 1/s, SNMP partly restricted
* **info**     — observability gaps: SNMP unpolled, missing MIBs

`problem_type` values: `device_down`, `link_down`,
`link_flapping`, `peer_down`, `peer_flapping`,
`high_utilisation`, `high_errors`, `snmp_restricted`,
`snmp_unreachable`, `snmp_unpolled`.

### `inventory_list`

What devices exist and what's their state?

```json
{
  "site": "string (optional, name OR slug)",
  "role": "string (optional, e.g. switch|router|ap)",
  "status": "string (optional, e.g. active|offline|alerting)",
  "flap_state": "string (optional: stable|unstable|flapping)",
  "snmp_health": "string (optional: full|partial|restricted|unreachable|cloud_only|unpolled)",
  "limit": "integer (default 50, cap 500)"
}
```

### `topology_get`

How is device X connected? Returns neighbours, interfaces, VLANs,
peers, tunnels out to ``hops`` cable-distance.

```json
{
  "device": "string (hostname)",
  "hops": "integer (1-4, default 1)"
}
```

### `links_list`

Which cables / WAN uplinks / SD-WAN tunnels are flapping / down /
over-utilised?  Pre-sorted server-side by flap score → recency →
health.

Request:

```json
{
  "status":         "string (optional: up|down)",
  "edge_type":      "string (optional: PHYSICAL_LINK|WAN_UPLINK|SDWAN_TUNNEL|VXLAN_TUNNEL)",
  "flap_state":     "string (optional: stable|unstable|flapping)",
  "min_flap_score": "float (default 0.0, ≥0.5 means actively flapping)",
  "min_util":       "float (default 0.0, percent)",
  "min_error_rate": "float (default 0.0, errors per second)",
  "site":           "string (optional, matches either side)",
  "device":         "string (optional, matches either side)",
  "limit":          "integer (default 50, cap 500)"
}
```

Each row in the slim response carries the universal status-history
fields plus type-specific and provenance fields. As of 0.6.0-dev20:

| Field | Always present | Notes |
|---|---|---|
| `edge_type` | yes | `PHYSICAL_LINK` / `WAN_UPLINK` / `SDWAN_TUNNEL` / `VXLAN_TUNNEL` |
| `a_name`, `b_name` | yes | A-side and B-side device names |
| `iface_a`, `iface_b` | yes | Port name; for `WAN_UPLINK` `iface_a` is COALESCEd from `r.wan_slot` so dual-WAN edges read as `wan1` / `wan2` in the same column |
| `oper_status` | yes | For `SDWAN_TUNNEL` derived from Meraki `reachability` (`reachable` → `up`, `unreachable` → `down`, `unknown` → unset) |
| `oper_status_flap_state` | yes | `stable` / `unstable` / `flapping` |
| `oper_status_flap_score_1h` | yes | `count_1h / 6.0` saturated at 1.0 |
| `oper_status_changed_at` | yes | epoch_ms — guaranteed honest (dev17/18 contract) |
| `health_score`, `util_pct`, `error_rate_per_s` | when measured | `PHYSICAL_LINK` / `WAN_UPLINK` only |
| `l3_prefix_v4`, `l3_prefix_v6` | when known | derived prefixes carried by the link |
| `discovery_proto` | when known | `lldp` / `cdp` / `meraki_api` / … |
| `source_adapter` (dev20) | when set | `meraki/*`, `catalyst_center/*`, `snmp/*`; empty for correlator-built edges (`WAN_UPLINK` to Internet, AS boundary peers) |
| `wan_slot` (dev20) | `WAN_UPLINK` only | `wan1` / `wan2` for `via=mx_uplink`; empty otherwise |
| `via` (dev20) | `WAN_UPLINK` only | `mx_uplink` / `ebgp` — the discovery rule that produced the edge |

### `peers_list`

Which routing adjacencies (BGP/OSPF/...) are down or unstable?

```json
{
  "state":      "string (optional: established|idle|active|connect|full|2way|...)",
  "flap_state": "string (optional: stable|unstable|flapping)",
  "protocol":   "string (optional: BGP|OSPF|EIGRP|ISIS)",
  "device":     "string (optional)",
  "limit":      "integer (default 50, cap 500)"
}
```

### `paths_find`

Shortest network path between two devices, traversing all
relationship types (physical, logical, routing, SD-WAN).

```json
{
  "src_device": "string (hostname)",
  "dst_device": "string (hostname)",
  "max_hops":   "integer (default 10, cap 15)"
}
```

### `history_get`

Fetch the 7-day transition history + flap stats for one element.

```json
{
  "element_name": "string (device hostname, or 'A-B' link pair, or 'device:peer_ip')",
  "field":        "string (default oper_status)",
  "target":       "string (default auto: device|link|peer|auto)"
}
```

Returns the full `history` list as `[[at_epoch_ms, new_state], ...]`
plus the four derived flap stats (`flap_state`, `flap_count_1h`,
`flap_count_24h`, `flap_score_1h`).

### `mac_lookup`

Where is this MAC learned in the network?  Accepts any common MAC
format (colons, dashes, dots, or no separators); case-insensitive.

```json
{
  "mac":   "string",
  "limit": "integer (default 50, cap 500)"
}
```

### `ip_lookup`

Where does this IP or CIDR prefix live?  Accepts both host IPs
(`10.0.0.5`) and CIDRs (`10.0.0.0/24`).

```json
{
  "ip":    "string",
  "limit": "integer (default 50, cap 500)"
}
```

### Top-20 network problems → tool mapping

| # | Problem                            | Tool(s)                              |
|---|------------------------------------|--------------------------------------|
| 1 | Link flapping                      | `top_problems` → `history_get`       |
| 2 | BGP/OSPF peer down/flapping        | `peers_list`, `top_problems`         |
| 3 | High link utilisation              | `links_list(min_util=80)`            |
| 4 | High link errors                   | `links_list(min_error_rate=1)`       |
| 5 | Device unreachable                 | `top_problems` → `topology_get`      |
| 6 | SNMP coverage gap                  | `inventory_list(snmp_health=...)`    |
| 7 | Wi-Fi outage at a site             | `inventory_list(site, role=ap)`      |
| 8 | WAN circuit down                   | `links_list(edge_type=WAN_UPLINK)`   |
| 9 | SD-WAN tunnel down                 | `links_list(edge_type=SDWAN_TUNNEL)` |
| 10| STP topology change                | `history_get(field=stp_state)`*      |
| 11| VLAN inconsistency / orphans       | `topology_get` (via `vlans`)         |
| 12| Path MTU / blackhole               | `paths_find` → `links_list`          |
| 13| Asymmetric routing                 | `paths_find` (twice, swap src/dst)   |
| 14| Duplicate MAC / IP                 | `mac_lookup`, `ip_lookup`            |
| 15| Default-gateway not reachable      | `ip_lookup`, `peers_list`            |
| 16| LACP unbalanced / single-sided     | `links_list` (single_sided field)    |
| 17| Power / hardware alarm             | `inventory_list(status=alerting)`    |
| 18| Recently-changed circuit           | sort `links_list` by `_changed_at`   |
| 19| Cable mis-cabled (wrong neighbour) | `topology_get` (diff vs intent)      |
| 20| New device appeared unexpectedly   | `inventory_list` filter by adapter   |

(*) `stp_state` history follows the same schema as `oper_status`
but isn't wired into the Phase-A correlator yet.

---

## Device Tools

> Current status: **placeholder surface**. These tool names are registered
> for schema stability but are not yet fully implemented.

### `find_device`
Search for devices by any combination of criteria.
```json
{
  "query": "string (optional, free text)",
  "name": "string (optional)",
  "ip": "string (optional)",
  "site": "string (optional, NetBox site slug)",
  "role": "string (optional, NetBox role slug)",
  "platform": "string (optional, adapter name: meraki|catalyst_center|intersight|snmp)",
  "status": "string (optional: active|planned|staged|failed|decommissioning|inventory)",
  "limit": "integer (default 25)"
}
```

### `list_devices`
Return a filtered inventory list (lighter weight than `find_device`).
```json
{
  "site": "string (optional)",
  "role": "string (optional)",
  "platform": "string (optional)",
  "status": "string (optional)",
  "limit": "integer (default 100)"
}
```

### `get_device_detail`
Full detail for a single device including interfaces, IPs, custom fields, and platform metadata.
```json
{ "device": "string (name or IP)" }
```

### `get_device_neighbors`
Topology neighbors discovered via LLDP/CDP or platform topology API.
```json
{
  "device": "string",
  "include_remote_details": "boolean (default false)"
}
```

---

## Access Tools

> Current status: **not implemented**. Access-layer MCP tools currently
> return explicit `not implemented` responses.

### `run_cli_command`
SSH/Telnet into a device and run a single command.
```json
{
  "device": "string",
  "command": "string",
  "parse": "boolean (default true — attempt TextFSM parsing)",
  "timeout": "integer seconds (default 30)",
  "force_method": "string (optional: ssh|telnet)"
}
```

### `run_cli_commands`
Run multiple commands in a single session.
```json
{
  "device": "string",
  "commands": ["string"],
  "parse": "boolean (default true)",
  "timeout": "integer seconds (default 60)"
}
```

### `get_restconf`
Fetch a YANG path via RESTCONF GET.
```json
{
  "device": "string",
  "path": "string (YANG path, e.g. ietf-interfaces:interfaces)",
  "datastore": "string (default running: running|candidate|startup|operational)"
}
```

### `put_restconf`
Push configuration via RESTCONF PUT/PATCH/POST.
```json
{
  "device": "string",
  "path": "string",
  "data": "object",
  "method": "string (default PUT: PUT|PATCH|POST)"
}
```
*Requires write scope.*

### `get_netconf`
Retrieve configuration or state via NETCONF.
```json
{
  "device": "string",
  "operation": "string (default get-config: get-config|get)",
  "source": "string (default running: running|candidate|startup)",
  "filter": {
    "type": "string (subtree|xpath)",
    "value": "string"
  },
  "format": "string (default dict: dict|xml)"
}
```

### `netconf_edit_config`
Push a NETCONF edit-config RPC.
```json
{
  "device": "string",
  "target": "string (default candidate: candidate|running)",
  "config": "string (XML config fragment)",
  "commit": "boolean (default true)"
}
```
*Requires write scope.*

### `netconf_get_schema`
Retrieve a YANG schema from a device.
```json
{
  "device": "string",
  "identifier": "string (YANG module name)",
  "version": "string (optional, YANG module revision date)"
}
```

### `get_device_capabilities`
List NETCONF/RESTCONF YANG capabilities for a device.
```json
{ "device": "string" }
```

---

## Topology Tools

> Current status: **legacy placeholder**. Use `agentic_ops.topology_get`
> and `agentic_ops.paths_find` for production diagnostics.

### `get_topology`
Return the network topology graph for a site.
```json
{
  "site": "string",
  "include_cables": "boolean (default true)",
  "include_wireless": "boolean (default false)"
}
```
Returns nodes (devices) and edges (cables/links) in a format suitable for graph visualization.

### `get_path`
Find the layer 2/3 path between two devices or IPs.
```json
{
  "source": "string (device name or IP)",
  "destination": "string (device name or IP)"
}
```

---

## VLAN & IPAM Tools

> Current status: **placeholder surface**.

### `list_vlans`
```json
{
  "site": "string (optional)",
  "tenant": "string (optional)",
  "vid": "integer (optional)"
}
```

### `get_ip_context`
What is this IP? What device, interface, prefix, and VRF does it belong to?
```json
{ "ip": "string" }
```

### `list_prefixes`
```json
{
  "site": "string (optional)",
  "vrf": "string (optional)",
  "tenant": "string (optional)",
  "contains": "string (optional, find prefix containing this IP)"
}
```

---

## Document Tools

> Current status: **not implemented**.

### `get_documents`
Retrieve MOPs, runbooks, and context notes.
```json
{
  "tag": "string (optional: mop|runbook|context|change)",
  "object_type": "string (optional: device|site|vlan|prefix)",
  "object_name": "string (optional)",
  "limit": "integer (default 10)"
}
```

### `search_context`
Semantic search across all Journal Entries (MOPs, runbooks, notes, change logs).
```json
{
  "query": "string",
  "limit": "integer (default 5)"
}
```

### `get_change_log`
Audit trail of observed network changes (auto-generated by the sync engine).
```json
{
  "device": "string (optional)",
  "site": "string (optional)",
  "platform": "string (optional)",
  "since": "string (optional, ISO 8601 datetime)",
  "limit": "integer (default 20)"
}
```

---

## Sync Tools

> Current status: **not implemented**.

### `get_pending_diffs`
Changes detected by the sync engine not yet reconciled with NetBox.
```json
{
  "platform": "string (optional)",
  "type": "string (optional: added|removed|changed)"
}
```

### `get_sync_status`
Last sync time, status, and error counts per platform.
```json
{ "platform": "string (optional — omit for all platforms)" }
```

### `trigger_sync`
Manually trigger a sync for a specific platform.
```json
{
  "platform": "string",
  "scope": "string (optional: devices|interfaces|vlans|topology|all — default all)"
}
```

---

## MCP Server Configuration

### Transport — what's actually shipped today

The MCP server is mounted as a **streamable-http** ASGI app on the
same FastAPI listener that serves the status UI.  Path defaults to
`/mcp`; configurable via `NETCORTEX_MCP_PATH`.  Disable entirely with
`NETCORTEX_MCP_ENABLED=0`.

stdio transport is **not** wired up today (the previous design doc
referenced `python -m netcortex.mcp.stdio`, but that module doesn't
exist).  HTTP is the supported path.

### Cursor configuration

`~/.cursor/mcp.json` — note the **trailing slash**:

```json
{
  "mcpServers": {
    "netcortex": {
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

The status-page header pill includes this exact snippet on hover,
so you can copy it from the UI if you forget.

### Authentication — not yet

**The transport currently has no authentication.**  Anyone who can
reach `http://<host>:8000/mcp/` can call every tool.  All shipped
tools are read-only against the graph, but operational data (mgmt
IPs, BGP peerings, MAC tables) is still exposed.

Mitigations until auth lands:

* Bind the container to `127.0.0.1` only (loopback access).
* Front it with your existing reverse proxy / Tailscale / VPN.
* Set `NETCORTEX_MCP_ENABLED=0` in production until needed.

Planned auth path (see `CHANGELOG.md` 0.6.0-dev16 "On auth"):

1. **Bearer token** first — a single shared token in env var,
   validated by a small `AuthProvider`.  Solves the "anyone on the
   LAN" problem in one PR.
2. **OAuth 2.1 / DCR** later — what the official MCP spec endorses;
   FastMCP has built-in providers for it.  Enables per-user identity
   in tool audit logs.
