# NetCortex Architecture

> **Primary reference**: see **[implementation-journal.md](implementation-journal.md)** for the complete, up-to-date record of what is built, how it works, all design decisions, the secrets schema, current graph state, and operational procedures. Where this file conflicts with the journal, the journal wins.

## Design evolution: graph-centric architecture

The strategic direction for NetCortex has **evolved from a NetBox-primary sync model to a graph-centric operational model**: a **network graph database** (Neo4j first; pluggable backends) holds **live, observed** topology and protocol state, while **NetBox remains the intended-state** source of truth for inventory, sites, tenancy, circuits, planned IPAM, and related business metadata. Ingestion adds **webhooks** and **streaming telemetry** alongside today’s REST polling; a **reconciliation engine** compares graph observations to NetBox and surfaces drift.

The **authoritative amended design**—data model, ingestion, reconciliation, MCP tools, `GraphBackend` abstraction, deployment, and migration phases—is documented in **[graph.md](graph.md)**. The sections below describe the **current codebase** and the **NetBox-centric** integration pattern; where they conflict with `graph.md`, treat `graph.md` as the forward-looking reference and this file as implementation context until the graph stack lands.

---

## Overview

NetCortex is a companion service to NetBox that bridges proprietary and standards-based network platform APIs into a single unified interface. It exposes this interface as an MCP server so AI agents can reason across your entire network without understanding the quirks of any individual platform.

NetBox is the **data backend**. NetCortex reads inventory from it, writes discovered state back to it, and stores operational documents in it. **Secrets are not stored in NetBox** — all credentials flow through AWS Secrets Manager or HashiCorp Vault.

---

## Design Principles

1. **NetBox is the data backend.** NetCortex has no schema of its own. All persistent state — inventory, topology, documents, audit logs — lives in NetBox.
2. **Secrets never touch NetBox.** All credentials (device passwords, API keys, tokens) are stored in an external secret backend: AWS Secrets Manager or HashiCorp Vault. NetBox holds no sensitive values.
3. **Bootstrap is minimal.** Only the secret backend location and its auth method need to be in environment variables. Everything else — including the NetBox URL and token — comes from the secret backend at startup.
4. **Adapters are the extensibility point.** Adding a new platform means writing one adapter class — nothing else changes.
5. **The access layer is separate from the adapter layer.** Adapters model *what* a platform knows. The access layer models *how* you talk to a device (SSH, RESTCONF, NETCONF). A single device can be reached via multiple access methods.
6. **The MCP layer is thin.** MCP tools delegate to adapters, the access layer, and NetBox. They do not contain business logic.
7. **Conflicts are explicit.** When platform state and NetBox state disagree, NetCortex doesn't silently overwrite either. It records the diff and applies a configurable policy.

---

## Component Breakdown

### Platform Adapter Layer

Each adapter is a Python class that implements `PlatformAdapter` (see [adapters.md](adapters.md)). An adapter is responsible for:

- Authenticating to its platform API
- Fetching devices, interfaces, VLANs, topology, and any other relevant state
- Normalizing that state into NetCortex's canonical models (see [netbox-integration.md](netbox-integration.md))
- Declaring its platform quirks via a `PlatformProfile`

Adapters are **read-only by default**. Write operations (pushing config) go through the Access Layer, not the adapter.

Built-in adapters:

| Adapter | Platform | Notes |
|---|---|---|
| `MerakiAdapter` | Cisco Meraki Dashboard API | Org/network hierarchy; knows MX/MS/MR distinctions |
| `CatalystCenterAdapter` | Cisco Catalyst Center (DNAC) | SDA fabric awareness; provisioning state |
| `IntersightAdapter` | Cisco Intersight | UCS blades, rack units, HyperFlex, server profiles |
| `SnmpAdapter` | Any SNMP v2c/v3 device | LLDP-MIB, IF-MIB, ENTITY-MIB; fallback for unmanaged gear |
| `GenericRestAdapter` | Any REST API | Schema-mapped via YAML config; for platforms without a native adapter |

Third-party adapters are registered as Python entry points under `netcortex.adapters`.

---

### Access Layer

The access layer provides **direct device access** independent of platform adapters. While adapters talk to *cloud/controller APIs*, the access layer talks *directly to devices*.

Three protocols are supported:

#### CLI (SSH / Telnet)
- Library: **Netmiko** (primary) with **Scrapli** as an alternative
- Credentials sourced from NetBox Secrets
- Supports: interactive commands, `show` output parsing (TextFSM/TTP templates), config push
- Device type detection from NetBox (`platform.slug` → Netmiko device type)
- Output is returned raw and, where a TextFSM template exists, also as structured data

#### RESTCONF (RFC 8040)
- Library: `httpx` with a thin YANG-path wrapper
- Auth: HTTP Basic or Token (sourced from NetBox)
- Supports GET (state/config), PUT/PATCH/POST (config push), DELETE
- Media types: `application/yang-data+json` (preferred) and `+xml`
- YANG model discovery via `/.well-known/host-meta` and `restconf/data/ietf-yang-library`

#### NETCONF (RFC 6241 / RFC 6242)
- Library: **ncclient**
- Transport: SSH subsystem (port 830)
- Credentials from NetBox Secrets
- Supports: `get`, `get-config`, `edit-config`, `commit`, `lock`/`unlock`, `copy-config`
- YANG capability advertisement parsed from `hello` exchange and stored in NetBox custom fields
- Responses returned as parsed Python dicts (via `xmltodict`) and optionally raw XML

Access method priority per device is configurable and stored in a NetBox custom field (`netcortex_access_methods: ["netconf", "restconf", "cli"]`).

---

### Normalized Data Models

NetCortex uses Pydantic models as the canonical intermediate representation between platform adapters and NetBox. These models map 1:1 to NetBox objects:

| NetCortex Model | NetBox Model | Key fields |
|---|---|---|
| `NormalizedDevice` | `dcim.Device` | name, platform, role, site, serial, mgmt_ip |
| `NormalizedInterface` | `dcim.Interface` | name, type, speed, oper_status, mac_address |
| `NormalizedTopologyLink` | `dcim.Cable` | device_a, interface_a, device_b, interface_b, discovery_proto |
| `NormalizedVLAN` | `ipam.VLAN` | vid, name, site, tenant, status |
| `NormalizedPrefix` | `ipam.Prefix` | prefix, site, vlan, tenant |
| `NormalizedIPAddress` | `ipam.IPAddress` | address, device, interface, dns_name |

Platform-specific metadata that has no NetBox equivalent is stored in **custom fields** prefixed with `nc_`:
- `nc_platform_id` — the platform's native ID for this object (e.g., Meraki serial, DNAC UUID)
- `nc_platform` — which adapter created/manages this record (e.g., `meraki`, `catalyst_center`)
- `nc_access_methods` — ordered list of access protocols for this device
- `nc_yang_capabilities` — NETCONF/RESTCONF YANG modules supported (JSON array)
- `nc_last_synced` — ISO timestamp of last successful sync

---

### Secret Backend

All sensitive values — API keys, device passwords, tokens, and the NetBox token itself — are stored in an external secret backend. NetCortex supports two:

| Backend | Library | Best for |
|---|---|---|
| AWS Secrets Manager | `boto3` | ECS/EKS/EC2 deployments; IAM role auth (no credentials in env) |
| HashiCorp Vault (KV v2) | `hvac` | On-prem or multi-cloud; AppRole / K8s / AWS IAM auth |

**Bootstrap flow:**
1. A minimal set of env vars tells NetCortex which backend to use and how to authenticate to it (e.g. `SECRET_BACKEND=aws_sm`, `AWS_REGION=us-east-1`).
2. At startup, NetCortex fetches `netcortex/core` from the backend — this contains the NetBox URL and token, Redis URL, MCP secret, and all tuning parameters.
3. Adapter configs and device credentials are fetched lazily from the backend as needed, with a TTL-based in-memory cache.

See [docs/secrets.md](secrets.md) for the full secret path schema, IAM policies, and Vault policies.

---

### NetBox Integration

NetCortex uses **pynetbox** to interact with NetBox's REST API. It treats NetBox as both source of truth and persistent store.

**Reads from NetBox:**
- Device inventory (filtered by `nc_platform` custom field to know which adapter manages each device)
- Credentials via NetBox Secrets (mapped by device and access method)
- Adapter configuration stored in NetBox custom fields on Site/Tenant objects
- Documents and context from Journal Entries

**Writes to NetBox:**
- All discovered devices, interfaces, VLANs, prefixes, IPs
- Topology as Cables between interfaces
- Sync diffs as Journal Entries (auto-generated, tagged `nc_sync_diff`)
- YANG capability lists per device

See [netbox-integration.md](netbox-integration.md) for the full field mapping and required custom field setup.

---

### Sync Engine

The sync engine is a scheduler (APScheduler in single-process mode, Celery for scaled deployments) that runs per-adapter sync jobs on a configurable interval.

Each sync cycle:

```
1. Adapter.fetch_all() → List[NormalizedDevice | NormalizedInterface | ...]
2. NetBox.fetch_current_state(adapter_platform) → List[same]
3. diff_engine.compute(platform_state, netbox_state) → DiffResult
4. for each change in DiffResult:
     apply policy (platform_wins | netbox_wins | alert)
     if apply: reconciler.write_to_netbox(change)
     log change as Journal Entry
5. Update nc_last_synced on each device
```

See [sync-engine.md](sync-engine.md) for conflict resolution policies and diff semantics.

---

### MCP Server

NetCortex exposes a single MCP server built with **FastMCP**. It supports both:
- `stdio` transport (for local Cursor/Claude Desktop integration)
- `HTTP/SSE` transport (for remote agents, default in Docker)

Tools are organized into modules:
- `devices` — inventory queries
- `topology` — neighbor and link queries
- `access` — CLI command execution, RESTCONF/NETCONF operations
- `vlans` / `ipam` — network addressing
- `documents` — MOP/runbook/context retrieval and search
- `sync` — diff inspection and manual sync triggers

See [mcp-tools.md](mcp-tools.md) for the full tool reference.

---

### Status Page

A server-rendered HTML dashboard (Jinja2 + Tailwind CSS via CDN) served at `/`. Refreshes every 30 seconds via `<meta http-equiv="refresh">`.

Panels:
- **System health** — NetBox reachability, Redis, MCP server, worker count
- **Adapter status** — per-adapter last sync time, status, pending diff count
- **Recent diffs** — last 20 changes detected across all platforms
- **Access layer** — recent CLI/RESTCONF/NETCONF sessions and their status
- **MCP activity** — registered tools, connected clients, recent tool calls

See [status-page.md](status-page.md).

---

## Data Flow Examples

### 1. AI Agent asks: "What interfaces are down on switches in Building A?"

```
Agent → MCP: find_device(site="Building A", role="switch")
NetCortex → NetBox: GET /dcim/devices/?site=building-a&role=switch
NetCortex → Agent: [sw-bldga-01, sw-bldga-02, sw-bldga-03]

Agent → MCP: get_device_detail("sw-bldga-01")
NetCortex → NetBox: GET /dcim/interfaces/?device=sw-bldga-01
NetCortex → Agent: {interfaces: [{name: "Gi0/1", oper_status: "down"}, ...]}
```

### 2. AI Agent asks: "Show me the spanning-tree output for sw-bldga-01"

```
Agent → MCP: run_cli_command(device="sw-bldga-01", command="show spanning-tree")
NetCortex → NetBox: GET secret for sw-bldga-01 (SSH credentials)
NetCortex → sw-bldga-01:22 (Netmiko SSH)
  → "show spanning-tree"
  → [raw output]
NetCortex → TextFSM parse (if template available)
NetCortex → Agent: {raw: "...", parsed: [{vlan: 10, role: "root", ...}]}
```

### 3. AI Agent asks: "Get the running config interfaces from sw-bldga-01 via NETCONF"

```
Agent → MCP: get_netconf(device="sw-bldga-01", filter="ietf-interfaces:interfaces")
NetCortex → NetBox: GET NETCONF credentials for sw-bldga-01
NetCortex → sw-bldga-01:830 (ncclient SSH)
  → <get-config> with subtree filter
  → [XML response]
NetCortex → xmltodict parse
NetCortex → Agent: {interfaces: [{name: "GigabitEthernet0/1", enabled: true, ...}]}
```

### 4. Sync engine detects a new device in Meraki

```
Sync timer fires → MerakiAdapter.fetch_all()
  → new device: "mr-bldga-ap-07" (Meraki MR)
diff_engine: device not in NetBox → CREATE
reconciler → NetBox POST /dcim/devices/ {name: "mr-bldga-ap-07", role: "ap", ...}
Journal Entry created: "nc_sync_diff: added mr-bldga-ap-07 (meraki)"
Status page reflects +1 pending diff (until auto-applied or acknowledged)
```

---

## Deployment

See the main [README](../README.md) for Docker Compose quick start.

For production:
- Run `loom-worker` with 2+ replicas for parallel sync jobs
- Put a reverse proxy (nginx/Caddy) in front for TLS termination
- Store `.env` secrets in Docker Secrets or a vault, not plain files
- Point `NETBOX_URL` at your existing NetBox instance — NetCortex does not require a dedicated one
