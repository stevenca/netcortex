"""SNMP credential resolver.

Resolution order (first match wins) varies by polling context:

  Device-level polling (direct to device IP):
    1. netcortex/snmp/device/{device_name}     — per-device override
    2. netcortex/snmp/adapter/meraki_device    — Meraki device-level (DES enforced)
       OR netcortex/snmp/adapter/{adapter_type} — other platform type-level
    3. netcortex/snmp/default                  — global fallback

  Meraki cloud/Dashboard SNMP polling:
    1. netcortex/snmp/adapter/meraki_cloud     — passwords for Meraki cloud SNMP
    2. netcortex/snmp/default                  — global fallback

IMPORTANT — Meraki device vs cloud SNMP:
  - Device-level SNMP (polling individual device management IP):
      Only DES is supported for SNMPv3 privacy at the device level.
      The resolver ALWAYS forces priv_protocol=DES for Meraki device polls,
      regardless of what the matched secret specifies.

  - Cloud/Dashboard SNMP (polling {orgId}.snmp.meraki.com:{port}):
      AES128 and AES256 are supported. The Meraki API returns the security
      name, auth mode, and priv mode; only the auth/priv passwords come
      from the secrets backend (netcortex/snmp/adapter/meraki_cloud).

Each secret JSON:
{
  "version":        "v3" | "v2c",          # default "v2c"
  "community":      "public",              # v2c community string
  "username":       "netcortex_ro",        # v3 security name
  "auth_protocol":  "SHA256",              # MD5|SHA|SHA224|SHA256|SHA384|SHA512
  "auth_password":  "...",
  "priv_protocol":  "AES256",             # DES|AES128|AES192|AES256
  "priv_password":  "...",
  "security_level": "authPriv"            # noAuthNoPriv|authNoPriv|authPriv
}

Meraki cloud secret may additionally contain:
  "hostname": "..."   (optional override; normally sourced from Meraki API)
  "port": 16100       (optional override)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

import structlog

# AWS SM secret names allow: alphanumeric, -, _, +, =, ., @, /
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9\-_+=.@/]")

log = structlog.get_logger(__name__)


class SnmpContext(str, Enum):
    """Which SNMP polling plane the credential is being resolved for."""
    DEVICE = "device"         # Direct poll of device management IP
    MERAKI_CLOUD = "meraki_cloud"  # Poll via Meraki Dashboard SNMP endpoint


@dataclass(frozen=True)
class SnmpV2Creds:
    community: str = "public"


@dataclass(frozen=True)
class SnmpV3Creds:
    username: str
    auth_protocol: str = "SHA256"
    auth_password: str = ""
    priv_protocol: str = "AES256"
    priv_password: str = ""
    security_level: str = "authPriv"   # noAuthNoPriv | authNoPriv | authPriv


SnmpCreds = SnmpV2Creds | SnmpV3Creds


def _parse_creds(raw: dict[str, Any]) -> SnmpCreds | None:
    """Parse a secret dict into a typed credential object."""
    if not raw:
        return None
    version = raw.get("version", "v2c").lower()
    if version == "v3":
        username = raw.get("username", "")
        if not username:
            return None
        return SnmpV3Creds(
            username=username,
            auth_protocol=raw.get("auth_protocol", "SHA256"),
            auth_password=raw.get("auth_password", ""),
            priv_protocol=raw.get("priv_protocol", "AES256"),
            priv_password=raw.get("priv_password", ""),
            security_level=raw.get("security_level", "authPriv"),
        )
    community = raw.get("community", raw.get("v2c_community", "public"))
    return SnmpV2Creds(community=community)


def _apply_meraki_cloud_metadata(
    creds: SnmpV3Creds,
    v3_user: str,
    v3_auth_mode: str,
    v3_priv_mode: str,
) -> SnmpV3Creds:
    """Override creds with metadata returned by the Meraki Dashboard API.

    The Meraki API returns the security name, auth mode, and priv mode for
    cloud SNMP — these always take precedence over what's stored in the secret.
    Only the auth/priv *passwords* come from the secrets backend.
    """
    # Normalise Meraki auth mode strings to our convention
    auth_map = {
        "SHA":    "SHA",
        "SHA1":   "SHA",
        "SHA2":   "SHA256",
        "SHA224": "SHA224",
        "SHA256": "SHA256",
        "SHA384": "SHA384",
        "SHA512": "SHA512",
        "MD5":    "MD5",
    }
    # Meraki cloud priv: AES128, AES256 (may also return "AES")
    priv_map = {
        "AES":    "AES128",
        "AES128": "AES128",
        "AES256": "AES256",
        "DES":    "DES",
    }
    return replace(
        creds,
        username=v3_user or creds.username,
        auth_protocol=auth_map.get(v3_auth_mode.upper(), creds.auth_protocol),
        priv_protocol=priv_map.get(v3_priv_mode.upper(), creds.priv_protocol),
    )


def _force_des(creds: SnmpCreds) -> SnmpCreds:
    """Return creds with priv_protocol forced to DES.

    Meraki device-level SNMPv3 (on actual Meraki hardware) only supports DES
    for privacy. See ``_is_meraki_hardware`` for the model gating.
    """
    if isinstance(creds, SnmpV3Creds) and creds.priv_protocol.upper() not in ("DES", "NONE"):
        return replace(creds, priv_protocol="DES")
    return creds


# Meraki hardware model prefixes.  Devices whose model starts with one of
# these are managed end-to-end by Meraki and either run Meraki firmware
# (MR/MS/MX/MV/MG/MT/Z) or are Catalyst-hardware APs onboarded via the
# Meraki Dashboard (CW91xx).  Both groups share Meraki's per-network
# direct-SNMP model: SNMPv3 SHA-1 + DES with a single passphrase used
# for both auth and priv.  Third-party devices managed via Meraki (e.g.,
# Catalyst 9k switches onboarded into Meraki) speak full standard SNMP
# and must NOT be downgraded — they are not in this list.
_MERAKI_HW_PREFIXES = ("MR", "MS", "MX", "MV", "MG", "MT", "Z", "CW")


def _is_meraki_hardware(model: str | None) -> bool:
    """Return True if *model* looks like real Meraki hardware (DES-only)."""
    if not model:
        # Conservative default when we don't know the model: assume real Meraki
        # hardware to preserve historical behavior on Meraki-only deployments.
        # Adapters now pass model explicitly for Meraki-source targets, so this
        # path is only hit for unknown/un-tagged devices.
        return True
    m = model.strip().upper()
    return any(m.startswith(p) for p in _MERAKI_HW_PREFIXES)


class SnmpCredentialResolver:
    """Resolves SNMP credentials from the configured secrets backend.

    Caches resolved credentials per lookup key for the lifetime of a
    discovery cycle (resolver instances are short-lived per discover() call).
    """

    def __init__(self, backend) -> None:   # backend: SecretBackend
        self._backend = backend
        self._cache: dict[str, SnmpCreds | None] = {}

    async def _fetch_path(self, path: str) -> SnmpCreds | None:
        """Fetch and parse one secret path, using cache."""
        if path in self._cache:
            return self._cache[path]
        try:
            raw = await self._backend.get(path, required=False)
            creds = _parse_creds(raw)
            self._cache[path] = creds
            if creds is not None:
                log.debug("snmp.creds.resolved", path=path, version=type(creds).__name__)
            return creds
        except Exception as exc:
            log.debug("snmp.creds.lookup_failed", path=path, error=str(exc))
            self._cache[path] = None
            return None

    async def resolve(
        self,
        device_name: str | None = None,
        source_adapter: str | None = None,
        context: SnmpContext = SnmpContext.DEVICE,
        device_model: str | None = None,
    ) -> SnmpCreds | None:
        """Return the best-matching SNMP credential.

        Args:
            device_name:    short device hostname (for per-device lookup).
            source_adapter: adapter instance_id like "meraki/CPN".
            context:        DEVICE (direct poll) or MERAKI_CLOUD (Dashboard SNMP).
            device_model:   platform model string (e.g., "MS225", "C9300").
                            Used to decide whether to force DES — only actual
                            Meraki hardware (MR/MS/MX/MV/MG) has the
                            DES-only limitation; third-party gear (Catalyst,
                            Nexus, etc.) referenced from Meraki's API runs a
                            full-feature SNMP agent that supports AES.
        """
        adapter_type = source_adapter.split("/")[0] if source_adapter else ""

        if context == SnmpContext.MERAKI_CLOUD:
            paths = ["snmp/adapter/meraki_cloud", "snmp/default"]
        else:
            # Device-level resolution
            paths = []
            if device_name:
                safe_name = _SAFE_PATH_RE.sub("-", device_name).strip("-")
                if safe_name:
                    paths.append(f"snmp/device/{safe_name}")
            if adapter_type == "meraki":
                # Only use Meraki-specific cred paths for actual Meraki
                # hardware. Third-party devices (Catalyst, Nexus, etc.)
                # tagged with source_adapter="meraki/*" run their OWN
                # SNMP agent that does NOT share creds with the Meraki
                # dashboard secrets — they must fall through to
                # `snmp/adapter/meraki` (catch-all) or `snmp/default`.
                if _is_meraki_hardware(device_model):
                    paths.append("snmp/adapter/meraki_device")
                paths.append("snmp/adapter/meraki")
            elif adapter_type:
                paths.append(f"snmp/adapter/{adapter_type}")
            paths.append("snmp/default")

        for path in paths:
            creds = await self._fetch_path(path)
            if creds is not None:
                if (context == SnmpContext.DEVICE
                        and adapter_type == "meraki"
                        and _is_meraki_hardware(device_model)):
                    creds = _force_des(creds)
                    log.debug("snmp.creds.des_enforced", host=device_name,
                              model=device_model)
                return creds

        log.debug("snmp.creds.not_found", device=device_name, adapter=source_adapter,
                  context=context.value)
        return None

    async def resolve_meraki_cloud_with_api_meta(
        self,
        v3_user: str,
        v3_auth_mode: str,
        v3_priv_mode: str,
    ) -> SnmpV3Creds | None:
        """Resolve Meraki cloud creds and overlay metadata from the Dashboard API.

        The Meraki API returns the security name, auth mode, and priv mode
        for the org's SNMP configuration.  We fetch only the passwords from SM.
        """
        creds = await self.resolve(context=SnmpContext.MERAKI_CLOUD)
        if not isinstance(creds, SnmpV3Creds):
            return None
        return _apply_meraki_cloud_metadata(creds, v3_user, v3_auth_mode, v3_priv_mode)
