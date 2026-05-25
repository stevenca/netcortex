# NetCortex — Implementation Journal

> **Current version: 0.6.0-dev23.**  See [`CHANGELOG.md`](../CHANGELOG.md) for the granular dev-by-dev history and [§17 Versioning Policy](#17-versioning-policy) for how to bump it.

This document is the authoritative record of everything built in NetCortex, why each decision was made, how things are wired together, and the current operational state. It is written for a developer (or AI agent) picking up the project fresh and being asked to either extend or recreate it.

---

## Table of Contents

1. [What NetCortex Is](#1-what-netcortex-is)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Project Layout](#3-project-layout)
4. [Secret Backend & Bootstrap](#4-secret-backend--bootstrap)
5. [Graph Data Model](#5-graph-data-model)
6. [Platform Adapters](#6-platform-adapters)
7. [SNMP Adapter — Deep Dive](#7-snmp-adapter--deep-dive)
8. [Graph Ingest & Correlation](#8-graph-ingest--correlation)
9. [REST API](#9-rest-api)
10. [Web UI](#10-web-ui)
11. [Worker & Scheduling](#11-worker--scheduling)
12. [Docker Deployment](#12-docker-deployment)
13. [Native Worker (macOS)](#13-native-worker-macos)
14. [Secrets Schema](#14-secrets-schema)
15. [Known Issues & Workarounds](#15-known-issues--workarounds)
16. [Current Graph State](#16-current-graph-state)
17. [Versioning Policy](#17-versioning-policy)
18. [Recent Major Changes (since 0.1.0)](#18-recent-major-changes-since-010)
19. [Operational Data Quality (the dev17 → dev20 framework)](#19-operational-data-quality-the-dev17--dev20-framework)
20. [Current Sprint State (dev23)](#20-current-sprint-state-dev23)

---

## 1. What NetCortex Is

NetCortex is an **intelligence layer** that sits alongside NetBox. It connects to multiple network management platforms (Meraki, Catalyst Center, Intersight, Nexus Dashboard, vSphere, and any SNMP-capable device), discovers the actual network state, and stores it as a **multi-dimensional graph** in Neo4j.

**NetBox is the source of truth for intended state.** NetCortex reads site/location/serial data from NetBox to enrich graph nodes. NetCortex does not write back to NetBox (read-only consumer).

**Neo4j is the operational graph store.** It holds observed device adjacencies, STP trees, routing protocol peers, MAC/ARP tables, VLAN memberships, and IP address assignments — all simultaneously queryable in multiple "dimensions."

**MCP is the AI interface.** An MCP server exposes graph queries to AI agents (Claude, Cursor, etc.) so they can reason across the network without knowing any platform API.

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  AI Agents (Claude / Cursor / custom)                                │
│                        ▲                                             │
│                    MCP (stdio / HTTP+SSE)                            │
└──────────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  NetCortex Web (FastAPI)                                             │
│  • /api/graph          multi-dimensional topology (Cytoscape.js fmt) │
│  • /api/inventory      flat device list                              │
│  • /api/cam            correlated MAC/ARP table                      │
│  • /api/graph/stp      STP tree per domain                           │
│  • /api/graph/routing  L3 prefix + routing peer table                │
│  • /api/status         adapter health + graph stats                  │
│  • /                   interactive web UI                            │
└──────────────────────────────────────────────────────────────────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                    ▼                 ▼
              ┌──────────┐      ┌──────────┐
              │  Neo4j   │      │  Redis   │
              │ (graph)  │      │ (queue)  │
              └──────────┘      └──────────┘
                    ▲
                    │ ingest
                    │
┌──────────────────────────────────────────────────────────────────────┐
│  NetCortex Worker (background)                                       │
│  • Runs each adapter's discover() on a timer                         │
│  • Merges resulting GraphData into Neo4j                             │
│  • Runs correlation passes (MAC→device, CDP/LLDP→physical links)     │
│  • Runs site correlation (NetBox site lookup by serial)              │
└──────────────────────────────────────────────────────────────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
    ┌──────────┐       ┌──────────┐       ┌──────────┐
    │  Meraki  │       │Catalyst  │       │Intersight│
    │  API     │       │Center API│       │  API     │
    └──────────┘       └──────────┘       └──────────┘
          │                  │                  │
    ┌──────────┐       ┌──────────┐
    │  SNMP v3 │       │  NetBox  │
    │(devices) │       │ (SoT)    │
    └──────────┘       └──────────┘
```

**Secret flow:**
```
.env (AWS creds only)
    → AWS Secrets Manager
        → netcortex/core      (neo4j, redis, netbox URLs)
        → netcortex/adapters/_index  (which adapters are enabled)
        → netcortex/adapters/{type}/{instance}  (per-adapter API keys)
        → netcortex/snmp/default    (SNMP v3 credentials)
        → netcortex/snmp/device/{name}  (per-device SNMP overrides)
```

---

## 3. Project Layout

```
netcortex/
├── adapters/
│   ├── base.py               PlatformAdapter ABC + PlatformProfile
│   ├── __init__.py           adapter registry: load_instances(), get_instances()
│   ├── meraki.py             Cisco Meraki Dashboard API
│   ├── catalyst_center.py    Cisco Catalyst Center (DNAC)
│   ├── intersight.py         Cisco Intersight (UCS/HX/servers)
│   ├── nexus_dashboard.py    Cisco Nexus Dashboard (NDFC)
│   ├── vsphere.py            VMware vSphere
│   ├── generic_rest.py       Schema-mapped generic REST
│   └── snmp.py               SNMP v2c/v3 (IF-MIB, BRIDGE-MIB, LLDP, CDP,
│                              OSPF, BGP, EIGRP, ipAddrTable, ipv6AddrTable)
├── graph/
│   ├── models.py             GraphNode, GraphEdge, GraphData Pydantic models
│   │                         NodeType + EdgeType enums
│   ├── ingest.py             MERGE nodes / replace edges in Neo4j
│   ├── query.py              Named Cypher queries (graph, inventory, STP,
│   │                          routing, CAM, path-finding, stats)
│   ├── correlate.py          Cross-adapter physical link correlation
│   ├── site_correlate.py     NetBox serial→site lookup & compound nodes
│   ├── client.py             Neo4j async driver init
│   └── schema.py             Uniqueness constraints
├── snmp/
│   └── credentials.py        SnmpCredentialResolver, SnmpContext enum
│                              SnmpV3Creds / SnmpV2Creds models
├── models/
│   ├── device.py             NormalizedDevice
│   ├── interface.py          NormalizedInterface
│   ├── vlan.py               NormalizedVLAN
│   └── topology.py           NormalizedTopologyLink
├── status/
│   ├── router.py             /api/status FastAPI router
│   └── templates/index.html  Single-page web UI (Tailwind + Cytoscape.js)
├── config.py                 Settings (NetBox URL, Neo4j URI, Redis URL …)
├── secrets.py                SecretBackend factory (AWS SM / Vault)
├── state.py                  In-process AppState (adapter health, graph counts)
├── main.py                   FastAPI app + all API endpoints
├── worker.py                 Background discovery loop
└── netbox.py                 pynetbox connectivity check
docs/
├── architecture.md           Original design reference (partially superseded)
├── graph.md                  Graph-centric design reference
├── graph-topology.md         Multi-layer topology model spec
├── implementation-journal.md ← THIS FILE — authoritative current state
├── secrets.md                Secret path schema + IAM/Vault policies
├── adapters.md               Adapter development guide
├── access-layer.md           CLI/RESTCONF/NETCONF access layer spec
├── mcp-tools.md              MCP tool reference
├── netbox-integration.md     NetBox field mapping
├── status-page.md            Status page spec
└── sync-engine.md            Sync/diff engine spec
docker-compose.yml
Dockerfile
pyproject.toml
run_worker.sh                 Native macOS worker launcher (bypasses Docker NAT)
```

---

## 4. Secret Backend & Bootstrap

### Supported backends

| Backend | Env vars required |
|---|---|
| AWS Secrets Manager | `SECRET_BACKEND=aws_sm`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| HashiCorp Vault | `SECRET_BACKEND=vault`, `VAULT_ADDR`, `VAULT_TOKEN` (or AppRole) |

All env vars live in `.env` (Docker reads it via `env_file`). The `.env` file must never be committed.

### Bootstrap sequence

1. `netcortex.config.init_settings()` reads `SECRET_BACKEND` from env.
2. The appropriate `SecretBackend` is constructed.
3. `netcortex/core` is fetched: `neo4j_uri`, `neo4j_user`, `neo4j_password`, `redis_url`, `netbox_url`, `netbox_token`, `netbox_verify_ssl`, `sync_interval`.
4. `netcortex/adapters/_index` is fetched: list of `{type, name, enabled}` dicts.
5. For each enabled adapter, `netcortex/adapters/{type}/{name}` is fetched and the adapter is instantiated.

### `sync_interval` override hierarchy

```
netcortex/core.sync_interval                    (global default, seconds)
netcortex/adapters/{type}.sync_interval         (per-adapter-type override)
netcortex/adapters/{type}/{instance}.sync_interval  (per-instance override)
```

---

## 5. Graph Data Model

### Dimensions

Each node and edge carries a `dimension` tag that routes it to the correct visual layer in the UI.

| Dimension | What it represents |
|---|---|
| `physical` | Physical cables, chassis, interfaces |
| `logical` | VLANs, SVIs, VRF memberships |
| `routing` | IP addresses, prefixes, OSPF/BGP/EIGRP peers |
| `stp` | Spanning-tree domains, port states/roles |
| `fabric` | EVPN VNIs, VXLAN overlays, fabric peers |
| `sdwan` | SD-WAN tunnels, policies |
| `virtual` | VMs, virtual networks (vSphere) |

### Node types (`NodeType` enum)

| Label | What it is | Key properties |
|---|---|---|
| `Device` | Physical or virtual network device | `name`, `role`, `platform`, `serial`, `mgmt_ip`, `model`, `os_version`, `snmp_polled`, `stub`, `status`, `status_history`, `status_changed_at`, `meraki_last_reported_at` (ms), `meraki_last_reported_at_iso` (raw) |
| `Interface` | Network interface/port | `name`, `device_id`, `mac`, `oper_status`, `speed` |
| `VLAN` | 802.1Q VLAN | `vid`, `name`, `source` |
| `VRF` | VRF/routing instance | `name` |
| `Prefix` | IP subnet (CIDR) | `cidr`, `version` (4 or 6), `scope` (`vlan`/`vlan6`/`svi`/`svi6`/`static`), `kind` (`vlan_subnet`/`static_route`/`transit`/`wan`), `vlan_id`, `network_id`, `device_serial`, `next_hop` |
| `IPAddress` | Assigned IP on an interface | `address`, `version`, `subnet`, `device` |
| `MACAddress` | Ethernet MAC address | `mac`, `vendor`, `ip`, `vlan`, `source` |
| `ARPEntry` | ARP/NDP binding | `ip`, `mac`, `device`, `source` |
| `STPDomain` | One STP instance (VLAN or MST) | `root_bridge_mac`, `bridge_protocol`, `vlan` |
| `RoutingPeer` | External routing peer (BGP/OSPF neighbor not in graph as Device) | `name`, `protocol`, `peer_ip`, `router_id`, `remote_as`, `stub` |
| `PlatformSite` | Platform-specific container (Meraki network, CATC site) | `name`, `platform` |
| `Site` | Canonical NetBox site | `name`, `slug` |
| `Location` | NetBox hierarchical location under a site | `name` |
| `AutonomousSystem` | External BGP AS (correlator-built) | `asn`, `name`, `is_home`, `dimensions=['wan']` |
| `Internet` | Singleton public-Internet node (correlator-built) | `id='internet:0'`, `dimensions=['wan']` |

**Source-of-truth timestamps on Device.** The two `meraki_last_reported_at*` properties are populated by `MerakiAdapter.discover()` from the `lastReportedAt` field of `getOrganizationApplianceUplinkStatuses`. They power the dev19 staleness policy (see §19) — every `top_problems` `device_down` and `link_down` problem consults the A-side device's `meraki_last_reported_at` and demotes / filters the problem when the dashboard has not refreshed in `top_problems_stale_after_seconds`.

**Status-history scalars on Device.** `status`, `status_history` (JSON timeline ≤200 events, 7-day window), `status_changed_at`, plus four flap-stat scalars (`status_flap_count_1h`/`_24h`, `status_flap_score_1h`, `status_flap_state`) — see §19 for the universal field convention shared with transit-edge `oper_status` fields.

### Edge types (`EdgeType` enum)

| Relationship | Meaning | Dimension |
|---|---|---|
| `PHYSICAL_LINK` | Cable between two devices (LLDP/CDP/API) | physical |
| `HAS_INTERFACE` | Device owns an interface | physical |
| `LOCATED_AT` | Device/Interface → PlatformSite or Location | physical |
| `WITHIN_LOCATION` | Location → parent Location or canonical Site | structural |
| `MAPS_TO_SITE` | PlatformSite → canonical Site (NetBox) | structural |
| `LOGICAL_MEMBER` | Interface carries a VLAN | logical |
| `HAS_SVI` | Device has SVI for a VLAN | logical |
| `ASSIGNED_IP` | Interface → IPAddress | routing |
| `ROUTES_TO` | Device → Prefix (from ipAddrTable/ipv6AddrTable) | routing |
| `ROUTING_PEER` | Device–peer L3 neighbor; protocol=ospf/bgp/eigrp | routing |
| `BGP_PEER` | BGP session (legacy; superseded by ROUTING_PEER) | routing |
| `VRF_MEMBER` | Interface/device belongs to VRF | routing |
| `LEARNED_MAC` | Interface learned a MAC (CAM table entry) | physical |
| `OWNS_MAC` | Device owns a MAC (NIC) | physical |
| `HAS_ARP` | Interface or MACAddress → ARPEntry (IP↔MAC) | physical |
| `STP_MEMBER` | Device participates in STP domain | stp |
| `STP_ROOT` | Device is root bridge for STP domain | stp |
| `STP_LINK` | Interface → STPDomain with `port_state`/`port_role` | stp |
| `VNI_EXTENDS` | VNI maps to VLAN | fabric |
| `FABRIC_PEER` | VTEP-to-VTEP relationship | fabric |
| `VNI_MEMBER` | Device participates in VNI | fabric |
| `HAS_VM` | Host → VM | virtual |
| `VM_NETWORK` | VM → virtual network/port group | virtual |
| `SDWAN_TUNNEL` | SD-WAN tunnel | sdwan |
| `POLICY_APPLIES` | SD-WAN policy → device | sdwan |
| `WAN_UPLINK` | Device → `Internet` (mx_uplink) or Device → AutonomousSystem (ebgp); correlator-built | wan |
| `TRANSITS` | AutonomousSystem → Internet (correlator-built) | wan |

**Transit-edge operational properties (universal contract).** Every edge in `{PHYSICAL_LINK, WAN_UPLINK, SDWAN_TUNNEL, ROUTING_PEER}` carries the same status-history schema — see §19 for the field list (`oper_status` + history + flap stats). Plus type-specific properties:

| Edge | Type-specific properties |
|---|---|
| `PHYSICAL_LINK` | `interface_a`, `interface_b`, `interface_a_raw`, `interface_b_raw`, `discovery_proto`, `media_type`, `speed_mbps`, `speed_bps`, `health_score`, `util_pct`, `error_rate_per_s`, `l3_prefix_v4[]`, `l3_prefix_v6[]` |
| `WAN_UPLINK` | `via` (`mx_uplink` \| `ebgp`), `wan_slot` (`wan1` \| `wan2` for `mx_uplink`), `public_ip`, `private_ip`, `asn`, `peer_ip` (for `ebgp`), `health_score`, `util_pct` |
| `SDWAN_TUNNEL` | `vpn_mode` (`hub` \| `spoke`), `reachability` (raw Meraki value: `reachable`/`unreachable`/`unknown`), `tunnel_type` (`meraki_autovpn`) |
| `ROUTING_PEER` | `protocol`, `address_family`, `local_ip`, `remote_ip`, `local_as`, `remote_as`, `state`, `router_id`, `peer_node_id` |

**SDWAN_TUNNEL.oper_status (0.6.0-dev20).** Derived from `reachability` via the adapter-level mapping `_reachability_to_oper_status` (`netcortex/adapters/meraki.py`):

```
reachable    → up
unreachable  → down
unknown / other / missing  → None (oper_status not set)
```

The `None` case is intentional — `_update_status_history` filters `WHERE r.oper_status IS NOT NULL`, so tunnels the dashboard has no opinion on don't appear in the transition log as fake "unknown" state changes.

### Compound node hierarchy (Cytoscape.js parentage)

```
Site (NetBox canonical)
  └── Location (optional, hierarchical)
        └── PlatformSite (Meraki network, CATC site, etc.)
              └── Device
```

This is expressed via the `parent` field on Cytoscape.js nodes, not as graph edges, so they render as nested compound containers.

### `stub` flag

Nodes with `stub=True` are placeholders created by SNMP discovery (LLDP/CDP neighbors, routing peers) that have not been verified as real devices. They are:
- Excluded from `GET /api/inventory`
- Visible in the topology graph (they contribute edges)
- Eligible for merging with real Device nodes by the correlator

---

## 6. Platform Adapters

### How adapters work

Every adapter implements `PlatformAdapter` (`netcortex/adapters/base.py`):

```python
class PlatformAdapter(ABC):
    name: str                  # e.g. "meraki"
    display_name: str          # e.g. "Cisco Meraki"
    instance_name: str         # e.g. "CPN"
    instance_id: str           # e.g. "meraki/CPN"  (name/instance_name)
    profile: PlatformProfile   # capabilities declaration

    async def authenticate(self) -> None: ...
    async def discover(self) -> GraphData: ...
    async def health_check(self) -> dict: ...
```

`discover()` returns a `GraphData` object (lists of `GraphNode` and `GraphEdge`). The worker calls `discover()` on every adapter, then calls `ingest_graph_data()` to upsert into Neo4j.

### Adapter registry

Adapter instances are loaded from `netcortex/adapters/_index` in the secret backend:
```json
[
  {"type": "meraki",           "name": "CPN",           "enabled": true},
  {"type": "meraki",           "name": "CPNGOV",        "enabled": true},
  {"type": "catalyst_center",  "name": "cpn-ful-catc1", "enabled": true},
  {"type": "nexus_dashboard",  "name": "cpn-ful-nd1",   "enabled": true},
  {"type": "intersight",       "name": "CPN",           "enabled": true},
  {"type": "snmp",             "name": "default",       "enabled": true}
]
```

Multiple instances of the same type are fully supported (e.g., two Meraki orgs, two Catalyst Centers).

### Meraki adapter (`meraki.py`)

- Authenticates via API key in `netcortex/adapters/meraki/{name}` (`api_key`, `org_id`, `base_url`)
- Discovers: devices, networks, VLANs, clients (MAC/IP), LLDP adjacencies, STP per-port state, SD-WAN hub topology
- Produces: Device nodes grouped under PlatformSite (Meraki network), PHYSICAL_LINK edges from LLDP, LOGICAL_MEMBER for VLANs, STP_DOMAIN + STP_LINK + STP_ROOT for spanning tree, SDWAN_TUNNEL for hub-spoke
- Two separate instances (CPN and CPNGOV) with different base URLs (`api.meraki.com` vs `api.gov.meraki.com`) and `verify_ssl=false` for gov
- SNMP polling is layered on top at cloud level (separate SNMP session to Meraki dashboard endpoint on custom port)

### Catalyst Center adapter (`catalyst_center.py`)

- Authenticates via username/password → JWT token
- Discovers: devices (inventory), interfaces, VLANs, topology links, MAC address tables (via CLI command runner), LLDP neighbors
- Produces: Device nodes with OS version, status; PHYSICAL_LINK from topology API; LOGICAL_MEMBER for VLANs; MACAddress + LEARNED_MAC from CAM tables
- Hostname deduplication: `cpn-ash-cat8k1.ciscops.net` and `cpn-ash-cat8k1` are the same device — resolved by serial number match during correlation

### Intersight adapter (`intersight.py`)

- Authenticates via API key ID + RSA private key (request signing, stored in `netcortex/adapters/intersight/{name}`)
- Discovers: compute blades, rack units, HyperFlex clusters, server profiles, fabric interconnects (FIs), vNIC/NIC inventory
- Produces: Device nodes for servers and FIs; PHYSICAL_LINK edges from FI port → server vNIC associations; HAS_INTERFACE edges; LOGICAL_MEMBER for vNIC VLANs

### Nexus Dashboard adapter (`nexus_dashboard.py`)

- Authenticates via username/password → session token
- Discovers: fabric sites, VLANs, VNIs, VTEP peers, MAC tables from NDFC
- Produces: Device nodes, VLAN nodes, VNI nodes, FABRIC_PEER edges, VNI_EXTENDS, LEARNED_MAC

### vSphere adapter (`vsphere.py`)

- Authenticates via vCenter REST API (username/password)
- Discovers: hosts, VMs, port groups, datastores
- Produces: Device nodes for ESXi hosts, HAS_VM edges to VM nodes, VM_NETWORK edges to virtual networks

### SNMP adapter (`snmp.py`) — see section 7 for full detail

---

## 7. SNMP Adapter — Deep Dive

The SNMP adapter is the most complex component. It provides a protocol-agnostic fallback for any device reachable via SNMP, and enriches data from other adapters with protocol-level detail (STP state, routing peers, MAC/ARP tables, IP addresses).

### Design principles

- **No static device list required.** Targets are read from Neo4j: any Device node with `mgmt_ip` set is polled.
- **Hierarchical credential resolution.** Per-device → per-adapter-type → global default (all from AWS Secrets Manager/Vault).
- **Parallel polling.** Up to `max_concurrent` (default 20) devices polled simultaneously via `asyncio.Semaphore`.
- **Hard timeouts.** Per-walk 90s, per-device 300s, Neo4j write 30s — prevents any single device from blocking the cycle.
- **No stub pollution.** LLDP/CDP neighbor names are validated before creating nodes. Garbage names (binary data, pure integers, < 3 chars) are silently dropped.

### Credential resolution order

```
netcortex/snmp/device/{device_name}    → per-device override (highest priority)
netcortex/snmp/adapter/{adapter_type}  → per-platform-type override
netcortex/snmp/default                 → global fallback
```

Each secret contains: `username`, `auth_password`, `priv_password`, `auth_protocol` (SHA/SHA256/MD5), `priv_protocol` (AES128/AES256/DES), `security_level` (authPriv/authNoPriv/noAuthNoPriv).

### Meraki dual-plane SNMP

Meraki has two SNMP planes with different capabilities:

| Plane | Endpoint | Supported priv | What it sees |
|---|---|---|---|
| Cloud | `snmp.meraki.com:port` (from Dashboard API) | AES | Org-wide: all devices, VLANs, STP |
| Device | Management IP:161 | DES only | Per-device: IF-MIB, STP ports |

The `SnmpContext` enum (`CLOUD` vs `DEVICE`) controls which credential set and which OIDs are used. The `SnmpCredentialResolver` enforces DES for device-level polls on Meraki regardless of the credential secret contents.

### MIBs polled per device

| Phase | MIBs | Data produced |
|---|---|---|
| 1 | SNMPv2-MIB, IF-MIB | sysDescr, sysUpTime, ifName, ifAlias, ifPhysAddress, ifOperStatus, ifSpeed |
| 2 | BRIDGE-MIB (CAM) | dot1dTpFdb → MACAddress + LEARNED_MAC edges |
| 2 | IP-MIB (ARP) | ipNetToMediaTable → ARPEntry + HAS_ARP edges |
| 3 | BRIDGE-MIB (STP) | dot1dStp scalars + port table → STPDomain + STP_MEMBER/ROOT/LINK |
| 3 | RSTP-MIB | port roles (backup/alternate/root/designated) |
| 4 | LLDP-MIB | lldpRemSysName/PortId/PortDesc → PHYSICAL_LINK stubs |
| 4 | CISCO-CDP-MIB | cdpCacheDeviceId/Port → PHYSICAL_LINK stubs |
| 5 | OSPF-MIB | ospfNbrTable → ROUTING_PEER edges (protocol=ospf) |
| 5 | BGP4-MIB | bgpPeerTable → ROUTING_PEER edges (protocol=bgp) |
| 5 | CISCO-EIGRP-MIB | cEigrpNbrTable → ROUTING_PEER edges (protocol=eigrp) |
| 6 | ipAddrTable (RFC 1213) | IPv4 addresses → IPAddress + ASSIGNED_IP + Prefix + ROUTES_TO |
| 6 | ipv6AddrTable (RFC 2465) | IPv6 addresses → same as above with version=6 |

### Value decoding

A key source of bugs was pysnmp returning raw pyasn1 objects whose `str()` representation is binary garbage for OctetString fields. Three helpers were added:

```python
_decode_display_str(val)  # DisplayString/OctetString → clean UTF-8, strips non-printable
_decode_ip_val(val)       # IpAddress → dotted-decimal; handles decimal integers too
_is_valid_neighbor_name(name)  # Returns True only for plausible hostnames
```

`_decode_ip_val` is critical for OSPF router IDs: some devices return the 32-bit router ID as a decimal integer (e.g., `1444263578`). The function converts this via `struct.pack("!I", int(s))` to `86.7.x.x`.

### SNMP coverage tracking

After each poll cycle, `_write_snmp_coverage()` writes `snmp_polled=True/False` and `snmp_polled_at=<timestamp>` to each Device node in Neo4j. This is read by the status page to show `✓ catalyst_center/cpn-ful-catc1: 2/5` (2 devices polled of 5 targets).

### Performance characteristics

- **O(N²) problem resolved.** Early versions used `any(n.id == x for n in data.nodes)` to deduplicate nodes — O(N²) when N is thousands of LLDP entries. Replaced everywhere with `seen: set[str]`.
- **Data caps.** LLDP: `max_neighbors=500`. Routing peers: `max_peers=200`. Prevents internet-facing border routers from generating thousands of nodes.
- **Walk timeout.** Each MIB walk has a 90s timeout via `asyncio.wait_for`. Critical for devices with large or slow `ifName` tables.
- **Device timeout.** Each device poll has a 300s hard cap. A single unresponsive device cannot block the entire cycle for 5+ minutes.

---

## 8. Graph Ingest & Correlation

### Ingest (`graph/ingest.py`)

```
ingest_graph_data(GraphData) →
  1. Pre-compute content hashes for every node + edge (sha1 of canonical JSON)
  2. Canonicalize undirected edges so source_id ≤ target_id (swap iface props)
  3. Look up existing node/edge hashes from Neo4j
  4. Purge stale edges for each rel_type owned by this adapter
  5. MERGE nodes by id; skip rows whose stored _content_hash already matches
  6. MERGE only changed edges; touch-only unchanged edges (`last_seen`) by key
  7. MERGE edges by (src, dst, rel) — plus interface_a/interface_b for
     multi-edge types (PHYSICAL_LINK) so parallel cables survive
```

Edge purge is scoped per `(rel_type, source_adapter)` — adapters do not accidentally delete each other's data.

Node MERGE uses `id` as the stable key. Properties are overwritten on each cycle. The `stub` flag must be explicitly set `false` by a real adapter to "promote" a stub node to a real device.

**Multi-edge PHYSICAL_LINK schema (since 0.2.0).** The relationship key
includes `interface_a` and `interface_b` (empty string instead of NULL)
so a switch with three cables to the same neighbor produces three
distinct Neo4j relationships instead of collapsing onto one.
`_MULTI_EDGE_REL_TYPES` controls which rel types behave this way —
currently only `PHYSICAL_LINK`. Content hashing follows the same
identity (`_edge_identity()`) so the hash table also keys per cable.

### Correlation (`graph/correlate.py`)

Runs after all adapters complete in this strict order:

1. **`_merge_neighbor_stubs_by_name()`** — LLDP/CDP stub Devices are
   re-keyed to a real Device with the same hostname (case-insensitive,
   first DNS label). Inbound *and* outbound `PHYSICAL_LINK` edges are
   redirected with the interface pair preserved, then the stub is
   `DETACH DELETE`-ed. A second pass collapses stub-to-stub groups (e.g.
   `lldp-neighbor:foo` and `cdp-neighbor:foo`) into a single canonical
   stub when no real device matches.
2. **`_correlate_via_mac()`** — Inserts a `PHYSICAL_LINK` edge tagged
   `source='correlated', discovery_proto='mac_correlation'` whenever a
   switch port's `LEARNED_MAC` matches a device's `OWNS_MAC`. Skips any
   pair that already has an LLDP/CDP/native-topology edge in either
   direction.
3. **`_correlate_via_arp()`** — Same shape as MAC correlation but uses
   ARP entries on a switch interface that resolve to another device's
   assigned IP. Skips any pair already covered by LLDP/CDP/native
   *or* MAC correlation (ARP is the weakest signal).
4. **`_dedupe_physical_links_by_pair()`** — Three-rule policy:
   1. Group all `PHYSICAL_LINK` edges by undirected pair `(a, b)` with
      `a.id < b.id`.
   2. If any LLDP/CDP/native-topology edge exists for the pair, delete
      every `mac_correlation` / `arp_correlation` edge for that pair.
   3. Sub-group the remaining edges by the canonical interface pair
      `tuple(sorted((iface_a, iface_b)))` so parallel cables on
      distinct ports survive, then keep the highest-priority edge per
      sub-group (priority table in `_PROTO_PRIORITY`).
5. **`_normalize_physical_link_interfaces()`** — Rewrites stored
   `interface_a`/`interface_b` through `normalize_ifname()` (`Vl80` →
   `Vlan80`, `Twe1/1/5` → `TwentyFiveGigE1/1/5`) as a safety net for
   legacy edges or adapters that bypassed normalization at creation.
6. **`_enrich_physical_links_with_health()`** — Copies per-interface
   util/error/health metrics onto the `PHYSICAL_LINK` edge.

### Site correlation (`graph/site_correlate.py`)

Runs after correlation. Queries NetBox for each Device's serial number:
- If found in NetBox: uses NetBox's `site.name` and `site.slug` to create/reference a canonical `Site` node and link `PlatformSite → Site` via `MAPS_TO_SITE`
- Creates compound node hierarchy: `Site → PlatformSite → Device` (expressed as Cytoscape.js `parent` fields)
- Preserves the platform container name in node properties even when the canonical site overrides the visual grouping

### Cytoscape.js compound nodes

The `get_full_graph()` query builds compound node parentage by:
1. Walking `MAPS_TO_SITE`, `WITHIN_LOCATION`, `LOCATED_AT` edges (marked as `_STRUCTURAL_RELS`)
2. Setting `data.parent` on each child node to the container's `id`
3. Never returning structural edges as Cytoscape edges — they are only used for parentage

---

## 9. REST API

All endpoints are in `netcortex/main.py`.

| Method | Path | Description |
|---|---|---|
| GET | `/` | Web UI (single HTML page) |
| GET | `/health` | Docker healthcheck — returns overall status + per-adapter status |
| GET | `/api/status` | Full adapter health, graph stats, SNMP coverage |
| GET | `/api/graph` | Topology graph (Cytoscape.js format); params: `dimension`, `site`, `limit`, `include_interfaces`, `include_mac_nodes` |
| GET | `/api/graph/device/{name}` | 2-hop subgraph around one device |
| GET | `/api/graph/mac-table` | MAC address table (filterable by device/MAC) |
| GET | `/api/graph/correlation` | Physical link correlation statistics |
| GET | `/api/graph/path` | Shortest path between two devices (BFS) |
| GET | `/api/graph/stats` | Node and relationship counts |
| GET | `/api/graph/stp` | STP topology: domains → root bridges → members → port states |
| GET | `/api/graph/routing` | L3 routing: prefixes (IPv4+IPv6) + routing peer table |
| GET | `/api/graph/vlans` | VLAN inventory table rows; optional `site` / `device` filters |
| GET | `/api/inventory` | Flat device list (excludes stub nodes) |
| GET | `/api/cam` | Correlated MAC/ARP table with vendor, port, owner, IPs |
| POST | `/api/adapters/refresh` | Re-check all adapter health (background) |
| POST | `/api/adapters/sync` | Trigger full discovery + ingest cycle (background) |

### Dimension filtering

The topology graph endpoint accepts `?dimension=physical|logical|routing|stp|fabric|sdwan|virtual`. The dimension controls which edge types are returned:

```python
_DIMENSION_RELS = {
    "physical": [PHYSICAL_LINK, HAS_INTERFACE],
    "logical":  [LOGICAL_MEMBER, HAS_SVI, ASSIGNED_IP, VRF_MEMBER],
    "routing":  [ROUTES_TO, BGP_PEER, VRF_MEMBER, ROUTING_PEER, ASSIGNED_IP],
    "stp":      [STP_MEMBER, STP_ROOT, STP_LINK],
    "fabric":   [VNI_EXTENDS, FABRIC_PEER, VNI_MEMBER],
    "sdwan":    [SDWAN_TUNNEL, POLICY_APPLIES],
    "virtual":  [HAS_VM, VM_NETWORK, LOGICAL_MEMBER, HAS_SVI, VNI_MEMBER],
}
```

---

## 10. Web UI

The entire UI is a single Jinja2-rendered HTML file (`netcortex/status/templates/index.html`). It uses:
- **Tailwind CSS** (CDN) for styling
- **Cytoscape.js** + **fcose** layout for interactive network graphs
- Vanilla JavaScript (no framework) for data loading and rendering

### Tabs

| Tab | Content |
|---|---|
| Topology | Interactive graph; dimension buttons (Physical/Logical/Routing/STP/Fabric/SD-WAN/Virtual); search; layout picker |
| Inventory | Sortable/filterable device table — name, role, model, serial, IP, site, adapter, data sources, OS version, status |
| MAC / ARP Table | Correlated MAC table — MAC, vendor, learned-on device/port, VLAN, owner device, NIC, IPs |
| Spanning Tree | Per-STP-domain cards showing root bridge, member devices (sorted by path cost), and port states/roles |
| Routing | Network prefixes (IPv4+IPv6) with attached devices; routing peer table (OSPF/BGP/EIGRP) |
| VLANs | Filterable VLAN inventory table with member devices, sites, and source/provenance |

### Topology features

- **Compound nodes**: devices nest inside PlatformSite containers, which nest inside Location/Site containers
- **Stable zoom**: zoom/pan/dimension state is not reset on background data refresh (only on explicit dimension change)
- **Node detail panel**: clicking a device opens a side panel showing all properties
- **Edge hover**: hovering an edge shows a tooltip with interface names, discovery protocol, and other properties
- **Color coding**: each node type has a fixed color; edge types have semantic colors (red=STP_ROOT, green=ROUTING_PEER, blue=PHYSICAL_LINK, etc.)

### Adapter status panel

Above the tabs, a collapsible table shows each adapter with:
- Status pill (connected / degraded / error)
- SNMP indicator for the `snmp/default` adapter
- Node/edge count contributed last cycle
- Refresh and Sync buttons

### Data source pills in inventory

Each device row in Inventory shows colored pills for each data source:
- `meraki` — data came from Meraki API
- `snmp` — device was successfully polled via SNMP
- Additional sources can be added (future: `netconf`, `restconf`)

---

## 11. Worker & Scheduling

`netcortex/worker.py` is the background process that:
1. Loads all adapter instances (same code path as the web server)
2. Runs a periodic loop: for each adapter, call `discover()` → `ingest_graph_data()` → correlation passes
3. Respects per-adapter `sync_interval` from the secret backend
4. Gates correlation on a full adapter round (one successful discover
   per configured instance) so correlator passes run on a coherent
   snapshot rather than a partial cycle

### Sync interval override hierarchy

```
netcortex/core → default_sync_interval (e.g., 300 seconds)
netcortex/adapters/{type} → sync_interval (e.g., snmp: 600)
netcortex/adapters/{type}/{name} → sync_interval (e.g., for a slow platform)
```

### Retry behavior

Each adapter runs independently. A failure in one adapter does not block others. Errors are logged with `structlog` and the adapter status is updated in Neo4j for display in the UI.

---

## 12. Docker Deployment

### Services

```yaml
services:
  neo4j:     # Graph database
  redis:     # Task queue / coordination
  netcortex: # FastAPI web server (uvicorn)
  # netcortex-worker: # Disabled on macOS — run natively instead
```

The worker container is defined in `docker-compose.yml` but not started by default on macOS because Docker's network isolation prevents it from reaching private management IPs (10.x.x.x, 172.x.x.x) on the corporate network. See section 13.

### Healthchecks

- `neo4j`: waits for Bolt port 7687 to accept connections
- `redis`: uses `redis-cli ping`
- `netcortex`: `GET http://localhost:8000/health` — returns `{"status": "healthy"}` when Neo4j is connected
- `netcortex-worker`: TCP connect to neo4j:7687 (Python one-liner, since redis-cli is not in the image)

### Build

```bash
docker compose build netcortex
docker compose up -d
```

The `Dockerfile` uses a multi-stage build: build stage installs all Python deps, runtime stage runs as non-root user `netcortex`.

---

## 13. Native Worker (macOS)

### Why this is needed

Docker Desktop on macOS uses a Linux VM. Containers cannot reach private network IPs (e.g., `10.x.x.x` device management IPs) without complex VPN routing. The SNMP adapter needs direct UDP:161 access to devices.

**Solution**: run `netcortex.worker` as a native macOS process. It connects to the containerized Neo4j and Redis via `localhost:7687` and `localhost:6379`, but can reach any IP the Mac can route to.

### `run_worker.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
set -a; source .env; set +a

# Point to Docker-hosted services
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export SYNC_BACKEND="${SYNC_BACKEND:-celery}"

exec /opt/homebrew/Caskroom/miniforge/base/bin/python3 -m netcortex.worker
```

Usage:
```bash
# Install native dependencies (once)
pip install -e ".[all]"

# Start
nohup bash run_worker.sh > /tmp/nc_worker.log 2>&1 &

# Monitor
tail -f /tmp/nc_worker.log

# Stop
pkill -f netcortex.worker
```

---

## 14. Secrets Schema

### `netcortex/core`

```json
{
  "neo4j_uri":        "bolt://neo4j:7687",
  "neo4j_user":       "neo4j",
  "neo4j_password":   "...",
  "redis_url":        "redis://redis:6379/0",
  "netbox_url":       "https://netbox.example.com",
  "netbox_token":     "...",
  "netbox_verify_ssl": false,
  "default_sync_interval": 300
}
```

`netbox_verify_ssl` defaults to `true` when omitted. Set it to `false`
for self-signed lab NetBox deployments.

### `netcortex/adapters/_index`

```json
[
  {"type": "meraki",          "name": "CPN",           "enabled": true},
  {"type": "meraki",          "name": "CPNGOV",        "enabled": true},
  {"type": "catalyst_center", "name": "cpn-ful-catc1", "enabled": true},
  {"type": "nexus_dashboard", "name": "cpn-ful-nd1",   "enabled": true},
  {"type": "intersight",      "name": "CPN",           "enabled": true},
  {"type": "snmp",            "name": "default",       "enabled": true}
]
```

### `netcortex/adapters/meraki/CPN`

```json
{
  "api_key":   "...",
  "org_id":    "686235993220619936",
  "base_url":  "https://api.meraki.com/api/v1",
  "verify_ssl": true
}
```

### `netcortex/adapters/meraki/CPNGOV`

```json
{
  "api_key":   "...",
  "org_id":    "...",
  "base_url":  "https://api.gov.meraki.com/api/v1",
  "verify_ssl": false
}
```

### `netcortex/adapters/catalyst_center/cpn-ful-catc1`

```json
{
  "base_url":  "https://cpn-ful-catc1.ciscops.net",
  "username":  "...",
  "password":  "...",
  "verify_ssl": true
}
```

### `netcortex/adapters/intersight/CPN`

```json
{
  "base_url":    "https://intersight.com",
  "api_key_id":  "...",
  "secret_key":  "-----BEGIN EC PRIVATE KEY-----\n..."
}
```

### `netcortex/snmp/default`

```json
{
  "username":       "netcortex",
  "auth_password":  "...",
  "priv_password":  "...",
  "auth_protocol":  "SHA",
  "priv_protocol":  "AES128",
  "security_level": "authPriv"
}
```

### `netcortex/snmp/device/{device_name}` (optional per-device override)

Same structure as `snmp/default`. Takes precedence for that specific device.

### Provisioning commands

```bash
# Create a secret
aws secretsmanager create-secret \
  --name "netcortex/snmp/default" \
  --secret-string '{"username": "netcortex", ...}'

# Update a secret
aws secretsmanager put-secret-value \
  --secret-id "netcortex/snmp/default" \
  --secret-string '{"username": "netcortex", ...}'

# Load key from file (Intersight)
aws secretsmanager put-secret-value \
  --secret-id "netcortex/adapters/intersight/CPN" \
  --secret-string "$(jq -n \
      --arg key_id "$KEY_ID" \
      --arg secret "$(cat secret_key.pem)" \
      '{api_key_id: $key_id, secret_key: $secret}')"
```

---

## 15. Known Issues & Workarounds

### Cat8k1 devices time out on ifName walk

**Symptom**: `snmp.walk.timeout` logged for `cpn-ful-cat8k1` and `cpn-ash-cat8k1` after 90 seconds during `ifName` walk.

**Cause**: These devices have a very large interface table (hundreds of tunnel interfaces, subinterfaces, etc.) that takes >90s to walk via bulk SNMP.

**Workaround**: Increase `walk_timeout` in the SNMP adapter config, or add a per-device secret to skip certain MIBs. The `_SnmpSession` class accepts `walk_timeout` as a parameter.

**Not yet done**: Per-device MIB exclusion list.

### LLDP stub nodes linger between poll cycles — RESOLVED in 0.2.0

LLDP/CDP stub Devices that lose all their relationships are now
garbage-collected by `_housekeeping_loop()` (see `netcortex/worker.py`).
The same loop also evicts orphan `RoutingPeer`, `MACAddress`,
`ARPEntry`, `IPAddress`, and `Prefix` nodes once they no longer have
any incoming edges.

### SNMP priv protocol note

Meraki device-level SNMP (direct poll on port 161) only supports `DES` for privacy. This is enforced by `SnmpCredentialResolver` which overrides `priv_protocol=DES` for Meraki targets in `SnmpContext.DEVICE` context. The global `snmp/default` can use `AES128`.

### IPv6 addresses not yet appearing (as of last poll cycle)

The `_poll_ip_addresses()` function was added in the most recent cycle. IPv6 addresses will appear after the worker completes its next full SNMP poll cycle. The `ipv6AddrTable` (OID `1.3.6.1.2.1.55.1.8`) is queried for all SNMP-responsive devices.

### STP `root_mac` is NULL for Meraki STP domains

Meraki STP data is collected via the Dashboard REST API (per-port state, root bridge election result). The API does not return the root bridge MAC directly. The `root_bridge_mac` field on `STPDomain` nodes from Meraki is therefore NULL; the root bridge is identified by the `STP_ROOT` edge instead.

---

## 16. Current Graph State

As of 0.6.0-dev20 against the live development graph:

| Node type | Count | Primary sources |
|---|---|---|
| Device | ~354 | Meraki (~290), CATC (~5), Intersight (~50), NDFC (~10) |
| Interface | ~510 | Meraki port-statuses, CATC, Intersight, SNMP |
| Prefix | ~120 | Meraki appliance VLANs + static routes + switch SVIs, SNMP `ipAddrTable` |
| MACAddress | ~545 | Meraki clients, CATC hosts, NDFC, SNMP CAM |
| ARPEntry | ~227 | Meraki, SNMP, CATC |
| PlatformSite | ~108 | Meraki (networks), CATC (sites), NDFC (fabrics) |
| VLAN | ~106 | Meraki, CATC, NDFC |
| STPDomain | ~52 | Meraki, SNMP |
| AutonomousSystem | small | correlator (external eBGP peers only — home AS dropped in dev3) |
| Internet | 1 | correlator singleton |

| Transit edge type | Count | Note |
|---|---|---|
| PHYSICAL_LINK | ~133 | Meraki topology + LLDP/CDP + SNMP |
| WAN_UPLINK | ~54 | correlator-built; ~47 `wan1` + ~7 `wan2` slots + 3 `ebgp` (0.6.0-dev20: `wan_slot` exposed via `links_list` slim view) |
| SDWAN_TUNNEL | ~70 | Meraki AutoVPN — 41 up, 29 down (0.6.0-dev20: `oper_status` now derived from `reachability`) |
| ROUTING_PEER | ~1,300 | SNMP (OSPF + BGP) |

| Operational signal | Status |
|---|---|
| `top_problems` `critical` count | ~30 (active SDWAN_TUNNEL outages dominate; staleness policy demoted dormant MX inventory to `info`) |
| Status-history coverage | All four transit edge types + `Device.status` tracked; 70/70 SDWAN_TUNNEL carry `oper_status_history` |
| Adapter source-of-truth timestamps | `meraki_last_reported_at` populated on ~290 Meraki Devices |

**SNMP coverage**: 2/5 Catalyst Center devices (cpn-ful-cat8k2, cpn-ash-cat8k2) are successfully polled. The two cat8k1 devices time out on ifName walk. Meraki cloud endpoint polling adds additional STP and neighbor data.

---

## 17. Versioning Policy

NetCortex follows [Semantic Versioning 2.0](https://semver.org/spec/v2.0.0.html).
Two files must be kept in lockstep:

* `netcortex/__init__.py` — `__version__ = "x.y.z"`
* `pyproject.toml` — `version = "x.y.z"`
* `CHANGELOG.md` — describe what changed

| Bump  | Trigger                                                                  |
| ----- | ------------------------------------------------------------------------ |
| MAJOR | User-declared. Breaking changes or a named product milestone.            |
| MINOR | A new feature — new adapter, new view, new MIB, new endpoint, new schema. |
| PATCH | A bug fix — behavior corrected without adding or removing functionality. |

Every commit that changes behavior must add a `CHANGELOG.md` entry
under the next-pending version section. Bump the appropriate digit at
the same time you commit the change (don't batch bumps).

## 18. Recent Major Changes (since 0.1.0)

A snapshot — the canonical record is `CHANGELOG.md`.

### 0.2.0 (the "big-bang" milestone)

* SNMP v3 harvester rewritten on top of `net-snmp` / `snmpbulkwalk`
  (the `pysnmp` 7.x version deadlocked under concurrent load).
* Per-adapter and per-instance sync-interval overrides.
* Multi-dimensional graph (physical / logical / routing / STP / fabric
  / SD-WAN / virtual) with Cytoscape compound parents.
* Stub merger, MAC + ARP correlation, dedupe with discovery-protocol
  priority, interface-name normalization, health enrichment.
* Per-port spanning-tree, per-VLAN logical membership, IPv4 + IPv6
  prefix discovery via ipAddrTable / ipv6AddrTable.
* Data Explorer endpoint + view.
* Inventory data-source pills + per-adapter SNMP coverage.
* Multi-edge `PHYSICAL_LINK` schema — parallel cables between the same
  two devices each become a distinct Neo4j edge (was: one collapsed
  edge that lost per-port detail). This required updates to ingest
  MERGE, content hashing, stub merger, dedupe, and the housekeeping
  reverse-edge collapse.

### 0.2.1

* Fixed Cytoscape edge-id collision for parallel `PHYSICAL_LINK` edges.
  `get_full_graph()` and `get_device_context()` now include the Neo4j
  relationship id in the Cytoscape edge id.

### 0.4.0 (latest)

* **Strict overlay mode.** UI now sends `strict_overlays=true` so an
  empty overlay selection returns nodes only (no edges) instead of the
  legacy "show everything". Devices without a PlatformSite parent are
  backfilled in nodes-only mode. Non-UI callers retain the old
  back-compat default.
* **Site grouping toggle.** New **Groups** toolbar button shows/hides
  the compound Site/PlatformSite parents. State persists across page
  reloads.

### 0.3.0

* **Multi-overlay topology.** The single-dimension picker is replaced
  by toggleable overlays — Physical, L2 (VLAN+STP), L3 (Routing),
  SD-WAN, Fabric (EVPN), Virtual — selectable in any combination.
  Backend accepts `?overlay=` (repeatable) and returns the UNION of
  the selected edge types. The legacy `?dimension=` parameter still
  works. UI overlay state persists in `localStorage`.
* **MAC vendor enrichment.** A new correlation pass
  (`_enrich_mac_vendors`) annotates every `MACAddress` with its IEEE
  vendor via an in-memory OUI table (`netcortex.util.oui`,
  `mac-vendor-lookup>=0.1.15`). Locally administered MACs return an
  empty string so randomized client MACs don't pollute the table.
* Header version pill is now visible (bordered monospace badge
  instead of muted gray text).

### 0.5.0 → 0.6.0-dev16 (skip-summary)

The 0.5.0 release line and the early-0.6.0 dev cycle introduced
NetCortex's MCP transport, the four-phase agentic-ops surface
(status-history correlator → connectivity-strip UI → Links table →
agentic-ops MCP tools), the streamable-HTTP `/mcp/` mount, and
21+ agentic-ops MCP tools. Per-release detail lives in
[`CHANGELOG.md`](../CHANGELOG.md); the design rationale lives in
[`docs/agentic-ops.md`](agentic-ops.md) and
[`docs/mcp-tools.md`](mcp-tools.md).

### 0.6.0-dev17 → dev20: data-quality stabilisation

A four-release arc that took `top_problems` from "technically correct
but operationally unusable" to "ranked, actionable, source-of-truth-
backed". Each release exists because the previous one's fix was
necessary but insufficient — together they form the contract
documented in §19.

* **dev17** — `apply_transition` seed branch no longer fakes a
  `<field>_changed_at` stamp on first observation. The seed writes
  history JSON (so the connectivity strip has data) but defers the
  `_changed_at` answer to `_stamp_freshness`, which backfills from
  `first_seen`. Before this, every long-standing-down link reported
  as "just went down at <rollout time>" in a 30-ms cluster on first
  boot. Includes a one-shot Cypher cleanup snippet for graphs that
  had already been corrupted.
* **dev18** — `_infer_wan_topology` snapshot/restore was missing
  `r.oper_status` itself. The correlator deletes and re-MERGEs every
  correlator-owned WAN_UPLINK every cycle; without snapshotting
  `oper_status`, the freshly-recreated edge looked like a transition
  to the enrichment query, which re-stamped `_changed_at` every
  cycle. Fix: snapshot AND restore `oper_status` alongside the
  history JSON and flap scalars, using `coalesce` so partially-
  populated snapshots are handled cleanly.
* **dev19** — Cross-verification against the Meraki dashboard
  revealed that the *remaining* `critical` `link_down` entries were
  accurate but mostly not actionable — ~17 of 19 reported MX uplinks
  were on appliances Meraki itself last heard from months ago.
  Introduces the **source-of-truth staleness policy**: every
  `device_down` and `link_down` problem consults the device's
  `meraki_last_reported_at` and is demoted (or filtered) when stale.
  Two new config keys (`top_problems_stale_after_seconds`,
  `top_problems_stale_severity`) live in the `netcortex/core`
  secret. See §19 for the full contract. Adds
  `netcortex.util.timestamps.iso_to_epoch_ms`.
* **dev20** — A second cross-verification against Meraki + Catalyst
  Center exposed six data-quality gaps where the graph either undersold
  what the source-of-truth already had, or lost information between the
  adapter and the MCP-tool projection. All six fixed in one drop:
  * **SDWAN_TUNNEL.oper_status from Meraki reachability** — Meraki
    adapter now maps each peer's `reachability` (`reachable` /
    `unreachable`) onto canonical `oper_status` (`up` / `down`). This
    wires SD-WAN tunnels into the existing history correlator AND the
    `top_problems` `link_down` check, so SD-WAN-only outages now
    surface alongside physical and WAN_UPLINK outages. The dev19
    staleness policy applies unchanged via the A-side MX's
    `meraki_last_reported_at`. `"unknown"` peers leave `oper_status`
    unset (history correlator filters NULLs).
  * **Prefix.kind discriminator** — Meraki adapter stamps a small
    operator-facing taxonomy onto every Prefix: `vlan_subnet` for
    `vlan`/`vlan6`/`svi`/`svi6` scopes, `static_route` for `static`.
    Future scopes (`transit`, `wan`) slot in without schema changes.
  * **Catalyst Center per-switch MAC-address-table fallback** —
    section 5 of CATC discover already creates LEARNED_MAC edges when
    `/v1/host` returns `connectedNetworkDeviceId` +
    `connectedInterfaceName`. New section 5b walks
    `/network-device/{deviceId}/mac-address-table` per switch as a
    fallback so port↔MAC binding gets stitched even when the
    assurance pipeline is empty. Best-effort: schema variations
    (`interfaceNumber` / `ifName` / `portName` / `interface`) are
    handled; per-switch failures degrade to log.debug.
  * **WAN_UPLINK per-slot visibility** — `_infer_wan_topology` has
    always created one WAN_UPLINK edge per slot (wan1/wan2),
    distinguished by `wan_slot`. `links_list` previously dropped
    `wan_slot` from the slim projection; both edges looked identical
    to an agent. `iface_a` now folds in `r.wan_slot` via COALESCE,
    and the slim view exposes `wan_slot`, `via`, and `source_adapter`
    as first-class fields.
  * **`links_list` exposes `source_adapter`** — agents can now tell
    adapter-discovered cables (meraki, catalyst_center, snmp) apart
    from correlator-built edges (WAN uplinks to Internet, AS boundary
    peers) without a second graph round-trip.
  * **Meraki device-name canonicalisation** — dashboard names with
    trailing/leading/internal whitespace (e.g. `"Home MX "`) are now
    trimmed and collapsed at ingest via `_norm_device_name`.
    Cross-system joins (NetBox lookups, `top_problems` grouping,
    history keys) stop silently missing matches.

Three new pure helpers in `netcortex/adapters/meraki.py`
(`_reachability_to_oper_status`, `_scope_to_prefix_kind`,
`_norm_device_name`) own these decision boundaries and are
unit-tested in `tests/adapters/test_meraki_helpers.py` with 24
parametrised cases. The CATC walk uses `import asyncio` for a
semaphore-bounded concurrent fan-out.

## 19. Operational Data Quality (the dev17 → dev20 framework)

This section captures the contracts that the dev17–dev20 arc made
load-bearing. A future AI rebuilding the system from scratch should
implement these invariants from day one, not retrofit them under
operator pressure.

### 19.1 Why this section exists

`top_problems` is the hero MCP tool. An agent calls it first, takes
the rank at face value, and drills in from there. If the ranking is
wrong — either because timestamps are fake (dev17 / dev18) or because
critical-severity rows are actually stale inventory the dashboard
itself has given up on (dev19) — the agent gets misled, the operator
loses trust, and the whole agentic-ops surface collapses to a manual
Cypher session.

Three independent failure modes existed in 0.6.0-dev16:

1. **Manufactured transitions.** Status-history scalars (`_changed_at`,
   `_history`) were stamped on every cycle even when nothing changed,
   so the rank-by-recency order was meaningless.
2. **No source-of-truth staleness signal.** A WAN_UPLINK on an MX the
   dashboard hadn't heard from in 90 days reported with the same
   `critical` severity as one Meraki polled five minutes ago.
3. **Schema drops between adapter and MCP projection.** Information
   the adapter had (Meraki `reachability`, wan slot, source adapter,
   CATC switch MAC tables, Meraki prefix scope) was either not
   promoted onto the graph or was dropped by the slim view, leaving
   `top_problems` unable to surface SDWAN outages, per-WAN-slot
   visibility, or port↔MAC binding.

dev17, dev18, dev19, dev20 — each release fixed exactly one of these
modes, and the contracts below are the result.

### 19.2 Universal status-history contract

Every tracked operational field on every tracked element follows the
same six-property schema. The math lives in `netcortex/graph/history.py`
(unit-tested in `tests/graph/test_history.py`); the per-cycle
application happens in `_update_status_history` in
`netcortex/graph/correlate.py`.

```
<field>                 — current value, e.g. "up"
<field>_changed_at      — epoch_ms of the last *real* transition
<field>_history         — JSON: [[at_ms, new_state], ...]   (≤200 events, 7-day window)
<field>_flap_count_1h
<field>_flap_count_24h
<field>_flap_score_1h   — count_1h / 6.0, saturated at 1.0
<field>_flap_state      — "stable" | "unstable" | "flapping"
```

Classification:

* **flapping** = ≥5 transitions in the last hour
* **unstable** = ≥5 transitions in the last 24h but not the last hour
* **stable**   = neither

Tracked fields today:

| Element        | Field         | Source                          |
|----------------|---------------|---------------------------------|
| `Device`       | `status`      | Adapter (Meraki, CATC, …)       |
| `PHYSICAL_LINK`| `oper_status` | Correlator (`_enrich_*_health`) |
| `WAN_UPLINK`   | `oper_status` | Correlator (`_enrich_wan_uplinks_with_health`) |
| `SDWAN_TUNNEL` | `oper_status` | Adapter via `_reachability_to_oper_status` (dev20) |
| `ROUTING_PEER` | `oper_status` | Adapter / SNMP                  |

Three invariants enforced across all tracked fields:

| Invariant | Where enforced | Why |
|---|---|---|
| `_changed_at` only on *real* transitions | `apply_transition` in `history.py` — seed branch writes history but NOT `_changed_at` | A seed event is "we just started tracking", not "the network just changed" |
| `_changed_at` backfilled from `first_seen` on edges without one | `_stamp_freshness` in `correlate.py` | The UI needs *something* to draw; "first time we saw this edge in its current state" is the honest answer |
| Destructive correlator rebuilds preserve state across the cycle | `_infer_wan_topology` snapshot/restore captures history JSON, flap scalars, `_changed_at`, `first_seen` AND `oper_status` itself | Without `oper_status` in the snapshot, the next enrichment query sees `prev_oper IS NULL` and fakes a transition every cycle (dev18 root cause) |

### 19.3 Source-of-truth staleness policy (dev19)

`top_problems` `device_down` and `link_down` rows consult the A-side
device's `meraki_last_reported_at`. The policy is configurable via
two `netcortex/core` secret keys with defaults shown:

```yaml
top_problems_stale_after_seconds: 86400      # 24 h
top_problems_stale_severity:      info       # "critical"|"warning"|"info"|"filter"
```

The decision matrix:

| Meraki `lastReportedAt`              | Resulting severity                                  |
|--------------------------------------|-----------------------------------------------------|
| within the threshold                 | unchanged (`critical`)                              |
| older than threshold, severity≠filter | demoted to `top_problems_stale_severity`             |
| older than threshold, severity=filter | omitted from the response                            |
| missing (non-Meraki, never reported)  | unchanged — fail open so other adapters aren't silenced |

Every demoted row carries a `stale: true` flag and a
`stale_seconds: N` evidence field, so an agent that wants to widen
its query can still see the inventory.

`top_problems_stale_severity` is validated in `Settings.hydrate` — an
unknown value logs a warning and falls back to the in-memory default.

### 19.4 Adapter-level normalisation contract

Pure helpers in `netcortex/adapters/meraki.py` own the decision
boundary between platform-native values and canonical graph values.
The "pure" constraint matters: each helper is a single-expression
function with no I/O, registered with parametrised unit tests in
`tests/adapters/test_meraki_helpers.py`. A future AI extending this
should follow the same pattern — never embed the mapping inline in
`discover()`.

| Helper | Input | Output | Notes |
|---|---|---|---|
| `_norm_device_name` | dashboard name | trimmed + internal whitespace collapsed | Apply at ingest; cross-system joins (NetBox, history keys) depend on the canonical form |
| `_reachability_to_oper_status` | Meraki `reachability` | `up` / `down` / `None` | `None` for `unknown`/missing — the history correlator's `WHERE oper_status IS NOT NULL` filter then keeps fake "unknown" transitions out of the timeline |
| `_scope_to_prefix_kind` | Meraki prefix scope | `vlan_subnet` / `static_route` / `None` | Extensible: future scopes (`transit`, `wan`) slot in without changing call sites |

### 19.5 MCP projection contract

The slim view used by `links_list` (`netcortex/mcp/tools/agentic_ops.py`)
is the authoritative agent-facing surface for transit edges. Any
field that an agent might filter on, or might use to disambiguate
two otherwise-identical edges, MUST appear in the slim projection —
even if it's empty for some edge types. As of dev20 the slim view
is the union of:

* the universal status-history fields (`oper_status`,
  `oper_status_flap_state`, `oper_status_flap_score_1h`,
  `oper_status_changed_at`, `oper_status_history`),
* the type-specific operational fields listed in §5,
* and three provenance/disambiguator fields:
  * `source_adapter` — `meraki/*`, `catalyst_center/*`, `snmp/*`, or
    empty for correlator-built edges.
  * `wan_slot` — `wan1`/`wan2` for dual-WAN MX uplinks; empty
    otherwise.
  * `via` — `mx_uplink` / `ebgp` for correlator-built WAN_UPLINK
    edges; empty otherwise.

`get_links` in `netcortex/graph/query.py` also COALESCEs
`r.wan_slot` into the canonical `iface_a` field so dual-WAN edges
read as `wan1` / `wan2` in the same column that physical-link edges
use for their port names. This makes the same query work for all
transit edge types.

### 19.6 Version-by-version rationale (one-line index)

| Version | Fix | Lives in |
|---|---|---|
| 0.6.0-dev17 | `_changed_at` no longer stamped on seed | `history.apply_transition`, `correlate._stamp_freshness` |
| 0.6.0-dev18 | `oper_status` preserved across WAN rebuilds | `correlate._infer_wan_topology` snapshot/restore |
| 0.6.0-dev19 | Staleness policy demotes dormant inventory | `mcp.tools.agentic_ops._apply_staleness_policy`, `Settings.top_problems_stale_*` |
| 0.6.0-dev20 (Fix #1) | SDWAN `reachability` → `oper_status` | `meraki._reachability_to_oper_status` |
| 0.6.0-dev20 (Fix #2) | WAN_UPLINK per-slot visibility | `query.get_links` + slim projection in `agentic_ops.links_list` |
| 0.6.0-dev20 (Fix #3) | `links_list` exposes `source_adapter` | slim projection in `agentic_ops.links_list` |
| 0.6.0-dev20 (Fix #4) | CATC MAC-table fallback | `catalyst_center.discover` section 5b |
| 0.6.0-dev20 (Fix #5) | Prefix.kind taxonomy | `meraki._scope_to_prefix_kind` + `list_prefixes` |
| 0.6.0-dev20 (Fix #6) | Device-name canonicalisation | `meraki._norm_device_name` |

The CHANGELOG entries for dev17–dev20 carry the full prose
rationale; this index is the cheat-sheet for "which file owns
this invariant?".

## Appendix A — Adding a New Adapter

1. Create `netcortex/adapters/myplatform.py` implementing `PlatformAdapter`.
2. Implement `authenticate()`, `health_check()`, and `discover()` (must return `GraphData`).
3. Register in `pyproject.toml` under `[project.entry-points."netcortex.adapters"]`:
   ```toml
   myplatform = "netcortex.adapters.myplatform:MyPlatformAdapter"
   ```
4. Add an instance to `netcortex/adapters/_index` in the secret backend.
5. Create the config secret at `netcortex/adapters/myplatform/{instance_name}`.

## Appendix B — Running Queries Directly

```bash
# Connect to Neo4j
docker exec -it netcortex-neo4j cypher-shell -u neo4j -p netcortex

# Example queries
MATCH (d:Device) WHERE d.snmp_polled = true RETURN d.name, d.mgmt_ip;
MATCH (d:Device)-[:STP_ROOT]->(dom:STPDomain) RETURN d.name, dom.root_bridge_mac;
MATCH (a:Device)-[r:ROUTING_PEER]->(b) RETURN a.name, r.protocol, b.name LIMIT 20;
MATCH (d:Device)-[:ROUTES_TO]->(p:Prefix) RETURN d.name, p.prefix ORDER BY p.prefix;
MATCH (d:Device) WHERE d.stub = true RETURN count(d);
```

## Appendix C — Key Design Decisions

| Decision | Rationale |
|---|---|
| Neo4j as the graph store | Native graph queries, Cypher language, Cytoscape.js integration; pluggable via `GraphBackend` interface |
| No separate database | NetBox is the SoT for intended state; Neo4j is for observed/operational state only |
| Secrets never in code or NetBox | External secret backend (AWS SM / Vault) is the only place credentials live |
| Native worker on macOS | Docker network isolation blocks SNMP to private management IPs; native process has full routing table access |
| `stub` flag on unverified nodes | LLDP/CDP/OSPF discovery creates neighbor references that may or may not be real devices; stub flag prevents inventory pollution while keeping topological edges |
| Set-based deduplication in SNMP | O(N²) list scans caused minute-long hangs when processing thousands of LLDP/routing entries; O(1) set lookups fixed this |
| Per-walk SNMP timeouts | A single unresponsive device's large MIB table could block the asyncio event loop for the entire cycle; asyncio.wait_for wraps every walk |
| Dimension-based graph filtering | A single graph contains all topology layers; the UI filters to one dimension via edge type allow-lists rather than maintaining separate graphs |
| Pure helper functions own canonical mappings (dev20) | Decision boundaries between platform values and graph values must be unit-testable in isolation; embedding them inline in `discover()` makes regressions invisible |
| Source-of-truth staleness > generic timeout (dev19) | The dashboard already knows when it last heard from a device; consulting that signal (rather than wall-clock time) means dormant inventory stops dominating `top_problems` without dropping genuinely fresh-but-still-down problems |
| Status-history `_changed_at` only on real transitions (dev17/18) | A correlator-side seed event is not a network event; faking the timestamp on first observation poisons every "rank by recency" query downstream |

## Appendix D — Cross-System Verification Playbook

The dev19 and dev20 fixes both started with a cross-verification
session against the source-of-truth platforms (Meraki dashboard,
Catalyst Center). This appendix captures the repeatable playbook so
the next agent doesn't have to rediscover it.

### When to run it

* Before bumping a major or minor version.
* When `top_problems` starts returning results that "feel" wrong
  (too many criticals, suspicious clustering of timestamps, missing
  outages an operator just saw).
* After adding a new adapter or a new correlator pass that touches
  transit edges.

### Step 1 — Pull both sides in parallel

```python
# Pseudocode — replace with the actual MCP tool calls / adapter APIs.
nc_inventory = mcp.netcortex.inventory_list(limit=500)
nc_links     = mcp.netcortex.links_list(limit=500)
nc_problems  = mcp.netcortex.top_problems(limit=200)

meraki_devices = meraki.getOrganizationDevices(org_id)
meraki_uplinks = meraki.getOrganizationApplianceUplinkStatuses(org_id)
meraki_vpn     = meraki.getOrganizationApplianceVpnStatuses(org_id)
catc_hosts     = catc.get_host_table()
catc_macs      = [catc.get_device_mac_table(dev_id) for dev_id in switch_ids]
```

Always pull paginated results to exhaustion — partial pulls have
fooled past verification runs into reporting fake "missing"
inventory.

### Step 2 — Normalize identifiers on both sides

The two sides use different canonical keys:

| Concept | Meraki | NetCortex |
|---|---|---|
| Device | `serial` | `Device.serial` (preferred) or `Device.name` |
| MX uplink | `(serial, interface)` (`wan1`/`wan2`) | `WAN_UPLINK(Device → Internet, wan_slot=…)` |
| AutoVPN tunnel | `(network_id, peer_network_id)` | `SDWAN_TUNNEL(Device → Device)` |
| Prefix | `(cidr, scope)` | `Prefix(cidr, scope, kind)` |

Trim whitespace, lower-case where appropriate, and use the same
canonical form on both sides before diffing.

### Step 3 — Diff for three patterns

| Pattern | What it usually means |
|---|---|
| In Meraki, not in NetCortex | Missing adapter call, pagination cap hit, or correlator dropped the entity |
| In NetCortex, not in Meraki | Stale inventory the housekeeping loop hasn't garbage-collected, OR Meraki removed it without us noticing |
| In both but property mismatch | Adapter parsing bug, correlator overwriting adapter value, or MCP slim view dropping the property |

The third pattern is the most insidious — it's the one that produced
all six dev20 fixes.

### Step 4 — Capture findings in a structured report

Use one row per discrepancy with these columns:

* **Pattern** (one of the three above)
* **Entity** (canonical id)
* **NetCortex value** (what we expose)
* **Meraki value** (what the dashboard shows)
* **Suspected location** (file:function)
* **Severity** (does it affect `top_problems` ranking? agent decisions? UI accuracy?)
* **Proposed fix** (one of: adapter normalisation, correlator, MCP projection, schema, policy)

### Step 5 — Implement the fix as one focused dev release

Each dev release in the chain should solve exactly one class of
problem, ship with unit tests for any new helper, bump the version,
and update CHANGELOG + this journal in the same commit. Don't batch
unrelated fixes — the chain of evidence in the dev17 → dev18 →
dev19 → dev20 arc only worked because each release could be
verified independently.

### Step 6 — Re-run the verification

Use the same scripts (with the version bumped in any version
assertions). If a new discrepancy appears that wasn't visible
before, you've likely uncovered a second-order effect — log it and
plan a follow-up release. If the targeted discrepancy disappeared
and nothing else broke, ship.

### Reusable verification snippet

A self-contained Python script for running the targeted checks
directly against Neo4j (bypasses MCP, useful when the MCP layer
itself is under suspicion):

```python
# /tmp/nc_verify.py — run inside the netcortex container:
#   docker compose exec netcortex python /tmp/nc_verify.py
import asyncio, os
from netcortex.config import init_settings, get_settings
from netcortex.graph.client import init_client, run_query

async def main() -> None:
    await init_settings()
    s = get_settings()
    await init_client(s.neo4j_uri, s.neo4j_user, s.neo4j_password)

    print("=== SDWAN_TUNNEL oper_status distribution ===")
    rows = await run_query(
        "MATCH ()-[r:SDWAN_TUNNEL]->() "
        "RETURN coalesce(r.oper_status, 'unset') AS s, count(r) AS n "
        "ORDER BY n DESC"
    )
    for r in rows:
        print(f"  {r['s']:>10}  {r['n']}")

    print("=== Prefix.kind distribution ===")
    rows = await run_query(
        "MATCH (p:Prefix) "
        "RETURN coalesce(p.kind, 'unset') AS k, count(p) AS n "
        "ORDER BY n DESC"
    )
    for r in rows:
        print(f"  {r['k']:>14}  {r['n']}")

    print("=== Devices with trailing whitespace in name (should be 0) ===")
    rows = await run_query(
        "MATCH (d:Device) WHERE d.name <> trim(d.name) "
        "RETURN d.name AS name, d.serial AS serial LIMIT 50"
    )
    for r in rows:
        print(f"  {r['serial']:>14}  {r['name']!r}")

    print("=== WAN_UPLINK per-slot counts ===")
    rows = await run_query(
        "MATCH ()-[r:WAN_UPLINK]->() "
        "RETURN coalesce(r.wan_slot, '∅') AS slot, "
        "       coalesce(r.via, '∅') AS via, count(r) AS n "
        "ORDER BY slot, via"
    )
    for r in rows:
        print(f"  slot={r['slot']:>4}  via={r['via']:>10}  {r['n']}")

asyncio.run(main())
```

Keep verification scripts in `/tmp/` (not the repo) — they exist
to capture a moment in time, not to become long-lived test
fixtures. Anything worth keeping graduates into
`tests/integration/` with proper Pytest scaffolding.

---

## 20. Current Sprint State (dev23)

This section captures the current operator-facing behavior as of
`0.6.0-dev23`, including sync controls, Meraki polling defaults, and
MX state semantics.

### 20.1 Manual per-adapter "Sync now" (UI + API)

- Added per-instance sync endpoint:
  - `POST /api/adapters/{adapter_type}/{instance_name}/sync`
- Existing global sync endpoint remains:
  - `POST /api/adapters/sync`
- Adapter table now includes per-row **Sync now**.
- While active, the same button flips to **Syncing…** and shows spinner.
- Running state is backend-driven by `AdapterStatus.sync_running`
  (exposed in `/api/status`) and reconciled by a per-adapter UI watcher
  so the button clears quickly when the adapter finishes.

### 20.2 Meraki default scheduler interval

- Default Meraki sync interval is now **60 minutes** (`3600s`):
  - `Settings.sync_interval_meraki = 3600`
  - docs/examples updated in `README.md`, `docs/sync-engine.md`,
    and `docs/secrets.md`.
- Explicit secret values still override built-in defaults.

### 20.3 MX node state rollup from uplinks + staleness

Historically, many Meraki MX nodes appeared `status=active` even when
both WAN circuits were down, because device inventory status and
per-uplink state came from different signals.

Current behavior:

- `WAN_UPLINK.oper_status` continues to use per-uplink Meraki states
  (`mx_wan1_status` / `mx_wan2_status`) when available.
- Correlation now rolls uplink truth up to `Device.oper_state` for MXs:
  - both WANs down/disabled -> `down`
  - stale `meraki_last_reported_at` (>24h) -> `alerting`
  - any WAN up -> `up`
  - other partial/unknown WAN state -> `alerting`
- Device status history and API projections now prefer `oper_state`
  before static `status`, so UI and MCP consumers observe operational
  state rather than inventory-only state for MX devices.
