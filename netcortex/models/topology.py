"""Normalized topology link model."""

from pydantic import BaseModel


class NormalizedTopologyLink(BaseModel):
    """Canonical link between two device interfaces — maps to dcim.Cable in NetBox."""

    device_a_platform_id: str
    interface_a_name: str
    device_b_platform_id: str
    interface_b_name: str
    discovery_proto: str = "unknown"    # "lldp" | "cdp" | "meraki" | "dnac" | etc.
    cable_type: str | None = None       # NetBox cable type if known
