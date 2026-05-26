"""MCP server definition — registers all tools and starts the server."""

from fastmcp import FastMCP

from netcortex import __version__

_INSTRUCTIONS = """\
NetCortex — unified network intelligence across all your platforms \
(Meraki, Catalyst Center, Nexus Dashboard, NetBox, SNMP).

## Quick-start workflow
1. top_problems()                   → ranked health report (best first call)
2. topology_get(device) / links_list() / peers_list()  → narrow scope
3. history_get(element)             → timeline & flap stats
4. run_cli_command(device, cmd)     → live evidence from the device
5. search_context(query)            → pull runbooks / MOPs from NetBox docs

## Tool categories

### Health & triage
- top_problems(limit, severity)         Ranked problems: device_down, link_down,
                                        link_flapping, peer_down, peer_flapping,
                                        high_utilisation, high_errors, snmp_*.
                                        Call with no args for a full health sweep.
- inventory_list(site, role, status,    Filtered device list with SNMP health,
                 flap_state,            flap state, and operational status.
                 snmp_health)
- links_list(status, edge_type,         Transit edges (cables, WAN uplinks,
             flap_state,                SD-WAN tunnels) sorted by urgency.
             min_flap_score,            Use min_util/min_flap_score to filter.
             min_util, site, device)
- peers_list(state, flap_state,         BGP / OSPF / EIGRP adjacencies with
             protocol, device)          flap stats; state = 'established'|'idle'.

### Topology & path
- topology_get(device, hops=1)          Neighbor graph around one device.
                                        hops 1–4; returns nodes+edges ready
                                        for Cytoscape or agent reasoning.
- paths_find(src_device, dst_device)    Shortest hop-count path between two
                                        devices (up to max_hops=10).

### History & flap timeline
- history_get(element_name,             24 h / 7 d transition log for a
              field='oper_status',       device (by name), link ("a--b"),
              target='auto')            or peer ("device:peer_ip").

### Host & prefix lookup
- ip_lookup(ip)                         Find device/interface/VLAN for a
                                        host IP or CIDR prefix.
- mac_lookup(mac)                       Find switch port and device for a MAC;
                                        spot duplicate/flapping MACs.

### Device detail & CLI access
- find_device(query, name, ip,          Fuzzy search across name, IP, site,
              site, role, adapter)      role, and adapter instance.
- get_device_detail(device)             Interfaces, IPs, VLANs, neighbors,
                                        and SNMP state for one device.
- run_cli_command(device, cmd)          SSH — execute a single CLI command.
- run_cli_commands(device, cmds)        SSH — execute multiple commands in
                                        one session.
- get_restconf(device, path)            RESTCONF GET a YANG path.
- get_netconf(device, filter)           NETCONF get-config / get-state.

### Sync & documents
- get_sync_status()                     Last-sync timestamps per adapter.
- trigger_sync(adapter)                 Kick a manual sync for one adapter.
- search_context(query)                 Full-text search of NetBox MOPs,
                                        runbooks, and config templates.

## Deep reference
Call the `get_skill` prompt with one of these topics for full workflow docs,
patterns, safety rules, and worked examples:

  health | topology | device | incident | lookup | all
"""

mcp = FastMCP(
    name="NetCortex",
    version=__version__,
    instructions=_INSTRUCTIONS,
)

# Tools are registered by importing their modules (side-effect registration)
from netcortex.mcp.tools import devices      # noqa: F401, E402
from netcortex.mcp.tools import access       # noqa: F401, E402
from netcortex.mcp.tools import topology     # noqa: F401, E402
from netcortex.mcp.tools import documents    # noqa: F401, E402
from netcortex.mcp.tools import sync         # noqa: F401, E402
from netcortex.mcp.tools import agentic_ops  # noqa: F401, E402

# Prompts are registered by importing the prompts module
from netcortex.mcp import prompts  # noqa: F401, E402
