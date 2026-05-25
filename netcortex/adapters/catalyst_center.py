"""Cisco Catalyst Center (formerly DNA Center) adapter.

API reference: https://developer.cisco.com/docs/dna-center/
Tested against Catalyst Center 2.3.x / 2.3.7.x
"""

from __future__ import annotations

import asyncio
import base64
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

_MAC_RE = re.compile(r"[^0-9a-fA-F]")
_ARP_LINE_RE = re.compile(
    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # IP
    r"\s+\S+"                                    # Age
    r"\s+((?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4})"  # MAC (cisco format)
    r"\s+\S+"                                    # Type
    r"\s+(\S+)"                                  # Interface
)


def _parse_show_ip_arp(text: str, device_uuid: str) -> list[dict]:
    """Parse 'show ip arp' CLI output into structured records."""
    results: list[dict] = []
    for line in text.splitlines():
        m = _ARP_LINE_RE.search(line)
        if not m:
            continue
        ip, mac_cisco, iface = m.group(1), m.group(2), m.group(3)
        # Convert cisco XXXX.XXXX.XXXX to colon-separated
        digits = mac_cisco.replace(".", "")
        if len(digits) == 12:
            mac = ":".join(digits[i:i+2] for i in range(0, 12, 2)).lower()
            results.append({"ip": ip, "mac": mac, "interface": iface, "device_uuid": device_uuid})
    return results


def _norm_site_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

def _norm_mac(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _MAC_RE.sub("", raw)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i+2] for i in range(0, 12, 2)).lower()


ROLE_MAP = {
    "Switches and Hubs": "switch",
    "Routers": "router",
    "Wireless Controller": "wireless-controller",
    "Access Points": "access-point",
    "Unified AP": "access-point",
    "Meraki": "other",
}


class CatalystCenterAdapter(PlatformAdapter):
    name = "catalyst_center"
    display_name = "Cisco Catalyst Center"
    profile = PlatformProfile(
        device_id_field="uuid",
        role_map=ROLE_MAP,
        native_topology=True,
        provides_oper_status=True,
        default_access_methods=["netconf", "ssh"],
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

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise RuntimeError("Not authenticated — call authenticate() first")
        return {"X-Auth-Token": self._token, "Content-Type": "application/json"}

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify_ssl,
            follow_redirects=True,
            timeout=timeout,
        )

    async def authenticate(self) -> None:
        """Obtain an X-Auth-Token via Basic auth."""
        creds = base64.b64encode(
            f"{self._username}:{self._password}".encode()
        ).decode()
        async with self._client(timeout=15) as client:
            resp = await client.post(
                f"{self._base_url}/dna/system/api/v1/auth/token",
                headers={"Authorization": f"Basic {creds}"},
            )
            if resp.status_code == 401:
                raise AuthError("Catalyst Center authentication failed — check username/password")
            if not resp.is_success:
                raise AdapterError(f"Catalyst Center auth error: HTTP {resp.status_code} — {resp.text[:200]}")
            self._token = resp.json().get("Token")
            if not self._token:
                raise AuthError("Catalyst Center returned no Token in auth response")
        log.debug("catc.authenticated", instance=self.instance_id)

    async def _get(self, path: str, params: dict | None = None, timeout: float = 30.0) -> Any:
        """Authenticated GET; re-authenticates once on 401."""
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
                raise AdapterError(f"CATC GET {path} failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return resp.json()

    async def list_devices(self) -> list[NormalizedDevice]:
        """Return all network devices from Catalyst Center."""
        devices: list[NormalizedDevice] = []
        offset = 1
        limit = 500
        while True:
            data = await self._get(
                "/dna/intent/api/v1/network-device",
                params={"offset": offset, "limit": limit},
            )
            items = data.get("response", [])
            for d in items:
                role = ROLE_MAP.get(d.get("family", ""), "other")
                devices.append(NormalizedDevice(
                    name=d.get("hostname") or d.get("managementIpAddress", d["id"]),
                    platform=self.name,
                    platform_id=d["id"],
                    role=role,
                    serial=d.get("serialNumber"),
                    mgmt_ip=d.get("managementIpAddress"),
                    status=d.get("reachabilityStatus", "active").lower() or "active",
                    platform_metadata={
                        "family": d.get("family"),
                        "series": d.get("series"),
                        "platformId": d.get("platformId"),
                        "softwareVersion": d.get("softwareVersion"),
                        "os_version": d.get("softwareVersion") or "",
                        "reachabilityStatus": d.get("reachabilityStatus"),
                        "status": d.get("reachabilityStatus") or "unknown",
                        "siteId": d.get("siteId"),
                        "siteName": d.get("siteName"),
                    },
                ))
            if len(items) < limit:
                break
            offset += limit
        return devices

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return interfaces for a device by UUID."""
        data = await self._get(
            f"/dna/intent/api/v1/interface/network-device/{device_id}",
        )
        interfaces = []
        for iface in data.get("response", []):
            interfaces.append(NormalizedInterface(
                name=iface.get("portName", iface.get("id", "")),
                device_platform_id=device_id,
                description=iface.get("description", ""),
                enabled=iface.get("adminStatus", "UP") == "UP",
                platform_id=iface.get("id", ""),
                platform_metadata={
                    "speed": iface.get("speed"),
                    "status": iface.get("status"),
                    "macAddress": iface.get("macAddress"),
                    "ipv4Address": iface.get("ipv4Address"),
                    "ipv4Mask": iface.get("ipv4Mask"),
                    "mediaType": iface.get("mediaType"),
                    "interfaceType": iface.get("interfaceType"),
                },
            ))
        return interfaces

    async def list_vlans(self) -> list[NormalizedVLAN]:
        """VLANs are not directly available via CATC top-level API."""
        return []

    async def get_device_vlans(self, device_id: str) -> list[dict]:
        """Return VLANs configured on a device.

        GET /dna/intent/api/v1/network-device/{deviceId}/vlan
        Returns a list of VLAN objects with vlanNumber, interfaceName, ipAddress,
        networkAddress, prefix, description.
        """
        try:
            data = await self._get(f"/dna/intent/api/v1/network-device/{device_id}/vlan")
            return data.get("response", [])
        except Exception as exc:
            log.debug("catc.get_device_vlans_failed", device_id=device_id, error=str(exc))
            return []

    async def get_physical_topology(self) -> dict:
        """Return the physical topology graph from Catalyst Center."""
        data = await self._get("/dna/intent/api/v1/topology/physical-topology")
        return data.get("response", {})

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        """Return CDP/LLDP neighbours for one device via the topology/detail API."""
        return []

    async def get_topology_detail(self) -> dict:
        """Try the v2 topology endpoint which may have more complete link data."""
        for path in (
            "/dna/intent/api/v2/topology/physical-topology",
            "/dna/intent/api/v1/topology/physical-topology?nodeType=device",
        ):
            try:
                resp = await self._get(path)
                result = resp.get("response", {})
                if result.get("links"):
                    return result
            except Exception:
                continue
        return {}

    async def get_host_table(self) -> list[dict]:
        """Return the CATC host/client table (wired + wireless endpoints).

        Tries multiple endpoints in order:
          1. /v1/host          (older stable endpoint, broad support)
          2. /v1/client/detail (CATC 2.3+ detail endpoint)

        Each returned record is normalised to have:
          hostMac, hostIp, connectedNetworkDeviceId, connectedInterfaceName,
          vlanId, hostType.
        """
        # 1. Legacy /v1/host — present in CATC 2.x+, returns hostMac + hostIp list
        try:
            data = await self._get("/dna/intent/api/v1/host", params={"limit": 500})
            raw = data.get("response", []) if isinstance(data, dict) else []
            normalised: list[dict] = []
            for h in raw:
                mac = h.get("hostMac", "")
                # hostIp may be a list or a single string
                ip_field = h.get("hostIp", "")
                if isinstance(ip_field, list):
                    ip = ip_field[0] if ip_field else ""
                else:
                    ip = ip_field or ""
                normalised.append({
                    "hostMac": mac,
                    "hostIp": ip,
                    "connectedNetworkDeviceId": h.get("connectedNetworkDeviceId", ""),
                    "connectedInterfaceName": h.get("connectedInterfaceName", ""),
                    "vlanId": h.get("vlanId"),
                    "hostType": h.get("hostType", ""),
                })
            if normalised:
                log.info("catc.host_table.via_v1_host",
                         count=len(normalised), instance=self.instance_id)
                return normalised
        except Exception as exc:
            log.debug("catc.host_table.v1_host_failed", error=str(exc))

        # 2. Newer CATC 2.3+ client detail endpoint
        try:
            data = await self._get("/dna/intent/api/v1/client/detail", params={"limit": 500})
            items = data.get("response", [])
            if items:
                return items
        except Exception:
            pass

        return []

    async def get_device_mac_table(self, device_id: str) -> list[dict]:
        """Return MAC address table entries for a specific device.

        GET /dna/intent/api/v1/network-device/{deviceId}/mac-address-table
        Returns entries with macAddress, interfaceNumber, vlanId.
        """
        try:
            data = await self._get(
                f"/dna/intent/api/v1/network-device/{device_id}/mac-address-table"
            )
            return data.get("response", [])
        except Exception as exc:
            log.debug("catc.get_mac_table_failed", device_id=device_id, error=str(exc))
            return []

    async def get_sites(self) -> list[dict]:
        """Return all sites from Catalyst Center hierarchy."""
        try:
            data = await self._get("/dna/intent/api/v1/site")
            return data.get("response", [])
        except Exception:
            return []

    async def get_vrf_list(self) -> list[dict]:
        """Return VRFs known to Catalyst Center."""
        try:
            data = await self._get("/dna/intent/api/v1/network-device/vrf")
            return data.get("response", [])
        except Exception:
            return []

    async def get_arp_via_command_runner(self, device_uuids: list[str]) -> list[dict]:
        """Run 'show ip arp' on L3 devices via CATC Command Runner.

        Returns a flat list of dicts with keys: ip, mac, interface, device_uuid.
        Falls back to empty list on any error (Command Runner requires specific licence).
        """
        if not device_uuids:
            return []
        try:
            # POST to schedule the command run
            resp = await self._post(
                "/dna/intent/api/v1/network-device-poller/cli/read-request",
                json={
                    "name": "netcortex_arp",
                    "commands": ["show ip arp"],
                    "deviceUuids": device_uuids[:20],  # cap at 20 to avoid long waits
                },
            )
            task_id = (resp.get("response") or {}).get("taskId") or resp.get("taskId", "")
            if not task_id:
                return []

            # Poll for task completion (max ~30s)
            import asyncio
            for _ in range(15):
                await asyncio.sleep(2)
                task = await self._get(f"/dna/intent/api/v1/task/{task_id}")
                prog = (task.get("response") or task).get("progress", "")
                if "fileId" in prog or "completed" in prog.lower():
                    break
            else:
                return []

            # Fetch the file result
            file_id = prog if "fileId" not in prog else ""
            # Try to parse as JSON progress dict
            try:
                import json
                prog_data = json.loads(prog) if prog.startswith("{") else {}
                file_id = prog_data.get("fileId", "")
            except Exception:
                file_id = ""

            if not file_id:
                return []

            output = await self._get(f"/dna/intent/api/v1/file/{file_id}")
            entries: list[dict] = []
            for device_result in (output if isinstance(output, list) else [output]):
                uuid = device_result.get("deviceUuid", "")
                cmd_outputs = device_result.get("commandResponses", {}).get("SUCCESS", {})
                arp_text = cmd_outputs.get("show ip arp", "")
                entries.extend(_parse_show_ip_arp(arp_text, uuid))
            return entries
        except Exception as exc:
            log.debug("catc.arp.command_runner_failed", error=str(exc))
            return []

    async def _post(self, path: str, json: dict | None = None) -> dict:
        """Authenticated POST helper."""
        data = await self._get.__func__(  # type: ignore[attr-defined]
            self, path  # POST via httpx
        ) if False else None  # placeholder

        async with self._client() as client:
            headers = {
                "X-Auth-Token": self._token or "",
                "Content-Type": "application/json",
            }
            resp = await client.post(
                f"{self._base_url}{path}",
                headers=headers,
                json=json or {},
            )
            if not resp.is_success:
                raise AdapterError(f"CATC POST {path} failed: HTTP {resp.status_code}")
            return resp.json()

    async def get_stp_topology(self) -> dict:
        """Return spanning-tree topology from Catalyst Center.

        The response includes per-VLAN STP info with root bridge and port roles.
        API: GET /dna/intent/api/v1/topology/stp
        """
        try:
            data = await self._get("/dna/intent/api/v1/topology/stp")
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log.debug("catc.stp_topology_failed", error=str(exc))
            return {}

    # ── Graph discovery ───────────────────────────────────────────────────────

    async def discover(self) -> GraphData:
        """Build graph from Catalyst Center devices, interfaces, and topology."""
        data = GraphData(adapter_id=self.instance_id)
        await self.authenticate()

        # 1. Sites → Site nodes
        try:
            sites = await self.get_sites()
        except Exception as exc:
            log.warning("catc.discover.sites_failed", error=str(exc))
            sites = []

        site_map: dict[str, str] = {}
        for site in sites:
            site_id = site.get("id", "")
            node_id = f"catc-site:{site_id}"
            site_map[site_id] = node_id
            # additionalInfo is a list that may be absent or empty
            additional_info = site.get("additionalInfo") or []
            site_type = ""
            if additional_info:
                site_type = additional_info[0].get("attributes", {}).get("type", "")
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": site.get("name", site_id),
                    "slug": site.get("name", site_id).lower().replace(" ", "-"),
                    "site_type": site_type,
                    "platform": "catalyst_center",
                    "normalized_name": _norm_site_name(site.get("name", site_id)),
                },
            ))

        # 2. Devices → Device nodes
        try:
            devices = await self.list_devices()
        except Exception as exc:
            log.warning("catc.discover.devices_failed", error=str(exc))
            devices = []

        device_node_map: dict[str, str] = {}
        for dev in devices:
            node_id = f"catc:{dev.platform_id}"
            device_node_map[dev.platform_id] = node_id
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": dev.name,
                    "platform": dev.platform,
                    "platform_id": dev.platform_id,
                    "role": dev.role,
                    "serial": dev.serial or "",
                    # Only include mgmt_ip when truthy — writing an empty
                    # string would clobber a previously-good value from
                    # another adapter via `SET n += row` (0.5.0-dev1).
                    **({"mgmt_ip": dev.mgmt_ip} if dev.mgmt_ip else {}),
                    **{k: v for k, v in dev.platform_metadata.items() if v is not None},
                },
            ))
            site_id = dev.platform_metadata.get("siteId", "")
            if site_id and site_id in site_map:
                container_id = site_map[site_id]
            else:
                # Device has no site assignment — place in a catch-all container
                container_id = f"catc-site:unassigned:{self.instance_name}"
                if container_id not in site_map.values():
                    site_map["__unassigned__"] = container_id
                    data.nodes.append(GraphNode(
                        id=container_id,
                        type=NodeType.PLATFORM_SITE,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties={
                            "name": f"CATC {self.instance_name} (unassigned)",
                            "slug": f"catc-unassigned-{self.instance_name}",
                            "platform": "catalyst_center",
                            "normalized_name": f"catc {self.instance_name} unassigned",
                        },
                    ))
            data.edges.append(GraphEdge(
                source_id=node_id,
                target_id=container_id,
                type=EdgeType.LOCATED_AT,
                dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
            ))

        # 3. Interfaces → Interface nodes + HAS_INTERFACE edges
        for dev in devices:
            try:
                ifaces = await self.list_interfaces(dev.platform_id)
            except Exception as exc:
                log.debug("catc.discover.iface_failed", device=dev.name, error=str(exc))
                continue

            dev_node_id = device_node_map[dev.platform_id]
            for iface in ifaces:
                iface_node_id = f"catc-if:{dev.platform_id}:{iface.platform_id}"
                props = {
                    "name": iface.name,
                    "description": iface.description,
                    "enabled": iface.enabled,
                    "device_id": dev.platform_id,
                }
                if iface.platform_metadata:
                    props.update({k: v for k, v in iface.platform_metadata.items() if v})
                data.nodes.append(GraphNode(
                    id=iface_node_id,
                    type=NodeType.INTERFACE,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=self.instance_id,
                    properties=props,
                ))
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=iface_node_id,
                    type=EdgeType.HAS_INTERFACE,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))
                # Interface MAC → OWNS_MAC edge (feeds correlation engine)
                iface_mac = iface.platform_metadata.get("macAddress") if iface.platform_metadata else None
                iface_mac = _norm_mac(iface_mac)
                if iface_mac:
                    mac_node_id = f"mac:{iface_mac}"
                    if mac_node_id not in {n.id for n in data.nodes}:
                        data.nodes.append(GraphNode(
                            id=mac_node_id,
                            type=NodeType.MAC_ADDRESS,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={"mac": iface_mac, "source": self.instance_id},
                        ))
                    data.edges.append(GraphEdge(
                        source_id=dev_node_id,
                        target_id=mac_node_id,
                        type=EdgeType.OWNS_MAC,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                        properties={"interface": iface.name},
                    ))

                # IP address node
                ip = iface.platform_metadata.get("ipv4Address") if iface.platform_metadata else None
                if ip:
                    mask = iface.platform_metadata.get("ipv4Mask", "")
                    ip_node_id = f"catc-ip:{ip}"
                    data.nodes.append(GraphNode(
                        id=ip_node_id,
                        type=NodeType.IP_ADDRESS,
                        dimensions=[Dimension.LOGICAL],
                        source_adapter=self.instance_id,
                        properties={"address": ip, "mask": mask, "prefix": f"{ip}/{mask}"},
                    ))
                    data.edges.append(GraphEdge(
                        source_id=iface_node_id,
                        target_id=ip_node_id,
                        type=EdgeType.ASSIGNED_IP,
                        dimension=Dimension.LOGICAL,
                        source_adapter=self.instance_id,
                    ))

        # 3b. VLANs per device → VLAN nodes + LOGICAL_MEMBER edges.
        #
        # NOTE: Catalyst Center has no concept of a "fabric" for traditional
        # (non-SDA) deployments, so we cannot rely on the platform itself
        # to scope a VLAN.  Previous versions emitted a single global node
        # `vlan:<vid>` per VLAN id; that collapsed VLAN 1 across every site
        # CatC managed (Fulton, Ashburn, Nashville) into one cross-site
        # node — wildly misleading when no L2 extension technology is in
        # use.
        #
        # We now emit one VLAN node per (device, vid) pair using the
        # device's CatC platform_id as the scope key.  The downstream
        # `_canonicalize_vlans_per_fabric` correlator pass groups these
        # per-device VLANs by the owning device's NetBox site slug and
        # produces a single canonical `vlan:nb:<slug>:<vid>` per
        # (NetBox-site, vid) pair — so VLAN 1 in cpn-ash and VLAN 1 in
        # cpn-ful become two distinct nodes, exactly as the topology
        # demands.
        vlan_node_set: set[str] = set()
        for dev in devices:
            dev_node_id = device_node_map[dev.platform_id]
            try:
                vlans = await self.get_device_vlans(dev.platform_id)
            except Exception as exc:
                log.debug("catc.discover.vlan_failed", device=dev.name, error=str(exc))
                vlans = []
            for vl in vlans:
                vid = vl.get("vlanNumber") or vl.get("interfaceVlan", "")
                if not vid:
                    continue
                try:
                    vid = int(vid)
                except (TypeError, ValueError):
                    continue
                if vid < 1 or vid > 4094:
                    continue
                # Per-device scope — canonicalization will fold these
                # together per NetBox site.  We don't dedupe across
                # devices because each (device, vid) pair carries its
                # own LOGICAL_MEMBER edge.
                vlan_node_id = f"vlan:catc:{dev.platform_id}:{vid}"
                if vlan_node_id not in vlan_node_set:
                    vlan_node_set.add(vlan_node_id)
                    data.nodes.append(GraphNode(
                        id=vlan_node_id,
                        type=NodeType.VLAN,
                        dimensions=[Dimension.LOGICAL, Dimension.VIRTUAL],
                        source_adapter=self.instance_id,
                        properties={
                            "name": f"VLAN {vid}",
                            "vid": vid,
                            "vlan_id": vid,
                            "description": vl.get("description", ""),
                            # Stamp the owning device so the correlator
                            # can attach this node to the right
                            # PlatformSite + NetBox-site canonical.
                            "owning_device_platform_id": dev.platform_id,
                        },
                    ))
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=vlan_node_id,
                    type=EdgeType.LOGICAL_MEMBER,
                    dimension=Dimension.LOGICAL,
                    source_adapter=self.instance_id,
                    properties={
                        "interface": vl.get("interfaceName", ""),
                        "ip": vl.get("ipAddress", ""),
                        "network": vl.get("networkAddress", ""),
                    },
                ))

        # 4. Physical topology → PHYSICAL_LINK edges (CDP/LLDP via CATC)
        # Try v1 first; if it returns no links, fall back to v2.
        try:
            topo = await self.get_physical_topology()
            if not topo.get("links"):
                topo = await self.get_topology_detail()
            raw_nodes = topo.get("nodes", [])
            raw_links = topo.get("links", [])
            log.debug("catc.topology_raw",
                      total_nodes=len(raw_nodes),
                      total_links=len(raw_links),
                      instance=self.instance_id)

            # Build a map of all topology node IDs → graph node IDs.
            # The CATC topology uses multiple ID types; try each in order:
            #   n["id"]          – the device UUID (matches /network-device response)
            #   n["deviceId"]    – alternate field name on some CATC versions
            #   n["ip"]          – management IP (last resort)
            ip_to_dev_id: dict[str, str] = {
                d.mgmt_ip: d.platform_id for d in devices if d.mgmt_ip
            }
            topo_nodes: dict[str, str] = {}
            for n in raw_nodes:
                for key in ("id", "deviceId"):
                    val = n.get(key, "")
                    if val and val in device_node_map:
                        topo_nodes[val] = device_node_map[val]
                        break
                else:
                    # Fallback: match by management IP
                    ip = n.get("ip", "") or n.get("managementIp", "")
                    dev_id = ip_to_dev_id.get(ip, "")
                    if dev_id and dev_id in device_node_map:
                        nid = n.get("id", "") or n.get("deviceId", "")
                        if nid:
                            topo_nodes[nid] = device_node_map[dev_id]

            log.debug("catc.topology_mapped",
                      mapped_nodes=len(topo_nodes),
                      instance=self.instance_id)

            seen_links: set[tuple[str, str]] = set()
            for link in raw_links:
                # Field names vary across CATC versions
                src_key = link.get("source") or link.get("startDeviceId", "")
                dst_key = link.get("target") or link.get("endDeviceId", "")
                src = topo_nodes.get(src_key, "")
                dst = topo_nodes.get(dst_key, "")
                if src and dst and src != dst:
                    pair = (min(src, dst), max(src, dst))
                    if pair in seen_links:
                        continue
                    seen_links.add(pair)
                    data.edges.append(GraphEdge(
                        source_id=src,
                        target_id=dst,
                        type=EdgeType.PHYSICAL_LINK,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                        properties={
                            "interface_a": normalize_ifname(
                                link.get("startPortName") or link.get("startPortID", "")),
                            "interface_b": normalize_ifname(
                                link.get("endPortName") or link.get("endPortID", "")),
                            "interface_a_raw": link.get("startPortName") or link.get("startPortID", ""),
                            "interface_b_raw": link.get("endPortName") or link.get("endPortID", ""),
                            "link_status": link.get("linkStatus", ""),
                            "discovery_proto": "catc_topology",
                        },
                    ))
            log.debug("catc.topology_links", physical_links=len(seen_links), instance=self.instance_id)
        except Exception as exc:
            log.warning("catc.discover.topology_failed", error=str(exc))

        # 5. Host table → MACAddress + ARPEntry nodes + LEARNED_MAC / HAS_ARP edges
        try:
            hosts = await self.get_host_table()
            for host in hosts:
                raw_mac = host.get("hostMac", "")
                mac = _norm_mac(raw_mac)
                if not mac:
                    continue
                host_ip: str = (host.get("hostIp") or "").strip()

                mac_node_id = f"mac:{mac}"
                if not any(n.id == mac_node_id for n in data.nodes):
                    mac_props: dict = {
                        "mac": mac,
                        "vlan": host.get("vlanId"),
                        "host_type": host.get("hostType", ""),
                        "hostname": host.get("hostName", ""),
                        "source": self.instance_id,
                    }
                    if host_ip:
                        mac_props["ip"] = host_ip
                    data.nodes.append(GraphNode(
                        id=mac_node_id,
                        type=NodeType.MAC_ADDRESS,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties=mac_props,
                    ))

                # Create ARP entry when IP is known
                if host_ip:
                    arp_node_id = f"arp:{host_ip}"
                    if arp_node_id not in {n.id for n in data.nodes}:
                        data.nodes.append(GraphNode(
                            id=arp_node_id,
                            type=NodeType.ARP_ENTRY,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={
                                "ip": host_ip,
                                "mac": mac,
                                "vlan": host.get("vlanId"),
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

                # Link MAC to the interface it was learned on
                connected_dev_id = host.get("connectedNetworkDeviceId", "")
                connected_iface_name = host.get("connectedInterfaceName", "")
                if connected_dev_id and connected_iface_name:
                    dev_node_id = device_node_map.get(connected_dev_id, "")
                    if dev_node_id:
                        # Find matching interface node or create one
                        iface_node_id = f"catc-if:{connected_dev_id}:{connected_iface_name}"
                        if not any(n.id == iface_node_id for n in data.nodes):
                            data.nodes.append(GraphNode(
                                id=iface_node_id,
                                type=NodeType.INTERFACE,
                                dimensions=[Dimension.PHYSICAL],
                                source_adapter=self.instance_id,
                                properties={
                                    "name": connected_iface_name,
                                    "device_id": connected_dev_id,
                                },
                            ))
                            data.edges.append(GraphEdge(
                                source_id=dev_node_id,
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
                            properties={"vlan": host.get("vlanId")},
                        ))
        except Exception as exc:
            log.warning("catc.discover.hosts_failed", error=str(exc))

        # 5b. Per-switch MAC address table → Interface→LEARNED_MAC→MACAddress edges
        #
        # Section 5 above relies on /v1/host (or /v1/client/detail) returning
        # connectedNetworkDeviceId + connectedInterfaceName for every endpoint.
        # On many CATC deployments those endpoints come back empty (the
        # assurance pipeline is not populated, or the endpoints are disabled
        # by RBAC) leaving the graph with MACs but no port linkage.
        #
        # As a fallback we walk every switch's mac-address-table directly
        # via /dna/intent/api/v1/network-device/{deviceId}/mac-address-table
        # so port→MAC binding still gets stitched even when the assurance
        # data is missing.  Endpoint availability varies by CATC version so
        # we treat the whole walk as best-effort and log.debug on failure.
        #
        # We tie the MAC to an Interface node keyed by
        # ``catc-if:{device_uuid}:{interface_name}`` — matching the same
        # convention used by sections 3, 5, and 6 so the per-device
        # Interface node is shared across all enrichment paths and the
        # ``LEARNED_MAC`` edge collapses onto the canonical Interface.
        try:
            switch_devs = [dev for dev in devices if dev.role == "switch"]
            sem = asyncio.Semaphore(8)

            async def _walk_mac_table(dev: NormalizedDevice) -> None:
                async with sem:
                    try:
                        entries = await self.get_device_mac_table(dev.platform_id)
                    except Exception as exc:
                        log.debug("catc.discover.mac_table_failed",
                                  device=dev.name, error=str(exc))
                        return
                    if not entries:
                        return
                    dev_gid = device_node_map.get(dev.platform_id, "")
                    if not dev_gid:
                        return
                    for entry in entries:
                        mac = _norm_mac(entry.get("macAddress")
                                        or entry.get("mac"))
                        if not mac:
                            continue
                        # Schema varies: interfaceNumber on most CATC 2.x,
                        # ifName/portName/interface on others.  Try them in
                        # order and skip the row when no port is exposed.
                        iface_name = (entry.get("interfaceNumber")
                                      or entry.get("ifName")
                                      or entry.get("portName")
                                      or entry.get("interface")
                                      or "")
                        if not iface_name:
                            continue
                        vlan_id = entry.get("vlanId") or entry.get("vlan")

                        mac_node_id = f"mac:{mac}"
                        if not any(n.id == mac_node_id for n in data.nodes):
                            data.nodes.append(GraphNode(
                                id=mac_node_id,
                                type=NodeType.MAC_ADDRESS,
                                dimensions=[Dimension.PHYSICAL],
                                source_adapter=self.instance_id,
                                properties={
                                    "mac": mac,
                                    "vlan": vlan_id,
                                    "source": self.instance_id,
                                },
                            ))
                        iface_node_id = f"catc-if:{dev.platform_id}:{iface_name}"
                        if not any(n.id == iface_node_id for n in data.nodes):
                            data.nodes.append(GraphNode(
                                id=iface_node_id,
                                type=NodeType.INTERFACE,
                                dimensions=[Dimension.PHYSICAL],
                                source_adapter=self.instance_id,
                                properties={
                                    "name": iface_name,
                                    "device_id": dev.platform_id,
                                },
                            ))
                            data.edges.append(GraphEdge(
                                source_id=dev_gid,
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
                            properties={"vlan": vlan_id,
                                        "source": "mac_table"},
                        ))

            if switch_devs:
                await asyncio.gather(
                    *[_walk_mac_table(dev) for dev in switch_devs],
                    return_exceptions=True,
                )
                log.info("catc.discover.mac_table_done",
                         switches=len(switch_devs),
                         instance=self.instance_id)
        except Exception as exc:
            log.warning("catc.discover.mac_table_walk_failed",
                        error=str(exc))

        # 6. ARP table via clientDetail → ARPEntry nodes (enables MAC/IP stitching).
        # CATC /v1/host is deprecated on newer versions; /v1/client/detail provides
        # richer connected endpoint data including MAC, IP, and connected interface.
        try:
            client_details = await self._get(
                "/dna/intent/api/v1/client/detail",
                params={"limit": 500},
            )
            clients = client_details.get("response", [])
            for client in clients:
                raw_mac = client.get("macAddress", "") or client.get("hostMac", "")
                mac = _norm_mac(raw_mac)
                if not mac:
                    continue
                ip = (client.get("ipAddress") or client.get("hostIp", "")).strip()
                if ip and mac:
                    arp_node_id = f"arp:{ip}"
                    if arp_node_id not in {n.id for n in data.nodes}:
                        data.nodes.append(GraphNode(
                            id=arp_node_id,
                            type=NodeType.ARP_ENTRY,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={"ip": ip, "mac": mac, "source": self.instance_id},
                        ))
                    mac_node_id = f"mac:{mac}"
                    if mac_node_id not in {n.id for n in data.nodes}:
                        data.nodes.append(GraphNode(
                            id=mac_node_id,
                            type=NodeType.MAC_ADDRESS,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={"mac": mac, "ip": ip, "source": self.instance_id},
                        ))
                    data.edges.append(GraphEdge(
                        source_id=arp_node_id,
                        target_id=mac_node_id,
                        type=EdgeType.HAS_ARP,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                    ))
                    # Link to the switch port where this endpoint was learned
                    dev_id = client.get("connectedNetworkDeviceId", "")
                    iface_name = client.get("connectedInterfaceName", "")
                    if dev_id and iface_name:
                        dev_gid = device_node_map.get(dev_id, "")
                        if dev_gid:
                            iface_node_id = f"catc-if:{dev_id}:{iface_name}"
                            if iface_node_id not in {n.id for n in data.nodes}:
                                data.nodes.append(GraphNode(
                                    id=iface_node_id,
                                    type=NodeType.INTERFACE,
                                    dimensions=[Dimension.PHYSICAL],
                                    source_adapter=self.instance_id,
                                    properties={"name": iface_name, "device_id": dev_id},
                                ))
                                data.edges.append(GraphEdge(
                                    source_id=dev_gid,
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
                                properties={"vlan": client.get("vlanId")},
                            ))
        except Exception as exc:
            log.debug("catc.discover.client_detail_failed", error=str(exc))

        # 6b. ARP via Command Runner — runs 'show ip arp' on L3 devices when
        #     the host-table REST endpoints are unavailable on this CATC version.
        l3_uuids = [
            dev.platform_id for dev in devices
            if dev.role in ("router", "switch", "border_router")
        ]
        arp_entries = await self.get_arp_via_command_runner(l3_uuids)
        existing_arp_ids: set[str] = {n.id for n in data.nodes if n.type == NodeType.ARP_ENTRY}
        existing_mac_ids: set[str] = {n.id for n in data.nodes if n.type == NodeType.MAC_ADDRESS}
        for entry in arp_entries:
            ip = entry.get("ip", "")
            mac = entry.get("mac", "")
            iface_name = entry.get("interface", "")
            uuid = entry.get("device_uuid", "")
            if not ip or not mac:
                continue
            mac_node_id = f"mac:{mac}"
            if mac_node_id not in existing_mac_ids:
                existing_mac_ids.add(mac_node_id)
                data.nodes.append(GraphNode(
                    id=mac_node_id, type=NodeType.MAC_ADDRESS,
                    dimensions=[Dimension.PHYSICAL], source_adapter=self.instance_id,
                    properties={"mac": mac, "ip": ip, "source": self.instance_id},
                ))
            arp_node_id = f"arp:{ip}"
            if arp_node_id not in existing_arp_ids:
                existing_arp_ids.add(arp_node_id)
                data.nodes.append(GraphNode(
                    id=arp_node_id, type=NodeType.ARP_ENTRY,
                    dimensions=[Dimension.PHYSICAL], source_adapter=self.instance_id,
                    properties={"ip": ip, "mac": mac, "interface": iface_name,
                                "source": self.instance_id},
                ))
            data.edges.append(GraphEdge(
                source_id=mac_node_id, target_id=arp_node_id,
                type=EdgeType.HAS_ARP, dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
            ))
        if arp_entries:
            log.info("catc.arp.command_runner_done",
                     count=len(arp_entries), instance=self.instance_id)

        # 7. STP topology — build STPDomain nodes per VLAN and link devices
        try:
            stp_raw = await self.get_stp_topology()
            # CATC STP response structure varies by version.  We handle both the
            # flat {"response": {"id": ..., "nodes": [...], "links": [...]}} shape
            # and the {"response": {"vlanInfoList": [...]}} shape.
            resp = stp_raw.get("response", stp_raw)

            # Normalise: collect a list of (vlan_id, root_node_id, members[])
            stp_instances: list[tuple[str, str, list[str]]] = []

            vlan_list = resp.get("vlanInfoList") if isinstance(resp, dict) else None
            if vlan_list:
                # vlanInfoList shape
                for vlan_info in vlan_list:
                    vid = str(vlan_info.get("vlanId", ""))
                    root_id = vlan_info.get("rootId", "")
                    members = [
                        d.get("id", "") or d.get("deviceId", "")
                        for d in (vlan_info.get("devices") or [])
                        if d.get("id") or d.get("deviceId")
                    ]
                    if vid and members:
                        stp_instances.append((vid, root_id, members))
            else:
                # Flat nodes/links shape: infer STP domains from the root-bridge role
                nodes_by_id = {
                    n.get("id", ""): n
                    for n in (resp.get("nodes") or [])
                    if n.get("id")
                }
                # Group links by vlan (label field often contains VLAN)
                vlan_members: dict[str, set[str]] = {}
                vlan_roots: dict[str, str] = {}
                for link in (resp.get("links") or []):
                    # Some CATC versions embed vlan in link label
                    vid = str(link.get("linkStatus", link.get("id", "stp-0")))
                    src = link.get("source", "")
                    tgt = link.get("target", "")
                    if src:
                        vlan_members.setdefault(vid, set()).add(src)
                    if tgt:
                        vlan_members.setdefault(vid, set()).add(tgt)
                # Find root bridges (nodes with role == "ROOT" or similar)
                for node in (resp.get("nodes") or []):
                    role = (node.get("role") or "").upper()
                    if "ROOT" in role:
                        vid = str(node.get("vlanId", "0"))
                        vlan_roots[vid] = node.get("id", "")
                for vid, members in vlan_members.items():
                    stp_instances.append((vid, vlan_roots.get(vid, ""), list(members)))

            stp_nodes_added: set[str] = set()
            for vid, root_catc_id, member_catc_ids in stp_instances:
                domain_id = f"stp:{self.instance_id}:vlan{vid}"
                if domain_id not in stp_nodes_added:
                    data.nodes.append(GraphNode(
                        id=domain_id,
                        type=NodeType.STP_DOMAIN,
                        dimensions=[Dimension.STP],
                        source_adapter=self.instance_id,
                        properties={
                            "name": f"STP VLAN {vid}",
                            "vlan_id": int(vid) if vid.isdigit() else vid,
                            "source": self.instance_id,
                        },
                    ))
                    stp_nodes_added.add(domain_id)

                for catc_id in member_catc_ids:
                    gid = device_node_map.get(catc_id, "")
                    if not gid:
                        continue
                    is_root = (catc_id == root_catc_id)
                    data.edges.append(GraphEdge(
                        source_id=gid,
                        target_id=domain_id,
                        type=EdgeType.STP_ROOT if is_root else EdgeType.STP_MEMBER,
                        dimension=Dimension.STP,
                        source_adapter=self.instance_id,
                        properties={"vlan_id": vid, "is_root": is_root},
                    ))

            log.debug("catc.discover.stp_done",
                      instances=len(stp_instances), stp_nodes=len(stp_nodes_added))
        except Exception as exc:
            log.debug("catc.discover.stp_failed", error=str(exc))

        log.info("catc.discover.done", instance=self.instance_id,
                 nodes=len(data.nodes), edges=len(data.edges))
        return data
