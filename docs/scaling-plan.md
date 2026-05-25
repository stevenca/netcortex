# NetCortex — Bug Triage & Scaling Plan

This document covers two things:

1. **Root-cause analysis of the four reported issues**, with specific fixes.
2. **An architectural plan to scale 10×–50×** while adding link/port health tracking and remaining open to more features.

The diagnostic data in section 1 was collected from the running system. Numbers in section 2 use today's totals as a baseline.

---

## Part 1 — Diagnosing the four reported issues

### Issue 1 — No IPv6 addresses

**Diagnosis (confirmed from worker log):**
```
snmp.ipv6_addr_failed  error="'429496729' is not a valid netmask"  host=192.133.161.150
snmp.ipv6_addr_failed  error="'429496729' is not a valid netmask"  host=192.133.176.150
```
The `ipv6AddrTable` walk succeeds, but our code is converting an IPv6 prefix length into a netmask via the wrong code path — `429496729` is `2³² / 10`, i.e. a malformed integer being fed into `ipaddress.IPv4Network(strict=False)` instead of `ipaddress.IPv6Network`.

**Verified in Neo4j**: 14 IPAddress nodes, 0 with `version=6`, 0 Prefix nodes total. So both v4 prefix creation and all v6 collection are broken.

**Fix:**
- Treat IPv4 and IPv6 in completely separate code paths. They share an MIB family but the row encoding is different (IPv6 row index is 16 bytes, IPv4 is 4).
- For IPv6, construct `ipaddress.IPv6Network(f"{addr}/{prefix_len}", strict=False)` directly from the prefix length returned by `ipv6AddrPfxLength`. Never compute a netmask.
- For IPv4, the legacy `ipAddrTable` returns `ipAdEntNetMask` as a dotted-quad string — use that directly.
- Add unit tests with fixtures: `2001:db8::1/64`, `fe80::1/10`, `10.1.2.3/24`.

### Issue 2 — No SNMP status in the UI

**Diagnosis (confirmed from Neo4j):**
```cypher
MATCH (d:Device) WHERE d.snmp_polled = true RETURN count(d)
→ 0
```
SNMP polling **is** running (the worker log shows `snmp.ip_addr_done count=366`, `snmp.bgp_done peers=70`, etc.), but `_write_snmp_coverage()` is not flagging any device. Two possible causes:
- The coverage writer matches devices by `mgmt_ip`, but the Meraki/CATC adapters write the IP under a different property name (e.g., `management_ip`, `ip_address`).
- The MERGE that adapters run after SNMP overwrites the `snmp_polled` property back to NULL because the property is not included in the source adapter's data.

**Fix:**
- Drive coverage off the `target` field that SNMP itself used (not a re-lookup). Record `snmp_polled_at` directly on the Interface or a new `:SnmpPoll` node keyed by `mgmt_ip`, then JOIN at query time.
- Use a separate property namespace owned exclusively by SNMP (`snmp_last_polled_at`, `snmp_status`), set in a dedicated post-poll transaction that does `MATCH ... ON MATCH SET` with no overwrites.
- Show "last polled" as a relative timestamp pill in the UI, not just true/false.

### Issue 3 — STP picture incomplete; no port info; some domains missing

**Diagnosis (confirmed from API):**
```
domains=52
  - name=None: members=3, ports=0
  - name=None: members=1, ports=0
  ...
```
- `name=None` on every STPDomain → the domain node is missing a `name` property. Likely we render `{name or id}` but the `id` carries useful info (`stp:meraki/CPN:L_686235993220637798`).
- `ports=0` on every member → `get_stp_topology()` is not joining `STP_LINK` edges to members. The query returns members but never expands to their interfaces' STP state.
- Some domains missing → adapters not all producing STPDomain nodes. Meraki collects per-port STP state but the Meraki adapter currently only emits a domain node for networks where STP is enabled cluster-wide. SNMP collects domains only for devices where `dot1dStpProtocolSpecification` returns a valid value.

**Fix:**
- Rewrite `get_stp_topology()` as a single Cypher that returns `(STPDomain)<-[:STP_LINK]-(Interface)-[:HAS_INTERFACE]-(Device)` with `port_state`, `port_role`, `path_cost`, `priority`. This gives the UI the full tree in one round trip.
- Derive a friendly STPDomain `name` from the VLAN id, MST instance, or root MAC, and synthesize one in the adapter.
- Add a fallback STPDomain per device in SNMP poll: even if the device reports no per-port state, at least record that the device has STP enabled, so the topology shows a placeholder.

### Issue 4 — Most graphs timing out

**Diagnosis (confirmed via curl):**
| Endpoint | Time | Size |
|---|---|---|
| `/api/graph` (default) | 0.16 s | 544 KB |
| `/api/graph?dimension=routing` | 0.13 s | 600 KB |
| `/api/graph?dimension=logical` | 0.06 s | 227 KB |
| `/api/graph/stp` | 0.05 s | 262 KB |

**The API is fast.** The timeout you see is the browser. Cytoscape.js with the fcose layout chokes above roughly:
- 1,500 nodes for a smooth layout (~5 s)
- 3,000 nodes is painful (~20 s)
- 5,000 nodes is effectively unusable (>60 s, often crashes the tab)

Today's routing graph already has ~1,000 RoutingPeer nodes + ~440 Device nodes — that's the ceiling for fcose. A single SNMP cycle of a border router added 189 OSPF neighbors and 70 BGP peers; another two border routers and we're past the limit.

**Fix (immediate):**
- Default the UI to physical dimension only. Routing/STP only render on explicit selection.
- Add a "max nodes" guard rail server-side: if a dimension query would return >2000 nodes, return an aggregated result instead.

**Fix (architectural — see Part 2):** server-side aggregation and lazy expansion.

---

## Part 2 — Scaling architecture (10×–50×)

### Current scale and growth model

| Class | Today | 10× | 50× |
|---|---|---|---|
| Devices | 441 | 4,400 | 22,000 |
| Interfaces | 328 | 50,000–150,000 | 250,000–750,000 |
| MAC entries | 545 | 50,000+ | 250,000+ |
| ARP entries | 227 | 30,000+ | 150,000+ |
| Routing peers | 972 | 10,000+ | 50,000+ |
| Total nodes | ~5,000 | ~150,000 | ~750,000+ |
| Total edges | ~3,000 | ~500,000 | ~2,500,000+ |

The growth is **superlinear in edges** because edge counts (CAM table, ARP table, LLDP fan-out, routing peers) grow with the network's size *and* its density. Plan for the worst case.

**Neo4j can handle this.** A single Neo4j instance comfortably serves 10⁸ nodes and 10⁹ edges if queries are indexed. The bottlenecks are elsewhere.

### Where it will break first

| Component | Breakage point | Why |
|---|---|---|
| Cytoscape.js (browser) | ~3,000 elements | fcose layout is O(N²) per iteration |
| `/api/graph` JSON payload | ~5 MB | Serialization + network transfer + JSON.parse |
| Worker SNMP cycle | ~500 devices serial | Even at 20 concurrent, polling 5,000 devices serially in one loop takes 30+ min |
| Worker memory | ~50k node objects in RAM | Each adapter builds the full `GraphData` in memory before flushing |
| Adapter ingest cycle | ~10k node MERGEs per transaction | Single transaction, single connection — Neo4j throttles |
| Cypher with unbounded MATCH | ~50k-row scans | Will block other queries; locks accumulate |
| In-process AppState | Per-cycle accumulation | State grows unbounded if not pruned |

### Six architectural changes to make now (before adding features)

#### Change 1 — Server-side aggregation for graph rendering

The UI should never receive more than ~2,000 elements at a time. Implement a **graph LOD (level-of-detail) system**:

```
zoom_level → query strategy
─────────────────────────────────────────────────
overview  → Sites only (compound containers, links between sites)
site      → PlatformSites within the selected Site
network   → Devices within the selected PlatformSite
device    → Interfaces + peers of the selected Device
```

API change:
```
GET /api/graph?level=overview
GET /api/graph?level=site&site_id=<id>
GET /api/graph?level=network&platform_site_id=<id>
GET /api/graph?level=device&device_id=<id>
```

The UI starts at `overview` and expands on click. Backend pre-computes site-to-site rollups (via `MATCH (a:Device)-[:PHYSICAL_LINK]-(b:Device) WHERE a.site <> b.site` aggregated by site). Cache the rollup in Redis with a short TTL.

This single change buys you ~100× UI scale.

#### Change 2 — Real work queue between adapters and ingest

Today: each adapter builds a complete `GraphData` in memory, then a single function call ingests it. At 50× scale, a Meraki org will produce 5–10k nodes per cycle — that's a 50 MB Python list and a multi-minute transaction.

Replace with a Redis Streams (or Celery) pipeline:
```
Adapter discover() → yield batches of 200 nodes/edges → Redis stream
Ingest workers (N processes) → consume from stream → MERGE in parallel transactions
```

Benefits:
- Memory-bounded adapters
- Horizontal scaling of ingest (just add more ingest workers)
- Failure isolation — a bad node doesn't kill the whole cycle
- Backpressure — slow ingest pauses fast adapters

#### Change 3 — Sharded, distributed SNMP polling

Today: one `SnmpAdapter` instance polls every device with an asyncio semaphore of 20. At 10× scale that's 4,400 devices × 6 MIB phases × ~2 s each = ~75 minutes per cycle even if perfectly parallel.

Solution:
- Make SNMP polling a separate worker pool (not one of the platform adapters).
- Shard the target list by consistent hashing across N pollers.
- Each poller is a small process pinned to ~100 devices.
- Run pollers as a Kubernetes Deployment with HPA, or on macOS as a process pool managed by the main worker.

Add per-device adaptive timing:
- Healthy device polled every 5 min for counters, every 60 min for topology.
- Unhealthy device backs off exponentially (5 min → 10 → 20 → 60).
- Unreachable device parked for 1 h before next attempt.

#### Change 4 — Incremental ingest (delta-only writes)

Today: every cycle MERGEs every node and replaces every edge. At 50× scale, that's millions of writes per cycle that mostly change nothing.

Replace with:
```
1. Adapter computes a content_hash per node and per edge bundle (per device).
2. Compare the hash against the cached hash from the last cycle (Redis key per device).
3. Only emit nodes/edges whose hash changed.
4. For deletion, use a per-(adapter, source_device) version number bumped each cycle;
   any node/edge with an older version is purged.
```

Cuts steady-state write load by ~95% in real networks where topology barely changes between cycles.

#### Change 5 — Required indexes and query budgets

Add composite indexes (one-time setup, in `graph/schema.py`):
```cypher
CREATE INDEX device_mgmt_ip       IF NOT EXISTS FOR (d:Device)      ON (d.mgmt_ip);
CREATE INDEX device_snmp_polled   IF NOT EXISTS FOR (d:Device)      ON (d.snmp_polled);
CREATE INDEX device_stub          IF NOT EXISTS FOR (d:Device)      ON (d.stub);
CREATE INDEX device_source        IF NOT EXISTS FOR (d:Device)      ON (d.source_adapter);
CREATE INDEX iface_device         IF NOT EXISTS FOR (i:Interface)   ON (i.device_id);
CREATE INDEX iface_mac            IF NOT EXISTS FOR (i:Interface)   ON (i.mac);
CREATE INDEX mac_addr             IF NOT EXISTS FOR (m:MACAddress)  ON (m.mac);
CREATE INDEX ip_addr              IF NOT EXISTS FOR (i:IPAddress)   ON (i.address);
CREATE INDEX ip_version           IF NOT EXISTS FOR (i:IPAddress)   ON (i.version);
CREATE INDEX prefix_cidr          IF NOT EXISTS FOR (p:Prefix)      ON (p.prefix);
CREATE INDEX stpdomain_root_mac   IF NOT EXISTS FOR (d:STPDomain)   ON (d.root_bridge_mac);
CREATE INDEX site_slug            IF NOT EXISTS FOR (s:Site)        ON (s.slug);
```

Every API handler must enforce a query budget:
- `LIMIT` on every MATCH
- A wall-clock timeout per request (default 5 s, via FastAPI middleware)
- Reject pathological queries (e.g., `MATCH p=()-[*..6]-()`)
- All graph endpoints get cursor-based pagination

#### Change 6 — Observability from day one

Before adding any feature, instrument:
- **Prometheus metrics** on every adapter cycle (duration, nodes-produced, edges-produced, errors), every ingest transaction, every API call (latency histogram, payload size).
- **Structured logs** with a `request_id` correlation tag.
- A small `/metrics` endpoint scraped by a local Prometheus + Grafana stack (add to docker-compose).

You cannot fix what you cannot measure. At 50× scale, "it feels slow" stops working as a diagnostic.

---

## Part 3 — New feature: link/port health tracking

You want every link (PHYSICAL_LINK edge) annotated with bidirectional metrics: utilization %, errors, oper/admin status, STP state, routing state, etc. No history yet — just current.

### Data model

Keep PHYSICAL_LINK as a single edge between two devices. Move per-side detail onto a richer **edge-attribute object** plus per-Interface counter properties.

```
Device(A) ──[:PHYSICAL_LINK { 
                 source_iface_id,      
                 target_iface_id,      
                 source_local_port,
                 target_remote_port,
                 discovery_proto,
                 last_seen_at,
                 source_side_status,   
                 target_side_status,
                 source_side_visible,  
                 target_side_visible
            }]──→ Device(B)

Each Interface gets:
  - oper_status, admin_status
  - last_change_at
  - speed_mbps                     
  - in_octets_latest, out_octets_latest
  - in_errors_latest, out_errors_latest
  - in_discards_latest, out_discards_latest
  - utilization_in_pct, utilization_out_pct
  - last_counter_poll_at
  - stp_port_state, stp_port_role  
  - cdp_lldp_neighbor              
```

Then a link's health is derived from its two endpoint interfaces. The UI shows a red/yellow/green dot on the edge based on `max(util)` and `errors > 0`.

### Counter math (no history)

To compute utilization from byte counters, you need *two* samples, not one. Implementation:

```
Redis key: snmp:counters:<device>:<if_index>
  value: {ts, in_octets, out_octets, in_errors, out_errors, speed}

On each poll:
  - Read previous from Redis
  - If exists, compute delta and rate → write derived metric to Neo4j Interface node
  - Always update Redis with current values

TTL on Redis key: 4 × poll_interval (so a missed cycle doesn't lose state forever)
```

This gives you live "what is the link doing right now" without keeping a time-series.

### Health classification

Per side, compute a `health` enum: `up_clean / up_errors / up_high_util / down / unknown`.

A link's overall health is `worst(side_a_health, side_b_health)`. If only one side is visible (`source_side_visible=true, target_side_visible=false`), the link's health is the visible side's health and is flagged "single-sided" in the UI.

### UI rendering

- On the topology graph, color edges by health. Edge thickness = `max(util_a, util_b)`.
- Hover an edge → tooltip with both sides: port name, oper status, in/out util, errors, last poll time.
- Click an edge → side panel with full health detail and historical counters (when you add history later).
- Inventory gets a new column "Port health" that aggregates per device.

### Polling strategy for counters

- IF-MIB counters (`ifInOctets`, `ifOutOctets`, `ifInErrors`, `ifOutErrors`, `ifHCInOctets`, `ifHCOutOctets`) every 60 s for interfaces that are part of a known PHYSICAL_LINK or have `oper_status=up`. Skip for shut/down interfaces.
- Topology MIBs (LLDP, STP) every 5–15 min.
- Routing peer state every 5 min for peers in `Idle`/`Active`/`OpenSent`, every 15 min for `Established`.

Adaptive cadence prevents the 50× explosion in poll work.

---

## Part 4 — Recommended phased roadmap

I would not implement everything before the next demo. Here is the order that returns the most value with the least risk:

### Phase A — Stabilize what's there (1 sprint)
1. Fix IPv6 polling (issue 1) — separate v4 and v6 code paths, unit tests.
2. Fix SNMP coverage writeback (issue 2) — dedicated property, separate write txn.
3. Fix STP query (issue 3) — single Cypher with port-level join, friendly name synthesis.
4. Add server-side `max_nodes=2000` guard rail and "level=overview" default so the UI never receives more than the renderer can handle (issue 4 mitigation).
5. Add the required Neo4j indexes (Change 5).
6. Cap orphan stub Device nodes — run a periodic cleanup query.

### Phase B — Lay the scaling foundations (2 sprints)
1. Server-side aggregation (Change 1) — overview / site / network / device levels.
2. Composite indexes + per-endpoint query budget (Change 5).
3. Prometheus + Grafana in docker-compose; instrument every cycle and endpoint (Change 6).
4. Adaptive SNMP cadence — counter vs topology, healthy vs unhealthy.

### Phase C — Decouple ingest (1–2 sprints)
1. Move adapter → ingest to a Redis Streams pipeline (Change 2).
2. Add an N-process ingest worker pool (configurable via env).
3. Incremental ingest with content hashes (Change 4).

### Phase D — Link/port health (2 sprints)
1. Counter polling and Redis-backed delta computation.
2. Interface health properties + edge enrichment.
3. UI: edge color/thickness, tooltips, "single-sided" badge.
4. STP/routing per-port state surfaced through the same model.

### Phase E — Distributed SNMP (when you actually hit 5+ pollers' worth of devices)
1. Shard target list, run a poller-pool as a separate process group (Change 3).
2. Per-poller liveness in Redis; main worker reassigns shards on poller death.

### Phase F — History (out of scope today, but plan the seam)
- Once link health is solid, the natural next step is per-interface time-series. Use the same Redis snapshot pattern but write each delta sample to a TSDB (Prometheus is already there) or a Cassandra-style store. Keep Neo4j for *current* state only — never put time-series in a graph DB.

---

## Part 5 — What I want your call on

A few decisions before I start cutting code:

1. **SNMP poller architecture**: process pool inside the existing native worker, or a separate `netcortex-snmp-poller` service that you run with its own systemd/launchd unit? The latter scales better but is more moving parts.

2. **Aggregation/rollup cache**: pre-compute on adapter cycle (write to Redis as a side effect), or compute on first request and cache for TTL? Pre-compute is faster; on-demand is simpler.

3. **`PHYSICAL_LINK` schema migration**: do you want me to keep the current edge and *enrich* it, or do you want a new `Link` node sitting between two `Interface` nodes (so each side's data lives on its own edge)? The node-model is cleaner for asymmetric data but breaks the visual graph parity ("one cable = one line").

4. **Counter polling cadence**: I proposed 60 s for IF-MIB counters. That's a lot of SNMP traffic at 50× scale (~50k interfaces × every 60 s = 833 polls/s). Are you OK with 5 min default and a per-link override for hot links?

5. **Browser graph library**: at 50× scale even Cytoscape with LOD will struggle on the most complex views. Want to leave the door open to swap to **sigma.js** (WebGL, handles 10k+ nodes natively) for the overview-level renderer? That's a UI investment but the only path to "show me everything at once."
