"""Cisco Meraki Dashboard API adapter."""

from __future__ import annotations

import asyncio
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
from netcortex.util.timestamps import iso_to_epoch_ms

log = structlog.get_logger(__name__)

# Meraki productType → NetBox role slug
ROLE_MAP = {
    "appliance": "firewall",
    "switch": "switch",
    "wireless": "access-point",
    "camera": "camera",
    "sensor": "sensor",
    "cellularGateway": "router",
    "systemsManager": "other",
}

# Normalise a MAC address to lower-case colon-separated form
_MAC_RE = re.compile(r"[^0-9a-fA-F]")


def _norm_site_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

def _norm_mac(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _MAC_RE.sub("", raw)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i+2] for i in range(0, 12, 2)).lower()


def _norm_device_name(raw: str | None) -> str:
    """Canonicalise a Meraki device name for graph use.

    Meraki dashboard occasionally has names with trailing/leading
    whitespace (e.g. ``"Home MX "``) which then makes cross-system
    joins (NetBox lookups, name-based correlation, ``device_down``
    grouping) silently miss matches.  This is a one-line ingest-time
    normalisation that strips both ends and folds internal runs of
    whitespace into single spaces.  Returns ``""`` for falsy input so
    callers can fall back to a stable identifier (serial, MAC).
    """
    if not raw:
        return ""
    return re.sub(r"\s+", " ", str(raw)).strip()


# Meraki AutoVPN peer reachability → operational status mapping.
# The dashboard reports ``reachable | unreachable | unknown`` per peer; we
# normalise to the same ``up | down`` vocabulary used everywhere else in
# the graph so the history correlator, top_problems link_down detector,
# and the staleness policy all "just work" for SD-WAN tunnels.
_REACHABILITY_TO_OPER_STATUS = {
    "reachable":   "up",
    "unreachable": "down",
}


def _reachability_to_oper_status(reachability: str | None) -> str | None:
    """Map Meraki AutoVPN ``reachability`` to canonical ``oper_status``.

    Returns ``None`` for ``"unknown"`` / missing / unrecognised values so
    the history correlator (which requires a non-null oper_status) skips
    the edge entirely rather than recording bogus ``unknown`` transitions.
    """
    if not reachability:
        return None
    return _REACHABILITY_TO_OPER_STATUS.get(str(reachability).strip().lower())


# Meraki prefix-discovery ``scope`` → discriminator ``kind`` mapping.
# Adapter-internal scopes (vlan/vlan6/svi/svi6/static) collapse onto a
# small operator-facing taxonomy that downstream tools (UI prefix table,
# audit reports, route-cause hints) can switch on without re-deriving the
# distinction.  Unknown scopes leave ``kind`` unset so we never invent a
# false label.
_PREFIX_SCOPE_TO_KIND = {
    "vlan":   "vlan_subnet",
    "vlan6":  "vlan_subnet",
    "svi":    "vlan_subnet",
    "svi6":   "vlan_subnet",
    "static": "static_route",
}


def _scope_to_prefix_kind(scope: str | None) -> str | None:
    """Map a Meraki prefix-source scope to the operator-facing ``kind``.

    ``scope`` is the granular adapter-internal label (vlan / vlan6 /
    svi / svi6 / static).  ``kind`` is the discriminator the rest of
    the system reasons about (``vlan_subnet`` / ``static_route`` /
    future ``transit`` / ``wan``).  Returns ``None`` when the scope is
    unknown so unknown sources don't get mislabeled.
    """
    if not scope:
        return None
    return _PREFIX_SCOPE_TO_KIND.get(str(scope).strip().lower())


class _MerakiRetryTransport(httpx.AsyncBaseTransport):
    """httpx transport that auto-retries on Meraki 429 rate-limit responses.

    Honors the server's ``Retry-After`` header when present (capped at
    ``max_retry_after_s`` to avoid pathological hangs) and falls back to
    capped exponential backoff for transient 5xx errors.  Other status
    codes are returned untouched.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport,
                 max_retries: int = 3,
                 max_retry_after_s: float = 8.0) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._max_retry_after_s = max_retry_after_s

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        last_resp: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            resp = await self._inner.handle_async_request(request)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            last_resp = resp
            if attempt >= self._max_retries:
                return resp
            # Compute sleep: prefer Retry-After header (seconds), else
            # exponential backoff with jitter (0.5s, 1s, 2s, 4s …).
            await resp.aread()
            await resp.aclose()
            retry_after = resp.headers.get("Retry-After")
            sleep_s = 0.5 * (2 ** attempt)
            if retry_after:
                try:
                    sleep_s = min(float(retry_after), self._max_retry_after_s)
                except (TypeError, ValueError):
                    pass
            sleep_s = min(sleep_s, self._max_retry_after_s)
            await asyncio.sleep(sleep_s)
        # If we exhausted retries return the last response so callers see
        # the original status.
        return last_resp  # type: ignore[return-value]

    async def aclose(self) -> None:
        await self._inner.aclose()


def _client(api_key: str, timeout: float = 30.0) -> httpx.AsyncClient:
    """Return a configured httpx client for Meraki API calls.

    follow_redirects=True is required because the Meraki Gov API (and some
    regional endpoints) return 308 Permanent Redirects to the assigned shard.
    The transport auto-retries on 429 (rate limit) and 5xx responses with
    backoff that honors Meraki's Retry-After header.
    """
    inner = httpx.AsyncHTTPTransport(retries=0)
    return httpx.AsyncClient(
        headers={"X-Cisco-Meraki-API-Key": api_key},
        follow_redirects=True,
        timeout=timeout,
        transport=_MerakiRetryTransport(inner),
    )


class MerakiAdapter(PlatformAdapter):
    name = "meraki"
    display_name = "Cisco Meraki"
    profile = PlatformProfile(
        device_id_field="serial",
        role_map=ROLE_MAP,
        native_topology=True,
        provides_oper_status=True,
        default_access_methods=["ssh"],
        netbox_platform_slug="meraki",
        supported_dimensions=["physical", "logical", "sdwan"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        # base_url allows pointing at a private Meraki Dashboard (e.g. on-prem/gov)
        self._base_url: str = config.get("base_url", "https://api.meraki.com/api/v1").rstrip("/")
        self._api_key: str = config["api_key"]
        self._org_id: str = config["org_id"]
        self._network_ids: list[str] | None = config.get("network_ids")

    async def authenticate(self) -> None:
        async with _client(self._api_key, timeout=10) as client:
            resp = await client.get(f"{self._base_url}/organizations/{self._org_id}")
            if resp.status_code == 401:
                raise AuthError("Invalid Meraki API key")
            if resp.status_code == 404:
                raise AuthError(f"Meraki org {self._org_id!r} not found")
            resp.raise_for_status()

    async def list_devices(self) -> list[NormalizedDevice]:
        devices: list[NormalizedDevice] = []
        # Pre-fetch MX uplink IPs (WAN/public) and per-network appliance VLAN
        # SVIs (the MX's own interface IP on each LAN). Both fetches are
        # best-effort; failures fall back to whatever lanIp is in the device
        # payload (which is often empty for MX).
        try:
            uplink_by_serial = await self._fetch_uplink_addresses_by_serial()
        except Exception as exc:
            log.warning("meraki.uplink_status_failed",
                        instance=self.instance_id, error=str(exc))
            uplink_by_serial = {}
        try:
            vlans_by_network = await self._fetch_appliance_vlans_by_network()
        except Exception as exc:
            log.warning("meraki.appliance_vlans_failed",
                        instance=self.instance_id, error=str(exc))
            vlans_by_network = {}
        try:
            sdwan_serials = await self._fetch_sdwan_member_serials()
        except Exception as exc:
            log.debug("meraki.sdwan_members_failed", error=str(exc))
            sdwan_serials = set()

        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/devices"
            )
            if not resp.is_success:
                raise AdapterError(f"Meraki list_devices failed: HTTP {resp.status_code}")

            for d in resp.json():
                if self._network_ids and d.get("networkId") not in self._network_ids:
                    continue
                product_type = d.get("productType", "switch")
                serial = d["serial"]
                network_id = d.get("networkId") or ""
                uplinks = uplink_by_serial.get(serial, {})

                # ── MX IP selection rule (user-specified, dev16, 0.5.0-dev1) ──
                # Operator-mandated rule, applied strictly at emit time so
                # housekeeping never has to repair a transient empty value:
                #
                #   * SD-WAN MX  → primary = applianceIp of the LOWEST-numbered
                #                  appliance VLAN (the address other SD-WAN
                #                  peers route to via AutoVPN).
                #                  Fallback chain (each only used when the
                #                  previous tier yields no data):
                #                    1. Lowest-VLAN applianceIp from
                #                       /networks/<n>/appliance/vlans
                #                    2. vpnIp from
                #                       /organizations/<o>/appliance/vpn/statuses
                #                       (Meraki's derived first-host of the
                #                       first exported AutoVPN subnet —
                #                       equivalent to lowest applianceIp)
                #                    3. wan1Ip → wan2Ip — last-resort *only*
                #                       for transit-only SD-WAN appliances
                #                       (no LAN VLANs configured, no AutoVPN
                #                       subnet exported). Without this
                #                       fallback those MXs would emit with
                #                       no mgmt_ip and sit empty until the
                #                       10-min housekeeping pass salvaged
                #                       them from candidate_ips[0].
                #
                #   * Non-SD-WAN MX → primary = WAN port IP (wan1Ip → wan2Ip).
                #                     Public/NAT addresses are informational
                #                     only and NEVER promoted to mgmt_ip —
                #                     they belong to the carrier, not the
                #                     device.
                #
                # Crucially: when the Meraki API returned NOTHING for this
                # device (rate-limited, transient 5xx, etc.), primary_ip
                # stays ``None`` and the empty-keyed fields are stripped
                # before the graph emit (see ``discover()`` ~L913) so the
                # previous known-good IP data in Neo4j is preserved instead
                # of being silently clobbered with empty strings.
                candidate_ips: list[str] = []
                primary_ip: str | None = None
                if product_type == "appliance":
                    on_sdwan = serial in sdwan_serials
                    net_vlans = vlans_by_network.get(network_id, [])
                    sorted_vlans = sorted(
                        [v for v in net_vlans if v.get("applianceIp")],
                        key=lambda v: int(v.get("id") or 0),
                    )
                    wan_ips = [
                        uplinks.get(k) for k in ("wan1Ip", "wan2Ip")
                        if uplinks.get(k)
                    ]
                    public_ips = [
                        uplinks.get(k)
                        for k in ("wan1PublicIp", "wan2PublicIp", "publicIp")
                        if uplinks.get(k)
                    ]
                    sdwan_ip = uplinks.get("vpnIp")
                    if on_sdwan:
                        # Primary MUST be the lowest-numbered VLAN's
                        # applianceIp. Fall back to vpnIp when the
                        # appliance-VLAN fetch returned nothing. As a
                        # LAST resort (no LAN, no AutoVPN subnet exported)
                        # accept the WAN IP — transit-only SD-WAN MXs
                        # have nothing else reachable.
                        if sorted_vlans:
                            primary_ip = sorted_vlans[0]["applianceIp"]
                        elif sdwan_ip:
                            primary_ip = sdwan_ip
                        elif wan_ips:
                            primary_ip = wan_ips[0]
                    else:
                        # Non-SD-WAN: primary MUST be a WAN port IP. Never
                        # promote a public NAT IP or a LAN SVI to primary.
                        if wan_ips:
                            primary_ip = wan_ips[0]

                    # Build the candidate list (primary first, then every
                    # other reachable address) for SNMP fall-through.
                    if primary_ip:
                        candidate_ips.append(primary_ip)
                    for v in (
                        [vl["applianceIp"] for vl in sorted_vlans]
                        + wan_ips
                        + public_ips
                        + ([sdwan_ip] if sdwan_ip else [])
                    ):
                        if v and v not in candidate_ips:
                            candidate_ips.append(v)
                else:
                    # Non-appliance Meraki devices (MS/MR/MV/CW/MG/MT/Z).
                    # Prefer the lanIp Meraki publishes for them.
                    if d.get("lanIp"):
                        primary_ip = d["lanIp"]
                        candidate_ips.append(d["lanIp"])

                # Build platform_metadata. Any field whose source value is
                # empty/None is OMITTED entirely so the downstream graph
                # emit (which strips Nones) does not overwrite the
                # previous good value in Neo4j with an empty string.
                meta: dict[str, Any] = {
                    "networkId": d.get("networkId"),
                    "model": d.get("model"),
                    "firmware": d.get("firmware"),
                    "os_version": d.get("firmware") or "",
                    "status": d.get("status") or "active",
                    "productType": product_type,
                    "tags": d.get("tags", []),
                    "lat": d.get("lat"),
                    "lng": d.get("lng"),
                    "address": d.get("address", ""),
                    "mac": _norm_mac(d.get("mac")),
                }
                # IP-related fields: only include when populated. This
                # protects against partial / rate-limited discovery cycles
                # clobbering a previously-good IP with "".
                if candidate_ips:
                    meta["candidate_ips"] = candidate_ips
                if uplinks.get("wan1Ip"):
                    meta["wan1_ip"] = uplinks["wan1Ip"]
                if uplinks.get("wan2Ip"):
                    meta["wan2_ip"] = uplinks["wan2Ip"]
                if uplinks.get("wan1PublicIp"):
                    meta["wan1_public_ip"] = uplinks["wan1PublicIp"]
                if uplinks.get("wan2PublicIp"):
                    meta["wan2_public_ip"] = uplinks["wan2PublicIp"]
                if uplinks.get("vpnIp"):
                    meta["vpn_ip"] = uplinks["vpnIp"]
                # on_sdwan is always known (False if not in sdwan_serials).
                meta["on_sdwan"] = (product_type == "appliance"
                                    and serial in sdwan_serials)

                devices.append(
                    NormalizedDevice(
                        name=_norm_device_name(d.get("name")) or serial,
                        platform=self.name,
                        platform_id=serial,
                        role=ROLE_MAP.get(product_type, "other"),
                        serial=serial,
                        mgmt_ip=primary_ip,
                        platform_metadata=meta,
                    )
                )
        return devices

    # ── Per-instance caches with TTL ─────────────────────────────────────
    # The Meraki Dashboard API enforces a strict 10 req/sec/org rate limit.
    # We cache the slow-changing data (VLAN SVI IPs, SD-WAN membership) for
    # several minutes so successive discovery passes don't burn through the
    # quota.
    _APPLIANCE_VLANS_TTL_S = 300.0
    _SDWAN_MEMBERS_TTL_S   = 300.0

    async def _fetch_appliance_vlans_by_network(
        self,
    ) -> dict[str, list[dict[str, str | int]]]:
        """Return appliance VLANs per network keyed by networkId.

        Each entry is ``{"id": <vlan_id>, "applianceIp": "..."}``. The
        ``applianceIp`` is the MX's own L3 interface on that VLAN — the
        ideal SNMP polling target for an MX sitting on the SD-WAN.
        Failures on individual networks are silently skipped.

        Cached on the adapter instance for ``_APPLIANCE_VLANS_TTL_S`` to
        avoid burning Meraki rate-limit budget across back-to-back cycles.
        """
        import time as _time
        cache = getattr(self, "_appliance_vlans_cache", None)
        if cache and (_time.monotonic() - cache[0]) < self._APPLIANCE_VLANS_TTL_S:
            return cache[1]

        import ipaddress as _ip

        out: dict[str, list[dict[str, str | int]]] = {}

        def _valid_ip(v: str | None) -> bool:
            if not v:
                return False
            try:
                addr = _ip.ip_address(v)
            except ValueError:
                return False
            return not (
                addr.is_unspecified or addr.is_loopback or addr.is_link_local
            )

        async with _client(self._api_key) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/networks"
                )
                resp.raise_for_status()
                networks = resp.json()
            except Exception as exc:
                log.debug("meraki.networks_failed", error=str(exc))
                return out

            sem = asyncio.Semaphore(8)

            async def _fetch_one(net: dict) -> None:
                if self._network_ids and net.get("id") not in self._network_ids:
                    return
                async with sem:
                    try:
                        r = await client.get(
                            f"{self._base_url}/networks/{net['id']}/"
                            f"appliance/vlans"
                        )
                        if not r.is_success:
                            return
                        vlans = []
                        for v in r.json() or []:
                            ip = v.get("applianceIp")
                            try:
                                vid = int(v.get("id"))
                            except (TypeError, ValueError):
                                continue
                            if _valid_ip(ip):
                                vlans.append({"id": vid, "applianceIp": str(ip)})
                        if vlans:
                            out[net["id"]] = vlans
                    except Exception as exc:
                        log.debug("meraki.appliance_vlans_one_failed",
                                  net=net.get("id"), error=str(exc))

            await asyncio.gather(*[_fetch_one(n) for n in networks])
        # Only cache non-empty results so a single rate-limit blip doesn't
        # poison the cache for the full TTL.
        if out:
            self._appliance_vlans_cache = (_time.monotonic(), out)
        return out

    async def _fetch_sdwan_member_serials(self) -> set[str]:
        """Return set of MX serials currently active in the AutoVPN/SD-WAN.

        An MX is considered "on SD-WAN" when it appears in
        ``/organizations/{org}/appliance/vpn/statuses`` with any exported
        subnet — that's the definitive sign it's participating in the
        Meraki AutoVPN overlay.

        Cached on the adapter instance for ``_SDWAN_MEMBERS_TTL_S``.
        """
        import time as _time
        cache = getattr(self, "_sdwan_members_cache", None)
        if cache and (_time.monotonic() - cache[0]) < self._SDWAN_MEMBERS_TTL_S:
            return cache[1]

        members: set[str] = set()
        async with _client(self._api_key) as client:
            try:
                r = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/"
                    f"appliance/vpn/statuses"
                )
                if not r.is_success:
                    return members
                for row in r.json() or []:
                    serial = row.get("deviceSerial")
                    if not serial:
                        continue
                    # Any exported subnet (even empty list with vpnMode hub)
                    # implies SD-WAN participation. Be permissive.
                    if (row.get("exportedSubnets") is not None
                            or row.get("vpnMode") in ("hub", "spoke")):
                        members.add(serial)
            except Exception as exc:
                log.debug("meraki.vpn_statuses_members_failed", error=str(exc))
        # Only cache non-empty results — see _fetch_appliance_vlans_by_network
        if members:
            self._sdwan_members_cache = (_time.monotonic(), members)
        return members

    async def _fetch_uplink_addresses_by_serial(self) -> dict[str, dict[str, str]]:
        """Return per-MX uplink IPs keyed by device serial.

        Combines two Meraki Dashboard endpoints:
          * ``/organizations/{org_id}/appliance/uplink/statuses`` — current
            WAN interface IPs (private or public, as the MX sees them) and
            publicIp (NAT egress).
          * ``/organizations/{org_id}/appliance/vpn/statuses`` — AutoVPN
            overlay address (subnet/IP advertised to other hubs/spokes),
            usable from any peer on the SD-WAN fabric.

        The returned dict has keys: ``wan1Ip``, ``wan2Ip``, ``wan1PublicIp``,
        ``wan2PublicIp``, ``publicIp``, ``vpnIp``. Missing fields are
        omitted, not None-padded. Best-effort: each endpoint is wrapped in
        its own try/except so a single failure doesn't poison the others.
        """
        import ipaddress as _ip

        out: dict[str, dict[str, str]] = {}

        def _store(serial: str, key: str, val: str | None) -> None:
            if not serial or not val:
                return
            v = str(val).strip()
            if not v:
                return
            # Skip obviously-bogus values (e.g., "0.0.0.0", link-local).
            try:
                addr = _ip.ip_address(v)
            except ValueError:
                return
            if addr.is_unspecified or addr.is_loopback or addr.is_link_local:
                return
            out.setdefault(serial, {}).setdefault(key, v)

        async with _client(self._api_key) as client:
            # Uplink statuses — gives wan1/wan2 + publicIp
            try:
                r = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/"
                    f"appliance/uplink/statuses"
                )
                if r.is_success:
                    for row in r.json() or []:
                        serial = row.get("serial") or ""
                        for u in row.get("uplinks") or []:
                            iface = (u.get("interface") or "").lower()
                            if iface not in ("wan1", "wan2"):
                                continue
                            _store(serial, f"{iface}Ip", u.get("ip"))
                            _store(serial, f"{iface}PublicIp", u.get("publicIp"))
                        # Org-level publicIp fallback (some payloads put it
                        # at the row level instead of per-uplink).
                        if row.get("publicIp"):
                            _store(serial, "publicIp", row.get("publicIp"))
            except Exception as exc:
                log.debug("meraki.uplink_statuses_failed", error=str(exc))

            # SD-WAN VPN overlay addresses
            try:
                r = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/"
                    f"appliance/vpn/statuses"
                )
                if r.is_success:
                    for row in r.json() or []:
                        serial = row.get("deviceSerial") or ""
                        # Each MX advertises subnets; the first IPv4 host
                        # in the first subnet is the closest thing to a
                        # "VPN IP" for the device itself.
                        for sub in row.get("exportedSubnets") or []:
                            cidr = sub.get("subnet")
                            if not cidr:
                                continue
                            try:
                                net = _ip.ip_network(cidr, strict=False)
                                if isinstance(net, _ip.IPv4Network):
                                    _store(serial, "vpnIp", str(next(net.hosts())))
                                    break
                            except (ValueError, StopIteration):
                                continue
            except Exception as exc:
                log.debug("meraki.vpn_statuses_failed", error=str(exc))

        return out

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        """Return switch ports for a Meraki MS device."""
        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/devices/{device_id}/switch/ports"
            )
            if resp.status_code == 404:
                return []  # Not a switch
            if not resp.is_success:
                raise AdapterError(f"Meraki list_interfaces failed: HTTP {resp.status_code}")

            return [
                NormalizedInterface(
                    name=f"Port {p['portId']}",
                    device_platform_id=device_id,
                    description=p.get("name", ""),
                    enabled=p.get("enabled", True),
                    platform_id=str(p["portId"]),
                )
                for p in resp.json()
            ]

    async def get_switch_port_statuses(
        self, serial: str, timespan: int = 600,
    ) -> dict[str, dict[str, Any]]:
        """Return per-port operational stats for one MS switch.

        Calls ``/devices/{serial}/switch/ports/statuses?timespan=N`` which
        returns each port's connection state, speed, error/warning counts,
        and bytes/sec utilization for the trailing window.

        Returned shape: ``{port_id: {oper_status, speed_mbps, util_pct,
        error_rate_per_s, health_score, status, errors, warnings,
        is_uplink, usage_total_kb}}``.  Empty dict on failure / non-switch.

        Health score follows the same 0-100 (higher = worse) vocabulary
        the SNMP adapter uses, so the existing
        ``_enrich_physical_links_with_health`` correlator picks Meraki
        and SNMP interfaces up uniformly.
        """
        async with _client(self._api_key) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/devices/{serial}/switch/ports/statuses",
                    params={"timespan": timespan},
                )
            except Exception as exc:
                log.debug("meraki.port_statuses.fetch_failed",
                          serial=serial, error=str(exc))
                return {}
            if resp.status_code == 404:
                return {}  # not a switch
            if not resp.is_success:
                log.debug("meraki.port_statuses.http_error",
                          serial=serial, status=resp.status_code)
                return {}

            out: dict[str, dict[str, Any]] = {}
            for p in resp.json():
                port_id = str(p.get("portId", ""))
                if not port_id:
                    continue
                status_raw = (p.get("status") or "").lower()
                # Meraki uses "Connected" / "Disconnected" / "Disabled" /
                # "Ready" — normalize to up/down/disabled vocabulary used
                # by the SNMP adapter so styling code can branch on one
                # vocab regardless of source.
                if status_raw == "connected":
                    oper = "up"
                elif status_raw in ("disconnected", "ready", ""):
                    oper = "down"
                elif status_raw == "disabled":
                    oper = "disabled"
                else:
                    oper = status_raw

                # Speed: Meraki returns strings like "1 Gbps" / "100 Mbps".
                # Convert to Mbps integer; None when the port is down.
                speed_mbps: int | None = None
                speed_str = (p.get("speed") or "").strip()
                if speed_str:
                    try:
                        n_str, unit = speed_str.split(" ", 1)
                        n = float(n_str)
                        u = unit.lower()
                        if u.startswith("gbp"):
                            speed_mbps = int(n * 1000)
                        elif u.startswith("mbp"):
                            speed_mbps = int(n)
                        elif u.startswith("kbp"):
                            speed_mbps = max(1, int(n / 1000))
                    except (ValueError, IndexError):
                        speed_mbps = None

                # Utilization: bytes/sec averaged across the timespan,
                # converted to % of port speed.  Meraki returns "kbps"
                # values inside trafficInKbps {total, sent, recv}.  When
                # absent, fall back to raw kb counters in usageInKb.
                util_pct: float | None = None
                traffic = p.get("trafficInKbps") or {}
                kbps_total = traffic.get("total")
                if kbps_total is None and speed_mbps:
                    usage = p.get("usageInKb") or {}
                    total_kb = usage.get("total") or 0
                    if total_kb and timespan:
                        # kb over the window → kbps
                        kbps_total = (total_kb * 8) / timespan
                if kbps_total is not None and speed_mbps:
                    util_pct = round(min(100.0, (kbps_total / 1000.0) /
                                          max(1, speed_mbps) * 100.0), 2)

                # Errors / warnings: Meraki returns string codes.  Treat
                # presence of any "error" entry as a fault signal, but
                # we don't have per-second rate from the API — derive a
                # coarse error_rate that pushes the health score up
                # without overstating noise.
                errors = p.get("errors") or []
                warnings = p.get("warnings") or []
                if errors:
                    err_rate = 5.0  # >=10/s bucket in _interface_health_score
                elif warnings:
                    err_rate = 0.5  # nudge into "warning" bucket
                else:
                    err_rate = 0.0

                # Health score with the SAME vocabulary the SNMP adapter
                # uses (higher = worse).  Computed inline so we don't
                # have to import _interface_health_score from snmp.py
                # (which would create a cross-adapter dep).
                if util_pct is None or util_pct >= 95:
                    u_score = 80 if util_pct and util_pct >= 95 else 0
                elif util_pct >= 80:
                    u_score = 50
                elif util_pct >= 50:
                    u_score = 20
                else:
                    u_score = 0
                if err_rate >= 10: e_score = 60
                elif err_rate >= 1: e_score = 30
                elif err_rate > 0:  e_score = 10
                else:               e_score = 0
                # Down ports always score worst so the link colors red.
                if oper == "down":
                    health_score = 80
                elif oper == "disabled":
                    health_score = 0  # admin-down ≠ failure
                else:
                    health_score = min(100, u_score + e_score)

                out[port_id] = {
                    "oper_status": oper,
                    "speed_mbps": speed_mbps,
                    "util_pct": util_pct,
                    "util_in_pct": util_pct,
                    "util_out_pct": util_pct,
                    "error_rate_per_s": err_rate,
                    "error_rate_in_per_s": err_rate / 2 if err_rate else 0.0,
                    "error_rate_out_per_s": err_rate / 2 if err_rate else 0.0,
                    "health_score": health_score,
                    "has_baseline": True,
                    "status_raw": p.get("status"),
                    "errors": errors,
                    "warnings": warnings,
                    "is_uplink": bool(p.get("isUplink")),
                }
            return out

    async def get_appliance_uplink_statuses(
        self,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Return per-MX uplink statuses keyed by serial then uplink interface.

        Calls ``/organizations/{orgId}/appliance/uplink/statuses`` which
        returns one row per MX with a list of uplinks (``wan1`` / ``wan2``
        / ``cellular``).  Used by the WAN-health enrichment correlator
        to color the MX→Internet WAN_UPLINK edges with their per-uplink
        operational status rather than the device-wide cloud-status
        rollup.

        Returned shape::

            {
              "Q3FA-8HH5-EAQE": {
                "wan1": {"status": "active", "ip": "...", "publicIp": "...",
                          "oper_status": "up", ...},
                "wan2": {...},
                "__device__": {"last_reported_at_ms": 1721234567000,
                               "last_reported_at_iso": "2024-07-17T16:42:47Z"},
              },
              ...
            }

        The ``__device__`` sentinel key (double-underscore-prefixed so
        it can never collide with a real Meraki uplink interface name)
        carries the per-device ``lastReportedAt`` from the same
        response, normalised to epoch ms.  The discover step uses it
        to stamp ``meraki_last_reported_at`` onto the MX Device node so
        top_problems can decide whether a "down" uplink is a live
        incident or abandoned inventory.
        """
        out: dict[str, dict[str, dict[str, Any]]] = {}
        async with _client(self._api_key) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}"
                    "/appliance/uplink/statuses"
                )
                if not resp.is_success:
                    log.debug("meraki.uplink_statuses.http_error",
                              status=resp.status_code)
                    return out
                rows = resp.json()
            except Exception as exc:
                log.debug("meraki.uplink_statuses.fetch_failed", error=str(exc))
                return out

            for row in rows:
                serial = row.get("serial")
                if not serial:
                    continue
                # Skip MX in networks we're not scoped to.  network_ids
                # is None when "all networks" mode, in which case we
                # accept every row.
                if (self._network_ids is not None
                        and row.get("networkId") not in self._network_ids):
                    continue
                # Capture the device-level "last reported at" so the
                # WAN_UPLINK enrichment step can stamp it onto the MX
                # Device node.  This is the discriminator between
                # "real outage" (last_reported = a few minutes ago)
                # and "abandoned inventory" (last_reported = months
                # ago), and is consumed by top_problems via the
                # `top_problems_stale_after_seconds` knob.
                last_reported_ms = iso_to_epoch_ms(row.get("lastReportedAt"))
                per_uplink: dict[str, dict[str, Any]] = {}
                if last_reported_ms is not None:
                    # Reserved key (double-underscore prefix) so a
                    # downstream iterator can ignore it when iterating
                    # over real uplink slots like wan1/wan2/cellular.
                    per_uplink["__device__"] = {
                        "last_reported_at_ms": last_reported_ms,
                        "last_reported_at_iso": row.get("lastReportedAt"),
                    }
                for u in (row.get("uplinks") or []):
                    iface = u.get("interface")
                    if not iface:
                        continue
                    raw_status = (u.get("status") or "").lower()
                    # Meraki uplink statuses: "active" / "ready" /
                    # "connecting" / "not connected" / "failed".  Map
                    # to up/down so styling code branches uniformly.
                    if raw_status == "active":
                        oper = "up"
                    elif raw_status == "ready":
                        # Standby uplink — physically up but not active
                        # (failover candidate).  Treat as up but mark.
                        oper = "up"
                    elif raw_status in ("not connected", "failed", ""):
                        oper = "down"
                    elif raw_status == "connecting":
                        oper = "unknown"
                    else:
                        oper = raw_status
                    per_uplink[iface] = {
                        "oper_status": oper,
                        "status_raw":  u.get("status"),
                        "ip":          u.get("ip"),
                        "public_ip":   u.get("publicIp"),
                        "gateway":     u.get("gateway"),
                        "primary_dns": u.get("primaryDns"),
                        "is_active":   raw_status == "active",
                    }
                if per_uplink:
                    out[serial] = per_uplink
        return out

    async def list_vlans(self) -> list[NormalizedVLAN]:
        """Return VLANs across all managed appliance networks."""
        vlans: list[NormalizedVLAN] = []
        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/networks"
            )
            resp.raise_for_status()
            networks = resp.json()

            for net in networks:
                if self._network_ids and net["id"] not in self._network_ids:
                    continue
                vlan_resp = await client.get(
                    f"{self._base_url}/networks/{net['id']}/appliance/vlans"
                )
                if not vlan_resp.is_success:
                    continue
                for v in vlan_resp.json():
                    vlans.append(
                        NormalizedVLAN(
                            vid=v["id"],
                            name=v.get("name", f"VLAN{v['id']}"),
                            platform_id=f"{net['id']}:{v['id']}",
                        )
                    )
        return vlans

    async def list_prefixes(self) -> list[dict]:
        """Return all IPv4/IPv6 prefixes Meraki knows about across the org.

        Sources (best-effort, each in its own try/except):
          1. Appliance (MX) VLAN subnets — IPv4 + IPv6
          2. Appliance static routes
          3. Switch L3 routing interfaces (SVIs) on MS switches
          4. Stack-level routing interfaces on switch stacks

        Each prefix is returned as ``{"cidr": "10.0.0.0/24", "name": "...",
        "scope": "vlan|static|svi", "network_id": "L_...", "device_serial":
        "Q5TY-..."}``.  All best-effort: failures on individual endpoints
        only skip that source.
        """
        prefixes: list[dict] = []
        seen: set[str] = set()

        def _add(cidr: str | None, **meta) -> None:
            if not cidr or not isinstance(cidr, str):
                return
            cidr = cidr.strip()
            if "/" not in cidr:
                return
            # Validate the CIDR — silently drop anything that isn't a real
            # IPv4/IPv6 network so the graph never gets garbage prefixes.
            import ipaddress as _ip
            try:
                net = _ip.ip_network(cidr, strict=False)
            except (ValueError, TypeError):
                return
            key = str(net)
            if key in seen:
                return
            seen.add(key)
            prefixes.append({"cidr": key, **meta})

        async with _client(self._api_key) as client:
            # ── List networks once ────────────────────────────────────────
            try:
                resp = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/networks"
                )
                resp.raise_for_status()
                networks = resp.json()
            except Exception as exc:
                log.warning("meraki.prefixes.networks_failed",
                            error=str(exc), instance=self.instance_id)
                return prefixes

            # Devices for switch-level routing interfaces
            try:
                dev_resp = await client.get(
                    f"{self._base_url}/organizations/{self._org_id}/devices"
                )
                dev_resp.raise_for_status()
                devices = dev_resp.json()
            except Exception as exc:
                log.debug("meraki.prefixes.devices_failed", error=str(exc))
                devices = []

            switch_serials_by_net: dict[str, list[str]] = {}
            for d in devices:
                net_id = d.get("networkId")
                serial = d.get("serial")
                ptype = (d.get("model") or "").upper()
                if net_id and serial and (ptype.startswith("MS")
                                          or ptype.startswith("C9")):
                    switch_serials_by_net.setdefault(net_id, []).append(serial)

            sem = asyncio.Semaphore(8)

            async def _fetch_appliance_vlans(net: dict) -> None:
                async with sem:
                    url = (f"{self._base_url}/networks/{net['id']}"
                           "/appliance/vlans")
                    try:
                        r = await client.get(url)
                        if not r.is_success:
                            return
                        for v in r.json():
                            _add(v.get("subnet"),
                                 name=v.get("name"),
                                 scope="vlan",
                                 vlan_id=v.get("id"),
                                 network_id=net["id"])
                            # IPv6
                            ipv6 = v.get("ipv6") or {}
                            for p in ipv6.get("prefixAssignments", []):
                                _add(p.get("staticPrefix"),
                                     name=v.get("name"),
                                     scope="vlan6",
                                     vlan_id=v.get("id"),
                                     network_id=net["id"])
                    except Exception as exc:
                        log.debug("meraki.prefix.vlan_fetch_failed",
                                  net=net.get("id"), error=str(exc))

            async def _fetch_static_routes(net: dict) -> None:
                async with sem:
                    url = (f"{self._base_url}/networks/{net['id']}"
                           "/appliance/staticRoutes")
                    try:
                        r = await client.get(url)
                        if not r.is_success:
                            return
                        for sr in r.json():
                            _add(sr.get("subnet"),
                                 name=sr.get("name"),
                                 scope="static",
                                 network_id=net["id"],
                                 next_hop=sr.get("gatewayIp"))
                    except Exception as exc:
                        log.debug("meraki.prefix.static_fetch_failed",
                                  net=net.get("id"), error=str(exc))

            async def _fetch_switch_l3(serial: str, net_id: str) -> None:
                async with sem:
                    url = (f"{self._base_url}/devices/{serial}"
                           "/switch/routing/interfaces")
                    try:
                        r = await client.get(url)
                        if not r.is_success:
                            return
                        for iface in r.json():
                            _add(iface.get("subnet"),
                                 name=iface.get("name"),
                                 scope="svi",
                                 vlan_id=iface.get("vlanId"),
                                 device_serial=serial,
                                 network_id=net_id)
                            ipv6 = iface.get("ipv6") or {}
                            _add(ipv6.get("prefix"),
                                 name=iface.get("name"),
                                 scope="svi6",
                                 vlan_id=iface.get("vlanId"),
                                 device_serial=serial,
                                 network_id=net_id)
                    except Exception as exc:
                        log.debug("meraki.prefix.svi_fetch_failed",
                                  serial=serial, error=str(exc))

            # Fan out across networks
            tasks: list = []
            for net in networks:
                if self._network_ids and net["id"] not in self._network_ids:
                    continue
                ptypes = net.get("productTypes") or []
                if "appliance" in ptypes:
                    tasks.append(_fetch_appliance_vlans(net))
                    tasks.append(_fetch_static_routes(net))
                if "switch" in ptypes:
                    for serial in switch_serials_by_net.get(net["id"], []):
                        tasks.append(_fetch_switch_l3(serial, net["id"]))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        log.info("meraki.prefixes.discovered",
                 instance=self.instance_id, count=len(prefixes))
        return prefixes

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        return []

    async def get_org_topology(self) -> list[NormalizedTopologyLink]:
        """Fetch org-wide link layer topology from Meraki."""
        links: list[NormalizedTopologyLink] = []
        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/topology/linkLayer"
            )
            if not resp.is_success:
                return []
            for link in resp.json().get("links", []):
                ends = link.get("ends", [])
                if len(ends) != 2:
                    continue
                a, b = ends
                links.append(
                    NormalizedTopologyLink(
                        device_a_platform_id=a.get("device", {}).get("serial", ""),
                        interface_a_name=a.get("discovered", {}).get("portId", "unknown"),
                        device_b_platform_id=b.get("device", {}).get("serial", ""),
                        interface_b_name=b.get("discovered", {}).get("portId", "unknown"),
                        discovery_proto="meraki",
                    )
                )
        return links

    async def get_device_lldp_cdp(self, serial: str) -> dict:
        """Return LLDP/CDP neighbor data for a single device.

        GET /devices/{serial}/lldpCdp
        Response: {"sourceMac": "...", "ports": {"1": {"cdp": {...}, "lldp": {...}}}}
        """
        async with _client(self._api_key) as client:
            resp = await client.get(f"{self._base_url}/devices/{serial}/lldpCdp")
            if not resp.is_success:
                return {}
            return resp.json()

    async def get_org_clients(self, timespan: int = 3600) -> list[dict]:
        """Return recently seen clients across the org.

        Tries the org-level endpoint first (newer API keys); falls back to
        fetching clients per-network in parallel (always available).
        Each entry includes mac, ip, recentDeviceSerial, switchport (wired).
        """
        # ── Attempt 1: org-level bulk endpoint ────────────────────────────
        clients: list[dict] = []
        async with _client(self._api_key) as client:
            params: dict = {"timespan": timespan, "perPage": 1000}
            url = f"{self._base_url}/organizations/{self._org_id}/clients"
            while url:
                resp = await client.get(url, params=params)
                if resp.status_code == 404:
                    break  # endpoint not available — fall through to per-network
                if not resp.is_success:
                    break
                page = resp.json()
                if isinstance(page, list):
                    clients.extend(page)
                else:
                    break
                link_header = resp.headers.get("Link", "")
                next_url = None
                for part in link_header.split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                        break
                url = next_url
                params = {}
            if clients:
                return clients

        # ── Attempt 2: per-network endpoint (always available) ────────────
        import asyncio as _aio
        try:
            networks = await self.list_networks()
        except Exception:
            return []

        # Limit concurrency to avoid API rate limits (5 req/s per org)
        sem = _aio.Semaphore(5)

        async def _fetch_network(net_id: str) -> list[dict]:
            async with sem:
                net_clients: list[dict] = []
                async with _client(self._api_key) as c:
                    p: dict = {"timespan": timespan, "perPage": 1000}
                    u: str | None = f"{self._base_url}/networks/{net_id}/clients"
                    while u:
                        r = await c.get(u, params=p)
                        if not r.is_success:
                            break
                        page = r.json()
                        if isinstance(page, list):
                            net_clients.extend(page)
                        else:
                            break
                        lh = r.headers.get("Link", "")
                        u = None
                        for part in lh.split(","):
                            part = part.strip()
                            if 'rel="next"' in part:
                                u = part.split(";")[0].strip().strip("<>")
                                break
                        p = {}
                return net_clients

        net_ids = [n.get("id") or n.get("networkId", "") for n in networks]
        net_ids = [n for n in net_ids if n]

        # Filter to configured network_ids if the adapter is scoped
        if self._network_ids:
            net_ids = [n for n in net_ids if n in self._network_ids]

        results = await _aio.gather(*[_fetch_network(nid) for nid in net_ids])
        seen_ids: set[str] = set()
        for batch in results:
            for c in batch:
                cid = c.get("id") or c.get("mac", "")
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    clients.append(c)

        log.info("meraki.clients.per_network",
                 networks=len(net_ids), clients=len(clients), instance=self.instance_id)
        return clients

    async def list_networks(self) -> list[dict]:
        """Return all networks in the org."""
        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/networks"
            )
            resp.raise_for_status()
            nets = resp.json()
            if self._network_ids:
                nets = [n for n in nets if n["id"] in self._network_ids]
            return nets

    async def get_vpn_topology(self) -> dict:
        """Return AutoVPN hub/spoke topology for the org (MX appliance networks)."""
        async with _client(self._api_key) as client:
            resp2 = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/appliance/vpn/statuses"
            )
            if not resp2.is_success:
                return {}
            return {"statuses": resp2.json()}

    async def get_uplink_statuses(self) -> list[dict]:
        """Return WAN uplink states for all appliances in the org."""
        async with _client(self._api_key) as client:
            resp = await client.get(
                f"{self._base_url}/organizations/{self._org_id}/appliance/uplink/statuses"
            )
            if not resp.is_success:
                return []
            return resp.json()

    # ── Graph discovery ───────────────────────────────────────────────────────

    async def discover(self) -> GraphData:
        """Build a GraphData object from Meraki org devices, VLANs, topology, and MAC tables."""
        data = GraphData(adapter_id=self.instance_id)

        # 1. Networks → Site nodes
        try:
            networks = await self.list_networks()
        except Exception as exc:
            log.warning("meraki.discover.networks_failed", error=str(exc), instance=self.instance_id)
            networks = []

        network_map: dict[str, str] = {}  # networkId → site node id
        for net in networks:
            site_node_id = f"meraki-network:{net['id']}"
            network_map[net["id"]] = site_node_id
            data.nodes.append(GraphNode(
                id=site_node_id,
                type=NodeType.PLATFORM_SITE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties={
                    "name": net.get("name", net["id"]),
                    "slug": net["id"],
                    "org_id": self._org_id,
                    "network_id": net["id"],
                    "product_types": net.get("productTypes", []),
                    "platform": "meraki",
                    "normalized_name": _norm_site_name(net.get("name", net["id"])),
                },
            ))

        # 2. Devices → Device nodes
        try:
            devices = await self.list_devices()
        except Exception as exc:
            log.warning("meraki.discover.devices_failed", error=str(exc), instance=self.instance_id)
            devices = []

        device_node_map: dict[str, str] = {}  # serial → node id
        # Maps networkId → MX appliance device node id.
        # Used so SD-WAN tunnel edges connect the actual MX device,
        # not the PlatformSite container.
        network_mx_map: dict[str, str] = {}
        for dev in devices:
            node_id = f"meraki:{dev.platform_id}"
            device_node_map[dev.platform_id] = node_id
            # Device-node properties. We deliberately OMIT any IP-related
            # field whose value is empty/None so a rate-limited / partial
            # discovery cycle never clobbers Neo4j's previously good IPs
            # (ingest uses `SET n += row`, so an empty-string write would
            # overwrite a valid stored value). Non-IP "shape" fields like
            # name/role/platform are always emitted because they're
            # always known.
            dev_props: dict[str, Any] = {
                "name": dev.name,
                "platform": dev.platform,
                "platform_id": dev.platform_id,
                "role": dev.role,
                "serial": dev.serial or "",
            }
            if dev.mgmt_ip:
                dev_props["mgmt_ip"] = dev.mgmt_ip
            for k, v in dev.platform_metadata.items():
                if v is None:
                    continue
                # Skip empty strings AND empty lists for IP-bearing fields so
                # they don't overwrite previously-good values during a
                # rate-limited discovery cycle.
                if k in (
                    "candidate_ips",
                    "wan1_ip", "wan2_ip",
                    "wan1_public_ip", "wan2_public_ip",
                    "vpn_ip",
                ) and not v:
                    continue
                dev_props[k] = v
            data.nodes.append(GraphNode(
                id=node_id,
                type=NodeType.DEVICE,
                dimensions=[Dimension.PHYSICAL],
                source_adapter=self.instance_id,
                properties=dev_props,
            ))
            net_id = dev.platform_metadata.get("networkId", "")
            if net_id and net_id in network_map:
                data.edges.append(GraphEdge(
                    source_id=node_id,
                    target_id=network_map[net_id],
                    type=EdgeType.LOCATED_AT,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))
                # Track the MX appliance for this network (last one wins if multiple)
                if dev.platform_metadata.get("productType") == "appliance":
                    network_mx_map[net_id] = node_id
            # OWNS_MAC for the device's own MAC address
            dev_mac = dev.platform_metadata.get("mac")
            if dev_mac:
                mac_node_id = f"mac:{dev_mac}"
                data.nodes.append(GraphNode(
                    id=mac_node_id,
                    type=NodeType.MAC_ADDRESS,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=self.instance_id,
                    properties={"mac": dev_mac, "source": self.instance_id},
                ))
                data.edges.append(GraphEdge(
                    source_id=node_id,
                    target_id=mac_node_id,
                    type=EdgeType.OWNS_MAC,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                ))

        # 3. VLANs → VLAN nodes
        try:
            vlans = await self.list_vlans()
        except Exception as exc:
            log.warning("meraki.discover.vlans_failed", error=str(exc), instance=self.instance_id)
            vlans = []

        for vlan in vlans:
            vlan_node_id = f"meraki-vlan:{vlan.platform_id}"
            data.nodes.append(GraphNode(
                id=vlan_node_id,
                type=NodeType.VLAN,
                dimensions=[Dimension.LOGICAL],
                source_adapter=self.instance_id,
                properties={"name": vlan.name, "vid": vlan.vid},
            ))

        # 3b. Prefixes — pulled from every Meraki network we can see.
        #     Sources: appliance VLAN subnets (v4 + v6), static routes,
        #     and switch L3 (SVI) routing interfaces.
        try:
            prefixes = await self.list_prefixes()
        except Exception as exc:
            log.warning("meraki.discover.prefixes_failed",
                        error=str(exc), instance=self.instance_id)
            prefixes = []

        for p in prefixes:
            cidr = p["cidr"]
            prefix_id = f"prefix:{cidr}"
            scope = p.get("scope")
            props: dict[str, Any] = {
                "cidr": cidr,
                "name": p.get("name") or cidr,
                "scope": scope,
                "vlan_id": p.get("vlan_id"),
                "network_id": p.get("network_id"),
                "device_serial": p.get("device_serial"),
                "next_hop": p.get("next_hop"),
            }
            kind = _scope_to_prefix_kind(scope)
            if kind:
                # ``kind`` collapses the granular ingest-time scope onto the
                # small operator-facing taxonomy (vlan_subnet | static_route)
                # so the UI / audit reports / top_problems can switch on a
                # stable discriminator without re-deriving it.
                props["kind"] = kind
            data.nodes.append(GraphNode(
                id=prefix_id,
                type=NodeType.PREFIX,
                dimensions=[Dimension.LOGICAL],
                source_adapter=self.instance_id,
                properties=props,
            ))

        # 4. Physical topology (link layer) → PHYSICAL_LINK edges
        try:
            links = await self.get_org_topology()
        except Exception as exc:
            log.warning("meraki.discover.topology_failed", error=str(exc), instance=self.instance_id)
            links = []

        for link in links:
            src_id = device_node_map.get(link.device_a_platform_id)
            dst_id = device_node_map.get(link.device_b_platform_id)
            if src_id and dst_id:
                data.edges.append(GraphEdge(
                    source_id=src_id,
                    target_id=dst_id,
                    type=EdgeType.PHYSICAL_LINK,
                    dimension=Dimension.PHYSICAL,
                    source_adapter=self.instance_id,
                    properties={
                        "interface_a": normalize_ifname(link.interface_a_name),
                        "interface_b": normalize_ifname(link.interface_b_name),
                        "interface_a_raw": link.interface_a_name,
                        "interface_b_raw": link.interface_b_name,
                        "discovery_proto": link.discovery_proto,
                    },
                ))

        # 5. Per-device LLDP/CDP — discovers links to non-Meraki neighbours
        try:
            switch_serials = [
                dev.platform_id for dev in devices
                if dev.platform_metadata.get("productType") == "switch"
            ]
            # Run LLDP/CDP calls concurrently, limit concurrency to 10
            semaphore = asyncio.Semaphore(10)

            async def _fetch_lldp(serial: str) -> tuple[str, dict]:
                async with semaphore:
                    return serial, await self.get_device_lldp_cdp(serial)

            lldp_results = await asyncio.gather(
                *[_fetch_lldp(s) for s in switch_serials],
                return_exceptions=True,
            )

            # Sort the walk results by serial so the per-device emission
            # loop processes Meraki↔Meraki pairs in a deterministic order
            # cycle-to-cycle.  Combined with the direction-agnostic dedup
            # check below, this guarantees the SAME side's emission wins
            # for any given pair on every ingest — which keeps the edge's
            # ``(source_id, target_id, interface_a, interface_b)``
            # identity stable so the ingest diff-purge is a no-op for
            # already-known links.  Without this, dict iteration order
            # would let cat9k1's emission win one cycle and fabric's
            # emission win the next, flipping the identity key and
            # forcing diff-purge to delete-then-recreate the edge — which
            # is the exact stale-edge window the MAC correlator's
            # ``NOT EXISTS`` guard misreads as "no LLDP", causing
            # spurious mac_correlation duplicates to be MERGEd.
            lldp_results = sorted(
                (item for item in lldp_results
                 if not isinstance(item, Exception)),
                key=lambda it: it[0],
            )

            for item in lldp_results:
                serial, lldp_data = item
                src_node = device_node_map.get(serial, "")
                if not src_node:
                    continue

                for port_id, port_info in lldp_data.get("ports", {}).items():
                    iface_node_id = f"meraki-if:{serial}:{port_id}"
                    # Ensure interface node exists
                    if not any(n.id == iface_node_id for n in data.nodes):
                        data.nodes.append(GraphNode(
                            id=iface_node_id,
                            type=NodeType.INTERFACE,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={
                                "name": f"Port {port_id}",
                                "device_id": serial,
                                "port_id": port_id,
                            },
                        ))
                        data.edges.append(GraphEdge(
                            source_id=src_node,
                            target_id=iface_node_id,
                            type=EdgeType.HAS_INTERFACE,
                            dimension=Dimension.PHYSICAL,
                            source_adapter=self.instance_id,
                        ))

                    # Prefer LLDP over CDP for neighbor info
                    neighbor_info = port_info.get("lldp") or port_info.get("cdp") or {}
                    if not neighbor_info:
                        continue

                    # Neighbor may be identified by chassis ID (MAC), system name, or deviceId
                    neighbor_chassis = _norm_mac(
                        neighbor_info.get("chassisId") or neighbor_info.get("address")
                    )
                    neighbor_name = (
                        neighbor_info.get("systemName")
                        or neighbor_info.get("deviceId", "")
                    ).split(".")[0]  # strip domain suffix from CDP device ID

                    neighbor_serial = None
                    # Check if the neighbor is a known Meraki device by MAC
                    for dev in devices:
                        if dev.platform_metadata.get("mac") == neighbor_chassis:
                            neighbor_serial = dev.platform_id
                            break

                    raw_a = f"Port {port_id}"
                    raw_b = neighbor_info.get("portId", "")
                    proto = "lldp" if "lldp" in port_info else "cdp"

                    if neighbor_serial and neighbor_serial in device_node_map:
                        # Meraki-to-Meraki link not already covered by linkLayer
                        # OR by the *other* end's per-device LLDP/CDP walk.
                        #
                        # The check is DIRECTION-AGNOSTIC on purpose: when
                        # both ends of a cable are Meraki switches, we walk
                        # BOTH of them and each end reports the cable from
                        # its own side.  Using a directed check
                        # (``source == src AND target == dst``) misses the
                        # reverse-direction duplicate, and even after
                        # ``_canonicalize_undirected_edges`` flips one of
                        # them to the lex-min direction the two reports end
                        # up with different ``(interface_a, interface_b)``
                        # tuples (because each side names its OWN port with
                        # the local CLI convention and the FAR port with
                        # whatever LLDP/CDP put on the wire) — so MERGE
                        # creates two distinct edges that no dedup pass can
                        # collapse (union-find finds no shared interface
                        # label).  Stop the duplicate at the source.
                        dst_node = device_node_map[neighbor_serial]
                        endpoints = {src_node, dst_node}
                        existing = any(
                            e.type == EdgeType.PHYSICAL_LINK
                            and {e.source_id, e.target_id} == endpoints
                            for e in data.edges
                        )
                        if not existing:
                            data.edges.append(GraphEdge(
                                source_id=src_node,
                                target_id=dst_node,
                                type=EdgeType.PHYSICAL_LINK,
                                dimension=Dimension.PHYSICAL,
                                source_adapter=self.instance_id,
                                properties={
                                    "interface_a": normalize_ifname(raw_a),
                                    "interface_b": normalize_ifname(raw_b),
                                    "interface_a_raw": raw_a,
                                    "interface_b_raw": raw_b,
                                    "discovery_proto": proto,
                                },
                            ))
                    elif neighbor_name:
                        # Non-Meraki neighbor with a discoverable hostname.
                        # Create a discovery-only Device stub so it appears in
                        # inventory with all the context LLDP/CDP gives us
                        # (mgmt IP, model, OS version). The graph correlator
                        # will later merge this stub with the real Device node
                        # if/when another adapter discovers the same hostname.
                        stub_id = f"lldp-neighbor:{neighbor_name}"

                        # CDP frequently includes the device IP and platform
                        # string; LLDP gives systemDescription and a chassis
                        # MAC. Capture whichever fields are present.
                        cdp_info = port_info.get("cdp") or {}
                        lldp_info = port_info.get("lldp") or {}
                        mgmt_addr = (
                            lldp_info.get("managementAddress")
                            or cdp_info.get("managementAddress")
                            or cdp_info.get("address", "")
                        )
                        platform = cdp_info.get("platform", "")
                        sys_descr = (
                            lldp_info.get("systemDescription")
                            or cdp_info.get("version", "")
                        )

                        if not any(n.id == stub_id for n in data.nodes):
                            stub_props: dict[str, Any] = {
                                "name": neighbor_name,
                                "platform": "cisco" if platform else "unknown",
                                "role": "other",
                                "stub": True,
                                # Flag this clearly so the inventory UI can
                                # surface it as an LLDP/CDP-only entry rather
                                # than as a real platform-managed device.
                                "discovered_via": proto,
                                "discovered_by": self.instance_id,
                            }
                            if mgmt_addr:
                                stub_props["mgmt_ip"] = mgmt_addr
                                stub_props["candidate_ips"] = [mgmt_addr]
                            if platform:
                                stub_props["model"] = platform
                            if sys_descr:
                                stub_props["sys_descr"] = sys_descr[:240]
                            if neighbor_chassis:
                                stub_props["chassis_mac"] = neighbor_chassis
                            data.nodes.append(GraphNode(
                                id=stub_id,
                                type=NodeType.DEVICE,
                                dimensions=[Dimension.PHYSICAL],
                                source_adapter=self.instance_id,
                                properties=stub_props,
                            ))
                        data.edges.append(GraphEdge(
                            source_id=src_node,
                            target_id=stub_id,
                            type=EdgeType.PHYSICAL_LINK,
                            dimension=Dimension.PHYSICAL,
                            source_adapter=self.instance_id,
                            properties={
                                "interface_a": normalize_ifname(raw_a),
                                "interface_b": normalize_ifname(raw_b),
                                "interface_a_raw": raw_a,
                                "interface_b_raw": raw_b,
                                "discovery_proto": proto,
                            },
                        ))
                    elif neighbor_chassis:
                        # Neighbor with a MAC but no hostname — keep the legacy
                        # MAC-only node so correlation can still bind it later.
                        mac_id = f"mac:{neighbor_chassis}"
                        if not any(n.id == mac_id for n in data.nodes):
                            data.nodes.append(GraphNode(
                                id=mac_id,
                                type=NodeType.MAC_ADDRESS,
                                dimensions=[Dimension.PHYSICAL],
                                source_adapter=self.instance_id,
                                properties={
                                    "mac": neighbor_chassis,
                                    "hostname": neighbor_name,
                                    "source": self.instance_id,
                                    "neighbor_port": neighbor_info.get("portId", ""),
                                },
                            ))
                        data.edges.append(GraphEdge(
                            source_id=iface_node_id,
                            target_id=mac_id,
                            type=EdgeType.LEARNED_MAC,
                            dimension=Dimension.PHYSICAL,
                            source_adapter=self.instance_id,
                            properties={
                                "discovery_proto": proto,
                            },
                        ))

        except Exception as exc:
            log.warning("meraki.discover.lldp_failed", error=str(exc), instance=self.instance_id)

        # 5c. Per-switch port statuses → enrich Interface nodes with
        #     oper_status / util / errors / health_score so the existing
        #     _enrich_physical_links_with_health correlator can color
        #     Meraki↔Meraki cables the same way it colors SNMP-polled
        #     cables.  Without this, Meraki devices have zero per-port
        #     telemetry in the graph and every Meraki cable renders as
        #     "unknown health" (neutral grey).
        #
        #     Note: this also CREATES Interface nodes for ports we
        #     haven't already created from LLDP (steps 5/5a), so EVERY
        #     switch port a Meraki device reports gets a node with
        #     current state.  Critical for hover-tooltip "where is this
        #     cable plugged in" lookups.
        try:
            switch_serials_for_ports = [
                dev.platform_id for dev in devices
                if dev.platform_metadata.get("productType") == "switch"
            ]
            port_sem = asyncio.Semaphore(8)

            async def _fetch_port_statuses(s: str) -> tuple[str, dict]:
                async with port_sem:
                    return s, await self.get_switch_port_statuses(s)

            port_results = await asyncio.gather(
                *[_fetch_port_statuses(s) for s in switch_serials_for_ports],
                return_exceptions=True,
            )
            # Build a quick set of existing interface IDs so we don't
            # double-emit nodes for ports that step 5 already created
            # from LLDP/CDP discovery.  We MERGE properties either way
            # via _enrich_physical_links_with_health, but emitting two
            # GraphNode records for the same id confuses the diff-purge
            # bookkeeping.
            existing_iface_ids: set[str] = {
                n.id for n in data.nodes if n.type == NodeType.INTERFACE
            }
            port_iface_count = 0
            port_health_count = 0
            for item in port_results:
                if isinstance(item, Exception):
                    continue
                serial, port_map = item
                if not port_map:
                    continue
                dev_node = device_node_map.get(serial)
                if not dev_node:
                    continue
                for port_id, stats in port_map.items():
                    iface_id = f"meraki-if:{serial}:{port_id}"
                    # Only emit non-None values so unset fields don't
                    # clobber data the LLDP / SNMP path already wrote.
                    props: dict[str, Any] = {
                        "name": f"Port {port_id}",
                        "device_id": serial,
                        "port_id": port_id,
                    }
                    for k, v in stats.items():
                        if v is None:
                            continue
                        props[k] = v
                    if iface_id not in existing_iface_ids:
                        existing_iface_ids.add(iface_id)
                        data.nodes.append(GraphNode(
                            id=iface_id,
                            type=NodeType.INTERFACE,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties=props,
                        ))
                        data.edges.append(GraphEdge(
                            source_id=dev_node,
                            target_id=iface_id,
                            type=EdgeType.HAS_INTERFACE,
                            dimension=Dimension.PHYSICAL,
                            source_adapter=self.instance_id,
                        ))
                        port_iface_count += 1
                    else:
                        # Re-emit with the merged property bag so the
                        # ingest MERGE updates the existing Interface
                        # node with stats (no separate HAS_INTERFACE
                        # since it already exists).
                        data.nodes.append(GraphNode(
                            id=iface_id,
                            type=NodeType.INTERFACE,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties=props,
                        ))
                    if stats.get("health_score") is not None:
                        port_health_count += 1
            log.info("meraki.discover.port_statuses",
                     instance=self.instance_id,
                     switches=len(switch_serials_for_ports),
                     ifaces_created=port_iface_count,
                     ports_with_health=port_health_count)
        except Exception as exc:
            log.warning("meraki.discover.port_statuses_failed",
                        error=str(exc), instance=self.instance_id)

        # 5d. MX appliance uplink statuses → stamp per-uplink oper_status
        #     onto the MX Device node as ``mx_wan1_status`` /
        #     ``mx_wan2_status`` etc. so _enrich_wan_uplinks_with_health
        #     can read more granular state than the device-wide cloud
        #     status (which says "online" even when one of two uplinks
        #     is dead).
        try:
            uplink_map = await self.get_appliance_uplink_statuses()
            mx_with_uplinks = 0
            for serial, per_uplink in uplink_map.items():
                dev_node = device_node_map.get(serial)
                if not dev_node:
                    continue
                # Re-emit the MX Device node with uplink-derived props
                # patched in.  Find the existing node and update it
                # in-place so we don't lose any existing properties.
                for n in data.nodes:
                    if n.id == dev_node and n.type == NodeType.DEVICE:
                        for slot, info in per_uplink.items():
                            # Sentinel key carrying device-level fields
                            # (not a real uplink slot).
                            if slot == "__device__":
                                if info.get("last_reported_at_ms") is not None:
                                    n.properties["meraki_last_reported_at"] = (
                                        info["last_reported_at_ms"]
                                    )
                                if info.get("last_reported_at_iso"):
                                    n.properties["meraki_last_reported_at_iso"] = (
                                        info["last_reported_at_iso"]
                                    )
                                continue
                            # ``slot`` is "wan1" / "wan2" / "cellular"
                            n.properties[f"mx_{slot}_status"] = info.get("oper_status")
                            n.properties[f"mx_{slot}_status_raw"] = info.get("status_raw")
                            if info.get("ip"):
                                n.properties[f"mx_{slot}_ip"] = info["ip"]
                            if info.get("public_ip"):
                                n.properties[f"mx_{slot}_public_ip"] = info["public_ip"]
                            if info.get("gateway"):
                                n.properties[f"mx_{slot}_gateway"] = info["gateway"]
                        mx_with_uplinks += 1
                        break
            log.info("meraki.discover.uplink_statuses",
                     instance=self.instance_id,
                     mx_count=mx_with_uplinks)
        except Exception as exc:
            log.warning("meraki.discover.uplink_statuses_failed",
                        error=str(exc), instance=self.instance_id)

        # 6. Org clients → MACAddress + ARPEntry nodes + LEARNED_MAC edges
        #
        # We iterate ALL clients (wired AND wireless) so that IP↔MAC ARP
        # bindings are captured even for wireless endpoints.  LEARNED_MAC
        # switch-port edges are only created for wired clients with a
        # known switchport.
        existing_mac_ids: set[str] = {n.id for n in data.nodes if n.type == NodeType.MAC_ADDRESS}
        existing_arp_ids: set[str] = {n.id for n in data.nodes if n.type == NodeType.ARP_ENTRY}
        try:
            clients = await self.get_org_clients(timespan=3600)
            for client_entry in clients:
                raw_mac = client_entry.get("mac", "")
                mac = _norm_mac(raw_mac)
                if not mac:
                    continue

                switch_serial = client_entry.get("recentDeviceSerial", "")
                switchport_raw = client_entry.get("switchport")
                switchport = str(switchport_raw) if switchport_raw is not None else ""
                connection_type = client_entry.get("recentDeviceConnection", "")

                # IP: Meraki returns null for pure-L2 switch clients; try both fields
                client_ip: str = client_entry.get("ip") or client_entry.get("ip6") or ""

                # ── MAC node (created for all clients) ────────────────────
                mac_node_id = f"mac:{mac}"
                if mac_node_id not in existing_mac_ids:
                    existing_mac_ids.add(mac_node_id)
                    mac_props: dict = {
                        "mac": mac,
                        "description": client_entry.get("description", ""),
                        "vlan": client_entry.get("vlan"),
                        "source": self.instance_id,
                    }
                    if client_ip:
                        mac_props["ip"] = client_ip
                    data.nodes.append(GraphNode(
                        id=mac_node_id,
                        type=NodeType.MAC_ADDRESS,
                        dimensions=[Dimension.PHYSICAL],
                        source_adapter=self.instance_id,
                        properties=mac_props,
                    ))

                # ── ARP node (when IP is known, regardless of wired/wireless)
                if client_ip:
                    arp_node_id = f"arp:{client_ip}"
                    if arp_node_id not in existing_arp_ids:
                        existing_arp_ids.add(arp_node_id)
                        data.nodes.append(GraphNode(
                            id=arp_node_id,
                            type=NodeType.ARP_ENTRY,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={
                                "ip": client_ip,
                                "mac": mac,
                                "vlan": client_entry.get("vlan"),
                                "connection_type": connection_type,
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

                # ── LEARNED_MAC edge: wired clients with a known switchport only
                if connection_type != "Wired" and not switchport:
                    continue  # skip LEARNED_MAC for wireless / AP-connected clients

                if switch_serial and switchport and switch_serial in device_node_map:
                    iface_node_id = f"meraki-if:{switch_serial}:{switchport}"
                    if not any(n.id == iface_node_id for n in data.nodes):
                        data.nodes.append(GraphNode(
                            id=iface_node_id,
                            type=NodeType.INTERFACE,
                            dimensions=[Dimension.PHYSICAL],
                            source_adapter=self.instance_id,
                            properties={
                                "name": f"Port {switchport}",
                                "device_id": switch_serial,
                                "port_id": switchport,
                            },
                        ))
                        data.edges.append(GraphEdge(
                            source_id=device_node_map[switch_serial],
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
                        properties={"vlan": client_entry.get("vlan")},
                    ))
        except Exception as exc:
            log.warning("meraki.discover.clients_failed", error=str(exc), instance=self.instance_id)

        # 7. AutoVPN topology → SDWAN_TUNNEL edges between MX appliances.
        # We connect the MX device (appliance) for each network rather than the
        # PlatformSite container, so tunnels render as device-to-device links
        # inside (or between) their site containers.
        try:
            vpn_data = await self.get_vpn_topology()
            vpn_statuses = vpn_data.get("statuses", [])

            for entry in vpn_statuses:
                net_id = entry.get("networkId", "")
                # Prefer the MX appliance; fall back to the PlatformSite node
                src_node = network_mx_map.get(net_id) or network_map.get(net_id, "")
                if not src_node:
                    continue
                vpn_mode = entry.get("vpnMode", "spoke")
                for peer in entry.get("merakiVpnPeers", []):
                    peer_net_id = peer.get("networkId", "")
                    dst_node = network_mx_map.get(peer_net_id) or network_map.get(peer_net_id, "")
                    if not dst_node or src_node == dst_node:
                        continue
                    reachability = peer.get("reachability", "unknown")
                    # Avoid duplicate bidirectional edges
                    if any(e.source_id == dst_node and e.target_id == src_node
                           and e.type == EdgeType.SDWAN_TUNNEL for e in data.edges):
                        continue
                    edge_props: dict[str, Any] = {
                        "vpn_mode": vpn_mode,
                        "reachability": reachability,
                        "tunnel_type": "meraki_autovpn",
                    }
                    # Promote Meraki's per-peer reachability onto the canonical
                    # ``oper_status`` so the history correlator tracks tunnel
                    # transitions / flaps and the top_problems link_down check
                    # surfaces SD-WAN-only outages.  ``unknown`` peers leave
                    # oper_status unset (history correlator filters NULLs) so
                    # we never record bogus state transitions for tunnels the
                    # dashboard has no opinion on.
                    oper = _reachability_to_oper_status(reachability)
                    if oper is not None:
                        edge_props["oper_status"] = oper
                    data.edges.append(GraphEdge(
                        source_id=src_node,
                        target_id=dst_node,
                        type=EdgeType.SDWAN_TUNNEL,
                        dimension=Dimension.SDWAN,
                        source_adapter=self.instance_id,
                        properties=edge_props,
                    ))
        except Exception as exc:
            log.warning("meraki.discover.vpn_failed", error=str(exc), instance=self.instance_id)

        # ── STP — per-network switch STP priority (identifies root bridges) ───
        # Meraki doesn't expose the full STP topology; we use priority data to mark
        # likely root bridges (lowest priority = root) per network (broadcast domain).
        try:
            # Build network_id → [serial] map from the already-processed device list
            net_switch_serials: dict[str, list[str]] = {}
            for dev in devices:
                net_id = dev.platform_metadata.get("networkId", "")
                product_type = dev.platform_metadata.get("productType", "")
                if net_id and product_type == "switch":
                    net_switch_serials.setdefault(net_id, []).append(dev.platform_id)

            log.debug("meraki.discover.stp_networks", count=len(net_switch_serials), instance=self.instance_id)
            async with _client(self._api_key) as stp_client:
                for net_id, switch_serials in net_switch_serials.items():
                    if not switch_serials:
                        continue
                    try:
                        resp = await stp_client.get(f"{self._base_url}/networks/{net_id}/switch/stp")
                        if not resp.is_success:
                            continue
                        stp_cfg = resp.json()
                        if not stp_cfg.get("rstpEnabled", True):
                            continue
                        overrides = stp_cfg.get("overridesBySwitch", [])
                        priority_map: dict[str, int] = {}
                        for ov in overrides:
                            prio = ov.get("stpPriority", 32768)
                            for sw in ov.get("switches", []):
                                sw_serial = sw.get("serial", "")
                                if sw_serial:
                                    priority_map[sw_serial] = prio

                        root_serial = min(switch_serials, key=lambda s: priority_map.get(s, 32768))

                        domain_id = f"stp:{self.instance_id}:{net_id}"
                        net_name = next(
                            (n.get("name", net_id) for n in networks if n["id"] == net_id), net_id
                        )
                        data.nodes.append(GraphNode(
                            id=domain_id,
                            type=NodeType.STP_DOMAIN,
                            dimensions=[Dimension.STP],
                            source_adapter=self.instance_id,
                            properties={
                                "name": f"STP {net_name}",
                                "network_id": net_id,
                                "rstp_enabled": True,
                                "source": self.instance_id,
                            },
                        ))

                        for sw_serial in switch_serials:
                            sw_node_id = device_node_map.get(sw_serial, "")
                            if not sw_node_id:
                                continue
                            is_root = (sw_serial == root_serial)
                            data.edges.append(GraphEdge(
                                source_id=sw_node_id,
                                target_id=domain_id,
                                type=EdgeType.STP_ROOT if is_root else EdgeType.STP_MEMBER,
                                dimension=Dimension.STP,
                                source_adapter=self.instance_id,
                                properties={
                                    "stp_priority": priority_map.get(sw_serial, 32768),
                                    "is_root": is_root,
                                    "network_id": net_id,
                                },
                            ))
                    except Exception as exc:
                        log.debug("meraki.discover.stp_network_failed",
                                  net_id=net_id, error=str(exc)[:120], instance=self.instance_id)
        except Exception as exc:
            log.debug("meraki.discover.stp_failed", error=str(exc), instance=self.instance_id)

        log.info("meraki.discover.done", instance=self.instance_id,
                 nodes=len(data.nodes), edges=len(data.edges))
        return data
