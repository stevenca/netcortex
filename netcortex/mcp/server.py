"""MCP server definition — registers all tools and starts the server."""

from fastmcp import FastMCP

from netcortex import __version__

mcp = FastMCP(
    name="NetCortex",
    version=__version__,
    instructions=(
        "Unified network intelligence — inspect inventory, topology, links, "
        "routing peers, and historical status across all your network platforms.\n\n"
        "For agentic-ops diagnostic work, the highest-value entrypoints "
        "are the 'agentic_ops' tools: start with `top_problems` for a "
        "ranked health report, then drill in with `links_list`, "
        "`peers_list`, or `history_get` for the underlying evidence. "
        "Use `topology_get` to understand connectivity around a device, "
        "and `paths_find` to trace end-to-end reachability."
    ),
)

# Tools are registered by importing their modules (side-effect registration)
from netcortex.mcp.tools import devices  # noqa: F401, E402
from netcortex.mcp.tools import access   # noqa: F401, E402
from netcortex.mcp.tools import topology  # noqa: F401, E402
from netcortex.mcp.tools import documents  # noqa: F401, E402
from netcortex.mcp.tools import sync     # noqa: F401, E402
from netcortex.mcp.tools import agentic_ops  # noqa: F401, E402
