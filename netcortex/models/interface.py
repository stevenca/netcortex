"""Normalized interface model."""

from pydantic import BaseModel


class NormalizedInterface(BaseModel):
    """Canonical interface representation mapping to dcim.Interface in NetBox."""

    name: str
    device_platform_id: str             # platform ID of the parent device
    type: str = "other"                 # NetBox interface type slug
    speed: int | None = None            # in Kbps
    mtu: int | None = None
    mac_address: str | None = None
    description: str | None = None
    enabled: bool = True
    oper_status: str | None = None      # "up" | "down" | None
    platform_id: str | None = None      # platform-native interface ID
    platform_metadata: dict | None = None
