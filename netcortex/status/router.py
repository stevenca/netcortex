"""Status page FastAPI router."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

import structlog

from netcortex import __version__
from netcortex.state import state

log = structlog.get_logger()
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _snmp_coverage_by_adapter() -> dict[str, tuple[int, int]]:
    """Query Neo4j for SNMP poll coverage grouped by source_adapter.

    Returns {adapter_instance_id: (ok_count, total_count)}.
    Both the web server and worker share Neo4j, so this works across containers.
    """
    try:
        from netcortex.graph.client import get_driver
        driver = get_driver()
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (d:Device)
                WHERE d.source_adapter IS NOT NULL AND d.source_adapter <> ''
                RETURN d.source_adapter AS adapter,
                       count(d) AS total,
                       count(CASE WHEN d.snmp_polled = true THEN 1 ELSE null END) AS ok
                """
            )
            return {r["adapter"]: (int(r["ok"]), int(r["total"])) async for r in result}
    except Exception as exc:
        log.debug("status.snmp_coverage.error", error=str(exc))
        return {}


def _apply_snmp_coverage(adapters, coverage: dict[str, tuple[int, int]]) -> None:
    """Overlay Neo4j SNMP coverage counts onto in-process AdapterStatus objects."""
    for a in adapters:
        if a.instance_id in coverage:
            ok, total = coverage[a.instance_id]
            a.snmp_ok = ok
            a.snmp_total = total


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def status_page(request: Request) -> HTMLResponse:
    """Render the NetCortex status dashboard."""
    adapters = state.sorted_adapters()
    _apply_snmp_coverage(adapters, await _snmp_coverage_by_adapter())
    adapter_counts = {
        "total": len(adapters),
        "connected": sum(1 for a in adapters if a.status == "connected"),
        "degraded":  sum(1 for a in adapters if a.status == "degraded"),
        "error":     sum(1 for a in adapters if a.status == "error"),
        "unknown":   sum(1 for a in adapters if a.status not in ("connected", "degraded", "error")),
    }
    # Overall adapter pill colour
    if adapter_counts["error"]:
        adapter_pill = "error"
    elif adapter_counts["degraded"]:
        adapter_pill = "degraded"
    elif adapter_counts["connected"]:
        adapter_pill = "connected"
    else:
        adapter_pill = "unknown"

    # Starlette 1.0+: request is the first arg; context must NOT include it
    context = {
        "version": __version__,
        "uptime": state.uptime_str(),
        # NetBox
        "netbox_status": state.netbox_status,
        "netbox_version": state.netbox_version,
        "netbox_message": state.netbox_message,
        # Neo4j
        "neo4j_status": state.neo4j_status,
        "neo4j_version": state.neo4j_version,
        "neo4j_message": state.neo4j_message,
        # Secret backend
        "secret_backend_status": state.secret_backend_status,
        "secret_backend_name": state.secret_backend_name,
        "secret_backend_message": state.secret_backend_message,
        # Redis
        "redis_status": state.redis_status,
        "redis_message": state.redis_message,
        # Graph stats — convert dict to list of tuples so Jinja2 can iterate it
        "graph_total_nodes": state.graph.total_nodes,
        "graph_total_rels": state.graph.total_relationships,
        "graph_last_ingest": state.graph.last_ingest_str,
        "graph_node_counts": list(state.graph.node_counts.items()),
        # Adapters — full list for table, aggregated counts for pill
        "adapters": adapters,
        "adapter_counts": adapter_counts,
        "adapter_pill": adapter_pill,
        # MCP transport — used by the "MCP" pill in the header
        "mcp_status":     state.mcp_status,
        "mcp_path":       state.mcp_path,
        "mcp_transport":  state.mcp_transport,
        "mcp_tool_count": state.mcp_tool_count,
        "mcp_message":    state.mcp_message,
        "refresh_interval": 30,
    }
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/api/status", tags=["system"])
async def api_status() -> dict:
    """Full machine-readable status."""
    adapters = state.sorted_adapters()
    _apply_snmp_coverage(adapters, await _snmp_coverage_by_adapter())
    return {
        "version": __version__,
        "uptime": state.uptime_str(),
        "netbox": {
            "status": state.netbox_status,
            "version": state.netbox_version,
            "message": state.netbox_message,
        },
        "neo4j": {
            "status": state.neo4j_status,
            "version": state.neo4j_version,
            "message": state.neo4j_message,
            "graph": {
                "nodes": state.graph.total_nodes,
                "relationships": state.graph.total_relationships,
                "last_ingest": state.graph.last_ingest_str,
                "node_counts": state.graph.node_counts,
            },
        },
        "secret_backend": {
            "status": state.secret_backend_status,
            "name": state.secret_backend_name,
            "message": state.secret_backend_message,
        },
        "redis": {
            "status": state.redis_status,
            "message": state.redis_message,
        },
        "adapters": {
            a.instance_id: {
                "type": a.adapter_type,
                "name": a.instance_name,
                "display_name": a.display_name,
                "status": a.status,
                "message": a.message,
                "last_checked": a.last_checked_str,
                "snmp_ok": a.snmp_ok,
                "snmp_total": a.snmp_total,
                "sync_running": a.sync_running,
            }
            for a in adapters
        },
        # MCP transport status — surfaced so the UI can paint the
        # "MCP" pill and so an operator can verify the endpoint is
        # actually mounted without curl-ing the streamable-http
        # path directly.
        "mcp": {
            "status":      state.mcp_status,
            "path":        state.mcp_path,
            "transport":   state.mcp_transport,
            "tool_count":  state.mcp_tool_count,
            "message":     state.mcp_message,
        },
    }
