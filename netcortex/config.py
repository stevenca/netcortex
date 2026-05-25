"""NetCortex configuration.

Two-phase startup:
  Phase 1 — Bootstrap: read only what's needed to reach the secret backend
             from environment variables. This is intentionally minimal.
  Phase 2 — Hydrate: pull the rest of the config from the secret backend
             (netcortex/core secret) and merge into Settings.

The Settings object is available immediately after import (Phase 1 only).
Call await Settings.hydrate() once at startup to complete Phase 2.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Phase 1: Bootstrap settings — sourced from env only
# ---------------------------------------------------------------------------

class BootstrapSettings(BaseSettings):
    """
    Minimal env vars needed to locate and authenticate to the secret backend.
    Everything else comes from the backend itself.

    These are the ONLY values that must be in environment / Docker secrets.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Which backend to use
    secret_backend: str  # "aws_sm" | "vault"

    # AWS SM
    aws_region: str | None = None
    aws_sm_endpoint_url: str | None = None  # LocalStack / testing

    # Vault
    vault_addr: str | None = None
    vault_mount: str = "secret"
    vault_token: str | None = None
    vault_role_id: str | None = None
    vault_secret_id: str | None = None
    vault_auth_method: str = "token"
    vault_aws_role: str | None = None
    vault_k8s_role: str | None = None
    vault_skip_verify: bool = False

    # Secret path prefix (default: "netcortex")
    nc_secret_prefix: str = "netcortex"
    # Cache TTL for secrets in seconds
    nc_secret_cache_ttl: int = 300


# ---------------------------------------------------------------------------
# Phase 2: Full runtime settings — sourced from secret backend (core secret)
# ---------------------------------------------------------------------------

class Settings:
    """
    Full NetCortex runtime configuration.

    Instantiate with Settings.create() at application startup.
    After creation, all attributes are populated from the secret backend.

    Secret layout (at prefix netcortex/core):
    {
        "netbox_url": "https://netbox.example.com",
        "netbox_token": "...",
        "netbox_verify_ssl": true,              # optional, secure default
        "mcp_secret": "...",
        "redis_url": "redis://redis:6379/0",   # optional
        "log_level": "INFO",                    # optional
        "log_format": "json",                   # optional
        "mcp_transport": "http",                # optional
        "sync_backend": "apscheduler",          # optional
        "sync_conflict_policy": "alert",        # optional
        "sync_interval_meraki": 3600,            # optional
        "top_problems_stale_after_seconds": 86400,   # optional, 24 h default
        "top_problems_stale_severity": "info",       # optional, critical|warning|info|filter
        ...
    }
    """

    # NetBox
    netbox_url: str
    netbox_token: str
    netbox_verify_ssl: bool

    # Neo4j graph database
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    # Redis
    redis_url: str

    # MCP
    mcp_transport: str
    mcp_secret: str

    # Sync engine
    sync_backend: str
    sync_conflict_policy: str
    # Global fallback — applies to every adapter type unless overridden per-type.
    # Set "sync_interval" in the core secret to change all at once.
    sync_interval: int
    sync_interval_meraki: int
    sync_interval_catalyst_center: int
    sync_interval_nexus_dashboard: int
    sync_interval_intersight: int
    sync_interval_snmp: int
    sync_interval_generic_rest: int
    sync_interval_netbox_sites: int

    # Access layer
    access_log_commands: bool
    ssh_timeout: int
    netconf_port: int
    restconf_port: int

    # Logging
    log_level: str
    log_format: str

    # Status page
    status_refresh_interval: int

    # Top-problems noise-suppression policy.
    #
    # Some cloud-managed adapters (notably Meraki Dashboard) keep
    # reporting a "down" oper status for devices that have actually
    # been claimed-but-never-deployed (e.g. a spare MX75 that sat in
    # a closet for 18 months without ever calling home).  Those show
    # up in `top_problems` as `critical` link_down events even though
    # there is no real outage to act on, drowning out genuinely
    # actionable signals.
    #
    # The two knobs below give operators a single, source-agnostic
    # way to suppress that noise without losing visibility.  A
    # problem is considered "stale" when its underlying device has
    # not reported to its source-of-truth (e.g. `lastReportedAt`
    # from Meraki) for at least `top_problems_stale_after_seconds`.
    # Stale problems are then re-emitted at
    # `top_problems_stale_severity` (which can be `"filter"` to drop
    # them entirely) and tagged with `evidence.stale = true` so any
    # downstream agent or UI can present them as "housekeeping"
    # rather than "incident".
    #
    # The threshold deliberately defaults to 24 h, which is long
    # enough that any genuine outage of practical concern (where the
    # device WAS reporting and stopped) keeps its `critical` rank,
    # while abandoned/never-deployed inventory (where the device has
    # not reported in days/weeks/months) is demoted to `info`.
    top_problems_stale_after_seconds: int
    top_problems_stale_severity: str  # "critical"|"warning"|"info"|"filter"

    # Held ref to bootstrap settings
    bootstrap: BootstrapSettings

    def __init__(self, bootstrap: BootstrapSettings) -> None:
        self.bootstrap = bootstrap
        # Defaults — overridden during hydrate()
        self.neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user = "neo4j"
        self.neo4j_password = "netcortex"
        # Default: built-in Docker redis container via env var set in docker-compose.
        # Override with REDIS_URL env var (external Redis) or redis_url in the secret.
        self.redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        self.mcp_transport = "http"
        self.mcp_secret = ""
        # Secure-by-default. Override with NETBOX_VERIFY_SSL=0 or
        # core-secret `netbox_verify_ssl=false` for self-signed labs.
        _verify_env = os.environ.get("NETBOX_VERIFY_SSL")
        if _verify_env is None:
            self.netbox_verify_ssl = True
        else:
            self.netbox_verify_ssl = _verify_env.strip().lower() in {
                "1", "true", "yes", "on",
            }
        self.sync_backend = "apscheduler"
        self.sync_conflict_policy = "alert"
        self.sync_interval = 300                    # global default: 5 min
        self.sync_interval_meraki = 3600        # default: 60 min
        self.sync_interval_catalyst_center = 300
        self.sync_interval_nexus_dashboard = 300
        self.sync_interval_intersight = 300
        self.sync_interval_snmp = 300
        self.sync_interval_generic_rest = 300
        self.sync_interval_netbox_sites = 300
        self.access_log_commands = False
        self.ssh_timeout = 30
        self.netconf_port = 830
        self.restconf_port = 443
        self.log_level = "INFO"
        self.log_format = "json"
        self.status_refresh_interval = 30
        # Defaults: 24 h staleness threshold, demote to `info` (still
        # visible but ranked below real incidents).  Operators who
        # want to drop stale problems entirely can set
        # `top_problems_stale_severity = "filter"` in the core secret.
        self.top_problems_stale_after_seconds = 86400
        self.top_problems_stale_severity = "info"

    @classmethod
    async def create(cls) -> "Settings":
        """
        Factory: read bootstrap env, connect to secret backend, hydrate settings.
        Call once at application startup.
        """
        bootstrap = BootstrapSettings()  # type: ignore[call-arg]
        instance = cls(bootstrap)
        await instance.hydrate()
        return instance

    async def hydrate(self) -> None:
        """Pull runtime config from the secret backend's core secret."""
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()

        try:
            core = await backend.get_core()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load core config from secret backend "
                f"({self.bootstrap.secret_backend}): {exc}"
            ) from exc

        # Required keys
        self.netbox_url = _require(core, "netbox_url", "netcortex/core")
        self.netbox_token = _require(core, "netbox_token", "netcortex/core")
        raw_verify_ssl = core.get("netbox_verify_ssl", self.netbox_verify_ssl)
        if isinstance(raw_verify_ssl, str):
            self.netbox_verify_ssl = raw_verify_ssl.strip().lower() in {
                "1", "true", "yes", "on",
            }
        else:
            self.netbox_verify_ssl = bool(raw_verify_ssl)

        # Optional keys with defaults
        self.neo4j_uri = core.get("neo4j_uri", self.neo4j_uri)
        self.neo4j_user = core.get("neo4j_user", self.neo4j_user)
        self.neo4j_password = core.get("neo4j_password", self.neo4j_password)
        # Only override redis_url from the secret if it is explicitly set and non-empty,
        # so the REDIS_URL env var (pointing to the built-in container) wins by default.
        self.redis_url = core.get("redis_url") or self.redis_url
        self.mcp_transport = core.get("mcp_transport", self.mcp_transport)
        self.mcp_secret = core.get("mcp_secret", self.mcp_secret)
        self.sync_backend = core.get("sync_backend", self.sync_backend)
        self.sync_conflict_policy = core.get("sync_conflict_policy", self.sync_conflict_policy)

        # Global interval — overrides the built-in 300 s default for ALL types.
        # Per-type keys (sync_interval_meraki, etc.) take precedence over this.
        global_iv = int(core.get("sync_interval", self.sync_interval))
        self.sync_interval = global_iv

        def _iv(key: str, current: int) -> int:
            """Return per-type override if set, else global interval."""
            return int(core[key]) if key in core else (global_iv if "sync_interval" in core else current)

        self.sync_interval_meraki           = _iv("sync_interval_meraki",           self.sync_interval_meraki)
        self.sync_interval_catalyst_center  = _iv("sync_interval_catalyst_center",  self.sync_interval_catalyst_center)
        self.sync_interval_nexus_dashboard  = _iv("sync_interval_nexus_dashboard",  self.sync_interval_nexus_dashboard)
        self.sync_interval_intersight       = _iv("sync_interval_intersight",       self.sync_interval_intersight)
        self.sync_interval_snmp             = _iv("sync_interval_snmp",             self.sync_interval_snmp)
        self.sync_interval_generic_rest     = _iv("sync_interval_generic_rest",     self.sync_interval_generic_rest)
        self.sync_interval_netbox_sites     = _iv("sync_interval_netbox_sites",     self.sync_interval_netbox_sites)
        self.access_log_commands = bool(core.get("access_log_commands", self.access_log_commands))
        self.ssh_timeout = int(core.get("ssh_timeout", self.ssh_timeout))
        self.netconf_port = int(core.get("netconf_port", self.netconf_port))
        self.restconf_port = int(core.get("restconf_port", self.restconf_port))
        self.log_level = core.get("log_level", self.log_level)
        self.log_format = core.get("log_format", self.log_format)
        self.status_refresh_interval = int(
            core.get("status_refresh_interval", self.status_refresh_interval)
        )
        self.top_problems_stale_after_seconds = int(core.get(
            "top_problems_stale_after_seconds",
            self.top_problems_stale_after_seconds,
        ))
        # Validate to an allowed enum so a typo in the secret can't
        # silently break the filter. Invalid values fall back to the
        # default "info" with a warning.
        raw_sev = str(core.get(
            "top_problems_stale_severity",
            self.top_problems_stale_severity,
        )).lower()
        if raw_sev not in {"critical", "warning", "info", "filter"}:
            log.warning(
                "settings.top_problems_stale_severity.invalid",
                value=raw_sev,
                fallback=self.top_problems_stale_severity,
            )
        else:
            self.top_problems_stale_severity = raw_sev

        log.info(
            "settings.hydrated",
            backend=self.bootstrap.secret_backend,
            netbox_url=self.netbox_url,
        )


def _require(d: dict[str, Any], key: str, path: str) -> Any:
    if key not in d:
        raise RuntimeError(
            f"Required key {key!r} missing from secret at {path!r}. "
            f"Add it to your secret backend."
        )
    return d[key]


# ---------------------------------------------------------------------------
# Singleton — populated at startup via Settings.create()
# ---------------------------------------------------------------------------

settings: Settings | None = None


async def init_settings() -> Settings:
    """Initialize the global settings singleton. Call once at startup."""
    global settings
    settings = await Settings.create()
    return settings


def get_settings() -> Settings:
    """Return the initialized settings singleton. Raises if called before init."""
    if settings is None:
        raise RuntimeError(
            "Settings not initialized. Call await init_settings() at startup."
        )
    return settings
