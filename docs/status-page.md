# Status Page

NetCortex serves a server-rendered HTML dashboard at `/`. It uses Jinja2 templates with Tailwind CSS (CDN) — no build step, no JavaScript framework.

## Auto-Refresh

The page refreshes every 30 seconds via `<meta http-equiv="refresh" content="30">`. A manual refresh button is always visible.

## Panels

### System Health (top bar)
- NetCortex version
- NetBox: reachable / unreachable + URL
- Redis: connected / disconnected
- MCP server: up / down + registered tool count + connected client count
- Worker count

### Platform Adapters Table
Per adapter:
- Name and icon
- Last sync timestamp (relative: "2m ago", "failed")
- Status indicator: 🟢 ok / 🟡 degraded / 🔴 error
- Pending diff count (changes detected, not yet reconciled)
- Click → drill-down modal with last error detail

### Recent Diffs Feed
Last 20 sync diffs across all platforms, newest first:
```
● sw-bldga-01  interface Gi0/1  oper_status: up → down  [meraki, 3m ago]
+ mr-bldga-ap-08  added (discovered by meraki)  [2h ago]
- sw-retired-02  removed from Meraki  [netbox_wins — not applied]  [1d ago]
```
Color coded: blue = added, amber = changed, red = removed.

### Access Layer Activity
Last 10 CLI/RESTCONF/NETCONF sessions:
- Device name
- Protocol used
- Status: success / timeout / auth_error
- Timestamp

### MCP Activity
- Registered tools count
- Connected clients (SSE)
- Last 10 tool calls: tool name, device/query, timestamp, duration

## Endpoints

| Path | Description |
|---|---|
| `/` | Status dashboard (HTML) |
| `/health` | Health check JSON (for Docker/load balancer probes) |
| `/api/status` | Full status as JSON (same data as the dashboard) |
| `/api/` | REST API root (FastAPI auto-docs at `/api/docs`) |
| `/mcp` | MCP server (HTTP/SSE) |
