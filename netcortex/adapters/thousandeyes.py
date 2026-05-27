"""ThousandEyes adapter.

Targets the ThousandEyes v7 REST API (``https://api.thousandeyes.com/v7``)
with OAuth2 bearer token auth. Verified against the published OpenAPI specs
on developer.cisco.com (Agents 7.0.88, Endpoint Agents 7.0.88, Alerts 7.0.88,
Internet Insights 7.0.85).

Phase 1 scope: vantage points (Cloud / Enterprise / Enterprise Cluster /
Endpoint agents) and their interfaces become nodes in the graph so they can
appear in topology views alongside the rest of the inventory. Alerts and
Internet Insights are queried (when enabled) and stashed on the platform site
node so MCP problem tools can surface them without yet wiring them as graph
nodes.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any

import httpx
import structlog

from netcortex.adapters.base import (
    AdapterError,
    AuthError,
    PlatformAdapter,
    PlatformProfile,
)
from netcortex.graph.models import (
    Dimension,
    EdgeType,
    GraphData,
    GraphEdge,
    GraphNode,
    NodeType,
)
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.thousandeyes.com/v7"

# Endpoint-agent `expand` values we want by default. Pulled directly from the
# v7.0.88 spec — anything not in this list is rejected by the API.
DEFAULT_ENDPOINT_EXPAND = (
    "clients",
    "vpnProfiles",
    "networkInterfaceProfiles",
    "targetVersion",
    "externalMetadata",
)


# ---------------------------------------------------------------------------
# Tiny helpers (mirrored from fmc.py for graph-property safety / IP parsing)
# ---------------------------------------------------------------------------


def _norm_bool(raw: Any, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off", "")
    return default


def _pick(*vals: Any) -> Any:
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _safe_name(text: Any) -> str:
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


def _valid_ip(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if "/" in s:
            return str(ipaddress.ip_interface(s).ip)
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _ip_is_useful(ip: str | None) -> bool:
    """Filter out link-local / loopback / unspecified addresses for mgmt_ip."""
    if not ip:
        return False
    try:
        obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (obj.is_loopback or obj.is_link_local or obj.is_unspecified or obj.is_multicast)


def _is_neo4j_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _to_neo4j_property(value: Any) -> Any:
    """Coerce arbitrary API values into Neo4j property-safe values."""
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
    """Strip identity keys, serialize nested structures."""
    reserved = {"id", "source", "target"}
    return {
        str(k): _to_neo4j_property(v)
        for k, v in raw.items()
        if str(k) not in reserved
    }


def _parse_network_asn(network: str | None) -> tuple[int | None, str | None]:
    """Extract ASN and ISP name from TE's "ISP Name (AS 12345)" string."""
    if not network:
        return None, None
    asn: int | None = None
    name: str | None = None
    try:
        if "(AS" in network and network.endswith(")"):
            head, _, tail = network.rpartition("(AS")
            name = head.strip().rstrip(",") or None
            asn_raw = tail.rstrip(")").strip()
            if asn_raw.isdigit():
                asn = int(asn_raw)
        else:
            name = network.strip() or None
    except Exception:
        name = network.strip() or None
    return asn, name


def _derive_agent_status(raw: dict[str, Any]) -> str:
    """Map a TE agent record to NetCortex device status (active/down/alerting)."""
    enabled = raw.get("enabled")
    if enabled is False:
        return "down"
    state = str(raw.get("agentState") or "").lower()
    if state in {"offline", "disconnected", "down"}:
        return "down"
    if state in {"impaired", "limited", "degraded"}:
        return "alerting"
    errors = raw.get("errorDetails") or []
    if isinstance(errors, list) and errors:
        return "alerting"
    return "active"


def _derive_endpoint_status(raw: dict[str, Any]) -> str:
    status = str(raw.get("status") or "").lower()
    if status == "disabled":
        return "down"
    return "active"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ThousandEyesAdapter(PlatformAdapter):
    name = "thousandeyes"
    display_name = "Cisco ThousandEyes"
    profile = PlatformProfile(
        device_id_field="id",
        role_map={"cloud": "probe", "enterprise": "probe", "enterprise-cluster": "probe",
                  "endpoint": "endpoint"},
        native_topology=False,
        provides_oper_status=True,
        default_access_methods=["https"],
        supported_dimensions=["physical", "logical", "wan"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._api_token = str(config.get("api_token") or "").strip()
        self._base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._aid = str(config.get("aid") or "").strip() or None
        self._verify_ssl = _norm_bool(config.get("verify_ssl", True), default=True)
        self._expand_endpoint = _norm_bool(
            config.get("expand_endpoint_agents", True), default=True
        )
        self._include_ii = _norm_bool(
            config.get("include_internet_insights", False), default=False
        )
        # Cloud agents are operated by ThousandEyes itself, not by the
        # customer. By default we only ingest customer-owned vantage points
        # (Enterprise agents, Enterprise Clusters, and Endpoint agents).
        # Set ``include_cloud_agents: true`` in the secret to also pull in
        # the public TE cloud agent fleet (~1000+ entries).
        self._include_cloud_agents = _norm_bool(
            config.get("include_cloud_agents", False), default=False
        )

        # Populated by authenticate(): the resolved account-group ID + label.
        self._account_label: str | None = None

        # Cached during a single discover() call so we can attach them to the
        # platform-site node without round-tripping a second time.
        self._cached_alerts: list[dict[str, Any]] = []

    # ── HTTP plumbing ─────────────────────────────────────────────────────

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Accept": "application/json",
            },
        )

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self._aid:
            params["aid"] = self._aid
        if extra:
            for k, v in extra.items():
                if v is None or v == "":
                    continue
                params[k] = v
        return params

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        if not self._api_token:
            raise AuthError("thousandeyes api_token is not set")
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        async with self._client(timeout=timeout) as client:
            resp = await client.get(url, params=self._params(params))
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"ThousandEyes auth failed for GET {path}: HTTP {resp.status_code}"
                    f" — {resp.text[:200]}"
                )
            if not resp.is_success:
                raise AdapterError(
                    f"ThousandEyes GET {path} failed: HTTP {resp.status_code}"
                    f" — {resp.text[:200]}"
                )
            return resp.json() if resp.content else {}

    async def _get_paged(
        self,
        path: str,
        items_key: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Walk HAL-style ``_links.next.href`` pagination until exhausted."""
        out: list[dict[str, Any]] = []
        page = await self._get(path, params=params)
        for _ in range(max_pages):
            if not isinstance(page, dict):
                break
            items = page.get(items_key)
            if isinstance(items, list):
                out.extend(x for x in items if isinstance(x, dict))
            elif isinstance(items, dict):
                out.append(items)
            next_link = (page.get("_links") or {}).get("next")
            if not isinstance(next_link, dict):
                break
            href = next_link.get("href")
            if not href:
                break
            # `next` already encodes aid + cursor; don't re-add aid.
            async with self._client(timeout=30) as client:
                resp = await client.get(href)
                if not resp.is_success:
                    break
                page = resp.json() if resp.content else {}
        return out

    # ── Authentication ────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        if not self._api_token:
            raise AuthError("thousandeyes api_token is required")
        # Pick a default account group when one wasn't pinned in the secret.
        # /account-groups is universally available to any user-scoped token.
        if not self._aid:
            try:
                payload = await self._get("/account-groups")
            except AuthError:
                raise
            except Exception as exc:
                raise AdapterError(f"Failed to list account groups: {exc}") from exc
            groups = (payload or {}).get("accountGroups") or []
            default = next(
                (g for g in groups if g.get("isDefaultAccountGroup")),
                groups[0] if groups else None,
            )
            if not default:
                raise AuthError("No account groups visible to this API token")
            self._aid = str(default.get("aid") or default.get("accountGroupName") or "")
            self._account_label = str(
                default.get("accountGroupName") or self._aid or "default"
            )
            log.info(
                "thousandeyes.account_group.resolved",
                instance=self.instance_id,
                aid=self._aid,
                name=self._account_label,
            )
        else:
            self._account_label = self._account_label or self._aid

    # ── Inventory fetchers ────────────────────────────────────────────────

    async def _fetch_agents(self) -> list[dict[str, Any]]:
        return await self._get_paged("/agents", items_key="agents")

    async def _fetch_endpoint_agents(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if self._expand_endpoint:
            # Spec accepts repeated `expand` or comma-separated; commas keep
            # the request URL short and avoid surprises with the next-link
            # serialiser.
            params["expand"] = ",".join(DEFAULT_ENDPOINT_EXPAND)
        return await self._get_paged(
            "/endpoint/agents", items_key="agents", params=params
        )

    async def _fetch_alerts(self) -> list[dict[str, Any]]:
        try:
            return await self._get_paged(
                "/alerts", items_key="alerts", params={"state": "trigger"}
            )
        except Exception as exc:
            log.debug(
                "thousandeyes.alerts.fetch_failed",
                instance=self.instance_id, error=str(exc),
            )
            return []

    async def _fetch_internet_outages(self) -> list[dict[str, Any]]:
        # Internet Insights requires a paying tier and not every token has it.
        # Use a POST filter call per the spec; surface an empty list on 403.
        url = f"{self._base_url}/internet-insights/outages/filter"
        try:
            async with self._client(timeout=30) as client:
                resp = await client.post(
                    url, params=self._params(), json={"window": "1h"}
                )
                if resp.status_code in (401, 403):
                    log.info(
                        "thousandeyes.ii.unavailable",
                        instance=self.instance_id,
                        status=resp.status_code,
                    )
                    return []
                if not resp.is_success:
                    return []
                body = resp.json() if resp.content else {}
                outages: list[dict[str, Any]] = []
                for key in ("networkOutages", "applicationOutages", "outages"):
                    val = body.get(key)
                    if isinstance(val, list):
                        outages.extend(x for x in val if isinstance(x, dict))
                return outages
        except Exception as exc:
            log.debug(
                "thousandeyes.ii.fetch_failed",
                instance=self.instance_id, error=str(exc),
            )
            return []

    # ── PlatformAdapter abstract methods ─────────────────────────────────

    async def list_devices(self) -> list[NormalizedDevice]:
        """Cloud/Enterprise + Endpoint agents flattened into NormalizedDevices.

        Discovery uses the more granular helpers below directly so it can
        attach interfaces, IPs, and cluster relationships at emit time. This
        method exists to satisfy the abstract contract and for ad-hoc tooling.
        """
        await self.authenticate()
        agents = await self._fetch_agents()
        endpoints = await self._fetch_endpoint_agents()
        return [
            *await self._normalize_cloud_enterprise(agents),
            *await self._normalize_endpoints(endpoints),
        ]

    @staticmethod
    def _summarise_vpn(vpn: dict[str, Any]) -> dict[str, Any]:
        return {
            "vpn_type": vpn.get("vpnType"),
            "gateway": vpn.get("vpnGatewayAddress"),
            "client_addresses": vpn.get("vpnClientAddresses"),
            "network_range": vpn.get("vpnClientNetworkRange"),
            "interface": vpn.get("interfaceName"),
        }

    def _endpoint_mgmt_ip(self, endpoint: dict[str, Any]) -> str | None:
        # Prefer the first useful private/global IPv4 from the first non-loopback
        # interface so the device shows up correctly in topology by IP.
        for iface in endpoint.get("networkInterfaceProfiles") or []:
            if not isinstance(iface, dict):
                continue
            if iface.get("hardwareType") == "loopback":
                continue
            for addr in iface.get("addressProfiles") or []:
                if not isinstance(addr, dict):
                    continue
                ip = _valid_ip(addr.get("ipAddress"))
                if _ip_is_useful(ip):
                    return ip
        pub = _valid_ip(endpoint.get("publicIP"))
        return pub if _ip_is_useful(pub) else None

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        # `list_interfaces(device_id)` is the legacy per-device fetch; for TE we
        # already pull interface profiles inline with the endpoint-agent expand.
        # discover() consumes them directly without going through this path,
        # so this method is a stub that satisfies the abstract contract.
        return []

    async def list_vlans(self) -> list[NormalizedVLAN]:
        # TE has no VLAN inventory concept.
        return []

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        # TE doesn't expose CDP/LLDP-style adjacency for agents.
        return []

    # ── Graph emission ────────────────────────────────────────────────────

    def _platform_site_id(self) -> str:
        aid = self._aid or "default"
        return f"te-account:{self.instance_name}:{aid}"

    def _agent_node_id(self, kind: str, agent_id: str) -> str:
        return f"te-{kind}:{self.instance_name}:{agent_id}"

    def _iface_node_id(self, agent_id: str, iface_name: str) -> str:
        return f"te-if:{self.instance_name}:{agent_id}:{iface_name}"

    def _emit_platform_site(self, data: GraphData) -> str:
        site_id = self._platform_site_id()
        data.nodes.append(GraphNode(
            id=site_id,
            type=NodeType.PLATFORM_SITE,
            source_adapter=self.instance_id,
            dimensions=[Dimension.PHYSICAL, Dimension.WAN],
            properties={
                "id": site_id,
                "name": f"ThousandEyes ({self._account_label or self._aid or 'default'})",
                "slug": f"te-{self.instance_name}-{(self._aid or 'default')[:12]}",
                "platform_type": "thousandeyes",
                "aid": self._aid or "",
                "te_account_name": self._account_label or "",
                "base_url": self._base_url,
                "alerts_active": len(self._cached_alerts),
            },
        ))
        return site_id

    def _emit_device(
        self,
        data: GraphData,
        site_id: str,
        dev: NormalizedDevice,
        kind: str,
    ) -> str:
        node_id = self._agent_node_id(kind, dev.platform_id)
        meta = dict(dev.platform_metadata or {})
        meta = _sanitize_props(meta)
        data.nodes.append(GraphNode(
            id=node_id,
            type=NodeType.DEVICE,
            source_adapter=self.instance_id,
            dimensions=[Dimension.PHYSICAL, Dimension.WAN],
            properties={
                "id": node_id,
                "name": dev.name,
                "platform": self.name,
                "role": dev.role,
                "serial": dev.serial or "",
                "mgmt_ip": dev.mgmt_ip or "",
                "status": dev.status,
                "access_methods": dev.access_methods,
                **meta,
            },
        ))
        data.edges.append(GraphEdge(
            source_id=node_id,
            target_id=site_id,
            type=EdgeType.LOCATED_AT,
            source_adapter=self.instance_id,
            dimension=Dimension.PHYSICAL,
            properties={"source": self.name, "dimension": "physical"},
        ))
        return node_id

    def _emit_enterprise_interfaces(
        self,
        data: GraphData,
        device_node_id: str,
        agent_record: dict[str, Any],
        ip_seen: set[str],
    ) -> None:
        """Synthesize an Interface + IPAddress per agent IP.

        TE Enterprise agents are software vantage points (typically a Cisco
        TE-VA appliance, a TE-on-CSP appliance, or a VM). The API exposes
        the agent's configured IPs in ``ipAddresses`` but no LLDP/CDP
        neighbours and no interface inventory. We emit a synthetic
        Interface for each IP so the existing ARP/MAC/CAM correlator can
        stitch the agent onto its actual upstream switch port through
        SNMP-discovered ARP bindings.
        """
        agent_id = str(_pick(agent_record.get("agentId"), agent_record.get("id")) or "")
        if not agent_id:
            return
        ip_list: list[str] = []
        for ip_raw in agent_record.get("ipAddresses") or []:
            ip = _valid_ip(ip_raw)
            if ip and _ip_is_useful(ip) and ip not in ip_list:
                ip_list.append(ip)
        if not ip_list:
            return
        for idx, ip in enumerate(ip_list):
            iface_name = f"eth{idx}"
            iface_node_id = self._iface_node_id(agent_id, iface_name)
            data.nodes.append(GraphNode(
                id=iface_node_id,
                type=NodeType.INTERFACE,
                source_adapter=self.instance_id,
                dimensions=[Dimension.PHYSICAL, Dimension.LOGICAL],
                properties={
                    "id": iface_node_id,
                    "name": iface_name,
                    "device_platform_id": agent_id,
                    "enabled": True,
                    "oper_status": "up",
                    "synthetic": True,
                    "synthetic_reason": "te_api_no_interface_inventory",
                },
            ))
            data.edges.append(GraphEdge(
                source_id=device_node_id,
                target_id=iface_node_id,
                type=EdgeType.HAS_INTERFACE,
                source_adapter=self.instance_id,
                dimension=Dimension.PHYSICAL,
                properties={"source": self.name, "dimension": "physical"},
            ))
            ip_node_id = f"ip:{ip}"
            if ip_node_id not in ip_seen:
                ip_seen.add(ip_node_id)
                data.nodes.append(GraphNode(
                    id=ip_node_id,
                    type=NodeType.IP_ADDRESS,
                    source_adapter=self.instance_id,
                    dimensions=[Dimension.ROUTING],
                    properties={
                        "id": ip_node_id,
                        "address": ip,
                        "version": "ipv6" if ":" in ip else "ipv4",
                        "name": ip,
                    },
                ))
            data.edges.append(GraphEdge(
                source_id=iface_node_id,
                target_id=ip_node_id,
                type=EdgeType.ASSIGNED_IP,
                source_adapter=self.instance_id,
                dimension=Dimension.ROUTING,
                properties={"source": self.name, "dimension": "routing"},
            ))

    def _emit_endpoint_interfaces(
        self,
        data: GraphData,
        device_node_id: str,
        endpoint: dict[str, Any],
        ip_seen: set[str],
    ) -> None:
        agent_id = str(endpoint.get("id") or "")
        if not agent_id:
            return
        for iface in endpoint.get("networkInterfaceProfiles") or []:
            if not isinstance(iface, dict):
                continue
            name = _safe_name(iface.get("interfaceName"))
            if not name:
                continue
            iface_node_id = self._iface_node_id(agent_id, name)
            wireless = iface.get("wirelessProfile") or {}
            ethernet = iface.get("ethernetProfile") or {}
            props: dict[str, Any] = {
                "id": iface_node_id,
                "name": name,
                "device_platform_id": agent_id,
                "enabled": True,
                "oper_status": "up",
                "hardware_type": iface.get("hardwareType") or "",
                "link_speed_mbps": ethernet.get("linkSpeed"),
                "ssid": wireless.get("ssid"),
                "bssid": wireless.get("bssid"),
                "rssi": wireless.get("rssi"),
                "channel": wireless.get("channel"),
                "phy_mode": wireless.get("phyMode"),
            }
            props = {k: v for k, v in props.items() if v is not None}
            data.nodes.append(GraphNode(
                id=iface_node_id,
                type=NodeType.INTERFACE,
                source_adapter=self.instance_id,
                dimensions=[Dimension.PHYSICAL, Dimension.LOGICAL],
                properties=props,
            ))
            data.edges.append(GraphEdge(
                source_id=device_node_id,
                target_id=iface_node_id,
                type=EdgeType.HAS_INTERFACE,
                source_adapter=self.instance_id,
                dimension=Dimension.PHYSICAL,
                properties={"source": self.name, "dimension": "physical"},
            ))
            for addr in iface.get("addressProfiles") or []:
                if not isinstance(addr, dict):
                    continue
                ip = _valid_ip(addr.get("ipAddress"))
                if not _ip_is_useful(ip):
                    continue
                ip_node_id = f"ip:{ip}"
                if ip_node_id not in ip_seen:
                    ip_seen.add(ip_node_id)
                    data.nodes.append(GraphNode(
                        id=ip_node_id,
                        type=NodeType.IP_ADDRESS,
                        source_adapter=self.instance_id,
                        dimensions=[Dimension.ROUTING],
                        properties={
                            "id": ip_node_id,
                            "address": ip,
                            "version": "ipv6" if ":" in ip else "ipv4",
                            "name": ip,
                        },
                    ))
                data.edges.append(GraphEdge(
                    source_id=iface_node_id,
                    target_id=ip_node_id,
                    type=EdgeType.ASSIGNED_IP,
                    source_adapter=self.instance_id,
                    dimension=Dimension.ROUTING,
                    properties={
                        "source": self.name,
                        "dimension": "routing",
                        "prefix_length": addr.get("prefixLength"),
                        "gateway": addr.get("gateway"),
                        "address_type": addr.get("addressType"),
                    },
                ))

    async def discover(self) -> GraphData:
        await self.authenticate()
        data = GraphData(adapter_id=self.instance_id)

        # Pull everything in parallel-ish but sequentially is fine for now —
        # TE rate-limits aggressively and our cycles are infrequent.
        self._cached_alerts = await self._fetch_alerts()
        agents = await self._fetch_agents()
        endpoint_records = await self._fetch_endpoint_agents()
        outages = await self._fetch_internet_outages() if self._include_ii else []

        site_id = self._emit_platform_site(data)

        # Index endpoint records by id so we can hand them to interface emission.
        endpoint_by_id: dict[str, dict[str, Any]] = {
            str(e.get("id")): e for e in endpoint_records if e.get("id")
        }

        ip_seen: set[str] = set()

        # Index raw cloud/enterprise records by id so we can hand them back
        # to the interface emitter without re-fetching.
        agent_by_id: dict[str, dict[str, Any]] = {}
        for a in agents:
            agent_id = str(_pick(a.get("agentId"), a.get("id")) or "").strip()
            if agent_id:
                agent_by_id[agent_id] = a

        # Cloud + Enterprise + Enterprise Cluster agents.
        cloud_devices = [
            d for d in await self._normalize_cloud_enterprise(agents) if d.platform_id
        ]
        agent_node_by_id: dict[str, str] = {}
        for dev in cloud_devices:
            meta = dev.platform_metadata or {}
            kind = meta.get("te_agent_type") or "agent"
            node_id = self._emit_device(data, site_id, dev, kind=f"agent-{kind}")
            agent_node_by_id[dev.platform_id] = node_id
            # Enterprise and Enterprise-Cluster agents have local IPs we can
            # anchor for cross-adapter ARP/MAC correlation. Cloud agents
            # (only ingested when explicitly opted in) get the same
            # treatment for consistency.
            self._emit_enterprise_interfaces(
                data,
                node_id,
                agent_by_id.get(dev.platform_id, {}),
                ip_seen,
            )

        # Wire enterprise-cluster members to their parent cluster node.
        for dev in cloud_devices:
            members = (dev.platform_metadata or {}).get("cluster_members") or []
            parent_node = agent_node_by_id.get(dev.platform_id)
            if not parent_node:
                continue
            for member_id in members:
                if member_id is None:
                    continue
                child = agent_node_by_id.get(str(member_id).strip())
                if child and child != parent_node:
                    data.edges.append(GraphEdge(
                        source_id=child,
                        target_id=parent_node,
                        type=EdgeType.LOCATED_AT,
                        source_adapter=self.instance_id,
                        dimension=Dimension.PHYSICAL,
                        properties={
                            "source": self.name,
                            "dimension": "physical",
                            "relationship": "cluster_member",
                        },
                    ))

        # Endpoint agents (separate kind so node IDs don't collide with cloud IDs).
        endpoint_devices = await self._normalize_endpoints(endpoint_records)
        for dev in endpoint_devices:
            node_id = self._emit_device(data, site_id, dev, kind="endpoint")
            self._emit_endpoint_interfaces(
                data,
                node_id,
                endpoint_by_id.get(dev.platform_id, {}),
                ip_seen,
            )

        if outages:
            site_node = next(
                (n for n in data.nodes if n.id == site_id), None
            )
            if site_node is not None:
                site_node.properties["internet_outages_recent"] = len(outages)

        log.info(
            "thousandeyes.discover.complete",
            instance=self.instance_id,
            cloud_enterprise=len(cloud_devices),
            endpoints=len(endpoint_devices),
            alerts=len(self._cached_alerts),
            outages=len(outages),
        )
        return data

    async def _normalize_cloud_enterprise(
        self, agents: list[dict[str, Any]]
    ) -> list[NormalizedDevice]:
        devices: list[NormalizedDevice] = []
        skipped_cloud = 0
        for a in agents:
            agent_type = str(a.get("agentType") or "").lower()
            agent_id = str(_pick(a.get("agentId"), a.get("id")) or "").strip()
            if not agent_id:
                continue
            # Filter out TE's own cloud fleet unless explicitly enabled.
            # The /agents endpoint returns the full Cloud + Enterprise
            # superset and there is no API-side filter to ask for only
            # customer-owned vantage points.
            if agent_type == "cloud" and not self._include_cloud_agents:
                skipped_cloud += 1
                continue
            name = _safe_name(_pick(a.get("agentName"), a.get("hostname"), agent_id))
            mgmt_ip_raw = next(
                (ip for ip in a.get("ipAddresses") or [] if _ip_is_useful(_valid_ip(ip))),
                None,
            )
            if not mgmt_ip_raw:
                pubs = a.get("publicIpAddresses") or []
                mgmt_ip_raw = next(
                    (ip for ip in pubs if _ip_is_useful(_valid_ip(ip))), None
                )
            mgmt_ip = _valid_ip(mgmt_ip_raw)
            asn, isp = _parse_network_asn(a.get("network"))
            cluster_members = [
                str(m.get("memberId") or m.get("agentId"))
                for m in (a.get("clusterMembers") or [])
                if isinstance(m, dict) and (m.get("memberId") or m.get("agentId"))
            ]
            devices.append(NormalizedDevice(
                name=name or agent_id,
                platform=self.name,
                platform_id=agent_id,
                role=self.profile.role_map.get(agent_type, "probe"),
                mgmt_ip=mgmt_ip,
                status=_derive_agent_status(a),
                access_methods=list(self.profile.default_access_methods),
                platform_metadata={
                    "te_agent_type": agent_type or "unknown",
                    "country_id": a.get("countryId"),
                    "location": a.get("location"),
                    "hostname": a.get("hostname"),
                    "version": a.get("version"),
                    "last_seen": a.get("lastSeen"),
                    "public_ip": next(iter(a.get("publicIpAddresses") or []), None),
                    "prefix": a.get("prefix"),
                    "isp": isp,
                    "asn": asn,
                    "enabled": a.get("enabled"),
                    "cluster_members": cluster_members,
                    "aid": self._aid,
                    "te_account_name": self._account_label,
                },
            ))
        if skipped_cloud:
            log.info(
                "thousandeyes.cloud_agents.filtered",
                instance=self.instance_id,
                skipped=skipped_cloud,
                hint="set include_cloud_agents=true in the secret to ingest them",
            )
        return devices

    async def _normalize_endpoints(
        self, endpoints: list[dict[str, Any]]
    ) -> list[NormalizedDevice]:
        devices: list[NormalizedDevice] = []
        for e in endpoints:
            agent_id = str(e.get("id") or "").strip()
            if not agent_id:
                continue
            name = _safe_name(_pick(e.get("name"), e.get("computerName"), agent_id))
            mgmt_ip = self._endpoint_mgmt_ip(e)
            asn = (e.get("asnDetails") or {}).get("asNumber")
            isp = (e.get("asnDetails") or {}).get("asName")
            location = e.get("location") or {}
            devices.append(NormalizedDevice(
                name=name or agent_id,
                platform=self.name,
                platform_id=agent_id,
                role=self.profile.role_map.get("endpoint", "endpoint"),
                serial=e.get("serialNumber"),
                mgmt_ip=mgmt_ip,
                status=_derive_endpoint_status(e),
                access_methods=list(self.profile.default_access_methods),
                platform_metadata={
                    "te_agent_type": "endpoint",
                    "computer_name": e.get("computerName"),
                    "os_version": e.get("osVersion"),
                    "platform_os": e.get("platform"),
                    "kernel_version": e.get("kernelVersion"),
                    "manufacturer": e.get("manufacturer"),
                    "model": e.get("model"),
                    "version": e.get("version"),
                    "target_version": e.get("targetVersion"),
                    "last_seen": e.get("lastSeen"),
                    "public_ip": e.get("publicIP"),
                    "asn": asn,
                    "isp": isp,
                    "license_type": e.get("licenseType"),
                    "latitude": location.get("latitude"),
                    "longitude": location.get("longitude"),
                    "location_name": location.get("locationName"),
                    "vpn_profiles": [
                        self._summarise_vpn(v)
                        for v in (e.get("vpnProfiles") or [])
                        if isinstance(v, dict)
                    ],
                    "client_count": e.get("numberOfClients"),
                    "aid": self._aid,
                    "te_account_name": self._account_label,
                },
            ))
        return devices

    # ── Health ────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        try:
            await self.authenticate()
            return {
                "status": "ok",
                "instance_id": self.instance_id,
                "aid": self._aid,
                "account": self._account_label,
            }
        except AuthError as exc:
            return {
                "status": "error",
                "instance_id": self.instance_id,
                "message": f"auth failed: {exc}",
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "instance_id": self.instance_id,
                "message": str(exc),
            }
