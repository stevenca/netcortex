# Platform Adapters

## Overview

A platform adapter is a Python class that knows how to talk to one specific network management platform and translate what it knows into NetCortex's canonical data models. Every adapter implements the `PlatformAdapter` abstract base class.

Adapters are **read-oriented** — they fetch state from platforms for comparison and sync into NetBox. Configuration *push* operations go through the Access Layer (CLI, RESTCONF, NETCONF), not adapters.

---

## The `PlatformAdapter` Interface

```python
from abc import ABC, abstractmethod
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN

class PlatformAdapter(ABC):
    name: str           # e.g. "meraki", "catalyst_center"
    display_name: str   # e.g. "Cisco Meraki"

    @abstractmethod
    async def authenticate(self) -> None:
        """Establish and validate credentials. Raise AuthError on failure."""

    @abstractmethod
    async def list_devices(self) -> list[NormalizedDevice]:
        """Return all devices visible to this adapter."""

    @abstractmethod
    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return interfaces for a device, keyed by platform-native ID."""

    @abstractmethod
    async def list_vlans(self) -> list[NormalizedVLAN]:
        """Return all VLANs known to this platform."""

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        """Return CDP/LLDP/API neighbors for a device. Optional — default returns []."""
        return []

    async def health_check(self) -> dict:
        """Return a dict with 'status' ('ok'|'degraded'|'error') and optional 'message'."""
        ...
```

---

## Platform Quirks & `PlatformProfile`

Every adapter declares a `PlatformProfile` that encodes platform-specific behavior:

```python
@dataclass
class PlatformProfile:
    # How this platform identifies devices
    device_id_field: str = "serial"        # e.g. "serial", "uuid", "mac"

    # What NetBox device role slug to use for each platform device type
    role_map: dict[str, str] = field(default_factory=dict)
    # e.g. {"MX": "firewall", "MS": "switch", "MR": "access-point"}

    # Whether topology data is available natively or requires LLDP/CDP crawl
    native_topology: bool = False

    # Whether the platform provides operational interface state (up/down)
    provides_oper_status: bool = True

    # Default access methods for devices from this platform (in priority order)
    default_access_methods: list[str] = field(default_factory=lambda: ["ssh"])

    # NetBox platform slug to assign to discovered devices
    netbox_platform_slug: str | None = None
```

---

## Built-in Adapters

### Meraki (`netcortex.adapters.meraki`)

- **API:** Meraki Dashboard API v1
- **Auth:** API key (stored in NetBox Secret, tag: `meraki-api-key`)
- **Scope:** Configurable per NetBox Site → Meraki Org/Network mapping
- **Quirks:**
  - Devices are scoped to networks inside orgs; NetCortex maps networks → NetBox Sites
  - MX = firewall, MS = switch, MR = AP, MV = camera, MT = sensor
  - Topology available via `/organizations/{id}/topology/linkLayer` (requires Meraki license)
  - Serial number is the canonical device identifier
  - Switch port types vary by model; NetCortex normalizes to NetBox interface types
- **Pure normalisation helpers (0.6.0-dev20):**
  Decision boundaries between Meraki values and canonical graph
  values live in module-level pure functions, not inline in
  `discover()`. Each is parametrically unit-tested in
  `tests/adapters/test_meraki_helpers.py` — when adding a new
  mapping, follow the same pattern so the test suite catches
  regressions before they ship.
  - `_norm_device_name(raw) -> str` — trims + collapses internal
    whitespace on dashboard names. Applied to every Device at ingest
    so cross-system joins (NetBox lookups, status-history keys,
    `top_problems` grouping) get a stable identifier.
  - `_reachability_to_oper_status(reachability) -> 'up'|'down'|None`
    — maps Meraki AutoVPN `reachability` to canonical `oper_status`
    on `SDWAN_TUNNEL` edges. `unknown` and missing values return
    `None`; the status-history correlator's
    `WHERE r.oper_status IS NOT NULL` clause then keeps fake
    transitions out of the timeline.
  - `_scope_to_prefix_kind(scope) -> 'vlan_subnet'|'static_route'|None`
    — maps Meraki prefix scopes (`vlan`/`vlan6`/`svi`/`svi6`/`static`)
    to the operator-facing `kind` taxonomy on Prefix nodes. Future
    scopes (`transit`, `wan`) slot in without changing call sites.
- **Source-of-truth timestamps:**
  `get_appliance_uplink_statuses` captures the dashboard's
  `lastReportedAt` per appliance, which `discover()` stamps onto
  each MX `Device` as both `meraki_last_reported_at` (epoch_ms via
  `netcortex.util.timestamps.iso_to_epoch_ms`) and
  `meraki_last_reported_at_iso` (raw string for human inspection).
  These power the dev19 staleness policy in `top_problems` — see
  [§19 of the implementation journal](implementation-journal.md#19-operational-data-quality-the-dev17--dev20-framework).

### Catalyst Center / DNAC (`netcortex.adapters.catalyst_center`)

- **API:** Cisco Catalyst Center REST API v2
- **Auth:** Username/password → JWT token exchange (stored as NetBox Secret)
- **Quirks:**
  - SDA fabric domains are mapped to NetBox VRFs
  - Devices have a `managementIpAddress` that becomes the NetBox primary IP
  - Provisioning state (`reachabilityStatus`, `collectionStatus`) stored in `nc_` custom fields
  - Topology available via `/topology/physical-topology` and `/topology/l2`
  - DNAC UUIDs stored in `nc_platform_id`
- **MAC-address-table fallback (0.6.0-dev20, `discover` section 5b):**
  When the `/v1/host` assurance endpoint returns
  `connectedNetworkDeviceId` + `connectedInterfaceName`, section 5
  already creates the `Interface → LEARNED_MAC → MACAddress` edges.
  When that pipeline is empty (common on early-life or storage-
  constrained CATC clusters), section 5b walks
  `/network-device/{deviceId}/mac-address-table` per switch as a
  fallback. Implementation notes for a future reimplementer:
  - Concurrency is bounded via `asyncio.Semaphore(8)` so a single
    discover cycle doesn't open hundreds of parallel sessions to
    CATC.
  - Schema variations between CATC versions are handled by reading
    `interfaceNumber`, then `ifName`, then `portName`, then
    `interface` in priority order. Entries with no port name are
    skipped.
  - Per-switch failures degrade to `log.debug` and the cycle
    continues — losing one switch's CAM data is much less harmful
    than losing the entire discover cycle.

### Intersight (`netcortex.adapters.intersight`)

- **API:** Cisco Intersight REST API
- **Auth:** API key ID + secret key (RSA signing; stored as NetBox Secret pair)
- **Quirks:**
  - Compute types: `compute/Blades` → server (blade), `compute/RackUnits` → server (rack)
  - Physical location: `FI` (Fabric Interconnect) maps to NetBox rack/chassis hierarchy
  - Server profiles link hardware to software config; stored as custom fields
  - HyperFlex clusters mapped to NetBox clusters

### SNMP (`netcortex.adapters.snmp`)

- **API:** SNMP v2c / v3
- **Auth:** Community string or v3 credentials (stored as NetBox Secret)
- **Library:** `pysnmp` (async)
- **MIBs walked:** `IF-MIB`, `ENTITY-MIB`, `LLDP-MIB`, `CISCO-CDP-MIB`, `IP-MIB`
- **Use case:** Fallback for devices with no API; legacy gear

### Generic REST (`netcortex.adapters.generic_rest`)

- **API:** Any REST API, schema defined in YAML
- **Use case:** Platforms without a native adapter
- **Config:** A YAML mapping file defines which endpoints to call and how to map fields to the canonical model
- **Example config:**
```yaml
adapter: generic_rest
name: my_nms
base_url: https://nms.example.com/api/v1
auth:
  type: bearer_token
  netbox_secret_tag: my-nms-token
endpoints:
  list_devices:
    path: /devices
    method: GET
    field_map:
      name: hostname
      serial: serial_number
      site: location.site_name
      role: device_type  # uses role_map below
role_map:
  switch: switch
  router: router
  server: server
```

---

## Writing a Custom Adapter

1. **Create** `netcortex/adapters/my_platform.py`:

```python
from netcortex.adapters.base import PlatformAdapter, PlatformProfile
from netcortex.models.device import NormalizedDevice

class MyPlatformAdapter(PlatformAdapter):
    name = "my_platform"
    display_name = "My Platform NMS"

    profile = PlatformProfile(
        device_id_field="uuid",
        role_map={"switch": "switch", "router": "router"},
        native_topology=True,
        default_access_methods=["netconf", "ssh"],
    )

    def __init__(self, config: dict):
        self.base_url = config["base_url"]
        self._token: str | None = None

    async def authenticate(self) -> None:
        # fetch token from your platform
        self._token = await self._fetch_token()

    async def list_devices(self) -> list[NormalizedDevice]:
        raw = await self._get("/devices")
        return [
            NormalizedDevice(
                name=d["hostname"],
                platform_id=d["uuid"],
                platform=self.name,
                role=self.profile.role_map.get(d["type"], "unknown"),
                serial=d.get("serial"),
                mgmt_ip=d.get("management_ip"),
            )
            for d in raw["items"]
        ]
    # ... implement list_interfaces, list_vlans, get_neighbors
```

2. **Register** in `pyproject.toml`:
```toml
[project.entry-points."netcortex.adapters"]
my_platform = "netcortex.adapters.my_platform:MyPlatformAdapter"
```

3. **Configure** in NetBox: add a custom field `nc_adapter_config` to the relevant Sites with JSON config for your adapter.

NetCortex auto-discovers registered adapters at startup.
