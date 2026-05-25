# NetBox Integration

## Overview

NetBox is NetCortex's sole backend. All persistent state — inventory, credentials, topology, documents, sync history — lives in NetBox. NetCortex has no database of its own beyond a Redis cache for in-flight sync coordination.

---

## Required NetBox Setup

### 1. API Token

Create a dedicated NetBox API token for NetCortex. For read+sync operations, the token needs:
- `dcim` — read/write (devices, interfaces, cables, sites, racks)
- `ipam` — read/write (prefixes, IPs, VLANs)
- `extras` — read/write (journal entries, tags, custom fields)
- `secrets` — read (credential retrieval)

Set in `.env`:
```
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=your-token-here
```

### 2. Custom Fields

Run the setup command to create all required custom fields automatically:
```bash
docker compose run --rm netcortex python -m netcortex.setup
```

Or create them manually in NetBox Admin → Customization → Custom Fields:

| Name | Type | Object(s) | Description |
|---|---|---|---|
| `nc_platform` | Text | Device | Adapter that manages this device (e.g. `meraki`) |
| `nc_platform_id` | Text | Device | Platform-native ID (Meraki serial, DNAC UUID, etc.) |
| `nc_access_methods` | JSON | Device | Ordered list of access protocols, e.g. `["netconf","ssh"]` |
| `nc_yang_capabilities` | JSON | Device | YANG modules supported by this device |
| `nc_last_synced` | DateTime | Device | Last successful sync timestamp |
| `nc_sync_field_policies` | JSON | Device, Site | Per-field conflict resolution overrides |
| `nc_adapter_config` | JSON | Site | Adapter configuration for this site |
| `nc_allow_telnet` | Boolean | Device | Allow Telnet fallback (default: false) |

### 3. Tags

NetCortex uses these NetBox tags (auto-created by setup):

| Tag | Applied to | Meaning |
|---|---|---|
| `nc_sync_diff` | Journal Entry | Auto-generated diff log from sync engine |
| `nc_discovered` | Device | Device was discovered (not manually created) |
| `mop` | Journal Entry | Method of Procedure document |
| `runbook` | Journal Entry | Operational runbook |
| `context` | Journal Entry | General network context note |
| `change` | Journal Entry | Change record |

### 4. Secrets (Credentials)

NetCortex reads credentials from NetBox Secrets. Create secrets with these naming conventions:

| Secret name pattern | Used for |
|---|---|
| `nc_ssh_{device_name}` | Device-specific SSH credentials |
| `nc_ssh_{site_slug}` | Site-wide SSH credentials |
| `nc_ssh_default` | Global SSH fallback |
| `nc_restconf_{device_name}` | RESTCONF credentials (Basic or Bearer) |
| `nc_restconf_default` | Global RESTCONF fallback |
| `nc_netconf_{device_name}` | NETCONF credentials |
| `nc_netconf_default` | Global NETCONF fallback |
| `nc_meraki_api_key` | Meraki Dashboard API key |
| `nc_catalyst_center_{site_slug}` | Catalyst Center username+password |
| `nc_intersight_key_id` | Intersight API key ID |
| `nc_intersight_secret` | Intersight API secret key |

---

## Data Model Mapping

### Devices → `dcim.Device`

| NetCortex field | NetBox field | Notes |
|---|---|---|
| `name` | `name` | Canonical name |
| `platform` | `nc_platform` (custom) | Adapter name |
| `platform_id` | `nc_platform_id` (custom) | Native ID |
| `role` | `device_role.slug` | Mapped via adapter `role_map` |
| `serial` | `serial` | Hardware serial |
| `mgmt_ip` | `primary_ip4` | Management IP |
| `site` | `site.slug` | Must exist in NetBox |
| `status` | `status` | `active` by default |
| `access_methods` | `nc_access_methods` (custom) | e.g. `["netconf","ssh"]` |

### Interfaces → `dcim.Interface`

| NetCortex field | NetBox field | Notes |
|---|---|---|
| `name` | `name` | Interface name |
| `type` | `type` | Mapped to NetBox interface type enum |
| `speed` | `speed` | In Kbps |
| `oper_status` | (via `enabled`) | `up`→`true`, `down`→`false` |
| `mac_address` | `mac_address` | |
| `mtu` | `mtu` | |
| `description` | `description` | |

### Topology Links → `dcim.Cable`

| NetCortex field | NetBox field | Notes |
|---|---|---|
| `device_a` | `termination_a` → Device+Interface | |
| `device_b` | `termination_b` → Device+Interface | |
| `discovery_proto` | `label` | `lldp`, `cdp`, `meraki`, etc. |
| `type` | `type` | `cat5e`, `smf`, etc. where known |

---

## Documents & Journal Entries

MOPs, runbooks, and context notes are stored as `extras.JournalEntry` objects attached to the relevant NetBox object (Device, Site, VLAN, Prefix).

To create a document via MCP:
```json
{
  "tool": "create_document",
  "arguments": {
    "object_type": "site",
    "object_name": "building-a",
    "tag": "mop",
    "title": "Building A Core Switch Replacement MOP",
    "body": "## Pre-work\n...\n## Steps\n..."
  }
}
```

To retrieve:
```json
{
  "tool": "get_documents",
  "arguments": {
    "tag": "mop",
    "object_type": "site",
    "object_name": "building-a"
  }
}
```

Journal entries support full Markdown in the `comments` field. NetCortex treats the first `# Heading` line as the document title for search and display.

---

## Adapter Configuration (per Site)

Each site that NetCortex should manage has adapter configuration in the `nc_adapter_config` custom field (JSON):

```json
{
  "adapters": [
    {
      "type": "meraki",
      "org_id": "123456",
      "network_ids": ["L_abc123", "L_def456"]
    },
    {
      "type": "catalyst_center",
      "base_url": "https://dnac.example.com",
      "site_hierarchy": "Global/Building A"
    },
    {
      "type": "snmp",
      "community": "nc_snmp_bldga",
      "version": "v3",
      "ip_range": "10.4.22.0/24"
    }
  ]
}
```

The `nc_snmp_bldga` value in `community` is a reference to a NetBox Secret name, not the community string itself.
