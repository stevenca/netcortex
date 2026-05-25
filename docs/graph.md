# NetCortex — Graph-Centric Architecture

This document is the **primary design reference** for NetCortex as it evolves from a NetBox-primary integration into a **graph-centric operational platform**. It describes how live network state, multi-path ingestion, reconciliation against intended inventory, and MCP tooling fit together.

---

## 1. Architecture Overview

### 1.1 High-level system diagram (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              AI / Operators / Integrations                           │
└───────────────────────────────────┬─────────────────────────────────────────────────┘
                                    │ MCP (stdio / HTTP+SSE)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              NetCortex MCP Layer                                     │
│   find_path · list_neighbors · get_device_state · query_graph · list_drift · …       │
│   (thin tools → GraphService + NetBox client; no business logic in tool modules)       │
└───────────────┬─────────────────────────────────────────────┬───────────────────────┘
                │ graph queries (Cypher / backend API)         │ supplementary reads
                ▼                                              ▼
┌───────────────────────────────┐              ┌──────────────────────────────────────┐
│   Network Graph DB (Neo4j*)     │◄────────────►│              NetBox                  │
│   *operational / observed      │  Reconciler  │  *intended / planned inventory       │
│   interfaces, adjacencies,      │  (diff +     │  roles, sites, circuits, tenants,    │
│   protocol state, tunnels, STP  │   policy)    │  planned IPAM, custom fields,        │
└───────────────┬───────────────┘              │  credentials via Secrets integration   │
                ▲                              └──────────────────────────────────────┘
                │ upserts / stream patches
┌───────────────┴───────────────────────────────────────────────────────────────────────┐
│                           Ingestion Layer (adapters + telemetry)                       │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐  ┌──────────────────┐  │
│  │ REST poll   │  │ Webhooks    │  │ Streaming telemetry   │  │ Manual / MCP API  │  │
│  │ (existing)  │  │ (push)      │  │ gNMI / gRPC, SNMP…   │  │ sync trigger      │  │
│  └──────┬──────┘  └──────┬──────┘  └───────────┬──────────┘  └─────────┬─────────┘  │
│         │                 │                      │                      │            │
│         └─────────────────┴──────────────────────┴──────────────────────┘            │
│                                    │                                                   │
│                         PlatformAdapter → push_to_graph(graph_backend, …)             │
│                         Webhook routes per adapter · Telemetry session manager         │
└────────────────────────────────────┬──────────────────────────────────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
   ┌──────────┐               ┌──────────────┐             ┌─────────────┐
   │ Meraki,  │               │ IOS-XR,      │             │ Controllers,│
   │ DNAC, …  │               │ NX-OS, Junos,│             │ cloud APIs  │
   │          │               │ EOS, …       │             │             │
   └──────────┘               └──────────────┘             └─────────────┘
         Platforms                Platforms                   Platforms
```

\* Neo4j is the reference backend; other graph engines are possible behind `GraphBackend`.

### 1.2 Core design principles

| Principle | Meaning |
|-----------|---------|
| **Graph = operational truth** | What is *observed* on the network—interface names, addresses, peerings, tunnels, STP roles, protocol state—lives in the graph as first-class nodes and relationships, optimized for traversals and path queries. |
| **NetBox = intended truth** | What *should* exist—planned devices, rack placement, tenant ownership, circuit records, “official” IP assignments, VLAN definitions—remains authoritative in NetBox. The graph reconciles *against* it, not the other way around for business metadata. |
| **Adapters = ingestion pipelines** | Adapters still abstract vendor APIs, but they **materialize graph state** (and register webhooks), rather than being thin translators whose sole persistence target is NetBox. |
| **Multi-path ingestion** | Poll, push (webhooks), stream (telemetry), and on-demand sync coexist; all paths converge on the same idempotent upsert semantics on the graph. |
| **Explicit linkage** | Graph nodes that correspond to NetBox objects carry `netbox_id` and `netbox_url`. Unsynced discoveries use `netbox_synced: false` until reconciled or created in NetBox. |
| **Thin MCP** | Tools delegate to graph and NetBox clients; no orchestration logic embedded in individual tool files. |

### 1.3 Comparison to the previous model

| Aspect | Previous (NetBox-centric) | Current vision (graph-centric) |
|--------|---------------------------|--------------------------------|
| Primary query store | NetBox REST API | Graph DB (Cypher / backend API) |
| Adapter output | Normalized models → NetBox writes | Normalized models → **graph upserts** (+ optional NetBox via reconciler) |
| Topology / protocol reasoning | Limited; scattered across NetBox objects and journal diffs | Native multi-hop, multi-layer graph queries |
| Operational vs planned | Conflated in one inventory model | **Observed** (graph) vs **intended** (NetBox), with explicit drift |
| Real-time posture | Poll-based sync engine | Poll + webhooks + sub-second telemetry streams |
| MCP tools | NetBox-oriented inventory/topology | **Graph-native** path, peer, anchor, and drift tools |

---

## 2. Graph Data Model

Nodes are labeled by **kind** (below). Additional labels such as `:LayerPhysical`, `:LayerBGP`, etc. may be used for filtering; the canonical **merge keys** should remain stable (`graph_id` or composite natural keys per kind).

### 2.1 Node types and properties

| Label | Properties (illustrative) |
|-------|---------------------------|
| **Device** | `hostname`, `platform`, `vendor`, `model`, `nc_platform_id`, `netbox_id`, `netbox_url`, `netbox_synced`, `last_seen` |
| **Interface** | `name`, `description`, `speed`, `mtu`, `mac_address`, `admin_state`, `oper_state`, `netbox_id`, `netbox_url`, `netbox_synced`, `last_seen` |
| **IPAddress** | `address`, `prefix_length`, `vrf`, `netbox_id`, `netbox_url`, `netbox_synced` |
| **Prefix** | `network`, `prefix_length`, `vrf`, `role`, `netbox_id`, `netbox_url` |
| **VRF** | `name`, `rd`, `netbox_id`, `netbox_url` |
| **VLAN** | `vid`, `name`, `tenant`, `netbox_id`, `netbox_url` |
| **BGPSession** | `local_as`, `peer_as`, `local_ip`, `peer_ip`, `state`, `hold_time`, `uptime`, `families` |
| **OSPFAdjacency** | `area`, `router_id_local`, `router_id_peer`, `state`, `interface` |
| **EVPNInstance** | `vni`, `vrf`, `type`, `rd`, `rt_import`, `rt_export` |
| **SDWANTunnel** | `tunnel_id`, `src_endpoint`, `dst_endpoint`, `transport`, `sla_class`, `path_quality_score`, `state` |
| **STPInstance** | `vlan_id`, `root_bridge_priority`, `root_bridge_mac` |

Platform-only entities may omit `netbox_id` until reconciliation; they **must** set `netbox_synced: false` where the field applies.

### 2.2 Relationship types

| Type | Usage |
|------|--------|
| `HAS_INTERFACE` | `(:Device)-[:HAS_INTERFACE]->(:Interface)` |
| `HAS_IP` | `(:Interface)-[:HAS_IP]->(:IPAddress)` (or Device-level loopbacks as modeled) |
| `CONNECTED_TO` | Physical / logical link between two `Interface` nodes (LLDP/CDP/API-derived) |
| `BGP_PEER_WITH` | Between devices or session nodes representing BGP adjacency |
| `OSPF_NEIGHBOR` | OSPF adjacency between routers (often via `OSPFAdjacency` node pattern) |
| `EVPN_PEER` | EVPN control-plane or VTEP peer relationship |
| `SDWAN_TUNNEL_TO` | SD-WAN tunnel endpoints |
| `STP_PORT_TO` | STP role/topology attachment from `STPInstance` to `Interface` |
| `IN_VRF` | Address or prefix scoped to a VRF |
| `IN_VLAN` | Interface or L2 entity membership |
| `ANCHORS_TO` | **Cross-layer** link (e.g. BGP session anchors to underlying interface or IP) |

### 2.3 Example schema constraints (Cypher)

```cypher
// Uniqueness for graph-managed identity (adjust prefix to your conventions)
CREATE CONSTRAINT device_graph_id IF NOT EXISTS
FOR (d:Device) REQUIRE d.graph_id IS UNIQUE;

CREATE CONSTRAINT interface_graph_id IF NOT EXISTS
FOR (i:Interface) REQUIRE i.graph_id IS UNIQUE;

// Fast correlation back to NetBox
CREATE INDEX device_netbox_id IF NOT EXISTS
FOR (d:Device) ON (d.netbox_id);

CREATE INDEX interface_netbox_id IF NOT EXISTS
FOR (i:Interface) ON (i.netbox_id);
```

### 2.4 Example: device with interface, IP, and physical neighbor

```cypher
MERGE (d:Device {graph_id: 'device:netbox:1234'})
ON CREATE SET d.hostname = 'sw-core-01', d.netbox_id = 1234, d.netbox_synced = true
SET d.last_seen = datetime()

MERGE (i:Interface {graph_id: 'iface:netbox:8810'})
ON CREATE SET i.name = 'Ethernet1/1', i.netbox_id = 8810, i.netbox_synced = true
SET i.admin_state = 'up', i.oper_state = 'up', i.last_seen = datetime()

MERGE (d)-[:HAS_INTERFACE]->(i)

MERGE (ip:IPAddress {graph_id: 'ip:10.0.0.2/30'})
SET ip.address = '10.0.0.2', ip.prefix_length = 30, ip.vrf = 'default',
    ip.netbox_synced = true

MERGE (i)-[:HAS_IP]->(ip)

MERGE (nbr:Interface {graph_id: 'iface:peer:aa:bb:cc:dd:ee:ff'})
SET nbr.name = 'Gi0/1', nbr.netbox_synced = false

MERGE (i)-[:CONNECTED_TO {discovery: 'lldp', last_seen: datetime()}]->(nbr);
```

### 2.5 Example: BGP session anchored to local interface

```cypher
MERGE (s:BGPSession {graph_id: 'bgp:device:1234:peer:10.1.1.1:vrf:5'})
SET s.local_as = 65000, s.peer_as = 65001,
    s.local_ip = '10.1.1.2', s.peer_ip = '10.1.1.1',
    s.state = 'Established', s.families = ['ipv4 unicast']

WITH s
MATCH (i:Interface {graph_id: 'iface:netbox:8810'})
MERGE (s)-[:ANCHORS_TO {role: 'local_transport'}]->(i);
```

---

## 3. What Stays in NetBox

The graph is **not** a NetBox replacement. The following remain **intended-state** and **business-context** sources:

| Domain | In NetBox |
|--------|-----------|
| Hierarchy | Sites, regions, locations, racks, elevation |
| Taxonomy | Device roles, device types, manufacturers, platforms |
| Circuits | Providers, circuits, terminations |
| Tenancy | Tenants, contacts, contracts (as modeled) |
| Planned IPAM | Authoritative prefix/IP *assignments* for provisioning workflows |
| VLAN database | Administrative VLAN definitions (graph may show *observed* usage) |
| Custom fields | Business metadata, ticketing IDs, compliance tags |
| Secrets linkage | References to credentials in the secret backend—not raw secrets in NetBox |

**Split responsibility example:** NetBox holds that `10.0.0.0/24` is assigned to *Site A* and a specific VLAN; the graph holds which interfaces actually carry addresses in that subnet *right now*, which BGP sessions export it, and which SD-WAN path carried packets last.

---

## 4. Ingestion Layer

### 4.1 Adapter interface evolution

Existing methods (`authenticate`, `list_devices`, `list_interfaces`, …) remain the **abstract view of platform state**. A new contract method projects that state into the graph:

```python
async def push_to_graph(self, graph_backend: GraphBackend, instance: str) -> None:
    """Upsert nodes/edges for this adapter's snapshot into the operational graph."""
```

Semantics:

- **Idempotent merges** keyed by stable `graph_id` / correlation (`nc_platform_id`, `netbox_id`).
- **Partial updates** allowed per device or per layer to limit write volume.
- Adapters may call `graph_backend.wipe_layer(layer_label)` only under orchestrated rebuild policies—not on every poll.

### 4.2 Webhook registration

- Each adapter **may** expose a **webhook URL prefix**, e.g. `/hooks/{adapter_name}/{instance_id}/{secret_token}`.
- The FastAPI app routes payloads to the adapter’s parser; parsers map events to **graph upserts** (and optionally enqueue reconciliation).
- Registration details (callback URL, signing secret) live in NetBox **configuration metadata** or the secret backend—not hardcoded.

### 4.3 Streaming telemetry

| Transport | Platforms (illustrative) | Notes |
|-----------|--------------------------|-------|
| **gNMI / gRPC** | IOS-XR, NX-OS, Junos, Arista EOS | Subscribe to YANG paths for interfaces, BGP, OSPF, LLDP, etc. |
| **SNMP traps** | Generic fallback | Event-driven *hints*; may require poll for full context |

**Telemetry session manager** responsibilities:

- Maintain long-lived streams per device/credentials; **exponential backoff** on failures.
- Map **gNMI paths** → graph **node/edge property updates** (batch writes to avoid per-update transactions).
- Track **subscription health** (last sync, error counters) for `get_telemetry_status` MCP tool.

### 4.4 Manual and API triggers

- Operators or MCP clients invoke **full graph refresh**, **per-adapter refresh**, or **per-device patch** without waiting for schedule.
- The same code paths are used by the worker/scheduler for consistency.

---

## 5. Reconciliation Engine

### 5.1 When it runs

- **Scheduled** (default **every 5 minutes**) for drift detection and batch tidy-up.
- **Event-driven** after significant webhook or telemetry bursts (debounced to avoid thrash).
- **On-demand** via API/MCP for operational response.

### 5.2 Comparison strategy

For each linked entity (`netbox_id` present):

- Compare graph **observed** fields to NetBox **intended** fields where both exist.
- For graph-only nodes (`netbox_synced: false`), classify as **unmanaged**, **pending create**, or **stale observation**.

For NetBox objects **missing** from the graph:

- Flag **missing from graph** and trigger **adapter poll** or targeted graph rebuild.

### 5.3 Reconciliation actions (examples)

| Condition | Action |
|-----------|--------|
| Graph has **new device** from LLDP/CDP not in NetBox | Mark `unmanaged`; optionally **auto-create** NetBox device via policy |
| NetBox has device **never seen** in graph | `missing_from_graph` → enqueue adapter coverage job |
| **IP drift** (observed ≠ planned assignment) | Record drift event; do not silently overwrite IPAM without policy |
| **New link** in graph, no `dcim.Cable` | Flag for NOC review; optional ticket / journal entry |
| Graph contradicts NetBox role/site | `divergence` with severity; human decision |

### 5.4 Event log / query surface

- Reconciliation emits **structured events** (timestamp, entity refs, severity, recommended action).
- Events are **durable** (graph nodes, NetBox journal entries, or append-only store—implementation choice) and **MCP-queryable** via `list_drift` and filtered APIs.

---

## 6. MCP Tools (Graph-Native)

These tools **prefer graph reads**; NetBox supplements fields the graph does not model.

| Tool | Purpose |
|------|---------|
| `find_path(src, dst, layer)` | Shortest (or constrained) path between entities for `physical`, `bgp`, `ospf`, `sdwan`, etc. |
| `list_neighbors(device, layer)` | Neighbors at a given layer with key relationship metadata. |
| `get_device_state(device)` | Consolidated view: interfaces, IPs, peers, tunnels, anchors. |
| `get_protocol_peers(device, protocol)` | BGP / OSPF / EVPN / SD-WAN peer listings from live graph state. |
| `get_cross_layer_anchors(node_id)` | All `ANCHORS_TO` and related cross-layer memberships. |
| `query_graph(cypher)` | **Power users**: read-only Cypher with guardrails (role, allowlist, timeouts). |
| `list_drift()` | Open reconciliation diffs between graph and NetBox. |
| `get_telemetry_status()` | Active streaming sessions, last message age, error state. |

**Example: shortest physical path between two devices**

```cypher
MATCH (src:Device {hostname: $src}), (dst:Device {hostname: $dst})
MATCH p = shortestPath(
  (src)-[:HAS_INTERFACE|CONNECTED_TO*..50]-(dst)
)
RETURN [n in nodes(p) |
  coalesce(n.hostname, n.name, n.graph_id)
] AS hop_names;
```

**Example: BGP peers for a device**

```cypher
MATCH (d:Device {hostname: $host})-[:HAS_INTERFACE|BGP_PEER_WITH*0..6]-(s:BGPSession)
RETURN s.peer_ip AS peer, s.peer_as AS asn, s.state AS state, s.families AS afis;
```

---

## 7. GraphBackend Abstraction

### 7.1 Interface sketch

```python
class GraphBackend(ABC):
    @abstractmethod
    async def upsert_node(
        self,
        label: str,
        identity_props: dict[str, object],
        all_props: dict[str, object],
    ) -> None: ...

    @abstractmethod
    async def upsert_edge(
        self,
        src: dict[str, object],
        dst: dict[str, object],
        rel_type: str,
        props: dict[str, object],
    ) -> None: ...

    @abstractmethod
    async def delete_node(
        self,
        identity_props: dict[str, object],
        label: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def query_cypher(
        self,
        query: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]: ...

    @abstractmethod
    async def wipe_layer(self, layer_label: str) -> None: ...
```

Exact signatures may evolve; the intent is **async**, **idempotent writes**, and **parameterized reads**.

### 7.2 Implementations

| Backend | Use case |
|---------|----------|
| **Neo4jBackend** | Production. Official **async** Neo4j driver, connection pooling, retries on transient errors (`ServiceUnavailable`, deadlocks). |
| **NetworkXBackend** | **Unit tests only** — in-memory, deterministic graphs. |

### 7.3 Registration (`pyproject.toml`)

```toml
[project.entry-points."netcortex.graph_backends"]
neo4j = "netcortex.graph.backends.neo4j:Neo4jBackend"
networkx = "netcortex.graph.backends.networkx:NetworkXBackend"
```

Selection via configuration (from the secret backend in production): `GRAPH_BACKEND=neo4j|networkx`.

---

## 8. Deployment Changes

### 8.1 Docker Compose — `graph` profile

Neo4j runs under a Compose **profile** so minimal stacks remain lightweight:

```bash
docker compose --profile graph up
```

Services (conceptual):

| Service | Role |
|---------|------|
| `neo4j` | Graph database |
| `netcortex` | API + MCP + webhook receivers |
| `netcortex-worker` | Scheduled poll + reconciliation |
| **`netcortex-telemetry`** | gNMI / streaming collectors (scalable replicas) |

### 8.2 Configuration

| Variable | Purpose |
|----------|---------|
| `GRAPH_BACKEND` | `neo4j` \| `networkx` |
| `NEO4J_URI` | e.g. `bolt://neo4j:7687` |
| `NEO4J_USER` / `NEO4J_PASSWORD` | **Resolved via secret backend** in production; env vars acceptable only for local dev |

Telemetry credentials follow existing NetCortex rules: **secret backend**, not plaintext in repo.

### 8.3 Status page extensions

Additional panels / metrics:

- **Graph DB health** (round-trip, cluster role if applicable)
- **Active telemetry streams** count + unhealthy subscriptions
- **Reconciliation** last-run timestamp, duration, error count
- **Drift** open items count by severity

---

## 9. Migration Path from Current Design

| Phase | Scope | Outcome |
|-------|-------|---------|
| **Phase 1** | Deploy graph **alongside** existing NetBox-primary sync | Graph is **additive**; adapters (or a builder) populate it read-only from current normalized models; MCP gains read-only graph probes behind a flag |
| **Phase 2** | Adapters implement `push_to_graph` | Graph becomes **primary** for topology/path queries; NetBox sync may continue in parallel for inventory |
| **Phase 3** | Webhooks + telemetry | Graph approaches **real-time**; stale TTLs shortened per layer |
| **Phase 4** | Reconciliation engine policies enabled | **Controlled write-back** to NetBox for discoveries; drift workflow mature |

Each phase should ship with **rollback**: disable graph reads in MCP, fall back to NetBox API tools, and stop telemetry workers without data loss to NetBox.

---

## 10. Relationship to Other Documents

- **[architecture.md](architecture.md)** — Historical and component-level description of the NetBox-centric implementation; superseded for strategic direction by this document.
- **[graph-topology.md](graph-topology.md)** — Earlier multi-layer topology plan; retained for layering **vocabulary** until fully merged into implementation guides.
- **[adapters.md](adapters.md)** — Adapter author guide; will gain `push_to_graph`, webhook, and telemetry notes as the code catches up.

---

## Summary

NetCortex’s **operational heart** moves to a **pluggable graph database** holding observed topology and protocol state across multiple ingestion paths. **NetBox** remains the **intended-state** system of record for inventory and business/IPAM semantics. A **reconciliation engine** continuously aligns the two, surfacing drift and optionally writing discoveries back under policy. **MCP tools** become **graph-native** for speed and expressiveness, while NetBox supplies supplementary context that does not belong in the graph.
