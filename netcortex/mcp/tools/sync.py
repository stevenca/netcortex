"""MCP tools for sync engine visibility and control."""

from typing import Literal

from netcortex.mcp.server import mcp

DiffType = Literal["added", "removed", "changed"]
SyncScope = Literal["devices", "interfaces", "vlans", "topology", "all"]


@mcp.tool()
async def get_sync_status(adapter: str | None = None) -> dict:
    """
    Return sync status for one or all adapter instances.

    adapter: optional instance ID filter, e.g. "meraki/corp", or type prefix "meraki".
             Omit to return status for all instances.
    """
    # TODO: read from Redis sync state cache, keyed by instance_id
    return {"instances": {}}


@mcp.tool()
async def get_pending_diffs(
    adapter: str | None = None,
    diff_type: DiffType | None = None,
) -> dict:
    """
    Return changes detected by the sync engine not yet reconciled with NetBox.

    adapter: optional instance ID filter, e.g. "meraki/corp".
    diff_type: optional filter — "added", "removed", or "changed".
    """
    # TODO: read pending diffs from Redis, keyed by instance_id
    return {"diffs": [], "count": 0}


@mcp.tool()
async def trigger_sync(adapter: str, scope: SyncScope = "all") -> dict:
    """
    Manually trigger a sync for one adapter instance or all instances of a type.

    adapter: instance ID ("meraki/corp") or type prefix ("meraki" for all Meraki instances).
    scope:   "devices" | "interfaces" | "vlans" | "topology" | "all" (default)
    """
    # TODO: enqueue sync job(s) via APScheduler or Celery, keyed by instance_id
    return {"adapter": adapter, "scope": scope, "jobs_queued": [], "error": "not implemented"}
