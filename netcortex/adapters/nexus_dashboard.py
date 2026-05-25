"""Cisco Nexus Dashboard / NDFC adapter.

Nexus Dashboard (ND) is the umbrella management platform for:
  - Nexus Dashboard Fabric Controller (NDFC, formerly DCNM)
  - Nexus Dashboard Insights (NDI)
  - Nexus Dashboard Orchestrator (NDO / MSO)

This adapter targets NDFC for fabric topology, inventory, and EVPN/VXLAN
overlay data. Multiple ND instances are supported — one per configured name.

API reference: https://developer.cisco.com/docs/nexus-dashboard/
Tested against NDFC 12.x
"""

from __future__ import annotations

import re
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
from netcortex.util.ifname import normalize_ifname

log = structlog.get_logger(__name__)

ROLE_MAP = {
    "switch": "switch",
    "spine": "switch",
    "leaf": "switch",
    "border_leaf": "switch",
    "border-leaf": "switch",
    "border_gateway": "switch",
    "super_spine": "switch",
}

# NDFC device role → graph label suffix (used for properties)
NDFC_SWITCH_ROLES = {"spine", "leaf", "border_leaf", "border-leaf", "border_gateway", "super_spine"}


class NexusDashboardAdapter(PlatformAdapter):
    name = "nexus_dashboard"
    display_name = "Cisco Nexus Dashboard"
    profile = PlatformProfile(
        device_id_field="serial",
        role_map=ROLE_MAP,
        native_topology=True,
        provides_oper_status=True,
        default_access_methods=["netconf", "nxapi", "ssh"],
        netbox_platform_slug="nxos",
        supported_dimensions=["physical", "logical", "routing", "fabric"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._base_url: str = config["url"].rstrip("/")
        self._username: str = config["username"]
        self._password: str = config["password"]
        raw_verify = config.get("verify_ssl", True)
        # AWS SM may deliver booleans as strings ("false"/"true") — normalise
        if isinstance(raw_verify, str):
            self._verify_ssl: bool = raw_verify.strip().lower() not in ("false", "0", "no")
        else:
            self._verify_ssl = bool(raw_verify)
        self._token: str | None = None

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            follow_redirects=True,
            timeout=timeout,
        )

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("Not authenticated — call authenticate() first")
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def authenticate(self) -> None:
        """Authenticate to Nexus Dashboard and obtain a JWT token."""
        async with self._client(timeout=15) as client:
            resp = await client.post(
                f"{self._base_url}/login",
                json={"userName": self._username, "userPasswd": self._password, "domain": "local"},
            )
            if resp.status_code in (401, 403):
                raise AuthError(f"Nexus Dashboard authentication failed: HTTP {resp.status_code}")
            if not resp.is_success:
                raise AdapterError(f"Nexus Dashboard auth error: HTTP {resp.status_code} — {resp.text[:200]}")

            body = resp.json()
            # ND returns token under different keys depending on version
            self._token = (
                body.get("jwttoken")
                or body.get("token")
                or body.get("accessToken")
            )
            if not self._token:
                raise AuthError(f"Nexus Dashboard returned no token. Response keys: {list(body.keys())}")
        log.debug("ndfc.authenticated", instance=self.instance_id)

    async def _get(self, path: str, params: dict | None = None, timeout: float = 30.0) -> Any:
        """Authenticated GET with automatic re-auth on 401."""
        if not self._token:
            await self.authenticate()
        async with self._client(timeout=timeout) as client:
            resp = await client.get(
                f"{self._base_url}{path}",
                headers=self._headers(),
                params=params,
            )
            if resp.status_code == 401:
                await self.authenticate()
                resp = await client.get(
                    f"{self._base_url}{path}",
                    headers=self._headers(),
                    params=params,
                )
            if not resp.is_success:
                raise AdapterError(f"NDFC GET {path} failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def list_fabrics(self) -> list[dict]:
        """Return all fabrics managed by NDFC."""
        try:
            data = await self._get(
                "/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/fabrics"
            )
            return data if isinstance(data, list) else data.get("DATA", [])
        except Exception as exc:
            log.warning("ndfc.list_fabrics_failed", error=str(exc))
            return []

    async def list_devices(self) -> list[NormalizedDevice]:
        """Return all switches from NDFC.

        Tries the global allswitches endpoint first (works on most NDFC 12.x
        builds), falling back to per-fabric inventory for older versions.
        """
        all_items: list[dict] = []

        # Try global endpoint first
        try:
            data = await self._get(
                "/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/inventory/allswitches"
            )
            all_items = data if isinstance(data, list) else data.get("DATA", [])
        except Exception:
            pass

        # Fall back to per-fabric if global returned nothing
        if not all_items:
            fabrics = await self.list_fabrics()
            for fabric in fabrics:
                fabric_name = fabric.get("fabricName") or fabric.get("name", "")
                if not fabric_name:
                    continue
                for path in [
                    f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/fabrics/{fabric_name}/inventory/allswitches",
                    f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/fabrics/{fabric_name}/inventory",
                ]:
                    try:
                        data = await self._get(path)
                        items = data if isinstance(data, list) else data.get("DATA", [])
                        if items:
                            for item in items:
                                item.setdefault("fabricName", fabric_name)
                            all_items.extend(items)
                            break
                    except Exception as exc:
                        log.debug("ndfc.list_devices.path_failed", path=path, error=str(exc))

        devices: list[NormalizedDevice] = []
        for d in all_items:
            serial = d.get("serialNumber", d.get("sysName", ""))
            role_raw = d.get("switchRole", d.get("role", "leaf")).lower().replace(" ", "_")
            devices.append(NormalizedDevice(
                name=d.get("logicalName") or d.get("sysName") or serial,
                platform=self.name,
                platform_id=serial,
                role=ROLE_MAP.get(role_raw, "switch"),
                serial=serial,
                mgmt_ip=d.get("ipAddress") or d.get("managementIp"),
                status=d.get("operStatus") or d.get("status") or "active",
                platform_metadata={
                    "switchRole": role_raw,
                    "fabricName": d.get("fabricName"),
                    "model": d.get("model"),
                    "release": d.get("release"),
                    "os_version": d.get("release") or "",
                    "status": d.get("operStatus") or d.get("status") or d.get("systemMode") or "active",
                    "vpcRole": d.get("vpcRole"),
                    "vpcPeerIp": d.get("peerIp"),
                    "vtepIp": d.get("vtepIp") or d.get("dtepIp"),
                    "bgpAsn": d.get("bgpAsn"),
                    "systemMode": d.get("systemMode"),
                },
            ))
        return devices

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return interfaces for a device by serial number."""
        data = await self._get(
            "/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/monitor/interfaces",
            params={"serialNumber": device_id},
        )
        items = data if isinstance(data, list) else data.get("DATA", [])
        interfaces = []
        for iface in items:
            interfaces.append(NormalizedInterface(
                name=iface.get("ifName", ""),
                device_platform_id=device_id,
                description=iface.get("description", ""),
                enabled=iface.get("operStatus", "up").lower() == "up",
                platform_id=f"{device_id}:{iface.get('ifName', '')}",
                platform_metadata={
                    "adminStatus": iface.get("adminStatus"),
                    "operStatus": iface.get("operStatus"),
                    "speed": iface.get("speed"),
                    "mtu": iface.get("mtu"),
                    "ipAddress": iface.get("ipAddress"),
                },
            ))
        return interfaces

    async def list_vlans(self) -> list[NormalizedVLAN]:
        """Return VLANs/networks from all fabrics."""
        vlans: list[NormalizedVLAN] = []
        fabrics = await self.list_fabrics()
        for fabric in fabrics:
            fabric_name = fabric.get("fabricName", fabric.get("name", ""))
            if not fabric_name:
                continue
            try:
                data = await self._get(
                    f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/top-down/fabrics/{fabric_name}/networks"
                )
                items = data if isinstance(data, list) else data.get("DATA", [])
                for net in items:
                    template = net.get("networkTemplateConfig", {})
                    if isinstance(template, str):
                        import json
                        try:
                            template = json.loads(template)
                        except Exception:
                            template = {}
                    vid = template.get("vlanId") or net.get("vlanId")
                    vni = template.get("vni") or net.get("vni")
                    if vid:
                        vlans.append(NormalizedVLAN(
                            vid=int(vid),
                            name=net.get("networkName", f"VLAN{vid}"),
                            platform_id=f"{fabric_name}:{vid}",
                            platform_metadata={"vni": vni, "fabricName": fabric_name},
                        ))
            except Exception as exc:
                log.debug("ndfc.list_vlans.fabric_failed", fabric=fabric_name, error=str(exc))
        return vlans

    async def get_fabric_links(self, fabric_name: str) -> list[dict]:
        """Return inter-switch links for a fabric.

        NDFC stores links in the control/links endpoint, filtered by fabric name.
        Each link has sw1-info and sw2-info sub-dicts with sw-serial-number and
        if-name fields confirmed from the NDFC MCP server response.
        """
        try:
            data = await self._get(
                "/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/links",
                params={"fabric-name": fabric_name},
            )
            links = data if isinstance(data, list) else data.get("DATA", [])
            return [lnk for lnk in links if lnk.get("link-type") == "ethisl"]
        except Exception as exc:
            log.debug("ndfc.get_fabric_links_failed", fabric=fabric_name, error=str(exc))
            return []

    async def get_fabric_topology(self, fabric_name: str) -> dict:
        """Legacy — superseded by get_fabric_links(); kept for compatibility."""
        return {}

    async def get_vrf_list(self, fabric_name: str) -> list[dict]:
        """Return VRFs for a fabric."""
        try:
            data = await self._get(
                f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/top-down/fabrics/{fabric_name}/vrfs"
            )
            return data if isinstance(data, list) else data.get("DATA", [])
        except Exception:
            return []

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        return []

    # ── Graph discovery ───────────────────────────────────────────────────────

    async def discover(self) -> GraphData:
        """Build a full EVPN/VXLAN fabric graph from Nexus Dashboard."""
        data = GraphData(adapter_id=self.instance_id)
        await self.authenticate()

        # 1. Fabrics → Site nodes
        fabrics = await self.list_fabrics()
        fabric_names = []
        for fabric in fabrics:
            fabric_name = fabric.get("fabricName") or fabric.get("name", "")
            if not fabric_name:
                continue
            fabric_names.append(fabric_name)
            fabric_node_id = f"ndfc-fabric:{self.instance_name}:{fabric_name}"
            data.nodes.append(GraphNode(
                id=fabric_node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL, Dimension.FABRIC],
                source_adapter=self.instance_id,
                properties={
                    "name": fabric_name,
                    "slug": fabric_name.lower().replace(" ", "-"),
                    "fabric_type": fabric.get("fabricType", ""),
                    "template": fabric.get("templateName", ""),
                    "asn": fabric.get("bgpAsn", ""),
                    "platform": "nexus_dashboard",
                    "normalized_name": re.sub(r"[^a-z0-9]+", "", (fabric_name or "").lower()),
                },
            ))

        # 2. Devices → Device nodes
        try:
            devices = await self.list_devices()
        except Exception as exc:
            log.warning("ndfc.discover.devices_failed", error=str(exc))
            devices = []

        device_node_map: dict[str, str] = {}
        for dev in devices:
            node_id = f"ndfc:{self.instance_name}:{dev.platform_id}"
            device_node_map[dev.platform_id] = node_id
            meta = {k: v for k, v in dev.platform_metadata.items() if v is not None}
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.PHYSICAL, Dimension.FABRIC],
                source_adapter=self.instance_id,
                properties={
                    "name": dev.name,
                    "platform": dev.platform,
                    "platform_id": dev.platform_id,
                    "role": dev.role,
                    "serial": dev.serial or "",
                    # Only include mgmt_ip when truthy — writing an empty
                    # string would clobber a previously-good value from
                    # another adapter via `SET n += row` (Pydantic 0.5.0-dev1).
                    **({"mgmt_ip": dev.mgmt_ip} if dev.mgmt_ip else {}),
                    **meta,
                },
            ))
            # LOCATED_AT → fabric site node
            fabric_name = dev.platform_metadata.get("fabricName", "")
            fabric_node_id = f"ndfc-fabric:{self.instance_name}:{fabric_name}"
            if fabric_name and any(n.id == fabric_node_id for n in data.nodes):
                data.edges.append(GraphEdge(
                    source_id=node_id,
                    target_id=fabric_node_id,
                    type=EdgeType.LOCATED_AT,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))

            # VTEP IP as node property — used for FABRIC_PEER edges later
            vtep_ip = dev.platform_metadata.get("vtepIp")
            if vtep_ip:
                vtep_node_id = f"ndfc-vtep:{vtep_ip}"
                if not any(n.id == vtep_node_id for n in data.nodes):
                    data.nodes.append(GraphNode(
                        id=vtep_node_id,
                        type=NodeType.IP_ADDRESS,
                        dimensions=[Dimension.FABRIC],
                        source_adapter=self.instance_id,
                        properties={"address": vtep_ip, "role": "vtep"},
                    ))
                data.edges.append(GraphEdge(
                    source_id=node_id,
                    target_id=vtep_node_id,
                    type=EdgeType.ASSIGNED_IP,
                    dimension=Dimension.FABRIC,
                    source_adapter=self.instance_id,
                ))

        # 3. Physical topology per fabric → PHYSICAL_LINK edges
        # Uses get_fabric_links() which returns NDFC ethisl links with
        # sw1-info/sw2-info dicts keyed by sw-serial-number and if-name.
        for fabric_name in fabric_names:
            try:
                links = await self.get_fabric_links(fabric_name)
                seen_links: set[frozenset] = set()
                for link in links:
                    sw1 = link.get("sw1-info", {})
                    sw2 = link.get("sw2-info", {})
                    src_serial = sw1.get("sw-serial-number", "")
                    dst_serial = sw2.get("sw-serial-number", "")
                    src_node = device_node_map.get(src_serial, "")
                    dst_node = device_node_map.get(dst_serial, "")
                    if not src_node or not dst_node or src_node == dst_node:
                        continue
                    link_key = frozenset([src_node, dst_node])
                    if link_key in seen_links:
                        continue
                    seen_links.add(link_key)
                    data.edges.append(GraphEdge(
                        source_id=src_node,
                        target_id=dst_node,
                        type=EdgeType.PHYSICAL_LINK,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                        properties={
                            "fabric": fabric_name,
                            "interface_a": normalize_ifname(sw1.get("if-name", "")),
                            "interface_b": normalize_ifname(sw2.get("if-name", "")),
                            "interface_a_raw": sw1.get("if-name", ""),
                            "interface_b_raw": sw2.get("if-name", ""),
                            "link_type": link.get("link-type", ""),
                        },
                    ))
            except Exception as exc:
                log.warning("ndfc.discover.topo_failed", fabric=fabric_name, error=str(exc))

        # 4. VRFs → VRF nodes + VRF_MEMBER edges
        for fabric_name in fabric_names:
            try:
                vrfs = await self.get_vrf_list(fabric_name)
                for vrf in vrfs:
                    vrf_name = vrf.get("vrfName", "")
                    if not vrf_name:
                        continue
                    vrf_node_id = f"ndfc-vrf:{self.instance_name}:{fabric_name}:{vrf_name}"
                    template = vrf.get("vrfTemplateConfig", {})
                    if isinstance(template, str):
                        import json
                        try:
                            template = json.loads(template)
                        except Exception:
                            template = {}
                    data.nodes.append(GraphNode(
                        id=vrf_node_id,
                        type=NodeType.VRF,
                        dimensions=[Dimension.ROUTING, Dimension.FABRIC],
                        source_adapter=self.instance_id,
                        properties={
                            "name": vrf_name,
                            "fabric": fabric_name,
                            "vni": template.get("vrfSegmentId") or vrf.get("vrfId"),
                            "rd": template.get("vrfRd", ""),
                            "rt_import": template.get("routeTargetImport", ""),
                            "rt_export": template.get("routeTargetExport", ""),
                        },
                    ))
            except Exception as exc:
                log.warning("ndfc.discover.vrfs_failed", fabric=fabric_name, error=str(exc))

        # 5. Networks (VLANs + VNIs) → VLAN + VNI nodes + VNI_EXTENDS edges
        try:
            vlans = await self.list_vlans()
            for vlan in vlans:
                vlan_node_id = f"ndfc-vlan:{self.instance_name}:{vlan.platform_id}"
                fabric_name = vlan.platform_metadata.get("fabricName", "") if vlan.platform_metadata else ""
                data.nodes.append(GraphNode(
                    id=vlan_node_id,
                    type=NodeType.VLAN,
                    dimensions=[Dimension.LOGICAL, Dimension.FABRIC, Dimension.VIRTUAL],
                    source_adapter=self.instance_id,
                    properties={
                        "name": vlan.name,
                        "vlan_id": vlan.vid,
                        "fabric": fabric_name,
                    },
                ))
                vni = vlan.platform_metadata.get("vni") if vlan.platform_metadata else None
                if vni:
                    vni_node_id = f"ndfc-vni:{self.instance_name}:{vni}"
                    if not any(n.id == vni_node_id for n in data.nodes):
                        data.nodes.append(GraphNode(
                            id=vni_node_id,
                            type=NodeType.VNI,
                            dimensions=[Dimension.FABRIC],
                            source_adapter=self.instance_id,
                            properties={"vni_id": int(vni), "name": f"VNI-{vni}"},
                        ))
                    data.edges.append(GraphEdge(
                        source_id=vni_node_id,
                        target_id=vlan_node_id,
                        type=EdgeType.VNI_EXTENDS,
                        dimension=Dimension.FABRIC,
                        source_adapter=self.instance_id,
                    ))
                # Connect every switch in the same fabric to this VLAN so the
                # VLAN node appears in the logical/virtual topology view.
                for dev in devices:
                    if dev.platform_metadata.get("fabricName") == fabric_name:
                        dev_node = device_node_map.get(dev.platform_id, "")
                        if dev_node:
                            data.edges.append(GraphEdge(
                                source_id=dev_node,
                                target_id=vlan_node_id,
                                type=EdgeType.LOGICAL_MEMBER,
                                dimension=Dimension.LOGICAL,
                                source_adapter=self.instance_id,
                                properties={"fabric": fabric_name},
                            ))
        except Exception as exc:
            log.warning("ndfc.discover.vlans_failed", error=str(exc))

        # 6. FABRIC_PEER edges between VTEPs (devices with vtepIp)
        vtep_devices = [
            dev for dev in devices if dev.platform_metadata.get("vtepIp")
        ]
        for dev_a in vtep_devices:
            for dev_b in vtep_devices:
                if dev_a.platform_id >= dev_b.platform_id:
                    continue
                if dev_a.platform_metadata.get("fabricName") != dev_b.platform_metadata.get("fabricName"):
                    continue
                src = device_node_map.get(dev_a.platform_id)
                dst = device_node_map.get(dev_b.platform_id)
                if src and dst:
                    data.edges.append(GraphEdge(
                        source_id=src,
                        target_id=dst,
                        type=EdgeType.FABRIC_PEER,
                        dimension=Dimension.FABRIC,
                        source_adapter=self.instance_id,
                        properties={
                            "vtep_a": dev_a.platform_metadata.get("vtepIp", ""),
                            "vtep_b": dev_b.platform_metadata.get("vtepIp", ""),
                        },
                    ))

        # 7. Endpoint tracking — MAC/ARP table from NDFC endpoint database
        # GET /appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/switches/
        #     {serialNumber}/mac-address-table
        # Falls back to per-fabric endpoint locator if switch-level API fails.
        await self._discover_endpoints(data, fabrics, devices, device_node_map)

        log.info("ndfc.discover.done", instance=self.instance_id,
                 nodes=len(data.nodes), edges=len(data.edges))
        return data

    async def _discover_endpoints(
        self,
        data: GraphData,
        fabrics: list[dict],
        devices: list,
        device_node_map: dict[str, str],
    ) -> None:
        """Fetch MAC+ARP endpoint data from NDFC and add MAC/ARP graph nodes."""
        # Try the per-fabric endpoint locator (works on NDFC 12.x)
        for fabric in fabrics:
            fabric_name = fabric.get("fabricName") or fabric.get("name", "")
            if not fabric_name:
                continue
            try:
                ep_data = await self._get(
                    f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control/fabrics"
                    f"/{fabric_name}/inventory/endpoints",
                    timeout=60.0,
                )
                endpoints = ep_data if isinstance(ep_data, list) else ep_data.get("DATA", [])
                await self._ingest_ndfc_endpoints(data, endpoints, device_node_map, fabric_name)
                log.info("ndfc.endpoints.done", fabric=fabric_name,
                         count=len(endpoints), instance=self.instance_id)
            except AdapterError as exc:
                # Fall back to per-switch MAC table
                log.debug("ndfc.endpoints.fabric_api_missing",
                          fabric=fabric_name, error=str(exc))
                await self._discover_switch_mac_tables(data, devices, device_node_map)
                break
            except Exception as exc:
                log.warning("ndfc.endpoints.failed", fabric=fabric_name, error=str(exc))

    async def _discover_switch_mac_tables(
        self,
        data: GraphData,
        devices: list,
        device_node_map: dict[str, str],
    ) -> None:
        """Per-switch MAC address table fallback (NDFC 12.x allotted)."""
        import asyncio
        sem = asyncio.Semaphore(5)

        async def _fetch_one(dev) -> list[dict]:
            async with sem:
                try:
                    result = await self._get(
                        f"/appcenter/cisco/ndfc/api/v1/lan-fabric/rest/control"
                        f"/switches/{dev.platform_id}/mac-address-table",
                        timeout=30.0,
                    )
                    rows = result if isinstance(result, list) else result.get("DATA", [])
                    return [(dev.platform_id, rows)]
                except Exception:
                    return []

        results = await asyncio.gather(*[_fetch_one(d) for d in devices])
        endpoints: list[dict] = []
        for batch in results:
            for switch_serial, rows in batch:
                for row in rows:
                    mac_raw = row.get("macAddress", "")
                    if mac_raw:
                        endpoints.append({
                            "macAddress": mac_raw,
                            "ipAddress": None,
                            "switchSerial": switch_serial,
                            "ifName": row.get("interface", ""),
                            "vlanId": row.get("vlan"),
                        })
        await self._ingest_ndfc_endpoints(data, endpoints, device_node_map, fabric_name="")

    async def _ingest_ndfc_endpoints(
        self,
        data: GraphData,
        endpoints: list[dict],
        device_node_map: dict[str, str],
        fabric_name: str,
    ) -> None:
        """Convert NDFC endpoint records into MAC/ARP graph nodes."""
        from netcortex.adapters.meraki import _norm_mac  # shared normaliser

        existing_mac_ids = {n.id for n in data.nodes if n.type == NodeType.MAC_ADDRESS}
        existing_arp_ids = {n.id for n in data.nodes if n.type == NodeType.ARP_ENTRY}

        for ep in endpoints:
            mac_raw = (
                ep.get("macAddress") or ep.get("mac") or ""
            )
            mac = _norm_mac(mac_raw)
            if not mac:
                continue

            ip = ep.get("ipAddress") or ep.get("ip") or ""
            # Some NDFC responses embed IP as a list
            if isinstance(ip, list):
                ip = ip[0] if ip else ""
            ip = str(ip).strip() if ip else ""

            vlan = ep.get("vlanId") or ep.get("vlan")
            switch_serial = ep.get("switchSerial") or ep.get("learnedSwitchSerial", "")
            iface_name = ep.get("ifName") or ep.get("interface", "")

            # MAC node
            mac_node_id = f"mac:{mac}"
            if mac_node_id not in existing_mac_ids:
                existing_mac_ids.add(mac_node_id)
                mac_props: dict = {
                    "mac": mac,
                    "vlan": vlan,
                    "fabric": fabric_name,
                    "source": self.instance_id,
                }
                if ip:
                    mac_props["ip"] = ip
                data.nodes.append(GraphNode(
                    id=mac_node_id,
                    type=NodeType.MAC_ADDRESS,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=self.instance_id,
                    properties=mac_props,
                ))

            # LEARNED_MAC edge from switch interface
            dev_node = device_node_map.get(switch_serial, "")
            if dev_node and iface_name:
                iface_node_id = f"ndfc-if:{switch_serial}:{iface_name}"
                if not any(n.id == iface_node_id for n in data.nodes):
                    data.nodes.append(GraphNode(
                        id=iface_node_id,
                        type=NodeType.INTERFACE,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties={
                            "name": iface_name,
                            "device_id": switch_serial,
                            "fabric": fabric_name,
                        },
                    ))
                    data.edges.append(GraphEdge(
                        source_id=dev_node,
                        target_id=iface_node_id,
                        type=EdgeType.HAS_INTERFACE,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                    ))
                data.edges.append(GraphEdge(
                    source_id=iface_node_id,
                    target_id=mac_node_id,
                    type=EdgeType.LEARNED_MAC,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                    properties={"vlan": vlan, "fabric": fabric_name},
                ))

            # ARP node when we have an IP
            if ip:
                arp_node_id = f"arp:{ip}"
                if arp_node_id not in existing_arp_ids:
                    existing_arp_ids.add(arp_node_id)
                    data.nodes.append(GraphNode(
                        id=arp_node_id,
                        type=NodeType.ARP_ENTRY,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties={
                            "ip": ip,
                            "mac": mac,
                            "vlan": vlan,
                            "fabric": fabric_name,
                            "source": self.instance_id,
                        },
                    ))
                data.edges.append(GraphEdge(
                    source_id=mac_node_id,
                    target_id=arp_node_id,
                    type=EdgeType.HAS_ARP,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))
