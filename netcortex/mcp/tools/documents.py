"""MCP tools for operational documents (MOPs, runbooks, context notes)."""

from netcortex.mcp.server import mcp


@mcp.tool()
async def get_documents(
    tag: str | None = None,
    object_type: str | None = None,
    object_name: str | None = None,
    limit: int = 10,
) -> dict:
    """Retrieve MOPs, runbooks, or context notes from NetBox Journal Entries."""
    # TODO: query NetBox journal entries with tag/object filters
    return {"documents": [], "count": 0}


@mcp.tool()
async def search_context(query: str, limit: int = 5) -> dict:
    """Semantic search across all NetBox Journal Entries (MOPs, runbooks, change logs, notes)."""
    # TODO: search embedding index
    return {"results": [], "query": query}


@mcp.tool()
async def get_change_log(
    device: str | None = None,
    site: str | None = None,
    platform: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict:
    """Return sync-generated change audit trail from NetBox Journal Entries."""
    # TODO: query NetBox journal entries tagged nc_sync_diff
    return {"changes": [], "count": 0}
