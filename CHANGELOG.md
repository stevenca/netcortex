# Changelog

All notable changes to NetCortex are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Version policy

| Bump  | When to use                                                              |
| ----- | ------------------------------------------------------------------------ |
| MAJOR | User-declared. Breaking schema/API changes or a named product milestone. |
| MINOR | A new feature (new adapter, new view, new MIB, new endpoint, etc.).      |
| PATCH | A bug fix — behavior corrected without adding or removing functionality. |

Between commits the live version carries a `-devN` suffix (e.g.
`0.4.0-dev3`) that increments with every change. At commit time the
`-devN` suffix is dropped and the appropriate MAJOR/MINOR/PATCH slot is
bumped based on what landed since the last release; the next change
then starts a fresh `-dev1` cycle on top.

The version source of truth is `netcortex/__init__.py`. `pyproject.toml`
and this file MUST be updated together whenever `__version__` changes.

---

## [Unreleased — 0.6.0-dev23]

### Feature: Network-aware topology layout (WAN-rooted tiers + orphan column)

The motivating gap: with ~300 devices and ~150 physical cables, the
default `fcose` force-directed layout produced a scattered blob in
which it was impossible to tell where the WAN was, what was
"part of the network", and what was floating because of a missing
correlation.  The operator's mental model is hierarchical (WAN →
LAN core → distribution → access → hosts) and disconnected boxes
should sit on the side as a punch-list, not be mixed in with the
live fabric.

### Added

* **"Network (WAN-rooted)" layout** — a new entry in the topology
  layout dropdown, now the default.  Implementation
  (`netcortex/status/templates/index.html`,
  `applyNetworkLayout` + `findWanAnchor` + `TOPOLOGY_EDGE_TYPES`):

    * **Anchor selection.** Pin the `type=Internet` pseudo-node on
      the left edge.  If no Internet node is in the current view
      (e.g. an overlay slice with no `WAN_UPLINK`s), fall back to
      the highest-degree node over topology-bearing edges.
    * **BFS tiering.** Walk the graph from the anchor over only
      "topology" edges — `PHYSICAL_LINK`, `WAN_UPLINK`,
      `SDWAN_TUNNEL`, `ROUTES_TO`, `ROUTING_PEER`, `FABRIC_PEER`,
      `VXLAN_TUNNEL`, `STP_ROOT`.  `LOGICAL_MEMBER` (VLAN/Prefix
      membership), `OWNS_MAC`, `ASSIGNED_IP`, etc. are excluded so
      a device's identity decorations don't make an otherwise
      unreachable device falsely look "connected to the WAN".
      BFS depth becomes the column index (tier).
    * **Within-tier ordering.** Sort by display label so reload
      positions are stable; stacked vertically and centred on
      `y=0` so the connected fabric is visually centred.
    * **Orphan column.** Any device-class node not reached by the
      BFS lands in a 4-column grid one full column-gap to the
      right of the deepest reached tier — directly answering the
      design ask of "everything not connected goes to the sides
      until we figure out why".  Pseudo-overlay node types
      (VLAN / Prefix / MACAddress / Interface / IPAddress) are
      excluded from the orphan list because they decorate their
      owning device rather than standing alone.
    * **Compound-parent friendliness.** Positions are set on the
      leaf nodes only; cytoscape auto-fits PlatformSite / Fabric /
      Domain compound bubbles around their relocated children so
      site grouping is preserved.
* `initCy` now runs the dropdown's selected layout on top of the
  FCOSE baseline (via a one-shot `layoutstop` hook) so the initial
  render matches the user's chosen layout without an extra
  bounce — and the FCOSE baseline guarantees pseudo nodes that
  the network layout doesn't position still have sensible
  coordinates.

### Notes

This is a pure client-side layout change: the graph API payload
and Neo4j schema are unchanged, no extra backend roundtrips, and
toggling between layouts is instant.  Pre-existing layouts
(`fcose`, `cose`, `breadthfirst`, `circle`, `grid`, `concentric`)
remain in the dropdown unchanged.

### Feature: Operator sync UX + MX status correctness + Meraki default interval

### Added

* **Per-adapter manual sync endpoint** — `POST /api/adapters/{adapter_type}/{instance_name}/sync`
  triggers an immediate discover→ingest run scoped to that adapter instance.
* **Adapter-table per-row sync control** — each adapter row now exposes a
  **Sync now** action that transitions to **Syncing…** while running.
* **Backend sync state surfaced to UI** — `AdapterStatus.sync_running` is now
  exported in `/api/status`, and the row button state is reconciled against it.

### Changed

* **Meraki default polling interval** changed from 15 minutes to 60 minutes
  (`sync_interval_meraki = 3600`) with docs/examples updated.
* **MX device operational state rollup** — device-level `oper_state` is now
  derived from WAN uplink states and staleness for Meraki MX/firewall nodes:
  both WANs down/disabled => `down`; stale last report (>24h) => `alerting`;
  any WAN up => `up`; otherwise `alerting`.
* **Device status projections now prefer `oper_state` over static `status`**
  in correlator history and query projections so UI/MCP consumers reflect
  operational truth for MX nodes instead of inventory-only "active" state.

---

## [0.6.0-dev22]

### Feature: Intersight port-accurate topology + observable FI identity

The motivating gap: in a UCS-fronted environment (Cisco AI Pod, Nutanix
on UCS), neither the `cpn-ful-aipod-fi-A/B` ↔ `cpn-ful-n9k1` cable nor
the `cpn-ful-ntnx-fi-1..7` ↔ FI cables were rendered in the topology,
even though LLDP on the Nexus and the Intersight inventory both knew
they existed.  Root cause was three-fold:

1. The Intersight adapter had no way to map a server's VIC port to an
   FI switch port.  Standalone-CIMC servers like Nutanix-on-UCS, whose
   `RegisteredDevice` is the server's own CIMC rather than a UCS
   Domain, fell through the existing domain-membership-only emitter
   and produced **zero** server→FI edges.
2. FI nodes carried only `mgmt_ip` for identity.  Without a chassis
   MAC or a list of alternate names, LLDP/CDP stubs from neighboring
   switches could only be merged onto the canonical FI via NetBox —
   violating the design principle that NetCortex is authoritative for
   current state and NetBox is intent-only.
3. The SNMP LLDP poller dropped any LLDP record whose advertised
   sysName was shorter than 3 characters, throwing away observed
   neighbor state that FIs frequently advertise as bare `A`/`B`.

### Added

* **Per-port server↔FI cabling from Intersight** — the Intersight
  adapter now calls `adapter/ExtEthInterfaces` (the server-side NIC
  ports) and `ether/PhysicalPorts` (the FI-side switch ports), then
  emits one `PHYSICAL_LINK` edge per resolved
  `AcknowledgedPeerInterface`, with port-accurate `interface_a`
  (`vic<slot>/<port>`) and `interface_b` (`Ethernet<slot>/<port>`)
  and the server NIC `mac_address` for downstream MAC correlation.
  The chain (`adapter.ExtEthInterface → AcknowledgedPeerInterface →
  ether.PhysicalPort`) works uniformly for X-Series blades (via the
  chassis IOM) AND standalone-CIMC C-series direct-attach servers
  (e.g. Nutanix-on-UCS), because the resolution doesn't depend on
  UCS Domain membership.

  Resolving an `ether/PhysicalPort` back to its owning FI required
  the keyed tuple `(RegisteredDevice.Moid, SwitchId)`: Intersight
  does *not* expose a direct `NetworkElement` MoRef on PhysicalPort,
  but every FI in a UCS domain shares the same `RegisteredDevice`
  and is uniquely identified within that domain by its
  `SwitchId` (`"A"`/`"B"`).  The parser falls back to the FI serial
  embedded in `PhysicalPort.Dn` (e.g.
  `switch-FDO28380QLJ/slot-1/switch-ether/port-11`) for stragglers.

  Resolving an `adapter.ExtEthInterface` back to its owning server
  similarly required a fallback: tenants where
  `adapter.Unit.ComputeNode` is unpopulated (common for standalone
  CIMC) get a `RegisteredDevice.Moid → server.platform_id`
  translation map built from `NormalizedDevice.platform_metadata.device_moid`.
  Server-side port names that aren't exposed as top-level
  `SlotId`/`PortId` are parsed out of the Redfish-style
  `Dn` (`.../NetworkPorts/Port-3` → `vic1/3`).

  When the planner finds no acknowledged peer interface for a
  server, the previous generic dual-FI fallback still fires for
  UCSM-/IMM-managed gear on firmware that doesn't publish peer
  interfaces.
* **Observable FI identity for stub merging** — every
  `intersight-fi:*` Device node now publishes:
  - `candidate_ips`  (Management + OOB IPv4)
  - `candidate_names`  (`SwitchId`, `Name`, `Dn`)
  - `OWNS_MAC → MACAddress(OutOfBandMac)` when the FI exposes its
    out-of-band MAC.
  These let the existing `_merge_neighbor_stubs_by_chassis_mac`,
  `_merge_neighbor_stubs_by_mgmt_ip`, and `_merge_neighbor_stubs_by_name`
  passes resolve LLDP/CDP stubs from neighboring switches onto the
  canonical FI directly — no NetBox round-trip required.  Worst-case
  (e.g. an FI firmware that omits `OutOfBandMac`), the mgmt-IP and
  name paths still merge correctly.
* **Name-merge consults `candidate_names`** — the stub-by-name merge
  in `correlate.py` now matches the stub's short hostname against any
  string in `real.candidate_names`, so a stub like
  `lldp-neighbor:cpn-ful-aipod-fi-A` merges onto an Intersight FI
  whose primary `name` is `FI-A-FCH2903782Y` but whose
  `candidate_names` includes the operator-facing hostname.
* **`netbox_delta` marker** — when the canonical (observed) Device
  disagrees with the matching NetBox device record, the enricher
  stamps a small JSON delta on the node (e.g.
  `{"name": {"intent": "cpn-ful-aipod-fi-A", "current": "FI-A-FCH2903782Y"}}`)
  instead of overriding either side.  Devices NetBox doesn't know
  about are stamped with `{"status": "absent_in_netbox"}`.  This
  formalises "NetCortex is authoritative for current state, NetBox
  is intent" — the reconciliation UI / tooling will read this field
  to surface drift to the operator.
* **`intersight` is now a high-confidence discovery protocol** —
  added to `_PROTO_PRIORITY` (rank 80) and to every MAC/ARP
  "already-linked" guard in `correlate.py`, so MAC inference no
  longer manufactures parallel edges over an authoritative
  Intersight-emitted cable.

### Changed

* **LLDP/CDP short-name records are kept when chassis MAC or mgmt IP
  is present** — previously rejected outright.  Stubs created from
  these records are given collision-safe IDs (`lldp-neighbor:by-id:<mac-or-ip>`)
  so two FIs both advertising `A`/`B` can never collide on the same
  stub.  All existing chassis-MAC / mgmt-IP / `candidate_names`
  merge paths apply unchanged.

### Feature: Data-quality fixes from the dev19 Meraki cross-verification report

Dev19 added the staleness policy that filtered abandoned-inventory
noise out of `top_problems`.  Re-running the cross-verification then
surfaced six concrete data-quality gaps where the graph either
undersold what Meraki / Catalyst Center already had, or lost
information between the adapter and the MCP-tool projection.  This
release fixes all six in one drop so the agent surface (`links_list`,
`top_problems`, `get_inventory`) matches the source-of-truth.

### Added

* **`SDWAN_TUNNEL.oper_status` from Meraki AutoVPN reachability** —
  the Meraki adapter now maps each peer's `reachability`
  (`reachable`/`unreachable`) onto the canonical `oper_status` field
  (`up`/`down`).  This wires SD-WAN tunnels into the existing history
  correlator (transition + flap tracking) and into the `top_problems`
  `link_down` check, so SD-WAN-only outages now surface alongside
  physical and WAN_UPLINK outages.  The dev19 staleness policy
  applies unchanged via the A-side MX's `meraki_last_reported_at`, so
  tunnels rooted on dormant MXs are demoted/filtered automatically.
  `"unknown"` peers intentionally leave `oper_status` unset so we
  never record bogus transitions for tunnels the dashboard has no
  opinion on.
* **`Prefix.kind` discriminator** — the Meraki adapter now stamps a
  small operator-facing taxonomy onto every Prefix:
  `vlan_subnet` for `vlan`/`vlan6`/`svi`/`svi6` scopes, `static_route`
  for `static`.  Future scopes (`transit`, `wan`) slot in without
  schema changes.  Unknown scopes leave `kind` unset rather than
  inventing a false label; the raw `scope` is preserved for forensics.
* **Catalyst Center per-switch MAC-address-table walk** — section 5
  of the CATC discover pipeline already creates
  `Interface→LEARNED_MAC→MACAddress` edges when `/v1/host` returns
  `connectedNetworkDeviceId` + `connectedInterfaceName`.  On
  deployments where the assurance pipeline is empty or the endpoint
  is RBAC-restricted, that section produces zero edges.  A new
  section 5b now falls back to walking
  `/network-device/{deviceId}/mac-address-table` per switch, which
  the API exposes regardless of assurance state.  Best-effort:
  per-switch failures degrade to log.debug, schema variations
  (`interfaceNumber` vs `ifName`/`portName`/`interface`) are
  handled.

### Changed

* **WAN_UPLINK per-slot visibility through the MCP surface** —
  `_infer_wan_topology` has always created two `WAN_UPLINK` edges per
  dual-WAN MX (one per slot), distinguished only by the `wan_slot`
  property.  `links_list` previously dropped `wan_slot` from the slim
  projection, making both edges look identical to an agent.  The
  query now folds `r.wan_slot` into `iface_a` via COALESCE (so
  WAN_UPLINKs render as `wan1`/`wan2` in the iface column), and the
  MCP slim view exposes `wan_slot`, `via` (`mx_uplink`/`ebgp`), and
  `source_adapter` as first-class fields.
* **`links_list` exposes `source_adapter`** — agents can now tell
  adapter-discovered cables (`meraki`, `catalyst_center`, `snmp`)
  apart from correlator-built edges (WAN uplinks to Internet, AS
  boundary peers) without a second graph round-trip.
* **Meraki device name canonicalisation** — dashboard names with
  trailing/leading whitespace or internal whitespace runs
  (e.g. `"Home MX "`) are now trimmed and collapsed at ingest via
  `_norm_device_name`.  Cross-system joins (NetBox lookups,
  `top_problems` grouping, history keys) stop silently missing
  matches.  Empty/whitespace-only input falls back to the serial as
  before.

### Internal

* **Three new pure helpers in `netcortex/adapters/meraki.py`** —
  `_reachability_to_oper_status`, `_scope_to_prefix_kind`,
  `_norm_device_name`.  Each owns one decision boundary so the rest
  of the adapter remains a thin API wrapper.  Unit-tested in
  `tests/adapters/test_meraki_helpers.py` with 24 parametrised
  cases covering happy path, case-insensitivity, whitespace, and
  unknown-input behaviour.
* `import asyncio` added to `catalyst_center.py` for the MAC-table
  semaphore-bounded concurrent walk.

### Migration / operational notes

* No schema migration required.  All changes are additive
  (`Prefix.kind`, `SDWAN_TUNNEL.oper_status`) or projection-only
  (`links_list` slim shape).
* Existing SDWAN_TUNNEL edges without `oper_status` will be picked
  up on the next Meraki sync cycle; the history correlator will
  seed `oper_status_history` on first observation.
* No new config keys; the dev19 staleness policy
  (`top_problems_stale_after_seconds`,
  `top_problems_stale_severity`) applies to the newly-surfaced
  SDWAN_TUNNEL `link_down` problems unchanged.

### Post-audit hardening / correctness fixes

* **Ingest edge hash-skip completed** — `ingest_graph_data` now looks up
  existing edge `_content_hash` values and skips unchanged edges, mirroring
  the node path. Unchanged edges are "touch-only" (`last_seen`) instead of a
  full MERGE rewrite.
* **Correlation freshness pass no longer rewrites the entire graph** —
  `_stamp_freshness` now targets only rows that need initialization
  (`first_seen`/`last_seen` missing) plus correlator-owned rows, instead of
  `MATCH (n) SET n.last_seen` / `MATCH ()-[r]->() SET r.last_seen` on every
  cycle.
* **Worker correlation gate tightened** — correlation now waits for at least
  one successful discovery from every configured adapter instance before
  running, preventing partial-round correlation on incomplete graph snapshots.
* **`history_get` correctness fixes**:
  * validates and actually applies the caller's `field` parameter (via dynamic
    property lookup with an allow-list),
  * supports robust link-pair parsing with explicit delimiters (`<->`, `⇄`,
    `|`, `--`) instead of naive `split('-')` behavior.
* **`peers_list` query is now server-filtered and bounded** — filters and
  sorting moved into Cypher with explicit `LIMIT`, plus a matching count query
  for truncation metadata.
* **`top_problems` now uses dedicated slim graph queries** — inventory/link
  checks now fetch only the fields used by the rules engine
  (`get_top_problems_inventory`, `get_top_problems_links`) instead of
  materializing the full inventory/links payloads.
* **`mac_lookup` is now targeted** — single-MAC lookup is done by a dedicated
  query (`get_mac_lookup`) rather than loading the full correlated CAM table
  and filtering in Python.
* **TLS verification for NetBox enrichment is secure-by-default**:
  * `enrich_devices_from_netbox` and `enrich_prefixes_from_netbox_ipam` now
    default to `verify_ssl=True`,
  * `Settings` adds `netbox_verify_ssl` (default true, env/secret override),
  * worker passes this flag explicitly to enrichment loops, so self-signed
    lab deployments can still set `netbox_verify_ssl=false`.
* **MCP docs/instructions now accurately describe tool maturity** — production
  guidance now clearly prioritizes `agentic_ops` tools and marks placeholder
  catalog sections as not yet implemented.
* **Stability fix for ingest deadlocks** — worker now serializes direct Neo4j
  ingest writes behind an async lock and retries transient
  `DeadlockDetected` failures with short backoff, preventing partial-cycle
  data drops that could skew MAC/ARP and routing views.
* **Routing table correctness fix** — routing peer queries now treat
  `ROUTING_PEER` as direction-agnostic (matching the canonical undirected
  ingest model) so peers are not silently hidden when edge direction flips.

### Added

* **VLAN table view** — new `VLANs` tab in the status UI with a dedicated
  `/api/graph/vlans` backend endpoint. The table supports site/device chip
  filtering plus free-text filtering and sorting, aligned with the Inventory,
  MAC/ARP, Routing, and Links views.

---

## [0.6.0-dev19]

### Feature: `top_problems` staleness policy (suppress abandoned-inventory noise)

After dev17/dev18 fixed the *artificial* clustering of WAN_UPLINK
`oper_status_changed_at` timestamps, cross-validation against Meraki
Dashboard showed that the remaining `critical` `link_down` entries
in `top_problems` were *accurate* but mostly not actionable: ~17 of
19 reported MX uplinks were on devices Meraki itself last heard
from months ago (one as far back as September 2024).  These are
claimed-but-never-deployed appliances and spare inventory — the
"down" signal is real Dashboard state, but it's not an incident.

That noise drowns out the small number of genuine outages, so an
operator skimming `top_problems` can't tell at a glance which alerts
need attention.  This release introduces a source-agnostic
**staleness policy** that demotes or filters such problems, plus the
config knobs to control it without code changes.

### Added

* **`netcortex/util/timestamps.py::iso_to_epoch_ms()`** — single
  parser for ISO-8601 strings (including Meraki's Z-suffixed shape)
  into epoch-ms so adapters stop re-implementing
  `datetime.fromisoformat` plumbing at each call site.
* **Meraki adapter** now captures `lastReportedAt` from the
  `getOrganizationApplianceUplinkStatuses` response and stamps it
  onto the corresponding MX `Device` node as
  `meraki_last_reported_at` (epoch ms) and
  `meraki_last_reported_at_iso` (raw string for human inspection).
  The data was already in the API response; we were simply
  throwing it away.
* **`get_inventory`** now surfaces both new properties on every row.
* **`top_problems`** now consults the staleness policy for
  `device_down` and `link_down` problems.  Demoted problems carry
  `evidence.stale = true` and `evidence.last_reported_at_ms` so
  agents/UIs can render them differently from real incidents, and
  the summary string is suffixed with `"(stale source data)"`.

### New config keys (see `netcortex.config.Settings`)

Both keys are optional and live in the `netcortex/core` secret:

| key | default | description |
| --- | --- | --- |
| `top_problems_stale_after_seconds` | `86400` (24 h) | A device's source-of-truth (today: Meraki `lastReportedAt`) must have refreshed within this window for the device to count as "live".  Older = "stale". |
| `top_problems_stale_severity`     | `"info"`       | Severity to assign to stale problems.  Allowed values: `"critical"`, `"warning"`, `"info"`, `"filter"`.  The literal `"filter"` drops the problem from the output entirely. |

Recommended tunings:

* **Default (`"info"`)** — keep stale problems visible (so abandoned
  inventory is still discoverable) but rank them below real
  incidents.  Best for operators who want to clean up inventory
  on a long horizon without losing the breadcrumbs.
* **`"filter"`** — drop them entirely.  Best for "I only want
  actionable alerts in `top_problems` and I track decommissioned
  inventory elsewhere."
* **`"warning"`** — useful in environments where stale ≈ "really
  should look at this, but not as an outage".  Rare.

The default of 24 h is conservative: any genuine outage (where the
device WAS reporting and stopped) will retain its `critical` rank
for the first 24 h, which is well past the SLA window for human
response.  Lower it (e.g. 3600) for environments where MX uplinks
are expected to check in every few minutes.

### Why source-agnostic

The policy itself is implemented in
`netcortex.mcp.tools.agentic_ops._apply_staleness_policy` as a
pure function over `(severity, last_reported_at_ms, now_ms,
threshold_seconds, stale_severity)`.  It does not know or care
about Meraki specifically — when other cloud-managed adapters
(Catalyst Center, Intersight, etc.) grow equivalent
"last-heard-from" semantics, surfacing them as
`<adapter>_last_reported_at` on the Device node is enough to
extend the policy.

### Limitations

* Today only the Meraki adapter populates
  `meraki_last_reported_at` (and only for MX appliances, since
  that's where the noise was — `getOrganizationApplianceUplinkStatuses`
  doesn't cover switches or APs).  Other devices fall through the
  policy with no demote, no filter — exactly the prior behaviour.
* The policy is one-sided: it can only demote/filter, never
  escalate.  An aged-and-actually-down device cannot be promoted
  by this knob; it would already be `critical` if its uplink
  status was `down`.
* `link_down` resolution looks up the source device via `a_name`
  (which for `WAN_UPLINK` edges is always the MX).  For
  `PHYSICAL_LINK` / `SDWAN_TUNNEL` / `VXLAN_TUNNEL` the same
  side-A heuristic applies; in practice those edge types don't
  involve Meraki-cloud-managed devices today, so the policy is a
  no-op for them.

---

## [Unreleased — 0.6.0-dev18]

### Fix: WAN_UPLINK snapshot/restore now preserves `oper_status` itself

After deploying dev17 the symptom in `top_problems` persisted — the
30-ms cluster of WAN_UPLINK `oper_status_changed_at` timestamps was
still re-stamping itself forward on every correlation cycle, even
though `apply_transition` no longer touched it.

Root cause turned out to be deeper than the history-feature seed:
`_infer_wan_topology` **deletes and re-MERGEs every correlator-owned
WAN_UPLINK edge on every cycle**.  A snapshot/restore block was
already in place to preserve `oper_status_history`, the flap
scalars, `oper_status_changed_at`, and `first_seen` across the
destructive rebuild — but it was missing the one field that
mattered most: `oper_status` itself.

So on every cycle, the freshly recreated edge had `oper_status =
NULL` until `_enrich_wan_uplinks_with_health` ran a moment later,
saw `prev_oper IS NULL` and `$oper = 'down'`, treated the difference
as a "transition", and re-stamped `oper_status_changed_at = now()`.
Every WAN uplink in the graph re-stamped within the few milliseconds
the enrichment query took to process them — exactly the cluster
pattern we observed.

**Fix:** the snapshot now also captures `r.oper_status`, and the
restore block writes it back with `coalesce(r.oper_status,
$oper_status)`.  The enrichment query then sees a meaningful
`prev_oper` and only stamps `_changed_at` when the new live value
actually differs from the prior cycle's value — which is the entire
point of having a `_changed_at` in the first place.

This also explains why the dev17 fix alone wasn't sufficient: the
history-feature seed branch was a second writer of `_changed_at`,
but the enrichment writer (which fires every cycle on every uplink,
not just on rollouts) was producing the majority of the bad data.
Both fixes are needed — dev17 to stop the seed from inventing
transitions on a quiet graph, and dev18 to stop the delete-rebuild
cycle from manufacturing a fake transition on every pass.

### Changed
* `_infer_wan_topology` snapshot/restore now includes `oper_status`
  and uses `coalesce` on every restored field so partially-populated
  snapshots (e.g. an edge with `oper_status` but no history yet) are
  handled correctly.
* Snapshot `WHERE` clause widened from `r.oper_status_history IS NOT
  NULL` to `(r.oper_status_history IS NOT NULL OR r.oper_status IS
  NOT NULL)` so edges that have a live oper status but haven't yet
  been seeded with history are also preserved across the rebuild.

### Migration note
The cleanup Cypher snippets documented in dev17 still apply if you
deployed dev15/dev16 and accumulated bad `_changed_at` timestamps.
Once dev18 is running, the timestamps for stable-down edges will
stop drifting forward on every cycle — but they will still reflect
whatever value the last bad cycle wrote, until you run the cleanup
or until a real transition occurs on each edge.

---

## [Unreleased — 0.6.0-dev17]

### Fix: status-history seed no longer fakes a transition timestamp

The status-history feature introduced in dev15/dev16 had a regression
in the "seed-on-first-observation" branch of
`netcortex.graph.history.apply_transition`: it stamped
`<field>_changed_at = now_ms` whenever it seeded a new history,
including the very first correlator pass after the feature rolled
out onto a graph full of pre-existing links.

The result was a misleading cluster in `top_problems`: every
long-standing-down WAN uplink reported as "just went down at
<rollout time>" within a ~30 ms window, indistinguishable in the
output from a real correlated outage.

**Fix:** the seed branch now writes the history JSON (so the UI
connectivity strip has something to draw) but does NOT stamp
`<field>_changed_at`. `_stamp_freshness` instead backfills
`oper_status_changed_at = first_seen` for every edge that has an
operational status but no `_changed_at` yet — the same pattern
already used for `Device.status_changed_at`. This gives the UI the
honest answer: "first time we observed this edge in its current
state", not "the instant our correlator first booted with this
feature enabled".

Real correlated outages are still fully detected: they go through
the `apply_transition` real-transition branch (which requires
non-empty prior history) and continue to stamp `_changed_at`
accurately.

### Migration: clean up existing bad data

The fix prevents new bad timestamps but does NOT scrub timestamps
that were already written incorrectly. After upgrading, run this
one-shot Cypher against the live graph to reset every edge whose
`oper_status_changed_at` came from the buggy seed branch. The
discriminator is unambiguous: the seed wrote exactly one entry into
`oper_status_history` whose timestamp equals `oper_status_changed_at`.

```cypher
MATCH ()-[r]->()
WHERE type(r) IN ['PHYSICAL_LINK', 'WAN_UPLINK',
                  'SDWAN_TUNNEL', 'ROUTING_PEER']
  AND r.oper_status_changed_at IS NOT NULL
  AND r.oper_status_history IS NOT NULL
  AND r.first_seen IS NOT NULL
WITH r,
     [x IN apoc.convert.fromJsonList(r.oper_status_history)
      WHERE x[0] = r.oper_status_changed_at] AS matches,
     apoc.convert.fromJsonList(r.oper_status_history) AS hist
WHERE size(hist) = 1 AND size(matches) = 1
SET r.oper_status_changed_at = r.first_seen
RETURN type(r) AS edge_type, count(r) AS reset
```

If APOC is not available, the same discriminator can be expressed
purely as a string check: a single-entry history JSON looks like
`[[ts,"val"]]` and contains no `],[` separator, while a multi-entry
history does. This lets us safely clear ONLY the seed-stamped edges
and let the next correlator cycle re-seed via the fixed code path
(which writes correct `_changed_at` values via `_stamp_freshness`):

```cypher
MATCH ()-[r]->()
WHERE type(r) IN ['PHYSICAL_LINK', 'WAN_UPLINK',
                  'SDWAN_TUNNEL', 'ROUTING_PEER']
  AND r.oper_status_history IS NOT NULL
  AND r.oper_status_changed_at IS NOT NULL
  AND NOT r.oper_status_history CONTAINS '],['
REMOVE r.oper_status_changed_at, r.oper_status_history
RETURN type(r) AS edge_type, count(r) AS reset
```

Note: the cleanup is optional — `_stamp_freshness` will handle any
edges where `oper_status_changed_at IS NULL` automatically going
forward. Only edges that were already mis-stamped need this
explicit cleanup.

### Added
* `apply_transition` distinguishes "seeded" from "transitioned"
  internally and only stamps `<field>_changed_at` on the latter.
* `_stamp_freshness` now backfills `oper_status_changed_at =
  first_seen` for `PHYSICAL_LINK`, `WAN_UPLINK`, `SDWAN_TUNNEL`,
  and `ROUTING_PEER` edges. New stat field
  `edges_oper_status_backfilled` on the correlation summary.
* New tests in `tests/graph/test_history.py`:
  `test_apply_transition_seed_on_rollout_does_not_stamp_changed_at`
  (regression test for the exact failure mode) and
  `test_apply_transition_seed_then_real_transition_stamps_changed_at`
  (verifies the end-to-end sequence behaves correctly).

### Changed
* `tests/graph/test_history.py::test_apply_transition_seeds_first_observation`
  updated to assert that `oper_status_changed_at` is NOT in the seed
  updates dict.

---

## [Unreleased — 0.6.0-dev16]

### MCP HTTP transport mounted on the container

The 27 MCP tools (including the 9 agentic-ops tools shipped in
dev15) are now actually reachable from MCP clients.  The FastMCP
``streamable-http`` ASGI app is mounted on the existing FastAPI
listener at ``/mcp/``, so the same container that serves the
status UI now also serves the MCP protocol — no separate stdio
sidecar, no second port, no extra process.

Verified end-to-end: a JSON-RPC ``initialize`` POST to
``http://localhost:8000/mcp/`` returns a proper
``protocolVersion: 2025-06-18`` handshake with all 27 tools
advertised.

Disabled by default in env-var-disabled environments via
``NETCORTEX_MCP_ENABLED=0``; path configurable via
``NETCORTEX_MCP_PATH`` (default ``/mcp``).

### Added
* MCP status fields on ``AppState``: ``mcp_status`` (``enabled`` /
  ``disabled`` / ``error``), ``mcp_path``, ``mcp_transport``,
  ``mcp_tool_count``, ``mcp_message``.
* ``mcp`` block on ``GET /api/status`` so the UI and external
  monitors can verify the transport without curling the
  streamable-http path directly.
* New "MCP" pill in the header (next to NetBox / Neo4j / Redis /
  Secrets / Graph).  Shows tool count, badge colour matches
  transport health.  Hover tooltip includes the live endpoint
  path AND a ready-to-paste Cursor ``~/.cursor/mcp.json`` snippet.
* Lifespan-merge in ``netcortex.main`` — the mounted MCP app's
  session-manager lifespan is now nested into the FastAPI
  lifespan so the streamable-http transport starts/stops cleanly.
* CORS preflight verbs extended (``OPTIONS``, ``DELETE``) — MCP
  clients use DELETE to terminate streamable-http sessions.

### Fixed
* The dev15 changelog falsely claimed an MCP server was running.
  In reality only the in-process tool registry existed — no
  transport, no listener, no client could reach it.  This release
  closes that gap.

---

## [Unreleased — 0.6.0-dev15]

### Agentic-ops MCP tools (Phase D of agentic-ops plan)

NetCortex now exposes a dedicated, single-purpose MCP tool surface
for LLMs and agentic workflows.  All tools live in the new
`netcortex/mcp/tools/agentic_ops.py` module and are registered via
`netcortex/mcp/server.py` alongside the existing device/access/
topology/documents/sync tools.

Design follows the workspace MCP-security rule: every tool is
single-purpose, bounded (default 50 rows, hard cap 500), with
stable field names that mirror the REST API surface.  No
business logic in the MCP layer — every tool delegates to an
existing `netcortex.graph.query` function (the thin-MCP
principle the workspace rule mandates).

#### The nine tools

| Tool             | One-line diagnostic question                                  |
|------------------|----------------------------------------------------------------|
| `inventory_list` | What devices exist and what's their state?                    |
| `topology_get`   | How is device X connected? (neighbours, interfaces, VLANs)    |
| `links_list`     | Which cables / WAN / SD-WAN tunnels are flapping/down/busy?   |
| `peers_list`     | Which routing adjacencies (BGP/OSPF/...) are down or unstable?|
| `paths_find`     | Show me the shortest path between A and B                     |
| `history_get`    | Fetch the 7-day transition history for an element             |
| `mac_lookup`     | Where is this MAC learned?                                    |
| `ip_lookup`      | Where does this IP / prefix live?                             |
| `top_problems`   | Run all health checks and rank the issues                     |

#### `top_problems` — the hero tool

The single tool an agent should call first when asked "what's
wrong with the network?".  Runs a battery of checks across the
device inventory, every transit edge, and every routing peer,
and returns a ranked problem list.  Each problem has a stable
`problem_type` (e.g. `device_down`, `link_flapping`,
`snmp_restricted`, `high_utilisation`) so an agent can group /
filter consistently across calls.

Severity ladder:

* **critical** — service-affecting now: device down, link down,
  peer down, currently flapping (high transition rate).
* **warning**  — recent instability or capacity pressure: unstable
  flap state, high utilisation, elevated error rate, SNMP partly
  restricted.
* **info**     — observability gaps: unpolled or missing-MIB devices.

Each problem carries `summary`, `evidence`, `suggested_action`,
and a `related` reference so the agent can quote-back to the
operator and drill in with `history_get` / `topology_get`
without further lookup work.

#### Top-20 network problems → tool mapping

Per the user's "agentic ops" requirement, the tools were
designed so the most common operator-grade problems can each be
diagnosed with a single tool call or a short sequence:

| # | Problem                          | Tool(s)                             |
|---|----------------------------------|-------------------------------------|
| 1 | Link flapping                    | `top_problems` → `history_get`      |
| 2 | BGP/OSPF peer down/flapping      | `peers_list`, `top_problems`        |
| 3 | High link utilisation            | `links_list(min_util=80)`           |
| 4 | High link errors                 | `links_list(min_error_rate=1)`      |
| 5 | Device unreachable               | `top_problems` → `topology_get`     |
| 6 | SNMP coverage gap                | `inventory_list(snmp_health=...)`   |
| 7 | Wi-Fi outage at a site           | `inventory_list(site, role=ap)`     |
| 8 | WAN circuit down                 | `links_list(edge_type=WAN_UPLINK)`  |
| 9 | SD-WAN tunnel down               | `links_list(edge_type=SDWAN_TUNNEL)`|
| 10| STP topology change              | `history_get(field=stp_state)`*     |
| 11| VLAN inconsistency / orphans     | `topology_get` (via `vlans`)        |
| 12| Path MTU / blackhole             | `paths_find` → `links_list`         |
| 13| Asymmetric routing               | `paths_find` (twice, swap src/dst)  |
| 14| Duplicate MAC / IP               | `mac_lookup`, `ip_lookup`           |
| 15| Default-gateway not reachable    | `ip_lookup`, `peers_list`           |
| 16| LACP unbalanced / single-sided   | `links_list` (single_sided field)   |
| 17| Power / hardware alarm           | `inventory_list(status=alerting)`   |
| 18| Recently-changed circuit         | sort `links_list` by `_changed_at`  |
| 19| Cable mis-cabled (wrong neighbor)| `topology_get` (diff vs intent)     |
| 20| New device appeared unexpectedly | `inventory_list` filter by adapter  |

(*) `stp_state` history follows the same schema as `oper_status`
but isn't wired into the Phase-A correlator yet — slot reserved
for a future revision.

### Added
* `netcortex/mcp/tools/agentic_ops.py` — the 9 new MCP tools,
  ~700 lines, fully bounded and self-documenting.
* `agentic_ops` import added to `netcortex/mcp/server.py`.

### Fixed
* `netcortex/mcp/server.py` no longer passes the removed
  `description=` kwarg to `FastMCP()` (it was deleted in
  fastmcp 3.x).  Migrated to the supported `instructions=`
  kwarg and added an agent-orientation paragraph so an LLM
  picks the right tool first time.  This was a latent bug
  that prevented the whole MCP server from starting on
  fastmcp 3.x.

---

## [Unreleased — 0.6.0-dev14]

### Links table view (Phase C of agentic-ops plan)

A new top-level "Links" tab puts every transit edge in the
network on one filterable, sortable, chip-scopable page —
with the Phase-B 24h connectivity strip rendered inline per
row.  Operationally this is the "where do I look first?" view
for an on-call engineer: flapping cables are guaranteed to be
the very first row even on a 1000-link fleet.

#### Coverage

* `PHYSICAL_LINK`     — cables (LLDP/CDP/MAC-derived)
* `WAN_UPLINK`        — MX uplinks + eBGP external transit
* `SDWAN_TUNNEL`      — vendor SD-WAN overlays
* `VXLAN_TUNNEL`      — VXLAN/EVPN tunnels (when present)

Deliberately omits `ROUTING_PEER` (control-plane adjacency,
not a data path — that's the Routing view's job) and
`LOGICAL_MEMBER` (semantic membership, not transit).

#### Columns (the headline operator UI)

| Column        | Why it's there                                    |
|---------------|---------------------------------------------------|
| Type          | Compact pill (Cable / WAN / SD-WAN / VXLAN)       |
| A side        | `device · interface`                              |
| Z side        | `device · interface`                              |
| State         | up / down pill + flap badge (⚡ FLAPPING / ⚠)     |
| Connectivity · 24h | Inline SVG strip (same component as tooltips) |
| Stability     | Flap state + per-hour transition rate             |
| Health        | Color-coded numeric (0 = best, 100 = critical)    |
| Util %        | Per-link utilisation                              |
| Err/s         | Error rate; amber when > 0                        |
| Since         | `oper_status_changed_at` as relative time         |
| Site(s)       | A↔Z, collapses when same                          |
| L3 / Carries  | Routed prefix list when present                   |
| Method        | discovery_proto / via / source                    |

#### Filters (all combinable)

* Chip filter — sites and/or devices, with autocomplete
  (the same component as Inventory / MAC / STP / Routing).
  Matches if EITHER side of a link is in scope, so a chip
  for one device shows every link that touches it.
* Type select — restrict to one edge type.
* Status select — restrict to up or down.
* Flapping-only checkbox — show only links the correlator
  flagged as `flapping` or `unstable`.
* Free-text search — device names, interfaces, sites, L3
  prefixes, peer IPs, public IPs.

#### Default sort

Server pre-sorts by `flap_score_1h DESC, oper_status_changed_at
DESC, health_score DESC` so the most operationally urgent rows
(currently flapping → recently changed → unhealthy) surface
without an operator click.  Column-header clicks switch to
client-side sort on that column.

#### Footer count

Live `<rendered>/of/total links · N down · M flapping` so the
operator can see at-a-glance how much of the fleet is in a
bad state without scanning the table.

### Added
* `get_links()` in `netcortex/graph/query.py` — single Cypher
  pass per edge type, projecting every field the table renders
  (health, flap stats, history JSON, L3 prefixes, sites).
  Capped at 5000 rows; type counts returned alongside.
* `GET /api/links` endpoint in `netcortex/main.py` with the
  same 10s query budget as the other table endpoints.
* `view-links` pane in `netcortex/status/templates/index.html`,
  the `loadLinks` / `filterLinks` / `sortLinks` / `renderLinks`
  JS functions, and the `_linksChipFilter` wiring.

### Changed
* `ALL_VIEWS` and `switchView()` lazy-loader extended to
  include the new view.
* Navigation tab bar grows a "Links" button between Routing
  and Explorer.

---

## [Unreleased — 0.6.0-dev13]

### Connectivity-strip UI (Phase B of agentic-ops plan)

The history captured by Phase A now lights up in the UI as the
operator's reference green/red/amber timeline strip — across
hover tooltips on every Device, `PHYSICAL_LINK`, `WAN_UPLINK`,
`SDWAN_TUNNEL`, and `ROUTING_PEER`, plus a wider 7-day version in
the right-side detail panel.

#### Reusable component — `createConnectivityStrip(historyJson, opts)`

Pure SVG, no dependencies, returns an HTML string suitable for
`innerHTML`.  Used by both the tooltip (compact 24h) and the
detail panel (wide 7d w/ axis labels).  Each segment is a
`<rect>` with a `<title>` child so the operator gets exact
"(state) for 3h 12m, started 14:22:01" on hover.

State→color map intentionally covers every status vocabulary we
track (oper_status, state, status) so the helper works for any
field without per-call configuration:

* `up / active / established / online / reachable / full` → green
* `down / failed / idle / offline / unreachable / disabled` → red
* `alerting / warning / degraded / dormant / partial /
   cloud_only / restricted / unknown / 2way / connect /
   opensent / openconfirm` → amber
* anything else → neutral grey

Pre-window state: the helper looks at the most-recent transition
*strictly before* the window start so the leading segment is
correctly coloured even when nothing has changed in the window.
This makes single-entry histories (the cold-start seed) render
as a single coloured bar — "this thing has been stable for >X
days" — rather than as blank/unknown.

#### Surface integration

**Hover tooltips (cytoscape `mouseover`):**

* Edge tooltips — when an edge has `oper_status_history`, the
  220×4px compact strip appears right under the status pill.
  Flap-state badge (`⚡ FLAPPING` red, `⚠ unstable` amber)
  surfaces alongside the status when applicable.
* Device tooltips — same treatment using `status_history`, with
  the matching flap badge.

**Detail panel (`showDetail`):**

* Raw `*_history` / `*_flap_count_*` / `*_flap_score_*` /
  `*_flap_state` fields are hidden from the generic property
  dump (a 200-char JSON blob and four cryptic numeric rows
  weren't actionable).
* A new "Connectivity history · 7 days" section renders one
  280×8px strip per tracked field on the element, with a
  per-field subtitle showing current value + "for X" + flap
  badge + "N transitions / 24h".  Probes by suffix
  (`*_history`) so any new tracked field added in the
  correlator surfaces here automatically.

### Added
* `createConnectivityStrip(historyJson, opts)` in
  `netcortex/status/templates/index.html` — the reusable SVG
  strip component.  Supports `windowMs`, `width`, `height`,
  `ticks`, `compact`, `showLabel`.
* `_stripStateColor(state)` + `_stripFmtAxis(ms, windowMs)`
  helpers next to the existing `_fmtAgo` / `_fmtAbs` family.
* Flap-state badges in the edge and device hover tooltips so
  flapping objects are visible without opening the detail panel.

### Changed
* `showDetail()` now hides history / flap scalar fields from
  the generic property dump and renders them in a dedicated
  Connectivity-history section at the bottom of the panel.
* Edge and device `mouseover` handlers include a compact 24h
  connectivity strip under the status pill when history is
  available.

---

## [Unreleased — 0.6.0-dev12]

### Status-transition history + flap detection (Phase A of agentic-ops plan)

This is foundation for two things the operator and the agents need:
"is this thing flapping right now?" and "show me the 7-day
connectivity timeline strip for this link/device/peer".

Every Device / `PHYSICAL_LINK` / `WAN_UPLINK` / `SDWAN_TUNNEL` /
`ROUTING_PEER` now records each operational-state transition in a
small, bounded history on the graph element itself.  No new
storage backend — we stay inside the "NetBox + Neo4j only" rule.

#### Schema (per tracked field on each element)

```
<field>                       current value (existing)
<field>_changed_at            epoch ms of last change (existing)
<field>_history               JSON '[[at, to], [at, to], ...]'
                              sorted ascending, capped to 7 days
                              and to 200 entries (runaway-flap guard)
<field>_flap_count_1h         int — transitions in the last hour
<field>_flap_count_24h        int — transitions in the last 24 hours
<field>_flap_score_1h         float in [0,1]; flap_count_1h / 6, saturating
<field>_flap_state            "stable" | "unstable" | "flapping"
```

Tuple-of-pairs is intentional — saves ~30 % bytes vs an array of
maps, and the "from" at index `i` is implicit (it's the `to` of
index `i-1`).  Stored as a JSON string for portability (any
language can parse, no Neo4j packed-bytes quirks).

#### Tracked fields (intentionally a short list to start)

* `Device.status` (up / alerting / down / etc.)
* `PHYSICAL_LINK.oper_status`
* `WAN_UPLINK.oper_status`
* `SDWAN_TUNNEL.oper_status`
* `ROUTING_PEER.oper_status`

Adding a new target is one extra row in the `targets` table in
`_update_status_history()` — by design.

#### Flap-state classification

* `flapping`  — ≥5 transitions in the last 60 minutes (RFC-4271
                damping-candidate threshold).  Alerting-grade.
* `unstable`  — ≥5 transitions in the last 24 hours but not in
                the last hour.  Operator attention recommended.
* `stable`    — neither of the above.

Pure decision logic lives in `netcortex/graph/history.py`
(13 unit tests in `tests/graph/test_history.py`).  The correlator
side (`_update_status_history()` in `netcortex/graph/correlate.py`)
is a thin orchestrator that reads current + history per element,
calls `apply_transition`, and writes back.  Idempotent on a
stable graph; runs every correlation cycle as Phase 5a (after the
oper_status correlators, before `_stamp_freshness`).

#### Cold-start behaviour

The first time `_update_status_history()` sees a Device or link,
it seeds a single-entry history at `now` so the connectivity
strip has something to draw.  No history is fabricated for
older timeframes — operators see real data only.

#### What this unblocks (Phases B/C/D)

* **Phase B** — connectivity-strip UI component (the green/red bar
  in the operator's screenshot) reads `<field>_history` directly.
* **Phase C** — Links table will sort by `flap_score_1h DESC` so
  the most-flappy cables surface first.
* **Phase D** — MCP `history.get` and `top_problems` tools will
  read flap_state and history to power agent diagnoses.

### Added
* `netcortex/graph/history.py` — pure module with
  `apply_transition`, `parse_history`, `trim_history`,
  `compute_flap_stats`.  Window/cap constants documented.
* `_update_status_history()` correlator step (Phase 5a).
* `tests/graph/test_history.py` — 13 unit tests covering
  seeding, transitions, window trim, max-events cap, flap
  classification thresholds, normalisation, and refresh-only
  semantics on the no-op observation path.

### Changed
* `correlate_all()` now runs `_update_status_history()` between
  the per-cycle correlators and `_stamp_freshness()`; its stats
  appear in the `correlate.done` log line.

### Fixed
* `_infer_wan_topology()` deletes and rebuilds every correlator-
  owned `WAN_UPLINK` edge each cycle for stale-uplink cleanup,
  which previously would have wiped the new history properties on
  every cycle and made every WAN edge look perpetually stable.
  Added a snapshot-and-replay step around the destructive rebuild
  that captures `oper_status_history` (and the flap scalars) keyed
  by `(src_id, dst_id, via, wan_slot|asn)` and replays them onto
  the recreated edges.  Truly-retired uplinks have nowhere to
  land and their history correctly drops; refreshed uplinks keep
  full 7-day history across the cycle.  Verified end-to-end: 57
  WAN uplinks restored per cycle, transition count drops from 57
  (false positives) to 0 in steady state.

---

## [Unreleased — 0.6.0-dev11]

### Reusable chip filter: scope every view by site(s) and/or device(s)

Operator request: "In the topology view, I want to filter by sites
and nodes with autocomplete, chip selection, and an X to remove.
Then I want the same thing on Inventory, MAC/ARP, Spanning Tree,
and Routing."

Built a single reusable `createChipFilter()` component (in
`netcortex/status/templates/index.html`) that every view mounts and
parameterises with its own onChange callback and `storageKey` so
selections persist per-view across reloads.  The component:

* Renders a chip bar + input; typing filters the dropdown by
  prefix/substring across site labels, site slugs, device names,
  and device-home-site labels.
* Supports keyboard navigation (↑/↓/Enter/Esc) and Backspace on an
  empty input to pop the last chip.
* Each chip has an `×` to remove it.  Selections persist in
  `localStorage` under `nc.chipfilter.<view>` so an operator's
  scope survives reloads, view switches, and graph reloads.
* Catalog (`/api/filter-catalog` — new endpoint) is fetched once
  per page load and shared by all instances.  Returns
  `{sites, devices}` derived from canonical Site nodes (preferred)
  with a fallback to `Device.netbox_site_slug` so deployments
  without explicit Site nodes still see a populated picker.

Per-view semantics:

* **Topology** — chip-filter scope flows through the existing
  `applyOverlayVisibility()` pass.  Site chips hide every Device
  whose `netbox_site_slug` doesn't match; Device chips keep the
  picked device PLUS its 1-hop Device neighbours so the operator
  still sees connectivity context.  Non-Device leaf nodes
  (Interface, VLAN, Prefix, IPAddress, MAC/ARP, RoutingPeer) are
  kept only when anchored to a visible Device.  Empty compound
  containers are hidden so the canvas doesn't show empty bubbles.
  Works in both the aggregated overview (matches `AggregateSite`
  bubbles by container_id/slug/label) and the full detail view.
* **Inventory** — chips filter rows by `name`/`site_slug`/`site`.
  Added `site_slug` to the inventory response so chip→row matching
  works by slug (more reliable than display name).
* **MAC/ARP** — chips filter by `learned_device` and `owner_device`.
  Site chips cross-reference via the catalog's device→site map so a
  site scope works even though CAM rows don't carry a site column.
* **STP** — a domain is kept if its root device OR any member
  matches a chip.  We don't chop members out of a kept domain;
  operators want to see the whole spanning tree once it's in scope.
* **Routing** — prefixes kept if any attached device matches;
  routing peers kept if either endpoint (local OR remote) matches.

Chip filter coexists cleanly with the existing free-text search
boxes — text search narrows within the chip-defined scope, not
across the whole dataset.  Topology's `/api/graph` truncation and
`fcose` layout cost both shrink with the chip filter applied, so
scoping to a single site is the recommended workflow for
operators investigating a specific deployment.

### Added
* `/api/filter-catalog` endpoint returning `{sites, devices}` for the
  chip picker.  Cached by browser for the page lifetime.
* `createChipFilter()` JS component (multi-instance, autocomplete,
  keyboard nav, localStorage persistence, X to remove).
* `chipFilterMatchesRow()` helper with device→site cross-reference.
* `site_slug` field on every inventory row.

### Changed
* Topology toolbar gains a chip-filter input next to the search box.
* Inventory, MAC/ARP, STP, and Routing toolbars gain a "Scope:"
  chip-filter input; their existing "Filter:" text boxes are
  renamed to "Search:" to make the two-stage filter model clear.
* `applyOverlayVisibility()` is now the single source of truth for
  topology element visibility — overlay state AND chip-filter scope
  both flow through one batched pass.

---

## [Unreleased — 0.6.0-dev10]

### PHYSICAL_LINK ↔ Prefix attachment: anchor routed /30s, /128s on their cable

Operator question: "Why are these /30 prefix nodes floating loose?
Shouldn't they hang off the physical link they live on?"

Yes.  Today we have three carriers a routed prefix can attach to:
**VLAN** (via SVI — handled), **physical link** (routed port — was
half-handled, mostly missing), and **tunnel/loopback** (out of
scope for this change).

The existing decorator `_decorate_physical_links_l3()` only
fired when **both** endpoints of a cable had an `ASSIGNED_IP`
discovered.  In real deployments this almost never happens for
operator-friendly reasons:

* eBGP transit /30s often go to a partner router we don't poll.
* ARP-correlated cables only know one side's IP by construction.
* Cable peer may be SNMP-blacked or behind a Meraki MX where the
  per-port IP table isn't exposed.

Result: of 44 floating Prefix nodes in the sample graph, only the
"perfect" few were absorbed.  The rest — all the /30s and IPv6
/64s between catalysts — rendered as free-floating cyan circles
around the device, polluting the canvas with information that
should have been on the cable.

Rewrite uses the much-richer `ROUTES_TO {interface}` evidence
that the SNMP / Catalyst-Center / Meraki adapters already emit
for every directly-connected route:

```cypher
MATCH (d:Device)-[rt:ROUTES_TO]->(p:Prefix)
WHERE rt.interface IS NOT NULL
  AND NOT toLower(rt.interface) STARTS WITH 'vlan'  -- SVI handled elsewhere
  AND NOT toLower(rt.interface) STARTS WITH 'loopback'
  AND NOT toLower(rt.interface) STARTS WITH 'tunnel'
MATCH (d)-[pl:PHYSICAL_LINK]-(:Device)
WHERE (startNode(pl) = d AND
         toLower(coalesce(pl.interface_a_active, pl.interface_a))
           = toLower(rt.interface))
   OR (endNode(pl) = d AND
         toLower(coalesce(pl.interface_b_active, pl.interface_b))
           = toLower(rt.interface))
RETURN elementId(pl), coalesce(p.cidr, p.prefix) AS cidr, p.version
```

Single-sided evidence is enough — if cat9k1's `Twe1/1/1` has a
ROUTES_TO `192.133.161.128/30` and there's a PHYSICAL_LINK out
of `Twe1/1/1`, the cable demonstrably carries that prefix
regardless of whether we ever spoke to the peer.

The original both-sides-ASSIGNED_IP pass is retained as a
fallback for cables where ROUTES_TO is missing but both
endpoints share a Prefix CIDR.

#### New PHYSICAL_LINK edge properties

A cable can now carry multiple CIDRs (dual-stack /30 + /127,
parent of several sub-interfaces, etc.), so the singular
`l3_prefix` is augmented with two lists:

| Property        | Type        | Meaning                                     |
|---|---|---|
| `l3_prefix`     | string      | first match (kept for backwards compat)    |
| `l3_prefix_v4`  | list[str]   | every IPv4 CIDR routed on this cable        |
| `l3_prefix_v6`  | list[str]   | every IPv6 CIDR routed on this cable        |
| `is_routed`     | bool        | true iff any prefix attached                |
| `l3_updated_at` | epoch-ms    | for stale-data pruning                      |

#### Absorption (`_mark_absorbed_prefixes`)

Updated to read both the legacy `l3_prefix` and the new lists.
A Prefix already attached to a VLAN OR a PHYSICAL_LINK gets
`absorbed=true` stamped so the UI hides the standalone node
(the same flag used by the existing VLAN-absorption path).

Stale-stamp clearing also widened to recognise list membership.

#### UI changes

* **On-canvas cable label** — collapsed "N prefixes" or single
  CIDR appears alongside the existing "N VLANs" / "STP F/F"
  tags, so a quick glance answers "what L3 does this cable
  carry?".
* **Hover tooltip** — new "L3 / Routed" block lists every
  IPv4 and IPv6 CIDR the cable carries, family-grouped.
* Floating Prefix nodes whose CIDR is now anchored to a cable
  disappear automatically (existing display-filter on
  `absorbed=true`).

Files touched:
- `netcortex/graph/correlate.py`
  (rewrite `_decorate_physical_links_l3`; widen
  `_mark_absorbed_prefixes` to read v4/v6 lists)
- `netcortex/status/templates/index.html`
  (PHYSICAL_LINK label + hover blocks)

### Not in scope for this change

* Loopback /32 and /128 prefixes — still free-floating; they
  belong on the Device, not on any cable.  Folded-into-device
  is the next iteration.
* Tunnel (DMVPN / GRE / IPsec) and SDWAN-learned remote subnets
  — also floating today; they need their own carrier
  abstraction (Tunnel or SDWANTunnel) before they can be
  absorbed.
* Routed cables that have no in-graph peer (e.g. `Te1/0/46`
  upstream to a partner router we don't poll) still leave the
  prefix orphaned — there's no cable to attach to.  These
  should be modelled as WAN_UPLINK / external-AS edges in a
  later pass.

---

## [Unreleased — 0.6.0-dev9]

### Local-AS halo: gate on the L3 overlay

Operator screenshot showed `cpn-ash-cat9k1`, `cpn-ash-cat8k1`, and
`cpn-ful-cat9k1` rendered with a thick gold border and amber fill
even when only the **Physical** overlay was active.  Those are the
eBGP-speaking edge routers; the gold treatment was the "home-AS
membership" halo introduced in dev3 to replace the retired home-AS
hexagon.  But the Cytoscape rule was always-on:

```js
selector: 'node[type="Device"][?local_asn]'
```

So *any* Device with `local_asn` populated lit up gold regardless of
which overlay the operator was looking at.  When the user is in the
Physical view (cables only), an AS halo on a handful of devices is
noise — AS membership is a routing concept, not a physical one.

Fix: gate the rule on a `.show-asn-halo` class and toggle that class
based on the L3 (Routing peers) overlay state.

```js
selector: 'node[type="Device"][?local_asn].show-asn-halo'
```

A new `applyAsnHalo()` helper stamps the class onto qualifying
Device nodes when L3 is active, and strips it otherwise.  It hooks
into `applyOverlayVisibility()` so it fires on initial load AND on
every overlay toggle — no refetch, no layout pass, just a class
flip.  When L3 is off:

* The 4px gold `border-color`/`border-style`/`border-width` and the
  amber `overlay-color`/`overlay-opacity` no longer match → the
  Device renders with its role-based default border.
* The custom `label` function no longer matches → label falls back
  to the earlier `node[type="Device"]` rule, which is the bare
  hostname.  The "(AS11017)" suffix disappears.

When L3 is on: same gold + AS-suffix treatment as before.

Files touched:
- `netcortex/status/templates/index.html` (gate halo selector;
  add `applyAsnHalo()`; call it from `applyOverlayVisibility()`).

---

## [Unreleased — 0.6.0-dev8]

### PHYSICAL_LINK health: scope to per-link interface (fix false "down" cables)

Operator screenshot showed essentially every cable on `cpn-ash-cat9k1`
(and similar chassis) rendered as a red dotted "down" link, including
healthy uplinks where both sides were operational.

Root cause: `_enrich_physical_links_with_health` joined **every**
`HAS_INTERFACE` on each device endpoint, not the specific interface
that the PHYSICAL_LINK actually terminates on.  Cypher excerpt before:

```cypher
MATCH (a:Device)-[link:PHYSICAL_LINK]->(b:Device)
OPTIONAL MATCH (a)-[:HAS_INTERFACE]->(ia:Interface)
  WHERE ia.health_score IS NOT NULL
OPTIONAL MATCH (b)-[:HAS_INTERFACE]->(ib:Interface)
  WHERE ib.health_score IS NOT NULL
```

A chassis with even one down port — a powered-off AP, an unused
copper port, a shutdown trunk — caused **every** PHYSICAL_LINK
incident to that chassis to inherit `oper_status='down'` and a red
`health_score=80`.  On a 48-port access switch this meant ~all
cables lit up red, which is what the screenshot captured.

Fix: scope the health match to the per-link interface using the
already-populated `interface_a` / `interface_b` (and their `_active`
variants for multi-rate SFP cages on Cisco platforms):

```cypher
WITH a, b, link,
     coalesce(link.interface_a_active, link.interface_a) AS ia_name,
     coalesce(link.interface_b_active, link.interface_b) AS ib_name
WHERE ia_name IS NOT NULL OR ib_name IS NOT NULL
OPTIONAL MATCH (a)-[:HAS_INTERFACE]->(ia:Interface)
  WHERE ia_name IS NOT NULL
    AND (ia.name = ia_name OR ia.canonical_name = ia_name)
OPTIONAL MATCH (b)-[:HAS_INTERFACE]->(ib:Interface)
  WHERE ib_name IS NOT NULL
    AND (ib.name = ib_name OR ib.canonical_name = ib_name)
```

Per-side `a_reports` / `b_reports` flags now gate the oper_status
derivation so a side without an Interface match doesn't pollute the
state.  `single_sided` is now `NOT (a_reports AND b_reports)` — true
only when the specific port on one end is unknown to us, instead of
being true whenever any random port on the chassis was missing data.

A separate one-shot cleanup also clears the stale per-edge
`oper_status` / `health_score` / `util_pct` / `single_sided` that
were stamped by the old (over-scoped) Cypher, so the first
post-deploy correlator pass starts from a clean slate; without this
the old red state would have persisted on every cable until the new
scoped pass re-stamped it.

Files touched:
- `netcortex/graph/correlate.py` (rewrite `_enrich_physical_links_with_health`).

---

## [Unreleased — 0.6.0-dev7]

### Routing peer adjacencies: both ASes populated, duplicate-edge fix

Two follow-ups to dev6's routing-peer rewrite, both driven by the
operator's verification of the hover tooltip:

1. **Hover only showed the remote AS, not the local AS.**  SNMP's
   BGP4-MIB only reports the *neighbor's* AS to whichever device is
   polling (the local AS isn't part of the per-peer row).  Our
   correlator filled in `remote_as` from the stub but left `local_as`
   permanently `NULL` unless a symmetric observation happened to land
   on the same edge.

   Fix: pull each observer's own AS from `Device.local_asn` (which
   the WAN correlator sets from the BGP4-MIB's `bgpLocalAs` scalar
   and from inferred home-AS detection), and stamp it into whichever
   side of the canonical adjacency the observer occupies.  Symmetric
   observations from the peer side fill in the *other* AS the same
   way, so after two cycles both AS numbers are present.  Result:
   tooltips now show `Local: 192.133.176.146 (AS64515)` ⇄
   `Remote: 192.133.176.130 (AS11017)` instead of one side being
   unattributed.

   For BGP sessions where the stub didn't report `remote_as` (e.g.
   the peer didn't expose BGP4-MIB), we also fall back to *B's*
   `local_asn` so the remote side is still attributed.

2. **Duplicate device-to-device adjacencies (one per observation
   side).**  The dev6 MERGE key was `(protocol, address_family,
   remote_ip)`, which is direction-dependent: when A polled B the
   remote_ip was B's IP, but when B polled A the remote_ip became A's
   IP.  Both observations landed on the same canonical (src, dst)
   pair but with swapped IPs, so the MERGE created two edges that
   should have been one.

   Fix: MERGE key is now the full canonical 4-tuple `(protocol,
   address_family, local_ip, remote_ip)` where local/remote are
   computed from the canonical direction (smaller device id is the
   local side), not from the observer's perspective.  Symmetric
   observations now converge on a single edge.  Combined with
   `coalesce()`-protected SET clauses for `local_as`/`remote_as`,
   each MERGE contributes only the fields its observation knows
   about, never overwriting fields the previous observation filled
   in.

3. **Bonus**: also tightened the observer's canonical-id redirect so
   if the source device A is itself a `cdp-neighbor:` stub, the new
   adjacency lands on A's canonical id instead — preventing the
   tail-purge from having to delete spurious stub-targeted edges
   every cycle.

---

## [0.6.0-dev6]

### Routing peer adjacencies are first-class dashed edges

Previously every BGP / OSPF / EIGRP session was folded onto the
`PHYSICAL_LINK` it (allegedly) rode over: `_collapse_routing_peers`
stamped a `routing_protocols` list on the cable, and the API's
`collapse_l3_on_physical` pass annotated each cable with a `carries[]`
array.  In the UI the operator had to read tiny labels on the cable
("BGP · OSPF") with no indication of which sessions were actually
*up*.  Worse, sessions that didn't follow the cable topology (iBGP on
loopbacks, OSPF on SVIs, eBGP across a multi-hop transit) were either
hidden behind a faded `ROUTES_OVER_UNKNOWN` arc or simply lost.

This release replaces that model end-to-end:

1. **Correlator** (`_collapse_routing_peers`, rewritten).  For every
   `(Device A)-[:ROUTING_PEER]->(RoutingPeer{peer_ip})` stub the
   adapter wrote, the correlator now resolves peer Device B by IP and
   MERGEs a direct `(A)-[:ROUTING_PEER]->(B)` adjacency edge
   carrying:

   * `protocol`, `address_family` (ipv4/ipv6), `state` (raw),
     `oper_status` (up/down/unknown derived from state)
   * `local_ip`, `remote_ip` (the IPs each side is using for *this*
     session, picked by /24 or /64 subnet match against A's
     interface IPs; falls back to A's `mgmt_ip`)
   * `local_as`, `remote_as`, `router_id`
   * `oper_status_changed_at` — stamped on transition only, so the UI
     can render "up for 3d" / "down since 12m ago"

   Edge key is `(protocol, address_family, remote_ip)` so parallel
   sessions between the same pair (v4 BGP + v6 BGP + OSPF) each get
   their own line.  Direction is canonicalised lex-smaller-id-first.

   Stub `(A)-[:ROUTING_PEER]->(RoutingPeer)` edges for *external*
   peers (transit, customer-managed) are left intact so those still
   render.

2. **Cleanup of the old model**.  Every cycle the correlator now
   *removes* `routing_protocols` / `routing_updated_at` properties
   from `PHYSICAL_LINK` edges and `VLAN` nodes, and DELETEs every
   `ROUTES_OVER_UNKNOWN` edge.  Idempotent and cheap.

3. **API** (`query.py` → `collapse_l3_on_physical`).  No longer
   builds `carries[]`.  Instead, when both endpoints of a Device→
   RoutingPeer stub edge resolve to known devices that already have a
   Device↔Device ROUTING_PEER for the same `(protocol, peer_ip)`,
   the stub edge is dropped (otherwise two overlapping renderings of
   the same session).  Orphan RoutingPeer nodes that lose their only
   incoming edge are also removed.

4. **UI** (`index.html`).
   * `PHYSICAL_LINK` style/label no longer reads `routing_protocols`
     or `carries[]`.  Cables are clean L1/L2 objects again.
   * `ROUTING_PEER` style: **always dashed** with a 6/4 pattern so
     adjacencies are visually distinct from cables; colour is
     `oper_status` (green=up, red=down, slate=unknown), not protocol.
     Label = `BGP AS65000` / `OSPF` / `OSPF v6`.
   * Edge hover for a `ROUTING_PEER`: protocol + AFI + raw state
     header, then `Local: <ip> (AS<n>)`, `Remote: <ip> (AS<n>)`,
     `Router-ID`.  The existing oper-status badge ("● UP (down 12m
     ago)") already handles the up/down + since timer.
   * `"Routing peers"` overlay tooltip rewritten to describe what
     the dashed lines mean.
   * `ROUTES_OVER_UNKNOWN` style block removed.

5. **Bookkeeping**.  `EdgeType.ROUTES_OVER_UNKNOWN` is no longer
   produced; the enum value is retained for back-compat in case
   external consumers still reference it.

---

## [0.6.0-dev5]

### Eliminate transient duplicate links + universal freshness stamping

Three follow-ups to the dev4 work, driven by what the operator actually
saw when verifying the screenshots:

1. **Transient duplicate PHYSICAL_LINK edges in the UI.**  After
   Meraki ingest wrote an `lldp`-derived cable between two Meraki
   devices, the next correlator pass would create a parallel
   `mac_correlation` edge (the `NOT EXISTS` guard correctly *skipped*
   creating it for new pairs, but pre-existing `mac_correlation`
   edges from earlier cycles, when no LLDP existed yet, were left
   intact and only cleaned up by `_dedupe_physical_links_by_pair`).
   Convergence took one full cycle — long enough for the operator's
   screenshot to capture both the LLDP edge (with health/state) and
   the orphan `mac_correlation` edge (no health/state) sitting
   side-by-side on the same pair.

   Fix: `_correlate_via_mac` and `_correlate_via_arp` now perform a
   **proactive cleanup pass** at the top of the function — they
   DELETE any `mac_correlation` / `arp_correlation` edge whose pair
   has since acquired a higher-confidence `lldp`/`cdp`/`*_topology`
   edge.  The cleanup runs in the SAME session as the MERGE so the
   graph is internally consistent at every read point; no follow-up
   dedupe pass is required to drop the stale edge.

2. **Universal freshness stamping.**  Coverage audit of the new
   `first_seen`/`last_seen` properties showed many edge types with
   0% coverage (`LOCATED_AT`, `LOGICAL_MEMBER`, `OWNS_MAC`,
   `HAS_PREFIX`, `HAS_SVI`, `STP_ROOT`, `SDWAN_TUNNEL`, `TRANSITS`,
   `FABRIC_PEER`, `WAN_UPLINK`) — these are all created by
   correlator-side `MERGE` statements that bypass the
   `ingest._merge_edges` codepath where the stamping was added.

   Fix: new `_stamp_freshness()` correlator step runs LAST in
   `run_correlation()` and stamps `first_seen` (when missing) and
   `last_seen` (every cycle) on **every** node and edge in the
   graph.  Uses Neo4j's native `timestamp()` for atomicity and
   batches efficiently on a fresh-load (~5k nodes + ~3k edges in
   roughly one round-trip each).

3. **`Device.status_changed_at` was always null on first
   observation.**  The Device branch of `_merge_nodes` only fires
   the `status_changed_at` stamp when `row.status` differs from
   `prev_status`.  On a freshly-rebuilt container, every device's
   `prev_status` was already correct from the prior schema's hash
   short-circuit (so the Device was touched via
   `_touch_last_seen_nodes` instead of being rewritten through the
   transition-detection branch).  Result: the UI's "up since X"
   widget never had a baseline.

   Fix: `_stamp_freshness()` also backfills
   `Device.status_changed_at = Device.first_seen` for every device
   that has a status but no recorded transition yet.  From that
   point on, real transitions detected in `_merge_nodes` will
   overwrite the backfilled value with the actual change time.

### Files

* `netcortex/graph/correlate.py` —
  `_correlate_via_mac` / `_correlate_via_arp` add a leading cleanup
  Cypher to DELETE stale low-conf edges.  New `_stamp_freshness()`
  function called as the final step of `run_correlation()`.
  Reported in `correlate.done` log as `freshness_nodes_touched`,
  `freshness_edges_touched`, `devices_status_backfilled`.

* `netcortex/__init__.py` / `pyproject.toml` — bump to
  `0.6.0-dev5` / `0.6.0.dev5`.

### Behaviour-equivalent in the API and UI

The topology JSON returned by `/api/graph` already filtered out
mismatched edges as the dedupe converged, so external integrators
see no change in shape — only that duplicate `mac_correlation`
edges no longer appear in mid-cycle reads.  The UI hover tooltips
that consume `first_seen`/`last_seen` / `status_changed_at` start
filling for previously-blank object types automatically once the
worker has run one full correlation cycle on the rebuilt code.

---

## [Unreleased — 0.6.0-dev4]

### Last-seen / first-seen on every object + device status badge + Meraki port health

Four operator-visible problems addressed in one cycle:

1. **Fairfax MS↔MX link rendered with no health/state.**  Meraki
   devices were never SNMP-polled for per-port telemetry, so the
   existing `_enrich_physical_links_with_health` (which reads
   `Interface.health_score`) had nothing to lift onto Meraki↔Meraki
   cables — they always rendered neutral grey "unknown health".
   New Meraki adapter steps:
     * `get_switch_port_statuses(serial)` — calls
       `/devices/{serial}/switch/ports/statuses?timespan=600`,
       converts Meraki's `Connected/Disconnected/Disabled/Ready`
       vocabulary to `up/down/disabled`, parses the `speed` string
       into Mbps, derives `util_pct` from `trafficInKbps.total /
       speed_mbps`, and rolls errors/warnings into a 0-100
       `health_score` (same buckets the SNMP adapter uses, so the
       enrichment correlator picks Meraki and SNMP ports up
       uniformly).
     * `get_appliance_uplink_statuses()` — calls
       `/organizations/{orgId}/appliance/uplink/statuses` and stamps
       per-uplink (`mx_wan1_status` / `mx_wan2_status`) onto the MX
       Device node properties.  More accurate than the device-wide
       `status` rollup, which says "online" even when one of two
       uplinks has failed.
     * `discover()` new step 5c: fetch port statuses for every
       MS/C9 switch (asyncio semaphore=8), MERGE the result onto
       `meraki-if:<serial>:<port>` Interface nodes so the existing
       `_enrich_physical_links_with_health` pass colors the cable
       on the next correlation tick.  Now MS↔MX cables render
       green/amber/red instead of grey.
     * `discover()` new step 5d: fetch uplink statuses for every
       MX and stamp them onto the Device node.
     * `_enrich_wan_uplinks_with_health` (Rule 2 / MX uplinks) now
       prefers `mx_wan1_status` / `mx_wan2_status` over `Device.status`
       when available, so the WAN_UPLINK edge color reflects the
       actual per-uplink state instead of the cloud-status rollup.

2. **"How long has this link been down?  When did we last see this
   MAC?"** — first_seen / last_seen on every node and edge, with
   transition timestamps on operational state:
     * New `netcortex/util/timestamps.py` with `epoch_ms()` helper
       and shared property-name constants.
     * `graph/ingest.py:_merge_nodes` and `_merge_edges` stamp
       `first_seen` (ON CREATE) and `last_seen` (every write) on
       every MERGE, using a single per-cycle `now_ms` so all rows
       in a batch share the same clock reading.
     * `graph/ingest.py:_touch_last_seen_nodes` refreshes
       `last_seen` even on nodes whose content-hash unchanged
       (without this we couldn't distinguish "no change since
       last cycle" from "node disappeared from the adapter's
       view"); legacy nodes that pre-date the refactor get
       `first_seen` back-filled to the current cycle.
     * Device-specific path in `_merge_nodes` captures the
       pre-merge `status` value via `WITH ... AS prev_status` and
       conditionally stamps `status_changed_at` only on a real
       transition — so the UI can render "down 3h ago" with the
       actual moment the device went offline, not the moment we
       last polled it.
     * `_enrich_wan_uplinks_with_health` (both eBGP and MX
       branches) does the same change-detection for
       `oper_status_changed_at` so the WAN_UPLINK tooltip can
       answer "down since when?" for ISP-facing cables.
     * `_enrich_physical_links_with_health` derives an `oper_status`
       on PHYSICAL_LINK from the worst of its two endpoint
       interfaces' `oper_status` and stamps
       `oper_status_changed_at` on transitions.  PHYSICAL_LINK now
       has the same "down since X" story WAN_UPLINK has.

3. **Device tooltip shows status + last-seen on every node.**
   Hovering any Device now shows a colored status badge
   (UP/DOWN/ALERTING with the same vocabulary normalized across
   Meraki/NetBox/SNMP), plus "(down 3h ago)" when the device
   isn't healthy and we have a `status_changed_at`.  Every node
   tooltip (Device, VLAN, Prefix, MACAddress) gets a universal
   "Last seen: 2m ago / First seen: 14d ago" footer with the
   absolute timestamp in the row's `title` attribute for hover.
   The edge tooltip gets the same footer plus a top-line
   oper-status badge with "(down N ago)" when the link's been
   broken.  New formatter helpers `_fmtAgo()`, `_fmtAbs()`, and
   `_normDeviceStatus()` keep the rendering uniform across
   nodes and edges.

4. **Arlington "duplicate dashed lines" report.**  Verified in
   Neo4j and via the API: there is currently exactly one
   `PHYSICAL_LINK` per pair.  The duplicates in the screenshot
   were a transient ingest→correlate→dedupe window state, caught
   and cleaned by the existing `_dedupe_physical_links_by_pair`
   pass on the next tick — the user's browser was holding a
   stale render from that window.  No code change needed in
   the dedupe logic; the underlying mechanism (`NOT EXISTS`
   guard on mac/arp correlation + dedupe Rule 2 dropping
   inferred edges when LLDP exists) already prevents the
   duplicate from being permanent.

### Schema additions (Device / Interface / PHYSICAL_LINK / WAN_UPLINK)
* `first_seen`, `last_seen` — epoch ms, on every node and edge
* `status_changed_at` — epoch ms, on Device (ingest-stamped)
* `oper_status` — string (`up` / `down` / `disabled` / `unknown`),
  on PHYSICAL_LINK (derived) and WAN_UPLINK
* `oper_status_changed_at` — epoch ms, on PHYSICAL_LINK / WAN_UPLINK
* Interface stat additions on Meraki-discovered switch ports:
  `oper_status`, `speed_mbps`, `util_pct`, `util_in_pct`,
  `util_out_pct`, `error_rate_per_s`, `health_score`, `has_baseline`,
  `status_raw`, `errors`, `warnings`, `is_uplink`
* Device additions on MX: `mx_wan1_status`, `mx_wan2_status` (and
  `_raw`), `mx_wan1_ip` / `mx_wan2_ip`, `mx_wan1_gateway` /
  `mx_wan2_gateway`

---

## [Unreleased — 0.6.0-dev3]

### WAN overlay v3: drop the home-AS hexagon, light up the home-AS members, link health on uplinks

Two changes from operator feedback on the dev2 picture:

1. The home AS hexagon (`AS11017 (home)`) and the `AS_PEER` edges
   feeding into it were "an intermediate shape" that didn't pull its
   weight on top of the per-device `local_asn` halo.  Dropped both:
     * `_infer_wan_topology` no longer materializes the home-AS node
       or AS_PEER edges; the eBGP `WAN_UPLINK` from the border
       device to the external AS IS the AS boundary now.
     * Stale `is_home=true` AS nodes are swept on the next pass so
       an upgrade-in-place stops showing the old hexagon.
     * `query.py` dropped `AS_PEER` from the WAN overlay rel set;
       the `EdgeType` is kept for back-compat but no overlay
       references it.
   The home AS membership is now communicated visually by:
     * a solid 4px gold border on every Device with
       `local_asn=<home_asn>` (was a 2.5px dashed ring in dev2)
     * a soft amber overlay tint behind the same devices (Cytoscape
       `overlay-color`) so they read as "inside our AS" at a glance
     * an "(AS11017)" suffix on the device label so the membership
       is legible without color (printed reports, colorblind ops).

2. New correlator `_enrich_wan_uplinks_with_health` annotates every
   `WAN_UPLINK` with port state, error rate, utilization, and a
   normalized `health_score` — same vocabulary as `PHYSICAL_LINK`.
   The UI now colors WAN uplinks by health (green / amber / red),
   sizes them by utilization, and falls back to dashed-red when the
   port is down.
     * **eBGP uplinks**: the egress interface is found by walking
       every interface IP on the border device and matching the
       smallest /N (start at /31, expand to /24) that contains both
       the eBGP peer IP and the interface IP.  Health properties
       (`oper_status`, `util_in_pct`, `health_score`,
       `error_rate_per_s`, `speed_mbps`) are copied onto the WAN_UPLINK
       edge.  For `cpn-ash-cat8k1` this resolves to
       `TenGigabitEthernet0/0/4` (the actual HE-facing port); for
       `cpn-ful-cat8k1` to its Lumen-facing 10G port.
     * **MX uplinks**: Meraki MX devices don't reliably expose WAN
       ports via IF-MIB, so health bucketizes from `Device.status`
       (active/online → up, alerting/offline → down) and
       `Device.snmp_health` (`cloud_only` → 25 yellow,
       `unreachable` → 70 red).  Same property names → same UI
       styling code path.
     * Edge label now reads e.g.
       `eBGP · AS3356 · Te0/0/4 (up) · peer 4.59.246.209`
       or `WAN1 · (up) · 72.83.205.239` so the port + state are
       visible without hover.
     * eBGP boundary uplinks get a heavier baseline width (3.5px vs
       2.0px) so the AS adjacency still anchors the canvas now that
       the dedicated AS_PEER edge is gone.

### Known follow-ups

* MX device-level health is coarse — bridging Meraki Dashboard's
  `/devices/{serial}/uplinkLossAndLatency` into the correlator would
  give real latency / loss / jitter per WAN slot.
* Egress-interface match uses subnet overlap; for VRF-isolated transit
  links a same-IP-in-multiple-VRFs collision would mis-match.  Once
  we stamp `vrf` on the Interface side this should also be added to
  the match key.

## [Unreleased — 0.6.0-dev2]

### WAN overlay v2: iBGP vs eBGP split, home-AS detection, AS boundary edges

dev1 mis-read iBGP route-reflector sessions as Internet uplinks
because the rule "any public-ASN BGP peer is upstream" can't tell
the difference between an internal reflector inside your own AS
(e.g. AS11017 from a Cisco `cat9k` to `192.133.x.129`) and a real
eBGP session to a transit provider.  In the field that surfaced as
AS11017 being drawn as a peer of cpn-ful-cat8k1 *and* cpn-ash-cat8k1
when AS11017 is actually *our own* AS, with the two `cat8k`s being
its border routers.

dev2 fixes the model:

* New `correlate._detect_home_asn()` — auto-detects the operator's
  home AS by scoring every public ASN on (distinct-sites,
  distinct-devices, total-peers) and picking the winner that peers
  from ≥2 sites or ≥3 devices.  In the fleet this lands on
  AS11017 unambiguously; for shops with only one site / one BGP
  speaker it correctly returns None and behaves like dev1 (treat
  every peer as external).
* `_infer_wan_topology` now:
  1.  Materialises the home AS as a distinct `AutonomousSystem`
      node with `is_home=true` and the label `AS<asn> (home)`.
  2.  Splits every BGP peer into iBGP (same as home AS — skipped)
      vs eBGP (everything else public — emits `WAN_UPLINK`).
  3.  Emits a new `AS_PEER` edge per (home_as, external_as,
      border_device) triple so the AS adjacency map is explicit:
      "AS11017 ↔ AS3356 via cpn-ful-cat8k1 peer 4.59.246.209".
  4.  Stamps `local_asn=<home_asn>` on every Device with iBGP
      evidence of being inside the home AS, so the UI can render an
      AS-membership halo.
* `models.py`: new `EdgeType.AS_PEER`; `query.py`: AS_PEER joins
  the `wan` overlay.

### UI changes — ASN boundary depiction

* Home AS hexagon is bigger (66×54 vs 44×36), filled deep teal,
  ringed in dashed gold — the same gold the `AS_PEER` edges use, so
  the eye follows "boundary = gold".
* External upstream AS hexagons stay the smaller cyan look.
* `AS_PEER` edge: heavy 3.5px gold line, no arrowheads, labelled
  with `<border_device> · peer <ip>` so an operator can read the
  AS adjacency map straight off the canvas.
* Devices with `local_asn` get a 2.5px yellow dashed border ("inside
  our AS") that wins over the cyan `is_wan_edge` ring on border
  routers — the boundary signal is more informative when the WAN
  overlay is on than the border-status badge.
* Tailwind safelist gained `border-cyan-400 text-cyan-300` so the
  auto-generated WAN toggle button actually renders the cyan accent.

### Net effect on the production fleet

* AS11017 is now correctly identified as the home AS.
* Two real upstream AS_PEER edges replace the four bogus
  WAN_UPLINKs from dev1:
    * `AS11017 ↔ AS3356 (Lumen)` via `cpn-ful-cat8k1`
      peer `4.59.246.209`
    * `AS11017 ↔ AS6939 (Hurricane Electric)` via `cpn-ash-cat8k1`
      peer `209.51.164.17`
* iBGP peers on the cat9k devices no longer manufacture a
  WAN_UPLINK; they keep their `local_asn=11017` halo instead.

### Known follow-ups

* `bgpLocalAs` SNMP poll would let us confirm home-AS detection
  rather than infer it from peer-count heuristics.
* IP→ASN reverse lookup (Team Cymru DNS) would let Meraki MX
  uplinks transit a real AS hop instead of going straight to
  `Internet`.
* AS registry name lookup (e.g., `AS6939 → Hurricane Electric`)
  for the hexagon label.

## [Unreleased — 0.6.0-dev1]

### New WAN topology overlay

First stab at the long-requested WAN dimension.  Adds two new node
types (`Internet`, `AutonomousSystem`) and two new edges
(`WAN_UPLINK`, `TRANSITS`), all stamped by a single new correlator
pass (`_infer_wan_topology`) that runs alongside the STP / VLAN /
routing decoration cycle.

Two discovery rules fire today:

1. **Meraki MX uplinks** — every Device with a `wan1_public_ip` or
   `wan2_public_ip` already populated by the Meraki adapter gets a
   direct `WAN_UPLINK` to the singleton `Internet` node.  Properties
   captured on the edge: `via=mx_uplink`, `wan_slot=wan1|wan2`,
   `public_ip`, `private_ip`.  Dual-WAN MXes emit two edges, one per
   slot.  ~224 Meraki MX devices in the corp/gov fleets light up
   immediately.
2. **eBGP-to-public-AS adjacencies** — every Device with an
   established `ROUTING_PEER` whose `remote_as` is outside the
   private/reserved ASN ranges (RFC 6996 et al.) gets a Device →
   AutonomousSystem → Internet path.  The AS node carries the
   numeric `asn` and a placeholder `name='AS<asn>'` (real registry
   names are a follow-up).  Today four Cisco border routers light up
   here: `cpn-ash-cat8k1` and `cpn-ful-cat9k2` to **AS6939**
   (Hurricane Electric), `cpn-ful-cat8k1` to **AS3356** (Lumen),
   and an AS11017 (Cisco corporate transit) ladder.

Both rules also stamp the participating Device with
`is_wan_edge=true` and a `wan_edge_reason` provenance tag so the UI
can highlight border devices (gold/cyan outline) even with the WAN
overlay off.  Default-route-based discovery is staged but not active
yet — SNMP isn't currently capturing `0.0.0.0/0` routes from the
Cisco devices; once that lands the same correlator can emit a Rule 3
edge tied to the next-hop's egress interface.

UI changes:

* New `WAN` overlay toggle in the topology toolbar (cyan accent),
  wired into the existing `applyOverlayVisibility()` machinery so
  enabling/disabling it is purely a client-side restyle — zero refetch.
* Cytoscape selectors for `Internet` (big cloud), `AutonomousSystem`
  (cyan hexagon labelled with `ASxxxx`), `WAN_UPLINK` (sky-blue
  dashed arrow labelled with slot + public IP, or eBGP + AS + peer
  IP), and `TRANSITS` (thinner solid AS-to-Internet arrow).
* Border devices get a 3px cyan outline whenever `is_wan_edge=true`,
  visible regardless of overlay state.

Internal:

* `models.py`: added `Dimension.WAN`, `NodeType.INTERNET`,
  `NodeType.AUTONOMOUS_SYSTEM`, `EdgeType.WAN_UPLINK`,
  `EdgeType.TRANSITS`.
* `query.py`: `wan` overlay added to both `_OVERLAY_RELS` and
  `_DIMENSION_RELS`; auto-appears in `/api/graph/overlays`.
* `correlate.py`: new `_infer_wan_topology()` pass with
  `_is_public_asn()` helper; clears stale `is_wan_edge` /
  correlator-tagged `WAN_UPLINK` before re-stamping each cycle, and
  sweeps orphan `AutonomousSystem` nodes that no device uplinks to.

## [Unreleased — 0.5.0-dev8]

### Inter-domain trunks and orphan STP members now render distinctly

The dev7 STP overlay coloring made coexisting domains visually
distinct, but reviewing `cpn-ful` and `cpn-gov-matrich` surfaced
two more visualization gaps:

**Gap A — inter-domain trunks looked like backbone cables.**
At `cpn-ful` the switch `cpn-ful-cat9k1` is L2-trunked into the
neighboring `cpn-ful-fabric` Meraki network with seven physical
cables (cat9k1 ↔ fabric-cat9k3/4/5/6, several as LAG pairs).
These trunks carry the site's 29 VLANs across an STP scope
boundary — they are **not** part of either spanning tree's active
path, but dev7 painted them in the legacy gold fallback color
which was indistinguishable from intra-domain backbone cables.
The operator couldn't tell which cables were active spanning-tree
edges and which were the bridge between the two trees.

**Gap B — phantom roots claimed to anchor a tree they have no link to.**
At `cpn-gov-matrich`, Meraki's cloud reports `SW2` as the root of
`STP cpn-gov-matrich` and `SW1` as a member, but the site has
**zero PHYSICAL_LINK cables** and `SW2` has no
`netbox_site_slug` / no inventory record.  Most likely SW2 was
once at this site, was decommissioned, and the cloud still
remembers its bridge id as the elected root.  Dev7 happily drew
SW2 with the gold ROOT crown, giving the impression of a real
spanning tree where there was none.

Both fixed by one new correlator pass and two new UI classes:

**`correlate._stamp_stp_link_topology`** writes two derived
projections:

  * `PHYSICAL_LINK.stp_inter_domain` (bool) — `true` when both
    endpoints carry an `stp_domain_id` but the IDs differ.
    Cleared (`NULL`) when endpoints share a domain or one lacks
    STP context.
  * `Device.stp_peers_in_domain` (int) — count of distinct
    PHYSICAL_LINK peers that share the device's `stp_domain_id`.
    Zero means "claimed STP membership but no in-domain peer
    reachable by cable" → phantom root / orphan member.

**UI** picks both up via new classes in `applyStpOverlay`:

  * `edge[type="PHYSICAL_LINK"].stp.interdomain` — muted slate
    dashed line + "inter-domain trunk" label.  At `cpn-ful` the
    seven cat9k1 ↔ fabric-cat9k3/4/5/6 cables now render as
    distinct grey-dashed bridges, leaving only the real
    intra-domain backbone (cat9k1 ↔ ccc01-sw1 ↔ n9k1, and the
    fabric-cat9k4/5/6 spokes back to fabric-cat9k3) in the
    domain's color.
  * `node[type="Device"].stp-root.stp-orphan` and
    `node[type="Device"].stp-member.stp-orphan` — dashed border
    and "ROOT (pri N) — unverified" / "↳ root (unverified): X"
    suffix.  At `cpn-gov-matrich` SW2 and SW1 now visibly
    declare the claimed STP membership is on paper only.

The inter-domain check uses the correlator-stamped flag and falls
back to a live endpoint comparison so a freshly-discovered cable
gets classified correctly before the next correlation tick.

### Files touched

| Change                                            | File                                                        |
| ------------------------------------------------- | ----------------------------------------------------------- |
| New correlator pass (inter-domain + peer count)   | `netcortex/graph/correlate.py` (`_stamp_stp_link_topology`) |
| New `.stp.interdomain` and `.stp-orphan` styles + toggle wiring | `netcortex/status/templates/index.html` (`applyStpOverlay`) |

---

## [Unreleased — 0.5.0-dev7]

### Per-domain colors so coexisting STP trees stop blending together

Reviewing `cpn-ful` in dev6 surfaced a real-but-confusing situation:
the site actually runs **three** independent STP domains visible at
the same time:

  * `STP cpn-ful`         — Meraki network `L_646829496481090953`,
    rooted at `cpn-ful-ccc01-sw1` with `cpn-ful-cat9k1` as a member
  * `STP cpn-ful-fabric`  — Meraki network `L_686235993220637798`,
    rooted at `cpn-ful-fabric-cat9k3` with cat9k4/5/6 as members
  * an SNMP-discovered IEEE bridge domain (`stp-domain:<root-mac>`,
    unnamed) — also rooted at `cpn-ful-ccc01-sw1`, with the Nexus
    `cpn-ful-n9k1` as its only member

These are real separate root elections (Meraki treats each
*Meraki network* as its own spanning tree even when peered together
physically), but dev6 painted every STP element in the same amber
palette so the two visible clusters looked fused.  The third
(unnamed SNMP) domain projected an empty domain name onto n9k1's
badge.

This release fixes both:

**1. SNMP and Meraki STP views are reconciled.**
A new correlator pass
(`correlate._merge_redundant_stp_domains`) walks every pair where a
named `stp:*` domain and an unnamed `stp-domain:*` domain share the
same root device and folds the unnamed one into the named survivor:
membership edges (`STP_ROOT` / `STP_MEMBER` with their priorities)
are MERGEd onto the survivor (tagged `merged_from_snmp=true`), the
survivor records the absorbed domain id in a `merged_from[]` list,
and the unnamed domain is `DETACH DELETE`d.  On the live fleet this
folded the Nexus's stub domain into `STP cpn-ful`, so `cpn-ful-n9k1`
now correctly shows `↳ root: cpn-ful-ccc01-sw1 / cpn-ful` instead
of a blank domain.

**2. Each STP domain gets its own deterministic color.**
`_stpDomainColor()` (new) hashes the `stp_domain_id` string into a
hue (HSL, S=70%, L=60%) so every domain id maps to a stable,
unique color that survives page reloads.  Five Cytoscape style
selectors were updated to call it for:

  * `node[type="Device"].stp-root`     border + label color
  * `node[type="Device"].stp-member`   ring + label color
  * `edge[type="PHYSICAL_LINK"].stp.backbone` line color + label color

The label format also expanded — root and member badges now
include the domain name as a third line, so the operator sees:

```
cpn-ful-ccc01-sw1
★ ROOT (pri 32768)
cpn-ful
```

and

```
cpn-ful-fabric-cat9k4
↳ root: cpn-ful-fabric-cat9k3
cpn-ful-fabric
```

The result is that the `cpn-ful` screenshot now renders as two
visually distinct trees (e.g. gold + cyan) instead of one big
amber smear with two random roots.

### Files touched

| Change                                | File                                                        |
| ------------------------------------- | ----------------------------------------------------------- |
| SNMP/Meraki STP domain reconciliation | `netcortex/graph/correlate.py` (`_merge_redundant_stp_domains`) |
| Per-domain color helper + selectors   | `netcortex/status/templates/index.html` (`_stpDomainColor`, 3 selector blocks) |

---

## [Unreleased — 0.5.0-dev6]

### STP overlay now actually conveys the spanning tree (not just the root)

`0.5.0-dev5` shipped the STP toggle but on Meraki-only sites it did
nothing beyond putting a gold border on the root bridge — there was
no per-port state, no membership context, no visual difference
between "in the spanning tree" and "off the spanning tree."  This
release lifts the STP topology out of the noise on every site.

Three changes combine:

**1. STP membership is projected onto every Device node.**
A new correlator pass
(`correlate._decorate_devices_with_stp_membership`) reads the
`STP_ROOT` / `STP_MEMBER` edges that already exist (16 + 53 in this
fleet) and stamps onto each participating Device:

  * `stp_is_root` (bool)
  * `stp_priority` (int)
  * `stp_domain_id`, `stp_domain_name`
  * `stp_root_bridge_id`, `stp_root_bridge_name`

The pass also clears these fields on devices that have dropped out
of all STP domains, so stale badges never linger.  Devices in
multiple STP domains keep the lowest-priority (root-most) domain;
ties prefer the `STP_ROOT`-tagged device, then deterministic id.

**2. Standalone `STPDomain` nodes are hidden from the topology API.**
`netcortex/graph/query.py` adds `NodeType.STP_DOMAIN` to
`_HIDDEN_NODE_TYPES_DEFAULT` (alongside `Interface`, `MACAddress`,
`ARPEntry`).  Every fact those nodes carried is now on the Device
nodes themselves, so the standalone red dot at every site
disappears.  STPDomain nodes still exist in Neo4j for query-layer
work (per-domain analytics, debug); they're just suppressed from
the visual topology.

**3. The STP toggle gains four new classes of styling.**
`netcortex/status/templates/index.html::applyStpOverlay` now stamps:

  * `node[type="Device"].stp-root`     — gold border + "★ ROOT (pri N)" label
  * `node[type="Device"].stp-member`   — softer yellow ring + "↳ root: <name>" label
  * `edge[type="PHYSICAL_LINK"].stp.backbone` — switch↔switch cable
    where both endpoints share an `stp_domain_id`; rendered heavy
    gold so the active spanning tree pops out even when SNMP didn't
    give us per-port state
  * `edge[type="PHYSICAL_LINK"].stp.no-stp`  — cable whose neither
    end is in any STP domain; faded to opacity 0.15
  * `.stp-dimmed`                      — every other node (phones,
    APs, MX firewalls, VLAN diamonds, prefix nodes); faded to 0.15
    so the eye stays on the spanning tree

All five rules are pure restyle: toggling the button removes the
classes inside a single `cy.batch()` block and the original styling
returns instantly.  No fetch, no layout pass, positions stay
stable.

After one correlation tick on the live fleet, the new pass
decorated ~70 Devices with STP membership and turned `STP cpn-ful`,
`STP cpn-ful-fabric`, `STP cpn-johnpar2`, etc. into clearly-visible
spanning trees with a gold root and softer-ringed members.

### Floating VLAN 1 nodes are now attached to their site devices

The cpn-arlington and cpn-jbonvici screenshots both showed a
`VLAN 1 · Infrastructure / 100.101.x.0/24` diamond sitting in the
site bubble with **zero edges to anything**.  The VLAN was being
created by the NetBox sync (correctly), but no adapter was ever
writing the device→VLAN membership edge for it: Meraki creates its
own per-org VLAN nodes that never join the NetBox ones; the SNMP
adapter only knows port-level VLANs on switches (not on phones,
APs, MX firewalls).

A new correlator pass
(`correlate._attach_devices_to_site_vlans`) closes the gap by
joining on the `netbox_site_slug` property both sides already
carry: for every Device d and canonical NetBox VLAN v (id starts
with `vlan:nb:`) where the slugs match and no `LOGICAL_MEMBER`
edge already exists, it MERGEs the edge tagged with
`source='correlator'` and `inferred_via='site_slug'`.  Real
per-port or per-SSID membership emitted by an adapter always wins
during dedup; this is the site-default fallback.

Verified on `vlan:nb:cpn-arlington:1`: 65 NetBox VLANs across the
fleet were unconnected before; after the pass runs on the next
correlation tick, every Device at every site (37 sites in this
fleet) will pick up `LOGICAL_MEMBER` edges to that site's NetBox
VLANs.

### Files touched

| Change                              | File                                                        |
| ----------------------------------- | ----------------------------------------------------------- |
| New correlator passes               | `netcortex/graph/correlate.py` (`_decorate_devices_with_stp_membership`, `_attach_devices_to_site_vlans`) |
| Hide STPDomain                      | `netcortex/graph/query.py` (`_HIDDEN_NODE_TYPES_DEFAULT`)   |
| Richer toggle + new style classes   | `netcortex/status/templates/index.html` (`applyStpOverlay`, 5 new selectors) |

---

## [Unreleased — 0.5.0-dev5]

### Visual STP overlay (toolbar toggle) + the bug that was silently hiding all STP data

A new **STP** toggle in the topology toolbar (sits next to *Ports* and
*Groups*) repaints the current view as a spanning-tree diagram —
**without re-fetching the graph or re-running the layout**.  Node
positions stay stable, you can flip it on and off instantly, and the
state persists across reloads.

When ON:

* **PHYSICAL_LINK cables are recolored from per-side STP state**
  (`stp_state_a` / `stp_state_b` populated by
  `_decorate_physical_links_stp`).  The *worst* of the two ends wins,
  so a blocked port turns the whole cable red even if the other end
  reports forwarding.
* **Blocked ports are dashed**, so redundant-but-inactive backups are
  obvious without reading the color.
* **Root ports are heavier**, letting you trace the active path back
  to the root bridge at a glance.
* **Root bridges get a gold border** (`node.stp-root`) — Devices with
  an outgoing `STP_ROOT` edge in the current payload are tagged in
  `applyStpOverlay()`.
* **Cables with no STP data on either side are dimmed** (`.no-stp`)
  so the eye gravitates to the parts of the graph the overlay can
  actually speak about.

When OFF: every added class is removed in a single `cy.batch()` block
and the default health-score coloring takes over.  No fetch, no
layout pass.

### The orphan-Interface bug that was hiding all decoration data

While wiring the toggle we found why the existing
`_decorate_physical_links_stp` correlator was matching zero edges
across the entire fleet despite 47 `STP_LINK` edges being present in
Neo4j: the SNMP STP / CAM / ARP / IP-address polls keyed their
Interface nodes as `snmp-if:<sess.host>:<ifname>` (the SNMP session
host IP), while the canonical counter poll keys them as
`snmp-if:<dev_node_id>:<ifname>`.  The two parallel Interface trees
never joined, so:

```
(d:Device)-[:HAS_INTERFACE]->(i:Interface)-[:STP_LINK]->(:STPDomain)
```

was always empty — the Interface nodes created by the STP walk were
orphan nodes that no Device pointed at, so the decoration JOIN
silently produced zero matches.  Verified live:

```
PHYSICAL_LINK STP decoration coverage: total=171 with_state_a=0 with_state_b=0
```

Even though the JOIN itself was correct.

Fixed `netcortex/adapters/snmp.py` so all four secondary polls
(CAM at line 1601, ARP at 1673, STP at 1853, IP-addr at 2749) key
Interface nodes by `dev_node_id` exactly the way the main counter
poll does.  The STP poll also emits a defensive `HAS_INTERFACE` edge
in case the counter poll skipped that port (management-only switches
or first-cycle race conditions).

A new one-shot housekeeping step
(`netcortex/worker.py::_housekeeping_loop`) detach-deletes legacy
`snmp-if:<ipv4>:<ifname>` Interface nodes that have no incoming
`HAS_INTERFACE` edge, so the orphans seeded by older builds are
swept on the next housekeeping pass and the canonical-id Interface
nodes (created by the corrected polls) take their place naturally.

After one correlation cycle the toggle has real data to color
PHYSICAL_LINK edges with on every Cisco-SNMP capable site.  Meraki-
only sites (no per-port STP API) still get root-bridge highlighting
plus a "no STP data" dim on the cables — graceful degradation.

### Where the code lives

| Concern                              | File                                                        |
| ------------------------------------ | ----------------------------------------------------------- |
| Toolbar button                       | `netcortex/status/templates/index.html` (`#btn-stp`)        |
| Toggle state + persistence           | `netcortex/status/templates/index.html` (`stpOverlayOn`)    |
| Restyle entry points                 | `toggleStp()`, `applyStpOverlay()`, `paintStpButton()`      |
| Stylesheet rules                     | `edge[type="PHYSICAL_LINK"].stp`, `.stp.no-stp`, `node[type="Device"].stp-root` |
| SNMP poll key fix                    | `netcortex/adapters/snmp.py` (4 sites)                      |
| Orphan-Interface sweep               | `netcortex/worker.py::_housekeeping_loop`                   |
| Live decorator (already existed)     | `netcortex/graph/correlate.py::_decorate_physical_links_stp` |

---

## [Unreleased — 0.5.0-dev4]

### Closing the "flickering duplicate cable" between Meraki-onboarded switches

The flickering grey/green parallel lines operators saw on Meraki-onboarded
Catalyst pairs (notably `cpn-ful-cat9k1 ↔ cpn-ful-fabric-cat9k{3,4,5,6}`)
came from three separate bugs that all conspired to push duplicate
edges through the graph every 30–60 seconds.  All three are fixed in
this release.

**1. Ingest is now diff-based instead of purge-then-rewrite**

For every adapter cycle, the ingest path used to delete every edge
owned by the adapter for the rel types in the new payload, then
re-MERGE the freshly-discovered set.  Inside that gap the adapter's
edges did not exist in the graph, and any other query that ran in
that window saw the stale state.

`_correlate_via_mac` guards against duplicating LLDP/CDP with a
`NOT EXISTS { MATCH (switch)-[:PHYSICAL_LINK]-(endpoint)
WHERE ex.discovery_proto IN [...] }` predicate.  If the LLDP edges
were momentarily absent (because the Meraki adapter was in the middle
of its ingest), the predicate evaluated to TRUE and the correlator
MERGEd a spurious `mac_correlation` PHYSICAL_LINK.  Then the adapter
finished re-inserting its edges, `_dedupe_physical_links_by_pair`
noticed the collision, dropped the inferred edge — and the next
ingest cycle started the loop over.

`netcortex/graph/ingest.py` now MERGEs the new payload first and
then diff-purges the per-adapter tail: edges tagged with the adapter
that are NOT in the payload's identity set get deleted.  The identity
key is `(source_id, target_id)` for normal rel types and
`(source_id, target_id, interface_a, interface_b)` for multi-edge
types so parallel cables each survive on their own identity.  Net
effect: an adapter's already-known edges are observable to other
queries at every instant.  The `graph.ingest_done` log line gains
`edges_purged_stale` so the diff-churn rate is visible per cycle
(steady-state is usually 0).

**2. Meraki per-device LLDP walk: deterministic order + direction-agnostic dedup**

For Meraki-to-Meraki cables the per-device LLDP walk used to fire
TWICE — once from each switch's perspective — and each side named
its own port using the Meraki "Port N" convention while the far
port came back through LLDP in whatever IOS-style label the neighbor
advertised.  The two reports ended up MERGEd as DIFFERENT
`(interface_a, interface_b)` keys (e.g. `('Port 1','Te1/0/24')` vs
`('Gig1/0/1','Port 24')`), and the dedup pass's interface-label
union-find could not collapse them because the two label sets
shared no characters.

`netcortex/adapters/meraki.py` now:
- sorts `lldp_results` by serial so the SAME side always wins the
  emission race cycle after cycle, giving the resulting edge a
  stable `(src, dst, ia, ib)` identity for the new ingest diff-purge
  to recognise; and
- uses a DIRECTION-AGNOSTIC existence check (`{src,dst}` as a frozen
  set) so the second walk to touch any Meraki-to-Meraki pair skips
  its emission entirely rather than producing the reversed-direction
  duplicate.

Parallel cables within a single walk still survive (one entry per
local port).  Asymmetric LAGs where each end reports DIFFERENT
cables to the same neighbor are an edge case we accept losing visibility
on for now; the Meraki org topology endpoint covers them once it
returns data for the org.

**3. Dedup Rule 4 union-find now normalises interface labels**

`_dedupe_physical_links_by_pair` Rule 4 walks the union-find over
the raw `interface_a`/`interface_b` strings stored on each edge.
The post-dedup normalisation pass then rewrites those strings to
canonical long form, which means the NEXT dedup pass would see the
canonical labels — but in the meantime the DB can hold a mix of
`Te1/0/24` (just MERGEd) and `TenGigabitEthernet1/0/24` (previously
normalised), and the raw strings share no characters so union-find
treats them as different cables.

Rule 4 now calls `normalize_ifname()` when adding each label to the
union-find map, so `Te1/0/24` and `TenGigabitEthernet1/0/24` (and
`Gi1/0/1` vs `GigabitEthernet1/0/1`, etc.) glue their edges into
the same component on the first dedup pass after a name variant
appears.  The `shared_iface_dropped` counter on the
`correlate.physical_links_deduped` log line now reports the resulting
drops (seen at e.g. 31 per cycle in steady state).

### Topology → Explorer deep-link

The detail panel that opens when you click a node in the topology view
now offers two one-click jumps into the per-device Explorer:

1. The device name in the panel header is rendered as a hyperlink for
   Device-type nodes — clicking it switches to the Explorer tab and
   loads that device's full inventory (interfaces, VLANs, MACs, ARP,
   routing peers, etc.) without forcing the operator to retype the
   name into the Explorer search box.
2. A prominent "Open in Explorer →" button appears at the top of the
   panel body for the same reason, for users who prefer button targets
   over linkified titles.

Both routes call a new shared helper ``openInExplorer(key)`` that
populates the Explorer search input, switches the view, and triggers
the fetch — the existing ``/api/devices/{device_key:path}/explorer``
endpoint accepts both the canonical Device id (e.g.
``meraki:Q3LA-6ZCD-UVY3``) and the short hostname.

### MS switch VLAN flood — fixed at ingest, not at render time

Once direct SNMP started working for Meraki MS switches in 0.5.0-dev2,
the topology view exploded into a ~600-node starburst of VLAN diamonds.
Every MS exposes its FULL VLAN database via SNMP (the Meraki cloud
config pushes the entire org-wide VLAN list to every switch in a
network), and the SNMP adapter was faithfully emitting one ``VLAN``
node + one ``LOGICAL_MEMBER`` edge per VID per device.  That's fine
for a Catalyst — its VLAN database really IS per-device state — but
on Meraki it just duplicates the org-level config N times over.

Two new ingest-time behaviours:

1. ``netcortex/adapters/snmp.py`` — when polling Meraki hardware
   (anything whose model starts with MR/MS/MX/MV/MG/MT/CW/Z), the
   adapter walks the same VLAN MIB as before via a new
   ``_poll_vlans_summary(sess)`` helper but returns just a sorted
   ``list[int]`` instead of a ``GraphData``.  The Cypher writer
   stamps the result directly onto the Device node as
   ``vlans_configured`` / ``vlan_count`` and marks
   ``vlans_source='snmp_meraki'`` so housekeeping knows the source.
   For Catalyst, Nexus, and every other platform the old
   ``_poll_vlans`` (node + edge emitter) is unchanged.
2. ``netcortex/worker.py`` housekeeping — the per-device VLAN
   denormalization that aggregates ``LOGICAL_MEMBER`` → VLAN vids
   now also unions in the existing ``vlans_configured`` value when
   ``vlans_source='snmp_meraki'``.  Without this carry-over the
   first housekeeping cycle after an SNMP poll would blank the
   property back to the empty LOGICAL_MEMBER aggregate.

The Explorer view still shows the full VLAN footprint per device
(reading from ``vlans_configured`` on the Device node).  Cable-level
trunk annotations on PHYSICAL_LINK edges (``vlans_carried``,
``native_vlan``) are unaffected — they come from per-interface
``vlans_allowed`` bitmaps, not the device-level VLAN database.

Existing stale ``snmp-vlan:`` nodes from previous Meraki MS polls
become orphans on the next adapter cycle (the SNMP adapter's per-
relationship-type purge wipes their edges) and are deleted by the
existing orphan-VLAN housekeeping pass on the next 10-min cycle.

---

## [0.5.0-dev2]

### Direct SNMP polling now works for Meraki MX/MS/MR/CW devices

For months the SNMP adapter could only reach Meraki hardware through
the org-level **cloud** endpoint (`{orgId}.snmp.meraki.com:16100`),
which exposes nothing but the proprietary `devTable` MIB. Every MX,
MS, MR, and CW in the inventory ended up flagged `snmp_health =
"cloud_only"` — no `ifTable`, no LLDP, no per-port VLAN bitmaps, no
ARP/CAM/STP context — even though the devices themselves are perfectly
willing to respond to direct SNMP queries on their management IP.

Root cause: a **credential model mismatch** between AWS Secrets
Manager and the Meraki Dashboard.

Meraki's per-network direct SNMP (`Network > General > SNMP`) is a
deliberately stripped-down v3 implementation:

* Single passphrase used for **both** authentication and privacy
* Hardcoded `SHA-1` (auth) + `DES` (priv) — no AES, no SHA-256
* One user per network, defined live in the dashboard

The `snmp/adapter/meraki_device` secret was modelled on a normal v3
agent (separate auth/priv passwords, `SHA-256` + `AES`), so every
direct poll authenticated against the wrong key. The MX would happily
discover the v3 engine, then reject the HMAC and we'd give up — falling
back to the cloud-only path.

The fix moves the credential lookup to **where the source of truth
lives**:

1. `_fetch_meraki_network_snmp_creds(backend)` — new method on
   `_SnmpAdapter` that, once per discover() cycle, walks every Meraki
   org/network and pulls `GET /networks/{netId}/snmp` for each. The
   returned map is keyed by `(instance_name, network_id)`. Bounded
   concurrency (5 req/s per org) keeps us well under Meraki's
   rate limit even for organisations with thousands of networks.
2. `_get_graph_targets` now also returns `d.networkId`, propagating it
   into each target dict so the resolver can find the right entry.
3. `_resolve_device_creds` checks the Meraki network-creds map first
   for any target whose `source_adapter` is `meraki/*` and whose model
   resolves to actual Meraki hardware. When a match is found it builds
   `SnmpV3Creds(username=<live>, auth_protocol="SHA",
   auth_password=<passphrase>, priv_protocol="DES",
   priv_password=<passphrase>)` — exactly what the device expects. The
   secret-backend fallback is preserved untouched for everything else.

Two cosmetic improvements come along for free:

* **CW models added to `_MERAKI_HW_PREFIXES`** in
  `netcortex/snmp/credentials.py`. The Catalyst Wireless 9160/9162/etc.
  APs onboarded via Meraki are managed under the same per-network
  SNMP model as the rest of the Meraki fleet, so they need to be
  classified as Meraki hardware for the resolver path.
* **Passphrase rotation is now automatic.** Operators who rotate the
  Meraki Network SNMP passphrase via the dashboard no longer need to
  also update AWS Secrets Manager — the SNMP adapter picks up the new
  value on the next discover() cycle.

Result: every Meraki appliance, switch, and access point that's
reachable on its management IP will now show `snmp_direct = true` and
`snmp_health = "full"` or `"direct+cloud"`, with the full LLDP/ARP/CAM/
per-port-VLAN MIB walk available.

---

## [0.5.0-dev1]

### Meraki MX mgmt_ip rule — enforced strictly at emit time

The operator-mandated rule for Meraki MX `mgmt_ip` has been in
the adapter since dev16, but the **enforcement was lopsided**: the
adapter applied it strictly at emit time, while the housekeeping
"repair" pass used a generic `candidate_ips[0]` heal. Two problems
fell out of that gap:

1. **Transit-only SD-WAN MXs emitted with empty `mgmt_ip`.** Edge
   gateways like `cpn-ash-sdwan-mx1` and `cpn-ful-sdwan-mx1` have no
   LAN appliance VLANs and don't export an AutoVPN subnet (no
   `vpnIp`), so `primary_ip` resolved to `None`. The empty value sat
   in Neo4j until the 10-minute housekeeping pass salvaged it from
   `candidate_ips[0]` (which happened to be the WAN IP because no
   LAN/VPN data was available). The operator saw a "lost the IP
   again" state for up to 10 minutes after every cycle.

2. **The housekeeping heal didn't enforce the rule, just `candidate_ips[0]`.**
   For SD-WAN MXs where `vpn_ip` was known but `mgmt_ip` was empty
   from a stale write, the generic heal happened to pick the right
   value only because `candidate_ips` was ordered to put it first.
   Any future code path that wrote a different `candidate_ips[0]`
   would have silently corrupted `mgmt_ip` against the rule.

#### Fix

- **`netcortex/adapters/meraki.py`**: extended the SD-WAN branch
  with a third fallback tier. When `sorted_vlans` is empty AND
  `sdwan_ip` (`vpnIp`) is empty, accept `wan1Ip → wan2Ip`. Transit-
  only SD-WAN MXs now ship with a valid `mgmt_ip` on the first
  cycle — no housekeeping wait.

- **`netcortex/worker.py`**: split the mgmt_ip repair pass into two
  tiers. Tier 1 is **Meraki-aware**: for every `:Device` with
  `platform='meraki' AND role='firewall'`, re-apply the exact rule
  from currently-stored fields (`on_sdwan`, `vpn_ip`, `wan1_ip`,
  `wan2_ip`) — `vpn_ip > wan1 > wan2` when SD-WAN, `wan1 > wan2`
  when not. Tier 2 is the previous generic `candidate_ips[0]`
  heal, scoped explicitly to NON-Meraki-appliance devices so it
  never fights tier 1. Both are idempotent (only write when result
  differs from current) and rule-faithful — a corrupted MX
  `mgmt_ip` is now repaired strictly per the operator's rule on
  the very next housekeeping cycle.

#### Why this took multiple rounds to nail

The system was actually working *most of the time* — the
discovery + housekeeping pipeline converged to the correct value
within ~10 min of any disturbance. But the operator only sees the
canvas state at the moment they look, so a transient empty
`mgmt_ip` mid-cycle reads as "you broke it again". This change
makes correctness **eventually-consistent on the first write**
instead of "eventually correct after housekeeping", removing the
visible window where the wrong value can be seen.

---

## [0.5.0] — 2026-05-18

First post-`0.1.0` release. Cuts the long `0.4.0-devN` in-flight cycle
(dev1 → dev20) into a single MINOR feature release. The detailed
per-dev change history follows below this section unchanged — read
top-down for the highlight reel, or dive into the per-dev sections
for the full reasoning behind each change.

### Highlights

- **Sharded SNMP poller pool** with per-host concurrency caps and a
  net-snmp subprocess transport (replaces deadlock-prone pysnmp 7.x).
- **Per-VLAN BRIDGE-MIB context walks** to recover trunk allow-lists
  when CISCO-VTP-MIB's `vlanTrunkPortVlansEnabled` is silent on
  IOS-XE.
- **Sibling-aware L2 decoration** for Cisco multi-rate ports — the
  correlator now picks the active speed-variant (e.g.
  `TwentyFiveGigE1/1/5` over its inactive `TenGigabitEthernet1/1/5`
  shadow) when stamping trunk/native facts on a PHYSICAL_LINK.
- **Per-device VLAN inventory** (`vlans_configured` + `vlan_count`)
  denormalized onto every Device, aggregated across every variant
  sharing the same name. Powers the new node-hover and link-hover
  VLAN summaries.
- **PHYSICAL_LINK tooltip restructured** around the operator's mental
  model — three blocks: per-side mode + allow-list, effective
  traversing VLAN set, asymmetries. On-canvas cable label now shows
  the traversing count instead of the widest side's allow-list.
- **New node hover tooltip** showing device VLAN inventory + identity
  fields without needing to open the detail panel.
- **Phase B-F scaling foundations**: decoupled ingest pipeline, link
  health (utilization, errors, single-sided), MIB-coverage probes,
  history-seam design doc.
- **Adapter overhauls**: Catalyst Center, Meraki (cloud SNMP via
  proprietary MIB walk, 308 redirect handling, prefix discovery),
  Nexus Dashboard.
- **Stub-purge correlator** with per-cycle housekeeping that GCs
  orphan stubs, mgmt-IP drift, and reverse-duplicate PHYSICAL_LINK
  edges.
- **MCP tool surface** for AI clients (see Phase A onward in the
  implementation journal).

### Per-dev change log

The `[Unreleased — 0.4.0-devN]` sections below capture each change as
it landed during the dev cycle. They are preserved verbatim so the
"why" behind each subsystem is recoverable; the dev numbering is
internal and corresponds to working-state increments only.

---

## [Folded into 0.5.0 — dev20]

### UI — VLAN footprint visualization (per-device + per-link)

- **Device VLAN inventory denormalized onto every Device node
  (dev20).** Worker housekeeping now stamps a sorted `vlans_configured`
  array (and matching `vlan_count`) on each `:Device` by aggregating
  `LOGICAL_MEMBER → VLAN` edges across *every* device-variant sharing
  the same `name`. This last bit matters because adapters fan out
  multiple Device nodes per physical box (one per discovery source —
  meraki:, snmp-if:, cdp-neighbor:, ndfc:, …) and only some of those
  variants ever receive the VLAN-membership edges. From the operator's
  perspective there's one `cpn-ful-cat9k1` with 9 VLANs, so every
  endpoint that resolves to a cat9k1 variant now sees the same VLAN
  footprint regardless of which adapter owns the link anchor. Sorting
  happens in Python (Neo4j 5 Community doesn't ship `apoc.coll.sort`
  and the pure-Cypher equivalent is painful); device counts are
  bounded so the extra round-trip is cheap.

- **Node hover tooltip now shows VLAN inventory.** Hovering a Device
  on the canvas pops a compact summary box: name, type/platform,
  mgmt-IP, and — when applicable — *N VLANs configured: 1, 10-16, 80*
  (range-compressed with `_formatVlanList`). This finally answers the
  question "what VLANs does this switch carry?" without having to
  click into the detail panel and scroll through `:HAS_VLAN`
  relationships. Non-Device nodes (VLAN, Prefix, …) get a lightweight
  type+ID summary so the operator can identify them at a glance too.

- **PHYSICAL_LINK tooltip restructured around an operator's mental
  model.** The L2/VLAN section is now three semantic blocks instead of
  two ad-hoc rows:

  1. **Mode** — per side: `trunk · native 1 · 9 cfg · allowed 9: 1,
     10-16, 80`. Includes the device's *total* VLAN count (`9 cfg`)
     alongside the trunk allow-list, so the operator instantly sees
     "this port allows everything the box has" vs. "this trunk is
     pruned".
  2. **Traversing the link** — the *effective* set of VLANs actually
     crossing the wire, computed via the operator's mental model:
     `(allowed_a ∩ configured_a) ∩ (allowed_b ∩ configured_b)`. A VLAN
     traverses iff both ends permit it on the trunk *and* both ends
     have it in their VLAN database. The native VLAN is annotated as
     `(N untagged)` so 802.1Q context is preserved. When the trunk
     allow-list is silent on one side (the common IOS-XE
     `vlanTrunkPortVlansEnabled` gap) we fall back to that side's
     device inventory as the upper bound, so the operator still gets
     a useful answer pre-MIB-fix.
  3. **Asymmetries** — VLANs sent by one side but dropped by the peer
     because the peer doesn't have them configured. Surfaced as `⚠ N
     on cpn-ful-n9k1 only (dropped by cpn-ful-cat9k1): 2000-2019` —
     immediate visual cue for pruning candidates or VLAN-DB
     misconfigs.

- **On-canvas link label now reflects the traversing set, not the
  widest side.** Previously the label read "511 VLANs (native 1)"
  because it picked whichever side had the longer allow-list — that
  over-stated the on-wire reality whenever the peer was missing VLANs
  or its trunk-list was truncated. New label: `9 VLANs (native 1)` —
  the actual cross-wire count derived from
  `_computeTraversingVlans()`. Falls back to `N VLAN(s) allowed ·
  native N` when no device inventory is available on either end (e.g.
  Meraki ↔ unknown).

- **New shared helpers** `_computeTraversingVlans()` and
  `_computeVlanFlow()` keep the on-canvas label and the hover tooltip
  in algorithmic lockstep — no more drift between the cable
  annotation and what the popup says.

#### Operator workflow

1. Hover a Device → see its full VLAN inventory.
2. Hover a PHYSICAL_LINK → see per-side mode + allow-list, then the
   effective traversing set, then any asymmetries.
3. Glance at the cable label → see the traversing VLAN count at a
   distance without hovering.

#### Why this matters

The previous popup showed `cpn-ful-cat9k1: trunk · native 1 · (no
allowed-VLAN list reported)` next to `cpn-ful-n9k1: trunk · native 1
· 511 VLANs: 1-127, …` — technically accurate but operationally
useless, because it left the operator unable to answer the actual
question ("how many VLANs are reaching cat9k1?"). dev20 turns that
into `Traversing: 9 VLANs · 1, 10-16, 80` plus a `⚠ 18 on cpn-ful-n9k1
only` flag — the same insight an engineer would derive by mentally
intersecting `show vlan brief` outputs from both boxes.

---

## [Unreleased — 0.4.0-dev19]

### SNMP adapter — per-VLAN BRIDGE-MIB context walks

- **Per-port carried-VLAN sets now derive from per-VLAN STP context
  walks when CISCO-VTP-MIB's `vlanTrunkPortVlansEnabled` is silent
  (dev19).** Observed in the wild: cat9300/cat9400 17.x IOS-XE with
  VTP transparent answers `vlanTrunkPortDynamicStatus` (`.14`) and
  `vlanTrunkPortNativeVlan` (`.5`) for every port but withholds all
  four `vlanTrunkPortVlansEnabled[2k|3k|4k]` (`.4 / .17 / .18 / .19`)
  bitmap columns — so we knew a port was trunking and what its
  native VLAN was, but the actual allowed-VLAN list came back empty.
  The single-context BRIDGE-MIB walk we already did for STP only sees
  the CST / VLAN-1 instance on Rapid-PVST+ devices, so STP couldn't
  fill the gap either.

  New `_poll_per_vlan_stp` recovers the data via per-VLAN-context
  BRIDGE-MIB walks — the same data path Cisco IOS uses internally to
  render `show spanning-tree interface X`:

    1. Walks `vtpVlanState` once to enumerate operational VLAN IDs
       (state=1, excluding reserved IDs 1002-1005).
    2. For each VLAN N, in SNMPv3 `contextName=vlan-<N>` (or v2c
       `<community>@vlan-<N>`), walks `dot1dStpPortState` and
       `dot1dBasePortIfIndex` — capped at 8 parallel context walks
       per device and 256 VLANs per device.
    3. Per ifIndex, unions the VLANs where state ≠ `disabled` into
       `vlans_stp_member` and the subset where state=`forwarding`
       into `vlans_stp_forwarding`.
    4. When a trunk port has no `vlans_allowed` (CISCO-VTP-MIB
       bitmap silent), promotes `vlans_stp_member` to `vlans_allowed`
       and stamps `vlans_allowed_source='stp_per_vlan_context'` so
       downstream code knows it's a derived list, not the
       authoritative configured-allowed bitmap.

  Existing `vlans_allowed` from the CISCO-VTP-MIB bitmap is never
  overwritten — it's the configured source-of-truth and may include
  pruned-but-permitted VLANs the STP table doesn't show.

- **Requires per-VLAN SNMP context indexing on the device.** Cisco
  IOS-XE / NX-OS only exposes per-VLAN BRIDGE-MIB views when the
  SNMP group is configured with `context vlan- match prefix`. When
  every per-VLAN walk on a device comes back empty we log a single
  `snmp.per_vlan_stp.context_not_configured` warning with the exact
  command to add:

      snmp-server group <grp> v3 priv read <view> context vlan- match prefix

- **`_SnmpSession.walk()` and the underlying v2c/v3 walk helpers
  now accept `context_name`.** For SNMPv3 this maps to net-snmp's
  `-n <context>` flag; for v2c it rewrites the community string to
  `<community>@<context>` (Cisco's documented per-VLAN community
  indexing trick). Defaults to `None` so existing callers are
  unaffected.

- **MIB-coverage probe split: `trunk_port` vs `trunk_port_allowed`.**
  Previously the `trunk_port` family probed `vlanTrunkPortNativeVlan`
  (`.5`) and we'd happily report `ok` even when the allowed-VLAN
  bitmap was silent. Now `trunk_port` keeps probing `.5` (native /
  status) and a new `trunk_port_allowed` family probes `.4` (the
  allowed-VLAN bitmap). This lets the per-device SNMP-coverage badge
  on the inventory page surface the bitmap gap accurately so the
  operator sees which devices are relying on the per-VLAN STP
  fallback.

## [0.4.0-dev18]

### Correlator — sibling-aware L2 decoration

- **PHYSICAL_LINK now reflects the active speed-variant of a
  multi-rate port instead of the inactive shadow (dev18).** Cisco
  multi-rate SFP cages (e.g. 10G/25G on cat9k 1/1/5) expose two
  IF-MIB rows — `TenGigabitEthernet1/1/5` and `TwentyFiveGigE1/1/5`
  — for the same physical port. Only one is the active variant at a
  time; the other is an inactive shadow whose SNMP `vmVlan`
  default-reads as `access vlan 1` (the documented MIB default for
  any port not in an access VLAN, including the shadow of a
  multi-rate port). LLDP/CDP can advertise either variant in the
  connected-port TLV depending on negotiated speed, so the
  PHYSICAL_LINK ended up anchored to whichever variant the neighbor
  reported — sometimes the dead shadow.

  Concretely on the `cpn-ful-n9k1 ↔ cpn-ful-cat9k1` cable: the
  cat9k1 side anchored to `TenGigabitEthernet1/1/5` (oper=down,
  trunk_mode='access', vlans_access=1) while the actual active port
  is `TwentyFiveGigE1/1/5` (oper=up, trunk_mode='trunk', native=1,
  per the user's `show spanning-tree interface Twe1/1/5` output
  showing VLANs 1, 10-16, 80 forwarding). The link tooltip therefore
  showed "access · access 1 · 1 VLAN: 1" on the cat9k1 side — wrong
  by 8 VLANs.

  The correlator now resolves each side by:

    1. Building a per-device inventory of L2-bearing Interface nodes
       in one round-trip query.
    2. For each link side, collecting all Interface nodes on that
       device whose canonical name matches the link's anchor OR
       whose numeric port-tail (`1/1/5` is the tail of both
       `TenGigabitEthernet1/1/5` and `TwentyFiveGigE1/1/5`) matches
       the anchor's tail.
    3. Scoring candidates with `_l2_rank()` — trunk-with-allowed-list
       beats trunk-no-list beats access-VLAN-≠-1 beats
       access-VLAN-=-1 (the suspicious default).
    4. Picking the highest-ranked candidate and stamping its L2
       facts onto the link.

  When the resolved active sibling differs from the link anchor,
  the correlator records the active variant's name on
  `interface_a_active` / `interface_b_active`. The link's
  `interface_a` / `interface_b` are NOT rewritten (the next ingest
  would MERGE on those keys and create a duplicate cable).

### UI — edge tooltip

- **Active sibling surfaced on the endpoint line (dev18).** When
  the link is anchored to a multi-rate shadow but the correlator
  resolved a different active sibling, the tooltip now shows the
  active variant prominently and the link anchor as a small grey
  footnote: `TwentyFiveGigE1/1/5 (link anchor:
  TenGigabitEthernet1/1/5)`. This makes the
  topology-join-key-vs-actual-port distinction explicit without
  losing either piece of information.

## [0.4.0-dev17]

### UI — edge tooltip

- **Side labels now use the device name, not "Side A / Side B"
  (dev17).** Previously the operator had to know which end of the
  cable was canonically A (the lex-smaller node id) to read the
  tooltip. The tooltip now renders the per-side rows with the actual
  device name pulled live from the Cytoscape graph, e.g.
  `cpn-ful-cat9k1: access · access 1 · 1 VLAN: 1` and
  `cpn-ful-n9k1: trunk · native 1 · 511 VLANs: 1-127, 1024-1151, …`.
  The endpoint line at the top of the tooltip also gained an
  `<srcDev> ⇄ <tgtDev>` summary.

- **L2-consistency check no longer false-flags access-vs-trunk
  cables where VLAN 1 passes untagged on both ends (dev17).** The
  previous strict check (`trunk_mode_a == trunk_mode_b AND
  native_vlan_a == native_vlan_b`) raised the amber "⚠ Sides
  disagree" warning on every cable where one side was a trunk and
  the other was reported by SNMP as access-VLAN-1 — even when the
  cat9k1 side actually has no `switchport` line at all (a routed
  L3 port). Cisco's `vmVlan` MIB returns 1 for any port not in an
  access VLAN, so our adapter reads "access vlan 1" for routed
  ports. Operationally that cable still passes VLAN 1 untagged on
  both ends, identical to the trunk's `native 1`. The new check
  only fires on true misconfigs:

    * `trunk` ↔ `trunk` with different native VLANs, OR
    * `access(N)` ↔ `access(M)` with N ≠ M, OR
    * mixed `access(N)` ↔ `trunk(native=M)` where N ≠ M.

  Mixed `access(N)` ↔ `trunk(native=N)` and cables where one side
  has no trunk_mode info are no longer flagged. The intersection
  row was also relabeled from "Effective" to "Common" to better
  describe what it represents (VLANs both ends agree on).

---

## [Unreleased — 0.4.0-dev16]

### Meraki adapter

- **MX primary IP rule re-asserted and made non-clobbering (dev16).**
  The `mgmt_ip` for Meraki MX (and Z-series) appliances regressed: 36
  out of 55 MXs in the live deployment had `mgmt_ip=""` in Neo4j even
  though their `candidate_ips` array was populated. Root cause was a
  failed-discovery clobber: when the Meraki Dashboard API rate-limited
  or transient-failed, `list_devices()` returned an MX with no WAN /
  no VLAN data, the adapter computed `mgmt_ip = candidate_ips[0]` over
  an empty list (→ `None`), and `discover()` then emitted `mgmt_ip=""`
  into the GraphData. Ingest's `SET n += row` happily overwrote the
  previously-good IP with the empty string.

  Fixes:

  1. **Strict primary-IP rule** in `MerakiAdapter.list_devices`:
     - SD-WAN MX → primary MUST be the **applianceIp of the
       lowest-numbered appliance VLAN**. Only fall back to Meraki's
       derived `vpnIp` (first host of the first exported AutoVPN
       subnet — equivalent to the lowest-VLAN applianceIp) when the
       appliance-VLAN fetch was rate-limited.
     - Non-SD-WAN MX → primary MUST be a **WAN port IP** (`wan1Ip`
       then `wan2Ip`). Public/NAT addresses are tracked as
       informational candidates but are NEVER promoted to `mgmt_ip`
       (they belong to the carrier, not the device). LAN SVI IPs are
       likewise never primary on a non-SD-WAN MX — they were creeping
       in via the old append-everything candidate list.
  2. **Non-clobbering emission** in `MerakiAdapter.discover`: any
     IP-bearing field (`mgmt_ip`, `candidate_ips`, `wan1_ip`,
     `wan2_ip`, `wan1_public_ip`, `wan2_public_ip`, `vpn_ip`) whose
     value is empty / None is now **omitted entirely** from the emitted
     Device node's properties. Since ingest uses `SET n += row`, omitted
     keys preserve the existing Neo4j value instead of being silently
     overwritten with `""`. Static "shape" fields (name / role /
     platform / serial) are always emitted because they're always
     known.
  3. **Worker direct heal** (`netcortex/worker.py`): the housekeeping
     pass that previously cleared `_content_hash` on MXs with
     `mgmt_ip=""` (forcing the next ingest to rewrite) now also
     directly sets `d.mgmt_ip = d.candidate_ips[0]` in-place. The repair
     no longer waits a discovery cycle; corrupt rows are healed every
     5 minutes regardless of adapter health.

### Graph correlator

- **Per-side carried-VLAN sets stamped on PHYSICAL_LINK
  (`_decorate_physical_links_l2`, dev16).** The single `vlans_carried`
  property is the conservative intersection of both sides' VLAN sets,
  which is correct when both sides report cleanly — but masks the
  common case where SNMP misreads one end as access-VLAN-1 while the
  other end is a 500-VLAN trunk (observed on the cpn-ful-n9k1 ↔
  cpn-ful-cat9k1 cable). The correlator now additionally stamps
  `vlans_carried_a` and `vlans_carried_b` so each side's independent
  set is preserved on the edge and the UI can render the asymmetry
  for the operator to diagnose.

- **Robust interface-join when multiple Interface nodes share a name
  (dev16).** SNMP on cat9k can produce two `Interface` nodes for the
  same physical port (`TenGigabitEthernet1/1/5` appears once with
  `trunk_mode='access' vlans_access=1` and once with both fields
  NULL — likely a shadow row from an older speed mode). The
  correlator's `OPTIONAL MATCH` previously picked one
  non-deterministically and a NULL-shadow win silently erased the
  link's trunk facts. We now `collect()` all candidates and prefer
  the one with `trunk_mode IS NOT NULL`, falling back to a NULL row
  only when nothing better exists. The dual-interface root cause
  (SNMP harvest emitting two rows for one port) is filed for
  follow-up.

### UI

- **Edge hover tooltip now surfaces L2 / VLAN context (dev16).** The
  PHYSICAL_LINK tooltip gained an "L2 / VLAN" section that lists
  per-side `trunk_mode`, `native_vlan`, `vlans_access`, and the full
  per-side carried-VLAN set — range-compressed so a 500-VLAN trunk
  renders as `1-127, 1024-1151, 2048-2175, 3072-3199` instead of a
  500-element comma list. When the two sides disagree, an "Effective"
  line shows the conservative intersection and an amber "⚠ Sides
  disagree" warning is rendered. New helper `_formatVlanList()` does
  the range compression.

- **Cable label now reflects the widest reported VLAN set, not the
  intersection (dev16).** The on-canvas link label used to read
  `VLAN 1` for any cable whose intersection was a single VLAN, even
  when one side was reporting a 500-VLAN trunk. It now picks the
  larger of the two per-side sets and appends a `⚠` marker when the
  sides disagree so the operator immediately sees that the cable
  actually carries the wider VLAN footprint (the full per-side
  breakdown is one hover away).

---

## [Unreleased — 0.4.0-dev15]

### UI

- **Overlay toggles are now a pure client-side display filter — no
  refetch, no relayout, no flicker (dev15).** Every overlay toggle
  (Physical / VLANs / Routing peers / SD-WAN / Fabric / Virtual) used
  to refire `/api/graph` with a different `?overlay=…` set, which
  destroyed the Cytoscape instance and ran fCoSE from scratch. The
  result was a full-graph reflow on every click, and — worse — the
  L3 overlay’s `rel_types` list still includes `PHYSICAL_LINK`, so
  toggling L3 off when L3 was the only active overlay collapsed
  every cable in the response (including the n9k1↔cat9k1 link the
  operator was inspecting).

  The detail-mode loader now fetches the **union of all overlays in
  a single call** (`?overlay=physical&overlay=l2&overlay=l3&…` with
  `collapse_l3_on_physical=true`), and `toggleOverlay()` is rewired
  to call a new `applyOverlayVisibility()` helper that flips
  `display: 'element' | 'none'` on each Cytoscape element based on a
  static `NODE_OVERLAY` / `EDGE_OVERLAY` membership map. Devices,
  sites, container nodes and PHYSICAL_LINK cables are baseline
  (always shown); only overlay-tagged types (VLAN, Prefix,
  RoutingPeer, VNI, SDWANTunnel, LOGICAL_MEMBER, HAS_SVI, BGP_PEER,
  ROUTES_TO, etc.) participate in toggling. Cytoscape positions are
  preserved when an element is hidden, so re-enabling an overlay is
  instantaneous and the layout stays stable.

  Because PHYSICAL_LINK is now membership-tagged as `physical` only
  (regardless of the server overlay catalog), toggling L3 off can
  never strip cables from the canvas — the bug where the
  cpn-ful-n9k1 ↔ cpn-ful-cat9k1 physical link disappeared when L3
  was disabled is fixed.

  The orphan-edge sweep in `applyOverlayVisibility()` also hides any
  edge whose endpoint was just hidden (e.g. LOGICAL_MEMBER edges to
  a VLAN node that is now off), preventing half-floating edges.

- **Overlay button labels + tooltips clarified (dev15).** L2 is now
  labelled "VLANs" (it controls VLAN-node visibility, not STP — STP
  is link decoration since dev6) and L3 is labelled "Routing peers"
  (it controls the routing-peer cloud and Prefix nodes; routing
  protocols on cables remain visible as link annotations because
  they live on the PHYSICAL_LINK edge itself, not on a separate
  overlay edge). Each button now carries a one-sentence tooltip
  describing what it *uniquely* toggles, so operators no longer
  expect cables or vlans_carried decorations to disappear when the
  layer is turned off.

- **Overlay counts now reflect what's actually rendered (dev15).**
  The `nodes · edges` readout in the lower-left graph overlay now
  counts only elements whose `display` style is not `none`. Previously
  it always showed the full payload size, which was misleading once
  any overlay was hidden.

### Graph API

- **`get_full_graph` now guarantees the PHYSICAL_LINK spine survives
  the global LIMIT cap (dev15).** The single MATCH-all-edges query in
  `get_full_graph` splits its `LIMIT` budget across every requested
  rel type. When the union spans many overlays (e.g. the UI's new
  full-union fetch covering physical+l2+l3+sdwan+fabric+virtual),
  heavy edge classes such as `SDWAN_TUNNEL` and `STP_ROOT` could
  squeeze the `PHYSICAL_LINK` cable map out of the truncated result
  — observed: `PHYSICAL_LINK` count dropped from 102 → 62 → 0 as more
  overlays were unioned at `limit=1000`, and the cpn-ful-n9k1 ↔
  cpn-ful-cat9k1 cable went missing entirely in the union response.

  The query now performs a second unlimited `MATCH (src)-[r:PHYSICAL_LINK]->(dst)`
  pass when the main rel pattern includes `PHYSICAL_LINK` *and* asks
  for additional edge types, and backfills any cables the truncated
  main query missed (deduped by Neo4j-internal relationship id). The
  cable map is the topological spine every overlay rides on top of —
  it must never be truncated. The UI also bumps its detail-mode
  `limit` from `1000` to `10000` to keep the rest of the payload
  intact for medium fleets, but the backend safeguard means even a
  tight cap can no longer hide the spine.

---

## [Unreleased — 0.4.0-dev14]

Working version. Items below will be promoted to a numbered release
when the user requests a commit; the slot bumped (MINOR vs PATCH)
depends on whether new features or only bug fixes are in this block.

### Fixed

- **LLDP/CDP neighbor stubs silently strip a device's physical
  cabling (dev14).** `cpn-ful-n9k1` was visibly losing ~14 of its
  17 LLDP/CDP physical neighbors in the data explorer — and the
  visible set was flapping cycle-to-cycle. Three independent bugs
  conspired to cause the loss; all are fixed together.

  1. *`netbox_enrich` was stamping `canonical_id` on stubs without
     re-pointing their edges.* The NetBox enrichment pass — which
     uniquely owns the (graph-node → NetBox-device-by-name →
     NetBox-serial → real-Device-by-serial) chain — would mark an
     `lldp-neighbor:<name>` / `cdp-neighbor:<name>` stub with
     `canonical_id` pointing at the matching real Device (typically
     an Intersight FI named by serial, e.g. `FI-A-FCH2903782Y`),
     but never redirect the stub's PHYSICAL_LINK edges onto that
     canonical. The topology query then *hid* the stub AND
     *dropped* every PHYSICAL_LINK edge incident to it. Two fixes:
     a defensive sort-key forces stubs to the bottom of the
     canonical-pick (a stub can never WIN as canonical, even as a
     tiebreaker), and the new
     `_absorb_stubs_with_canonical_id` correlator pass is invoked
     immediately at the end of `enrich_devices_from_netbox` so
     stamp-and-absorb is a single atomic unit.

  2. *No self-healing pass for stranded stubs.* Stubs already
     stamped with `canonical_id` by the buggy enrichment above (or
     by any future pass that wants to mark-then-redirect) had no
     mechanism to be reabsorbed in subsequent correlator cycles.
     New pass `_absorb_stubs_with_canonical_id` runs alongside the
     other stub-merge passes: for every stub carrying
     `canonical_id` that points at an existing real Device, it
     redirects the stub's PHYSICAL_LINK edges onto the canonical
     and DETACH-deletes the stub via the shared
     `_absorb_stub_into_real` plumbing.

  3. *Absorbed cables were getting wiped by the next SNMP ingest.*
     When stub-merge re-pointed an edge it inherited the stub
     edge's `source_adapter = 'snmp/default'` via `SET nr += p`.
     The per-adapter purge in `ingest.ingest_graph_data` then
     deleted every `snmp/default` PHYSICAL_LINK at the START of
     the next SNMP cycle — including these promoted edges. The
     fix re-stamps the absorbed edge with
     `source_adapter = 'correlator'` (consistent with how SVI and
     HAS_PREFIX promotions already work in the same file) and
     preserves provenance in a new `original_source_adapter`
     property. Promoted cables are now idempotent across SNMP
     cycles: discovered once, promoted once, kept.

  4. *Absorbed stubs were being DETACH-deleted, losing the
     `(stub-id → canonical-id)` mapping.* Every SNMP cycle then
     re-MERGEd the stub (LLDP/CDP is the same neighbor info each
     cycle) and re-created the `(peer)→(stub)` edge with no
     canonical mapping in place — so canonical devices flashed
     "both the canonical edge AND a fresh duplicate stub edge"
     between absorptions. `_absorb_stub_into_real` now tombstones
     the stub (`canonical_id` set, `tombstoned = true`) instead of
     deleting it.  The topology query already hides any node with
     `canonical_id` set (see `query.py`: `canonical_map`), so the
     stub stays operator-invisible.  Next SNMP cycle re-MERGEs the
     stub but preserves its `canonical_id` (Cypher `+=` only writes
     keys present in the new payload), so the new edges absorb
     in-place via the existing `(interface_a, interface_b)` MERGE
     key — no transient duplicates ever escape the cycle they were
     created in.  The pool of tombstoned stubs is bounded by the
     number of distinct LLDP/CDP neighbor names ever observed.

  Steps 2 and 3 of `_merge_neighbor_stubs_by_name` were
  refactored to call the shared `_absorb_stub_into_real` helper
  so the name-based stub-merge path benefits from the same
  re-tag-and-tombstone behavior as the chassis_mac and mgmt_ip
  paths.

  The `_dedupe_physical_links_by_pair` pass keys on
  `discovery_proto` rather than `source_adapter`, so promoted
  edges still register as LLDP/CDP for priority resolution against
  inferred mac/arp correlations — no regression there.

- **VLANs falsely extending across NetBox sites (dev13).** The data
  explorer was rendering a single `vlan:nb:cpn-ful:1` node connected
  to devices in `cpn-ful`, `cpn-ash`, AND `cpn-nashville` — wildly
  misleading because this deployment runs no L2 extension technology
  (no VXLAN-EVPN, no OTV, no L2VPN). Three independent bugs all
  contributed and have been fixed together:

- **VLANs falsely extending across NetBox sites (dev13).** The data
  explorer was rendering a single `vlan:nb:cpn-ful:1` node connected
  to devices in `cpn-ful`, `cpn-ash`, AND `cpn-nashville` — wildly
  misleading because this deployment runs no L2 extension technology
  (no VXLAN-EVPN, no OTV, no L2VPN). Three independent bugs all
  contributed and have been fixed together:
  1. **Catalyst Center adapter** was emitting a single global
     `vlan:<vid>` node per VLAN id, collapsing VLAN 1 across every
     site CatC managed into one cross-site node. It now emits one
     `vlan:catc:<device-platform-id>:<vid>` node per (device, vid)
     pair so the correlator can scope each to the owning device's
     NetBox site. (`netcortex/adapters/catalyst_center.py`)
  2. **`_canonicalize_vlans_per_fabric` legacy migration** picked a
     single "chosen_slug" per legacy VLAN and re-pointed every member
     device to that one slug's canonical, smushing minority-slug
     members into the majority site. It now migrates **per (device,
     legacy-VLAN) pair**, with each device's own `netbox_site_slug`
     choosing its destination canonical. Outbound legacy edges
     (HAS_SVI / HAS_PREFIX / LOCATED_AT) are dropped because they're
     inherently un-scoped on the global form; the normal linkage
     pass rebuilds them per canonical. Orphaned legacy nodes are
     reaped. (`netcortex/graph/correlate.py`)
  3. **`_link_vlan_svis_and_prefixes` SVI + ROUTES_TO scopes** used
     `platform_site_ids` as the membership scope, which is unsafe
     when a single PlatformSite straddles NetBox sites (the
     Catalyst-Center "unassigned" bucket at
     `catc-site:unassigned:cpn-ful-catc1` contained devices from
     cpn-ful, cpn-ash, AND cpn-nashville). Both rules now require an
     exact `d.netbox_site_slug = v.netbox_site_slug` match for any
     NetBox-scoped (`vlan:nb:`) canonical and fall back to
     PlatformSite containment only for pre-NetBox legacy canonicals.
  4. A new per-cycle invariant-enforcement purge at the top of
     `_link_vlan_svis_and_prefixes` deletes any pre-existing
     `LOGICAL_MEMBER` or `HAS_SVI` edge on a `vlan:nb:` canonical
     that violates the per-site scope — so the graph self-heals on
     the very next correlation pass without requiring a wipe.

### Fixed

- **Duplicate prefix visualization.** Once a `Prefix` CIDR was folded
  into a VLAN's `prefix_v4`/`prefix_v6` label (or stamped onto a
  `PHYSICAL_LINK.l3_prefix`), the standalone `Prefix` node and its
  HAS_PREFIX / ROUTES_TO edges kept rendering anyway — e.g. cat9k1
  showed an orange `ROUTES_TO prefix:192.133.164.0/24` arc *alongside*
  the `VLAN 14 · cpn-ful-ai1 · 192.133.164.0/24` node it was already
  attached to. New correlator pass `_mark_absorbed_prefixes` stamps
  `Prefix.absorbed=true` (plus `absorbed_by_kind`/`absorbed_by_id`)
  when the CIDR is already visible elsewhere; the topology query then
  excludes those Prefix nodes and any edge incident to them. The
  stamp is cleared when neither absorber still applies, so a Prefix
  can reappear as a standalone node if its VLAN/link goes away.
  (`netcortex/graph/correlate.py`, `netcortex/graph/query.py`)
- **L3 link decoration missed SNMP-discovered prefixes.**
  `_decorate_physical_links_l3` only matched against `Prefix.cidr`
  (Meraki path), so cables whose endpoint subnets only existed as
  SNMP-derived `Prefix.prefix` were never tagged with `l3_prefix`.
  The lookup now `coalesce`s both fields, which also feeds the new
  absorption pass so those SNMP-only prefixes can be folded onto
  their cables instead of floating alongside them.
  (`netcortex/graph/correlate.py`)
- **Multiple "cpn-ful" containers in the topology.** The graph-query
  step that introduced NetBox-site containers (`nb-site:<slug>`) only
  re-parented *Device* nodes; the original PlatformSite containers
  (Meraki network, NDFC fabric, Intersight UCS domain) were still
  emitted as siblings, so users saw three or four boxes labelled
  "cpn-ful" / "Intersight CPN" sitting next to the NetBox-site
  container. `_topology` now computes, for each PlatformSite, the
  dominant NetBox slug across its enriched device children and:
  (A) when the PlatformSite *name* equals the NetBox-site *name*,
  hides the PlatformSite entirely and re-parents every child
  (including non-enriched CDP stubs and nested chassis) onto the
  NetBox site; (B) when the names differ, keeps the PlatformSite but
  nests it inside the NetBox site so structure like
  `nb-site:cpn-ful → Intersight CPN → Chassis-1 → blades` survives.
  (`netcortex/graph/query.py`)
- **VLAN consolidation gaps in the visual graph.** Even though
  `vlan:nb:cpn-ful:11` correctly collected both v4 and v6 prefixes,
  `cpn-ful-cat9k1` was not visibly connected to it because no
  adapter ever emitted a `LOGICAL_MEMBER` edge from a Meraki-managed
  catalyst to a VLAN.  `_link_vlan_svis_and_prefixes` now also
  MERGEs a `LOGICAL_MEMBER` from the contributing device whenever it
  adds an SVI / routes-to / NetBox-IPAM derived prefix linkage, and
  a new backfill step at the end of the pass adds `LOGICAL_MEMBER`
  for any device whose `netbox_site_slug` matches a NetBox-scoped
  VLAN that already owns a prefix that device `ROUTES_TO` (catches
  cat9k1 → vlan:nb:cpn-ful:11 even when the v4 path leaves via the
  OOB Gi0/0 mgmt port instead of the Vl11 SVI).
  (`netcortex/graph/correlate.py`)

### Added

- **SNMP MIB-coverage visibility (dev12).** Every direct SNMP poll now
  runs a lightweight per-MIB-family probe scan on the topology cadence
  (≈12 single-OID walks with `-Cr 2`, bounded 4-in-flight parallelism,
  6s per probe — well under a second of extra wall time per device).
  Each probe records `ok` / `empty` / `restricted` / `not_instrumented`
  / `timeout` / `error` plus a row count and the probe OID, so the UI
  can distinguish "this Catalyst's `snmp-server view` is blocking us"
  from "the device genuinely has no data here". The map is JSON-encoded
  into `Device.snmp_mib_coverage_json` and accompanied by three derived
  scalars: `snmp_health` (`full` / `partial` / `restricted` /
  `unreachable` / `cloud_only`), `snmp_missing_mibs`, and
  `snmp_restricted_mibs`. Meraki-cloud-only devices are stamped
  `snmp_health=cloud_only` (no probe is run against the org endpoint)
  so the UI knows not to render a remediation hint for them.
  (`netcortex/adapters/snmp.py`)
- **SNMP coverage card in the data explorer.** The `/data-explorer`
  page now has a dedicated "SNMP MIB coverage" panel per device,
  listing every probed family with status badge, row count and probe
  OID. Required families (the ones whose absence demotes `snmp_health`
  from `full` → `partial`) are flagged with a red dot. When at least
  one family is `restricted` the panel shows an amber remediation box
  with the exact Cisco IOS-XE commands to widen the view; when the
  device is `unreachable` it shows a red reachability hint instead.
  (`netcortex/status/templates/index.html`)
- **Color-coded SNMP pill across the UI.** Both the inventory table
  and the topology detail panel now render the SNMP pill in a color
  derived from `snmp_health` — green = full, amber = partial,
  red = view-restricted, rose = unreachable, purple = cloud-only,
  gray = unpolled. Hovering surfaces the list of missing / restricted
  MIB families plus the same remediation hint. This addresses the
  user's request: "Make the SNMP pill a different color in the
  inventory and list the missing MIB when you hover over it. Also,
  some indication in the data explorer for that node."
  (`netcortex/status/templates/index.html`,
  `netcortex/graph/query.py`)

### Earlier additions in this devN cycle

- **NetBox-site-scoped canonical VLANs.** `_canonicalize_vlans_per_fabric`
  now buckets canonical VLAN ids by NetBox site slug when at least one
  owning device is NetBox-enriched (`vlan:nb:<slug>:<vid>`), falling
  back to the legacy `vlan:<platform_site_id>:<vid>` only when no
  enrichment is available. This collapses the same broadcast/STP
  domain across multiple PlatformSites that map to one physical site —
  e.g. cat9k1 (in `meraki-network:L_xxx`) and n9k1 (in
  `ndfc-fabric:cpn-ful-nd1:cpn-ful`) now share a single `vlan:nb:cpn-ful:11`
  node carrying both IPv4 and IPv6 prefixes. Canonical VLANs also
  track every contributing PlatformSite in a new `platform_site_ids`
  list property so downstream SVI/prefix scoping continues to work
  across fabrics. (`netcortex/graph/correlate.py`)
- **NetBox-IPAM prefix → VLAN linkage.** A new
  `enrich_prefixes_from_netbox_ipam` pass in the worker reads
  `/api/ipam/prefixes/` and stamps `netbox_vlan_vid` / `netbox_site_slug`
  onto existing Prefix nodes. `_link_vlan_svis_and_prefixes` then has a
  fifth join path that uses NetBox IPAM as the source of truth for the
  prefix→VLAN association — picking up cases that on-the-wire
  discovery misses (e.g. `192.133.162.0/24` belongs to VLAN 11 in
  NetBox even though cat9k1's route table reaches it via the OOB
  `Gi0/0` mgmt port rather than `Vl11`). When the NetBox prefix has no
  site of its own, the slug is derived from the enriched device that
  ROUTES_TO the prefix. (`netcortex/sync/netbox_enrich.py`,
  `netcortex/worker.py`, `netcortex/graph/correlate.py`)
- **NetBox enrichment by name fallback.** `enrich_devices_from_netbox`
  now also indexes NetBox devices by normalized hostname (FQDN suffix
  stripped, case-insensitive) and matches graph Devices by name
  whenever the serial doesn't match. NetBox records with empty
  `serial` fields (NX-OS chassis, several CDP-stub neighbours) used to
  be silently skipped — `cpn-ful-n9k1` and `cpn-ful-cat9k2` now both
  acquire `netbox_site_slug='cpn-ful'` and join the unified container
  with cat9k1 in the topology view. Duplicate detection groups graph
  nodes by matched NetBox device id rather than by serial alone, so
  serial-only and name-only matches collapse correctly.
  (`netcortex/sync/netbox_enrich.py`)
- **Cross-adapter legacy VLAN migration.** When NetBox enrichment lands
  after the first canonicalizer pass, the per-fabric `vlan:<plat>:<vid>`
  twin can end up orphaned (its `LOGICAL_MEMBER` edges already moved
  to the NetBox-scoped canonical). A new migration step in
  `_canonicalize_vlans_per_fabric` finds those orphans by matching them
  to a sibling `vlan:nb:<slug>:<vid>` whose `platform_site_ids` list
  contains the orphan's `platform_site_id`, then re-points its
  HAS_SVI/HAS_PREFIX/LOCATED_AT/LOGICAL_MEMBER edges onto the survivor
  and deletes the orphan. (`netcortex/graph/correlate.py`)

- **VLAN node folds in its associated prefix(es).** Canonical VLAN nodes
  now stamp `prefix_v4` / `prefix_v6` (Neo4j string lists) plus a
  `has_prefix` flag whenever a `HAS_PREFIX` edge exists. The topology
  view renders them as a multi-line label: `VLAN <vid> · <name>` with
  the IPv4 and IPv6 CIDRs on their own lines, so the previously
  dangling `Prefix` cyan circle is folded into the VLAN itself. VLAN
  nodes with prefixes also get a slightly larger size and soft purple
  border. (`netcortex/graph/correlate.py`,
  `netcortex/status/templates/index.html`)
- **SVI-IP-to-Prefix linkage.** `_link_vlan_svis_and_prefixes` now also
  links a canonical VLAN to its prefix via the SVI's `ASSIGNED_IP →
  IPAddress.subnet → Prefix.{cidr,prefix}` chain, picking up prefixes
  that adapters (SNMP especially) don't tag with `vlan_id`.
  (`netcortex/graph/correlate.py`)

### Changed

- **Meraki VLANs canonicalise without `LOGICAL_MEMBER` edges.**
  `_canonicalize_vlans_per_fabric` gained a fallback that parses the
  `meraki-vlan:L_xxx:<vid>` id pattern directly and binds it to the
  `meraki-network:L_xxx` PlatformSite, so Meraki VLANs now consolidate
  even though the adapter doesn't emit per-device `LOGICAL_MEMBER`
  edges. Canonical VLAN count went from 32 → 130 in the CPN fleet.
  (`netcortex/graph/correlate.py`)
- **Prefix→VLAN MERGE is now fabric-scoped.** Previously any
  `Prefix.vlan_id = v.vid` pair was linked regardless of fabric, so
  a single Meraki prefix tagged `vlan_id=1` would attach to every
  other fabric's VLAN 1 (CATC default, NDFC default, every other
  Meraki network's VLAN 1, …) — leaving the `default` VLAN with all
  ~45 unrelated subnets. The MERGE now also requires the prefix's
  `network_id` to match the canonical VLAN's `platform_site_id` tail
  whenever the prefix carries one. A pre-step in the correlator also
  sweeps any HAS_PREFIX edges left over from the broad MERGE.
  (`netcortex/graph/correlate.py`)
- **L2 overlay no longer pulls in `HAS_PREFIX`.** The information is
  now on the VLAN node itself, so the L2 view stays clean (Prefix
  nodes only render in L3 via `ROUTES_TO`).
  (`netcortex/graph/query.py`)

### Fixed

- **Legacy orphan canonical VLAN nodes pruned.** Two leftover
  `vlan:1` / `vlan:20` nodes (no `platform_site_id`) from an earlier
  buggy MERGE were collecting cross-fabric HAS_PREFIX edges. The
  housekeeping loop now reaps any canonical VLAN missing a
  `platform_site_id`. (`netcortex/worker.py`)
- **NDFC VLANs now pick up their SVI prefix from the cat9k1 gateway.**
  VLANs 11/12/14/15/16/… on the NDFC fabric were rendering without a
  CIDR even though `cpn-ful-cat9k1` is the L3 gateway and the SNMP
  walk had captured the routes. The catalyst's ASSIGNED_IP edges land
  on a different Interface id than the Meraki-emitted SVI node, so
  the existing SVI→ASSIGNED_IP→Prefix path silently dropped them.
  `_link_vlan_svis_and_prefixes` now also walks
  `Device-[:ROUTES_TO {interface: 'Vl<N>'}]->Prefix` to derive
  (device, vid, prefix) triples and links the prefix to the canonical
  VLAN that the device either lives in or is trunked into via a
  `PHYSICAL_LINK`. Edges from this path are stamped
  `via_routes_to=true` and carry the original interface name on
  `routes_to_iface`. (`netcortex/graph/correlate.py`)
- **Cross-adapter cable duplicates collapsed.** When two adapters
  describe the same physical cable using different port names — e.g.
  cat9k1↔n9k1 reported once as `TwentyFiveGigE1/1/5 ↔ Ethernet1/46`
  by SNMP/CDP and once as `Port 1::C9300x-NM-8Y::5 ↔ Ethernet1/46`
  by Meraki/CDP — `_dedupe_physical_links_by_pair` previously kept
  both because its sub-grouping required the *full* interface-pair
  tuple to match. A new Rule 4 collapses any two surviving edges on
  the same `(a,b)` pair that share at least one non-empty interface
  name (a switch port can only terminate one cable), preferring the
  edge with the more human-readable port naming (non-`Port X::Y::Z`
  encoding). (`netcortex/graph/correlate.py`)
- **Legacy `port-<N>` PHYSICAL_LINK edges pruned.** The
  pre-Phase-3 STP poller minted MAC-correlation edges whose
  `interface_a` was the stale `port-<basePort>` name. Deleting the
  matching Interface nodes (done earlier) left the edges dangling.
  Housekeeping now also drops PHYSICAL_LINK edges whose interface
  name matches that pattern. (`netcortex/worker.py`)

### Versioning

- Switched to `-devN` pre-release suffix between commits so any in-flight
  change is obviously ahead of the last release. (`netcortex/__init__.py`,
  `pyproject.toml`, `CHANGELOG.md`)

---

## [0.4.0] — 2026-05-16

### Added

- **Site grouping toggle.** A new **Groups** button in the topology
  toolbar shows/hides the compound site/container parents. When off,
  every device renders flat so you can compare devices in different
  sites side-by-side without the visual nesting. PlatformSite empty
  containers are also dropped when groups are off. State persists in
  `localStorage`.
  (`netcortex/status/templates/index.html`)

### Changed

- **Overlay selection is now strictly additive.** Previously, an empty
  overlay selection in the UI fell back to "show every edge type",
  which made overlays feel inverted. The UI now sends
  `strict_overlays=true`, so the topology shows ONLY what's toggled:
  - No overlay selected → all canonical devices (and their site
    containers, if Groups is on), **no edges**.
  - One or more overlays selected → exactly those overlays' edges,
    plus the nodes they touch.
  - Devices without a PlatformSite parent are now backfilled when
    rendering nodes-only, so they no longer silently disappear.
  - Backend back-compat preserved: callers that don't pass
    `strict_overlays=true` (e.g. direct `curl` or MCP tools) still get
    the legacy "no overlay = full graph" behavior.
  (`netcortex/graph/query.py`, `netcortex/main.py`,
  `netcortex/status/templates/index.html`)

---

## [0.3.0] — 2026-05-16

### Added

- **Multi-overlay topology view (UI + API).** The dimension picker has
  been replaced with a row of toggleable overlays that can be enabled
  individually or in combination. Suggested defaults: **Physical**,
  **L2 (VLAN + STP)**, **L3 (Routing)**, **SD-WAN**, **Fabric (EVPN)**,
  and **Virtual**. Selecting multiple overlays returns the UNION of
  their underlying edge types so an operator can, for example, see
  cables and routing peers in one canvas.
  - New endpoint: `GET /api/graph/overlays` returns the catalog so the
    UI auto-renders a button per server-side overlay.
  - New query parameter: `GET /api/graph?overlay=<name>` (repeatable).
    The legacy `?dimension=<name>` is still accepted but ignored when
    `overlay` is present.
  - UI: separate **View** group (Overview vs Detail) and **Overlays**
    group (multi-select toggles). The active selection persists in
    `localStorage` so reloads land on the same view.
  - Backward-compat: the legacy `Dimension` enum and `_DIMENSION_RELS`
    map are preserved so deep links and MCP tools keep working.
  - Files: `netcortex/graph/query.py`, `netcortex/main.py`,
    `netcortex/status/templates/index.html`.

- **MAC/ARP vendor enrichment.** `MACAddress` nodes now carry a
  `vendor` property populated from the IEEE OUI registry, surfaced in
  the **MAC/ARP** table's existing Vendor column.
  - New utility: `netcortex/util/oui.py` — a thread-safe, lazy,
    in-memory OUI table sourced from `mac-vendor-lookup`. Locally
    administered MACs (U/L bit set) and unregistered OUIs return
    `""` so we don't litter the graph with junk strings.
  - New correlation pass: `_enrich_mac_vendors()` runs on every cycle,
    writes only when a vendor is resolved, and chunk-batches updates
    (500 MACs per UNWIND) for scalable writes.
  - New dependency: `mac-vendor-lookup>=0.1.15`.
  - Typical coverage on Cisco-heavy fleets: ~90% of MACs annotated;
    the remainder are MAC-randomized client devices with no OUI.

### Changed

- **Header version pill is now visible.** The `v{{ version }}` label
  in the top bar has been promoted from low-contrast `text-gray-500`
  to a bordered monospace pill (`text-gray-300 bg-gray-800`) so the
  current release is glanceable. The version source remains
  `netcortex/__init__.py` and is propagated by `pyproject.toml`.

### Migration notes

- No schema migration required. The vendor column will populate on
  the next correlation cycle (≤30 s by default).
- The UI auto-loads `/api/graph/overlays` at boot; if you've pinned an
  older browser cache, do a hard reload to pick up the new toolbar.

---

## [0.2.1] — 2026-05-16

### Fixed

- **Topology graph edge-id collision for parallel PHYSICAL_LINK edges.**
  `get_full_graph()` and `get_device_context()` built Cytoscape.js edge
  IDs as `"{src}-{rel_type}-{dst}"`, which collided once a device pair had
  more than one cable between them (now possible after the 0.2.0
  multi-edge schema). Cytoscape.js dropped the second edge or threw a
  duplicate-id error. The relationship's internal `id(r)` is now appended
  to the Cytoscape edge id so every parallel cable renders.
  (`netcortex/graph/query.py`)

---

## [0.2.0] — 2026-05-16

This release wraps up the substantial work that landed on top of the
original `0.1.0` scaffold: SNMP harvesting, the multi-dimensional graph,
LLDP/CDP/MAC/ARP topology inference, the data explorer, and the
multi-edge PHYSICAL_LINK schema that finally captures parallel cables.

### Added

- **SNMP v3 harvester (`netcortex.adapters.snmp`).** Wraps `net-snmp`'s
  `snmpbulkwalk` in an async `_SnmpSession` (subprocess + per-walk and
  per-device timeouts) instead of `pysnmp`, which deadlocked under
  concurrent load. Credentials come from AWS Secrets Manager / Vault via
  `SnmpCredentialResolver` with per-device → per-adapter-type → global
  resolution.
- **Meraki dual-plane SNMP polling.** `SnmpContext.CLOUD` (AES, broad
  visibility) vs `SnmpContext.DEVICE` (DES-only on per-device port 161),
  with the resolver enforcing DES for device-level Meraki polls.
- **Multi-dimensional graph in Neo4j.** `physical`, `logical`, `routing`,
  `stp`, `fabric`, `sdwan`, and `virtual` dimensions selectable in the
  UI; structural relationships (`LOCATED_AT`, `MAPS_TO_SITE`, `WITHIN_LOCATION`)
  expressed as Cytoscape compound parents rather than visible edges.
- **MIB coverage.** SNMPv2-MIB, IF-MIB, BRIDGE-MIB (CAM + STP), RSTP-MIB,
  LLDP-MIB, CISCO-CDP-MIB, OSPF-MIB, BGP4-MIB, CISCO-EIGRP-MIB,
  ipAddrTable (RFC 1213), and ipv6AddrTable (RFC 2465).
- **Correlation engine (`netcortex.graph.correlate`).** Stub merger
  (LLDP/CDP neighbor → real device), MAC and ARP based PHYSICAL_LINK
  inference, link dedupe by discovery-protocol priority, and interface
  name normalization.
- **Adaptive SNMP polling cadence.** Topology MIBs (LLDP/CDP) polled at
  a longer interval than IF-MIB and CAM/ARP to keep cycle time low.
- **Multi-edge PHYSICAL_LINK schema.** Relationships now MERGE on
  `(src, dst, interface_a, interface_b)` instead of `(src, dst, type)`,
  so a switch with three cables to the same neighbor surfaces as three
  distinct edges (was: one collapsed edge). Affects ingest, content
  hashing (`_edge_identity`), stub merging, dedupe (`_dedupe_physical_links_by_pair`
  three-rule policy), and the housekeeping reverse-edge collapse.
- **Cisco interface-name normalization (`netcortex/util/ifname.py`).**
  Maps short forms to long (`Twe1/1/5` → `TwentyFiveGigE1/1/5`,
  `Vl80` → `Vlan80`) so LLDP-reported and CDP-reported sides of the
  same cable no longer surface as duplicate links.
- **Data Explorer.** Per-device REST endpoint and UI view returning
  interfaces, neighbors, routing peers, STP, VLANs, prefixes, MACs, and
  SNMP source coverage.
- **Inventory data-source pills** showing each device's discovery
  sources (`meraki`, `snmp`, …) and the specific adapter instance
  (`meraki/CPN` vs `meraki/CPNGOV`).
- **SNMP coverage status** on the adapter card and per-device in
  inventory; SNMP polled timestamps written to each Device node and read
  back in the status page.
- **MAC/ARP table view** that correlates learned MAC, owner device, and
  resolved IP across Meraki, CATC, NDFC, and SNMP.
- **STP topology view** with per-domain root, members ordered by path
  cost, and per-port roles and states.
- **Routing view** combining IPv4/IPv6 prefixes with the routing-peer
  table (OSPF/BGP/EIGRP).
- **NetBox site enrichment** via serial-number lookup — overrides the
  visual container while preserving the platform-reported site as a
  node property.
- **Aggregate site / adapter / dimension views** for scalable topology
  navigation.
- **Sync interval hierarchy.** `netcortex/core.sync_interval` →
  per-adapter-type `sync_interval` → per-instance `sync_interval`, with
  manual `Sync` and `Refresh` buttons on each adapter row.
- **Housekeeping loop.** Periodic cleanup of orphan stubs, satellite
  nodes (RoutingPeer, MACAddress, ARPEntry, IPAddress, Prefix), and
  reverse-direction PHYSICAL_LINK edges left over from pre-canonicalization
  cycles.
- **macOS native worker (`run_worker.sh`).** Runs the discovery loop
  outside Docker so it can reach private management IPs that Docker
  Desktop's network namespace cannot.

### Changed

- **Set-based deduplication** throughout the SNMP adapter. `seen: set[str]`
  replaces O(N²) `any(n.id == x for n in data.nodes)` scans that hung
  the worker on devices with thousands of LLDP entries or routing peers.
- **Hard timeouts everywhere** in the SNMP pipeline: 90 s per walk,
  300 s per device, 30 s per Neo4j write — one slow device can no longer
  stall the entire cycle.
- **Canonical edge direction** (`source_id ≤ target_id`) for all
  undirected relationship types (`PHYSICAL_LINK`, `STP_LINK`,
  `ROUTING_PEER`) so the same link reported from both ends collapses to
  one edge.
- **Content-hash incremental ingest.** Nodes (and edges, where keys are
  stable) carry a `_content_hash`; rows whose hash hasn't changed are
  skipped, making a steady-state "nothing new" cycle nearly free.
- **Removed NetBox nodes from the topology view.** NetBox is used for
  enrichment only; canonical Site/Location nodes no longer clutter the
  graph.
- **Topology view stability.** Zoom, pan, and dimension selection are
  preserved across background refreshes (only an explicit dimension
  change resets them).

### Fixed

- **LLDP/CDP port-ID decoding** when the subtype is `macAddress(3)`.
  `_decode_port_id()` now formats 6-byte values as `aa:bb:cc:dd:ee:ff`
  instead of letting `str(bytes)` surface Python `b'...'` repr in the
  UI.
- **OSPF router-ID as decimal integer** (e.g. `1444263578`). `_decode_ip_val()`
  detects the integer form and converts via `struct.pack("!I", …)` to
  `86.7.x.x`.
- **Routing peer IPs showing junk strings.** Validation now requires a
  parseable IPv4/IPv6 before creating a `RoutingPeer` node.
- **Two `cpn-ash-cat8k1` devices** representing the same physical box.
  Cross-platform deduplication via serial number sets `canonical_id` on
  the non-canonical copy, which the topology query then hides.
- **`vlan80` / `vl80` duplicate STP links** between the same pair.
  Interface-name normalization (`Vl80` → `Vlan80`) collapses them.
- **Stub LLDP/CDP nodes losing their outbound edges.** The stub merger
  now redirects edges in both directions (inbound peer→stub AND outbound
  stub→peer) before `DETACH DELETE`-ing the stub.
- **Stale ARP-correlated PHYSICAL_LINKs** persisting in the graph.
  Deterministic tie-breaking in `_dedupe_physical_links_by_pair` plus
  the "high-confidence overrides inferred" rule drops MAC/ARP edges
  whenever an LLDP/CDP/native-topology edge exists for the same pair.
- **PHYSICAL_LINK edges collapsing parallel cables into one.** New
  multi-edge MERGE on `(src, dst, interface_a, interface_b)` plus
  matching `_edge_identity` for content hashing fixes the long-standing
  issue where `cpn-ful-n9k1`'s 17 LLDP neighbors surfaced as only 9
  edges in Neo4j. (This change is also listed under *Added* because it
  required schema-level work.)
- **Housekeeping over-deletion of reverse-direction PHYSICAL_LINKs.**
  The reverse-edge collapse query in `_housekeeping_loop` now requires
  the canonical-direction edge to share the same `(interface_a,
  interface_b)` pair (in either order) before deleting the reverse —
  previously legitimate parallel cables could be wiped when their
  forward-direction sibling had a different port pair.
- **Inventory junk entries.** `_is_valid_neighbor_name()` rejects names
  that are pure integers, < 3 chars, or non-printable, so SNMP-discovered
  LLDP/CDP stubs only appear when they have a plausible hostname.
- **Adapter status pills not scaling** with many adapters. The status
  page aggregates SNMP coverage per-adapter instead of one pill per
  device.

---

## [0.1.0] — initial scaffold

- Pydantic graph model (`GraphNode`, `GraphEdge`, `GraphData`).
- `PlatformAdapter` interface + entry-point discovery.
- Meraki, Catalyst Center, Intersight, Nexus Dashboard, vSphere
  adapter skeletons.
- FastAPI status page, MCP server scaffold, Neo4j client, Docker
  Compose stack with Redis.
- Secret backend bootstrap (AWS Secrets Manager / HashiCorp Vault).
