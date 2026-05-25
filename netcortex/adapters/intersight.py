"""Cisco Intersight adapter.

Authentication
--------------
Intersight uses per-request HTTP Signature authentication — there is no
session token.  Every request must carry:

  Authorization: Signature keyId="<key_id>", algorithm="rsa-sha256"|"ecdsa-sha256",
                 headers="(request-target) date host [digest] [content-type]",
                 signature="<base64>"
  Date:   <RFC 7231>
  Host:   intersight.com
  Digest: SHA-256=<base64(sha256(body))>   # only for POST/PUT/PATCH

Both RSA-2048 and ECDSA-256 API key types are supported; the algorithm is
detected from the PEM header at init time.

API reference: https://intersight.com/apidocs/apirefs/
Tested against Intersight SaaS (cloud) and on-prem appliance.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
from typing import Any
from urllib.parse import urlparse

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

_PAGE_SIZE = 500  # Intersight allows up to 1000; 500 is safe
_DEFAULT_URL = "https://intersight.com"

ROLE_MAP = {
    "blade": "server",
    "rack-unit": "server",
    "rack": "server",
    "fi": "switch",          # Fabric Interconnect
    "chassis": "other",
    "hyperflex": "server",
}


# ---------------------------------------------------------------------------
# HTTP Signature helpers
# ---------------------------------------------------------------------------

def _load_private_key(pem: str):
    """Load an RSA or ECDSA private key from a PEM string."""
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _detect_algorithm(private_key) -> tuple[str, str]:
    """Return (header_algorithm, signing_algorithm) based on the loaded key type.

    Intersight uses 'rsa-sha256' for RSA-2048 keys and 'hs2019' for ECDSA-256
    keys in the Authorization header, regardless of the actual signing algorithm.
    PKCS#8-wrapped ECDSA keys use 'BEGIN PRIVATE KEY' (not 'BEGIN EC PRIVATE KEY').
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "hs2019", "ecdsa-sha256"
    return "rsa-sha256", "rsa-sha256"


def _sign(private_key, signing_alg: str, data: bytes) -> bytes:
    """Sign *data* with the loaded private key using the given signing algorithm."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, ec

    if signing_alg == "ecdsa-sha256":
        return private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    else:
        return private_key.sign(data, asym_padding.PKCS1v15(), hashes.SHA256())


_USERAGENT = "netcortex-intersight-adapter/1.0"

def _build_signed_headers(
    key_id: str,
    private_key: Any,
    header_alg: str,
    signing_alg: str,
    method: str,
    url: str,
    body: bytes = b"",
) -> dict[str, str]:
    """Return the full set of headers needed for an Intersight API request.

    Intersight requires:
    - Digest on ALL requests (SHA-256 of body; empty body → SHA-256 of "")
    - Content-Type: application/json always
    - x-starship-useragent in signing string
    - For hs2019 (ECDSA) keys: (created) pseudo-header with Unix timestamp

    header_alg  is placed in the Authorization Signature header ('rsa-sha256' or 'hs2019').
    signing_alg is the actual crypto used ('rsa-sha256' or 'ecdsa-sha256').
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    created = int(now.timestamp())
    digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
    method_lc = method.lower()

    use_hs2019 = header_alg == "hs2019"

    if use_hs2019:
        signed_headers = "(request-target) (created) date host content-type x-starship-useragent digest"
        signing_string = (
            f"(request-target): {method_lc} {path}\n"
            f"(created): {created}\n"
            f"date: {date_str}\n"
            f"host: {host}\n"
            f"content-type: application/json\n"
            f"x-starship-useragent: {_USERAGENT}\n"
            f"digest: {digest}"
        )
    else:
        signed_headers = "(request-target) date host content-type x-starship-useragent digest"
        signing_string = (
            f"(request-target): {method_lc} {path}\n"
            f"date: {date_str}\n"
            f"host: {host}\n"
            f"content-type: application/json\n"
            f"x-starship-useragent: {_USERAGENT}\n"
            f"digest: {digest}"
        )

    sig_bytes = _sign(private_key, signing_alg, signing_string.encode("utf-8"))
    sig_b64 = base64.b64encode(sig_bytes).decode()

    if use_hs2019:
        auth = (
            f'Signature keyId="{key_id}", '
            f'algorithm="{header_alg}", '
            f'created={created}, '
            f'headers="{signed_headers}", '
            f'signature="{sig_b64}"'
        )
    else:
        auth = (
            f'Signature keyId="{key_id}", '
            f'algorithm="{header_alg}", '
            f'headers="{signed_headers}", '
            f'signature="{sig_b64}"'
        )

    return {
        "Authorization": auth,
        "Date": date_str,
        "Host": host,
        "Digest": digest,
        "Content-Type": "application/json",
        "x-starship-useragent": _USERAGENT,
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Port-name helpers (module-level so they're trivially unit-testable)
# ---------------------------------------------------------------------------

def _fi_port_name(slot: Any, port: Any) -> str:
    """Format an FI-side ``ether/PhysicalPort`` as ``Ethernet<slot>/<port>``.

    Both ``slot`` and ``port`` are Intersight integers (or strings that
    coerce to integers).  When the firmware omits either field we
    default to ``1/0`` so the cable still has a usable label.
    A doubly-missing identifier yields the empty string so callers can
    distinguish "no port info" from a fabricated value.
    """
    if slot is None and port is None:
        return ""
    s = slot if slot not in (None, "") else "1"
    p = port if port not in (None, "") else "0"
    return f"Ethernet{s}/{p}"


def _host_port_name(slot: Any, port: Any) -> str:
    """Format a server-side ``ether/HostPort`` as ``vic<slot>/<port>``.

    Same coercion / default behaviour as :func:`_fi_port_name`.  The
    ``vic`` prefix matches the convention UCSM uses for VIC-card host
    ports so the rendered cable labels are familiar to UCS operators.
    """
    if slot is None and port is None:
        return ""
    s = slot if slot not in (None, "") else "1"
    p = port if port not in (None, "") else "0"
    return f"vic{s}/{p}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class IntersightAdapter(PlatformAdapter):
    name = "intersight"
    display_name = "Cisco Intersight"
    profile = PlatformProfile(
        device_id_field="moid",
        role_map=ROLE_MAP,
        native_topology=True,
        provides_oper_status=True,
        default_access_methods=["ssh"],
        netbox_platform_slug="ucs",
        supported_dimensions=["physical", "logical", "fabric"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._base_url: str = config.get("base_url", _DEFAULT_URL).rstrip("/")
        self._key_id: str = config["key_id"]
        secret_pem: str = config["secret_key"]
        # Normalise escaped newlines that come from JSON/AWS SM storage
        if "\\n" in secret_pem and "\n" not in secret_pem:
            secret_pem = secret_pem.replace("\\n", "\n")
        self._private_key = _load_private_key(secret_pem)
        # header_alg goes in Authorization header; signing_alg is used for the actual crypto
        self._header_alg, self._signing_alg = _detect_algorithm(self._private_key)

    def _signed_headers(self, method: str, url: str, body: bytes = b"") -> dict[str, str]:
        return _build_signed_headers(
            self._key_id, self._private_key,
            self._header_alg, self._signing_alg,
            method, url, body,
        )

    async def authenticate(self) -> None:
        """Verify credentials by querying the API keys list (accessible to any valid key)."""
        url = f"{self._base_url}/api/v1/iam/ApiKeys?$top=1&$select=KeyId"
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers=self._signed_headers("GET", url))
        if resp.status_code == 401:
            try:
                detail = resp.json().get("message", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            log.debug("intersight.auth.401", detail=detail, key_id=self._key_id[:20])
            raise AuthError(f"Intersight 401 — {detail}")
        if not resp.is_success:
            raise AdapterError(f"Intersight auth probe failed: HTTP {resp.status_code} — {resp.text[:200]}")

    async def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from an Intersight collection endpoint.

        We build the full URL string ourselves (no httpx params=) so that the
        request-target in the signing string exactly matches what is sent on the wire.
        Intersight's signature verification is strict about this.
        """
        base_params = dict(params or {})
        base_params.setdefault("$top", _PAGE_SIZE)
        results: list[dict] = []
        skip = 0

        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            while True:
                all_params = {**base_params, "$skip": skip}
                # Build query string manually — keeps $ unencoded, matching the signing string
                qs = "&".join(f"{k}={v}" for k, v in all_params.items())
                full_url = f"{self._base_url}{path}?{qs}"
                resp = await client.get(
                    full_url,
                    headers=self._signed_headers("GET", full_url),
                )
                if not resp.is_success:
                    raise AdapterError(
                        f"Intersight GET {path} failed: HTTP {resp.status_code} — {resp.text[:200]}"
                    )
                body = resp.json()
                page = body.get("Results") or []
                results.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                skip += _PAGE_SIZE

        return results

    async def list_devices(self) -> list[NormalizedDevice]:
        """Return all compute nodes (blades + rack units) via PhysicalSummaries."""
        items = await self._get_all(
            "/api/v1/compute/PhysicalSummaries",
            params={"$select": "Moid,Name,Model,Serial,ManagementIp,OperState,"
                               "NumCpus,NumCpuCores,AvailableMemory,ServiceProfile,"
                               "SourceObjectType,RegisteredDevice"},
        )
        devices: list[NormalizedDevice] = []
        for d in items:
            src_type = d.get("SourceObjectType", "").lower()
            role = "server"
            if "blade" in src_type:
                role = "server"
            elif "rack" in src_type:
                role = "server"
            devices.append(NormalizedDevice(
                name=d.get("Name") or d.get("Serial", d["Moid"]),
                platform=self.name,
                platform_id=d["Moid"],
                role=role,
                serial=d.get("Serial"),
                mgmt_ip=d.get("ManagementIp"),
                status=(d.get("OperState") or "unknown").lower(),
                platform_metadata={
                    "model": d.get("Model"),
                    "oper_state": d.get("OperState"),
                    "status": (d.get("OperState") or "unknown").lower(),
                    "os_version": "",  # firmware version requires separate API call
                    "num_cpus": d.get("NumCpus"),
                    "num_cpu_cores": d.get("NumCpuCores"),
                    "memory_gb": round(d.get("AvailableMemory", 0) / 1024, 1) if d.get("AvailableMemory") else None,
                    "source_type": src_type,
                    "service_profile_moid": (d.get("ServiceProfile") or {}).get("Moid"),
                    "device_moid": (d.get("RegisteredDevice") or {}).get("Moid"),
                },
            ))
        return devices

    async def list_fabric_interconnects(self) -> list[dict]:
        """Return Fabric Interconnects (network/Elements).

        Field selection notes
        ---------------------
        We pull a few "identity" fields beyond what's strictly needed
        for FI node creation so the correlation engine can merge LLDP
        / CDP stubs onto the canonical FI node using observed state
        only (no NetBox round-trip required):

          * ``OutOfBandMac`` — the FI's out-of-band management
            interface MAC.  When present it lets
            ``_merge_neighbor_stubs_by_chassis_mac`` resolve any LLDP
            stub whose ``lldpRemChassisId`` (subtype 4 = MAC) equals
            this value, regardless of what hostname the FI advertises.
          * ``Dn`` / ``Name`` — extra string identifiers that some
            FI firmware reports as ``lldpRemSysName`` (e.g. ``A``,
            ``B``, ``UCS-FI-A``) so we can publish them as
            ``candidate_names`` for stub-by-name merging.
          * ``OutOfBandIpAddress`` / ``OutOfBandIpv4Address`` —
            extra IPs that may show up in ``lldpRemManAddr`` even
            when they differ from ``ManagementIpAddress``.
        Intersight ignores ``$select`` fields it doesn't know, so it
        is safe to ask for fields that some firmware versions don't
        populate.
        """
        return await self._get_all(
            "/api/v1/network/Elements",
            params={"$select": "Moid,Model,Serial,SwitchId,Name,Dn,"
                               "ManagementIpAddress,OutOfBandIpAddress,"
                               "OutOfBandIpv4Address,OutOfBandMac,"
                               "OperState,RegisteredDevice,NetworkFcZoning"},
        )

    async def list_chassis(self) -> list[dict]:
        """Return UCS chassis (equipment/Chassis) including the Blades reference list.

        The Blades field gives us the blade Moid list so we can map each blade
        back to its chassis (and therefore its UCS domain / FI pair) without
        needing a separate per-blade API call.
        """
        return await self._get_all(
            "/api/v1/equipment/Chasses",
            params={"$select": "Moid,Model,Serial,ChassisId,OperState,RegisteredDevice,Blades"},
        )

    async def list_blades(self) -> list[dict]:
        """Return blade servers (compute/Blades) for chassis-slot correlation."""
        try:
            return await self._get_all(
                "/api/v1/compute/Blades",
                params={"$select": "Moid,Name,Model,Serial,ChassisId,SlotId,RegisteredDevice"},
            )
        except Exception as exc:
            log.debug("intersight.blades_failed", error=str(exc))
            return []

    async def list_server_nodes(self) -> list[NormalizedDevice]:
        """Return X-Series server nodes (compute/ServerNodes) — modular servers in UCSX chassis."""
        try:
            items = await self._get_all(
                "/api/v1/compute/ServerNodes",
                params={"$select": "Moid,Name,Model,Serial,ManagementIp,OperState,RegisteredDevice"},
            )
            servers = []
            for d in items:
                servers.append(NormalizedDevice(
                    name=d.get("Name") or d.get("Serial", d["Moid"]),
                    platform=self.name,
                    platform_id=d["Moid"],
                    role="server",
                    serial=d.get("Serial"),
                    mgmt_ip=d.get("ManagementIp"),
                    platform_metadata={
                        "model": d.get("Model"),
                        "oper_state": d.get("OperState"),
                        "source_type": "compute.servernode",
                        "device_moid": (d.get("RegisteredDevice") or {}).get("Moid"),
                    },
                ))
            return servers
        except Exception as exc:
            log.debug("intersight.server_nodes_failed", error=str(exc))
            return []

    async def list_hyperflex_clusters(self) -> list[dict]:
        """Return HyperFlex clusters."""
        try:
            return await self._get_all(
                "/api/v1/hyperflex/Clusters",
                params={"$select": "Moid,Name,ClusterType,HxVersion,NumNodes,ManagementIpAddress"},
            )
        except Exception as exc:
            log.debug("intersight.hx_clusters_failed", error=str(exc))
            return []

    async def list_server_profiles(self) -> list[dict]:
        """Return Server Profiles (server/Profiles)."""
        try:
            return await self._get_all(
                "/api/v1/server/Profiles",
                params={"$select": "Moid,Name,AssignedServer,ConfigContext"},
            )
        except Exception as exc:
            log.debug("intersight.profiles_failed", error=str(exc))
            return []

    async def list_adapters(self) -> list[dict]:
        """Return adapter/NIC cards for all compute nodes."""
        try:
            return await self._get_all(
                "/api/v1/adapter/Units",
                params={"$select": "Moid,Model,Serial,ComputeNode,Pid"},
            )
        except Exception as exc:
            log.debug("intersight.adapters_failed", error=str(exc))
            return []

    async def list_host_eth_ports(self) -> list[dict]:
        """Return host-side Ethernet ports (server uplinks to FI) from ``ether/HostPorts``.

        Each entry's ``AcknowledgedPeerInterface`` is a MoRef pointing
        at an ``ether/PhysicalPort`` on the FI side that the server
        adapter port is cabled to.  Combined with
        :meth:`list_physical_ports` this lets us emit per-port
        ``PHYSICAL_LINK`` edges from the server to the right FI even
        for standalone CIMC-managed servers that don't belong to a
        UCS Domain.

        We also request ``EquipmentBaseEnclosure`` / ``AdapterUnit``
        so we can walk the host-port → adapter → compute-node chain
        when ``RegisteredDevice`` alone is ambiguous (e.g. multiple
        servers under the same CIMC device connector).
        """
        try:
            return await self._get_all(
                "/api/v1/ether/HostPorts",
                params={"$select": "Moid,PortId,SlotId,Speed,OperState,"
                                   "AcknowledgedPeerInterface,"
                                   "EquipmentBaseEnclosure,AdapterUnit,"
                                   "RegisteredDevice"},
            )
        except Exception as exc:
            log.debug("intersight.host_eth_ports_failed", error=str(exc))
            return []

    async def list_physical_ports(self) -> list[dict]:
        """Return FI-side switch ports from ``ether/PhysicalPorts``.

        Each ``ether/PhysicalPort`` is the FI-end of a cable.  We resolve
        it back to the owning FI using the combination
        ``(RegisteredDevice.Moid, SwitchId)`` — Intersight does not
        expose a direct ``NetworkElement`` MoRef on ``PhysicalPort``,
        but every FI in a UCS domain shares the same
        ``RegisteredDevice`` Moid and is uniquely identified within
        that domain by its ``SwitchId`` ("A" / "B").

        ``Role`` tells us what the port is for (``Server``,
        ``Uplink PC Member``, ``unknown``, ...) so callers can filter
        to just server-facing ports when emitting host cables.
        """
        try:
            return await self._get_all(
                "/api/v1/ether/PhysicalPorts",
                params={"$select": "Moid,PortId,SlotId,SwitchId,Dn,"
                                   "OperState,OperSpeed,Role,"
                                   "RegisteredDevice"},
            )
        except Exception as exc:
            log.debug("intersight.physical_ports_failed", error=str(exc))
            return []

    async def list_ext_eth_interfaces(self) -> list[dict]:
        """Return server-side adapter ports from ``adapter/ExtEthInterfaces``.

        These are the NIC ports on each server's adapter card (Cisco VIC,
        UCS-M-V5Q50GV2, etc.).  Each entry's ``AcknowledgedPeerInterface``
        is a MoRef pointing at the ``ether/PhysicalPort`` on the FI that
        this server NIC port is cabled to.  Combined with
        :meth:`list_physical_ports` and :meth:`list_adapters` (which
        provides the ``AdapterUnit`` Moid → ``ComputeNode`` Moid lookup)
        this yields a port-accurate server↔FI ``PHYSICAL_LINK`` edge
        that works for **both** X-Series blades (via IOM) and
        standalone-CIMC C-series direct-attach servers (e.g. the
        Nutanix nodes).

        ``HostPort`` ports on the FI/IOM are the chassis-facing
        endpoints that match ``AcknowledgedPeerInterface`` of
        ``ExtEthInterface`` for blades; for direct-attach C-series
        the peer is always a ``PhysicalPort``.  We keep the chain
        uniform by always pivoting through the server-side
        ``ExtEthInterface``.
        """
        try:
            return await self._get_all(
                "/api/v1/adapter/ExtEthInterfaces",
                params={"$select": "Moid,PortId,SlotId,SwitchId,Dn,"
                                   "MacAddress,AdapterUnit,"
                                   "AcknowledgedPeerInterface,"
                                   "RegisteredDevice"},
            )
        except Exception as exc:
            log.debug("intersight.ext_eth_interfaces_failed", error=str(exc))
            return []

    async def list_host_eth_interfaces(self) -> list[dict]:
        """Return server NIC host Ethernet interfaces with MAC addresses.

        adapter/HostEthInterfaces gives per-vNIC MACs and the compute node they
        belong to, enabling server→FI PHYSICAL_LINK creation and MAC correlation.
        """
        try:
            return await self._get_all(
                "/api/v1/adapter/HostEthInterfaces",
                params={"$select": "Moid,Name,MacAddress,InterfaceType,OperState,"
                                   "PinGroupName,VethAction,"
                                   "AdapterUnit,RegisteredDevice"},
            )
        except Exception as exc:
            log.debug("intersight.host_eth_ifaces_failed", error=str(exc))
            return []

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return adapter units for a specific compute node Moid."""
        items = await self._get_all(
            "/api/v1/adapter/Units",
            params={
                "$filter": f"ComputeNode.Moid eq '{device_id}'",
                "$select": "Moid,Model,Pid,ComputeNode",
            },
        )
        return [
            NormalizedInterface(
                name=item.get("Pid") or item.get("Model") or item["Moid"],
                device_platform_id=device_id,
                platform_id=item["Moid"],
                platform_metadata={"model": item.get("Model"), "pid": item.get("Pid")},
            )
            for item in items
        ]

    async def list_vlans(self) -> list[NormalizedVLAN]:
        return []

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        return []

    # ── Graph discovery ───────────────────────────────────────────────────────

    async def discover(self) -> GraphData:
        """Build a graph from Intersight compute, FI, chassis, and HX cluster data."""
        data = GraphData(adapter_id=self.instance_id)

        # ── 1. Fetch all data in parallel ────────────────────────────────────
        import asyncio
        import re as _re

        _mac_re = _re.compile(r"[^0-9a-fA-F]")

        def _norm_mac(raw: str | None) -> str | None:
            if not raw:
                return None
            digits = _mac_re.sub("", raw)
            if len(digits) != 12:
                return None
            return ":".join(digits[i:i+2] for i in range(0, 12, 2)).lower()

        (
            devices,
            fis,
            chassis_list,
            hx_clusters,
            server_profiles,
            adapters,
            host_eth_ifaces,
            blades_raw,
            server_nodes,
            physical_ports,
            ext_eth_ifaces,
        ) = await asyncio.gather(
            self.list_devices(),
            self.list_fabric_interconnects(),
            self.list_chassis(),
            self.list_hyperflex_clusters(),
            self.list_server_profiles(),
            self.list_adapters(),
            self.list_host_eth_interfaces(),
            self.list_blades(),
            self.list_server_nodes(),
            self.list_physical_ports(),
            self.list_ext_eth_interfaces(),
            return_exceptions=True,
        )

        def _safe(result, label: str) -> list:
            if isinstance(result, Exception):
                log.warning(f"intersight.discover.{label}_failed", error=str(result))
                return []
            return result or []

        devices = _safe(devices, "devices")
        fis = _safe(fis, "fis")
        chassis_list = _safe(chassis_list, "chassis")
        hx_clusters = _safe(hx_clusters, "hx_clusters")
        server_profiles = _safe(server_profiles, "server_profiles")
        adapters = _safe(adapters, "adapters")
        host_eth_ifaces = _safe(host_eth_ifaces, "host_eth_ifaces")
        blades_raw = _safe(blades_raw, "blades")
        server_nodes = _safe(server_nodes, "server_nodes")
        physical_ports = _safe(physical_ports, "physical_ports")
        ext_eth_ifaces = _safe(ext_eth_ifaces, "ext_eth_ifaces")

        # Merge X-Series ServerNodes into the devices list (dedup by platform_id)
        existing_moids = {d.platform_id for d in devices}
        for sn in server_nodes:
            if sn.platform_id not in existing_moids:
                devices.append(sn)
                existing_moids.add(sn.platform_id)

        # Adapter → compute Moid map. Built once here so both the
        # per-port server→FI link emission (step 5b) and the HAS_INTERFACE
        # / OWNS_MAC passes (step 6/7) can share it.
        adapter_to_compute: dict[str, str] = {}
        for adp in adapters:
            cm = (adp.get("ComputeNode") or {}).get("Moid", "")
            if cm:
                adapter_to_compute[adp["Moid"]] = cm

        # Standalone-CIMC / direct-attach fallback: ``adapter.Unit.ComputeNode``
        # is often empty in Intersight tenants that don't manage these
        # servers under a UCS Domain.  In that case the only stable
        # bridge from an ``adapter.ExtEthInterface`` back to its
        # owning server is via the shared ``RegisteredDevice`` Moid
        # (the CIMC device-connector that registers the server in
        # Intersight).  Build a translation map from each device's
        # registration Moid to its compute Moid so the per-port
        # planner can resolve a server even when ``adapter_to_compute``
        # is empty.
        reg_to_compute: dict[str, str] = {}
        for d in devices:
            reg = d.platform_metadata.get("device_moid", "") if d.platform_metadata else ""
            if reg and d.platform_id:
                reg_to_compute[reg] = d.platform_id

        # ── 2. UCS Domain nodes (group FIs by RegisteredDevice Moid) ─────────
        domain_map: dict[str, str] = {}   # registered_device_moid → domain node id
        fi_node_map: dict[str, str] = {}  # FI Moid → node id

        fi_by_domain: dict[str, list[dict]] = {}
        for fi in fis:
            reg = (fi.get("RegisteredDevice") or {}).get("Moid", "")
            fi_by_domain.setdefault(reg, []).append(fi)

        for reg_moid, fi_group in fi_by_domain.items():
            domain_node_id = f"intersight-domain:{self.instance_name}:{reg_moid}"
            domain_map[reg_moid] = domain_node_id
            # Name the domain after its FIs (e.g. "FI-6454-A / FI-6454-B")
            fi_names = sorted(f.get("SwitchId", "?") for f in fi_group)
            data.nodes.append(GraphNode(
                id=domain_node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL, Dimension.FABRIC],
                source_adapter=self.instance_id,
                properties={
                    "name": f"UCS-Domain-{reg_moid[-8:]}",
                    "slug": f"ucs-domain-{reg_moid[-8:]}",
                    "fi_switch_ids": fi_names,
                    "registered_device_moid": reg_moid,
                },
            ))

            for fi in fi_group:
                fi_node_id = f"intersight-fi:{self.instance_name}:{fi['Moid']}"
                fi_node_map[fi["Moid"]] = fi_node_id

                # ── Observable identity for stub-merge correlation ───
                # Publish every IP and hostname Intersight has for this
                # FI so the correlator can resolve LLDP/CDP stubs from
                # neighbors (e.g. cpn-ful-n9k1) onto the canonical FI
                # node without going through NetBox.  Each list is
                # de-duplicated and order-preserved (first entry wins
                # for ``mgmt_ip`` selection).
                ip_candidates: list[str] = []
                for ip in (
                    fi.get("ManagementIpAddress"),
                    fi.get("OutOfBandIpAddress"),
                    fi.get("OutOfBandIpv4Address"),
                ):
                    if ip and ip not in ip_candidates:
                        ip_candidates.append(ip)

                # ``lldpRemSysName`` could match any of these strings
                # depending on FI firmware: bare ``A``/``B`` (SwitchId),
                # the DN, or the friendly Intersight ``Name``.
                name_candidates: list[str] = []
                for n in (
                    fi.get("Name"),
                    fi.get("Dn"),
                    fi.get("SwitchId"),
                ):
                    n = (n or "").strip()
                    if n and n not in name_candidates:
                        name_candidates.append(n)

                fi_props: dict[str, Any] = {
                    "name": f"FI-{fi.get('SwitchId', '?')}-{fi.get('Serial', '')}",
                    "platform": self.name,
                    "platform_id": fi["Moid"],
                    "role": "switch",
                    "serial": fi.get("Serial", ""),
                    "model": fi.get("Model", ""),
                    "switch_id": fi.get("SwitchId", ""),
                    "oper_state": fi.get("OperState", ""),
                }
                # Same `SET n += row` clobber guard as the main
                # device emit path — only include mgmt_ip / lists
                # when the Intersight payload actually populated them.
                if ip_candidates:
                    fi_props["mgmt_ip"] = ip_candidates[0]
                    fi_props["candidate_ips"] = ip_candidates
                if name_candidates:
                    fi_props["candidate_names"] = name_candidates

                data.nodes.append(GraphNode(
                    id=fi_node_id,
                    type=NodeType.DEVICE,
                    dimensions=[Dimension.PHYSICAL, Dimension.FABRIC],
                    source_adapter=self.instance_id,
                    properties=fi_props,
                ))
                # FI → domain
                data.edges.append(GraphEdge(
                    source_id=fi_node_id,
                    target_id=domain_node_id,
                    type=EdgeType.LOCATED_AT,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))

                # FI chassis MAC — only emit if Intersight actually
                # populated ``OutOfBandMac``.  The chassis-MAC stub
                # merge in the correlator keys off ``OWNS_MAC`` edges
                # from real Devices, so this is what lets an LLDP
                # stub on cpn-ful-n9k1 (whose ``lldpRemChassisId``
                # subtype 4 returns the FI's OOB MAC) merge onto the
                # canonical FI node here without any name match.
                oob_mac = _norm_mac(fi.get("OutOfBandMac"))
                if oob_mac:
                    fi_mac_node_id = f"mac:{oob_mac}"
                    if not any(n.id == fi_mac_node_id for n in data.nodes):
                        data.nodes.append(GraphNode(
                            id=fi_mac_node_id,
                            type=NodeType.MAC_ADDRESS,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={
                                "mac": oob_mac,
                                "nic_name": "mgmt0",
                                "source": self.instance_id,
                            },
                        ))
                    data.edges.append(GraphEdge(
                        source_id=fi_node_id,
                        target_id=fi_mac_node_id,
                        type=EdgeType.OWNS_MAC,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                        properties={"nic_name": "mgmt0",
                                    "source": "oob_mac"},
                    ))

            # FABRIC_PEER between FI-A and FI-B in the same domain
            if len(fi_group) == 2:
                a_id = fi_node_map.get(fi_group[0]["Moid"], "")
                b_id = fi_node_map.get(fi_group[1]["Moid"], "")
                if a_id and b_id:
                    data.edges.append(GraphEdge(
                        source_id=a_id,
                        target_id=b_id,
                        type=EdgeType.FABRIC_PEER,
                        dimension=Dimension.FABRIC,
                        source_adapter=self.instance_id,
                        properties={"peer_type": "ucs_cluster"},
                    ))

        # ── 2a. Resolve FI physical ports → (FI Moid, port name) ─────────────
        #
        # ``ether/PhysicalPort`` records have no direct ``NetworkElement``
        # MoRef — Intersight only exposes ``RegisteredDevice`` (the UCS
        # domain registration shared by both FIs) and ``SwitchId``
        # ("A" / "B").  Combine them with the FIs themselves (which
        # carry both fields) to key each port back to its owning FI Moid.
        # ``Dn`` (e.g. ``switch-FDO28380QLJ/slot-1/switch-ether/port-11``)
        # is a useful fallback when ``SwitchId`` is missing.
        fi_by_reg_switch: dict[tuple[str, str], str] = {}
        fi_by_serial: dict[str, str] = {}
        for fi in fis:
            fi_moid = fi.get("Moid", "")
            reg_moid = (fi.get("RegisteredDevice") or {}).get("Moid", "")
            switch_id = (fi.get("SwitchId") or "").strip()
            if fi_moid and reg_moid and switch_id:
                fi_by_reg_switch[(reg_moid, switch_id)] = fi_moid
            serial = (fi.get("Serial") or "").strip()
            if fi_moid and serial:
                fi_by_serial[serial] = fi_moid

        def _fi_for_port(pp: dict) -> str:
            reg_m = (pp.get("RegisteredDevice") or {}).get("Moid", "")
            sw = (pp.get("SwitchId") or "").strip()
            if reg_m and sw:
                hit = fi_by_reg_switch.get((reg_m, sw))
                if hit:
                    return hit
            dn = pp.get("Dn", "") or ""
            if dn.startswith("switch-"):
                serial = dn.split("/", 1)[0][len("switch-"):]
                hit = fi_by_serial.get(serial)
                if hit:
                    return hit
            return ""

        fi_port_to_fi_moid: dict[str, str] = {}
        fi_port_to_port_name: dict[str, str] = {}
        for pp in physical_ports:
            pp_moid = pp.get("Moid", "")
            if not pp_moid:
                continue
            fi_moid = _fi_for_port(pp)
            if not fi_moid:
                continue
            fi_port_to_fi_moid[pp_moid] = fi_moid
            fi_port_to_port_name[pp_moid] = _fi_port_name(
                pp.get("SlotId"), pp.get("PortId"),
            )

        # ── 2b. Plan per-port server→FI links from adapter/ExtEthInterface ───
        #
        # The server-side ``adapter.ExtEthInterface`` is the right
        # bridge for **all** server topologies in Intersight:
        #
        #   * X-Series blades: ExtEthInterface → FI's PhysicalPort (via
        #     the chassis IOM).
        #   * Standalone-CIMC C-series direct-attach (e.g. Nutanix):
        #     ExtEthInterface → FI's PhysicalPort directly.
        #
        # In both cases ``AcknowledgedPeerInterface`` on the
        # ExtEthInterface points at an ``ether.PhysicalPort`` MoRef,
        # and ``AdapterUnit`` maps to ``adapter.Unit`` which we already
        # keyed by ``ComputeNode`` Moid in ``adapter_to_compute``.
        # This replaces the older ``ether/HostPort`` pivot, which
        # only modelled IOM-side ports and broke for direct-attach
        # C-series.
        compute_moid_to_port_links: dict[str, list[dict]] = {}
        ext_ports_resolved = 0
        ext_ports_total = len(ext_eth_ifaces)
        for ext in ext_eth_ifaces:
            peer = ext.get("AcknowledgedPeerInterface") or {}
            if not peer or peer.get("ObjectType") != "ether.PhysicalPort":
                continue
            peer_moid = peer.get("Moid", "")
            fi_moid = fi_port_to_fi_moid.get(peer_moid, "")
            if not fi_moid:
                continue
            adapter_moid = (ext.get("AdapterUnit") or {}).get("Moid", "")
            compute_moid = adapter_to_compute.get(adapter_moid, "")
            if not compute_moid:
                # Fallback for tenants where adapter/Units records
                # don't carry ComputeNode (common for standalone-CIMC
                # direct-attach servers, e.g. Nutanix-on-UCS).  Use
                # the ExtEthInterface's RegisteredDevice (the CIMC
                # device-connector that registers the server in
                # Intersight) and translate to the server's own
                # compute Moid via ``reg_to_compute``.  We index
                # ``compute_moid_to_port_links`` by compute Moid so
                # the per-server loop (which keys on
                # ``device.platform_id``) finds them.
                reg_m = (ext.get("RegisteredDevice") or {}).get("Moid", "")
                compute_moid = reg_to_compute.get(reg_m, "")
            if not compute_moid:
                continue
            host_port = _host_port_name(
                ext.get("SlotId"), ext.get("PortId"),
            )
            if not host_port:
                # Standalone-CIMC servers (Nutanix and other C-series
                # direct-attach) expose ``adapter.ExtEthInterface``
                # without top-level ``SlotId`` / ``PortId`` — the port
                # info is only present in the Redfish-style ``Dn``,
                # e.g. ``/redfish/v1/Chassis/1/NetworkAdapters/
                # UCSC-M-V5Q50GV2_FCH29027EYQ/NetworkPorts/Port-3``.
                # Parse the trailing ``Port-<n>`` for a useful label.
                dn = ext.get("Dn", "") or ""
                if "/NetworkPorts/Port-" in dn:
                    port_n = dn.rsplit("/Port-", 1)[1].split("/", 1)[0]
                    if port_n.isdigit():
                        host_port = f"vic1/{port_n}"
            compute_moid_to_port_links.setdefault(compute_moid, []).append({
                "fi_moid":         fi_moid,
                "host_port_name":  host_port,
                "fi_port_name":    fi_port_to_port_name.get(peer_moid, ""),
                "mac_address":     (ext.get("MacAddress") or "").upper(),
                "ext_dn":          ext.get("Dn", ""),
            })
            ext_ports_resolved += 1

        log.debug(
            "intersight.discover.host_port_map",
            ext_ports_total=ext_ports_total,
            ext_ports_resolved=ext_ports_resolved,
            servers_with_port_data=len(compute_moid_to_port_links),
            fi_physical_ports=len(fi_port_to_fi_moid),
            fis_keyed=len(fi_by_reg_switch),
        )

        # ── 2c. Build blade→chassis and chassis→domain mappings ──────────────
        # equipment/Chassis carries a Blades[] list of MoRefs, allowing us to
        # reverse-map each blade Moid → its chassis Moid (and therefore its
        # UCS domain via chassis.RegisteredDevice).
        blade_moid_to_chassis_moid: dict[str, str] = {}   # blade Moid → chassis Moid
        chassis_moid_to_domain_reg: dict[str, str] = {}   # chassis Moid → domain reg Moid
        for ch in chassis_list:
            ch_moid = ch["Moid"]
            ch_reg = (ch.get("RegisteredDevice") or {}).get("Moid", "")
            if ch_reg:
                chassis_moid_to_domain_reg[ch_moid] = ch_reg
            for blade_ref in (ch.get("Blades") or []):
                b_moid = blade_ref.get("Moid", "")
                if b_moid:
                    blade_moid_to_chassis_moid[b_moid] = ch_moid

        log.debug(
            "intersight.discover.blade_map",
            blades_in_chassis=len(blade_moid_to_chassis_moid),
            chassis_with_domain=len(chassis_moid_to_domain_reg),
        )

        # ── 3. Chassis nodes ──────────────────────────────────────────────────
        chassis_node_map: dict[str, str] = {}  # chassis Moid → node id
        for ch in chassis_list:
            ch_node_id = f"intersight-chassis:{self.instance_name}:{ch['Moid']}"
            chassis_node_map[ch["Moid"]] = ch_node_id
            reg = (ch.get("RegisteredDevice") or {}).get("Moid", "")
            data.nodes.append(GraphNode(
                id=ch_node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": f"Chassis-{ch.get('ChassisId', ch['Moid'][-6:])}",
                    "slug": f"chassis-{ch.get('ChassisId', ch['Moid'][-6:])}",
                    "model": ch.get("Model", ""),
                    "serial": ch.get("Serial", ""),
                    "chassis_id": ch.get("ChassisId"),
                    "oper_state": ch.get("OperState", ""),
                },
            ))
            # Chassis → UCS domain
            if reg and reg in domain_map:
                data.edges.append(GraphEdge(
                    source_id=ch_node_id,
                    target_id=domain_map[reg],
                    type=EdgeType.LOCATED_AT,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))

        # ── 4. Server Profile → name mapping ─────────────────────────────────
        profile_map: dict[str, str] = {}  # assigned server Moid → profile name
        for sp in server_profiles:
            assigned = (sp.get("AssignedServer") or {}).get("Moid", "")
            if assigned:
                profile_map[assigned] = sp.get("Name", "")

        # ── 5. Compute nodes ──────────────────────────────────────────────────
        # Fallback container for servers that aren't tied to a discovered domain
        _unassigned_site_id = f"intersight-site:unassigned:{self.instance_name}"
        _unassigned_added = False

        device_node_map: dict[str, str] = {}
        for dev in devices:
            node_id = f"intersight:{self.instance_name}:{dev.platform_id}"
            device_node_map[dev.platform_id] = node_id
            props = {
                "name": dev.name,
                "platform": dev.platform,
                "platform_id": dev.platform_id,
                "role": dev.role,
                "serial": dev.serial or "",
                "service_profile": profile_map.get(dev.platform_id, ""),
            }
            # Only include mgmt_ip when truthy — writing an empty
            # string would clobber a previously-good value from
            # another adapter via `SET n += row` (0.5.0-dev1).
            if dev.mgmt_ip:
                props["mgmt_ip"] = dev.mgmt_ip
            props.update({k: v for k, v in dev.platform_metadata.items() if v is not None})
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties=props,
            ))

            # Determine the container and FI connections for this server.
            #
            # Resolution order:
            #  1. Blade in chassis: parent = Chassis node; FIs = chassis domain's FIs
            #  2. Server whose own RegisteredDevice matches a UCS domain: parent = domain
            #  3. Standalone CIMC-managed server: parent = unassigned fallback
            #
            dev_moid = dev.platform_id
            reg_moid = dev.platform_metadata.get("device_moid", "")
            fi_domain_reg: str = ""   # domain reg Moid to look up FIs

            if dev_moid in blade_moid_to_chassis_moid:
                # ── Blade server — parent is its chassis ──────────────────────
                ch_moid = blade_moid_to_chassis_moid[dev_moid]
                container_id = chassis_node_map.get(ch_moid, "")
                fi_domain_reg = chassis_moid_to_domain_reg.get(ch_moid, "")
                if not container_id:
                    # Chassis not yet registered; fall back to domain
                    container_id = domain_map.get(fi_domain_reg, "")

            elif reg_moid and reg_moid in domain_map:
                # ── Rack-unit managed by a UCS domain ─────────────────────────
                container_id = domain_map[reg_moid]
                fi_domain_reg = reg_moid

            else:
                # ── Standalone CIMC-managed server ────────────────────────────
                container_id = ""

            if not container_id:
                container_id = _unassigned_site_id
                if not _unassigned_added:
                    data.nodes.append(GraphNode(
                        id=_unassigned_site_id,
                        type=NodeType.PLATFORM_SITE,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties={
                            "name": f"Intersight {self.instance_name}",
                            "slug": f"intersight-{self.instance_name}",
                            "platform": "intersight",
                            "normalized_name": f"intersight {self.instance_name}",
                        },
                    ))
                    _unassigned_added = True

            data.edges.append(GraphEdge(
                source_id=node_id,
                target_id=container_id,
                type=EdgeType.LOCATED_AT,
                dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
            ))

            # Server → FI PHYSICAL_LINK emission.
            #
            # Two paths, in preference order:
            #
            #   (1) Port-accurate emission from ``ether/HostPorts``:
            #       one edge per host port whose ``AcknowledgedPeerInterface``
            #       Intersight has confirmed.  Carries real interface_a /
            #       interface_b values so the cable shows up in Detail
            #       view exactly where the operator expects.  Works for
            #       standalone CIMC-managed servers (e.g. Nutanix nodes
            #       on Cisco UCS) where ``RegisteredDevice`` is the
            #       server's own CIMC, not the UCS Domain.
            #
            #   (2) Generic dual-FI fallback when no acknowledged host
            #       ports are available for this server.  Emits one
            #       edge per FI in the domain with empty interfaces.
            #       This preserves behaviour for UCSM-/IMM-managed
            #       servers whose VIC port → FI port mapping isn't
            #       exposed via HostPorts on the firmware in use.
            port_links = compute_moid_to_port_links.get(dev_moid, [])
            if port_links:
                for pl in port_links:
                    fi_node = fi_node_map.get(pl["fi_moid"], "")
                    if not fi_node:
                        continue
                    edge_props: dict[str, Any] = {
                        "discovery_proto": "intersight",
                        "link_type":       "server_to_fi",
                        "interface_a":     pl["host_port_name"],
                        "interface_b":     pl["fi_port_name"],
                    }
                    if pl.get("mac_address"):
                        edge_props["mac_address"] = pl["mac_address"]
                    data.edges.append(GraphEdge(
                        source_id=node_id,
                        target_id=fi_node,
                        type=EdgeType.PHYSICAL_LINK,
                        dimension=Dimension.PHYSICAL,
                        source_adapter=self.instance_id,
                        properties=edge_props,
                    ))
            elif fi_domain_reg and fi_domain_reg in fi_by_domain:
                for fi in fi_by_domain[fi_domain_reg]:
                    fi_node = fi_node_map.get(fi["Moid"], "")
                    if fi_node:
                        data.edges.append(GraphEdge(
                            source_id=node_id,
                            target_id=fi_node,
                            type=EdgeType.PHYSICAL_LINK,
                            dimension=Dimension.PHYSICAL,
                            source_adapter=self.instance_id,
                            properties={
                                "discovery_proto": "intersight",
                                "link_type": "server_to_fi",
                                "switch_id": fi.get("SwitchId", ""),
                            },
                        ))

        # ── 6. Adapter cards → Interface nodes + HAS_INTERFACE edges ─────────
        # Reuses ``adapter_to_compute`` built upfront (step 1).
        for adp in adapters:
            compute_moid = (adp.get("ComputeNode") or {}).get("Moid", "")
            if not compute_moid:
                continue
            server_node = device_node_map.get(compute_moid, "")
            if not server_node:
                continue
            iface_node_id = f"intersight-adp:{self.instance_name}:{adp['Moid']}"
            data.nodes.append(GraphNode(
                id=iface_node_id,
                type=NodeType.INTERFACE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": adp.get("Pid") or adp.get("Model") or adp["Moid"],
                    "model": adp.get("Model", ""),
                    "device_id": compute_moid,
                },
            ))
            data.edges.append(GraphEdge(
                source_id=server_node,
                target_id=iface_node_id,
                type=EdgeType.HAS_INTERFACE,
                dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
            ))

        # ── 7. Server host NIC interfaces → MACAddress nodes + OWNS_MAC edges ──
        # adapter/HostEthInterfaces gives per-vNIC MACs tied to an AdapterUnit
        # (which links to a compute node).  We build: server→mac and
        # mac is later correlated by the correlation engine to a FI switch port.
        # ``adapter_to_compute`` is the upfront map built in step 1.
        for hei in host_eth_ifaces:
            raw_mac = hei.get("MacAddress", "")
            mac = _norm_mac(raw_mac)
            if not mac:
                continue
            adapter_moid = (hei.get("AdapterUnit") or {}).get("Moid", "")
            compute_moid = adapter_to_compute.get(adapter_moid, "")
            if not compute_moid:
                # Try registered device fallback
                compute_moid = (hei.get("RegisteredDevice") or {}).get("Moid", "")
            server_node = device_node_map.get(compute_moid, "")
            if not server_node:
                continue

            mac_node_id = f"mac:{mac}"
            if not any(n.id == mac_node_id for n in data.nodes):
                data.nodes.append(GraphNode(
                    id=mac_node_id,
                    type=NodeType.MAC_ADDRESS,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=self.instance_id,
                    properties={
                        "mac": mac,
                        "nic_name": hei.get("Name", ""),
                        "oper_state": hei.get("OperState", ""),
                        "source": self.instance_id,
                    },
                ))
            data.edges.append(GraphEdge(
                source_id=server_node,
                target_id=mac_node_id,
                type=EdgeType.OWNS_MAC,
                dimension=Dimension.PHYSICAL,
                source_adapter=self.instance_id,
                properties={"nic_name": hei.get("Name", "")},
            ))

        # ── 7. HyperFlex clusters → VNI-style site nodes ─────────────────────
        for hx in hx_clusters:
            hx_node_id = f"intersight-hx:{self.instance_name}:{hx['Moid']}"
            data.nodes.append(GraphNode(
                id=hx_node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL, Dimension.FABRIC],
                source_adapter=self.instance_id,
                properties={
                    "name": hx.get("Name", hx["Moid"]),
                    "slug": (hx.get("Name", hx["Moid"])).lower().replace(" ", "-"),
                    "cluster_type": hx.get("ClusterType", ""),
                    "hx_version": hx.get("HxVersion", ""),
                    "num_nodes": hx.get("NumNodes"),
                    # Don't emit empty mgmt_ip (same `SET n += row` guard).
                    **({"mgmt_ip": hx["ManagementIpAddress"]}
                       if hx.get("ManagementIpAddress") else {}),
                },
            ))

        log.info(
            "intersight.discover.done",
            instance=self.instance_id,
            nodes=len(data.nodes),
            edges=len(data.edges),
        )
        return data
