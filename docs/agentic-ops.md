# Agentic Ops

NetCortex was built so a model (Cursor, Claude, a custom LLM agent)
can answer operator-grade network questions without a human
hand-walking it through every Cypher query and graph traversal.

This document describes the five-phase data path that makes that
possible — from "we know the current state" to "we know what's
*changing* and why" to "an agent can call one tool and get the
ranked problem list".

> **TL;DR for agent prompt engineers**: tell your agent to call
> [`top_problems`](mcp-tools.md#top_problems--the-hero-tool) first.
> It runs every health check and returns a ranked list with stable
> `problem_type` strings; the agent then drills in with `history_get`
> / `topology_get` / `links_list` / `peers_list` as needed.

---

## The five phases

### Phase A — Status history correlator

Every transit edge, every device status, and every routing peer
has a `*_history` JSON string + four derived flap statistics
recorded directly on the graph element.  No new datastore — Neo4j
remains the only persistent backend.

Schema (per tracked field):

```
<field>             — current value, e.g. "up"
<field>_changed_at  — epoch_ms of the last transition
<field>_history     — JSON: [[at_ms, new_state], ...]   (≤200 events, 7-day window)
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

| Element        | Field         | Where the value comes from                       |
|----------------|---------------|--------------------------------------------------|
| `Device`       | `status`      | Adapter (Meraki, CATC, …)                        |
| `PHYSICAL_LINK`| `oper_status` | Correlator (`_enrich_*_health`)                  |
| `WAN_UPLINK`   | `oper_status` | Correlator (`_enrich_wan_uplinks_with_health`)   |
| `SDWAN_TUNNEL` | `oper_status` | Meraki `reachability` via `_reachability_to_oper_status` (0.6.0-dev20) |
| `ROUTING_PEER` | `oper_status` | Adapter / SNMP                                   |

Invariants the correlator enforces on every tracked field:

* `_changed_at` is stamped only on **real** transitions, never on
  the first observation of an element (dev17). Seed events write
  history JSON only — `_stamp_freshness` then backfills
  `_changed_at = first_seen` for elements that don't have one yet.
* Destructive rebuilds (`_infer_wan_topology` deletes and re-MERGEs
  every correlator-owned WAN_UPLINK each cycle) snapshot AND restore
  `oper_status` itself, not just the history JSON (dev18). Without
  this, the next enrichment query sees `prev_oper IS NULL`, fakes a
  transition, and re-stamps `_changed_at` every cycle.
* Adapter mappings that have no opinion (`reachability="unknown"`,
  missing values) leave `oper_status` unset; the correlator's
  `WHERE r.oper_status IS NOT NULL` filter then keeps fake
  "unknown" transitions out of the timeline.

Implementation: pure-Python history math in
`netcortex/graph/history.py` (fully unit-tested in
`tests/graph/test_history.py`), correlator wiring in the
`_update_status_history()` step of `netcortex/graph/correlate.py`.
The correlator handles the WAN_UPLINK destructive-rebuild cycle
with a snapshot-and-replay pass so history isn't wiped between
ingest cycles.

### Phase B — Connectivity-strip UI

The Phase-A data shows up in the topology view as a horizontal
green/red/amber timeline strip — modelled on the operator's
reference screenshot — rendered as a pure SVG component
(`createConnectivityStrip()` in `index.html`).

Three surfaces:

* **Edge hover tooltip** — 24h compact strip under the status pill.
* **Device hover tooltip** — 24h compact strip + flap badge.
* **Detail panel (sidebar)** — 7-day wide strip with axis labels;
  each segment has a hover tooltip showing `<STATE> for <duration>,
  started <wall-clock>`.

The state→color map deliberately covers every status vocabulary we
track (oper_status, state, status), so the same helper works for any
new tracked field added in the correlator without per-call config.

Flap badges (`⚡ FLAPPING` red, `⚠ unstable` amber) surface alongside
the status pill so flapping objects are visible without opening the
detail panel.

### Phase C — Links table

Every "transit" edge in the network on one filterable, sortable,
chip-scopable page (`/api/links` → "Links" tab).  This is the "where
do I look first?" view for an on-call engineer.

Covers `PHYSICAL_LINK`, `WAN_UPLINK`, `SDWAN_TUNNEL`,
`VXLAN_TUNNEL`.  Deliberately omits `ROUTING_PEER` (control plane,
covered by the Routing view) and `LOGICAL_MEMBER` (semantic
membership, not transit).

Default sort: server pre-sorts by `flap_score_1h DESC,
oper_status_changed_at DESC, health_score DESC` so the most
operationally urgent rows are row 1 even on a 1000-link fleet.

Inline 24h connectivity strip per row — same SVG component as the
hover tooltips, single source of truth.

Filters: chip filter (sites/devices, matches either side), type
select, status select, "flapping only" checkbox, free-text search.
Live footer: `<rendered>/of/total links · N down · M flapping`.

### Phase D — MCP tools

Nine single-purpose tools, ~700 lines in
`netcortex/mcp/tools/agentic_ops.py`, exposed over streamable-http
at `/mcp/`.  See [MCP Tools Reference](mcp-tools.md) for full
schemas.

| Tool             | Diagnostic question                                       |
|------------------|-----------------------------------------------------------|
| `top_problems`   | Run all health checks, rank the issues                    |
| `inventory_list` | What devices exist, what's their state?                   |
| `topology_get`   | How is device X connected?                                |
| `links_list`     | Which transit edges are flapping/down/busy?               |
| `peers_list`     | Which routing adjacencies are down or unstable?           |
| `paths_find`     | Shortest path between A and B                             |
| `history_get`    | Fetch 7-day transition history for an element             |
| `mac_lookup`     | Where is this MAC learned?                                |
| `ip_lookup`      | Where does this IP / prefix live?                         |

Design principles (per workspace MCP-security rule):

* **Single-purpose** — each tool answers one diagnostic question.
  No "do anything" tools.
* **Bounded output** — every tool caps at 50 rows (configurable),
  hard cap 500, with an explicit truncation indicator so an agent
  can paginate.
* **Stable field names** — match the REST API surface so the same
  field name means the same thing across UI, JSON, and MCP.
* **Self-explaining** — each docstring names the diagnostic
  question and points to the top-20-problems map.
* **Thin** — every tool delegates to a `netcortex.graph.query`
  function; zero business logic in the MCP layer.
* **Database-first filtering** — high-cardinality tools (`peers_list`,
  `top_problems`, `mac_lookup`) push filtering/limits into Cypher so
  MCP calls don't materialize whole-fleet tables just to drop rows in
  Python.

### Phase E — Source-of-truth staleness policy (dev19)

Phases A–D made `top_problems` *correct* — every reported issue
maps to a real graph element with an honest timestamp. They did
not make it *operationally useful*: a graph with two-month-old MX
inventory still surfaced ~17 of 19 WAN_UPLINK outages as `critical`
even though the dashboard itself had given up on those appliances
months ago. The agent then took the rank at face value and burned
context on dead inventory.

Phase E demotes (or filters) `device_down` and `link_down` problems
when the A-side device's source-of-truth timestamp is older than a
configurable threshold. Two `netcortex/core` secret keys with their
defaults:

```yaml
top_problems_stale_after_seconds: 86400      # 24 h
top_problems_stale_severity:      info       # "critical"|"warning"|"info"|"filter"
```

Decision matrix:

| `meraki_last_reported_at` age | Outcome |
|---|---|
| Within threshold | unchanged (`critical`) |
| Older than threshold, severity ≠ `filter` | demoted to `top_problems_stale_severity` |
| Older than threshold, severity = `filter` | omitted from the response |
| Missing (non-Meraki, never reported) | unchanged — fail open so other adapters aren't silenced |

Every demoted row carries `stale: true` and a `stale_seconds: N`
evidence field, so an agent that wants to widen its query can still
see the inventory. The policy is implemented in
`_apply_staleness_policy` in `netcortex/mcp/tools/agentic_ops.py`
and consults `Device.meraki_last_reported_at`, which the Meraki
adapter stamps from `getOrganizationApplianceUplinkStatuses`'s
`lastReportedAt` ISO timestamp via
`netcortex.util.timestamps.iso_to_epoch_ms`.

`top_problems_stale_severity` is validated in `Settings.hydrate`;
unknown values log a warning and fall back to the in-memory default.

For the full data-quality contract (universal status-history schema,
adapter normalisation helpers, MCP projection rules), see
[§19 of the implementation journal](implementation-journal.md#19-operational-data-quality-the-dev17--dev20-framework).

---

## End-to-end agent flow

A typical agent diagnostic session looks like this:

```
USER: "What's wrong with the network?"

AGENT → top_problems(limit=20)
  ← returns 143 problems ranked critical → warning → info
    [
      {"problem_type": "device_down", "severity": "critical",
       "summary": "Device cpn-nashville-cat9k1 is unreachable",
       "related": {"kind": "Device", "name": "cpn-nashville-cat9k1"}},
      {"problem_type": "link_down", "severity": "critical",
       "summary": "WAN_UPLINK johnmi2-MX75 ⇄ Internet is DOWN", ...},
      ...
    ]

AGENT (to user): "I see a critical device-down on cpn-nashville-cat9k1
                 and 8 WAN uplinks down.  Let me check the cat9k1 first."

AGENT → topology_get(device="cpn-nashville-cat9k1", hops=1)
  ← {device, neighbors, interfaces, vlans, ...}

AGENT → history_get(element_name="cpn-nashville-cat9k1")
  ← {current: "unreachable", flap_state: "stable",
     history: [[1779215363027, "unreachable"]], ...}

AGENT (to user): "It's been unreachable continuously since 2026-05-13.
                 No flapping — this is a longstanding outage, not a
                 fresh failure.  Its neighbour cat9k2 is up, so the
                 path to the rest of the site is fine."
```

That entire flow is 3 MCP tool calls.  No human had to hand the
agent a Cypher query, no agent had to discover the schema by trial
and error — every relevant question maps directly to a single
single-purpose tool.

---

## Verified scale (as of 0.6.0-dev16)

Against the live development graph:

* **354 devices** inventoried
* **253 transit edges** in the Links view (133 physical · 54 WAN ·
  70 SD-WAN tunnels)
* **143 active problems** ranked by `top_problems`
* **27 MCP tools** registered (9 agentic-ops + 18 pre-existing)
* **End-to-end protocol smoke test**: JSON-RPC `initialize` →
  `notifications/initialized` → `tools/call top_problems(limit=3)`
  returns 3 of 143 real critical problems in <500 ms.

---

## Roadmap

Things deliberately not yet shipped:

* **MCP auth** — bearer token first, OAuth 2.1 later.  See the
  [auth section](mcp-tools.md#authentication--not-yet) of the MCP
  reference.
* **`stp_state` history field** — schema is ready (just add another
  entry to the correlator's `_targets` list), the connectivity strip
  will pick it up automatically.
* **Per-interface counters history** — currently only the binary
  `oper_status` is tracked; util / error rate are point-in-time.
  Adding sliding-window history for numeric fields would let
  `top_problems` flag "slowly degrading errors" before they hit the
  hard threshold.
* **Cross-element correlation** — when both an interface and the BGP
  session over it go down within seconds, `top_problems` should
  collapse them into one root-cause problem with the BGP entry as a
  consequence.  Currently they're listed separately.
