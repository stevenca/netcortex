"""Normalized VLAN model."""

from pydantic import BaseModel


class NormalizedVLAN(BaseModel):
    """Canonical VLAN representation mapping to ipam.VLAN in NetBox."""

    vid: int
    name: str
    site: str | None = None             # NetBox site slug
    tenant: str | None = None
    status: str = "active"
    platform_id: str | None = None
    platform_metadata: dict | None = None
