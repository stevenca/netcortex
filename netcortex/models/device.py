"""Normalized device model — canonical representation across all adapters."""

from pydantic import BaseModel


class NormalizedDevice(BaseModel):
    """Canonical device representation that maps to dcim.Device in NetBox."""

    name: str
    platform: str                        # adapter name, e.g. "meraki"
    platform_id: str                     # platform-native unique ID
    role: str                            # NetBox role slug, e.g. "switch"
    serial: str | None = None
    mgmt_ip: str | None = None
    site: str | None = None              # NetBox site slug
    tenant: str | None = None            # NetBox tenant slug
    status: str = "active"
    access_methods: list[str] = []       # e.g. ["netconf", "ssh"]
    platform_metadata: dict = {}         # platform-specific extra fields
