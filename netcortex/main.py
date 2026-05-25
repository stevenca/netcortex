"""FastAPI application entry point."""

import asyncio
import time
import os
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from netcortex import __version__
from netcortex.config import init_settings, get_settings
from netcortex.state import AppState, AdapterStatus, state
from netcortex.status.router import router as status_router
from netcortex.webhooks.router import router as webhook_router

log = structlog.get_logger(__name__)


async def _check_redis(redis_url: str) -> tuple[str, str]:
    """Return (status, message) for Redis connectivity."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, socket_timeout=3)
        await r.ping()
        await r.aclose()
        return "connected", ""
    except Exception as exc:
        return "error", str(exc)


async def _probe_adapters() -> None:
    """Load all adapter instances and run health checks; populate state.adapters."""
    from netcortex.adapters import load_instances, get_instances, get_failed_instances
    from netcortex.state import AdapterStatus, state

    log.info("adapters.loading")
    try:
        await load_instances()
    except Exception as exc:
        log.error("adapters.load_failed", error=str(exc))
        return

    instances = get_instances()
    if not instances:
        log.warning("adapters.none_loaded",
                    hint="Add instances to netcortex/adapters/_index in your secret backend")

    # Register adapters that failed to load so they appear in the UI
    for instance_id, err_msg in get_failed_instances().items():
        adapter_type, _, instance_name = instance_id.partition("/")
        entry = AdapterStatus(
            instance_id=instance_id,
            adapter_type=adapter_type,
            instance_name=instance_name,
            display_name=adapter_type.replace("_", " ").title(),
            status="error",
            message=err_msg,
        )
        entry.last_checked = time.monotonic()
        state.adapters[instance_id] = entry

    checks = []
    for instance_id, adapter in instances.items():
        checks.append(_check_adapter(instance_id, adapter))
    await asyncio.gather(*checks, return_exceptions=True)


async def _check_adapter(instance_id: str, adapter) -> None:  # type: ignore[type-arg]
    """Run health check for one adapter and write result into state."""
    from netcortex.state import AdapterStatus, state
    entry = AdapterStatus(
        instance_id=instance_id,
        adapter_type=adapter.name,
        instance_name=adapter.instance_name,
        display_name=adapter.display_name,
        status="checking",
    )
    state.adapters[instance_id] = entry
    try:
        result = await adapter.health_check()
        hstatus = result.get("status", "unknown")
        entry.status = "connected" if hstatus == "ok" else hstatus
        entry.message = result.get("message", "")
    except Exception as exc:
        entry.status = "error"
        entry.message = str(exc)
    finally:
        entry.last_checked = time.monotonic()
    log.info("adapter.health", instance_id=instance_id, status=entry.status)


async def _refresh_adapter_health_loop(interval: int = 60) -> None:
    """Background task: re-check all adapter health every `interval` seconds."""
    from netcortex.adapters import get_instances, get_failed_instances
    from netcortex.state import AdapterStatus, state
    while True:
        await asyncio.sleep(interval)
        # Keep failed instances visible and up-to-date in the UI
        for instance_id, err_msg in get_failed_instances().items():
            if instance_id in state.adapters:
                state.adapters[instance_id].message = err_msg
            else:
                adapter_type, _, instance_name = instance_id.partition("/")
                entry = AdapterStatus(
                    instance_id=instance_id,
                    adapter_type=adapter_type,
                    instance_name=instance_name,
                    display_name=adapter_type.replace("_", " ").title(),
                    status="error",
                    message=err_msg,
                )
                entry.last_checked = time.monotonic()
                state.adapters[instance_id] = entry
        for instance_id, adapter in get_instances().items():
            try:
                await _check_adapter(instance_id, adapter)
            except Exception:
                pass


async def _refresh_graph_stats_loop(interval: int = 60) -> None:
    """Background task: refresh graph node/edge counts for the status page."""
    from netcortex.graph.query import get_graph_stats
    while True:
        await asyncio.sleep(interval)
        try:
            stats = await get_graph_stats()
            state.graph.node_counts = stats.get("nodes", {})
            state.graph.relationship_counts = stats.get("relationships", {})
        except Exception as exc:
            log.warning("graph.stats_refresh_failed", error=str(exc))


# ── MCP (Model Context Protocol) HTTP transport ──────────────────────────
#
# We mount the FastMCP HTTP/streamable-http app under ``/mcp`` so MCP
# clients (Cursor, Claude Desktop, custom agents) can reach the
# agentic-ops tool surface without standing up a separate stdio
# process — the container already runs uvicorn, so adding ``/mcp`` to
# the same listener costs us nothing.
#
# Setting ``NETCORTEX_MCP_ENABLED=0`` in the env disables the transport
# entirely (still leaves the in-process tool registry available for
# tests or in-process callers).
#
# The MCP path is configurable via ``NETCORTEX_MCP_PATH`` (default
# ``/mcp``) so operators can move it behind their own reverse proxy
# rule without code changes.
_MCP_ENABLED = os.environ.get("NETCORTEX_MCP_ENABLED", "1") not in ("0", "false", "False")
_MCP_PATH    = os.environ.get("NETCORTEX_MCP_PATH", "/mcp").rstrip("/") or "/mcp"


class _MCPBearerAuth:
    """ASGI middleware: validate ``Authorization: Bearer <mcp_secret>``.

    - If ``mcp_secret`` is empty (not set in the core secret) the middleware
      is a no-op so local/dev environments can connect without a token.
    - The secret is read lazily at request time so it picks up whatever
      value ``init_settings()`` loaded from AWS SM / Vault during startup,
      without needing the config to be ready at module-import time.
    - Constant-time comparison (``hmac.compare_digest``) prevents
      timing-oracle attacks on the token.
    """

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        import hmac
        from starlette.responses import Response

        if scope["type"] in ("http", "websocket"):
            try:
                from netcortex.config import get_settings
                secret = get_settings().mcp_secret or ""
            except RuntimeError:
                secret = ""

            if secret:
                headers = dict(scope.get("headers", []))
                auth_header = headers.get(b"authorization", b"").decode()
                token = auth_header.removeprefix("Bearer ").strip()
                if not hmac.compare_digest(token, secret):
                    resp = Response(
                        "Unauthorized",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="NetCortex MCP"'},
                    )
                    await resp(scope, receive, send)
                    return

        await self.app(scope, receive, send)


_mcp_app = None
if _MCP_ENABLED:
    try:
        # Import for the side-effect of registering every tool
        # decorator before we materialise the Starlette app.
        from netcortex.mcp.server import mcp as _mcp_instance
        # path="/" because the mount prefix already provides the
        # ``/mcp`` segment — relative path keeps the routes clean.
        _raw_mcp_app = _mcp_instance.http_app(path="/", transport="http")
        _mcp_app = _MCPBearerAuth(_raw_mcp_app)
    except Exception as exc:
        log.error("netcortex.mcp.init_failed", error=str(exc))
        state.mcp_status = "error"
        state.mcp_message = str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    log.info("netcortex.startup", version=__version__)
    state.version = __version__

    # ── Phase 2: pull config from secret backend ──────────────────────────
    # If the secret backend is unavailable we fall back to env-var defaults
    # so that Neo4j and Redis (which have env overrides in docker-compose)
    # can still connect and the status page remains useful.
    cfg = None
    try:
        await init_settings()
        cfg = get_settings()
        state.secret_backend_status = "connected"
        state.secret_backend_name = cfg.bootstrap.secret_backend
    except Exception as exc:
        state.secret_backend_status = "error"
        state.secret_backend_message = str(exc)
        log.warning("netcortex.config_partial", error=str(exc),
                    hint="Falling back to env-var defaults for Neo4j/Redis")

    # ── Neo4j connectivity + schema setup ─────────────────────────────────
    # Read URI/creds from cfg if available, otherwise fall back to env vars.
    from netcortex.graph import client as neo4j_client
    from netcortex.graph.schema import setup_schema

    neo4j_uri  = (cfg.neo4j_uri      if cfg else None) or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = (cfg.neo4j_user     if cfg else None) or os.environ.get("NEO4J_USER", "neo4j")
    neo4j_pass = (cfg.neo4j_password if cfg else None) or os.environ.get("NEO4J_PASSWORD", "netcortex")

    neo4j_result = await neo4j_client.check_connectivity(neo4j_uri, neo4j_user, neo4j_pass)
    state.neo4j_status  = neo4j_result["status"]
    state.neo4j_version = neo4j_result.get("neo4j_version", "")
    state.neo4j_message = neo4j_result.get("message", "")

    if state.neo4j_status == "connected":
        try:
            await neo4j_client.init_client(neo4j_uri, neo4j_user, neo4j_pass)
            await setup_schema()
        except Exception as exc:
            state.neo4j_status = "error"
            state.neo4j_message = str(exc)
            log.error("neo4j.schema_setup_failed", error=str(exc))

    # ── NetBox connectivity ───────────────────────────────────────────────
    if cfg:
        from netcortex import netbox as nb_module
        nb_result = await nb_module.check_connectivity(cfg.netbox_url, cfg.netbox_token)
        state.netbox_status  = nb_result["status"]
        state.netbox_version = nb_result.get("netbox_version", "")
        state.netbox_message = nb_result.get("message", "")
        if state.netbox_status == "connected":
            try:
                await nb_module.init_client(cfg.netbox_url, cfg.netbox_token)
            except Exception as exc:
                state.netbox_status = "error"
                state.netbox_message = str(exc)

    # ── Redis connectivity ────────────────────────────────────────────────
    redis_url = (cfg.redis_url if cfg else None) or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_status, redis_msg = await _check_redis(redis_url)
    state.redis_status  = redis_status
    state.redis_message = redis_msg

    # ── Load and health-check adapters ───────────────────────────────────
    if cfg:
        await _probe_adapters()

    # ── Background tasks ──────────────────────────────────────────────────
    refresh_adapter_task = asyncio.create_task(_refresh_adapter_health_loop(interval=60))
    refresh_graph_task = asyncio.create_task(_refresh_graph_stats_loop(interval=60))

    # ── MCP transport readiness ───────────────────────────────────────────
    # The FastMCP HTTP app is *mounted* below (see ``app.mount(_MCP_PATH, ...)``);
    # here we just probe the in-process tool registry so the status pill
    # can render the tool count without an extra HTTP round-trip.
    if _MCP_ENABLED and _mcp_app is not None:
        try:
            from netcortex.mcp.server import mcp as _mcp_instance
            tools = await _mcp_instance.list_tools()
            state.mcp_status      = "enabled"
            state.mcp_path        = _MCP_PATH
            state.mcp_transport   = "streamable-http"
            state.mcp_tool_count  = len(tools)
            state.mcp_message     = ""
            log.info("netcortex.mcp.ready",
                     path=_MCP_PATH, transport="streamable-http",
                     tools=state.mcp_tool_count)
        except Exception as exc:
            state.mcp_status  = "error"
            state.mcp_message = str(exc)
            log.error("netcortex.mcp.tool_probe_failed", error=str(exc))
    elif not _MCP_ENABLED:
        state.mcp_status = "disabled"

    log.info(
        "netcortex.ready",
        netbox=state.netbox_status,
        neo4j=state.neo4j_status,
        adapters=len(state.adapters),
        secret_backend=state.secret_backend_name,
        mcp=state.mcp_status,
    )

    # ── Nest the mounted MCP app's lifespan so its session manager
    # ── starts/stops cleanly with the FastAPI app.  Starlette doesn't
    # ── propagate lifespans into mounted sub-apps automatically.
    # _mcp_app is _MCPBearerAuth wrapping _raw_mcp_app; lifespan is on
    # the inner FastMCP ASGI app.
    if _mcp_app is not None:
        async with _raw_mcp_app.lifespan(app):
            yield
    else:
        yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    refresh_adapter_task.cancel()
    refresh_graph_task.cancel()
    await neo4j_client.close()
    log.info("netcortex.shutdown")


app = FastAPI(
    title="NetCortex",
    description="Multi-dimensional network graph with NetBox as SoT.",
    version=__version__,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # MCP clients (Cursor / Claude / web agents) preflight with OPTIONS
    # and DELETE (DELETE is used to terminate streamable-http sessions),
    # so the public CORS surface needs to include those verbs.
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["*"],
)

# Mount the MCP HTTP transport.  This MUST happen AFTER ``app`` is
# created but BEFORE the catch-all routes — Starlette resolves mounts
# in registration order.  Sub-app's lifespan is composed into the
# parent's lifespan above so the session manager starts cleanly.
if _mcp_app is not None:
    app.mount(_MCP_PATH, _mcp_app)
    log.info("netcortex.mcp.mounted", path=_MCP_PATH)


# Per-endpoint time budget.  Any /api/* request that runs longer than the
# bucket-specific budget is force-aborted with HTTP 503 so a single bad
# query cannot exhaust the worker pool.
_QUERY_BUDGETS_S: dict[str, float] = {
    "/api/graph/aggregated": 5.0,
    # First cold-cache call can take 20-30 s on a large graph; raise budget
    # accordingly. Subsequent calls return from the 30-second in-process cache
    # in < 1 ms, so the elevated budget only burns on genuine cold-starts.
    "/api/graph":            45.0,
    "/api/graph/device":     15.0,
    "/api/graph/path":       15.0,
    "/api/graph/stp":        15.0,
    "/api/graph/routing":    15.0,
    "/api/graph/vlans":      15.0,
    "/api/graph/mac-table":  15.0,
    "/api/graph/stats":      5.0,
    "/api/inventory":        15.0,
    "/api/filter-catalog":   5.0,
    "/api/cam":              15.0,
    "/api/links":            15.0,
}
_DEFAULT_QUERY_BUDGET_S = 30.0


@app.middleware("http")
async def _query_budget(request, call_next):
    from starlette.responses import JSONResponse
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    budget = _DEFAULT_QUERY_BUDGET_S
    for prefix, val in _QUERY_BUDGETS_S.items():
        if path == prefix or path.startswith(prefix + "/"):
            budget = val
            break
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(call_next(request), timeout=budget)
    except asyncio.TimeoutError:
        log.warning("api.query_budget.exceeded",
                    path=path, budget_s=budget,
                    elapsed_s=round(time.monotonic() - started, 2))
        return JSONResponse(
            status_code=503,
            content={
                "error": "query_budget_exceeded",
                "budget_s": budget,
                "hint": "Try /api/graph/aggregated for a lighter view, or add filters.",
            },
        )
    elapsed = time.monotonic() - started
    response.headers["X-Elapsed-S"] = f"{elapsed:.3f}"
    response.headers["X-Budget-S"] = f"{budget:.1f}"
    # Slow-query warning (>50% of budget)
    if elapsed > budget * 0.5:
        log.info("api.slow_query", path=path, elapsed_s=round(elapsed, 2),
                 budget_s=budget)
    return response


app.include_router(status_router)
app.include_router(webhook_router)


@app.post("/api/graph/cache/invalidate", tags=["graph"], status_code=200)
async def invalidate_graph_cache() -> dict[str, int]:
    """Clear the in-process full-graph result cache.

    Call this after a large sync completes so the next topology request
    fetches fresh data from Neo4j instead of serving the cached snapshot.
    The cache self-expires in 30 seconds; this endpoint forces immediate
    expiry.
    """
    from netcortex.graph.query import _GRAPH_CACHE
    n = len(_GRAPH_CACHE)
    _GRAPH_CACHE.clear()
    log.info("api.graph_cache.invalidated", entries_cleared=n)
    return {"cleared": n}


# ── Prometheus-style /metrics endpoint ────────────────────────────────────────
#
# We emit a small, hand-rolled exposition (no prometheus_client dep) so this
# stays light and dependency-free.  Add Prometheus + Grafana to the compose
# stack to scrape it as part of Phase B observability.

# Counters: request totals and outcomes
_REQ_COUNT: dict[tuple[str, str], int] = {}    # (path, status)
_REQ_LATENCY_SUM: dict[str, float] = {}        # path -> total seconds
_REQ_LATENCY_COUNT: dict[str, int] = {}        # path -> count


@app.middleware("http")
async def _metrics(request, call_next):
    started = time.monotonic()
    path = request.url.path
    # Normalize parameterized paths so we don't blow up the cardinality.
    norm = path.split("?")[0]
    if norm.startswith("/api/graph/device/"):
        norm = "/api/graph/device/{name}"
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        status = "500"
        raise
    finally:
        elapsed = time.monotonic() - started
        key = (norm, status)
        _REQ_COUNT[key] = _REQ_COUNT.get(key, 0) + 1
        _REQ_LATENCY_SUM[norm] = _REQ_LATENCY_SUM.get(norm, 0.0) + elapsed
        _REQ_LATENCY_COUNT[norm] = _REQ_LATENCY_COUNT.get(norm, 0) + 1
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics() -> "starlette.responses.PlainTextResponse":  # type: ignore[name-defined]
    """Prometheus exposition format.  No external library required."""
    from starlette.responses import PlainTextResponse

    lines: list[str] = []

    lines.append("# HELP netcortex_http_requests_total HTTP requests handled.")
    lines.append("# TYPE netcortex_http_requests_total counter")
    for (path, status), n in _REQ_COUNT.items():
        # Escape any reserved characters in path
        safe_path = path.replace('"', '\\"')
        lines.append(
            f'netcortex_http_requests_total{{path="{safe_path}",status="{status}"}} {n}'
        )

    lines.append("# HELP netcortex_http_request_seconds Sum of request latencies.")
    lines.append("# TYPE netcortex_http_request_seconds summary")
    for path, total in _REQ_LATENCY_SUM.items():
        safe_path = path.replace('"', '\\"')
        count = _REQ_LATENCY_COUNT.get(path, 0)
        lines.append(
            f'netcortex_http_request_seconds_sum{{path="{safe_path}"}} {total:.6f}'
        )
        lines.append(
            f'netcortex_http_request_seconds_count{{path="{safe_path}"}} {count}'
        )

    # Adapter status snapshot
    lines.append("# HELP netcortex_adapter_status Adapter health: 1=connected, 0=else.")
    lines.append("# TYPE netcortex_adapter_status gauge")
    for instance_id, adapter in state.adapters.items():
        safe_inst = instance_id.replace('"', '\\"')
        val = 1 if adapter.status == "connected" else 0
        lines.append(
            f'netcortex_adapter_status{{instance="{safe_inst}",type="{adapter.adapter_type}"}} {val}'
        )

    # Neo4j status
    lines.append("# HELP netcortex_neo4j_connected 1 if Neo4j is connected.")
    lines.append("# TYPE netcortex_neo4j_connected gauge")
    lines.append(f'netcortex_neo4j_connected {{}} '
                 f'{1 if state.neo4j_status == "connected" else 0}')

    return PlainTextResponse("\n".join(lines) + "\n",
                              media_type="text/plain; version=0.0.4")


# ── Adapter refresh / sync endpoints ─────────────────────────────────────────

@app.post("/api/adapters/refresh", tags=["system"])
async def refresh_adapters() -> dict:
    """Trigger an immediate re-check of all adapter health statuses."""
    asyncio.create_task(_probe_adapters())
    return {"status": "refreshing", "adapters": len(state.adapters)}


@app.post("/api/adapters/sync", tags=["system"])
async def sync_adapters_now() -> dict:
    """Trigger an immediate full discovery + ingest cycle for all adapters.

    Runs in the background — returns immediately.  Poll /api/status or
    /api/graph afterwards to see the results.
    """
    asyncio.create_task(_run_discovery_cycle())
    return {"status": "started", "adapters": len(state.adapters)}


@app.post("/api/adapters/{adapter_type}/{instance_name}/sync", tags=["system"])
async def sync_adapter_instance_now(adapter_type: str, instance_name: str) -> dict:
    """Trigger an immediate discover+ingest cycle for one adapter instance."""
    from netcortex.adapters import get_instances

    instance_id = f"{adapter_type}/{instance_name}"
    if instance_id not in get_instances():
        raise HTTPException(
            status_code=404,
            detail=f"adapter instance not found: {instance_id}",
        )
    asyncio.create_task(_run_discovery_cycle(instance_ids={instance_id}))
    return {"status": "started", "instance_id": instance_id}


async def _run_discovery_cycle(instance_ids: set[str] | None = None) -> None:
    """Run discover → ingest for selected adapters, then correlation."""
    from netcortex.adapters import get_instances
    from netcortex.graph.ingest import ingest_graph_data
    from netcortex.graph.correlate import run_correlation
    from netcortex.graph.site_correlate import run_site_correlation

    all_instances = get_instances()
    if instance_ids:
        instances = {
            iid: adapter
            for iid, adapter in all_instances.items()
            if iid in instance_ids
        }
    else:
        instances = all_instances

    log.info("manual_sync.start", adapters=len(instances), scope=sorted(instances.keys()))

    # Expose "running" state to the status API/UI so each adapter row can
    # show an in-flight indicator while discover() is executing.
    for iid in instances:
        if iid in state.adapters:
            state.adapters[iid].sync_running = True

    async def _one(iid: str, adapter) -> None:  # type: ignore[type-arg]
        try:
            data = await adapter.discover()
            await ingest_graph_data(data)
            log.info("manual_sync.adapter_done", instance=iid,
                     nodes=len(data.nodes), edges=len(data.edges))
        except NotImplementedError:
            log.debug("manual_sync.not_implemented", instance=iid)
        except Exception as exc:
            log.error("manual_sync.adapter_failed", instance=iid, error=str(exc))
        finally:
            if iid in state.adapters:
                state.adapters[iid].sync_running = False

    await asyncio.gather(*[_one(iid, a) for iid, a in instances.items()])

    # Run correlations after all adapters complete
    try:
        await run_correlation()
        await run_site_correlation()
    except Exception as exc:
        log.warning("manual_sync.correlation_failed", error=str(exc))

    log.info("manual_sync.done")


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health() -> dict:
    """Health check for Docker/load balancer probes."""
    overall = (
        "healthy"
        if state.neo4j_status == "connected"
        else "degraded"
    )
    return {
        "status": overall,
        "version": __version__,
        "netbox": state.netbox_status,
        "neo4j": state.neo4j_status,
        "redis": state.redis_status,
        "secret_backend": state.secret_backend_status,
        "adapters": {
            iid: a.status for iid, a in state.adapters.items()
        },
    }


# ── Graph API endpoints ───────────────────────────────────────────────────────

@app.get("/api/graph", tags=["graph"])
async def get_graph(
    dimension: Annotated[str | None, Query(description="[legacy] physical|logical|routing|sdwan|fabric|stp|virtual")] = None,
    overlay: Annotated[list[str] | None, Query(
        description="Overlay name(s). Repeatable. Union of selected overlays' edge types. "
                    "Valid: physical, l2, l3, sdwan, fabric, virtual.")] = None,
    strict_overlays: Annotated[bool, Query(
        description="When true, the response is bounded exactly by the overlay selection — "
                    "an empty selection returns NO edges (nodes only). Default false "
                    "preserves the legacy 'no overlay = full graph' behavior for "
                    "non-UI clients.")] = False,
    collapse_l3_on_physical: Annotated[bool, Query(
        description="When true, fold routing/BGP adjacencies onto the PHYSICAL_LINK "
                    "edge that carries them (carries[] annotation). Multi-hop / SVI-only "
                    "adjacencies are left as standalone edges. The UI sets this when "
                    "both 'physical' and 'l3' overlays are active.")] = False,
    site: Annotated[str | None, Query(description="Filter by site slug")] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 2000,
    include_interfaces: Annotated[bool, Query(description="Include Interface nodes (default: false)")] = False,
    include_mac_nodes: Annotated[bool, Query(description="Include MACAddress/ARPEntry nodes")] = False,
    max_nodes: Annotated[int, Query(ge=100, le=10000,
        description="Hard ceiling on returned nodes. Returns truncated=True when exceeded.")] = 2000,
) -> dict:
    """Return the network graph in Cytoscape.js format.

    Use ``overlay`` (repeatable) for multi-layer rendering, e.g.
    ``?overlay=physical&overlay=l3``. The legacy single-layer
    ``dimension`` parameter is still accepted but ignored when
    ``overlay`` is set.

    Omit both to return every non-structural edge type.
    By default Interface, MACAddress, and ARPEntry nodes are hidden so the
    topology shows Device-level connectivity only. Set ``include_interfaces=true``
    to reveal port-level detail.
    """
    if state.neo4j_status != "connected":
        return {"nodes": [], "edges": [], "error": "Neo4j not connected"}
    from netcortex.graph.query import get_full_graph
    result = await get_full_graph(
        dimension=dimension,
        overlays=overlay,
        strict_overlays=strict_overlays,
        collapse_l3_on_physical=collapse_l3_on_physical,
        site=site,
        limit=limit,
        include_interfaces=include_interfaces,
        include_mac_nodes=include_mac_nodes,
    )
    # Browser-renderer scale guard: Cytoscape.js fcose layout chokes above
    # ~3000 elements.  Truncate before sending so we never spin the browser.
    nodes = result.get("nodes", []) or []
    edges = result.get("edges", []) or []
    truncated = False
    if len(nodes) > max_nodes:
        truncated = True
        kept_ids = {n.get("data", {}).get("id") for n in nodes[:max_nodes]}
        nodes = nodes[:max_nodes]
        edges = [
            e for e in edges
            if e.get("data", {}).get("source") in kept_ids
            and e.get("data", {}).get("target") in kept_ids
        ]
    result["nodes"] = nodes
    result["edges"] = edges
    result["truncated"] = truncated
    result["limits"] = {
        "max_nodes": max_nodes,
        "returned_nodes": len(nodes),
        "returned_edges": len(edges),
    }
    if truncated:
        log.info("api.graph.truncated",
                 dimension=dimension, max_nodes=max_nodes,
                 returned=len(nodes))
    return result


@app.get("/api/graph/overlays", tags=["graph"])
async def get_graph_overlays() -> dict:
    """Return the catalog of available topology overlays.

    Each entry is ``{id, rel_types}``. The UI uses this to render a
    toggle for every server-side overlay without hard-coding the list.
    """
    from netcortex.graph.query import list_overlays
    return {"overlays": list_overlays()}


@app.get("/api/ingest/stats", tags=["graph"])
async def get_ingest_stats() -> dict:
    """Return current Redis Streams ingest queue stats."""
    try:
        from netcortex.ingest.queue import stream_stats
        return await stream_stats()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/graph/aggregated", tags=["graph"])
async def get_aggregated_graph(
    level: Annotated[str, Query(description="site|adapter|dimension")] = "site",
    dimension: Annotated[str | None, Query()] = None,
) -> dict:
    """Aggregated topology — small (<200 element) summary view safe for any browser.

    Use as the default landing view; drill into a specific bubble via
    /api/graph?site=<id> (filters to one container) or
    /api/graph/device/<name> (one-device subgraph) for detail.
    """
    if state.neo4j_status != "connected":
        return {"nodes": [], "edges": [], "error": "Neo4j not connected"}
    from netcortex.graph.query import get_aggregated_topology
    return await get_aggregated_topology(level=level, dimension=dimension)


@app.get("/api/graph/device/{device_name}", tags=["graph"])
async def get_device_graph(device_name: str) -> dict:
    """Return the local subgraph for a specific device (2-hop neighbourhood)."""
    if state.neo4j_status != "connected":
        return {"error": "Neo4j not connected"}
    from netcortex.graph.query import get_device_context
    return await get_device_context(device_name)


@app.get("/api/graph/mac-table", tags=["graph"])
async def get_mac_table(
    device: Annotated[str | None, Query(description="Filter by device name")] = None,
    mac: Annotated[str | None, Query(description="Filter by MAC address")] = None,
) -> dict:
    """Return consolidated MAC address table entries from the graph."""
    if state.neo4j_status != "connected":
        return {"entries": [], "error": "Neo4j not connected"}
    from netcortex.graph.query import get_mac_table
    return await get_mac_table(device_name=device, mac=mac)


@app.get("/api/graph/correlation", tags=["graph"])
async def get_correlation_stats() -> dict:
    """Return statistics about correlated vs adapter-discovered physical links."""
    if state.neo4j_status != "connected":
        return {"error": "Neo4j not connected"}
    from netcortex.graph.correlate import get_correlation_stats
    return await get_correlation_stats()


@app.get("/api/graph/path", tags=["graph"])
async def get_path(
    src: Annotated[str, Query(description="Source device name")],
    dst: Annotated[str, Query(description="Destination device name")],
    max_hops: Annotated[int, Query(ge=1, le=20)] = 10,
) -> dict:
    """Find the shortest path between two devices in the graph."""
    if state.neo4j_status != "connected":
        return {"error": "Neo4j not connected"}
    from netcortex.graph.query import find_path
    return await find_path(src, dst, max_hops)


@app.get("/api/graph/stats", tags=["graph"])
async def get_graph_stats_endpoint() -> dict:
    """Return graph node and relationship counts."""
    if state.neo4j_status != "connected":
        return {"error": "Neo4j not connected"}
    from netcortex.graph.query import get_graph_stats
    return await get_graph_stats()


@app.get("/api/inventory", tags=["graph"])
async def get_inventory_endpoint() -> dict:
    """Return flat device inventory list for the inventory table view."""
    if state.neo4j_status != "connected":
        return {"devices": [], "count": 0, "error": "Neo4j not connected"}
    from netcortex.graph.query import get_inventory
    return await get_inventory()


@app.get("/api/filter-catalog", tags=["graph"])
async def get_filter_catalog_endpoint() -> dict:
    """Return the slim catalog (sites + devices) that powers the UI
    chip filter on the Topology, Inventory, MAC/ARP, STP, and Routing
    views.

    Cached implicitly by the browser for the page lifetime (the chip
    filter only fetches this on first selector open).  See
    ``netcortex.graph.query.get_filter_catalog`` for the schema.
    """
    if state.neo4j_status != "connected":
        return {"sites": [], "devices": [],
                "counts": {"sites": 0, "devices": 0},
                "error": "Neo4j not connected"}
    from netcortex.graph.query import get_filter_catalog
    return await get_filter_catalog()


@app.get("/api/devices/{device_key:path}/explorer", tags=["graph"])
async def get_device_explorer_endpoint(device_key: str) -> dict:
    """Return everything the graph knows about one device — for the
    per-device debug/explorer UI.

    ``device_key`` may be either a Device.id (``meraki:Q5TY-EB22-LPZG``) or
    a hostname (``cpn-ful-cat9k1``, ``cpn-ful-cat9k1.ciscops.net``).
    """
    if state.neo4j_status != "connected":
        return {"error": "Neo4j not connected"}
    from netcortex.graph.query import get_device_explorer
    # Strip leading/trailing whitespace; FastAPI already URL-decodes
    # the path segment, so colons in ids like "meraki:Q5..." round-trip OK.
    return await get_device_explorer(device_key.strip())


@app.get("/api/cam", tags=["graph"])
async def get_cam_endpoint() -> dict:
    """Return correlated CAM/ARP table — MAC addresses with port, device, and IP context."""
    if state.neo4j_status != "connected":
        return {"entries": [], "count": 0, "error": "Neo4j not connected"}
    from netcortex.graph.query import get_cam_correlated
    return await get_cam_correlated()


@app.get("/api/graph/stp", tags=["graph"])
async def get_stp_topology_endpoint() -> dict:
    """Return STP topology — domains, root bridges, and port states/roles."""
    if state.neo4j_status != "connected":
        return {"domains": [], "count": 0, "error": "Neo4j not connected"}
    from netcortex.graph.query import get_stp_topology
    return await get_stp_topology()


@app.get("/api/graph/routing", tags=["graph"])
async def get_routing_topology_endpoint() -> dict:
    """Return L3 routing topology — prefixes, attached devices, and routing peers."""
    if state.neo4j_status != "connected":
        return {"prefixes": [], "peers": [], "error": "Neo4j not connected"}
    from netcortex.graph.query import get_routing_topology
    return await get_routing_topology()


@app.get("/api/graph/vlans", tags=["graph"])
async def get_vlans_endpoint(site: str | None = None, device: str | None = None) -> dict:
    """Return VLAN table rows with optional site/device filters."""
    if state.neo4j_status != "connected":
        return {"vlans": [], "count": 0, "error": "Neo4j not connected"}
    from netcortex.graph.query import get_vlans
    return await get_vlans(site=site, device=device)


@app.get("/api/links", tags=["graph"])
async def get_links_endpoint() -> dict:
    """Return a flat list of every transit edge (PHYSICAL_LINK / WAN_UPLINK
    / SDWAN_TUNNEL / VXLAN_TUNNEL) for the Links table view.

    Server-side sorted by flap score → recency → health so the most
    operationally interesting rows surface first.  Carries the full
    ``oper_status_history`` so the UI can render its connectivity
    strip inline per row.  See ``netcortex.graph.query.get_links``
    for the schema.
    """
    if state.neo4j_status != "connected":
        return {"links": [], "count": 0, "type_counts": {},
                "error": "Neo4j not connected"}
    from netcortex.graph.query import get_links
    return await get_links()
