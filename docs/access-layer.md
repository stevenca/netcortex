# Access Layer: CLI, RESTCONF, and NETCONF

## Overview

The access layer provides **direct device access** — bypassing controller/cloud APIs to talk directly to network devices. This complements platform adapters (which talk to management planes) by giving you access to the device's own management interfaces.

All three protocols share a common pattern:
- Credentials are always sourced from **NetBox Secrets**
- The device's access methods and their priority are stored in the `nc_access_methods` custom field
- All sessions are logged (session metadata, not content, unless audit logging is enabled)

---

## CLI Access (SSH / Telnet)

### Libraries

- **Netmiko** (primary) — broad multi-vendor support, TextFSM/TTP parsing built-in
- **Scrapli** (alternative) — faster async, better for high-volume automation

The driver is selected from the NetBox `platform.slug`:
```python
PLATFORM_DRIVER_MAP = {
    "ios": "cisco_ios",
    "ios-xe": "cisco_ios",
    "ios-xr": "cisco_xr",
    "nxos": "cisco_nxos",
    "eos": "arista_eos",
    "junos": "juniper_junos",
    "asa": "cisco_asa",
    # ... etc
}
```

### MCP Tool: `run_cli_command`

```json
{
  "tool": "run_cli_command",
  "arguments": {
    "device": "sw-bldga-01",
    "command": "show interfaces status",
    "parse": true,
    "timeout": 30
  }
}
```

Response:
```json
{
  "device": "sw-bldga-01",
  "command": "show interfaces status",
  "access_method": "ssh",
  "raw": "Port      Name               Status       Vlan       ...\nGi0/1                Up           1          ...",
  "parsed": [
    {"port": "Gi0/1", "name": "", "status": "connected", "vlan": "1", "duplex": "a-full", "speed": "a-1000"},
    {"port": "Gi0/2", "name": "uplink", "status": "connected", "vlan": "trunk", "duplex": "a-full", "speed": "a-10G"}
  ],
  "template_used": "cisco_ios_show_interfaces_status.textfsm",
  "elapsed_ms": 1243
}
```

`parse: true` attempts TextFSM parsing using NTC-Templates. If no template is found, only `raw` is returned.

### MCP Tool: `run_cli_commands` (bulk)

Run multiple commands in a single SSH session:

```json
{
  "tool": "run_cli_commands",
  "arguments": {
    "device": "sw-bldga-01",
    "commands": [
      "show version",
      "show interfaces status",
      "show spanning-tree summary"
    ],
    "parse": true
  }
}
```

Returns a list of results, one per command.

### Credential Source

Credentials are fetched from NetBox Secrets using this lookup order:
1. Secret tagged `nc_ssh_{device_name}` — device-specific override
2. Secret tagged `nc_ssh_{site_slug}` — site-wide credential
3. Secret tagged `nc_ssh_default` — global fallback

Enable/disable Telnet per device via `nc_allow_telnet` custom field (default: false).

---

## RESTCONF (RFC 8040)

RESTCONF provides a REST-like HTTP interface to a device's YANG data model. It is available on most modern IOS-XE, NX-OS, and other platforms.

### Discovery

On first connect, NetCortex probes `https://{device}/.well-known/host-meta` and `restconf/data/ietf-yang-library:modules-state` to discover supported YANG modules. Results are cached in the `nc_yang_capabilities` custom field on the device.

### MCP Tool: `get_restconf`

Fetch state or config from a YANG path:

```json
{
  "tool": "get_restconf",
  "arguments": {
    "device": "rtr-core-01",
    "path": "ietf-interfaces:interfaces",
    "datastore": "running"
  }
}
```

Supported `datastore` values: `running` (default), `candidate`, `startup`, `operational`

Response:
```json
{
  "device": "rtr-core-01",
  "path": "ietf-interfaces:interfaces",
  "datastore": "running",
  "data": {
    "ietf-interfaces:interfaces": {
      "interface": [
        {
          "name": "GigabitEthernet1",
          "type": "iana-if-type:ethernetCsmacd",
          "enabled": true,
          "ietf-ip:ipv4": {
            "address": [{"ip": "10.0.0.1", "prefix-length": 24}]
          }
        }
      ]
    }
  }
}
```

### MCP Tool: `put_restconf`

Push configuration via RESTCONF:

```json
{
  "tool": "put_restconf",
  "arguments": {
    "device": "rtr-core-01",
    "path": "ietf-interfaces:interfaces/interface=GigabitEthernet1/description",
    "data": {"ietf-interfaces:description": "WAN Uplink - ISP1"},
    "method": "PUT"
  }
}
```

`method` can be `PUT`, `PATCH`, or `POST`.

### Authentication

RESTCONF auth is sourced from NetBox Secrets tagged `nc_restconf_{device_name}` or `nc_restconf_default`. Supports HTTP Basic and Bearer token.

---

## NETCONF (RFC 6241 / RFC 6242)

NETCONF is the gold standard for structured network configuration. It runs over SSH (port 830) and uses XML with YANG models.

### Library: ncclient

NetCortex uses **ncclient** wrapped in a thin async executor to avoid blocking the event loop.

### Connection & Capability Exchange

On connect, the NETCONF `hello` exchange reveals the device's supported capabilities (YANG modules, RFC features). These are parsed and stored in `nc_yang_capabilities` on the NetBox device.

### MCP Tool: `get_netconf`

Retrieve configuration or state:

```json
{
  "tool": "get_netconf",
  "arguments": {
    "device": "rtr-core-01",
    "operation": "get-config",
    "source": "running",
    "filter": {
      "type": "subtree",
      "value": "<interfaces xmlns='urn:ietf:params:xml:ns:yang:ietf-interfaces'/>"
    },
    "format": "dict"
  }
}
```

`operation` options:
- `get-config` — retrieve configuration datastore
- `get` — retrieve running state + config (operational data)

`format` options:
- `dict` (default) — parsed via `xmltodict`
- `xml` — raw XML string

Response:
```json
{
  "device": "rtr-core-01",
  "operation": "get-config",
  "source": "running",
  "data": {
    "interfaces": {
      "interface": [
        {"name": "GigabitEthernet1", "enabled": "true", ...}
      ]
    }
  }
}
```

### MCP Tool: `netconf_edit_config`

Push configuration changes:

```json
{
  "tool": "netconf_edit_config",
  "arguments": {
    "device": "rtr-core-01",
    "target": "candidate",
    "config": "<config><interfaces xmlns='urn:ietf:params:xml:ns:yang:ietf-interfaces'><interface><name>GigabitEthernet1</name><description>WAN Uplink</description></interface></interfaces></config>",
    "commit": true
  }
}
```

When `commit: true`, NetCortex issues a `<commit>` RPC after a successful `edit-config`. If `target` is `running`, `commit` is ignored.

### MCP Tool: `netconf_get_schema`

Retrieve the YANG schema for a specific module:

```json
{
  "tool": "netconf_get_schema",
  "arguments": {
    "device": "rtr-core-01",
    "identifier": "ietf-interfaces",
    "version": "2014-05-08"
  }
}
```

Returns the YANG module definition as a string.

---

## Access Method Selection

NetCortex determines which access method to use per device via the `nc_access_methods` custom field (set on the NetBox Device):

```json
["netconf", "restconf", "ssh"]
```

Methods are tried in order. If the first fails (connection refused, timeout, auth error), the next is tried. The successful method is cached in Redis for the duration of the session.

You can override at tool call time:

```json
{
  "tool": "run_cli_command",
  "arguments": {
    "device": "sw-bldga-01",
    "command": "show version",
    "force_method": "ssh"
  }
}
```

---

## Security Considerations

- All credentials are fetched from NetBox Secrets at session time — never cached to disk
- SSH host keys are verified against known_hosts or NetBox's device management IP (configurable)
- RESTCONF and NETCONF always use TLS/SSH — plain HTTP is rejected
- CLI sessions have a configurable timeout (default: 30s) to prevent hung sessions
- All access events are logged: device, protocol, timestamp, initiating MCP tool call — but **command content is not logged by default** (enable with `ACCESS_LOG_COMMANDS=true`)
- `netconf_edit_config` and `put_restconf` require the MCP client to have write scope (`netcortex:write`) — read-only clients cannot push config
