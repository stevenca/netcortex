---
name: netcortex-lookup
description: >-
  Look up IP addresses, MAC addresses, and devices in the NetCortex graph.
  Finds the switch port, VLAN, and owning device for a MAC; the interface,
  prefix, and device for an IP; and searches inventory by hostname fragment,
  IP, site, or role. Use when the user asks "where is IP X?", "which port is
  MAC Y on?", "find device Z", "what device owns this address?", "which VLAN
  is this prefix on?", "is this MAC flapping?", or any host-location query.
---

# NetCortex Lookup

Uses the **netcortex** MCP server.

## IP lookup

### `ip_lookup(ip, limit)`

Accepts a **host IP** (`"10.0.0.5"`) or a **CIDR prefix** (`"10.0.0.0/24"`).

**Host IP response:**
```json
{
  "addresses": [{"ip": "10.0.0.5", "version": 4,
                 "endpoints": [{"device": "cat9k1", "interface": "Vlan10"}]}],
  "routes":    [{"device": "cat8k1", "interface": "Gi0/1", "prefix": "10.0.0.0/24"}],
  "links":     []
}
```

**CIDR prefix response:**
```json
{
  "prefixes": [{"prefix": "10.0.0.0/24", "version": 4,
                "devices": [{"name": "cat8k1", "interface": "Gi0/1", "ip": "10.0.0.1"}]}],
  "links":    [{"a_name": "cat8k1", "b_name": "cat9k1",
                "l3_prefix_v4": ["10.0.0.0/24"]}]
}
```

**Workflow:** IP → find `addresses` for the host → `routes` for the upstream
routed path → `links` to see which physical cable carries the prefix.

## MAC lookup

### `mac_lookup(mac, limit)`

Accepts any MAC format: `aa:bb:cc:dd:ee:ff`, `aabbccddeeff`, `AA-BB-CC-DD-EE-FF`.

```json
{
  "entries": [{
    "mac": "aa:bb:cc:dd:ee:ff",
    "learned_device": "cat9k1",
    "learned_port":   "GigabitEthernet1/0/5",
    "vlan":           10,
    "owner_device":   "server-a",
    "owner_nic":      "eth0",
    "ip_addresses":   ["10.0.0.50"],
    "source":         "snmp/default"
  }]
}
```

**Multiple entries for the same MAC** → possible duplicate MAC or device
moving between ports. Check `learned_device` and `learned_port` diversity.

**Common follow-ups after MAC lookup:**
```
topology_get("cat9k1")       # see what else is on that switch
links_list(device="cat9k1")  # check if learned_port is healthy
ip_lookup("10.0.0.50")       # if IP known from entry
```

## Device search

### `find_device(query, site, role, adapter)`
Fuzzy search for a device. Returns a ranked list of matches.

```python
find_device("cat8k1")                     # name fragment
find_device("10.0.0.1")                   # management IP
find_device(site="cpn-ful", role="router") # site + role
find_device(adapter="catalyst_center")     # by source adapter
```

### `list_devices(site, role, status, platform, limit)`
Filtered NetBox inventory — more precise than `find_device` when you know
the exact site or role.

```python
list_devices(site="cpn-ful")
list_devices(role="switch", status="active")
```

## Lookup patterns

**"What device owns 10.1.2.3?"**
```
ip_lookup("10.1.2.3")
→ addresses[0].endpoints → device name + interface
→ if not found: routes → device with ROUTES_TO containing the IP
```

**"Which port is MAC aa:bb:cc:dd:ee:ff on?"**
```
mac_lookup("aa:bb:cc:dd:ee:ff")
→ learned_device + learned_port + vlan
→ owner_device if the host is known
```

**"Is this MAC bouncing between switches?"**
```
mac_lookup("aa:bb:cc:dd:ee:ff")
→ multiple entries with different learned_device → MAC flapping
→ check links_list(device=<each switch>) for a flapping uplink
```

**"Find all Catalyst Center devices at cpn-ful"**
```
list_devices(site="cpn-ful", adapter="catalyst_center")
```

**"What's at 10.0.0.0/24?"**
```
ip_lookup("10.0.0.0/24")
→ prefixes → which devices route this subnet
→ links → which cables carry the L3 prefix
```

**"Where is device cpn-ful-cat8k1?"**
```
find_device("cpn-ful-cat8k1")
→ get_device_detail("cpn-ful-cat8k1") for full record
```
