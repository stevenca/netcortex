"""Generic REST adapter — schema-mapped via YAML config for platforms without a native adapter."""

from netcortex.adapters.base import PlatformAdapter, PlatformProfile
from netcortex.graph.models import GraphData
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN


class GenericRestAdapter(PlatformAdapter):
    name = "generic_rest"
    display_name = "Generic REST"
    profile = PlatformProfile(
        device_id_field="id",
        role_map={},
        native_topology=False,
        provides_oper_status=False,
        default_access_methods=["ssh"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._base_url: str = config["base_url"].rstrip("/")
        self._auth_config: dict = config.get("auth", {})
        self._field_map: dict = config.get("field_map", {})
        self._role_map: dict = config.get("role_map", {})
        self.profile.role_map = self._role_map

    async def discover(self) -> GraphData:
        # TODO: implement discovery using the configured field_map to extract
        # devices and optionally interfaces from configured REST endpoints
        raise NotImplementedError

    async def authenticate(self) -> None:
        # TODO: support bearer_token, basic, api_key auth types
        raise NotImplementedError

    async def list_devices(self) -> list[NormalizedDevice]:
        # TODO: call configured list_devices endpoint and apply field_map
        raise NotImplementedError

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        raise NotImplementedError

    async def list_vlans(self) -> list[NormalizedVLAN]:
        return []

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        return []
