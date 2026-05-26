"""MCP prompts — serve workflow guidance on demand.

Two ways to use:
  1. Every session: read the ``instructions`` field from the initialize
     response for a concise tool-category overview.
  2. Before a deep diagnostic task: call ``get_skill(topic)`` once to
     load full workflow docs, patterns, safety rules, and worked examples
     into your context.

Available topics: health | topology | device | incident | lookup | all
"""

from __future__ import annotations

from netcortex.mcp.server import mcp

# ── Per-topic skill content ────────────────────────────────────────────────────

_SKILL_HEALTH = """\
# NetCortex Skill: Network Health Assessment

## Purpose
Use these tools to answer "what's wrong right now?" across the full fleet.
The canonical starting point for any agentic ops session.

## Primary tool: top_problems()
Runs inventory, link, and peer checks in one call and returns a ranked list.

```
top_problems()                    # full sweep, top 20 problems
top_problems(severity="critical") # only service-affecting issues
top_problems(limit=50)            # more results
```

**problem_type** values and what they mean:
| problem_type        | severity | meaning                                         |
|---------------------|----------|-------------------------------------------------|
| device_down         | critical | device.status in {down, offline, unreachable}   |
| link_down           | critical | transit edge oper_status == down                |
| link_flapping       | critical | transit edge flap_state == flapping             |
| peer_down           | critical | routing peer oper_status == down                |
| peer_flapping       | critical | routing peer flap_state == flapping             |
| link_flapping       | warning  | transit edge flap_state == unstable             |
| peer_flapping       | warning  | routing peer flap_state == unstable             |
| high_utilisation    | warning  | link util_pct_avg_1h ≥ 80 (critical ≥ 95)      |
| high_errors         | warning  | link error_rate_per_s_avg_1h ≥ 1 (crit ≥ 5)   |
| snmp_unreachable    | warning  | SNMP polls failing — blind to counters          |
| snmp_restricted     | warning  | SNMP view missing MIB families                  |
| snmp_unpolled       | info     | device never reached by SNMP poller             |

Each problem includes:
- `summary`          — human-readable sentence
- `evidence`         — raw counters, timestamps, or SNMP details
- `suggested_action` — what to do next
- `related`          — {kind, name, id} for follow-up tool calls

**Staleness policy**: device_down and link_down problems on devices whose
source-of-truth (e.g. Meraki Dashboard) hasn't refreshed in >24 h are
automatically demoted to `info` and tagged `evidence.stale = true`.
This prevents abandoned inventory from drowning out live incidents.

## Follow-up: narrow scope

After top_problems, drill into the category with the highest count:

```
# Devices
inventory_list(status="down")
inventory_list(snmp_health="unreachable")
inventory_list(site="cpn-ful")

# Links
links_list(flap_state="flapping")
links_list(status="down", edge_type="WAN_UPLINK")
links_list(min_util=80)

# Peers
peers_list(state="idle")
peers_list(flap_state="flapping", protocol="BGP")
```

## Follow-up: timeline
```
history_get("device-name")              # device uptime / status history
history_get("sw1--sw2")                 # link flap history (-- separator)
history_get("router1:192.168.0.2")      # peer session history (: separator)
```

## Safety rules
- Do NOT call top_problems in a tight loop — each call runs live queries.
  Cache the result and re-use it for the rest of a diagnostic session.
- `suggested_action` is guidance, not automation. Always confirm the action
  with the operator before executing CLI commands on devices.
- Stale problems (evidence.stale == true) should be presented as housekeeping
  items, not live incidents.

## Worked example: "What's wrong in the network?"
```
1. Call top_problems()
   → 3 critical device_down (2 stale, 1 live)
   → 1 critical link_flapping on "sw1 <-> sw2"
   → 2 warning snmp_unreachable

2. For the live device_down:
   history_get("router1")           # is this a new outage or long-standing?
   topology_get("router1")          # which devices lose connectivity?

3. For the flapping link:
   history_get("sw1--sw2")          # how many flaps in 1h vs 24h?
   links_list(device="sw1")         # other links on sw1 for context

4. Report findings, propose: swap cable/SFP on sw1--sw2, investigate
   router1 power/console, add SNMP credentials for the two unreachable devices.
```
"""

_SKILL_TOPOLOGY = """\
# NetCortex Skill: Topology Exploration

## Purpose
Understand how devices are connected, trace paths, and spot topology
anomalies before executing any changes.

## Primary tools

### topology_get(device, hops=1)
Returns the subgraph around one device — neighbors, interfaces, VLANs,
VRFs, BGP peers, and SD-WAN tunnels — out to `hops` cable-distance.

```
topology_get("cpn-ful-cat9k1")           # immediate neighbors
topology_get("cpn-ful-cat9k1", hops=2)  # 2-hop neighborhood
topology_get("cpn-ful-mx1", hops=1)     # MX and its WAN uplinks
```

Response shape:
```json
{
  "device": {name, role, mgmt_ip, site, status, snmp_health},
  "neighbors": [{name, iface_local, iface_remote, edge_type, oper_status}],
  "interfaces": [{name, oper_status, speed_mbps, util_pct}],
  "vlans": [{vlan_id, name}],
  "vrfs": [{name, rd}],
  "bgp_peers": [{peer_ip, remote_as, state, oper_status}],
  "sdwan_tunnels": [{remote_device, oper_status, health_score}],
  "graph": {"nodes": [...], "edges": [...]}   ← Cytoscape-compatible
}
```

### paths_find(src_device, dst_device, max_hops=10)
Finds the shortest hop-count path between two devices.

```
paths_find("cpn-ful-cat9k1", "cpn-ful-mx1")
paths_find("server1", "core-router", max_hops=5)
```

Response: `{source, destination, hops, path: [{node, via}, ...]}`
If no path exists within `max_hops` you get `{error: "no path found"}`.

### links_list(device=..., site=..., edge_type=...)
When you want all links touching a device or site rather than a graph view.

```
links_list(device="cpn-ful-cat9k1")         # all links on this device
links_list(site="cpn-ful", status="down")   # all down links at a site
links_list(edge_type="WAN_UPLINK")          # all WAN uplinks fleet-wide
```

## Edge types
| edge_type       | meaning                                    |
|-----------------|--------------------------------------------|
| PHYSICAL_LINK   | LLDP/CDP discovered cable or fiber         |
| WAN_UPLINK      | MX uplink / ISP circuit (correlator-built) |
| SDWAN_TUNNEL    | Meraki / VPN tunnel                        |
| VXLAN_TUNNEL    | VXLAN overlay (Nexus / ACI)                |

## Flap state values
| flap_state | meaning                                          |
|------------|--------------------------------------------------|
| stable     | no transitions in the last 24 h                  |
| unstable   | 1–4 transitions in the last 24 h                 |
| flapping   | ≥ 5 transitions in the last 1 h                  |

## Worked example: "How is server1 reaching the internet?"
```
1. paths_find("server1", "internet-gateway")
   → hops: server1 → access-sw1 → dist-sw1 → core-rtr → mx1

2. topology_get("mx1", hops=1)
   → WAN uplinks: WAN1 (up), WAN2 (down)
   → SD-WAN tunnels to hub: 3 up, 0 down

3. links_list(device="core-rtr", status="down")
   → no down links on core-rtr

4. history_get("mx1")
   → WAN2 went down 6h ago, 2 flaps before settling

Report: primary path is healthy; WAN2 on mx1 is down (6h) — open ticket
for ISP circuit investigation.
```

## Safety rules
- `topology_get` with hops ≥ 3 on a large fleet can return hundreds of nodes.
  Use hops=1 by default and expand only if the immediate neighborhood is clear.
- Prefer `paths_find` over manually traversing hops — it uses graph shortest-
  path and handles multi-hop routing peers correctly.
- Never assume a path is functional just because nodes exist on it;
  always correlate with `links_list` oper_status and `peers_list` for the L3 hop.
"""

_SKILL_DEVICE = """\
# NetCortex Skill: Device Access & Detail

## Purpose
Find devices, inspect their configuration state, and run read-only
diagnostic commands over SSH / RESTCONF / NETCONF.

## Finding a device

### find_device(query, name, ip, site, role, adapter)
Fuzzy search across name, IP, site, role, and adapter.

```
find_device(query="cat8k")              # partial name
find_device(ip="10.1.1.1")             # by management IP
find_device(site="cpn-ful", role="router")
find_device(adapter="meraki")           # all devices from Meraki adapters
```

### get_device_detail(device)
Full detail for one device: interfaces, IPs, VLANs, neighbors, SNMP state.

```
get_device_detail("cpn-ful-cat8k1")
```

## CLI access (SSH)

### run_cli_command(device, cmd)
Execute a single command. Returns raw output.

```
run_cli_command("cpn-ful-cat9k1", "show interfaces status")
run_cli_command("cpn-ful-cat9k1", "show ip bgp summary")
run_cli_command("cpn-ful-mx1",    "show arp")
```

### run_cli_commands(device, cmds)
Execute multiple commands in one SSH session (avoids reconnect overhead).

```
run_cli_commands("cpn-ful-cat9k1", [
    "show version",
    "show ip interface brief",
    "show spanning-tree summary",
])
```

## RESTCONF / NETCONF

### get_restconf(device, path)
HTTP GET a YANG path via RESTCONF (IOS-XE, IOS-XR, NXOS).

```
get_restconf("cpn-ful-cat9k1",
             "Cisco-IOS-XE-native:native/ip/route")
get_restconf("cpn-ful-cat9k1",
             "ietf-interfaces:interfaces")
```

### get_netconf(device, filter)
NETCONF get-config or get-state using an XML subtree filter.

```
get_netconf("cpn-ful-cat9k1",
            "<interfaces xmlns='urn:ietf:params:xml:ns:yang:ietf-interfaces'/>")
```

## Runbooks & MOPs

### search_context(query)
Full-text search of NetBox documents (MOPs, runbooks, config templates).

```
search_context("BGP reset procedure")
search_context("WAN uplink failover")
search_context("spanning tree root")
```

## Safety rules
- **Always read-only first.** Never run `write`, `configure`, `no`, `shutdown`,
  or any state-changing CLI command without explicit operator confirmation.
- Use `run_cli_command` for quick single checks; prefer `run_cli_commands`
  for any workflow that needs >2 commands to avoid repeated SSH handshakes.
- RESTCONF paths must include the full YANG module prefix
  (e.g., `Cisco-IOS-XE-native:native/...`) — partial paths will 404.
- NETCONF filter must be valid XML; malformed filters return a protocol error.
- If `get_device_detail` returns `snmp_health: unreachable`, CLI access may
  also fail. Report the SNMP issue first so the operator can fix credentials
  before you attempt SSH diagnostics.

## Worked example: "Diagnose high CPU on cpn-ful-cat9k1"
```
1. get_device_detail("cpn-ful-cat9k1")
   → snmp_health: full, status: active  ← good, proceed

2. run_cli_commands("cpn-ful-cat9k1", [
       "show processes cpu sorted",
       "show ip bgp summary",
       "show interfaces | inc input rate",
   ])
   → BGP process at 45% CPU; one peer in Active state

3. peers_list(device="cpn-ful-cat9k1", state="active")
   → peer 10.0.0.2 (AS 65001) in Active state, 3 flaps in 1h

4. history_get("cpn-ful-cat9k1:10.0.0.2")
   → peer bouncing every ~12 min since 09:15

5. run_cli_command("cpn-ful-cat9k1",
                   "show bgp neighbor 10.0.0.2 | inc Hold|Keepalive|Error")
   → Hold timer mismatch

Report: BGP peer 10.0.0.2 is flapping due to hold-timer mismatch causing
CPU spike. Recommended fix: align hold/keepalive timers on both sides.
```
"""

_SKILL_INCIDENT = """\
# NetCortex Skill: Incident Investigation Workflow

## Purpose
Structured, repeatable approach for diagnosing network incidents from
"something is broken" through to root cause and remediation proposal.
Always read-only — never make changes without operator approval.

## Phase 1: Establish scope (< 2 min)
```
top_problems(severity="critical")
```
Goal: identify all active critical problems in one call.
Output: ranked list with summary, evidence, suggested_action, and a
        related {kind, name, id} for every problem.

Note: check `scanned.devices`, `scanned.links`, `scanned.peers` in the
      response — if any are 0 a sync may be running; call get_sync_status().

## Phase 2: Triage (2–5 min)
Group problems by symptom cluster:
- All device_down + link_down pointing to the same site → likely site failure
- Single device_down with no related link_down → device-specific issue
- Multiple link_flapping on the same device → SFP / power instability
- peer_down with link_down on the same path → L3 follows L2 outage
- peer_down with link UP → authentication / timer / policy issue

## Phase 3: Timeline context
For each problem cluster, call history_get to determine:
- Is this new (< 1h) or long-standing?
- Is it one clean transition or a flap pattern?
- Did it correlate with a change window?

```
history_get("device-name")        # device status history
history_get("sw1--sw2")           # link flap timeline
history_get("rtr1:192.0.2.1")     # peer session timeline
```

## Phase 4: Topology context
```
topology_get("affected-device", hops=2)   # blast radius
paths_find("src", "dst")                  # path affected?
links_list(device="affected-device")      # all edges for this device
```

## Phase 5: Live evidence (read-only CLI)
Run diagnostic commands — confirm before each one with the operator.

Common read-only commands by symptom:
| symptom              | commands                                            |
|----------------------|-----------------------------------------------------|
| link flapping        | show interfaces <iface>; show log | inc %LINK       |
| BGP peer down        | show bgp neighbor <ip>; show ip bgp summary         |
| OSPF peer down       | show ip ospf neighbor; show ip ospf interface       |
| device unreachable   | ping <mgmt_ip> source <loopback>; show ip route     |
| high CPU             | show processes cpu sorted; show log | inc CPUHOG    |
| high utilisation     | show interfaces <iface> | inc rate; show qos int    |
| SNMP unreachable     | show snmp; show run | inc snmp-server               |

```
run_cli_commands("device", [
    "show interfaces GigabitEthernet1/0/1",
    "show log | inc %LINK-3-UPDOWN",
])
```

## Phase 6: Runbook lookup
```
search_context("BGP flapping remediation")
search_context("SFP replacement procedure")
search_context("spanning-tree recovery")
```

## Phase 7: Remediation proposal
Do NOT execute changes. Summarise:
1. Root cause (with evidence quotes from the tool responses)
2. Affected scope (devices, sites, services)
3. Proposed fix (with exact CLI / config snippet if applicable)
4. Rollback plan
5. Change window recommendation

## Safety rules
- Never use `run_cli_command` with any write operation (configure, no, write,
  reload, shutdown, clear) without explicit operator confirmation for each step.
- If top_problems returns stale data (evidence.stale = true), always call
  get_sync_status() and potentially trigger_sync() before concluding on scope.
- Do not call trigger_sync() during a live incident unless the operator
  explicitly asks — a sync during an outage can mask transient state changes.
- For changes on production devices, always reference a runbook from
  search_context first; propose the change with the document citation.

## Worked example: "Users at cpn-ful can't reach the internet"
```
Phase 1: top_problems(severity="critical")
  → device_down: cpn-ful-mx1 (stale: false)
  → link_down:   WAN_UPLINK cpn-ful-mx1 <-> Internet

Phase 2: single site, WAN uplink down — likely ISP or MX issue

Phase 3: history_get("cpn-ful-mx1")
  → went down 47 min ago, clean single transition

Phase 4: topology_get("cpn-ful-mx1")
  → WAN1: down, WAN2: down (both!)
  → SD-WAN hub tunnels: all down
  paths_find("cpn-ful-cat9k1", "cpn-ful-mx1") → reachable internally

Phase 5: run_cli_commands("cpn-ful-mx1", [
    "show interface status",
    "show arp",
    "ping 8.8.8.8",
])
  → WAN1/WAN2 physically up but no ARP from ISP
  → ping fails, default route present

Phase 6: search_context("WAN uplink failover MX")
  → runbook: NC-OPS-042 "MX dual-WAN failover"

Phase 7 (proposal):
  Root cause: ISP-side issue — both WAN ports up physically but no
              upstream ARP/DHCP response since 14:13.
  Scope: all internet-bound traffic at cpn-ful site (~200 users).
  Proposed: Contact ISP, reference circuit IDs from topology_get output.
  Rollback: N/A (no changes proposed yet).
  CW: Emergency — ISP ticket immediately.
```
"""

_SKILL_LOOKUP = """\
# NetCortex Skill: IP & MAC Address Lookup

## Purpose
Answer "where does this address live?" questions: locate hosts, find
switch ports, spot duplicates, and confirm VLAN/subnet assignments.

## IP address lookup

### ip_lookup(ip)
Accepts a host IP or CIDR prefix.

```
ip_lookup("10.1.2.3")          # find device + interface owning this IP
ip_lookup("192.168.10.0/24")   # find prefix node, attached devices, links
```

Response for host IP:
```json
{
  "prefixes":  [],
  "addresses": [{ip, version, endpoints: [{device, interface}]}],
  "routes":    [{device, interface, prefix}],
  "links":     []
}
```

Response for CIDR:
```json
{
  "prefixes": [{prefix, version, devices: [{name, interface, ip}]}],
  "addresses": [],
  "links":    [{a_name, b_name, l3_prefix_v4, l3_prefix_v6}]
}
```

**Tip**: if `addresses` is empty for a host IP, check `routes` — the IP
may be assigned via ROUTES_TO (common when there's no IPAddress node but
the adapter has populated the device's route table entry).

## MAC address lookup

### mac_lookup(mac)
Accepts any common MAC format (colon, dash, dotted, or plain hex).

```
mac_lookup("aa:bb:cc:11:22:33")
mac_lookup("aabb.cc11.2233")       # Cisco dotted notation
mac_lookup("aabbcc112233")         # no separator
```

Response:
```json
{
  "entries": [{
    "mac":           "aa:bb:cc:11:22:33",
    "learned_device": "access-sw1",
    "learned_port":   "GigabitEthernet1/0/5",
    "vlan":           100,
    "owner_device":   "server1",
    "owner_nic":      "eth0",
    "ip_addresses":   ["10.1.2.3"],
    "source":         "snmp"
  }],
  "truncated": false, "returned": 1, "total": 1
}
```

If `entries` is empty the MAC is not in the switch MAC tables (either
the device hasn't communicated recently, or SNMP is not polling this switch).

## Duplicate detection
Both tools naturally surface duplicates:
- `ip_lookup` returns multiple `endpoints` if the same IP is assigned to
  more than one interface (duplicate IP).
- `mac_lookup` returns multiple entries if the same MAC is learned on more
  than one switch port (MAC flapping / duplicate NIC).

```
ip_lookup("10.1.2.3")
  → endpoints: [{device: "server1", interface: "eth0"},
                {device: "server2", interface: "eth1"}]  ← DUPLICATE IP

mac_lookup("aa:bb:cc:11:22:33")
  → entries: [{learned_device: "sw1", learned_port: "Gi1/0/5"},
              {learned_device: "sw2", learned_port: "Gi2/0/1"}]  ← MAC FLAP
```

## Worked examples

### "Which device owns 10.0.50.1 and what VLAN is it in?"
```
ip_lookup("10.0.50.1")
  → addresses: [{device: "core-sw1", interface: "Vlan50"}]

get_device_detail("core-sw1")   # confirm VLAN 50 SVI config

topology_get("core-sw1")        # see which access switches are in VLAN 50
```

### "A server at MAC aa:bb:cc:dd:ee:ff is not reachable — where is it?"
```
mac_lookup("aa:bb:cc:dd:ee:ff")
  → entries: [{learned_device: "access-sw3", learned_port: "Gi0/12",
               vlan: 200, ip_addresses: ["10.2.0.55"]}]

# Port found — check port state
run_cli_command("access-sw3",
    "show interfaces GigabitEthernet0/12 status")

links_list(device="access-sw3")   # check upstream connectivity
```

### "Is there an IP conflict on 172.16.1.10?"
```
ip_lookup("172.16.1.10")
  → 2 endpoints → DUPLICATE IP confirmed

# Document both endpoints, escalate to IPAM team
search_context("IP conflict resolution")
```

## Safety rules
- `ip_lookup` searches the graph (last-sync data) — it is not a live ARP
  scan. If the result is empty, check get_sync_status() to confirm the
  relevant adapter has synced recently before concluding the IP is unused.
- `mac_lookup` depends on SNMP poll of switch MAC tables. Devices with
  snmp_health = unreachable or unpolled will not appear in results.
- Multiple entries for the same MAC are diagnostic evidence, not confirmation
  of a fault. The port could be a legitimate trunk. Correlate with the VLAN
  field and learned_port names before escalating.
"""

_SKILLS: dict[str, str] = {
    "health":   _SKILL_HEALTH,
    "topology": _SKILL_TOPOLOGY,
    "device":   _SKILL_DEVICE,
    "incident": _SKILL_INCIDENT,
    "lookup":   _SKILL_LOOKUP,
}

_ALL_SKILLS = "\n\n---\n\n".join(_SKILLS[k] for k in _SKILLS)

_TOPIC_LIST = " | ".join(_SKILLS) + " | all"


# ── Prompt: get_skill ──────────────────────────────────────────────────────────

@mcp.prompt(
    name="get_skill",
    description=(
        "Return full workflow docs, patterns, safety rules, and worked examples "
        "for a NetCortex diagnostic topic. "
        f"Topics: {_TOPIC_LIST}"
    ),
)
def get_skill(topic: str = "all") -> str:  # type: ignore[return]
    """Return the deep-reference skill document for one topic (or all).

    Args:
        topic: One of ``health | topology | device | incident | lookup | all``.
               Defaults to ``all`` if omitted or unrecognised.

    Returns:
        A Markdown document with tool usage patterns, worked examples,
        and safety rules for the requested topic area.
    """
    key = (topic or "all").strip().lower()
    if key in _SKILLS:
        return _SKILLS[key]
    if key == "all":
        return _ALL_SKILLS
    # Fuzzy fallback — return the closest match or the index
    for k in _SKILLS:
        if k.startswith(key):
            return _SKILLS[k]
    # Still no match — return index with available topics
    return (
        f"Unknown topic '{topic}'. Available topics: {_TOPIC_LIST}\n\n"
        "Call get_skill() with no argument (or topic='all') for the full guide."
    )
