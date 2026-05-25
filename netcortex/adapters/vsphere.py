"""VMware vSphere adapter — discovers VMs, hosts, and clusters via vCenter REST API.

Secrets (stored at ``netcortex/adapters/vsphere/<instance_name>``):
    vcenter_url   : https://<vcenter-fqdn>
    username      : administrator@vsphere.local
    password      : <password>
    verify_ssl    : true (optional, default true)

Graph output:
    PlatformSite nodes : datacenter (top level)
    Device nodes       : hosts (PHYSICAL), VMs (VIRTUAL)
    Edges              : LOCATED_AT (host→datacenter), HAS_VM (host→VM)
"""

from __future__ import annotations

import re
import structlog
import httpx

from netcortex.adapters.base import PlatformAdapter
from netcortex.graph.models import (
    Dimension,
    EdgeType,
    GraphData,
    GraphEdge,
    GraphNode,
    NodeType,
)

log = structlog.get_logger()

# vCenter REST API paths
_SESSION_PATH = "/api/session"
_DC_PATH       = "/api/vcenter/datacenter"
_CLUSTER_PATH  = "/api/vcenter/cluster"
_HOST_PATH     = "/api/vcenter/host"
_VM_PATH       = "/api/vcenter/vm"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class VSphereAdapter(PlatformAdapter):
    name = "vsphere"

    def __init__(self, config: dict, instance_name: str) -> None:
        super().__init__(config, instance_name)
        self.vcenter_url: str = config.get("vcenter_url", "").rstrip("/")
        self.username: str    = config.get("username", "")
        self.password: str    = config.get("password", "")
        self.verify_ssl: bool = str(config.get("verify_ssl", "true")).lower() not in ("false", "0", "no")
        self._session_token: str | None = None

    # ── Authentication ────────────────────────────────────────────────────────

    async def authenticate(self) -> None:
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            resp = await client.post(
                f"{self.vcenter_url}{_SESSION_PATH}",
                auth=(self.username, self.password),
            )
            resp.raise_for_status()
            self._session_token = resp.json()
        log.debug("vsphere.authenticated", instance=self.instance_id)

    def _auth_headers(self) -> dict[str, str]:
        return {"vmware-api-session-id": self._session_token or ""}

    # ── REST helpers ─────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict | None = None) -> list | dict:
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            resp = await client.get(
                f"{self.vcenter_url}{path}",
                headers=self._auth_headers(),
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def discover(self) -> GraphData:
        data = GraphData(adapter_id=self.instance_id)
        await self.authenticate()

        # 1. Datacenters → PlatformSite (top-level containers)
        dc_map: dict[str, str] = {}  # dc_id → node_id
        try:
            dcs = await self._get(_DC_PATH)
        except Exception as exc:
            log.warning("vsphere.discover.dc_failed", error=str(exc), instance=self.instance_id)
            dcs = []

        for dc in dcs:
            dc_id   = dc.get("datacenter", dc.get("id", ""))
            dc_name = dc.get("name", dc_id)
            node_id = f"vsphere-dc:{self.instance_name}:{dc_id}"
            dc_map[dc_id] = node_id
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL, Dimension.VIRTUAL],
                source_adapter=self.instance_id,
                properties={
                    "name": dc_name,
                    "slug": _slug(dc_name),
                    "platform": "vsphere",
                    "normalized_name": dc_name.lower(),
                    "vsphere_type": "datacenter",
                },
            ))

        # 2. Clusters → PlatformSite (inside datacenter)
        cluster_map: dict[str, str] = {}  # cluster_id → node_id
        try:
            clusters = await self._get(_CLUSTER_PATH)
        except Exception as exc:
            log.warning("vsphere.discover.cluster_failed", error=str(exc), instance=self.instance_id)
            clusters = []

        for cl in clusters:
            cl_id   = cl.get("cluster", cl.get("id", ""))
            cl_name = cl.get("name", cl_id)
            node_id = f"vsphere-cluster:{self.instance_name}:{cl_id}"
            cluster_map[cl_id] = node_id
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL, Dimension.VIRTUAL],
                source_adapter=self.instance_id,
                properties={
                    "name": cl_name,
                    "slug": _slug(cl_name),
                    "platform": "vsphere",
                    "normalized_name": cl_name.lower(),
                    "vsphere_type": "cluster",
                },
            ))

        # 3. Hosts → Device (physical servers)
        host_map: dict[str, str]      = {}  # host_id → node_id
        host_container: dict[str, str] = {}  # host_id → container node_id
        try:
            hosts = await self._get(_HOST_PATH)
        except Exception as exc:
            log.warning("vsphere.discover.host_failed", error=str(exc), instance=self.instance_id)
            hosts = []

        for h in hosts:
            h_id      = h.get("host", h.get("id", ""))
            h_name    = h.get("name", h_id)
            node_id   = f"vsphere-host:{self.instance_name}:{h_id}"
            host_map[h_id] = node_id

            # Determine container: cluster if present, else datacenter
            container_id: str | None = None
            if h.get("cluster"):
                container_id = cluster_map.get(h["cluster"])
            if not container_id and h.get("datacenter"):
                container_id = dc_map.get(h["datacenter"])
            if not container_id:
                # Use (or create) a fallback standalone container
                fb_id = f"vsphere-standalone:{self.instance_name}"
                if fb_id not in {n.id for n in data.nodes}:
                    data.nodes.append(GraphNode(
                        id=fb_id,
                        type=NodeType.PLATFORM_SITE,
                        dimensions=[Dimension.PHYSICAL, Dimension.VIRTUAL],
                        source_adapter=self.instance_id,
                        properties={
                            "name": f"vSphere {self.instance_name} (standalone)",
                            "slug": f"vsphere-standalone-{_slug(self.instance_name)}",
                            "platform": "vsphere",
                        },
                    ))
                container_id = fb_id
            host_container[h_id] = container_id

            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": h_name,
                    "platform": "vsphere",
                    "platform_id": h_id,
                    "role": "server",
                    "power_state": h.get("power_state", ""),
                    "connection_state": h.get("connection_state", ""),
                },
            ))
            data.edges.append(GraphEdge(
                source_id=node_id,
                target_id=container_id,
                type=EdgeType.LOCATED_AT,
                dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
            ))

        # 4. VMs → Device (virtual)
        try:
            vms = await self._get(_VM_PATH)
        except Exception as exc:
            log.warning("vsphere.discover.vm_failed", error=str(exc), instance=self.instance_id)
            vms = []

        for vm in vms:
            vm_id      = vm.get("vm", vm.get("id", ""))
            vm_name    = vm.get("name", vm_id)
            vm_node_id = f"vsphere-vm:{self.instance_name}:{vm_id}"
            host_id    = vm.get("host", "")
            host_node  = host_map.get(host_id, "")

            # VM container follows its host's container
            container_id = host_container.get(host_id, "")

            data.nodes.append(GraphNode(
                id=vm_node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.VIRTUAL],
                source_adapter=self.instance_id,
                properties={
                    "name": vm_name,
                    "platform": "vsphere",
                    "platform_id": vm_id,
                    "role": "vm",
                    "power_state": vm.get("power_state", ""),
                    "memory_size_mib": vm.get("memory", {}).get("size_MiB"),
                    "cpu_count": vm.get("cpu", {}).get("count"),
                },
            ))
            if container_id:
                data.edges.append(GraphEdge(
                    source_id=vm_node_id,
                    target_id=container_id,
                    type=EdgeType.LOCATED_AT,
                    dimension=Dimension.VIRTUAL,
                    source_adapter=self.instance_id,
                ))
            if host_node:
                data.edges.append(GraphEdge(
                    source_id=host_node,
                    target_id=vm_node_id,
                    type=EdgeType.HAS_VM,
                    dimension=Dimension.VIRTUAL,
                    source_adapter=self.instance_id,
                    properties={"host_name": hosts[0].get("name", "") if hosts else ""},
                ))

        log.info(
            "vsphere.discover.done",
            instance=self.instance_id,
            nodes=len(data.nodes),
            edges=len(data.edges),
        )
        return data
