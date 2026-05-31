# NetCortex

> **The intelligence layer for your network.** NetCortex connects to any network platform API — Meraki, Catalyst Center, Intersight, and beyond — normalizes everything into NetBox's data model, and exposes it all through a unified MCP server so AI agents can reason across your entire infrastructure.

**Current version: 0.7.0.** See [`CHANGELOG.md`](CHANGELOG.md) for the
release history and [`docs/implementation-journal.md`](docs/implementation-journal.md)
for the deep-dive design record (with [§17 Versioning Policy](docs/implementation-journal.md#17-versioning-policy)).

> **Where this is headed (post-`0.7.0`):** see **[`docs/architecture/brain.md`](docs/architecture/brain.md)** — the brain-mapped agentic redesign with a NATS event bus, reflex/cognitive split, sensory unification (poll + webhooks + SNMP traps + Cisco streaming telemetry), memory layers, LLM router, and three external surfaces (MCP, A2A, SSE subscription stream).
>
> Contributing? Read **[`CONTRIBUTING.md`](CONTRIBUTING.md)** first.

---

## What It Does

| Capability | Description |
|---|---|
| **Platform Adapters** | Native connectors for Meraki, Catalyst Center, Intersight, SNMP, RESTCONF, NETCONF, and generic REST |
| **CLI Access** | SSH/Telnet into devices to run arbitrary commands, with output parsed and returned |
| **RESTCONF / NETCONF** | Standards-based configuration and state retrieval from any RFC 8040 / RFC 6241 device |
| **NetBox Backend** | Reads inventory, credentials, and config from NetBox; writes discovered state back |
| **Sync Engine** | Periodic diff loop per platform — detects changes and reconciles with NetBox |
| **Topology Discovery** | LLDP/CDP/API-based neighbor discovery → NetBox cables |
| **Document Store** | MOPs, runbooks, and network context stored as NetBox Journal Entries, queryable via MCP |
| **MCP Server** | Single unified MCP interface exposing all of the above to any AI agent |
| **Status Page** | Web dashboard showing adapter health, sync state, and recent diffs |

---

## Quick Start

```bash
# 1. Clone and enter the project
git clone https://github.com/your-org/netcortex.git
cd netcortex

# 2. Copy and configure environment
cp .env.example .env
# Edit .env — set NETBOX_URL, NETBOX_TOKEN, and adapter credentials

# 3. Start everything
docker compose up -d

# 4. Open the status page
open http://localhost:8000
```

NetCortex will be available at:
- **Status page:** http://localhost:8000
- **REST API:** http://localhost:8000/api/
- **MCP server (HTTP/SSE):** http://localhost:8000/mcp
- **Health check:** http://localhost:8000/health

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  AI Agents / Cursor / Claude / Custom clients            │
│  (MCP clients)                                           │
└────────────────────────┬────────────────────────────────┘
                         │  MCP (stdio or HTTP/SSE)
┌────────────────────────▼────────────────────────────────┐
│                    NetCortex                             │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │  MCP Server │  │  Status Page │  │   REST API     │  │
│  │  /mcp       │  │  /           │  │   /api/        │  │
│  └─────────────┘  └──────────────┘  └────────────────┘  │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │                   Sync Engine                        │ │
│  │  (APScheduler / Celery — per-platform diff loops)   │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │                 Platform Adapters                    │ │
│  │                                                      │ │
│  │  ┌─────────┐ ┌──────────┐ ┌────────────┐ ┌──────┐  │ │
│  │  │ Meraki  │ │Catalyst  │ │ Intersight │ │ SNMP │  │ │
│  │  │Dashboard│ │  Center  │ │            │ │      │  │ │
│  │  └─────────┘ └──────────┘ └────────────┘ └──────┘  │ │
│  │  ┌─────────┐ ┌──────────┐ ┌────────────┐           │ │
│  │  │  SSH /  │ │RESTCONF  │ │  Generic   │           │ │
│  │  │ Telnet  │ │ NETCONF  │ │    REST    │           │ │
│  │  └─────────┘ └──────────┘ └────────────┘           │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │               Access Layer                           │ │
│  │  CLI (Netmiko/Scrapli) │ RESTCONF │ NETCONF (ncclient)│
│  └─────────────────────────────────────────────────────┘ │
└────────────────────────┬────────────────────────────────┘
                         │  pynetbox (REST API)
┌────────────────────────▼────────────────────────────────┐
│                      NetBox                              │
│  Inventory · Credentials · Topology · Journal Entries    │
└─────────────────────────────────────────────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for the full design.

---

## Project Structure

```
netcortex/
├── netcortex/                  # Main Python package
│   ├── adapters/               # Platform adapter plugins
│   │   ├── base.py             # Abstract base class all adapters implement
│   │   ├── meraki.py
│   │   ├── catalyst_center.py
│   │   ├── intersight.py
│   │   ├── snmp.py
│   │   └── generic_rest.py
│   ├── access/                 # Low-level device access
│   │   ├── cli.py              # SSH/Telnet via Netmiko/Scrapli
│   │   ├── restconf.py         # RESTCONF (RFC 8040)
│   │   └── netconf.py          # NETCONF (RFC 6241, ncclient)
│   ├── mcp/                    # MCP server and tool definitions
│   │   ├── server.py           # FastMCP server entry point
│   │   └── tools/              # MCP tool implementations
│   │       ├── devices.py
│   │       ├── topology.py
│   │       ├── access.py       # CLI/RESTCONF/NETCONF tools
│   │       ├── vlans.py
│   │       ├── ipam.py
│   │       ├── documents.py
│   │       └── sync.py
│   ├── sync/                   # Sync engine
│   │   ├── engine.py           # Scheduler and orchestration
│   │   ├── diff.py             # Diff computation
│   │   └── reconciler.py       # NetBox write-back
│   ├── models/                 # Normalized/canonical data models
│   │   ├── device.py
│   │   ├── interface.py
│   │   ├── topology.py
│   │   └── document.py
│   ├── status/                 # Status page
│   │   ├── router.py           # FastAPI router
│   │   └── templates/          # Jinja2 HTML templates
│   │       └── index.html
│   ├── netbox.py               # NetBox client wrapper (pynetbox)
│   ├── config.py               # Settings (pydantic-settings)
│   └── main.py                 # FastAPI app entry point
├── tests/
│   ├── adapters/
│   ├── mcp/
│   ├── sync/
│   └── access/
├── docs/
│   ├── architecture.md         # Full architecture deep-dive
│   ├── adapters.md             # How to write a platform adapter
│   ├── mcp-tools.md            # All MCP tools reference
│   ├── access-layer.md         # CLI, RESTCONF, NETCONF usage
│   ├── sync-engine.md          # Sync and diff engine design
│   ├── netbox-integration.md   # NetBox data model mapping
│   └── status-page.md          # Status page reference
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── docker-compose.yml
├── docker-compose.override.yml.example
├── .env.example
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

---

## Configuration

All configuration is via environment variables (or a `.env` file):

```bash
# NetBox
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=your-netbox-api-token

# Redis (for sync worker queue)
REDIS_URL=redis://redis:6379/0

# Sync intervals (seconds)
SYNC_INTERVAL_MERAKI=3600
SYNC_INTERVAL_CATALYST_CENTER=600
SYNC_INTERVAL_INTERSIGHT=3600
SYNC_INTERVAL_SNMP=1800

# Conflict resolution policy: platform_wins | netbox_wins | alert
SYNC_CONFLICT_POLICY=alert

# MCP transport: stdio | http
MCP_TRANSPORT=http

# Log level
LOG_LEVEL=INFO
```

Adapter-specific credentials are read from NetBox `Secrets` — not environment variables. See [docs/netbox-integration.md](docs/netbox-integration.md).

---

## MCP Tools Reference

See [docs/mcp-tools.md](docs/mcp-tools.md) for the full reference. Key tools:

### Device Tools
- `find_device` — search by name, IP, site, role, or platform
- `list_devices` — filtered inventory
- `get_device_detail` — full detail including platform-native metadata
- `get_device_neighbors` — topology neighbors

### Access Tools
- `run_cli_command` — SSH into a device and run a command; returns parsed output
- `get_restconf` — fetch a YANG path via RESTCONF
- `get_netconf` — retrieve configuration or state via NETCONF
- `netconf_edit_config` — push a NETCONF edit-config RPC

### Network Tools
- `get_topology` — graph of devices and links for a site
- `list_vlans` — VLAN inventory
- `get_ip_context` — context for an IP address

### Document Tools
- `get_documents` — retrieve MOPs, runbooks, notes by tag or object
- `search_context` — semantic search across all Journal Entries
- `get_change_log` — audit trail of observed network changes

### Sync Tools
- `get_pending_diffs` — changes detected but not yet reconciled
- `get_sync_status` — last sync time and error counts per platform
- `trigger_sync` — manually trigger a sync for a platform

---

## Contributing a New Adapter

See [docs/adapters.md](docs/adapters.md). The short version: implement `PlatformAdapter` from `netcortex.adapters.base` and register it via the `netcortex.adapters` entry point in `pyproject.toml`.

---

## License

MIT
