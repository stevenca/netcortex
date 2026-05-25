"""Abstract base class for all NetCortex platform adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from netcortex.graph.models import GraphData
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN


@dataclass
class PlatformProfile:
    """Encodes platform-specific behaviors and mapping quirks."""

    # How this platform identifies devices uniquely
    device_id_field: str = "serial"

    # Maps platform device type strings to NetBox role slugs
    role_map: dict[str, str] = field(default_factory=dict)

    # Whether the platform has a native topology API (vs LLDP/CDP crawl)
    native_topology: bool = False

    # Whether the platform provides live operational interface state
    provides_oper_status: bool = True

    # Ordered list of default access methods for devices from this platform
    default_access_methods: list[str] = field(default_factory=lambda: ["ssh"])

    # NetBox platform slug to assign to discovered devices (None = don't set)
    netbox_platform_slug: str | None = None

    # Which graph dimensions this adapter can populate
    supported_dimensions: list[str] = field(
        default_factory=lambda: ["physical", "logical"]
    )


class AuthError(Exception):
    """Raised when adapter authentication fails."""


class AdapterError(Exception):
    """Raised on non-auth adapter errors."""


class PlatformAdapter(ABC):
    """Base class all NetCortex platform adapters must implement.

    Each concrete adapter class handles one platform *type* (e.g. Meraki).
    Multiple *instances* of the same class can run simultaneously — one per
    configured instance (e.g. "meraki/corp", "meraki/branch"). The
    instance_name distinguishes them at runtime.

    Subclass __init__ signature must be:
        def __init__(self, config: dict, instance_name: str) -> None

    The primary discovery path is `discover()` which returns a `GraphData`
    object containing all nodes and edges found. The legacy list_* methods
    remain as helpers that concrete adapters may call internally.
    """

    #: Short type identifier, e.g. "meraki" — must match entry point name
    name: str
    #: Human-readable type name for display, e.g. "Cisco Meraki"
    display_name: str
    #: Platform-specific behavior profile
    profile: PlatformProfile
    #: Instance name set at construction time, e.g. "corp", "branch", "dc1"
    instance_name: str
    #: Discovery interval override in seconds (None = use global/type default).
    #: Set by load_instances() from instance secret, type secret, or core secret.
    _interval: int | None = None

    @property
    def instance_id(self) -> str:
        """Fully qualified instance ID: "{type}/{name}", e.g. "meraki/corp"."""
        return f"{self.name}/{self.instance_name}"

    # ── Primary interface (graph-oriented) ───────────────────────────────────

    @abstractmethod
    async def discover(self) -> GraphData:
        """Discover all network state and return it as a graph.

        This is the primary method called by the sync engine. It should:
        1. Authenticate to the platform
        2. Fetch devices, interfaces, neighbors, VLANs, VRFs, routes, tunnels
        3. Build and return a GraphData object with nodes and edges

        The returned GraphData is written directly into Neo4j and used to
        keep NetBox in sync.
        """

    # ── Authentication ────────────────────────────────────────────────────────

    @abstractmethod
    async def authenticate(self) -> None:
        """Establish and validate credentials. Raise AuthError on failure."""

    # ── Legacy helpers (called from within discover()) ───────────────────────

    @abstractmethod
    async def list_devices(self) -> list[NormalizedDevice]:
        """Return all devices visible to this adapter instance."""

    @abstractmethod
    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return interfaces for a device, keyed by platform-native device ID."""

    @abstractmethod
    async def list_vlans(self) -> list[NormalizedVLAN]:
        """Return all VLANs known to this platform instance."""

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        """Return topology neighbors for a device. Override to implement."""
        return []

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        """Return adapter health status. Override for platform-specific checks."""
        try:
            await self.authenticate()
            return {"status": "ok", "instance_id": self.instance_id}
        except NotImplementedError:
            return {"status": "degraded", "instance_id": self.instance_id,
                    "message": "Adapter authenticate() not yet implemented (stub)"}
        except AuthError as exc:
            return {"status": "error", "instance_id": self.instance_id, "message": f"auth failed: {exc}"}
        except Exception as exc:
            return {"status": "degraded", "instance_id": self.instance_id, "message": str(exc)}
