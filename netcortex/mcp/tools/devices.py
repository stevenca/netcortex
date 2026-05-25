"""MCP tools for device inventory queries."""

from netcortex.mcp.server import mcp


@mcp.tool()
async def find_device(
    query: str | None = None,
    name: str | None = None,
    ip: str | None = None,
    site: str | None = None,
    role: str | None = None,
    adapter: str | None = None,
    status: str | None = None,
    limit: int = 25,
) -> dict:
    """
    Search for devices by name, IP, site, role, or adapter instance.

    adapter: optional instance ID filter, e.g. "meraki/corp", "catalyst_center/dc1",
             or just a type prefix like "meraki" to match all Meraki instances.
    """
    # TODO: implement via netbox client, filter nc_platform custom field by adapter
    return {"devices": [], "count": 0}


@mcp.tool()
async def list_devices(
    site: str | None = None,
    role: str | None = None,
    adapter: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Return a filtered list of devices from NetBox inventory.

    adapter: optional instance ID filter, e.g. "meraki/corp", "meraki" (all Meraki instances).
    """
    # TODO: implement via netbox client
    return {"devices": [], "count": 0}


@mcp.tool()
async def get_device_detail(device: str) -> dict:
    """Return full detail for a single device including interfaces, IPs, and adapter metadata."""
    # TODO: implement via netbox client
    return {"device": None, "error": "not implemented"}


@mcp.tool()
async def get_device_neighbors(device: str, include_remote_details: bool = False) -> dict:
    """Return topology neighbors for a device discovered via LLDP/CDP or adapter topology API."""
    # TODO: implement via netbox client cables
    return {"neighbors": []}


@mcp.tool()
async def list_adapter_instances(adapter_type: str | None = None) -> dict:
    """
    List all running adapter instances and their health status.

    adapter_type: optional filter, e.g. "meraki" to list only Meraki instances.

    Returns each instance's ID, type, name, status, and last sync time.
    Example response:
    {
      "instances": [
        {"id": "meraki/corp",          "type": "meraki", "name": "corp",  "status": "ok",      "last_sync": "2m ago"},
        {"id": "meraki/branch",        "type": "meraki", "name": "branch","status": "ok",      "last_sync": "3m ago"},
        {"id": "catalyst_center/dc1",  "type": "catalyst_center", ...},
        {"id": "nexus_dashboard/prod", "type": "nexus_dashboard", ...}
      ]
    }
    """
    from netcortex.adapters import get_instances
    instances = get_instances(adapter_type)
    result = []
    for iid, adapter in instances.items():
        health = await adapter.health_check()
        result.append({
            "id": iid,
            "type": adapter.name,
            "name": adapter.instance_name,
            "display_name": adapter.display_name,
            **health,
        })
    return {"instances": result, "count": len(result)}
