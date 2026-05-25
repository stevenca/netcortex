# Sync Engine

## Overview

The sync engine is the heartbeat of NetCortex. It periodically polls each platform adapter, computes a diff against the current NetBox state, and reconciles differences according to configured policies.

---

## Sync Cycle

For each adapter, on its configured interval:

```
1.  Adapter.authenticate()             — refresh credentials if needed
2.  Adapter.list_devices()             — fetch all devices from platform
3.  Adapter.list_interfaces(each)      — fetch interfaces (batched)
4.  Adapter.list_vlans()               — fetch VLANs
5.  Adapter.get_neighbors(each)        — fetch topology (if supported)
6.  NetBox.fetch_platform_state()      — fetch all NetBox objects tagged nc_platform=<adapter>
7.  diff_engine.compute(platform, nb)  — compute adds/removes/changes
8.  for each diff:
        apply conflict policy
        if apply: reconciler.write_to_netbox(diff)
        log Journal Entry
9.  Update nc_last_synced on all devices
10. Emit sync_complete event → status page
```

---

## Conflict Resolution Policies

Configured globally via `SYNC_CONFLICT_POLICY` and overridable per field in `nc_sync_field_policies` custom field on a Site or Device.

| Policy | Behavior |
|---|---|
| `platform_wins` | Platform state is applied to NetBox unconditionally |
| `netbox_wins` | NetBox state is preserved; platform differences are noted but not applied |
| `alert` | Diff is recorded and surfaced (status page, Journal Entry) but nothing is auto-applied |

### Default field policies

Some fields have sensible defaults that differ from the global policy:

| Field | Default Policy | Rationale |
|---|---|---|
| `interface.oper_status` | `platform_wins` | Live state always comes from the device |
| `device.serial` | `platform_wins` | Hardware serial doesn't change |
| `device.role` | `netbox_wins` | Role is human intent, not platform data |
| `device.name` | `netbox_wins` | Names may be normalized in NetBox |
| `device.site` | `netbox_wins` | Site assignment is authoritative in NetBox |
| `interface.description` | `netbox_wins` | Descriptions are often maintained in NetBox |
| `vlan.name` | `alert` | VLANs may have different names in platform vs NetBox |

---

## Diff Semantics

The diff engine compares objects by `nc_platform_id` (the platform's native ID stored in NetBox). This handles renames gracefully — if a device is renamed in NetBox but the platform ID is unchanged, it's treated as an update, not a delete + create.

Diff types:

| Type | Meaning |
|---|---|
| `added` | Object exists in platform, not in NetBox |
| `removed` | Object exists in NetBox (for this platform), not in platform |
| `changed` | Object exists in both; one or more fields differ |
| `unchanged` | Object exists in both; all fields match |

Only `added`, `removed`, and `changed` diffs are logged and acted upon.

---

## Scheduler

In single-container mode, APScheduler runs inside the main process:

```
SYNC_INTERVAL_MERAKI=3600         # 60 minutes
SYNC_INTERVAL_CATALYST_CENTER=600 # 10 minutes
SYNC_INTERVAL_INTERSIGHT=3600     # 1 hour
SYNC_INTERVAL_SNMP=1800           # 30 minutes
```

In scaled mode (multiple workers), Celery + Redis is used with `celery beat` as the scheduler. Set `SYNC_BACKEND=celery` to enable.

---

## Audit Trail

Every sync cycle that produces diffs writes a Journal Entry to the affected NetBox object:

```
[nc_sync_diff | meraki | 2026-05-13T19:02:11Z]

CHANGED: interface Gi0/1 on sw-bldga-01
  oper_status: up → down  (policy: platform_wins, applied)

ADDED: device mr-bldga-ap-08
  (policy: platform_wins, applied)
  NetBox device ID: 412
```

These entries are:
- Tagged `nc_sync_diff`
- Queryable via `get_change_log` MCP tool
- Visible in the NetBox UI under the device/site's journal tab
- Indexed for `search_context` semantic search
