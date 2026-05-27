"""Cisco Secure Firewall Management Center adapter.

Supports both:
  - On-prem FMC (token auth via /api/fmc_platform/v1/auth/generatetoken)
  - Cloud-delivered FMC (cdFMC via Security Cloud Control bearer token)

The adapter intentionally starts with inventory + interface + IP context so it
fits NetCortex's existing Device/Interface/Routing capabilities without
requiring policy-write semantics.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import time
from typing import Any

import httpx
import structlog

from netcortex.adapters.base import AdapterError, AuthError, PlatformAdapter, PlatformProfile
from netcortex.graph.models import (
    Dimension, EdgeType, GraphData, GraphEdge, GraphNode, NodeType,
)
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN

log = structlog.get_logger(__name__)


def _norm_bool(raw: Any, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off")
    return default


def _pick(*vals: Any) -> Any:
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _safe_name(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


def _valid_ip(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Handle either plain host IP or CIDR strings.
    try:
        if "/" in s:
            return str(ipaddress.ip_interface(s).ip)
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _is_neo4j_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _to_neo4j_property(value: Any) -> Any:
    """Coerce arbitrary API values into Neo4j property-safe values.

    Neo4j allows only primitives and arrays of primitives as property values.
    We preserve nested objects as compact JSON strings instead of dropping them.
    """
    if _is_neo4j_primitive(value):
        return value
    if isinstance(value, list):
        if all(_is_neo4j_primitive(v) for v in value):
            return value
        return json.dumps(value, separators=(",", ":"), default=str)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _sanitize_props(raw: dict[str, Any]) -> dict[str, Any]:
    # Keep graph identity stable: never let adapter payloads overwrite core IDs.
    reserved = {"id", "source", "target"}
    return {
        str(k): _to_neo4j_property(v)
        for k, v in raw.items()
        if str(k) not in reserved
    }


class FmcAdapter(PlatformAdapter):
    name = "fmc"
    display_name = "Cisco Secure Firewall Management Center"
    profile = PlatformProfile(
        device_id_field="id",
        role_map={"ftd": "firewall", "firewall": "firewall"},
        native_topology=False,
        provides_oper_status=True,
        default_access_methods=["https", "ssh"],
        supported_dimensions=["physical", "routing", "wan"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._mode = str(config.get("deployment_mode", config.get("mode", "onprem"))).lower()
        if self._mode not in {"onprem", "cdfmc"}:
            raise ValueError("fmc deployment_mode must be 'onprem' or 'cdfmc'")

        # On-prem FMC
        self._url = str(config.get("url", "")).rstrip("/")
        self._username = str(config.get("username", ""))
        self._password = str(config.get("password", ""))

        # Cloud-delivered FMC (Security Cloud Control Firewall API)
        region = str(config.get("region", "us")).lower()
        default_cloud = f"https://api.{region}.security.cisco.com/firewall"
        self._cloud_base_url = str(config.get("base_url", default_cloud)).rstrip("/")
        # cdFMC credential forms:
        # - preferred: key_id + access_token + refresh_token
        # - backward-compatible: api_token (treated as access token)
        self._key_id = str(config.get("key_id", ""))
        self._cloud_access_token = str(config.get("access_token", config.get("api_token", "")))
        self._cloud_refresh_token = str(config.get("refresh_token", ""))
        self._token_url = str(config.get("token_url", "")).rstrip("/")

        # Shared
        self._verify_ssl = _norm_bool(config.get("verify_ssl", True), default=True)
        self._domain_uuid: str | None = config.get("domain_uuid")
        self._domain_name: str = str(config.get("domain_name", "")).strip()
        self._expand_details = _norm_bool(config.get("expand_details", True), default=True)

        # Runtime auth state
        self._access_token: str | None = None
        self._cloud_access_token_exp_epoch: int | None = self._jwt_exp(self._cloud_access_token)

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            follow_redirects=True,
            timeout=timeout,
        )

    def _platform_base(self) -> str:
        if self._mode == "onprem":
            return f"{self._url}/api/fmc_platform/v1"
        return f"{self._cloud_base_url}/v1/cdfmc/api/fmc_platform/v1"

    def _config_base(self) -> str:
        if not self._domain_uuid:
            raise AdapterError("FMC domain UUID is not set")
        if self._mode == "onprem":
            return f"{self._url}/api/fmc_config/v1/domain/{self._domain_uuid}"
        return f"{self._cloud_base_url}/v1/cdfmc/api/fmc_config/v1/domain/{self._domain_uuid}"

    def _extract_domain_uuid(self, payload: Any) -> str | None:
        """Pick a domain UUID from FMC domain-info payload.

        Accepts either a dict with ``items`` or a direct list; handles both
        ``uuid`` and ``id`` keys defensively.
        """
        items: list[Any] = []
        if isinstance(payload, dict):
            raw_items = payload.get("items")
            if isinstance(raw_items, list):
                items = raw_items
        elif isinstance(payload, list):
            items = payload
        if self._domain_name:
            wanted = self._domain_name.casefold()
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(_pick(item.get("name"), item.get("displayName"), "")).strip()
                if name and name.casefold() == wanted:
                    maybe = _pick(item.get("uuid"), item.get("id"))
                    if maybe:
                        return str(maybe)
        for item in items:
            if not isinstance(item, dict):
                continue
            maybe = _pick(item.get("uuid"), item.get("id"))
            if maybe:
                return str(maybe)
        return None

    def _cdfmc_token_endpoint(self) -> str:
        if self._token_url:
            return self._token_url
        return f"{self._cloud_base_url}/oauth/token"

    def _jwt_exp(self, token: str) -> int | None:
        """Best-effort JWT exp extraction for proactive refresh.

        If token is opaque/non-JWT this returns None and we fall back to
        on-demand refresh on 401 responses.
        """
        if not token or token.count(".") < 2:
            return None
        try:
            payload_b64 = token.split(".")[1]
            padding = "=" * ((4 - (len(payload_b64) % 4)) % 4)
            payload = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
            body = json.loads(payload)
            raw_exp = body.get("exp")
            if isinstance(raw_exp, int):
                return raw_exp
            if isinstance(raw_exp, str) and raw_exp.isdigit():
                return int(raw_exp)
            return None
        except Exception:
            return None

    async def _refresh_cdfmc_access_token(self) -> None:
        if not self._cloud_refresh_token:
            raise AuthError("cdFMC refresh_token is required to refresh access token")
        token_url = self._cdfmc_token_endpoint()
        async with self._client(timeout=20) as client:
            # Try common OAuth2 shapes used by cloud APIs. We intentionally keep
            # this flexible because tenants can issue slightly different payload
            # contracts around the same refresh-token grant.
            attempts = [
                {"grant_type": "refresh_token", "refresh_token": self._cloud_refresh_token, "client_id": self._key_id},
                {"grantType": "refresh_token", "refreshToken": self._cloud_refresh_token, "keyId": self._key_id},
            ]
            last_status = None
            last_body = ""
            for form in attempts:
                # Drop empty values so APIs that reject unknown/blank fields still succeed.
                form = {k: v for k, v in form.items() if v}
                resp = await client.post(token_url, data=form)
                if resp.is_success:
                    body = resp.json()
                    new_access = str(_pick(
                        body.get("access_token"),
                        body.get("accessToken"),
                        body.get("token"),
                    ) or "")
                    if not new_access:
                        raise AuthError("cdFMC token refresh succeeded but no access token returned")
                    self._cloud_access_token = new_access
                    self._cloud_access_token_exp_epoch = self._jwt_exp(new_access)
                    # Some providers rotate refresh token too.
                    maybe_refresh = _pick(body.get("refresh_token"), body.get("refreshToken"))
                    if maybe_refresh:
                        self._cloud_refresh_token = str(maybe_refresh)
                    return
                last_status = resp.status_code
                last_body = resp.text[:240]
            raise AuthError(
                f"cdFMC token refresh failed: HTTP {last_status} — {last_body}"
            )

    async def _ensure_cdfmc_access_token(self) -> None:
        if not self._cloud_access_token:
            await self._refresh_cdfmc_access_token()
            return
        # If token is JWT and close to expiry, refresh preemptively.
        if self._cloud_access_token_exp_epoch is not None:
            if time.time() >= float(self._cloud_access_token_exp_epoch - 90):
                await self._refresh_cdfmc_access_token()

    async def authenticate(self) -> None:
        if self._mode == "onprem":
            if not self._url or not self._username or not self._password:
                raise AuthError("FMC on-prem requires url, username, and password")
            async with self._client(timeout=20) as client:
                resp = await client.post(
                    f"{self._url}/api/fmc_platform/v1/auth/generatetoken",
                    auth=(self._username, self._password),
                )
                if resp.status_code in (401, 403):
                    raise AuthError("FMC on-prem authentication failed")
                if not resp.is_success:
                    raise AdapterError(f"FMC auth failed: HTTP {resp.status_code}")
                token = resp.headers.get("X-auth-access-token") or resp.headers.get(
                    "x-auth-access-token"
                )
                if not token:
                    raise AuthError("FMC auth succeeded but no access token header returned")
                self._access_token = token
                if not self._domain_uuid:
                    self._domain_uuid = resp.headers.get("DOMAIN_UUID") or resp.headers.get(
                        "domain_uuid"
                    )
                if not self._domain_uuid:
                    # Try domain info endpoint as fallback.
                    domain_info = await self._get_platform("/info/domain")
                    self._domain_uuid = self._extract_domain_uuid(domain_info)
                if not self._domain_uuid:
                    raise AuthError("Could not resolve FMC domain UUID; set domain_uuid in secret")
        else:
            if not self._cloud_access_token and not self._cloud_refresh_token:
                raise AuthError(
                    "cdFMC requires credentials: access_token (+ optional key_id/refresh_token) "
                    "or refresh_token-based flow"
                )
            await self._ensure_cdfmc_access_token()
            # Only call domain-info API when we still need to resolve domain UUID.
            # Some cdFMC tenants/gateways reject /info/domain for restricted roles
            # even when direct domain-scoped config APIs work. Allow explicit
            # domain_uuid to bypass this lookup.
            if not self._domain_uuid:
                info = await self._get_platform("/info/domain")
                self._domain_uuid = self._extract_domain_uuid(info)
            if not self._domain_uuid:
                raise AuthError("Could not resolve cdFMC domain UUID; set domain_uuid in secret")

        log.debug(
            "fmc.authenticated",
            instance=self.instance_id,
            mode=self._mode,
            domain_uuid=self._domain_uuid,
            domain_name=self._domain_name or None,
        )

    def _auth_headers(self) -> dict[str, str]:
        if self._mode == "onprem":
            if not self._access_token:
                raise RuntimeError("Not authenticated")
            return {"X-auth-access-token": self._access_token}
        return {"Authorization": f"Bearer {self._cloud_access_token}"}

    async def _get_platform(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._mode == "cdfmc":
            await self._ensure_cdfmc_access_token()
        async with self._client(timeout=20) as client:
            resp = await client.get(
                f"{self._platform_base()}{path}",
                headers=self._auth_headers(),
                params=params,
            )
            if resp.status_code == 401 and self._mode == "cdfmc":
                await self._refresh_cdfmc_access_token()
                resp = await client.get(
                    f"{self._platform_base()}{path}",
                    headers=self._auth_headers(),
                    params=params,
                )
            if not resp.is_success:
                raise AdapterError(
                    f"FMC platform GET {path} failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            return resp.json()

    async def _get_url(self, url: str) -> Any:
        """Authenticated GET against an absolute URL (for links.self expansion)."""
        if self._mode == "cdfmc":
            await self._ensure_cdfmc_access_token()
        async with self._client(timeout=30) as client:
            resp = await client.get(url, headers=self._auth_headers())
            if resp.status_code == 401 and self._mode == "onprem":
                await self.authenticate()
                resp = await client.get(url, headers=self._auth_headers())
            elif resp.status_code == 401 and self._mode == "cdfmc":
                await self._refresh_cdfmc_access_token()
                resp = await client.get(url, headers=self._auth_headers())
            if not resp.is_success:
                raise AdapterError(f"FMC GET {url} failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def _expand_self_links(self, rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        """Expand summary rows via links.self when available.

        cdFMC list endpoints often return sparse objects. Detail endpoints behind
        links.self carry the fields needed for rich inventory (health, model,
        interface mode/MTU/speed/IP).
        """
        if not self._expand_details:
            return rows
        out: list[dict[str, Any]] = []
        for row in rows:
            self_url = None
            links = row.get("links")
            if isinstance(links, dict):
                self_url = links.get("self")
            if not self_url:
                out.append(row)
                continue
            try:
                full = await self._get_url(str(self_url))
                if isinstance(full, dict):
                    out.append(full)
                else:
                    out.append(row)
            except Exception as exc:
                # Keep summary row if detail lookup fails; do not fail discovery.
                log.debug("fmc.expand_self_link_failed", kind=kind, url=self_url, error=str(exc))
                out.append(row)
        return out

    async def _get_config(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._domain_uuid:
            await self.authenticate()
        if self._mode == "cdfmc":
            await self._ensure_cdfmc_access_token()
        base = self._config_base()
        async with self._client(timeout=30) as client:
            resp = await client.get(
                f"{base}{path}",
                headers=self._auth_headers(),
                params=params,
            )
            if resp.status_code == 401 and self._mode == "onprem":
                # One re-auth retry for short-lived on-prem tokens.
                await self.authenticate()
                resp = await client.get(
                    f"{base}{path}",
                    headers=self._auth_headers(),
                    params=params,
                )
            elif resp.status_code == 401 and self._mode == "cdfmc":
                await self._refresh_cdfmc_access_token()
                resp = await client.get(
                    f"{base}{path}",
                    headers=self._auth_headers(),
                    params=params,
                )
            if not resp.is_success:
                raise AdapterError(
                    f"FMC config GET {path} failed: HTTP {resp.status_code} — {resp.text[:200]}"
                )
            return resp.json()

    async def _get_all_items(self, path: str, limit: int = 500) -> list[dict[str, Any]]:
        # FMC APIs are usually paginated with offset+limit and top-level "items".
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(200):
            payload = await self._get_config(path, params={"limit": limit, "offset": offset})
            if isinstance(payload, list):
                out.extend([x for x in payload if isinstance(x, dict)])
                break
            if not isinstance(payload, dict):
                break
            items = payload.get("items")
            if not isinstance(items, list):
                break
            out.extend([x for x in items if isinstance(x, dict)])
            if len(items) < limit:
                break
            offset += len(items)
        return out

    def _derive_device_status(self, raw: dict[str, Any]) -> str:
        joined = " ".join(
            str(_pick(
                raw.get("healthStatus"),
                raw.get("status"),
                raw.get("metadata", {}).get("state"),
                raw.get("metadata", {}).get("deviceState"),
            ) or "")
            .lower()
            .split()
        )
        if any(k in joined for k in ("down", "unreachable", "failed", "disabled", "offline")):
            return "down"
        if any(k in joined for k in ("warning", "degraded", "alert")):
            return "alerting"
        return "active"

    def _extract_interface_ips(self, iface: dict[str, Any]) -> list[str]:
        ips: list[str] = []
        for key in ("ipAddress", "ipv4Address", "ipv6Address"):
            ip = _valid_ip(iface.get(key))
            if ip:
                ips.append(ip)

        # Common nested forms in FMC object payloads.
        ipv4 = iface.get("ipv4")
        if isinstance(ipv4, dict):
            static = ipv4.get("static")
            if isinstance(static, dict):
                ip = _valid_ip(static.get("address"))
                if ip:
                    ips.append(ip)
        ipv6 = iface.get("ipv6")
        if isinstance(ipv6, dict):
            for addr in ipv6.get("addresses", []) or []:
                if isinstance(addr, dict):
                    ip = _valid_ip(addr.get("address"))
                    if ip:
                        ips.append(ip)
                else:
                    ip = _valid_ip(addr)
                    if ip:
                        ips.append(ip)
        # De-dupe preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for ip in ips:
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
        return out

    async def list_devices(self) -> list[NormalizedDevice]:
        await self.authenticate()
        rows = await self._get_all_items("/devices/devicerecords")
        rows = await self._expand_self_links(rows, kind="device")
        devices: list[NormalizedDevice] = []
        for d in rows:
            did = _pick(d.get("id"), d.get("metadata", {}).get("id"))
            if not did:
                continue
            name = _safe_name(_pick(d.get("name"), d.get("hostName"), str(did))) or str(did)
            model = _pick(d.get("model"), d.get("hardware"), d.get("metadata", {}).get("model"))
            serial = _pick(
                d.get("serialNumber"),
                d.get("metadata", {}).get("chassisData", {}).get("chassisSerialNo"),
                d.get("metadata", {}).get("deviceSerialNumber"),
                d.get("metadata", {}).get("serial"),
            )
            mgmt_ip = _valid_ip(_pick(
                d.get("managementIpAddress"),
                d.get("ipAddress"),
                d.get("metadata", {}).get("managementIpAddress"),
                d.get("hostName"),
            ))
            dev = NormalizedDevice(
                name=name,
                platform=self.name,
                platform_id=str(did),
                role="firewall",
                serial=str(serial) if serial else None,
                mgmt_ip=mgmt_ip,
                status=self._derive_device_status(d),
                access_methods=["https", "ssh"],
                platform_metadata={
                    "model": model,
                    "sw_version": d.get("sw_version"),
                    "deploymentStatus": d.get("deploymentStatus"),
                    "managementState": d.get("managementState"),
                    "isConnected": d.get("isConnected"),
                    "domain_uuid": self._domain_uuid,
                    "deployment_mode": self._mode,
                    "license_caps": d.get("license_caps"),
                    "healthStatus": d.get("healthStatus"),
                    "raw_type": d.get("type"),
                },
            )
            devices.append(dev)
        return devices

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        await self.authenticate()
        interfaces: list[NormalizedInterface] = []

        def _norm_if(raw: dict[str, Any], kind: str) -> NormalizedInterface | None:
            name = _safe_name(_pick(raw.get("name"), raw.get("ifname"), raw.get("interfaceName")))
            if not name:
                return None
            enabled = bool(_pick(raw.get("enabled"), raw.get("isEnabled"), True))
            oper = str(_pick(raw.get("status"), raw.get("linkState"), "") or "").lower()
            if oper not in ("up", "down"):
                oper = None
            return NormalizedInterface(
                name=name,
                device_platform_id=device_id,
                description=_pick(raw.get("description"), raw.get("comment")),
                enabled=enabled,
                oper_status=oper,
                platform_id=str(_pick(raw.get("id"), f"{device_id}:{name}:{kind}")),
                platform_metadata={
                    "kind": kind,
                    "domain_uuid": self._domain_uuid,
                    "deployment_mode": self._mode,
                    **_sanitize_props(raw),
                },
            )

        for path, kind in (
            (f"/devices/devicerecords/{device_id}/physicalinterfaces", "physical"),
            (f"/devices/devicerecords/{device_id}/subinterfaces", "subinterface"),
        ):
            try:
                rows = await self._get_all_items(path)
                rows = await self._expand_self_links(rows, kind=kind)
            except Exception as exc:
                log.debug("fmc.list_interfaces.path_failed", instance=self.instance_id,
                          device_id=device_id, path=path, error=str(exc))
                continue
            for r in rows:
                iface = _norm_if(r, kind)
                if iface:
                    interfaces.append(iface)
        return interfaces

    async def list_vlans(self) -> list[NormalizedVLAN]:
        await self.authenticate()
        vlans: list[NormalizedVLAN] = []
        try:
            rows = await self._get_all_items("/object/vlans")
        except Exception as exc:
            log.debug("fmc.list_vlans.failed", instance=self.instance_id, error=str(exc))
            return vlans
        for v in rows:
            tag = _pick(v.get("vlanTag"), v.get("tag"), v.get("vid"))
            try:
                vid = int(tag)
            except (TypeError, ValueError):
                continue
            name = _safe_name(_pick(v.get("name"), f"VLAN{vid}")) or f"VLAN{vid}"
            vlans.append(NormalizedVLAN(
                vid=vid,
                name=name,
                platform_id=str(v.get("id", f"vlan:{vid}")),
                platform_metadata={
                    "domain_uuid": self._domain_uuid,
                    "deployment_mode": self._mode,
                    **_sanitize_props(v),
                },
            ))
        return vlans

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        # FMC API is management/policy-centric; native LLDP/CDP adjacency is
        # not consistently exposed. Leave neighbor correlation to SNMP/other adapters.
        return []

    async def discover(self) -> GraphData:
        await self.authenticate()
        data = GraphData(adapter_id=self.instance_id)
        adapter_id = self.instance_id

        devices = await self.list_devices()
        vlans = await self.list_vlans()

        # Represent FMC domain as a PlatformSite-like container for all devices
        # discovered through this FMC instance.
        domain_id = self._domain_uuid or "unknown"
        domain_node_id = f"fmc-domain:{self.instance_name}:{domain_id}"
        data.nodes.append(GraphNode(
            id=domain_node_id,
            type=NodeType.PLATFORM_SITE,
            source_adapter=adapter_id,
            dimensions=[Dimension.PHYSICAL, Dimension.ROUTING],
            properties={
                "id": domain_node_id,
                "name": f"FMC Domain {domain_id}",
                "slug": f"fmc-{self.instance_name}-{domain_id[:8]}",
                "platform_type": "fmc",
                "domain_uuid": domain_id,
                "deployment_mode": self._mode,
            },
        ))

        dev_id_map: dict[str, str] = {}
        for d in devices:
            node_id = f"fmc:{self.instance_name}:{d.platform_id}"
            dev_id_map[d.platform_id] = node_id
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                source_adapter=adapter_id,
                dimensions=[Dimension.PHYSICAL, Dimension.ROUTING],
                properties={
                    "id": node_id,
                    "name": d.name,
                    "platform": "fmc",
                    "role": d.role,
                    "serial": d.serial or "",
                    "mgmt_ip": d.mgmt_ip or "",
                    "status": d.status,
                    "access_methods": d.access_methods,
                    **(d.platform_metadata or {}),
                },
            ))
            data.edges.append(GraphEdge(
                source_id=node_id,
                target_id=domain_node_id,
                type=EdgeType.LOCATED_AT,
                source_adapter=adapter_id,
                dimension=Dimension.PHYSICAL,
                properties={
                    "source": "fmc",
                    "dimension": "physical",
                },
            ))

        # VLAN objects (if exposed by FMC instance).
        for v in vlans:
            vlan_id = f"fmc-vlan:{self.instance_name}:{v.vid}"
            data.nodes.append(GraphNode(
                id=vlan_id,
                type=NodeType.VLAN,
                source_adapter=adapter_id,
                dimensions=[Dimension.LOGICAL],
                properties={
                    "id": vlan_id,
                    "vid": v.vid,
                    "name": v.name,
                    "status": v.status,
                    **(v.platform_metadata or {}),
                },
            ))

        # Interfaces + assigned IPs.
        ip_seen: set[str] = set()
        for d in devices:
            try:
                ifaces = await self.list_interfaces(d.platform_id)
            except Exception as exc:
                log.warning("fmc.discover.interfaces_failed",
                            instance=adapter_id, device=d.name, error=str(exc))
                continue
            dev_node_id = dev_id_map.get(d.platform_id)
            if not dev_node_id:
                continue
            for i in ifaces:
                iface_node_id = f"fmc-if:{self.instance_name}:{d.platform_id}:{i.name}"
                i_props = dict(i.platform_metadata or {})
                data.nodes.append(GraphNode(
                    id=iface_node_id,
                    type=NodeType.INTERFACE,
                    source_adapter=adapter_id,
                    dimensions=[Dimension.PHYSICAL, Dimension.ROUTING],
                    properties={
                        "id": iface_node_id,
                        "name": i.name,
                        "device_platform_id": d.platform_id,
                        "description": i.description or "",
                        "enabled": i.enabled,
                        "oper_status": i.oper_status or "",
                        **i_props,
                    },
                ))
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=iface_node_id,
                    type=EdgeType.HAS_INTERFACE,
                    source_adapter=adapter_id,
                    dimension=Dimension.PHYSICAL,
                    properties={
                        "source": "fmc",
                        "dimension": "physical",
                    },
                ))

                for ip in self._extract_interface_ips(i_props):
                    ip_node_id = f"ip:{ip}"
                    if ip_node_id not in ip_seen:
                        ip_seen.add(ip_node_id)
                        ip_ver = "ipv6" if ":" in ip else "ipv4"
                        data.nodes.append(GraphNode(
                            id=ip_node_id,
                            type=NodeType.IP_ADDRESS,
                            source_adapter=adapter_id,
                            dimensions=[Dimension.ROUTING],
                            properties={
                                "id": ip_node_id,
                                "address": ip,
                                "version": ip_ver,
                                "name": ip,
                            },
                        ))
                    data.edges.append(GraphEdge(
                        source_id=iface_node_id,
                        target_id=ip_node_id,
                        type=EdgeType.ASSIGNED_IP,
                        source_adapter=adapter_id,
                        dimension=Dimension.ROUTING,
                        properties={
                            "source": "fmc",
                            "dimension": "routing",
                        },
                    ))

        return data
