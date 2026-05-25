"""MCP tools for querying the network graph.

These tools are called by LLMs via the MCP protocol to get network state
and topology context for troubleshooting and answering questions.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def get_network_graph(
    dimension: str | None = None,
    site: str | None = None,
    limit: int = 500,
) -> dict:
    """Query the multi-dimensional network graph.

    Args:
        dimension: Filter to a single layer — one of:
                   'physical'  (cables, ports, device locations)
                   'logical'   (VLANs, SVIs, LAGs, IP assignments)
                   'routing'   (BGP sessions, routing adjacencies, VRFs)
                   'fabric'    (EVPN/VXLAN VNIs, VTEP peers)
                   'sdwan'     (SD-WAN tunnels, policies)
                   Omit to return the full multi-dimensional graph.
        site:      Restrict to devices at a specific site (slug).
        limit:     Maximum number of relationships to return (default 500).

    Returns:
        Cytoscape.js-compatible graph: {"nodes": [...], "edges": [...]}
        Each node has: id, label, type, platform, role, site, ...
        Each edge has: id, source, target, type, dimension, ...
    """
    try:
        from netcortex.graph.query import get_full_graph
        return await get_full_graph(dimension=dimension, site=site, limit=limit)
    except RuntimeError as exc:
        return {"error": str(exc), "nodes": [], "edges": []}
    except Exception as exc:
        log.error("mcp.get_network_graph.failed", error=str(exc))
        return {"error": f"Graph query failed: {exc}", "nodes": [], "edges": []}


async def get_device_context(device_name: str) -> dict:
    """Get full network context for a specific device.

    Returns a rich context object suitable for LLM consumption, including:
    - Device properties (platform, role, serial, management IP)
    - All directly connected neighbors (physical links)
    - Interfaces and their status
    - VLANs the device participates in
    - VRFs configured on the device
    - BGP peer relationships
    - SD-WAN tunnel endpoints

    Args:
        device_name: The device hostname as discovered (exact match or partial).

    Returns:
        {
            "device": {...},
            "neighbors": [...],
            "interfaces": [...],
            "vlans": [...],
            "vrfs": [...],
            "bgp_peers": [...],
            "sdwan_tunnels": [...],
            "graph": {"nodes": [...], "edges": [...]}
        }
    """
    try:
        from netcortex.graph.query import get_device_context as _ctx
        return await _ctx(device_name)
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.error("mcp.get_device_context.failed", device=device_name, error=str(exc))
        return {"error": f"Device context query failed: {exc}"}


async def find_path(src_device: str, dst_device: str, max_hops: int = 10) -> dict:
    """Find the shortest network path between two devices.

    Traverses all relationship types (physical, logical, routing, SD-WAN)
    to find the shortest path.

    Args:
        src_device: Source device hostname.
        dst_device: Destination device hostname.
        max_hops:   Maximum path length to consider (default 10).

    Returns:
        {
            "source": "...",
            "destination": "...",
            "hops": 3,
            "path": [
                {"node": {"name": "..."}, "via": "PHYSICAL_LINK"},
                ...
            ]
        }
    """
    try:
        from netcortex.graph.query import find_path as _find
        return await _find(src_device, dst_device, max_hops)
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.error("mcp.find_path.failed", src=src_device, dst=dst_device, error=str(exc))
        return {"error": f"Path query failed: {exc}"}


async def get_vlan_members(vid: int, site: str | None = None) -> dict:
    """Get all devices and interfaces that participate in a VLAN.

    Args:
        vid:  VLAN ID (1–4094).
        site: Optionally restrict to a site slug.

    Returns:
        {
            "vlan": {"vid": 100, "name": "..."},
            "members": [
                {"device": "...", "interface": "...", "role": "access|trunk|svi"}
            ]
        }
    """
    try:
        from netcortex.graph.client import get_driver
        driver = get_driver()
        async with driver.session() as session:
            site_filter = "AND d.site = $site" if site else ""
            result = await session.run(
                f"MATCH (v:VLAN {{vid: $vid}})<-[:LOGICAL_MEMBER]-(i:Interface)<-[:HAS_INTERFACE]-(d:Device) "
                f"WHERE 1=1 {site_filter} "
                f"RETURN v, d, i LIMIT 200",
                vid=vid,
                site=site,
            )
            records = await result.data()
            members = [
                {
                    "device": dict(r["d"]).get("name", ""),
                    "interface": dict(r["i"]).get("name", ""),
                }
                for r in records
            ]
            vlan_props = dict(records[0]["v"]) if records else {"vid": vid}
            return {"vlan": vlan_props, "members": members}
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.error("mcp.get_vlan_members.failed", vid=vid, error=str(exc))
        return {"error": f"VLAN query failed: {exc}"}


async def get_graph_summary() -> dict:
    """Return a high-level summary of the network graph.

    Useful for an LLM to understand the scale and composition of the network
    before diving into specific queries.

    Returns:
        {
            "total_devices": 42,
            "total_interfaces": 512,
            "total_vlans": 18,
            "total_links": 89,
            "adapters": ["meraki/corp", "nexus_dashboard/dc1"],
            "dimensions": ["physical", "logical", "routing"],
            "node_counts": {"Device": 42, "Interface": 512, ...},
        }
    """
    try:
        from netcortex.graph.query import get_graph_stats
        stats = await get_graph_stats()
        nodes = stats.get("nodes", {})
        rels = stats.get("relationships", {})
        return {
            "total_devices": nodes.get("Device", 0),
            "total_interfaces": nodes.get("Interface", 0),
            "total_vlans": nodes.get("VLAN", 0),
            "total_links": rels.get("PHYSICAL_LINK", 0),
            "node_counts": nodes,
            "relationship_counts": rels,
        }
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        log.error("mcp.get_graph_summary.failed", error=str(exc))
        return {"error": f"Stats query failed: {exc}"}
