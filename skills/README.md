# NetCortex OpenClaw Skills

Five skills that wire OpenClaw to the NetCortex MCP server for network
intelligence and operations.

## Prerequisites

NetCortex MCP server must be registered in OpenClaw:

```bash
openclaw mcp set netcortex '{
  "transport": "streamable-http",
  "url": "https://cpn-ful-netcortex1.ciscops.net/mcp/",
  "headers": {
    "Authorization": "Bearer <your-mcp-secret>"
  }
}'
```

Retrieve your token from the `netcortex/core` secret in AWS Secrets Manager
under the key `mcp_secret`.

## Install

```bash
# Global install (all channels)
cp -r skills/* ~/.openclaw/skills/

# Or per-project
cp -r skills/* .openclaw/skills/
```

## Skills

| Skill | Purpose |
|---|---|
| `netcortex-health` | Overall network health check — ranked problems, device/link/peer state |
| `netcortex-topology` | Topology traversal, path finding, and link analysis |
| `netcortex-device-access` | CLI commands, RESTCONF, NETCONF, device detail |
| `netcortex-incident-response` | Step-by-step incident investigation workflow |
| `netcortex-lookup` | IP address and MAC address lookups |

## MCP Tool Summary

| Tool | Category | One-line purpose |
|---|---|---|
| `top_problems` | Health | Ranked health report — start every ops conversation here |
| `inventory_list` | Health | Device list filtered by site / role / status / SNMP health |
| `links_list` | Health | Cable/tunnel list filtered by status / flap state / utilisation |
| `peers_list` | Health | BGP/OSPF adjacencies filtered by state / flap / protocol |
| `topology_get` | Topology | Neighbor graph around one device |
| `paths_find` | Topology | Shortest path between two devices |
| `history_get` | Topology | Flap timeline for a device, link, or peer |
| `find_device` | Devices | Search devices by name, IP, site, or role |
| `list_devices` | Devices | Filtered NetBox inventory |
| `get_device_detail` | Devices | Full device record — interfaces, IPs, VLANs |
| `get_device_neighbors` | Devices | LLDP/CDP neighbors |
| `list_adapter_instances` | Devices | Running adapters and health status |
| `run_cli_command` | Access | Run a single CLI command via SSH |
| `run_cli_commands` | Access | Run multiple CLI commands in one SSH session |
| `get_restconf` | Access | RESTCONF GET for a YANG path |
| `put_restconf` | Access | RESTCONF PUT/PATCH/POST (write) |
| `get_netconf` | Access | NETCONF get-config / get operational state |
| `netconf_edit_config` | Access | NETCONF edit-config (write) |
| `get_device_capabilities` | Access | NETCONF/RESTCONF YANG capabilities |
| `ip_lookup` | Lookup | Find device/interface/VLAN for an IP or prefix |
| `mac_lookup` | Lookup | Find switch port and device for a MAC address |
| `get_documents` | Docs | MOPs, runbooks, and notes from NetBox |
| `search_context` | Docs | Semantic search across journal entries |
| `get_change_log` | Docs | Sync-generated audit trail |
| `get_sync_status` | Sync | Adapter sync status and last-run timestamps |
| `get_pending_diffs` | Sync | Changes detected but not yet in NetBox |
| `trigger_sync` | Sync | Manually kick a sync for one or all adapters |
