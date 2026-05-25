"""Shared in-process application state.

All mutable runtime state lives here so the status router, lifespan,
and background tasks all read from the same place without circular imports.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterStatus:
    instance_id: str          # e.g. "meraki/corp"
    adapter_type: str         # e.g. "meraki"
    instance_name: str        # e.g. "corp"
    display_name: str         # e.g. "Cisco Meraki"
    status: str = "unknown"   # "connected" | "error" | "unknown"
    message: str = ""         # error detail or empty string
    last_checked: float = 0.0 # monotonic timestamp
    # SNMP polling coverage — populated by the SNMP adapter after each discover cycle
    snmp_ok: int = 0          # devices that responded to SNMP in the last cycle
    snmp_total: int = 0       # total devices owned by this adapter that were attempted
    # True while a manual "sync now" discover job is actively running.
    sync_running: bool = False

    @property
    def last_checked_str(self) -> str:
        if self.last_checked == 0:
            return "never"
        elapsed = int(time.monotonic() - self.last_checked)
        if elapsed < 60:
            return f"{elapsed}s ago"
        if elapsed < 3600:
            return f"{elapsed // 60}m ago"
        return f"{elapsed // 3600}h ago"


@dataclass
class GraphStats:
    """Snapshot of graph counts reported on the status page."""
    node_counts: dict[str, int] = field(default_factory=dict)
    relationship_counts: dict[str, int] = field(default_factory=dict)
    last_ingest: float = 0.0

    @property
    def total_nodes(self) -> int:
        return sum(self.node_counts.values())

    @property
    def total_relationships(self) -> int:
        return sum(self.relationship_counts.values())

    @property
    def last_ingest_str(self) -> str:
        if self.last_ingest == 0:
            return "never"
        elapsed = int(time.monotonic() - self.last_ingest)
        if elapsed < 60:
            return f"{elapsed}s ago"
        if elapsed < 3600:
            return f"{elapsed // 60}m ago"
        return f"{elapsed // 3600}h ago"


@dataclass
class AppState:
    netbox_status: str = "unknown"      # "connected" | "error" | "unknown"
    netbox_message: str = ""
    netbox_version: str = ""
    neo4j_status: str = "unknown"       # "connected" | "error" | "unknown"
    neo4j_message: str = ""
    neo4j_version: str = ""
    secret_backend_status: str = "unknown"
    secret_backend_name: str = ""
    secret_backend_message: str = ""
    redis_status: str = "unknown"
    redis_message: str = ""
    adapters: dict[str, AdapterStatus] = field(default_factory=dict)
    graph: GraphStats = field(default_factory=GraphStats)
    startup_time: float = field(default_factory=time.monotonic)
    version: str = "0.1.0"
    # ── MCP (Model Context Protocol) HTTP transport ─────────────────────
    # Populated by ``netcortex.main`` when the FastMCP HTTP app is
    # mounted onto the FastAPI tree at startup.  ``mcp_path`` is the URL
    # prefix the transport listens on (e.g. ``/mcp``); ``mcp_tool_count``
    # is the number of tools the agent surface exposes.  ``mcp_status``
    # is one of ``enabled`` / ``disabled`` / ``error``.
    mcp_status: str = "disabled"
    mcp_path: str = ""
    mcp_tool_count: int = 0
    mcp_transport: str = ""
    mcp_message: str = ""

    def uptime_str(self) -> str:
        elapsed = int(time.monotonic() - self.startup_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def sorted_adapters(self) -> list[AdapterStatus]:
        return sorted(self.adapters.values(), key=lambda a: a.instance_id)


# Singleton
state = AppState()
