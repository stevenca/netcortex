---
name: netcortex-incident-response
description: >-
  Step-by-step incident investigation using NetCortex MCP tools. Guides a
  systematic triage: health check → scope → root cause → evidence → action.
  Use when the user reports an outage, degradation, or alerts; asks to
  "investigate", "troubleshoot", "diagnose", or "help me with this incident";
  or when a specific device, site, or service is reported as having issues.
---

# NetCortex Incident Response

Uses the **netcortex** MCP server. Follow the phases in order; skip a phase
only if the answer is already in a prior call's response.

---

## Phase 1 — Triage (always start here)

```
top_problems(limit=50)
```

Identify the highest-severity problems. Group by `problem_type`.
Quote `summary` and `suggested_action` for each critical item.

**Determine scope from the response:**
- Multiple `device_down` at one site → power / uplink issue at that site.
- Many `link_flapping` on one device → SFP / cabling issue at that device.
- `peer_down` with matching `link_down` → physical carries the routing issue.
- Isolated `peer_down` with no `link_down` → BGP config / auth / timer issue.

---

## Phase 2 — Scope the impact

For a specific **site**:
```
inventory_list(site="<slug>", status="offline")
links_list(site="<slug>", status="down")
links_list(site="<slug>", flap_state="flapping")
peers_list(device="<gateway-at-site>")
```

For a specific **device**:
```
topology_get("<device>", hops=2)
links_list(device="<device>")
peers_list(device="<device>")
```

---

## Phase 3 — Timeline

For every element flagged in Phase 2:
```
history_get("<device>")               # device status transitions
history_get("<device-a>--<device-b>") # link oper_status transitions
history_get("<device>:<peer_ip>")     # peer state transitions
```

**Look for:**
- Correlated timestamps across multiple elements → common cause.
- First failure time vs. scheduled maintenance → planned or unplanned.
- Repeated bouncing in `history` list → hardware degradation pattern.

---

## Phase 4 — Path analysis

If traffic between two endpoints is affected:
```
paths_find("<src>", "<dst>")
```

Check each hop device in the path:
```
links_list(device="<hop-n>", status="down")
```

---

## Phase 5 — Live device evidence

For each suspected device, pull live state via CLI:

```
run_cli_commands("<device>", [
    "show interface <iface>",       # errors, drops, oper state
    "show log | last 50",           # recent syslog
    "show ip route summary",        # routing table health
])
```

For routing issues:
```
run_cli_command("<device>", "show bgp ipv4 unicast summary")
run_cli_command("<device>", "show ip ospf neighbor")
```

---

## Phase 6 — Document and remediate

Pull the runbook and MOP if one exists:
```
search_context("<incident keyword or device name>")
get_documents(device="<device>")
```

Check for recent sync changes that may have introduced the issue:
```
get_change_log(limit=20)
get_pending_diffs()
```

---

## Decision tree

```
top_problems → critical items?
    │
    ├─ device_down ──► history_get(device) → fresh or chronic?
    │                    fresh → check topology_get(device, hops=2) for upstream
    │                    chronic → check SNMP coverage, stale source-of-truth
    │
    ├─ link_down ────► links_list(device=<either end>) → more down links?
    │                    yes (many down) → upstream / power
    │                    no (isolated) → SFP / cable / config
    │
    ├─ link_flapping ► history_get("a--b") → flap pattern
    │                    regular interval → duplex/autoneg/SFP thermal
    │                    random → physical layer — inspect counters
    │
    ├─ peer_down ────► links_list(device=<local>) → underlying link ok?
    │                    link ok → BGP auth/timer/prefix-limit
    │                    link down → fix physical first
    │
    └─ high_util ────► links_list(device=<device>, min_util=75)
                        identify top interfaces; consider LAG or upgrade
```

---

## Incident summary template

After investigation, summarise as:

```
## Incident summary — <device or site>

**Impact**: <what is affected>
**Start time**: <from history_get changed_at>
**Root cause**: <identified or suspected>

**Evidence**:
- <key facts from tool responses>

**Actions taken / recommended**:
1. <immediate>
2. <follow-up>

**Related problems**: <list any correlated items from top_problems>
```
