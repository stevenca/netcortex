# Phase F — History Seam (Design)

**Status:** _Design only — no implementation yet._  The user explicitly said
"I do not want to keep history yet" in the scaling discussion that produced
Phases A–E.  Phase F documents the seam so the team can layer history on
top later without rewriting any of Phases A–E.

---

## Goals

1. **Time-series for interface health:** utilization %, error rate, oper
   status changes per interface per minute.
2. **Topology change log:** when a `PHYSICAL_LINK`, `LLDP_NEIGHBOR`,
   `BGP_PEER`, or `STP_LINK` appears, disappears, or changes properties.
3. **STP / routing state transitions:** record state changes (forwarding ↔
   blocking, OSPF up ↔ down) with timestamps.
4. **Inventory churn:** when a Device first appears and last seen.
5. **Long-term retention** (months to years) without bloating Neo4j.

## Non-goals

* Per-packet or per-flow telemetry (use NetFlow/sFlow tooling for that).
* Sub-second resolution.  Cycle-resolution (default 5 min) is enough.

---

## Where the data already lives

Phase D already produces the raw signal we need on every cycle:

* `Interface.in_octets / out_octets / in_errors / out_errors`  (absolute)
* `Interface.rate_in_bps / rate_out_bps / util_in_pct / util_out_pct`
  (delta-derived)
* `Interface.health_score`
* `PHYSICAL_LINK.health_score / util_pct / single_sided`
* `STP_LINK.port_state / port_role`
* `RoutingPeer.state` (from OSPF / BGP MIBs)

What's missing is a *durable, queryable, time-stamped* copy.

---

## Proposed architecture

```
┌───────────────────┐    Phase C    ┌──────────────────┐
│  SNMP / Adapters  │  ──stream──▶  │  Ingest Worker   │
└───────────────────┘               │   (graph write)  │
                                    └─────────┬────────┘
                                              │
                                       Phase F│  fan-out
                                              ▼
                       ┌───────────────────────────────────┐
                       │  History Sink (pluggable)         │
                       │   ┌──── Prometheus remote_write   │
                       │   ├──── InfluxDB / VictoriaMetrics│
                       │   ├──── TimescaleDB (Postgres)    │
                       │   └──── Loki (for event stream)   │
                       └───────────────────────────────────┘
```

### Why a separate sink

Neo4j is optimized for graph traversals, not time-series.  Storing a row
per (interface, timestamp) for thousands of interfaces over months would
explode the page cache and slow every graph query.  Instead:

* **Current state** → Neo4j (already there).
* **Counter samples / change events** → time-series store.

The ingest worker already deserializes every GraphData payload — it is the
right fan-out point.  Adding a sink is a 30-line interface and a config
option, no changes to adapters.

---

## Schema

### Time-series (one row per interface per cycle)

```
metric: netcortex_interface_util_pct
labels: { device, ifname, ifindex, side (in/out) }
value:  float (0–100)
ts:     cycle end time

metric: netcortex_interface_error_rate_per_s
labels: { device, ifname, ifindex, side }
value:  float

metric: netcortex_interface_health_score
labels: { device, ifname }
value:  int (0–100)
```

### Change events (append-only, one row per change)

```
event_type: link_appeared | link_disappeared | port_state_change |
            routing_peer_up | routing_peer_down | device_appeared |
            device_disappeared
ts: float
adapter: str
attrs: { source, target, rel_type, before, after, ... }
```

Detection logic: compare the GraphData against what's currently in Neo4j
(or against the last GraphData payload from the same adapter — Phase C3
hashes already give us a cheap delta signal).

---

## Implementation outline

1. **`netcortex/history/__init__.py`** — public API:
   ```python
   record_counter_sample(device_id, ifindex, sample: dict, ts: float)
   record_change_event(event_type: str, attrs: dict, ts: float)
   ```
2. **`netcortex/history/sinks/`** — pluggable sink classes:
   * `prometheus_remote.py` — POST to a Prometheus remote_write endpoint
   * `influx.py`, `timescale.py`, `loki.py` — straightforward
3. **`netcortex/history/detect.py`** — pure-Python diff:
   * Compare incoming GraphData vs `_content_hash` lookups (already cached
     in Neo4j from Phase C3) → emit change events.
4. **Integration point:** `netcortex.graph.ingest.ingest_graph_data` —
   one call after writing each batch to call into history.
5. **`docker-compose`** — optional services for Prometheus +
   Grafana, off by default.

Estimated effort: ~2 days for a single sink (Prometheus remote_write +
Grafana), ~1 week for the multi-sink + change-event pipeline + UI panel.

---

## Migration path

Phase F is additive:

* All of Phases A–E continue to function unchanged.
* Setting `HISTORY_SINK=prometheus_remote` (or similar) in `.env`
  enables capture.
* Phase B's `/metrics` endpoint already gives a poller-level
  observability signal — Phase F adds the *device-level* time series
  that real network monitoring tools care about.

## Open questions for the future

* Per-link sample retention: 1 sample / 5 min ≈ 100k samples / interface
  / year → cheap in TSDB land, expensive in Neo4j land.
* Should we also feed the SNMP counter samples directly to an external
  TSDB *before* the rate computation, so power users can compute their
  own metrics?  (Probably yes — flag `RAW_COUNTERS_TO_SINK=true`.)
* UI: a "Show timeline for this link" button on edges (server-side
  proxies a Prometheus query and Grafana iframe is overkill).
