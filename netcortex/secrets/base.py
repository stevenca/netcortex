"""Abstract secret backend — all backends implement this interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Well-known secret path constants
# All backends use these paths; the prefix (e.g. "netcortex") is configurable.
# ---------------------------------------------------------------------------

class SecretPaths:
    """Canonical secret paths within the configured prefix."""

    CORE = "core"
    """netbox_url, netbox_token, mcp_secret, redis_url, log_level, etc."""

    SYNC = "sync"
    """sync_conflict_policy, sync intervals, sync_backend."""

    ADAPTER_INDEX = "adapters/_index"
    """
    Registry of all adapter instances. Value is a JSON object:
    {
      "instances": [
        {"type": "meraki",           "name": "corp",    "enabled": true},
        {"type": "meraki",           "name": "branch",  "enabled": true},
        {"type": "catalyst_center",  "name": "dc1",     "enabled": true},
        {"type": "catalyst_center",  "name": "dc2",     "enabled": false},
        {"type": "intersight",       "name": "primary", "enabled": true},
        {"type": "nexus_dashboard",  "name": "prod",    "enabled": true}
      ]
    }
    Instance names must be unique within a type. The instance ID used
    throughout NetCortex is "{type}/{name}", e.g. "meraki/corp".
    """

    @staticmethod
    def adapter_type(adapter_type: str) -> str:
        """
        Type-level defaults shared by all instances of an adapter type.
        e.g. adapters/meraki, adapters/catalyst_center

        Supported keys (all optional):
          interval: int  — discovery interval in seconds for all instances of this type
        """
        return f"adapters/{adapter_type}"

    @staticmethod
    def adapter(adapter_type: str, instance_name: str) -> str:
        """
        Credentials and config for one named adapter instance.
        e.g. adapters/meraki/corp, adapters/catalyst_center/dc1

        Each instance secret contains whatever that adapter type needs:
          meraki:          { api_key, org_id, base_url (optional) }
          catalyst_center: { url, username, password }
          intersight:      { key_id, secret_key, base_url (optional) }
          nexus_dashboard: { url, username, password }
          fmc:             { deployment_mode, domain_uuid (optional; auto-discovered by default),
                             domain_name (optional selector for multi-domain FMC),
                             expand_details (optional, default true),
                             on-prem: url+username+password,
                             cdFMC: key_id + access_token + refresh_token
                                     (api_token supported for compatibility),
                                     optional base_url/region/token_url }
          snmp:            { community, version, auth_key, priv_key, ip_range }

        Any instance may also include:
          interval: int  — override the discovery interval for this instance only
        """
        return f"adapters/{adapter_type}/{instance_name}"

    @staticmethod
    def device_site(site_slug: str) -> str:
        """Site-wide device credentials. e.g. devices/site/building-a"""
        return f"devices/site/{site_slug}"

    @staticmethod
    def device_host(hostname: str) -> str:
        """Per-device credential override. e.g. devices/host/sw-bldga-01"""
        return f"devices/host/{hostname}"


# ---------------------------------------------------------------------------
# TTL cache — avoids hammering the secret backend on every request
# ---------------------------------------------------------------------------

class _SecretCache:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[dict, float]] = {}

    def get(self, key: str) -> dict | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: dict) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SecretNotFoundError(Exception):
    """Raised when a secret path does not exist in the backend."""

class SecretBackendError(Exception):
    """Raised on backend connectivity or auth errors."""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class SecretBackend(ABC):
    """
    Abstract base for secret backends.

    Concrete implementations: AwsSecretsManagerBackend, VaultBackend.

    All secrets are stored as key-value dicts at a given path. Each backend
    translates paths to its own native format (e.g. AWS SM secret names,
    Vault KV paths).
    """

    def __init__(self, prefix: str = "netcortex", cache_ttl: int = 300) -> None:
        self._prefix = prefix.strip("/")
        self._cache = _SecretCache(ttl_seconds=cache_ttl)

    # ------------------------------------------------------------------
    # Abstract interface — implement these in each backend
    # ------------------------------------------------------------------

    @abstractmethod
    async def _fetch(self, full_path: str) -> dict[str, Any]:
        """
        Fetch a secret by its full path (prefix already applied).
        Returns dict of key→value pairs.
        Raise SecretNotFoundError if the secret does not exist.
        Raise SecretBackendError on connectivity/auth failures.
        """

    @abstractmethod
    async def _store(self, full_path: str, values: dict[str, Any]) -> None:
        """Create or update a secret at the given full path."""

    @abstractmethod
    async def health_check(self) -> dict:
        """Return {"status": "ok"|"error", "message": str}."""

    # ------------------------------------------------------------------
    # Public API — uses cache, builds full paths
    # ------------------------------------------------------------------

    def _full_path(self, path: str) -> str:
        return f"{self._prefix}/{path}"

    async def get(self, path: str, required: bool = True) -> dict[str, Any]:
        """
        Retrieve a secret by its relative path.

        Uses a TTL cache to avoid repeated backend calls.
        If required=True (default), raises SecretNotFoundError when missing.
        """
        full = self._full_path(path)
        cached = self._cache.get(full)
        if cached is not None:
            return cached

        try:
            value = await self._fetch(full)
            self._cache.set(full, value)
            return value
        except SecretNotFoundError:
            if required:
                raise
            return {}

    async def get_key(self, path: str, key: str, default: Any = None) -> Any:
        """Convenience: fetch one key from a secret dict."""
        secret = await self.get(path, required=default is None)
        return secret.get(key, default)

    async def put(self, path: str, values: dict[str, Any]) -> None:
        """Create or update a secret, invalidating the cache entry."""
        full = self._full_path(path)
        await self._store(full, values)
        self._cache.invalidate(full)

    def invalidate_cache(self, path: str | None = None) -> None:
        """Invalidate one cache entry (or all if path is None)."""
        if path is None:
            self._cache.clear()
        else:
            self._cache.invalidate(self._full_path(path))

    # ------------------------------------------------------------------
    # Convenience helpers for well-known paths
    # ------------------------------------------------------------------

    async def get_core(self) -> dict[str, Any]:
        """Return the core NetCortex config secret."""
        return await self.get(SecretPaths.CORE)

    async def get_adapter_index(self) -> list[dict[str, Any]]:
        """
        Return the list of configured adapter instances from the index secret.
        Each entry has at minimum: {"type": str, "name": str, "enabled": bool}.
        Returns [] if the index secret does not exist.
        """
        data = await self.get(SecretPaths.ADAPTER_INDEX, required=False)
        return data.get("instances", [])

    async def get_adapter_type_config(self, adapter_type: str) -> dict[str, Any]:
        """
        Return type-level defaults for an adapter type.
        e.g. get_adapter_type_config("meraki")   ← reads adapters/meraki
        Returns {} if the secret does not exist.
        """
        return await self.get(SecretPaths.adapter_type(adapter_type), required=False)

    async def get_adapter_config(self, adapter_type: str, instance_name: str) -> dict[str, Any]:
        """
        Return config/credentials for one named adapter instance.
        e.g. get_adapter_config("meraki", "corp")
             get_adapter_config("catalyst_center", "dc1")
        Returns {} if the secret does not exist.
        """
        return await self.get(
            SecretPaths.adapter(adapter_type, instance_name), required=False
        )

    async def get_device_creds(self, hostname: str, site_slug: str | None = None) -> dict[str, Any]:
        """
        Return credentials for a device.
        Merges site-wide creds with per-host overrides (host wins).
        """
        site_creds: dict[str, Any] = {}
        if site_slug:
            site_creds = await self.get(SecretPaths.device_site(site_slug), required=False)

        host_creds = await self.get(SecretPaths.device_host(hostname), required=False)

        return {**site_creds, **host_creds}
