"""MCP tools for direct device access: CLI, RESTCONF, NETCONF."""

from typing import Literal

from netcortex.mcp.server import mcp
from netcortex.access import cli, restconf, netconf

RestconfDatastore = Literal["running", "candidate", "startup"]
RestconfMethod = Literal["PUT", "PATCH", "POST"]
NetconfOperation = Literal["get-config", "get-state", "get"]
NetconfSource = Literal["running", "candidate", "startup"]
NetconfFilterType = Literal["subtree", "xpath"]
NetconfOutputFormat = Literal["dict", "xml"]
NetconfTarget = Literal["candidate", "running", "startup"]


@mcp.tool()
async def run_cli_command(
    device: str,
    command: str,
    parse: bool = True,
    timeout: int = 30,
    force_method: str | None = None,
) -> dict:
    """SSH into a device and run a single CLI command. Returns raw output and optional TextFSM-parsed structured data."""
    # TODO: resolve device credentials from NetBox, then call cli.run_command()
    return {"device": device, "command": command, "raw": "", "parsed": None, "error": "not implemented"}


@mcp.tool()
async def run_cli_commands(
    device: str,
    commands: list[str],
    parse: bool = True,
    timeout: int = 60,
) -> dict:
    """Run multiple CLI commands in a single SSH session."""
    # TODO: implement bulk command execution
    return {"device": device, "results": [], "error": "not implemented"}


@mcp.tool()
async def get_restconf(
    device: str,
    path: str,
    datastore: RestconfDatastore = "running",
) -> dict:
    """Fetch a YANG path from a device via RESTCONF GET (RFC 8040)."""
    # TODO: resolve credentials from NetBox, then call restconf.get()
    return {"device": device, "path": path, "data": None, "error": "not implemented"}


@mcp.tool()
async def put_restconf(
    device: str,
    path: str,
    data: dict,
    method: RestconfMethod = "PUT",
) -> dict:
    """Push configuration to a device via RESTCONF PUT/PATCH/POST. Requires write scope."""
    # TODO: resolve credentials and call restconf.put()
    return {"device": device, "path": path, "status_code": None, "error": "not implemented"}


@mcp.tool()
async def get_netconf(
    device: str,
    operation: NetconfOperation = "get-config",
    source: NetconfSource = "running",
    filter_type: NetconfFilterType | None = None,
    filter_value: str | None = None,
    output_format: NetconfOutputFormat = "dict",
) -> dict:
    """Retrieve configuration or operational state from a device via NETCONF (RFC 6241)."""
    # TODO: resolve credentials and call netconf.get_config() or netconf.get_state()
    return {"device": device, "operation": operation, "data": None, "error": "not implemented"}


@mcp.tool()
async def netconf_edit_config(
    device: str,
    config_xml: str,
    target: NetconfTarget = "candidate",
    commit: bool = True,
) -> dict:
    """Push a NETCONF edit-config RPC to a device. Requires write scope."""
    # TODO: resolve credentials and call netconf.edit_config()
    return {"device": device, "target": target, "success": False, "error": "not implemented"}


@mcp.tool()
async def get_device_capabilities(device: str) -> dict:
    """List NETCONF/RESTCONF YANG capabilities for a device."""
    # TODO: retrieve nc_yang_capabilities from NetBox device custom field
    return {"device": device, "capabilities": []}
