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
    # When true, the worker's NetBox writeback loop computes the full diff but
    # short-circuits every PATCH/POST/DELETE. The resulting `report` still lists
    # every intended change with `dry_run=True` on each entry, which is useful
    # for verifying a new release against a live NetBox without modifying it.
    # Override with NETBOX_WRITEBACK_DRY_RUN=1 or core-secret
    # `netbox_writeback_dry_run=true`.
    netbox_writeback_dry_run: bool

    # Neo4j graph database
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    # Redis
    redis_url: str

    # MCP
    mcp_transport: str
    mcp_secret: str

    # HTTP API / webhook security (0.8.0-dev10)
    #
    # api_secret: when non-empty, every non-public HTTP route (the status
    #   UI, /api/*, /metrics, the telemetry SSE monitor) requires
    #   `Authorization: Bearer <api_secret>`. Empty disables app-level API
    #   auth — in that mode the *ingress* is the control: the shipped chart
    #   only exposes the webhook/health receiver paths publicly, so /api
    #   stays cluster-internal. Set this whenever the API is reachable from
    #   an untrusted network.
    # webhook_allow_unsigned: master fail-open switch. Default False =
    #   fail closed: a webhook for a tenant with no configured secret is
    #   rejected (503) instead of silently accepted. Set True only to
    #   bootstrap a brand-new receiver before its secret is provisioned.
    # webhook_max_body_bytes: hard cap on webhook/telemetry request bodies.
    #   Requests larger than this are rejected (413) before parsing.
    # webhook_replay_window_seconds: when a webhook payload carries a
    #   trusted timestamp (e.g. Meraki `sentAt`), reject it if older than
    #   this many seconds. 0 disables the freshness check.
    # telemetry_secret: shared token required (header `X-Telemetry-Token`)
    #   on the HTTP telemetry-push ingest endpoint. Same fail-closed rules
    #   as webhook secrets.
    # cors_allow_origins: explicit allow-list of browser origins. Empty =
    #   no cross-origin access (same-origin only) — we never ship `*`.
    api_secret: str
    webhook_allow_unsigned: bool
    webhook_max_body_bytes: int
    webhook_replay_window_seconds: int
    telemetry_secret: str
    cors_allow_origins: list[str]

    # SAML SSO + session management (0.8.0-dev11)
    #
    # When saml_enabled is true the web app runs an in-app SAML 2.0
    # Service Provider: human browser access to the UI/API is gated behind
    # an IdP login (Okta), while machine callers keep using the api_secret
    # bearer and webhook HMAC/token auth. Sessions are stored server-side
    # in Redis (opaque CSPRNG id in a Secure/HttpOnly cookie) — no PII or
    # privileges live in the cookie.
    saml_enabled: bool
    # SP (this app) identity. saml_sp_base_url is the externally-reachable
    # https origin; ACS/SLS/metadata URLs are derived from it unless set.
    saml_sp_base_url: str
    saml_sp_entity_id: str
    # IdP (Okta) metadata — copy from the Okta app's "View SAML setup
    # instructions" / metadata. The x509 cert verifies signed assertions.
    saml_idp_entity_id: str
    saml_idp_sso_url: str
    saml_idp_slo_url: str
    saml_idp_x509_cert: str
    # Optional SP signing keypair (sign AuthnRequests / SLO). Secrets —
    # store in the backend, never env.
    saml_sp_x509_cert: str
    saml_sp_private_key: str
    # Optional coarse authorization: restrict who may establish a session.
    # Empty lists = any successfully-authenticated IdP user is allowed.
    saml_allowed_email_domains: list[str]
    saml_allowed_groups: list[str]
    saml_attr_groups: str  # SAML attribute name carrying group membership

    # Session cookie / lifetime policy.
    session_cookie_name: str
    session_cookie_secure: bool
    session_idle_timeout_seconds: int
    session_absolute_timeout_seconds: int

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

        # HTTP API / webhook security (0.8.0-dev10). Env vars provide the
        # bootstrap defaults; the core secret can override during hydrate().
        self.api_secret = os.environ.get("NETCORTEX_API_SECRET", "")
        self.webhook_allow_unsigned = _env_bool(
            "NETCORTEX_WEBHOOK_ALLOW_UNSIGNED", default=False
        )
        self.webhook_max_body_bytes = _env_int(
            "NETCORTEX_WEBHOOK_MAX_BODY_BYTES", default=1_048_576  # 1 MiB
        )
        self.webhook_replay_window_seconds = _env_int(
            "NETCORTEX_WEBHOOK_REPLAY_WINDOW_SECONDS", default=300
        )
        self.telemetry_secret = os.environ.get("NETCORTEX_TELEMETRY_SECRET", "")
        self.cors_allow_origins = _env_csv("NETCORTEX_CORS_ALLOW_ORIGINS")

        # SAML SSO + sessions (0.8.0-dev11). Bootstrap from env; the core
        # secret (which holds the IdP cert and SP key) overrides in hydrate().
        self.saml_enabled = _env_bool("NETCORTEX_SAML_ENABLED", default=False)
        self.saml_sp_base_url = os.environ.get("NETCORTEX_SAML_SP_BASE_URL", "")
        self.saml_sp_entity_id = os.environ.get("NETCORTEX_SAML_SP_ENTITY_ID", "")
        self.saml_idp_entity_id = os.environ.get("NETCORTEX_SAML_IDP_ENTITY_ID", "")
        self.saml_idp_sso_url = os.environ.get("NETCORTEX_SAML_IDP_SSO_URL", "")
        self.saml_idp_slo_url = os.environ.get("NETCORTEX_SAML_IDP_SLO_URL", "")
        # The IdP signing cert is public (it ships in IdP metadata), so it can
        # be supplied via Helm values/env. The SP private key stays
        # secret-backend-only (never read from env).
        self.saml_idp_x509_cert = os.environ.get("NETCORTEX_SAML_IDP_X509_CERT", "")
        self.saml_sp_x509_cert = os.environ.get("NETCORTEX_SAML_SP_X509_CERT", "")
        self.saml_sp_private_key = ""
        self.saml_allowed_email_domains = _env_csv("NETCORTEX_SAML_ALLOWED_EMAIL_DOMAINS")
        self.saml_allowed_groups = _env_csv("NETCORTEX_SAML_ALLOWED_GROUPS")
        self.saml_attr_groups = os.environ.get("NETCORTEX_SAML_ATTR_GROUPS", "groups")
        self.session_cookie_name = os.environ.get(
            "NETCORTEX_SESSION_COOKIE_NAME", "nc_session"
        )
        self.session_cookie_secure = _env_bool(
            "NETCORTEX_SESSION_COOKIE_SECURE", default=True
        )
        self.session_idle_timeout_seconds = _env_int(
            "NETCORTEX_SESSION_IDLE_TIMEOUT_SECONDS", default=1800  # 30 min
        )
        self.session_absolute_timeout_seconds = _env_int(
            "NETCORTEX_SESSION_ABSOLUTE_TIMEOUT_SECONDS", default=28800  # 8 h
        )
        # Secure-by-default. Override with NETBOX_VERIFY_SSL=0 or
        # core-secret `netbox_verify_ssl=false` for self-signed labs.
        _verify_env = os.environ.get("NETBOX_VERIFY_SSL")
        if _verify_env is None:
            self.netbox_verify_ssl = True
        else:
            self.netbox_verify_ssl = _verify_env.strip().lower() in {
                "1", "true", "yes", "on",
            }
        _dry_env = os.environ.get("NETBOX_WRITEBACK_DRY_RUN")
        if _dry_env is None:
            self.netbox_writeback_dry_run = False
        else:
            self.netbox_writeback_dry_run = _dry_env.strip().lower() in {
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
        raw_dry_run = core.get("netbox_writeback_dry_run", self.netbox_writeback_dry_run)
        if isinstance(raw_dry_run, str):
            self.netbox_writeback_dry_run = raw_dry_run.strip().lower() in {
                "1", "true", "yes", "on",
            }
        else:
            self.netbox_writeback_dry_run = bool(raw_dry_run)

        # Optional keys with defaults
        self.neo4j_uri = core.get("neo4j_uri", self.neo4j_uri)
        self.neo4j_user = core.get("neo4j_user", self.neo4j_user)
        self.neo4j_password = core.get("neo4j_password", self.neo4j_password)
        # Only override redis_url from the secret if it is explicitly set and non-empty,
        # so the REDIS_URL env var (pointing to the built-in container) wins by default.
        self.redis_url = core.get("redis_url") or self.redis_url
        self.mcp_transport = core.get("mcp_transport", self.mcp_transport)
        self.mcp_secret = core.get("mcp_secret", self.mcp_secret)

        # HTTP API / webhook security — core secret overrides env defaults.
        self.api_secret = core.get("api_secret", self.api_secret)
        raw_allow_unsigned = core.get("webhook_allow_unsigned", self.webhook_allow_unsigned)
        if isinstance(raw_allow_unsigned, str):
            self.webhook_allow_unsigned = raw_allow_unsigned.strip().lower() in {
                "1", "true", "yes", "on",
            }
        else:
            self.webhook_allow_unsigned = bool(raw_allow_unsigned)
        self.webhook_max_body_bytes = int(
            core.get("webhook_max_body_bytes", self.webhook_max_body_bytes)
        )
        self.webhook_replay_window_seconds = int(
            core.get("webhook_replay_window_seconds", self.webhook_replay_window_seconds)
        )
        self.telemetry_secret = core.get("telemetry_secret", self.telemetry_secret)
        raw_cors = core.get("cors_allow_origins", None)
        if raw_cors is not None:
            if isinstance(raw_cors, str):
                self.cors_allow_origins = [
                    o.strip() for o in raw_cors.split(",") if o.strip()
                ]
            elif isinstance(raw_cors, (list, tuple)):
                self.cors_allow_origins = [str(o).strip() for o in raw_cors if str(o).strip()]
        # ── SAML SSO + sessions (0.8.0-dev11) ─────────────────────────────
        raw_saml_enabled = core.get("saml_enabled", self.saml_enabled)
        if isinstance(raw_saml_enabled, str):
            self.saml_enabled = raw_saml_enabled.strip().lower() in {
                "1", "true", "yes", "on",
            }
        else:
            self.saml_enabled = bool(raw_saml_enabled)
        self.saml_sp_base_url = core.get("saml_sp_base_url", self.saml_sp_base_url)
        self.saml_sp_entity_id = core.get("saml_sp_entity_id", self.saml_sp_entity_id)
        self.saml_idp_entity_id = core.get("saml_idp_entity_id", self.saml_idp_entity_id)
        self.saml_idp_sso_url = core.get("saml_idp_sso_url", self.saml_idp_sso_url)
        self.saml_idp_slo_url = core.get("saml_idp_slo_url", self.saml_idp_slo_url)
        self.saml_idp_x509_cert = core.get("saml_idp_x509_cert", self.saml_idp_x509_cert)
        self.saml_sp_x509_cert = core.get("saml_sp_x509_cert", self.saml_sp_x509_cert)
        self.saml_sp_private_key = core.get("saml_sp_private_key", self.saml_sp_private_key)

        def _csv_or_list(value: Any, current: list[str]) -> list[str]:
            if value is None:
                return current
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if str(v).strip()]
            return current

        self.saml_allowed_email_domains = _csv_or_list(
            core.get("saml_allowed_email_domains"), self.saml_allowed_email_domains
        )
        self.saml_allowed_groups = _csv_or_list(
            core.get("saml_allowed_groups"), self.saml_allowed_groups
        )
        self.saml_attr_groups = core.get("saml_attr_groups", self.saml_attr_groups)
        self.session_cookie_name = core.get("session_cookie_name", self.session_cookie_name)
        raw_cookie_secure = core.get("session_cookie_secure", self.session_cookie_secure)
        if isinstance(raw_cookie_secure, str):
            self.session_cookie_secure = raw_cookie_secure.strip().lower() in {
                "1", "true", "yes", "on",
            }
        else:
            self.session_cookie_secure = bool(raw_cookie_secure)
        self.session_idle_timeout_seconds = int(
            core.get("session_idle_timeout_seconds", self.session_idle_timeout_seconds)
        )
        self.session_absolute_timeout_seconds = int(
            core.get("session_absolute_timeout_seconds", self.session_absolute_timeout_seconds)
        )

        # Fail loudly if SAML is enabled but its required IdP fields are
        # missing — otherwise every UI login would 500 at the IdP redirect.
        if self.saml_enabled:
            missing = [
                k for k, v in {
                    "saml_sp_base_url": self.saml_sp_base_url,
                    "saml_idp_entity_id": self.saml_idp_entity_id,
                    "saml_idp_sso_url": self.saml_idp_sso_url,
                    "saml_idp_x509_cert": self.saml_idp_x509_cert,
                }.items() if not v
            ]
            if missing:
                log.error(
                    "settings.saml_enabled_but_incomplete",
                    missing=missing,
                    hint="Provide these in netcortex/core or disable saml_enabled.",
                )

        # Loud warning if the operator left fail-open enabled — this should
        # never be true in a production deployment.
        if self.webhook_allow_unsigned:
            log.warning(
                "settings.webhook_allow_unsigned_enabled",
                hint="Webhooks for tenants without a configured secret will be "
                     "ACCEPTED. Set webhook_allow_unsigned=false once secrets "
                     "are provisioned.",
            )
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


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("settings.env_int_invalid", name=name, value=raw, fallback=default)
        return default


def _env_csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


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
