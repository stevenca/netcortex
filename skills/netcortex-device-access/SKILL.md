---
name: netcortex-device-access
description: >-
  Access network devices via NetCortex MCP — SSH CLI commands, RESTCONF YANG
  paths, NETCONF configuration, and full device detail from inventory. Use when
  the user asks to run a command on a device, pull interface config, show
  running config, fetch YANG data, push configuration changes, check
  capabilities, look up a device's interfaces or IPs, or troubleshoot
  a specific device directly.
---

# NetCortex Device Access

Uses the **netcortex** MCP server. Write operations (`put_restconf`,
`netconf_edit_config`) require explicit user confirmation before calling.

## Find a device first

Before accessing a device, resolve its exact name:

```
find_device(query="cat8k1")          # fuzzy search
list_devices(site="cpn-ful")         # by site
get_device_detail("cpn-ful-cat8k1")  # full record if name is known
```

`find_device` accepts: hostname fragment, IP address, site slug, role.

## CLI access

### `run_cli_command(device, command, timeout)`
Single command via SSH. Returns `{output, exit_code, error}`.

```python
# Examples
run_cli_command("cpn-ful-cat8k1", "show version")
run_cli_command("cpn-ful-cat8k1", "show ip interface brief")
run_cli_command("cpn-ful-cat8k1", "show bgp summary")
```

### `run_cli_commands(device, commands, timeout)`
Multiple commands in one SSH session — more efficient than repeated single calls.

```python
run_cli_commands("cpn-ful-cat8k1", [
    "show version",
    "show ip route summary",
    "show interface GigabitEthernet0/0/1",
])
```

**Safety:** Never run write commands (`conf t`, `no ...`) without a
`write memory` as the final command and explicit user approval.

## RESTCONF

### `get_restconf(device, path, params)`
RFC 8040 GET. `path` is relative to `/restconf/data/`.

```python
# Common paths
get_restconf("cpn-ful-cat8k1",
    "Cisco-IOS-XE-native:native/interface")

get_restconf("cpn-ful-cat8k1",
    "ietf-interfaces:interfaces/interface=GigabitEthernet0%2F0%2F1")

get_restconf("cpn-ful-cat8k1",
    "Cisco-IOS-XE-bgp-oper:bgp-state-data")
```

### `put_restconf(device, path, payload, method)`
Write operation. `method` defaults to `"PUT"`, can be `"PATCH"` or `"POST"`.

**Requires user confirmation before calling.**

```python
put_restconf(
    "cpn-ful-cat8k1",
    "Cisco-IOS-XE-native:native/interface/GigabitEthernet=0%2F0%2F1/description",
    {"Cisco-IOS-XE-native:description": "Uplink to cat8k2"},
    method="PUT",
)
```

## NETCONF

### `get_device_capabilities(device)`
List YANG modules the device supports. Call first to verify the correct
module name before `get_netconf` or `netconf_edit_config`.

### `get_netconf(device, filter_xml, datastore)`
RFC 6241 `<get>` or `<get-config>`. `datastore` defaults to `"running"`.

```python
# Full running config (large — use a filter)
get_netconf("cpn-ful-cat8k1",
    filter_xml="""
    <filter>
      <native xmlns="http://cisco.com/ns/yang/Cisco-IOS-XE-native">
        <interface/>
      </native>
    </filter>
    """)
```

### `netconf_edit_config(device, config_xml, operation, datastore)`
`<edit-config>` RPC. `operation` is `"merge"` (default), `"replace"`, or
`"delete"`. `datastore` defaults to `"running"`.

**Requires user confirmation before calling.**

## Device detail

### `get_device_detail(device)`
Full record: interfaces, IPs, VLANs, VRFs, neighbors, SNMP state,
platform, OS version, model, serial, adapter source.

### `get_device_neighbors(device)`
LLDP/CDP discovered neighbors — name, local port, remote port, platform.
Faster than `topology_get` when you only need the neighbor list.

### `list_adapter_instances(adapter_type)`
Running adapters and health. `adapter_type` optional: `"meraki"`,
`"catalyst_center"`, `"snmp"`, `"intersight"`, etc.

## Common workflows

**"Show me the BGP summary on cat8k1"**
```
run_cli_command("cpn-ful-cat8k1", "show bgp ipv4 unicast summary")
```

**"What VLANs are on cat9k1's uplink?"**
```
get_device_detail("cpn-ful-cat9k1")
→ inspect interfaces section for VLAN membership
```

**"Pull the interface config via RESTCONF"**
```
get_device_capabilities("cpn-ful-cat8k1")   # verify module support
get_restconf("cpn-ful-cat8k1",
    "Cisco-IOS-XE-native:native/interface")
```

**"What YANG modules does this device support?"**
```
get_device_capabilities("cpn-ful-cat8k1")
→ list modules; filter for BGP / interface / routing
```

**"Run show commands on 3 devices at cpn-ful"**
```
list_devices(site="cpn-ful", role="router")
→ for each: run_cli_commands(device, ["show ip route summary", "show proc cpu"])
```
