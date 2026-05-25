"""SNMP v3 adapter — topology, STP, CAM, ARP, and routing protocol collection.

Phases implemented:
  Phase 1 — System / IF-MIB: sysDescr, sysUpTime, ifName/ifDescr
  Phase 2 — MAC/ARP tables: BRIDGE-MIB CAM, IP-MIB ARP (IPv4+IPv6 NDP)
  Phase 3 — STP: BRIDGE-MIB dot1dStp + RSTP-MIB port roles
  Phase 4 — Neighbors: IETF LLDP-MIB, Cisco CDP-MIB (no stub nodes)
  Phase 5 — L3 topology: OSPF-MIB, BGP4-MIB, CISCO-EIGRP-MIB neighbors
  Phase 6 — IP addresses: ipAddrTable (IPv4) + ipv6AddrTable (IPv6)

Credential resolution order (per device, first match wins):
  netcortex/snmp/device/{name}    → per-device
  netcortex/snmp/adapter/{type}   → per platform type (meraki, catalyst_center …)
  netcortex/snmp/default          → global fallback

Targets are taken from ALL Device nodes in the graph (mgmt_ip field), so no
static IP list is required in the adapter config.

Required: pysnmp >= 7.0 (already in project dependencies).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import struct
import unicodedata
from typing import Any

import structlog

from netcortex.adapters.base import PlatformAdapter, PlatformProfile
from netcortex.graph.models import (
    Dimension, EdgeType, GraphData, GraphEdge, GraphNode, NodeType,
)
from netcortex.models.device import NormalizedDevice
from netcortex.models.interface import NormalizedInterface
from netcortex.models.topology import NormalizedTopologyLink
from netcortex.models.vlan import NormalizedVLAN
from netcortex.snmp.credentials import (
    SnmpContext, SnmpCredentialResolver, SnmpV2Creds, SnmpV3Creds,
)
from netcortex.util.ifname import normalize_ifname

log = structlog.get_logger(__name__)


async def _detect_outbound_ip() -> str:
    """Best-effort detection of the worker's public outbound IP.

    Used by the Meraki cloud SNMP diagnostic to tell the user exactly
    which IP to add to Dashboard's `peerIps` allow-list.  Falls back to
    "unknown" if the lookup service is unreachable — never raises.
    """
    import httpx
    # Hosted service that just echoes the requester's IP.  No auth.
    # 4s timeout — the diagnostic is best-effort, must not stall the
    # worker if the lookup is slow.
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=4.0, write=2.0, pool=2.0),
        ) as c:
            r = await c.get("https://api.ipify.org")
            if r.status_code == 200:
                ip = r.text.strip()
                if ip:
                    return ip
    except Exception:
        pass
    return "unknown"

# ---------------------------------------------------------------------------
# OID constants (numeric — no MIB files required)
# ---------------------------------------------------------------------------

# System MIB
OID_SYS_DESCR      = "1.3.6.1.2.1.1.1"
OID_SYS_NAME       = "1.3.6.1.2.1.1.5"
OID_SYS_UPTIME     = "1.3.6.1.2.1.1.3"

# IF-MIB
OID_IF_DESCR       = "1.3.6.1.2.1.2.2.1.2"    # ifDescr
OID_IF_NAME        = "1.3.6.1.2.1.31.1.1.1.1"  # ifName (preferred)
OID_IF_ALIAS       = "1.3.6.1.2.1.31.1.1.1.18" # ifAlias (description)
OID_IF_PHYS_ADDR   = "1.3.6.1.2.1.2.2.1.6"     # ifPhysAddress (MAC per iface)
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"     # ifOperStatus
OID_IF_ADMIN_STATUS= "1.3.6.1.2.1.2.2.1.7"     # ifAdminStatus
OID_IF_SPEED       = "1.3.6.1.2.1.31.1.1.1.15" # ifHighSpeed (Mbps)

# BRIDGE-MIB — CAM table
OID_CAM_MAC        = "1.3.6.1.2.1.17.4.3.1.1"  # dot1dTpFdbAddress
OID_CAM_PORT       = "1.3.6.1.2.1.17.4.3.1.2"  # dot1dTpFdbPort
OID_CAM_STATUS     = "1.3.6.1.2.1.17.4.3.1.3"  # dot1dTpFdbStatus (3=learned)

# BRIDGE-MIB — dot1dBase: bridge MAC address, number of ports
OID_DOT1D_BASE_MAC       = "1.3.6.1.2.1.17.1.1"        # dot1dBaseBridgeAddress
# dot1dBasePortIfIndex translates dot1dBasePortNumber → ifIndex. STP MIBs
# (and many bridge MIBs) index by dot1dBasePortNumber, NOT ifIndex, so
# without this lookup table the STP port we read would say "port-181"
# instead of "Ethernet1/46".
OID_DOT1D_BASE_PORT_IF   = "1.3.6.1.2.1.17.1.4.1.2"    # dot1dBasePortIfIndex

# BRIDGE-MIB — STP
OID_DOT1D_STP_PROT_SPEC     = "1.3.6.1.2.1.17.2.1"   # 3=ieee8021d, 4=rstp
OID_DOT1D_STP_PRIORITY       = "1.3.6.1.2.1.17.2.2"
OID_DOT1D_STP_ROOT           = "1.3.6.1.2.1.17.2.5"   # dot1dStpDesignatedRoot (8 bytes)
OID_DOT1D_STP_ROOT_COST      = "1.3.6.1.2.1.17.2.6"
OID_DOT1D_STP_ROOT_PORT      = "1.3.6.1.2.1.17.2.7"
OID_DOT1D_STP_TOPOLOGY_CHGS  = "1.3.6.1.2.1.17.2.10"
OID_DOT1D_STP_PORT_TABLE     = "1.3.6.1.2.1.17.2.15"  # dot1dStpPortTable

# dot1dStpPortEntry column OIDs
OID_STP_PORT_STATE      = "1.3.6.1.2.1.17.2.15.1.3"   # 1=disabled,2=blocking,3=listening,4=learning,5=forwarding,6=broken
OID_STP_PORT_ROLE       = "1.3.6.1.2.1.17.2.15.1.4"   # Cisco: 0=disabled,1=root,2=designated,3=alternate,4=backup,5=boundary (not standard)
OID_STP_PORT_PRIORITY   = "1.3.6.1.2.1.17.2.15.1.5"
OID_STP_PORT_PATH_COST  = "1.3.6.1.2.1.17.2.15.1.7"
OID_STP_PORT_DESIG_ROOT = "1.3.6.1.2.1.17.2.15.1.8"   # dot1dStpPortDesignatedRoot
OID_STP_PORT_DESIG_COST = "1.3.6.1.2.1.17.2.15.1.9"
OID_STP_PORT_DESIG_BRDG = "1.3.6.1.2.1.17.2.15.1.10"
OID_STP_PORT_DESIG_PORT = "1.3.6.1.2.1.17.2.15.1.11"

# RSTP-MIB (IEEE 802.1D-2004) — port roles
OID_RSTP_PORT_ROLE       = "1.3.6.1.2.1.17.6.1.4.1.19.1.3"  # dot1dStpExtPortRoleValue (draft variant)
# Alternative: Cisco RSTP port role (in Cisco PVST instances reported via STP MIB)

# IP-MIB — ARP table
OID_ARP_PHYS   = "1.3.6.1.2.1.4.22.1.2"   # ipNetToMediaPhysAddress
OID_ARP_NET    = "1.3.6.1.2.1.4.22.1.3"   # ipNetToMediaNetAddress
OID_ARP_TYPE   = "1.3.6.1.2.1.4.22.1.4"   # ipNetToMediaType (3=dynamic)

# IETF LLDP-MIB
OID_LLDP_LOC_SYS_NAME    = "1.0.8802.1.1.2.1.3.3"
OID_LLDP_REM_CHASSIS_ID  = "1.0.8802.1.1.2.1.4.1.1.5"  # lldpRemChassisId
OID_LLDP_REM_PORTID      = "1.0.8802.1.1.2.1.4.1.1.7"
OID_LLDP_REM_PORTDESC    = "1.0.8802.1.1.2.1.4.1.1.8"
OID_LLDP_REM_SYSNAME     = "1.0.8802.1.1.2.1.4.1.1.9"
OID_LLDP_REM_MGMT        = "1.0.8802.1.1.2.1.4.2.1.4"
# lldpRemChassisIdSubtype tells us how to interpret lldpRemChassisId.
# Value 4 = macAddress (the only subtype we can resolve directly against
# our MAC inventory). Other subtypes (chassisComponent, ifAlias, ifName,
# local, networkAddress, portComponent) require fuzzier matching.
OID_LLDP_REM_CHASSIS_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.4"

# Cisco CDP-MIB
OID_CDP_NEIGHBOR_NAME     = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"  # cdpCacheDeviceId
OID_CDP_NEIGHBOR_ADDR     = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"  # cdpCacheAddress
OID_CDP_NEIGHBOR_PORT     = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"  # cdpCacheDevicePort
OID_CDP_NEIGHBOR_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"  # cdpCachePlatform
# cdpCacheAddressType (1=IP). cdpCacheAddress is a 4-byte string for IPv4
# or 16-byte for IPv6 — we decode it explicitly because net-snmp prints
# the raw octets as Hex-STRING for non-DISPLAY-HINT types.
OID_CDP_NEIGHBOR_ADDR_TYPE = "1.3.6.1.4.1.9.9.23.1.2.1.1.3"

# CISCO-VTP-MIB — VLAN inventory (works on IOS/IOS-XE/NX-OS when SNMP view permits).
# Index format: <vtpDomainIdx>.<vlanId>
OID_VTP_VLAN_NAME    = "1.3.6.1.4.1.9.9.46.1.3.1.1.4"  # vtpVlanName
OID_VTP_VLAN_STATE   = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"  # vtpVlanState (1=operational)
OID_VTP_VLAN_TYPE    = "1.3.6.1.4.1.9.9.46.1.3.1.1.3"  # vtpVlanType (1=ethernet)

# CISCO-VLAN-MEMBERSHIP-MIB — access-port VLAN per interface (ifIndex).
OID_VMVLAN           = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"  # vmVlan

# CISCO-VTP-MIB / vlanTrunkPort* — trunk membership per ifIndex.
# vlanTrunkPortDynamicStatus tells us whether the port is actually trunking
# (1=trunking, 2=notTrunking) so we can disambiguate access vs trunk modes
# even when both the access-VLAN and trunk-bitmap are present.
OID_VLAN_TRUNK_STATUS    = "1.3.6.1.4.1.9.9.46.1.6.1.1.14"  # vlanTrunkPortDynamicStatus
OID_VLAN_TRUNK_NATIVE    = "1.3.6.1.4.1.9.9.46.1.6.1.1.5"   # vlanTrunkPortNativeVlan
# vlanTrunkPortVlansEnabled: bitmap of allowed VLANs.
#   .4  → VLANs 1–1023   (1024 bits)
#   .17 → VLANs 1025–2047 (vlanTrunkPortVlansEnabled2k)
#   .18 → VLANs 2049–3071 (vlanTrunkPortVlansEnabled3k)
#   .19 → VLANs 3073–4095 (vlanTrunkPortVlansEnabled4k)
OID_VLAN_TRUNK_ENABLED   = "1.3.6.1.4.1.9.9.46.1.6.1.1.4"
OID_VLAN_TRUNK_ENABLED2K = "1.3.6.1.4.1.9.9.46.1.6.1.1.17"
OID_VLAN_TRUNK_ENABLED3K = "1.3.6.1.4.1.9.9.46.1.6.1.1.18"
OID_VLAN_TRUNK_ENABLED4K = "1.3.6.1.4.1.9.9.46.1.6.1.1.19"

# IEEE Q-BRIDGE-MIB — vendor-neutral fallback for VLAN egress per port.
# dot1qVlanCurrentEgressPorts gives a port bitmap per (TimeMark, VlanIndex).
# We need to transpose (vlan → ports) into (port → vlans) at the device side.
OID_DOT1Q_VLAN_EGRESS    = "1.3.6.1.2.1.17.7.1.4.2.1.4"  # dot1qVlanCurrentEgressPorts

# Q-BRIDGE-MIB — standard VLAN table (IETF, used by everything that's
# strict standards-only; some Cisco views permit this where VTP-MIB
# isn't visible).
OID_Q_VLAN_STATIC_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"  # dot1qVlanStaticName

# OSPF-MIB (RFC 1850 / RFC 4750)
OID_OSPF_ROUTER_ID      = "1.3.6.1.2.1.14.1.1"    # ospfRouterId (scalar)
OID_OSPF_ADMIN_STAT     = "1.3.6.1.2.1.14.1.2"    # ospfAdminStat (1=enabled)
OID_OSPF_NBR_IP         = "1.3.6.1.2.1.14.10.1.1" # ospfNbrIpAddr
OID_OSPF_NBR_RTR_ID     = "1.3.6.1.2.1.14.10.1.3" # ospfNbrRtrId
OID_OSPF_NBR_STATE      = "1.3.6.1.2.1.14.10.1.6" # ospfNbrState
OID_OSPF_NBR_PRIO       = "1.3.6.1.2.1.14.10.1.5" # ospfNbrPriority
OID_OSPF_IF_AREA        = "1.3.6.1.2.1.14.7.1.2"  # ospfIfAreaId (per-interface area)

# BGP4-MIB (RFC 1657)
OID_BGP_LOCAL_AS        = "1.3.6.1.2.1.15.2"       # bgpLocalAs (scalar)
OID_BGP_PEER_STATE      = "1.3.6.1.2.1.15.3.1.2"   # bgpPeerState
OID_BGP_PEER_REMOTE_AS  = "1.3.6.1.2.1.15.3.1.9"   # bgpPeerRemoteAs
OID_BGP_PEER_REMOTE_ADDR= "1.3.6.1.2.1.15.3.1.7"   # bgpPeerRemoteAddr
OID_BGP_PEER_LOCAL_ADDR = "1.3.6.1.2.1.15.3.1.5"   # bgpPeerLocalAddr
OID_BGP_PEER_IN_UPDATES = "1.3.6.1.2.1.15.3.1.12"  # bgpPeerInUpdates
OID_BGP_PEER_OUT_UPDATES= "1.3.6.1.2.1.15.3.1.13"  # bgpPeerOutUpdates

# CISCO-EIGRP-MIB
OID_EIGRP_AS            = "1.3.6.1.4.1.9.9.449.1.1.1.1.1" # cEigrpAsNumber
OID_EIGRP_NBR_ADDR      = "1.3.6.1.4.1.9.9.449.1.2.1.1.2" # cEigrpNbrAddr
OID_EIGRP_NBR_IF_IDX    = "1.3.6.1.4.1.9.9.449.1.2.1.1.1" # cEigrpNbrIfIndex
OID_EIGRP_NBR_HOLDTIME  = "1.3.6.1.4.1.9.9.449.1.2.1.1.7" # cEigrpNbrHoldTime (uptime proxy)

# IP address tables — legacy IPv4 only (ipAddrTable, RFC 1213)
OID_IP_ADDR_ADDR_V4     = "1.3.6.1.2.1.4.20.1.1"  # ipAdEntAddr
OID_IP_ADDR_IF_V4       = "1.3.6.1.2.1.4.20.1.2"  # ipAdEntIfIndex
OID_IP_ADDR_MASK_V4     = "1.3.6.1.2.1.4.20.1.3"  # ipAdEntNetMask

# Modern unified IPv4/IPv6 (ipAddressTable, RFC 4293)
# Row index encoding: {InetAddressType}.{addr-len}.{addr-bytes}
#   InetAddressType: 1 = ipv4, 2 = ipv6, 4 = ipv4z, 5 = ipv6z
#   addr-len: 4 for v4, 16 for v6 (preceded as length byte in some impls)
OID_IP_ADDRESS_IF_IDX   = "1.3.6.1.2.1.4.34.1.3"  # ipAddressIfIndex
OID_IP_ADDRESS_TYPE     = "1.3.6.1.2.1.4.34.1.4"  # ipAddressType  (1=unicast,2=anycast,3=broadcast)
OID_IP_ADDRESS_PREFIX   = "1.3.6.1.2.1.4.34.1.5"  # ipAddressPrefix (RowPointer)
# ipAddressPrefix value is an OID; the LAST integer of that OID is the prefix length.

# IP-MIB (RFC 4293) — unified IPv4+IPv6 NDP/ARP
OID_NET_TO_PHYS_PHYS    = "1.3.6.1.2.1.4.35.1.4"  # ipNetToPhysicalPhysAddress
OID_NET_TO_PHYS_TYPE    = "1.3.6.1.2.1.4.35.1.6"  # ipNetToPhysicalType
OID_NET_TO_PHYS_STATE   = "1.3.6.1.2.1.4.35.1.7"  # ipNetToPhysicalState

# OSPF neighbor state decode
OSPF_NBR_STATES = {
    "1": "down", "2": "attempt", "3": "init", "4": "twoWay",
    "5": "exchangeStart", "6": "exchange", "7": "loading", "8": "full",
}

# BGP peer state decode
BGP_PEER_STATES = {
    "1": "idle", "2": "connect", "3": "active",
    "4": "openSent", "5": "openConfirm", "6": "established",
}

# ---------------------------------------------------------------------------
# State and role decodings
# ---------------------------------------------------------------------------

STP_PORT_STATES = {
    "1": "disabled",
    "2": "blocking",
    "3": "listening",
    "4": "learning",
    "5": "forwarding",
    "6": "broken",
}

STP_PORT_ROLES_CISCO = {
    "0": "disabled",
    "1": "root",
    "2": "designated",
    "3": "alternate",
    "4": "backup",
    "5": "boundary",
}

STP_PORT_ROLES_IEEE = {
    "1": "disabled",
    "2": "root",
    "3": "designated",
    "4": "alternate",
    "5": "backup",
}

# ---------------------------------------------------------------------------
# VLAN-bitmap helper
# ---------------------------------------------------------------------------


def _decode_vlan_bitmap(val: Any, offset: int = 0) -> list[int]:
    """Convert a CISCO-VTP-MIB / Q-BRIDGE-MIB VLAN bitmap to a list of VIDs.

    The bitmap is an OctetString where bit ``n`` (MSB first, byte 0 is
    the first byte) represents VLAN ``offset + n``. So for
    ``vlanTrunkPortVlansEnabled`` (offset 0) the byte/bit pair (0, 0)
    is VLAN 0, (0, 7) is VLAN 7, (1, 0) is VLAN 8, etc. For the 2k/3k/4k
    extension OIDs the offsets are 1024 / 2048 / 3072 respectively.

    We accept either ``bytes`` (preferred — net-snmp Hex-STRING) or a
    string like ``"FF FF FF ..."`` and return the **sorted ascending**
    list of VIDs whose bit is set. VLAN 0 and 4095 are filtered out
    because they are reserved in 802.1Q.
    """
    if val is None:
        return []
    raw: bytes
    if isinstance(val, (bytes, bytearray)):
        raw = bytes(val)
    else:
        try:
            raw = bytes.fromhex(str(val).replace(" ", "").replace(":", ""))
        except ValueError:
            return []
    vids: list[int] = []
    for byte_idx, byte_val in enumerate(raw):
        if not byte_val:
            continue
        for bit_idx in range(8):
            if byte_val & (0x80 >> bit_idx):
                vid = offset + byte_idx * 8 + bit_idx
                if 0 < vid < 4095:
                    vids.append(vid)
    return vids


# ---------------------------------------------------------------------------
# MAC normalisation helper
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[^0-9a-fA-F]")


def _norm_mac(raw: Any) -> str | None:
    """Return lowercase colon-separated MAC, or None if input is unusable."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) != 6:
            return None
        return ":".join(f"{b:02x}" for b in raw)
    s = str(raw)
    # OctetString may appear as "0x0a:1b:2c:..." — strip leading 0x
    s = s.removeprefix("0x")
    digits = _STRIP_RE.sub("", s)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i+2] for i in range(0, 12, 2)).lower()


def _mac_from_bridge_id(raw: Any) -> str | None:
    """Extract the MAC portion from an 8-byte bridge ID (drop first 2 bytes)."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) >= 8:
            return _norm_mac(bytes(raw[2:8]))
    s = str(raw).removeprefix("0x")
    digits = _STRIP_RE.sub("", s)
    if len(digits) >= 16:
        return _norm_mac(digits[4:16])   # skip first 4 hex digits (2 bytes)
    return None


# ---------------------------------------------------------------------------
# SNMP value decoding helpers
# ---------------------------------------------------------------------------

def _decode_display_str(val: Any) -> str:
    """Safely decode an SNMP DisplayString / OctetString to a Python str.

    pysnmp returns OctetString/DisplayString values whose `str()` may contain
    raw bytes when the device sends non-UTF-8 data.  This helper decodes
    the underlying bytes safely, stripping non-printable characters.
    """
    if val is None:
        return ""
    # Try to get raw bytes from pyasn1 object
    raw_bytes: bytes | None = None
    try:
        raw_bytes = bytes(val)
    except Exception:
        pass
    if raw_bytes is not None:
        try:
            s = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            s = raw_bytes.decode("ascii", errors="replace")
        # Strip non-printable characters (keep printable + common whitespace)
        return "".join(
            ch for ch in s
            if unicodedata.category(ch)[0] not in ("C",) or ch in ("\t", "\n", " ")
        ).strip()
    return str(val).strip()


def _decode_port_id(val: Any) -> str:
    """Decode an LLDP/CDP remote port-ID value.

    LLDP's ``lldpRemPortId`` (and CDP's ``cdpCacheDevicePort``) can be:
      * a printable interface name string (``Gi1/0/1``, ``Eth1/8``)
      * a 6-byte MAC address (when port-ID subtype is ``macAddress(3)``;
        net-snmp delivers this as a ``Hex-STRING:`` → ``bytes``)
      * an interface ifIndex integer (``portNumber(7)`` subtype)
      * an arbitrary OctetString

    We never decoded ``bytes`` properly before, so MAC-subtype port IDs
    surfaced in the UI as ``b'x\\x85...'`` Python repr. This helper:
      - formats 6-byte values as ``aa:bb:cc:dd:ee:ff``
      - decodes other ``bytes`` to UTF-8 (replacing non-printable bytes)
      - returns string values unchanged (already a port name)
    """
    if val is None:
        return ""
    if isinstance(val, (bytes, bytearray)):
        b = bytes(val)
        if len(b) == 6:
            return ":".join(f"{x:02x}" for x in b)
        # Try as text — falls back to a stripped repr if non-printable.
        try:
            s = b.decode("utf-8")
            if s and all(ch.isprintable() for ch in s):
                return s
        except UnicodeDecodeError:
            pass
        # Hex-string fallback so the UI sees something meaningful.
        return ":".join(f"{x:02x}" for x in b)
    return str(val).strip()


def _decode_ip_val(val: Any) -> str:
    """Convert an SNMP IpAddress value to a dotted-decimal string.

    Handles three forms that pysnmp may return:
      - pyasn1 IpAddress object (str() gives dotted-decimal)
      - raw bytes (4 bytes → dotted-decimal)
      - decimal integer string like "3232235777" → struct unpack → dotted-decimal
    """
    if val is None:
        return ""
    # Try bytes first
    try:
        b = bytes(val)
        if len(b) == 4:
            return ".".join(str(x) for x in b)
    except Exception:
        pass
    s = str(val).strip()
    # Already dotted-decimal?
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s):
        return s
    # Raw decimal integer (32-bit) → IPv4
    if s.isdigit():
        try:
            packed = struct.pack("!I", int(s))
            return ".".join(str(x) for x in packed)
        except Exception:
            pass
    return s


_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.\_]{1,253}[a-zA-Z0-9])?$"
)


def _is_valid_neighbor_name(name: str) -> bool:
    """Return True if *name* looks like a real device hostname or FQDN.

    Filters out:
      - empty strings
      - purely numeric strings (raw integers/IDs)
      - strings shorter than 3 characters
      - strings containing non-printable or non-ASCII characters
      - strings that look like binary garbage
    """
    if not name or len(name) < 3:
        return False
    # Must be printable ASCII
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return False
    # Filter out purely numeric (these are raw SNMP integer OIDs or counter values)
    if re.match(r"^\d+$", name.strip()):
        return False
    # Must pass basic hostname pattern (letters, digits, hyphens, dots, underscores)
    return bool(_HOSTNAME_RE.match(name.strip()))


# ---------------------------------------------------------------------------
# SNMP walk helpers — net-snmp subprocess implementation.
#
# History: We previously used pysnmp 7.x's asyncio API.  Two well-known
# bugs made it unsuitable for our workload:
#   1. Creating a new SnmpEngine() per walk leaks UDP sockets at the OS
#      level even with closeDispatcher() in a finally block.
#   2. With a shared SnmpEngine across many concurrent walks, the
#      single asyncio dispatcher task wedges after a few dozen walks
#      against unreachable hosts — timeout callbacks pile up and never
#      complete.  Worker becomes silent and stops processing.
#
# net-snmp's `snmpbulkwalk` is the 30-year-old reference implementation.
# Each walk runs as an OS process with hard timeout enforced via SIGKILL,
# so deadlocks are impossible — a hung walk is killed by the kernel,
# not by our event loop.
# ---------------------------------------------------------------------------

# Map our internal protocol name → net-snmp's name
_NETSNMP_AUTH = {
    "MD5":    "MD5",
    "SHA":    "SHA",
    "SHA1":   "SHA",
    "SHA128": "SHA",
    "SHA224": "SHA-224",
    "SHA256": "SHA-256",
    "SHA384": "SHA-384",
    "SHA512": "SHA-512",
    "NONE":   None,
}
_NETSNMP_PRIV = {
    "DES":    "DES",
    "AES":    "AES",
    "AES128": "AES",
    "AES192": "AES-192",
    "AES256": "AES-256",
    "NONE":   None,
}


def _parse_snmpwalk_line(line: str) -> tuple[str, Any] | None:
    """Parse one ``snmpbulkwalk -On -Oq -Ov`` style output line.

    We invoke snmpbulkwalk with ``-On`` (numeric OIDs) and standard
    output, e.g. ``.1.3.6.1.2.1.1.1.0 = STRING: "Linux foo"``.
    Returns (oid, value) or None for unparseable / error lines.
    """
    if not line or line.startswith("#"):
        return None
    if " = " not in line:
        return None
    oid, _, rest = line.partition(" = ")
    oid = oid.strip().lstrip(".")
    rest = rest.strip()
    if rest in (
        "No Such Instance currently exists at this OID",
        "No Such Object available on this agent at this OID",
        "End of MIB",
        "No more variables left in this MIB View"
        " (It is past the end of the MIB tree)",
    ):
        return None
    # Strip type prefix.  Examples:
    #   STRING: "..."
    #   INTEGER: 42
    #   Counter32: 1234
    #   Hex-STRING: 00 11 22 33 44 55
    #   IpAddress: 10.0.0.1
    #   OID: .1.3.6.1.2.1.1.1.0
    #   Network Address: 0A:00:00:01
    if ":" in rest:
        kind, _, val = rest.partition(":")
        val = val.strip()
        kind = kind.strip()
        if kind == "STRING":
            # Strip surrounding quotes if present.
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return oid, val
        if kind in ("INTEGER", "Counter32", "Counter64", "Gauge32",
                     "Unsigned32", "TimeTicks"):
            try:
                # Some types prefix the int with a label, e.g. "INTEGER: up(1)"
                m = re.match(r"^\s*(-?\d+)", val)
                if m:
                    return oid, int(m.group(1))
                return oid, val
            except ValueError:
                return oid, val
        if kind in ("Hex-STRING", "Hex"):
            # Hex bytes separated by spaces
            try:
                clean = val.replace(" ", "").replace(":", "")
                return oid, bytes.fromhex(clean)
            except ValueError:
                return oid, val
        if kind == "IpAddress":
            return oid, val
        if kind == "OID":
            return oid, val.lstrip(".")
        # Default: just return the raw value.
        return oid, val
    return oid, rest


# ---------------------------------------------------------------------------
# MIB coverage probes
# ---------------------------------------------------------------------------
#
# Each device gets a per-cycle "coverage" map recording, for each MIB family
# we care about, whether the agent exposed it (and how many rows came back).
# This lets the UI tell the user *why* something looks empty — e.g. a Catalyst
# with a narrow ``snmp-server view`` will show ``vlan: restricted``, which is
# very different from ``vlan: empty`` (the agent allowed the walk but the
# device genuinely has no VLANs).
#
# Probes are intentionally tiny — single OID per family, ``-Cr 2`` repetitions,
# short timeout — so the full scan adds well under a second per device per
# topology cycle.
#
# Status enum:
#   ok               — agent returned 1+ rows of valid data
#   empty            — agent allowed the walk but returned 0 rows (no data)
#   restricted       — snmp-server view blocked us
#                      ("No more variables left in this MIB View" or
#                      "authorizationError")
#   not_instrumented — agent does not implement this MIB
#                      ("No Such Object available on this agent")
#   timeout          — subprocess timed out
#   error            — other failure (transport, auth, unparseable output)
MIB_COVERAGE_PROBES: dict[str, dict[str, Any]] = {
    "interface":    {"label": "ifTable / ifXTable",     "oid": "1.3.6.1.2.1.31.1.1.1.1",      "required": True},
    "ip":           {"label": "ipAddress table",        "oid": "1.3.6.1.2.1.4.34.1.3",        "required": True},
    "lldp":         {"label": "IEEE 802.1AB LLDP",      "oid": "1.0.8802.1.1.2.1.4.1.1.5",    "required": True},
    "cdp":          {"label": "Cisco CDP cache",        "oid": "1.3.6.1.4.1.9.9.23.1.2.1.1.6", "required": False},
    "vlan":         {"label": "Cisco VTP VLANs",        "oid": "1.3.6.1.4.1.9.9.46.1.3.1.1.4", "required": False},
    "trunk_port":         {"label": "Cisco trunk-port native/status (.5/.14)", "oid": "1.3.6.1.4.1.9.9.46.1.6.1.1.5", "required": False},
    "trunk_port_allowed": {"label": "Cisco trunk allowed-VLAN bitmap (.4)",     "oid": "1.3.6.1.4.1.9.9.46.1.6.1.1.4", "required": False},
    "access_port":  {"label": "Cisco vmMembership",     "oid": "1.3.6.1.4.1.9.9.68.1.2.2.1.2", "required": False},
    "cam":          {"label": "802.1Q FDB (CAM)",       "oid": "1.3.6.1.2.1.17.7.1.2.2.1.2",  "required": False},
    "arp":          {"label": "ipNetToMedia (ARP)",     "oid": "1.3.6.1.2.1.4.22.1.2",        "required": False},
    "stp":          {"label": "dot1d STP port state",   "oid": "1.3.6.1.2.1.17.2.15.1.3",     "required": False},
    "bgp":          {"label": "BGP peer table",         "oid": "1.3.6.1.2.1.15.3.1.2",        "required": False},
    "ospf":         {"label": "OSPF neighbor table",    "oid": "1.3.6.1.2.1.14.10.1.6",       "required": False},
}

# Markers the net-snmp toolchain emits when the agent blocks or doesn't
# instrument an OID.  Lowercased before comparison.
_SNMP_RESTRICTED_MARKERS = (
    "no more variables left in this mib view",
    "authorizationerror",
    "(it is past the end of the mib tree)",
)
_SNMP_NOT_INSTRUMENTED_MARKERS = (
    "no such object available on this agent",
    "no such instance currently exists at this oid",
)


def _derive_snmp_health(
    coverage: dict[str, dict[str, Any]],
) -> tuple[str, list[str], list[str]]:
    """Compute (snmp_health, missing_required, restricted_families) from a
    coverage map.

    snmp_health is one of:
      * ``unreachable`` — no families came back at all (probe didn't run, or
        the agent is silent)
      * ``restricted``  — at least one *required* family is ``restricted``
                          (the agent's view is blocking us)
      * ``partial``     — at least one *required* family is missing/empty
                          but the agent is reachable on others
      * ``full``        — every required family returned data

    ``missing_required`` lists every required family with a non-``ok`` status.
    ``restricted_families`` lists every family (required or optional) whose
    status is ``restricted`` so the UI can offer a remediation hint.
    """
    if not coverage:
        return "unreachable", [], []

    missing_required: list[str] = []
    restricted: list[str] = []
    any_required_blocked = False
    for fam, entry in coverage.items():
        status = entry.get("status", "error")
        if status == "restricted":
            restricted.append(fam)
        if entry.get("required") and status != "ok":
            missing_required.append(fam)
            if status == "restricted":
                any_required_blocked = True

    if any_required_blocked:
        return "restricted", missing_required, restricted
    if missing_required:
        return "partial", missing_required, restricted
    return "full", missing_required, restricted


async def _probe_oid_with_status(
    args_no_oid: list[str],
    oid: str,
    timeout_s: float,
) -> tuple[str, int]:
    """Run snmpbulkwalk on a single OID and return (status, row_count).

    Unlike :func:`_run_snmpbulkwalk` we inspect both stdout AND stderr for
    diagnostic markers so we can tell the difference between
    "agent has no data here" and "agent's view restricts us".

    ``args_no_oid`` must be the full snmpbulkwalk command-line *without* the
    final OID argument — we append the OID ourselves.
    """
    args = args_no_oid + [oid]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return "timeout", 0

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        combined_low = (stdout + " " + stderr).lower()

        # Order matters — "restricted" is a stronger signal than
        # "not_instrumented" because view blocks frequently masquerade as
        # the "past end of MIB" sentinel.
        if any(m in combined_low for m in _SNMP_RESTRICTED_MARKERS):
            return "restricted", 0
        if any(m in combined_low for m in _SNMP_NOT_INSTRUMENTED_MARKERS):
            return "not_instrumented", 0

        rows = 0
        for raw in stdout.splitlines():
            parsed = _parse_snmpwalk_line(raw)
            if parsed is not None:
                rows += 1

        if rows > 0:
            return "ok", rows
        if proc.returncode != 0:
            return "error", 0
        return "empty", 0
    except FileNotFoundError:
        return "error", 0
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return "error", 0


async def _run_snmpbulkwalk(
    args: list[str],
    timeout: float,
    host: str,
    oid: str,
) -> list[tuple[str, Any]]:
    """Spawn snmpbulkwalk as a subprocess and parse its output.

    A hard wall-clock timeout is enforced via SIGKILL so a hung walk
    cannot block the event loop or leak resources.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.debug("snmp.walk.subprocess_timeout", host=host, oid=oid,
                      timeout_s=timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            return []
        if proc.returncode != 0:
            # snmpbulkwalk returns non-zero on auth failure, timeout, etc.
            return []
        results: list[tuple[str, Any]] = []
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            parsed = _parse_snmpwalk_line(raw)
            if parsed is not None:
                results.append(parsed)
        return results
    except FileNotFoundError:
        log.error("snmp.walk.snmpbulkwalk_not_found",
                  hint="Install net-snmp tools (apt-get install snmp)")
        return []
    except Exception as exc:
        log.debug("snmp.walk.subprocess_error", host=host, oid=oid, error=str(exc))
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return []


async def _snmp_walk_v2c(
    host: str,
    community: str,
    oid: str,
    timeout: int = 5,
    port: int = 161,
    *,
    chunk_repetitions: int = 25,
    ignore_not_increasing: bool = False,
    context_name: str | None = None,
) -> list[tuple[str, Any]]:
    """Walk an OID subtree using SNMPv2c via net-snmp's snmpbulkwalk.

    Args:
        chunk_repetitions: ``GetBulk`` max-repetitions per PDU (-Cr<n>).
            Lower values break a large walk into many smaller PDUs, which
            helps agents that drop or time out on large bulk responses.
            Inspired by Splunk-Connect-for-SNMP's PDU chunking.
        ignore_not_increasing: If True, pass ``-Cc`` so net-snmp keeps
            walking even when the agent returns non-monotonic OIDs (a
            known bug on some IOS-XE / NX-OS versions).
        context_name: Cisco PVST+ per-VLAN community indexing. When set,
            the community string is rewritten to ``<community>@<context>``
            — the documented v2c trick for selecting Cisco's per-VLAN
            BRIDGE-MIB view (e.g. ``public@vlan-10``).
    """
    rep = max(1, min(int(chunk_repetitions), 200))
    eff_community = f"{community}@{context_name}" if context_name else community
    args = [
        "snmpbulkwalk",
        "-v", "2c",
        "-c", eff_community,
        "-On",                  # numeric OIDs
        f"-Cr{rep}",            # bulk repetitions (NB: net-snmp -C* is glued)
        "-t", str(max(1, int(timeout))),
        "-r", "0",              # no retries
    ]
    if ignore_not_increasing:
        args.append("-Cc")
    args += [f"{host}:{port}", oid]
    # Wall-clock timeout = SNMP timeout × bulk roundtrips (rough)
    wall = max(float(timeout) * 4.0, 15.0)
    return await _run_snmpbulkwalk(args, wall, host, oid)


async def _snmp_walk_v3(
    host: str,
    creds: SnmpV3Creds,
    oid: str,
    timeout: int = 5,
    port: int = 161,
    *,
    chunk_repetitions: int = 25,
    ignore_not_increasing: bool = False,
    context_name: str | None = None,
) -> list[tuple[str, Any]]:
    """Walk an OID subtree using SNMPv3 USM via net-snmp's snmpbulkwalk.

    Args:
        chunk_repetitions: ``GetBulk`` max-repetitions per PDU. Lowering
            helps when the agent or path can't handle large bulk replies.
        ignore_not_increasing: Pass ``-Cc`` to ignore non-monotonic OID
            returns from buggy agents (common workaround on IOS-XE).
        context_name: SNMPv3 ``contextName`` (-n flag). Cisco PVST+/Rapid-
            PVST+ devices expose per-VLAN BRIDGE-MIB / Q-BRIDGE-MIB views
            under the context ``vlan-<vid>`` (when the SNMP group is
            configured with ``context vlan- match prefix``). Pass the
            context name to read those per-VLAN instances. ``None`` walks
            the default context, which on PVST+ devices is the CST /
            VLAN-1 instance.
    """
    auth_proto = _NETSNMP_AUTH.get(creds.auth_protocol.upper())
    priv_proto = _NETSNMP_PRIV.get(creds.priv_protocol.upper())
    level = creds.security_level.lower()

    if level == "noauthnopriv":
        sec_level = "noAuthNoPriv"
    elif level == "authnopriv":
        sec_level = "authNoPriv"
    else:
        sec_level = "authPriv"

    rep = max(1, min(int(chunk_repetitions), 200))
    args = [
        "snmpbulkwalk",
        "-v", "3",
        "-l", sec_level,
        "-u", creds.username,
        "-On",
        f"-Cr{rep}",            # net-snmp -C* options are glued (no space)
        "-t", str(max(1, int(timeout))),
        "-r", "0",
    ]
    if ignore_not_increasing:
        args.append("-Cc")
    if context_name:
        args += ["-n", context_name]
    if sec_level in ("authNoPriv", "authPriv"):
        if not auth_proto:
            log.debug("snmp.walk.unsupported_auth_proto",
                      proto=creds.auth_protocol)
            return []
        args += ["-a", auth_proto, "-A", creds.auth_password or ""]
    if sec_level == "authPriv":
        if not priv_proto:
            log.debug("snmp.walk.unsupported_priv_proto",
                      proto=creds.priv_protocol)
            return []
        args += ["-x", priv_proto, "-X", creds.priv_password or ""]
    args += [f"{host}:{port}", oid]

    wall = max(float(timeout) * 4.0, 15.0)
    return await _run_snmpbulkwalk(args, wall, host, oid)


class _SnmpSession:
    """Wraps a host + credential pair into a convenient walk interface.

    ``walk_timeout`` caps the total time for a single OID subtree walk,
    independent of the per-PDU timeout. For large tables (LLDP, routing) this
    prevents individual walks from blocking the event loop for many minutes.

    Per-host tunables (inspired by Splunk-Connect-for-SNMP):
      * ``chunk_repetitions``: max-repetitions per GetBulk PDU. Smaller
        values trade more PDUs for smaller responses, which is friendlier
        to agents that drop large replies.
      * ``ignore_not_increasing``: pass ``-Cc`` so net-snmp keeps walking
        when an agent returns non-monotonic OIDs (a known IOS-XE bug).
    """

    def __init__(
        self,
        host: str,
        creds: SnmpV2Creds | SnmpV3Creds,
        timeout: int,
        walk_timeout: float = 90.0,
        *,
        chunk_repetitions: int = 25,
        ignore_not_increasing: bool = False,
    ) -> None:
        self.host = host
        self.creds = creds
        self.timeout = timeout
        self.walk_timeout = walk_timeout
        self.chunk_repetitions = chunk_repetitions
        self.ignore_not_increasing = ignore_not_increasing

    async def walk(
        self,
        oid: str,
        *,
        context_name: str | None = None,
    ) -> list[tuple[str, Any]]:
        """Walk an OID subtree.

        ``context_name`` selects a non-default SNMPv3 ``contextName`` (or
        the v2c ``community@context`` index trick). Required for reading
        per-VLAN BRIDGE-MIB / Q-BRIDGE-MIB views on Cisco PVST+ devices,
        which expose each VLAN as ``vlan-<vid>``.
        """
        try:
            if isinstance(self.creds, SnmpV3Creds):
                coro = _snmp_walk_v3(
                    self.host, self.creds, oid, self.timeout,
                    chunk_repetitions=self.chunk_repetitions,
                    ignore_not_increasing=self.ignore_not_increasing,
                    context_name=context_name,
                )
            else:
                coro = _snmp_walk_v2c(
                    self.host, self.creds.community, oid, self.timeout,
                    chunk_repetitions=self.chunk_repetitions,
                    ignore_not_increasing=self.ignore_not_increasing,
                    context_name=context_name,
                )
            return await asyncio.wait_for(coro, timeout=self.walk_timeout)
        except asyncio.TimeoutError:
            log.debug("snmp.walk.timeout", host=self.host, oid=oid,
                      context=context_name,
                      walk_timeout=self.walk_timeout)
            return []
        except Exception as exc:
            log.debug("snmp.walk.error", host=self.host, oid=oid,
                      context=context_name, error=str(exc))
            return []

    async def get_scalar(self, base_oid: str) -> Any | None:
        """GET scalar OID (appends .0) — v3 only for now; falls back to walk."""
        rows = await self.walk(base_oid)
        if rows:
            return rows[0][1]
        return None

    def _probe_args_no_oid(self) -> list[str] | None:
        """Build snmpbulkwalk argv (sans final OID) for a fast coverage probe.

        Returns None if the configured credentials use an algorithm we don't
        support — in that case the probe is silently skipped.
        """
        if isinstance(self.creds, SnmpV3Creds):
            auth_proto = _NETSNMP_AUTH.get(self.creds.auth_protocol.upper())
            priv_proto = _NETSNMP_PRIV.get(self.creds.priv_protocol.upper())
            level = self.creds.security_level.lower()
            if level == "noauthnopriv":
                sec_level = "noAuthNoPriv"
            elif level == "authnopriv":
                sec_level = "authNoPriv"
            else:
                sec_level = "authPriv"
            args = [
                "snmpbulkwalk",
                "-v", "3",
                "-l", sec_level,
                "-u", self.creds.username,
                "-On",
                "-Cr2",                 # 2 repetitions is plenty for a probe
                "-t", "2",              # 2s per-PDU
                "-r", "1",              # one retry
            ]
            if sec_level in ("authNoPriv", "authPriv"):
                if not auth_proto:
                    return None
                args += ["-a", auth_proto, "-A", self.creds.auth_password or ""]
            if sec_level == "authPriv":
                if not priv_proto:
                    return None
                args += ["-x", priv_proto, "-X", self.creds.priv_password or ""]
            args += [f"{self.host}:161"]
            return args
        # SNMPv2c
        return [
            "snmpbulkwalk",
            "-v", "2c",
            "-c", self.creds.community,
            "-On",
            "-Cr2",
            "-t", "2",
            "-r", "1",
            f"{self.host}:161",
        ]

    async def probe_coverage(
        self,
        families: list[str] | None = None,
        per_probe_timeout_s: float = 8.0,
    ) -> dict[str, dict[str, Any]]:
        """Walk a single sentinel OID per MIB family and report status.

        Returns ``{family: {"status": <enum>, "rows": <int>, "oid": <str>,
                            "label": <str>, "required": <bool>, "at": <epoch>}}``.

        Probes are run concurrently (bounded by 4 in-flight) to keep wall time
        proportional to the slowest family, not the sum.
        """
        import time as _time

        args_no_oid = self._probe_args_no_oid()
        if args_no_oid is None:
            return {}

        sem = asyncio.Semaphore(4)
        probe_families = families or list(MIB_COVERAGE_PROBES.keys())

        async def _one(fam: str) -> tuple[str, dict[str, Any]]:
            spec = MIB_COVERAGE_PROBES[fam]
            async with sem:
                status, rows = await _probe_oid_with_status(
                    args_no_oid, spec["oid"], per_probe_timeout_s,
                )
            return fam, {
                "status": status,
                "rows": rows,
                "oid": spec["oid"],
                "label": spec["label"],
                "required": spec["required"],
                "at": int(_time.time()),
            }

        results = await asyncio.gather(
            *[_one(f) for f in probe_families if f in MIB_COVERAGE_PROBES],
            return_exceptions=True,
        )
        out: dict[str, dict[str, Any]] = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            fam, entry = r
            out[fam] = entry
        return out


# ---------------------------------------------------------------------------
# Per-device poll helpers
# ---------------------------------------------------------------------------

def _build_interface_health_nodes(
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
    counters: dict[str, dict[str, Any]],
    source_adapter: str,
) -> GraphData:
    """Materialize Interface nodes carrying counter-derived health properties.

    These nodes also get a HAS_INTERFACE edge from the parent Device.  The
    properties (rate_*, util_*, error_rate_*, has_baseline) are picked up by
    the UI to render edge color/thickness and tooltips on PHYSICAL_LINK.
    """
    data = GraphData(adapter_id=source_adapter)
    for ifindex, health in counters.items():
        iface = if_map.get(ifindex, {})
        name = iface.get("name") or f"if-{ifindex}"
        iface_id = f"snmp-if:{dev_node_id}:{name}"

        props = {
            "name": name,
            "ifindex": int(ifindex) if ifindex.isdigit() else ifindex,
            "device_id": dev_node_id,
            "alias": iface.get("alias"),
            "mac": iface.get("mac"),
            "oper_status": iface.get("oper_status"),
            "speed_mbps": iface.get("speed_mbps"),
            # L2 attributes (populated by _poll_port_vlans). Stored as
            # plain dict/list values so the UI can render them without
            # extra Cypher round-trips.
            "trunk_mode": iface.get("trunk_mode"),
            "vlans_access": iface.get("vlans_access"),
            "vlans_allowed": iface.get("vlans_allowed"),
            "native_vlan": iface.get("native_vlan"),
            # Health metrics — surface as Cytoscape-friendly fields
            **health,
            "health_updated_at": _now_ts(),
        }
        # Compute a single "score" so UI can color uniformly (0–100; higher = worse)
        score = _interface_health_score(health)
        if score is not None:
            props["health_score"] = score

        data.nodes.append(GraphNode(
            id=iface_id,
            type=NodeType.INTERFACE,
            dimensions=[Dimension.PHYSICAL],
            source_adapter=source_adapter,
            properties={k: v for k, v in props.items() if v is not None},
        ))
        data.edges.append(GraphEdge(
            source_id=dev_node_id,
            target_id=iface_id,
            type=EdgeType.HAS_INTERFACE,
            source_adapter=source_adapter,
            dimension=Dimension.PHYSICAL,
            properties={},
        ))
    return data


def _now_ts() -> float:
    import time as _t
    return _t.time()


def _interface_health_score(health: dict[str, Any]) -> int | None:
    """Combine utilization, errors, and oper_status into 0-100 (higher = worse).

    Returns None when we don't yet have a baseline for the interface.
    """
    if not health.get("has_baseline"):
        return None
    util = max(health.get("util_in_pct") or 0, health.get("util_out_pct") or 0)
    err  = (health.get("error_rate_in_per_s")  or 0) + \
           (health.get("error_rate_out_per_s") or 0)
    # Util buckets: <50%=0, <80%=20, <95%=50, >=95%=80
    if   util >= 95: u_score = 80
    elif util >= 80: u_score = 50
    elif util >= 50: u_score = 20
    else:            u_score = 0
    # Error buckets: 0=0, <1/s=10, <10/s=30, >=10/s=60
    if   err >= 10: e_score = 60
    elif err >= 1:  e_score = 30
    elif err >  0:  e_score = 10
    else:           e_score = 0
    return min(100, u_score + e_score)


async def _poll_interfaces(sess: _SnmpSession) -> dict[str, dict[str, Any]]:
    """Return {if_index: {name, alias, mac, oper_status, speed}} from IF-MIB.

    Short-circuits immediately if the device does not respond to the first walk,
    avoiding the cost of 6 more sequential timeouts for unreachable hosts.
    """
    ifaces: dict[str, dict[str, Any]] = {}

    # ifName first — if the device doesn't respond here, stop immediately.
    name_rows = await sess.walk(OID_IF_NAME)
    if not name_rows:
        # Try ifDescr as the only fallback; still give up if empty.
        name_rows = await sess.walk(OID_IF_DESCR)
        if not name_rows:
            return ifaces  # device unreachable — skip remaining walks

    for oid, val in name_rows:
        idx = oid.rsplit(".", 1)[-1]
        ifaces.setdefault(idx, {})["name"] = str(val)

    # Device responded — collect remaining interface properties concurrently.
    alias_rows, phys_rows, status_rows, speed_rows = await asyncio.gather(
        sess.walk(OID_IF_ALIAS),
        sess.walk(OID_IF_PHYS_ADDR),
        sess.walk(OID_IF_OPER_STATUS),
        sess.walk(OID_IF_SPEED),
    )

    for oid, val in alias_rows:
        ifaces.setdefault(oid.rsplit(".", 1)[-1], {})["alias"] = str(val)

    for oid, val in phys_rows:
        mac = _norm_mac(val)
        if mac:
            ifaces.setdefault(oid.rsplit(".", 1)[-1], {})["mac"] = mac

    for oid, val in status_rows:
        ifaces.setdefault(oid.rsplit(".", 1)[-1], {})["oper_status"] = (
            "up" if str(val) == "1" else "down"
        )

    for oid, val in speed_rows:
        try:
            ifaces.setdefault(oid.rsplit(".", 1)[-1], {})["speed_mbps"] = int(val)
        except Exception:
            pass

    return ifaces


async def _poll_port_vlans(
    sess: _SnmpSession,
    if_map: dict[str, dict[str, Any]],
) -> None:
    """Augment ``if_map`` with per-port VLAN membership.

    Populates three new keys per ifIndex (when known):
      * ``vlans_access``  — int VID for access ports
      * ``vlans_allowed`` — sorted list of trunk-allowed VIDs
      * ``trunk_mode``    — "trunk" / "access" / "unknown"
      * ``native_vlan``   — int (only when trunking)

    Strategy (tried in order, each layered on top of the previous):
      1. ``vlanTrunkPortDynamicStatus`` + ``vlanTrunkPortVlansEnabled[2k|3k|4k]``
         — Cisco IOS/IOS-XE/NX-OS trunks.
      2. ``vmVlan`` — Cisco access-port VLAN.
      3. ``dot1qVlanCurrentEgressPorts`` — vendor-neutral fallback used by
         NX-OS and Arista when the CISCO-VLAN-MEMBERSHIP-MIB is denied.

    All walks fail soft — a device that doesn't expose the MIB just
    leaves the fields unset and downstream code treats that as "unknown".
    """
    try:
        trunk_status_rows, trunk_native_rows = await asyncio.gather(
            sess.walk(OID_VLAN_TRUNK_STATUS),
            sess.walk(OID_VLAN_TRUNK_NATIVE),
        )

        # ── Trunk allowed-VLAN bitmaps (1k + 2k + 3k + 4k segments) ──
        trunk_bitmaps: dict[str, list[int]] = {}
        for oid_base, offset in (
            (OID_VLAN_TRUNK_ENABLED,    0),
            (OID_VLAN_TRUNK_ENABLED2K,  1024),
            (OID_VLAN_TRUNK_ENABLED3K,  2048),
            (OID_VLAN_TRUNK_ENABLED4K,  3072),
        ):
            rows = await sess.walk(oid_base)
            for oid, val in rows:
                ifindex = oid.rsplit(".", 1)[-1]
                trunk_bitmaps.setdefault(ifindex, []).extend(
                    _decode_vlan_bitmap(val, offset=offset)
                )

        for oid, val in trunk_status_rows:
            ifindex = oid.rsplit(".", 1)[-1]
            iface = if_map.setdefault(ifindex, {})
            # vlanTrunkPortDynamicStatus: 1=trunking, 2=notTrunking
            sval = str(val).strip()
            if sval == "1":
                iface["trunk_mode"] = "trunk"
                vids = sorted(set(trunk_bitmaps.get(ifindex, [])))
                if vids:
                    iface["vlans_allowed"] = vids
            elif sval == "2":
                iface["trunk_mode"] = "access"

        for oid, val in trunk_native_rows:
            ifindex = oid.rsplit(".", 1)[-1]
            try:
                native = int(str(val).strip())
            except ValueError:
                continue
            iface = if_map.setdefault(ifindex, {})
            if iface.get("trunk_mode") == "trunk":
                iface["native_vlan"] = native

        # ── Access-port VLAN (Cisco) ───────────────────────────────
        vm_rows = await sess.walk(OID_VMVLAN)
        for oid, val in vm_rows:
            ifindex = oid.rsplit(".", 1)[-1]
            try:
                vid = int(str(val).strip())
            except ValueError:
                continue
            iface = if_map.setdefault(ifindex, {})
            iface["vlans_access"] = vid
            if "trunk_mode" not in iface:
                iface["trunk_mode"] = "access"

        # ── Vendor-neutral fallback (Q-BRIDGE) ─────────────────────
        # dot1qVlanCurrentEgressPorts is indexed by (TimeMark, VlanId)
        # and value is a port bitmap. We transpose into ifindex → [vids].
        # Used only when CISCO-VTP-MIB walks returned nothing.
        if not trunk_bitmaps:
            qb_rows = await sess.walk(OID_DOT1Q_VLAN_EGRESS)
            ifindex_vlans: dict[str, list[int]] = {}
            for oid, val in qb_rows:
                suffix = oid[len(OID_DOT1Q_VLAN_EGRESS) + 1:]
                parts = suffix.split(".")
                if len(parts) < 2:
                    continue
                try:
                    vid = int(parts[-1])
                except ValueError:
                    continue
                if not isinstance(val, (bytes, bytearray)):
                    continue
                # Each set bit in the port bitmap → that port carries this VLAN
                raw = bytes(val)
                for byte_idx, byte_val in enumerate(raw):
                    if not byte_val:
                        continue
                    for bit_idx in range(8):
                        if byte_val & (0x80 >> bit_idx):
                            port_num = byte_idx * 8 + bit_idx + 1
                            ifindex_vlans.setdefault(str(port_num), []).append(vid)
            for ifindex, vids in ifindex_vlans.items():
                iface = if_map.setdefault(ifindex, {})
                merged = sorted(set(iface.get("vlans_allowed", []) + vids))
                if merged:
                    iface["vlans_allowed"] = merged
                if "trunk_mode" not in iface and len(merged) > 1:
                    iface["trunk_mode"] = "trunk"

        log.debug(
            "snmp.port_vlans_done",
            host=sess.host,
            trunks=sum(1 for v in if_map.values() if v.get("trunk_mode") == "trunk"),
            access=sum(1 for v in if_map.values() if v.get("trunk_mode") == "access"),
        )
    except Exception as exc:
        log.warning("snmp.port_vlans_failed", host=sess.host, error=str(exc))


# ── Per-VLAN STP context walks (dev19) ────────────────────────────────────
#
# Some Cisco IOS-XE / NX-OS agents withhold ``vlanTrunkPortVlansEnabled``
# even when the SNMP view is wide open and VTP is configured — observed
# in the wild on cat9300/cat9400 17.x running VTP transparent. The
# device cheerfully returns trunk status and native VLAN but every
# allowed-VLAN bitmap column (.4 / .17 / .18 / .19) walks empty.
#
# The same data is recoverable via per-VLAN BRIDGE-MIB walks: with
# Rapid-PVST+, each VLAN has its own STP instance, and the set of VLANs
# where a given port appears in ``dot1dStpPortTable`` with state ≠
# ``disabled`` is exactly the set of VLANs that traverse that port —
# the same set the operator sees in ``show spanning-tree interface X``.
#
# Implementation notes
# --------------------
# * Walks ``vtpVlanState`` once in the default context to enumerate
#   operational VLAN IDs (state=1 means operational).
# * For each VLAN, walks ``dot1dStpPortState`` AND
#   ``dot1dBasePortIfIndex`` in SNMPv3 ``contextName=vlan-<N>``
#   (community-string ``<community>@vlan-<N>`` for v2c). The base→ifIndex
#   table is walked per context because some platforms shift the mapping
#   between PVST instances.
# * Concurrent context walks are capped at 8 per device. Each walk is
#   small (≤ port-count rows) and most devices have < 30 active VLANs,
#   so the steady-state cost is sub-second.
# * Requires the SNMP group to expose per-VLAN contexts. On IOS-XE that
#   means:
#       snmp-server group <grp> v3 priv read <view> context vlan- match prefix
#   If the device hasn't been configured for context indexing, every
#   walk returns 0 rows and we log a single warning suggesting the fix.

OID_VTP_VLAN_STATE_FULL = OID_VTP_VLAN_STATE  # alias for readability below

# Index format of vtpVlanState rows is ``<vtpDomainIdx>.<vlanId>`` —
# the last numeric component is the VLAN ID we want.

# Cap on parallel per-VLAN walks so we don't flood the agent. A typical
# distribution switch has 10–40 active VLANs; 8 concurrent context walks
# keeps the device responsive while still finishing the per-VLAN harvest
# in well under 10 seconds on commodity hardware.
_PER_VLAN_PARALLELISM = 8

# Maximum number of VLANs to walk per device. Guards against pathological
# cases (e.g. a core that advertises every reserved VLAN). Each context
# walk costs one PDU round-trip, so 256 walks is ~2-4s budget at typical
# RTTs. Operational VLAN counts above this are exceedingly rare.
_PER_VLAN_WALK_LIMIT = 256


async def _poll_per_vlan_stp(
    sess: _SnmpSession,
    if_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Derive per-port carried-VLAN sets from per-VLAN STP context walks.

    Augments each ``if_map[ifIndex]`` with:

      * ``vlans_stp_member`` — sorted list of VLANs where this port has
        an STP entry with state ≠ ``disabled`` (the union over all
        per-VLAN instances). This is the same set the CLI's
        ``show spanning-tree interface X`` would print.
      * ``vlans_stp_forwarding`` — subset of the above where state is
        ``forwarding`` (useful for highlighting actively-forwarding VLANs
        vs blocked/learning).

    Also, when a port has ``trunk_mode == 'trunk'`` but no
    ``vlans_allowed`` (CISCO-VTP-MIB bitmap silent), the STP-derived
    ``vlans_stp_member`` is promoted to ``vlans_allowed`` so the
    correlator's L2 decoration pipeline can use it without changes.

    Returns a small diagnostic dict ``{vlans_probed, contexts_with_data,
    ports_enriched, stp_promoted_allowed}`` for logging and the
    SNMP-coverage badge.
    """
    diag = {
        "vlans_probed": 0,
        "contexts_with_data": 0,
        "ports_enriched": 0,
        "stp_promoted_allowed": 0,
    }
    try:
        vtp_rows = await sess.walk(OID_VTP_VLAN_STATE_FULL)
        # vtpVlanState=1 means "operational"; skip 2/3/4 (suspended/etc.)
        # and skip reserved IDs (0, 1002-1005 — Cisco's legacy fddi/trcrf).
        operational_vlans: list[int] = []
        for oid, val in vtp_rows:
            tail = oid.rsplit(".", 1)[-1]
            if not tail.isdigit():
                continue
            try:
                state = int(str(val).strip())
            except (ValueError, TypeError):
                continue
            if state != 1:
                continue
            vid = int(tail)
            if vid in (0, 1002, 1003, 1004, 1005):
                continue
            operational_vlans.append(vid)
        operational_vlans.sort()
        if len(operational_vlans) > _PER_VLAN_WALK_LIMIT:
            log.warning(
                "snmp.per_vlan_stp.too_many_vlans",
                host=sess.host,
                operational=len(operational_vlans),
                walking=_PER_VLAN_WALK_LIMIT,
            )
            operational_vlans = operational_vlans[:_PER_VLAN_WALK_LIMIT]
        diag["vlans_probed"] = len(operational_vlans)
        if not operational_vlans:
            return diag

        # Aggregate: ifIndex → {vid: state_str}
        port_member_states: dict[str, dict[int, str]] = {}
        sem = asyncio.Semaphore(_PER_VLAN_PARALLELISM)

        async def _walk_one_vlan(vid: int) -> tuple[int, int, int]:
            """Returns (vid, rows_seen, ports_with_state) for diagnostics."""
            ctx = f"vlan-{vid}"
            async with sem:
                # base_port → ifIndex (per-context to be safe on platforms
                # that shift the mapping between PVST instances).
                base_rows = await sess.walk(
                    OID_DOT1D_BASE_PORT_IF, context_name=ctx,
                )
                state_rows = await sess.walk(
                    OID_STP_PORT_STATE, context_name=ctx,
                )
            if not state_rows:
                return vid, 0, 0
            base_to_if: dict[str, str] = {}
            for oid, val in base_rows:
                bnum = oid.rsplit(".", 1)[-1]
                idx = str(val).strip()
                if bnum.isdigit() and idx.isdigit():
                    base_to_if[bnum] = idx
            ports_with_state = 0
            for oid, val in state_rows:
                bnum = oid.rsplit(".", 1)[-1]
                state_str = STP_PORT_STATES.get(str(val).strip(), "")
                if not state_str or state_str == "disabled":
                    continue
                ifindex = base_to_if.get(bnum, bnum)
                port_member_states.setdefault(ifindex, {})[vid] = state_str
                ports_with_state += 1
            return vid, len(state_rows), ports_with_state

        results = await asyncio.gather(
            *[_walk_one_vlan(v) for v in operational_vlans],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.debug("snmp.per_vlan_stp.walk_error",
                          host=sess.host, error=str(r))
                continue
            _vid, _rows, ports = r
            if ports:
                diag["contexts_with_data"] += 1

        # Stamp aggregates onto if_map.
        for ifindex, vid_states in port_member_states.items():
            iface = if_map.setdefault(ifindex, {})
            vids_member = sorted(vid_states.keys())
            vids_fwd = sorted(v for v, s in vid_states.items() if s == "forwarding")
            iface["vlans_stp_member"] = vids_member
            iface["vlans_stp_forwarding"] = vids_fwd
            diag["ports_enriched"] += 1
            # Promote to vlans_allowed when the CISCO-VTP bitmap was
            # silent. We DON'T overwrite an existing allowed list — if
            # the device DID return the bitmap, that's authoritative and
            # already includes pruned-out VLANs we shouldn't strip.
            if iface.get("trunk_mode") == "trunk" and not iface.get("vlans_allowed"):
                iface["vlans_allowed"] = vids_member
                iface["vlans_allowed_source"] = "stp_per_vlan_context"
                diag["stp_promoted_allowed"] += 1

        if diag["vlans_probed"] and diag["contexts_with_data"] == 0:
            # All context walks came back empty — the agent almost
            # certainly doesn't have ``context vlan- match prefix`` on
            # the SNMP group. Log a single actionable warning per device.
            log.warning(
                "snmp.per_vlan_stp.context_not_configured",
                host=sess.host,
                vlans_probed=diag["vlans_probed"],
                hint=(
                    "Add to the device: snmp-server group <grp> v3 priv "
                    "read <view> context vlan- match prefix"
                ),
            )
        else:
            log.debug(
                "snmp.per_vlan_stp.done",
                host=sess.host,
                vlans_probed=diag["vlans_probed"],
                contexts_with_data=diag["contexts_with_data"],
                ports_enriched=diag["ports_enriched"],
                stp_promoted_allowed=diag["stp_promoted_allowed"],
            )
    except Exception as exc:
        log.warning("snmp.per_vlan_stp.failed",
                    host=sess.host, error=str(exc))
    return diag


async def _poll_cam_table(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
) -> GraphData:
    """CAM table → MACAddress nodes + LEARNED_MAC edges."""
    data = GraphData(adapter_id="snmp")
    try:
        mac_rows = await sess.walk(OID_CAM_MAC)
        port_rows = await sess.walk(OID_CAM_PORT)

        port_map: dict[str, int] = {}
        for oid, val in port_rows:
            idx = oid[len(OID_CAM_PORT)+1:]
            try:
                port_map[idx] = int(val)
            except Exception:
                pass

        seen_macs: set[str] = set()
        seen_ifaces: set[str] = set()
        seen_mac_nodes: set[str] = set()
        for oid, val in mac_rows:
            idx = oid[len(OID_CAM_MAC)+1:]
            mac = _norm_mac(val)
            if not mac or mac in seen_macs:
                continue
            seen_macs.add(mac)

            port_num = port_map.get(idx)
            if_info = if_map.get(str(port_num), {}) if port_num else {}
            if_name = if_info.get("name", f"port-{port_num}" if port_num else "unknown")
            # Key Interface nodes by the canonical dev_node_id (not the
            # SNMP host IP) so they MERGE with the Interface nodes created
            # by the main counter/interface poll.  Without this every
            # SNMP-host-IP keyed Interface lived as an orphan that no
            # decorator pass could ever traverse from a Device.
            iface_node_id = f"snmp-if:{dev_node_id}:{if_name}"

            if iface_node_id not in seen_ifaces:
                seen_ifaces.add(iface_node_id)
                data.nodes.append(GraphNode(
                    id=iface_node_id,
                    type=NodeType.INTERFACE,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties={"name": if_name, "device_id": dev_node_id},
                ))
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=iface_node_id,
                    type=EdgeType.HAS_INTERFACE,
                    dimension=Dimension.PHYSICAL,
                    source_adapter="snmp",
                ))

            mac_node_id = f"mac:{mac}"
            if mac_node_id not in seen_mac_nodes:
                seen_mac_nodes.add(mac_node_id)
                data.nodes.append(GraphNode(
                    id=mac_node_id,
                    type=NodeType.MAC_ADDRESS,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties={"mac": mac, "source": f"snmp:{sess.host}"},
                ))

            data.edges.append(GraphEdge(
                source_id=iface_node_id,
                target_id=mac_node_id,
                type=EdgeType.LEARNED_MAC,
                dimension=Dimension.PHYSICAL,
                source_adapter="snmp",
            ))

        log.debug("snmp.cam_done", host=sess.host, entries=len(seen_macs))
    except Exception as exc:
        log.warning("snmp.cam_failed", host=sess.host, error=str(exc))
    return data


async def _poll_arp_table(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
) -> GraphData:
    """ARP table → ARPEntry nodes + HAS_ARP edges."""
    data = GraphData(adapter_id="snmp")
    try:
        mac_rows = await sess.walk(OID_ARP_PHYS)

        seen: set[str] = set()
        seen_ifaces: set[str] = set()
        seen_arp_nodes: set[str] = set()
        for oid, val in mac_rows:
            suffix = oid[len(OID_ARP_PHYS)+1:]
            parts = suffix.split(".", 1)
            if_idx = parts[0]
            ip_str = parts[1] if len(parts) > 1 else ""
            mac = _norm_mac(val)
            if not mac or not ip_str:
                continue
            key = f"{ip_str}:{mac}"
            if key in seen:
                continue
            seen.add(key)

            if_info = if_map.get(if_idx, {})
            if_name = if_info.get("name", f"if-{if_idx}")
            # See note in _poll_cam_table — key Interface nodes by
            # canonical dev_node_id so the ARP-derived Interface is the
            # same Neo4j node as the counter/STP/L2 polls produce.
            iface_node_id = f"snmp-if:{dev_node_id}:{if_name}"

            if iface_node_id not in seen_ifaces:
                seen_ifaces.add(iface_node_id)
                data.nodes.append(GraphNode(
                    id=iface_node_id,
                    type=NodeType.INTERFACE,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties={"name": if_name, "device_id": dev_node_id},
                ))
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=iface_node_id,
                    type=EdgeType.HAS_INTERFACE,
                    dimension=Dimension.PHYSICAL,
                    source_adapter="snmp",
                ))

            arp_node_id = f"arp:{ip_str}"
            if arp_node_id not in seen_arp_nodes:
                seen_arp_nodes.add(arp_node_id)
                data.nodes.append(GraphNode(
                    id=arp_node_id,
                    type=NodeType.ARP_ENTRY,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties={
                        "ip": ip_str,
                        "mac": mac,
                        "device": sess.host,
                        "source": f"snmp:{sess.host}",
                    },
                ))

            data.edges.append(GraphEdge(
                source_id=iface_node_id,
                target_id=arp_node_id,
                type=EdgeType.HAS_ARP,
                dimension=Dimension.PHYSICAL,
                source_adapter="snmp",
            ))

        log.debug("snmp.arp_done", host=sess.host, entries=len(seen))
    except Exception as exc:
        log.warning("snmp.arp_failed", host=sess.host, error=str(exc))
    return data


async def _poll_stp(
    sess: _SnmpSession,
    dev_node_id: str,
    dev_name: str,
    if_map: dict[str, dict[str, Any]],
) -> GraphData:
    """STP data → STPDomain node + STP_MEMBER/STP_ROOT/STP_LINK edges."""
    data = GraphData(adapter_id="snmp")
    try:
        # Scalar STP values
        priority_rows = await sess.walk(OID_DOT1D_STP_PRIORITY)
        root_rows     = await sess.walk(OID_DOT1D_STP_ROOT)
        root_port_rows = await sess.walk(OID_DOT1D_STP_ROOT_PORT)
        root_cost_rows = await sess.walk(OID_DOT1D_STP_ROOT_COST)

        if not priority_rows:
            log.debug("snmp.stp.no_data", host=sess.host)
            return data

        def _safe_int(val: Any, default: int = 0) -> int:
            s = str(val)
            return int(s) if s.lstrip("-").isdigit() else default

        bridge_priority = _safe_int(priority_rows[0][1], 32768) if priority_rows else 32768
        root_bridge_id_raw = root_rows[0][1] if root_rows else None
        root_bridge_mac = _mac_from_bridge_id(root_bridge_id_raw)
        root_port_num = _safe_int(root_port_rows[0][1]) if root_port_rows else 0
        root_path_cost = _safe_int(root_cost_rows[0][1]) if root_cost_rows else 0

        # Use the bridge MAC as the STP domain key when available
        domain_id = f"stp:{sess.host}:default"
        if root_bridge_mac:
            domain_id = f"stp-domain:{root_bridge_mac}"

        # Create or reference the STPDomain node (always unique per poll call)
        if True:  # noqa: SIM210 — always add once; domain_id is unique per device
            data.nodes.append(GraphNode(
                id=domain_id,
                type=NodeType.STP_DOMAIN,
                dimensions=[Dimension.STP],
                source_adapter="snmp",
                properties={
                    "root_bridge_mac": root_bridge_mac or "",
                    "vlan": 1,   # default instance; PVST data would have per-VLAN
                    "bridge_protocol": "rstp",
                },
            ))

        # Membership edge
        data.edges.append(GraphEdge(
            source_id=dev_node_id,
            target_id=domain_id,
            type=EdgeType.STP_MEMBER,
            dimension=Dimension.STP,
            source_adapter="snmp",
            properties={
                "bridge_priority": bridge_priority,
                "root_path_cost": root_path_cost,
            },
        ))

        # Root bridge edge — if root_port_num==0, this device IS the root
        is_root = (root_port_num == 0)
        if is_root:
            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=domain_id,
                type=EdgeType.STP_ROOT,
                dimension=Dimension.STP,
                source_adapter="snmp",
                properties={"bridge_priority": bridge_priority},
            ))

        # Per-port STP state. Walk dot1dBasePortIfIndex first so we can
        # translate the STP-table key (dot1dBasePortNumber, NOT ifIndex)
        # back into the real ifName via if_map. Without this every STP
        # link would say "port-<basePortNum>" instead of "Ethernet1/46".
        base_port_rows  = await sess.walk(OID_DOT1D_BASE_PORT_IF)
        base_to_ifindex: dict[str, str] = {}
        for oid, val in base_port_rows:
            base_num = oid.rsplit(".", 1)[-1]
            ifidx = str(val).strip()
            if ifidx.isdigit():
                base_to_ifindex[base_num] = ifidx

        port_state_rows = await sess.walk(OID_STP_PORT_STATE)
        port_role_rows  = await sess.walk(OID_STP_PORT_ROLE)
        port_cost_rows  = await sess.walk(OID_STP_PORT_PATH_COST)
        port_desig_root = await sess.walk(OID_STP_PORT_DESIG_ROOT)

        port_states: dict[str, str] = {}
        for oid, val in port_state_rows:
            port_num = oid.rsplit(".", 1)[-1]
            port_states[port_num] = STP_PORT_STATES.get(str(val), str(val))

        port_roles: dict[str, str] = {}
        for oid, val in port_role_rows:
            port_num = oid.rsplit(".", 1)[-1]
            # Try IEEE first, then Cisco encoding
            role_str = STP_PORT_ROLES_IEEE.get(str(val)) or STP_PORT_ROLES_CISCO.get(str(val), str(val))
            port_roles[port_num] = role_str

        port_costs: dict[str, int] = {}
        for oid, val in port_cost_rows:
            port_num = oid.rsplit(".", 1)[-1]
            try:
                port_costs[port_num] = int(val)
            except Exception:
                pass

        # Designated root per-port (to detect which ports connect to another bridge)
        port_desig_roots: dict[str, str | None] = {}
        for oid, val in port_desig_root:
            port_num = oid.rsplit(".", 1)[-1]
            port_desig_roots[port_num] = _mac_from_bridge_id(val)

        seen_stp_ifaces: set[str] = set()
        # Emit STP_LINK edges for ports in interesting states. port_num
        # here is dot1dBasePortNumber — translate to ifIndex first, then
        # look up the ifName. Devices that don't expose the base→ifIndex
        # table fall back to the legacy "use base num as ifIndex" path so
        # we don't regress anyone who was working before.
        for port_num, state in port_states.items():
            if state in ("disabled",):
                continue
            ifindex = base_to_ifindex.get(port_num, port_num)
            if_info = if_map.get(ifindex, {})
            if_name = if_info.get("name", f"port-{port_num}")
            role = port_roles.get(port_num, "unknown")
            cost = port_costs.get(port_num, 0)

            # Key Interface nodes by the canonical dev_node_id so the
            # STP poll MERGEs into the same Interface node that the main
            # counter poll already created (and HAS_INTERFACE-linked to
            # the Device).  Without this the STP_LINK edge anchored to an
            # orphan Interface and the `_decorate_physical_links_stp`
            # join — which traverses Device→HAS_INTERFACE→Interface
            # →STP_LINK — never matched anything.
            iface_node_id = f"snmp-if:{dev_node_id}:{if_name}"
            if iface_node_id not in seen_stp_ifaces:
                seen_stp_ifaces.add(iface_node_id)
                data.nodes.append(GraphNode(
                    id=iface_node_id,
                    type=NodeType.INTERFACE,
                    dimensions=[Dimension.STP, Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties={"name": if_name, "device_id": dev_node_id},
                ))
                # Also stamp the HAS_INTERFACE relationship.  The counter
                # poll usually does this for us, but emit defensively so
                # the STP decorator works even on devices where the
                # counter poll skipped this port (e.g. management-only).
                data.edges.append(GraphEdge(
                    source_id=dev_node_id,
                    target_id=iface_node_id,
                    type=EdgeType.HAS_INTERFACE,
                    dimension=Dimension.PHYSICAL,
                    source_adapter="snmp",
                ))

            data.edges.append(GraphEdge(
                source_id=iface_node_id,
                target_id=domain_id,
                type=EdgeType.STP_LINK,
                dimension=Dimension.STP,
                source_adapter="snmp",
                properties={
                    "port_state": state,
                    "port_role": role,
                    "path_cost": cost,
                    "port_num": int(port_num) if port_num.isdigit() else 0,
                },
            ))

        log.debug("snmp.stp_done", host=sess.host,
                  is_root=is_root, ports=len(port_states))
    except Exception as exc:
        log.warning("snmp.stp_failed", host=sess.host, error=str(exc))
    return data


async def _poll_lldp(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
    max_neighbors: int = 500,
    discovered_by: str = "snmp",
) -> GraphData:
    """LLDP neighbors → PHYSICAL_LINK edges.

    Caps at ``max_neighbors`` unique remote system names to avoid graph bloat
    when border/core switches see thousands of WAN-side LLDP entries.
    Uses a set for O(1) deduplication instead of a linear scan.
    """
    data = GraphData(adapter_id="snmp")
    try:
        name_rows     = await sess.walk(OID_LLDP_REM_SYSNAME)
        port_rows     = await sess.walk(OID_LLDP_REM_PORTID)
        desc_rows     = await sess.walk(OID_LLDP_REM_PORTDESC)
        chassis_rows  = await sess.walk(OID_LLDP_REM_CHASSIS_ID)
        subtype_rows  = await sess.walk(OID_LLDP_REM_CHASSIS_SUBTYPE)
        mgmt_rows     = await sess.walk(OID_LLDP_REM_MGMT)

        port_map: dict[str, str] = {}
        for oid, val in port_rows:
            suffix = oid[len(OID_LLDP_REM_PORTID)+1:]
            port_map[suffix] = _decode_port_id(val)

        desc_map: dict[str, str] = {}
        for oid, val in desc_rows:
            suffix = oid[len(OID_LLDP_REM_PORTDESC)+1:]
            desc_map[suffix] = _decode_display_str(val)

        # Chassis-id subtype (per-row enum). Only subtype 4 (macAddress)
        # is directly resolvable against our MAC inventory — other
        # subtypes are kept in the stub metadata for forensics but not
        # used for the deterministic chassis-MAC merge in the correlator.
        chassis_subtype: dict[str, int] = {}
        for oid, val in subtype_rows:
            suffix = oid[len(OID_LLDP_REM_CHASSIS_SUBTYPE)+1:]
            try:
                chassis_subtype[suffix] = int(str(val).strip())
            except (TypeError, ValueError):
                pass

        chassis_map: dict[str, dict[str, str]] = {}
        for oid, val in chassis_rows:
            suffix = oid[len(OID_LLDP_REM_CHASSIS_ID)+1:]
            subtype = chassis_subtype.get(suffix, 0)
            raw = _decode_port_id(val)  # handles 6-byte MACs cleanly
            entry: dict[str, str] = {"raw": raw, "subtype": str(subtype)}
            if subtype == 4 and re.match(
                r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", raw.lower()
            ):
                entry["mac"] = raw.lower()
            chassis_map[suffix] = entry

        # Remote management address: index is
        # lldpRemTimeMark.lldpRemLocalPortNum.lldpRemIndex
        # .lldpRemManAddrSubtype.lldpRemManAddr… so we strip back to the
        # (time, port, rem) triple to match the per-neighbor suffix used
        # by the other walks. Only IPv4 (subtype 1, 4 bytes) and IPv6
        # (subtype 2, 16 bytes) are resolvable to a Device today; we
        # record the first one seen per neighbor.
        mgmt_map: dict[str, str] = {}
        for oid, val in mgmt_rows:
            suffix = oid[len(OID_LLDP_REM_MGMT)+1:]
            parts = suffix.split(".")
            if len(parts) < 5:
                continue
            triple = ".".join(parts[:3])
            if triple in mgmt_map:
                continue
            ip = _decode_ip_val(val)
            if ip:
                mgmt_map[triple] = ip

        seen_neighbors: set[str] = set()  # O(1) deduplication
        skipped = 0

        for oid, val in name_rows:
            suffix = oid[len(OID_LLDP_REM_SYSNAME)+1:]
            parts = suffix.split(".")
            if len(parts) < 3:
                continue
            local_port_idx = parts[1]
            remote_name = _decode_display_str(val).split(".")[0].strip()
            remote_port = port_map.get(suffix) or desc_map.get(suffix) or ""
            chassis = chassis_map.get(suffix, {})
            mgmt_ip = mgmt_map.get(suffix, "")

            # NetCortex philosophy: do not throw away observed state.
            # ``_is_valid_neighbor_name`` rejects short/garbled sysNames
            # (Cisco UCS Fabric Interconnects famously advertise just
            # ``A``/``B``), but if the remote ALSO advertised a chassis
            # MAC (subtype 4) or a management IP, the correlation engine
            # can still resolve it onto a real Device deterministically.
            # In that case keep the record but synthesise a stable,
            # globally-unique stub id from the chassis MAC / mgmt IP
            # — two different FIs both advertising "A" must NOT collide
            # on ``lldp-neighbor:A``.
            if not _is_valid_neighbor_name(remote_name):
                fallback_id = (chassis.get("mac") or mgmt_ip or "").strip().lower()
                if not fallback_id:
                    skipped += 1
                    continue
                neighbor_node_id = f"lldp-neighbor:by-id:{fallback_id}"
                # Preserve whatever the box did advertise so the UI has
                # SOMETHING to display until correlation merges this
                # stub onto the canonical Device.
                if not remote_name:
                    remote_name = fallback_id
            else:
                neighbor_node_id = f"lldp-neighbor:{remote_name}"

            if_info = if_map.get(local_port_idx, {})
            local_if = if_info.get("name", f"if-{local_port_idx}")
            if neighbor_node_id not in seen_neighbors:
                if len(seen_neighbors) >= max_neighbors:
                    continue
                seen_neighbors.add(neighbor_node_id)
                # Stub node — marked so inventory shows it as
                # discovery-only. The graph correlator will attempt to
                # merge it with a real Device when another adapter
                # discovers the same hostname OR when chassis_mac /
                # mgmt_ip resolve against our MAC/IP inventory.
                stub_props: dict[str, Any] = {
                    "name": remote_name,
                    "platform": "unknown",
                    "role": "other",
                    "stub": True,
                    "discovered_via": "lldp",
                    "discovered_by": discovered_by,
                }
                if chassis.get("mac"):
                    stub_props["chassis_mac"] = chassis["mac"]
                if chassis.get("raw"):
                    stub_props["chassis_id_raw"] = chassis["raw"]
                if chassis.get("subtype"):
                    stub_props["chassis_id_subtype"] = chassis["subtype"]
                if mgmt_ip:
                    stub_props["mgmt_ip"] = mgmt_ip
                data.nodes.append(GraphNode(
                    id=neighbor_node_id,
                    type=NodeType.DEVICE,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties=stub_props,
                ))

            # Canonicalize interface names so "Twe1/1/5" (LLDP) and
            # "TwentyFiveGigE1/1/5" (CDP/long form) collapse to the same
            # value, allowing the ingest layer to dedupe correctly.
            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=neighbor_node_id,
                type=EdgeType.PHYSICAL_LINK,
                dimension=Dimension.PHYSICAL,
                source_adapter="snmp",
                properties={
                    "interface_a": normalize_ifname(local_if),
                    "interface_b": normalize_ifname(remote_port),
                    "interface_a_raw": local_if,
                    "interface_b_raw": remote_port,
                    "discovery_proto": "lldp",
                },
            ))

        log.debug("snmp.lldp_done", host=sess.host, neighbors=len(name_rows),
                  unique=len(seen_neighbors), skipped=skipped,
                  capped=len(seen_neighbors) >= max_neighbors)
    except Exception as exc:
        log.warning("snmp.lldp_failed", host=sess.host, error=str(exc))
    return data


async def _poll_vlans_summary(sess: _SnmpSession) -> list[int]:
    """Return the configured VLAN-ID list for this device WITHOUT emitting
    any VLAN nodes or Device→VLAN edges.

    Used for Meraki hardware (MS/MX/MR/MV/MG/MT/CW/Z) where the device's
    full VLAN table is a *mirror* of the Meraki network's org-wide VLAN
    configuration — emitting one VLAN node per VID per MS would explode
    the topology with hundreds of diamonds that carry no per-device
    information.  Instead the caller stamps the returned list directly on
    the Device node as ``vlans_configured`` / ``vlan_count`` so the
    Explorer and detail panel still see the full footprint.

    The walk strategy is identical to ``_poll_vlans`` (CISCO-VTP-MIB
    first, Q-BRIDGE-MIB fallback) so the inventory is exactly the same
    set; only the graph emission is suppressed.
    """
    try:
        rows = await sess.walk(OID_VTP_VLAN_NAME)
        if not rows:
            rows = await sess.walk(OID_Q_VLAN_STATIC_NAME)
        seen: set[int] = set()
        for oid, _val in rows:
            suffix = oid.split(".")[-1] if oid else ""
            try:
                vid = int(suffix)
            except ValueError:
                continue
            if vid <= 0 or vid > 4094:
                continue
            if 1002 <= vid <= 1005:
                continue
            seen.add(vid)
        log.debug("snmp.vlans_summary_done", host=sess.host, vlans=len(seen))
        return sorted(seen)
    except Exception as exc:
        log.warning("snmp.vlans_summary_failed", host=sess.host, error=str(exc))
        return []


async def _poll_vlans(
    sess: _SnmpSession,
    dev_node_id: str,
    dev_name: str,
    source_adapter: str,
) -> GraphData:
    """Poll the VLAN inventory for this device.

    Tries CISCO-VTP-MIB first (works on Cisco IOS/IOS-XE/NX-OS) and falls
    back to Q-BRIDGE-MIB. Emits one ``VLAN`` node per discovered VLAN and
    a ``LOGICAL_MEMBER`` edge from the Device to the VLAN.

    VLAN node IDs are namespaced per device (``snmp-vlan:<dev>:<vid>``)
    intentionally — the correlation engine merges them into the canonical
    cross-device VLAN later. Without that namespacing two different sites
    using the same VLAN ID would collide.

    NOTE: For Meraki hardware (MS/MX/MR/etc.) we use ``_poll_vlans_summary``
    instead — see that function for the rationale.
    """
    data = GraphData(adapter_id="snmp")
    try:
        rows = await sess.walk(OID_VTP_VLAN_NAME)
        if not rows:
            rows = await sess.walk(OID_Q_VLAN_STATIC_NAME)

        seen: set[int] = set()
        for oid, val in rows:
            # VTP-MIB suffix is "<vtpDomainIdx>.<vlanId>"; Q-BRIDGE is just
            # "<vlanId>". Take the LAST integer in either case.
            suffix = oid.split(".")[-1] if oid else ""
            try:
                vid = int(suffix)
            except ValueError:
                continue
            # VLAN IDs > 4094 are extended-range reserved on Cisco and
            # the 1000-series reserved VTP VLANs (1002–1005) are FDDI/TR
            # placeholders we should skip.
            if vid <= 0 or vid > 4094:
                continue
            if 1002 <= vid <= 1005:
                continue
            if vid in seen:
                continue
            seen.add(vid)

            vlan_name = _decode_display_str(val).strip()
            vlan_node_id = f"snmp-vlan:{dev_node_id}:{vid}"
            data.nodes.append(GraphNode(
                id=vlan_node_id,
                type=NodeType.VLAN,
                dimensions=[Dimension.LOGICAL, Dimension.VIRTUAL],
                source_adapter=source_adapter,
                properties={
                    "name": vlan_name or f"VLAN{vid}",
                    "vlan_id": vid,
                    "vid": vid,
                    "device_id": dev_node_id,
                    "device_name": dev_name,
                },
            ))
            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=vlan_node_id,
                type=EdgeType.LOGICAL_MEMBER,
                dimension=Dimension.LOGICAL,
                source_adapter=source_adapter,
                properties={},
            ))
        log.debug("snmp.vlans_done", host=sess.host, vlans=len(seen))
    except Exception as exc:
        log.warning("snmp.vlans_failed", host=sess.host, error=str(exc))
    return data


async def _poll_cdp(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
    discovered_by: str = "snmp",
) -> GraphData:
    """CDP neighbors → PHYSICAL_LINK edges."""
    data = GraphData(adapter_id="snmp")
    try:
        name_rows     = await sess.walk(OID_CDP_NEIGHBOR_NAME)
        port_rows     = await sess.walk(OID_CDP_NEIGHBOR_PORT)
        addr_rows     = await sess.walk(OID_CDP_NEIGHBOR_ADDR)
        platform_rows = await sess.walk(OID_CDP_NEIGHBOR_PLATFORM)

        port_map: dict[str, str] = {}
        for oid, val in port_rows:
            suffix = oid[len(OID_CDP_NEIGHBOR_PORT)+1:]
            port_map[suffix] = _decode_port_id(val)

        # cdpCacheAddress is per (ifIndex, cacheIndex). Decode whatever the
        # device returned (IPv4 / IPv6 / raw bytes) into a plain string so
        # the correlator can match it against an IPAddress in our inventory.
        addr_map: dict[str, str] = {}
        for oid, val in addr_rows:
            suffix = oid[len(OID_CDP_NEIGHBOR_ADDR)+1:]
            ip = _decode_ip_val(val)
            if ip:
                addr_map[suffix] = ip

        platform_map: dict[str, str] = {}
        for oid, val in platform_rows:
            suffix = oid[len(OID_CDP_NEIGHBOR_PLATFORM)+1:]
            platform_map[suffix] = _decode_display_str(val)

        seen_neighbors: set[str] = set()
        skipped = 0
        for oid, val in name_rows:
            suffix = oid[len(OID_CDP_NEIGHBOR_NAME)+1:]
            parts = suffix.split(".")
            if len(parts) < 2:
                continue
            local_port_idx = parts[0]
            remote_name = _decode_display_str(val).split(".")[0]
            remote_port = port_map.get(suffix, "")
            remote_addr = addr_map.get(suffix, "")
            remote_platform = platform_map.get(suffix, "")

            # See the matching block in ``_poll_lldp`` for the rationale.
            # CDP rarely carries a chassis MAC the way LLDP does, so the
            # mgmt-IP fallback (``cdpCacheAddress``) is the only handle
            # we have for short/garbled names — but it's enough to let
            # ``_merge_neighbor_stubs_by_mgmt_ip`` resolve the stub.
            if not _is_valid_neighbor_name(remote_name):
                fallback_id = (remote_addr or "").strip().lower()
                if not fallback_id:
                    skipped += 1
                    continue
                neighbor_node_id = f"cdp-neighbor:by-id:{fallback_id}"
                if not remote_name:
                    remote_name = fallback_id
            else:
                neighbor_node_id = f"cdp-neighbor:{remote_name}"

            if_info = if_map.get(local_port_idx, {})
            local_if = if_info.get("name", f"if-{local_port_idx}")
            if neighbor_node_id not in seen_neighbors:
                seen_neighbors.add(neighbor_node_id)
                stub_props: dict[str, Any] = {
                    "name": remote_name,
                    "platform": remote_platform or "cisco",
                    "role": "other",
                    "stub": True,
                    "discovered_via": "cdp",
                    "discovered_by": discovered_by,
                }
                if remote_addr:
                    stub_props["mgmt_ip"] = remote_addr
                data.nodes.append(GraphNode(
                    id=neighbor_node_id,
                    type=NodeType.DEVICE,
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter="snmp",
                    properties=stub_props,
                ))

            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=neighbor_node_id,
                type=EdgeType.PHYSICAL_LINK,
                dimension=Dimension.PHYSICAL,
                source_adapter="snmp",
                properties={
                    "interface_a": normalize_ifname(local_if),
                    "interface_b": normalize_ifname(remote_port),
                    "interface_a_raw": local_if,
                    "interface_b_raw": remote_port,
                    "discovery_proto": "cdp",
                },
            ))

        log.debug("snmp.cdp_done", host=sess.host, neighbors=len(name_rows),
                  unique=len(seen_neighbors), skipped=skipped)
    except Exception as exc:
        log.warning("snmp.cdp_failed", host=sess.host, error=str(exc))
    return data


# ---------------------------------------------------------------------------
# Phase 5 — L3 routing protocol neighbors
# ---------------------------------------------------------------------------

def _ip_from_oid_suffix(suffix: str, n_octets: int = 4) -> str:
    """Extract a dotted-decimal IP from the last N octets of an OID suffix."""
    parts = suffix.split(".")
    if len(parts) < n_octets:
        return ""
    return ".".join(parts[-n_octets:])


async def _poll_ospf_neighbors(
    sess: _SnmpSession,
    dev_node_id: str,
    dev_name: str,
    max_peers: int = 200,
) -> GraphData:
    """OSPF neighbor table → ROUTING_PEER edges in the routing dimension.

    Edges are bidirectional-intent (source=local device, target=neighbor).
    The neighbor is identified by router-ID; if no matching Device node exists
    in Neo4j the target node will be a RoutingPeer stub that graph correlation
    can later merge with the real device when it is discovered.
    Capped at ``max_peers`` to prevent WAN-facing border routers from flooding
    the graph with hundreds of transient neighbors.
    """
    data = GraphData(adapter_id="snmp")
    try:
        nbr_ip_rows, nbr_rid_rows, nbr_state_rows = await asyncio.gather(
            sess.walk(OID_OSPF_NBR_IP),
            sess.walk(OID_OSPF_NBR_RTR_ID),
            sess.walk(OID_OSPF_NBR_STATE),
        )
        if not nbr_ip_rows:
            return data

        # ospfNbrTable index = <nbr-ip>.<addr-less-if-index>
        def _parse_ospf_suffix(oid: str, base: str) -> str:
            suffix = oid[len(base) + 1:]
            return _ip_from_oid_suffix(suffix, 4)

        nbr_states: dict[str, str] = {}
        for oid, val in nbr_state_rows:
            ip = _parse_ospf_suffix(oid, OID_OSPF_NBR_STATE)
            nbr_states[ip] = OSPF_NBR_STATES.get(str(val), str(val))
            # Also index by decoded IP in case format differs
            decoded = _decode_ip_val(ip)
            if decoded != ip:
                nbr_states[decoded] = nbr_states[ip]

        nbr_rids: dict[str, str] = {}
        for oid, val in nbr_rid_rows:
            ip = _parse_ospf_suffix(oid, OID_OSPF_NBR_RTR_ID)
            # ospfNbrRtrId is an IpAddress — decode properly (handles decimal ints)
            nbr_rids[ip] = _decode_ip_val(val)

        seen: set[str] = set()          # O(1) dedup by neighbor IP
        seen_nodes: set[str] = set()    # O(1) dedup by node ID

        for oid, val in nbr_ip_rows:
            nbr_ip = _decode_ip_val(val)
            if not nbr_ip or nbr_ip in seen:
                continue
            # Strict: must be a real IPv4/IPv6 address (rejects raw bytes/junk
            # like '\x07\xea\x05' that pysnmp 7.x used to emit for non-IpAddress
            # SNMP value types).
            try:
                addr = ipaddress.ip_address(nbr_ip)
                if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
                    continue
            except (ValueError, TypeError):
                log.debug("snmp.ospf.skip_bad_ip", host=sess.host, raw=repr(nbr_ip))
                continue
            seen.add(nbr_ip)
            if len(seen) > max_peers:
                break

            state = nbr_states.get(nbr_ip, "unknown")
            rid = nbr_rids.get(nbr_ip, nbr_ip)
            # Fall back to nbr_ip if rid is still garbage
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", rid):
                rid = nbr_ip

            target_id = f"routing-peer:ospf:{rid}"
            if target_id not in seen_nodes:
                seen_nodes.add(target_id)
                data.nodes.append(GraphNode(
                    id=target_id,
                    type=NodeType.ROUTING_PEER,
                    dimensions=[Dimension.ROUTING],
                    source_adapter="snmp",
                    properties={
                        "name": f"OSPF {rid}",
                        "protocol": "ospf",
                        "peer_ip": nbr_ip,
                        "router_id": rid,
                        "stub": True,
                    },
                ))

            edge_id = f"{dev_node_id}-OSPF-{target_id}"
            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=target_id,
                type=EdgeType.ROUTING_PEER,
                dimension=Dimension.ROUTING,
                source_adapter="snmp",
                properties={
                    "protocol": "ospf",
                    "peer_ip": nbr_ip,
                    "router_id": rid,
                    "state": state,
                    "id": edge_id,
                },
            ))

        log.debug("snmp.ospf_done", host=sess.host, neighbors=len(seen),
                  capped=len(seen) >= max_peers)
    except Exception as exc:
        log.debug("snmp.ospf_failed", host=sess.host, error=str(exc))
    return data


async def _poll_bgp_peers(
    sess: _SnmpSession,
    dev_node_id: str,
    dev_name: str,
    max_peers: int = 200,
) -> GraphData:
    """BGP4-MIB peer table → ROUTING_PEER edges in the routing dimension.

    Capped at ``max_peers`` to prevent internet-facing border routers with full
    BGP tables (400+ peers) from flooding the graph.
    """
    data = GraphData(adapter_id="snmp")
    try:
        state_rows, remote_as_rows = await asyncio.gather(
            sess.walk(OID_BGP_PEER_STATE),
            sess.walk(OID_BGP_PEER_REMOTE_AS),
        )
        if not state_rows:
            return data

        # bgpPeerTable index = <peer-ip> (4 octets appended to OID)
        def _peer_ip(oid: str, base: str) -> str:
            suffix = oid[len(base) + 1:]
            return _ip_from_oid_suffix(suffix, 4)

        remote_as: dict[str, int] = {}
        for oid, val in remote_as_rows:
            ip = _peer_ip(oid, OID_BGP_PEER_REMOTE_AS)
            try:
                remote_as[ip] = int(val)
            except Exception:
                pass

        seen: set[str] = set()           # O(1) dedup by peer IP
        seen_nodes: set[str] = set()     # O(1) dedup by node ID

        for oid, val in state_rows:
            peer_ip = _peer_ip(oid, OID_BGP_PEER_STATE)
            if not peer_ip or peer_ip in seen:
                continue
            # Strict: real IPv4/IPv6 only
            try:
                addr = ipaddress.ip_address(peer_ip)
                if (addr.is_unspecified or addr.is_loopback
                        or addr.is_multicast):
                    continue
            except (ValueError, TypeError):
                log.debug("snmp.bgp.skip_bad_ip", host=sess.host, raw=repr(peer_ip))
                continue
            seen.add(peer_ip)
            if len(seen) > max_peers:
                break

            state = BGP_PEER_STATES.get(str(val), str(val))
            asn = remote_as.get(peer_ip, 0)

            target_id = f"routing-peer:bgp:{peer_ip}"
            if target_id not in seen_nodes:
                seen_nodes.add(target_id)
                data.nodes.append(GraphNode(
                    id=target_id,
                    type=NodeType.ROUTING_PEER,
                    dimensions=[Dimension.ROUTING],
                    source_adapter="snmp",
                    properties={
                        "name": f"BGP {peer_ip}" + (f" AS{asn}" if asn else ""),
                        "protocol": "bgp",
                        "peer_ip": peer_ip,
                        "remote_as": asn,
                        "stub": True,
                    },
                ))

            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=target_id,
                type=EdgeType.ROUTING_PEER,
                dimension=Dimension.ROUTING,
                source_adapter="snmp",
                properties={
                    "protocol": "bgp",
                    "peer_ip": peer_ip,
                    "remote_as": asn,
                    "state": state,
                },
            ))

        log.debug("snmp.bgp_done", host=sess.host, peers=len(seen),
                  capped=len(seen) >= max_peers)
    except Exception as exc:
        log.debug("snmp.bgp_failed", host=sess.host, error=str(exc))
    return data


async def _poll_eigrp_neighbors(
    sess: _SnmpSession,
    dev_node_id: str,
    dev_name: str,
    if_map: dict[str, dict[str, Any]],
) -> GraphData:
    """CISCO-EIGRP-MIB neighbor table → ROUTING_PEER edges."""
    data = GraphData(adapter_id="snmp")
    try:
        addr_rows, holdtime_rows = await asyncio.gather(
            sess.walk(OID_EIGRP_NBR_ADDR),
            sess.walk(OID_EIGRP_NBR_HOLDTIME),
        )
        if not addr_rows:
            return data

        holdtimes: dict[str, int] = {}
        for oid, val in holdtime_rows:
            suffix = oid[len(OID_EIGRP_NBR_HOLDTIME) + 1:]
            try:
                holdtimes[suffix] = int(val)
            except Exception:
                pass

        seen: set[str] = set()
        seen_nodes: set[str] = set()
        for oid, val in addr_rows:
            nbr_ip = str(val)
            if not nbr_ip or nbr_ip in seen:
                continue
            seen.add(nbr_ip)

            suffix = oid[len(OID_EIGRP_NBR_ADDR) + 1:]
            # cEigrpNbrTable index = <AS>.<nbr-index>
            as_num = suffix.split(".")[0] if "." in suffix else "0"
            holdtime = holdtimes.get(suffix, 0)

            target_id = f"routing-peer:eigrp:{nbr_ip}"
            if target_id not in seen_nodes:
                seen_nodes.add(target_id)
                data.nodes.append(GraphNode(
                    id=target_id,
                    type=NodeType.ROUTING_PEER,
                    dimensions=[Dimension.ROUTING],
                    source_adapter="snmp",
                    properties={
                        "name": f"EIGRP {nbr_ip}",
                        "protocol": "eigrp",
                        "peer_ip": nbr_ip,
                        "as_number": as_num,
                    },
                ))

            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=target_id,
                type=EdgeType.ROUTING_PEER,
                dimension=Dimension.ROUTING,
                source_adapter="snmp",
                properties={
                    "protocol": "eigrp",
                    "peer_ip": nbr_ip,
                    "as_number": as_num,
                    "holdtime": holdtime,
                },
            ))

        log.debug("snmp.eigrp_done", host=sess.host, neighbors=len(seen))
    except Exception as exc:
        log.debug("snmp.eigrp_failed", host=sess.host, error=str(exc))
    return data


def _parse_inet_address_index(suffix: str) -> tuple[str, int] | None:
    """Parse an RFC 4293 InetAddress OID-index suffix into (ip_string, version).

    Suffix format (dotted-decimal OID parts):
        {InetAddressType}.{addr-len}.{addr-byte}.{addr-byte}...

    Returns None if the suffix is malformed.
    """
    try:
        parts = [int(p) for p in suffix.split(".") if p]
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    addr_type = parts[0]
    addr_len = parts[1]
    addr_bytes = parts[2:2 + addr_len]
    if len(addr_bytes) != addr_len:
        return None
    try:
        if addr_type == 1 and addr_len == 4:
            return (str(ipaddress.IPv4Address(bytes(addr_bytes))), 4)
        if addr_type == 2 and addr_len == 16:
            return (str(ipaddress.IPv6Address(bytes(addr_bytes))), 6)
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None


def _extract_prefix_length_from_oid(value: Any) -> int | None:
    """ipAddressPrefix value is an OID pointing to an ipAddressPrefixEntry row.
    The LAST integer of that OID is the prefix length.
    """
    s = str(value)
    if not s or s == "0.0":  # 0.0 = no prefix info
        return None
    try:
        parts = s.strip(".").split(".")
        last = int(parts[-1])
    except (ValueError, IndexError):
        return None
    if 0 <= last <= 128:
        return last
    return None


async def _poll_ip_addresses_rfc4293(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
) -> tuple[list[dict], bool]:
    """Walk the modern unified ipAddressTable (RFC 4293).

    Returns (entries, supported) where supported=False signals we should fall
    back to the legacy ipAddrTable for IPv4.

    Each entry: {"address", "version", "if_index", "prefix_len"}
    """
    entries: list[dict] = []
    try:
        if_rows = await sess.walk(OID_IP_ADDRESS_IF_IDX)
    except Exception as exc:
        log.debug("snmp.ip4293.if_idx_failed", host=sess.host, error=str(exc))
        return [], False

    if not if_rows:
        return [], False

    # Build a per-address map of (ip, version, if_index)
    if_by_idx: dict[str, tuple[str, int, str]] = {}
    for oid, val in if_rows:
        suffix = oid[len(OID_IP_ADDRESS_IF_IDX) + 1:]
        parsed = _parse_inet_address_index(suffix)
        if not parsed:
            continue
        ip_str, version = parsed
        if_by_idx[suffix] = (ip_str, version, str(val))

    if not if_by_idx:
        return [], True  # MIB present but no addresses (rare)

    # Walk ipAddressPrefix to get prefix length per address
    pfx_by_idx: dict[str, int] = {}
    try:
        pfx_rows = await sess.walk(OID_IP_ADDRESS_PREFIX)
        for oid, val in pfx_rows:
            suffix = oid[len(OID_IP_ADDRESS_PREFIX) + 1:]
            pfxlen = _extract_prefix_length_from_oid(val)
            if pfxlen is not None:
                pfx_by_idx[suffix] = pfxlen
    except Exception as exc:
        log.debug("snmp.ip4293.prefix_failed", host=sess.host, error=str(exc))

    for suffix, (ip_str, version, if_idx) in if_by_idx.items():
        pfx = pfx_by_idx.get(suffix)
        if pfx is None:
            pfx = 24 if version == 4 else 64  # sensible default
        entries.append({
            "address": ip_str, "version": version,
            "if_index": if_idx, "prefix_len": pfx,
        })
    return entries, True


async def _poll_ip_addresses_legacy_v4(
    sess: _SnmpSession,
) -> list[dict]:
    """Walk the legacy IPv4-only ipAddrTable (RFC 1213).
    Returns the same shape as _poll_ip_addresses_rfc4293.
    """
    entries: list[dict] = []
    try:
        addr_rows, if_rows, mask_rows = await asyncio.gather(
            sess.walk(OID_IP_ADDR_ADDR_V4),
            sess.walk(OID_IP_ADDR_IF_V4),
            sess.walk(OID_IP_ADDR_MASK_V4),
        )
    except Exception as exc:
        log.debug("snmp.ip_legacy.failed", host=sess.host, error=str(exc))
        return []

    if_by_ip: dict[str, str] = {}
    for oid, val in if_rows:
        suffix = oid[len(OID_IP_ADDR_IF_V4) + 1:]
        if_by_ip[suffix] = str(val)

    mask_by_ip: dict[str, str] = {}
    for oid, val in mask_rows:
        suffix = oid[len(OID_IP_ADDR_MASK_V4) + 1:]
        mask_by_ip[suffix] = _decode_ip_val(val)

    seen: set[str] = set()
    for oid, val in addr_rows:
        suffix = oid[len(OID_IP_ADDR_ADDR_V4) + 1:]
        ip_str = _decode_ip_val(val) or suffix
        if not ip_str or ip_str in seen:
            continue
        seen.add(ip_str)
        if_idx = if_by_ip.get(ip_str, "")
        mask = mask_by_ip.get(ip_str, "")
        pfx = 24
        if mask:
            try:
                pfx = ipaddress.IPv4Network(f"0.0.0.0/{mask}", strict=False).prefixlen
            except (ValueError, ipaddress.NetmaskValueError):
                pass
        entries.append({
            "address": ip_str, "version": 4,
            "if_index": if_idx, "prefix_len": pfx,
        })
    return entries


async def _poll_ip_addresses(
    sess: _SnmpSession,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
) -> GraphData:
    """Collect IPv4 and IPv6 addresses on the device.

    Strategy: try RFC 4293 ipAddressTable first (unified v4+v6, widely supported
    on modern Cisco IOS-XE/NX-OS/IOS-XR).  If that table is absent, fall back to
    the legacy IPv4-only ipAddrTable.  Each address becomes an IPAddress node
    linked to its Interface via ASSIGNED_IP, and contributes to a shared Prefix
    node which lets us see which devices share the same subnet in the routing
    dimension.
    """
    data = GraphData(adapter_id="snmp")
    rfc_entries, rfc_supported = await _poll_ip_addresses_rfc4293(sess, dev_node_id, if_map)
    if rfc_supported and rfc_entries:
        entries = rfc_entries
        source_mib = "rfc4293"
    else:
        entries = await _poll_ip_addresses_legacy_v4(sess)
        source_mib = "rfc1213"

    seen_ip_nodes: set[str] = set()
    seen_prefix_nodes: set[str] = set()

    for e in entries:
        ip_str = e["address"]
        version = e["version"]
        if_idx = e["if_index"]
        pfx = e["prefix_len"]
        try:
            addr = ipaddress.ip_address(ip_str)
            if addr.is_loopback or addr.is_unspecified:
                continue
            if version == 6 and addr.is_link_local:
                continue
        except ValueError:
            continue

        try:
            if version == 4:
                net = ipaddress.IPv4Network(f"{ip_str}/{pfx}", strict=False)
            else:
                net = ipaddress.IPv6Network(f"{ip_str}/{pfx}", strict=False)
            prefix_str = str(net)
        except (ValueError, ipaddress.NetmaskValueError):
            prefix_str = ""

        if_info = if_map.get(if_idx, {})
        if_name = if_info.get("name", f"if-{if_idx}")
        # Canonical dev_node_id keying (see _poll_cam_table) so this
        # Interface MERGEs with the counter-poll's node.
        iface_node_id = f"snmp-if:{dev_node_id}:{if_name}"
        ip_node_id = f"ip:{ip_str}"

        if ip_node_id in seen_ip_nodes:
            continue
        seen_ip_nodes.add(ip_node_id)

        data.nodes.append(GraphNode(
            id=ip_node_id,
            type=NodeType.IP_ADDRESS,
            dimensions=[Dimension.ROUTING, Dimension.PHYSICAL],
            source_adapter="snmp",
            properties={
                "address": ip_str,
                "version": version,       # store as int now
                "subnet": prefix_str,
                "device": sess.host,
                "device_node_id": dev_node_id,
            },
        ))
        data.edges.append(GraphEdge(
            source_id=iface_node_id,
            target_id=ip_node_id,
            type=EdgeType.ASSIGNED_IP,
            dimension=Dimension.ROUTING,
            source_adapter="snmp",
            properties={"subnet": prefix_str, "version": version},
        ))

        if prefix_str:
            prefix_node_id = f"prefix:{prefix_str}"
            if prefix_node_id not in seen_prefix_nodes:
                seen_prefix_nodes.add(prefix_node_id)
                data.nodes.append(GraphNode(
                    id=prefix_node_id,
                    type=NodeType.PREFIX,
                    dimensions=[Dimension.ROUTING],
                    source_adapter="snmp",
                    properties={"prefix": prefix_str, "version": version},
                ))
            data.edges.append(GraphEdge(
                source_id=dev_node_id,
                target_id=prefix_node_id,
                type=EdgeType.ROUTES_TO,
                dimension=Dimension.ROUTING,
                source_adapter="snmp",
                properties={"interface": if_name, "ip": ip_str, "version": version},
            ))

    log.debug("snmp.ip_addr_done",
              host=sess.host,
              source=source_mib,
              v4=sum(1 for e in entries if e["version"] == 4),
              v6=sum(1 for e in entries if e["version"] == 6),
              total=len(seen_ip_nodes))
    return data


# ---------------------------------------------------------------------------
# Meraki cloud SNMP session (non-standard port)
# ---------------------------------------------------------------------------

class _MerakiCloudSession(_SnmpSession):
    """SNMPv3 session targeting a Meraki Dashboard cloud endpoint on a custom port."""

    def __init__(
        self,
        host: str,
        port: int,
        creds: SnmpV3Creds,
        timeout: int,
    ) -> None:
        self.host = host
        self._port = port
        self.creds = creds
        self.timeout = timeout

    async def walk(self, oid: str) -> list[tuple[str, Any]]:
        """Walk using the cloud port instead of the default 161."""
        try:
            return await _snmp_walk_v3_port(
                self.host, self._port, self.creds, oid, self.timeout
            )
        except Exception as exc:
            log.debug("snmp.cloud_walk.error",
                      host=self.host, port=self._port, oid=oid, error=str(exc))
            return []


async def _snmp_walk_v3_port(
    host: str,
    port: int,
    creds: SnmpV3Creds,
    oid: str,
    timeout: int = 5,
) -> list[tuple[str, Any]]:
    """Walk using SNMPv3 on a non-standard port (for Meraki cloud endpoints)."""
    return await _snmp_walk_v3(host, creds, oid, timeout=timeout, port=port)


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------

class SnmpAdapter(PlatformAdapter):
    """SNMP v3/v2c adapter — device-level + Meraki cloud SNMP polling.

    Meraki SNMP architecture (two distinct planes):
      - Device-level:  Poll individual device mgmt IPs directly.
                       SNMPv3 only supports DES for privacy at this level
                       (AES is NOT available). Credential path:
                       snmp/adapter/meraki_device → snmp/adapter/meraki → snmp/default
      - Cloud/Dashboard: Poll Meraki's per-org SNMP proxy endpoint
                       (e.g. 123456.snmp.meraki.com:16100). Supports AES128/256.
                       Host/port/security-name/auth-mode/priv-mode come from the
                       Meraki Dashboard API; passwords come from SM path:
                       snmp/adapter/meraki_cloud → snmp/default

    Credentials for all other platforms resolved via:
      snmp/device/{name} → snmp/adapter/{type} → snmp/default

    Config keys (all optional):
      timeout:          int   — SNMP timeout seconds (default 2)
      max_concurrent:   int   — parallel hosts (default 20)
      collect_cam:      bool  — CAM table (default true)
      collect_arp:      bool  — ARP table (default true)
      collect_stp:      bool  — STP state (default true)
      collect_lldp:     bool  — LLDP neighbors (default true)
      collect_cdp:      bool  — CDP neighbors (default true)
      meraki_cloud:     bool  — Enable Meraki cloud SNMP polling (default true)
      targets:          list  — explicit IP list; if empty, reads from graph
    """

    name = "snmp"
    display_name = "SNMP"
    profile = PlatformProfile(
        device_id_field="mgmt_ip",
        role_map={},
        native_topology=True,
        provides_oper_status=True,
        default_access_methods=["snmp"],
        supported_dimensions=["physical", "stp"],
    )

    def __init__(self, config: dict, instance_name: str = "default") -> None:
        self.instance_name = instance_name
        self._targets: list[str] = config.get("targets", [])
        self._timeout: int = int(config.get("timeout", 2))
        # Per-device concurrency. Each SNMP walk runs as an isolated
        # snmpbulkwalk subprocess (kernel-level timeout via SIGKILL),
        # so we can safely run many in parallel without dispatcher
        # bottlenecks.  15 simultaneous device polls × ~9 parallel
        # topology walks = ~135 subprocesses peak, well within OS limits.
        self._max_concurrent: int = int(config.get("max_concurrent", 15))
        # Per-walk timeout (one MIB walk).  Wraps the wall-clock budget
        # for the snmpbulkwalk subprocess.  30s is comfortable for big
        # core switches on slow links.
        self._walk_timeout: float = float(config.get("walk_timeout", 30.0))
        # Per-device timeout (full poll: ifTable + CAM + ARP + STP + LLDP
        # + CDP + OSPF + BGP + IP + counters).  120s is comfortably above
        # the worst-case cumulative time on a busy core switch.
        self._device_timeout: float = float(config.get("device_timeout", 120.0))
        self._discover_timeout: float = float(config.get("discover_timeout", 600.0))
        # Whether to ONLY poll the explicit `targets` list from the secret
        # (skipping graph-discovered devices).  Defaults to False — the
        # process-wide shared SnmpEngine + walk semaphore fixed the
        # pysnmp 7.x deadlock, so graph-target polling is safe by default.
        self._only_manual_targets: bool = bool(
            config.get("only_manual_targets", False)
        )
        # Hard ceiling on graph-discovered targets per cycle, used when
        # only_manual_targets is False.  Default is high (250) since each
        # snmpbulkwalk subprocess is independent and the negative cache
        # quickly filters known-bad hosts on subsequent cycles.
        self._max_graph_targets: int = int(config.get("max_graph_targets", 250))
        # Absolute ceiling on the entire pass-1 (per-device) budget,
        # regardless of how many targets are queued.  Anything still in
        # flight when this fires is dropped; coverage is written for
        # whatever responded so far.  600s = 10 min, enough to attempt
        # ~250 devices at max_concurrent=15 with 30s/device average.
        self._pass1_max_seconds: float = float(
            config.get("pass1_max_seconds", 600.0)
        )
        # Allow override via secret; otherwise use defaults
        self._unreachable_prefixes: list[str] = (
            config.get("unreachable_prefixes")
            or self._UNREACHABLE_PREFIXES_DEFAULT
        )
        self._collect_cam: bool = config.get("collect_cam", True)
        self._collect_arp: bool = config.get("collect_arp", True)
        self._collect_stp: bool = config.get("collect_stp", True)
        self._collect_lldp: bool = config.get("collect_lldp", True)
        self._collect_cdp: bool = config.get("collect_cdp", True)
        self._collect_ospf: bool = config.get("collect_ospf", True)
        self._collect_bgp: bool = config.get("collect_bgp", True)
        self._collect_eigrp: bool = config.get("collect_eigrp", True)
        self._meraki_cloud: bool = config.get("meraki_cloud", True)
        self._fallback_community: str = config.get("community", "public")
        # ── Adaptive cadence (Phase B4) ──────────────────────────────────────
        # Topology MIBs (LLDP/CDP/STP/CAM/ARP/routing-protocol) change rarely,
        # so we poll them on a slow cycle while continuing to refresh
        # cheap/fast MIBs (interfaces, IP addresses) every cycle.  This is the
        # cleanest knob we can offer until Phase D adds dedicated counter
        # polling.
        #
        #   topology_interval_s  — how often a single device gets a full
        #                          topology walk (default 30 min)
        #   counter_interval_s   — placeholder for future per-counter cadence
        #                          (not yet wired — Phase D)
        self._topology_interval_s: float = float(
            config.get("topology_interval_s", 1800.0)
        )
        self._counter_interval_s: float = float(
            config.get("counter_interval_s", 60.0)
        )
        # IP -> monotonic timestamp of last successful topology poll
        self._last_topology_poll: dict[str, float] = {}
        # Profile matcher (Splunk-Connect-for-SNMP-inspired). Loaded lazily
        # on first discover() so the secret backend is available; cached
        # thereafter. None means "every group enabled with defaults".
        self._profile_matcher = None  # type: ignore[assignment]
        # Per-network Meraki direct-SNMP credentials, refreshed once per
        # discover() cycle from each Meraki org's
        # ``GET /networks/{netId}/snmp`` endpoint.  Keyed by
        # ``(instance_name, network_id)``.  See _fetch_meraki_network_snmp_creds
        # and _resolve_device_creds for the consumption path.
        self._meraki_network_snmp_creds: dict[tuple[str, str], dict[str, str]] = {}

    async def authenticate(self) -> None:
        """No separate auth step — credentials resolved per-device at poll time."""

    async def list_devices(self) -> list[NormalizedDevice]:
        return []

    async def list_interfaces(self, device_id: str) -> list[NormalizedInterface]:
        raise NotImplementedError

    async def list_vlans(self) -> list[NormalizedVLAN]:
        return []

    async def get_neighbors(self, device_id: str) -> list[NormalizedTopologyLink]:
        raise NotImplementedError

    # ── Profile resolution (Splunk-Connect-for-SNMP-inspired) ────────────────

    async def _refresh_profile_matcher(self, backend) -> None:
        """Reload SNMP polling profiles from the secrets backend.

        Profiles live at ``snmp/profiles`` as YAML/JSON. Missing secret →
        empty matcher (default profile only), which preserves the
        historical "walk everything" behavior.
        """
        from netcortex.snmp.profiles import ProfileMatcher
        raw = None
        try:
            raw = await backend.get("snmp/profiles", required=False)
        except Exception as exc:
            log.debug("snmp.profiles.fetch_failed", error=str(exc))
        if raw and isinstance(raw, dict) and "yaml" in raw:
            # Some backends stash the YAML as a string under a "yaml" key
            import yaml as _yaml
            try:
                raw = _yaml.safe_load(raw["yaml"]) or {}
            except Exception as exc:
                log.warning("snmp.profiles.yaml_parse_failed", error=str(exc))
                raw = None
        elif isinstance(raw, str):
            import yaml as _yaml
            try:
                raw = _yaml.safe_load(raw) or {}
            except Exception as exc:
                log.warning("snmp.profiles.yaml_parse_failed", error=str(exc))
                raw = None
        self._profile_matcher = ProfileMatcher.from_raw(raw)
        log.info("snmp.profiles.loaded",
                 instance=self.instance_id,
                 profiles=[p.name for p in self._profile_matcher.profiles])

    def _profile_for_target(self, target: dict[str, str]):
        """Resolve the polling profile for one target. Always non-None."""
        from netcortex.snmp.profiles import DeviceContext, ProfileMatcher
        if self._profile_matcher is None:
            self._profile_matcher = ProfileMatcher([])
        ctx = DeviceContext(
            ip=target.get("ip", ""),
            name=target.get("name", "") or "",
            model=target.get("model", "") or "",
            sys_object_id=target.get("sys_object_id", "") or "",
            source_adapter=target.get("source_adapter", "") or "",
        )
        return self._profile_matcher.resolve(ctx)

    # ── Credential resolution ────────────────────────────────────────────────

    async def _resolve_device_creds(
        self,
        resolver: SnmpCredentialResolver,
        dev_name: str | None,
        source_adapter: str | None,
        device_model: str | None = None,
        network_id: str | None = None,
    ) -> SnmpV2Creds | SnmpV3Creds:
        """Resolve device-level SNMP creds.

        Resolution order for Meraki hardware (MR/MS/MX/MV/MG/MT/CW/Z):
          1. Per-NETWORK creds fetched live from the Meraki Dashboard API
             (``GET /networks/{netId}/snmp``).  This is authoritative —
             the dashboard's per-network SNMP page IS what the devices
             accept.  Builds v3 creds with the SAME passphrase for both
             auth and priv, with hardcoded SHA-1 + DES (Meraki's only
             supported per-network v3 algorithms).
          2. Fall back to the secret-backend resolver (``snmp/device/*``,
             ``snmp/adapter/meraki_device``, ``snmp/default``).

        ``device_model`` is forwarded to the secret-backend resolver so
        DES-only privacy is forced only for actual Meraki hardware;
        third-party gear referenced through the Meraki API (Catalyst,
        Nexus, etc.) keeps whatever priv protocol its agent supports.
        """
        # Per-network Meraki creds (live from Dashboard API) take
        # precedence for actual Meraki hardware.  We deliberately do NOT
        # apply this path to third-party devices onboarded through Meraki
        # (e.g. Catalyst 9k in hybrid mode) — those run a standard
        # vendor SNMP agent that uses its own user/password pair.
        from netcortex.snmp.credentials import _is_meraki_hardware
        if (
            network_id
            and source_adapter
            and source_adapter.startswith("meraki/")
            and _is_meraki_hardware(device_model)
        ):
            instance_name = source_adapter.split("/", 1)[1]
            net_creds = (self._meraki_network_snmp_creds or {}).get(
                (instance_name, network_id)
            )
            if net_creds and net_creds.get("access") == "users":
                pp = net_creds["passphrase"]
                # Meraki per-network v3 uses the SAME passphrase for both
                # auth and priv, with hardcoded SHA-1 + DES.  Documented at
                # https://documentation.meraki.com/General_Administration/Monitoring_and_Reporting/SNMP_Overview_and_Configuration
                log.debug("snmp.creds.meraki_network",
                          host=dev_name, network_id=network_id)
                return SnmpV3Creds(
                    username=net_creds["user"],
                    auth_protocol="SHA",
                    auth_password=pp,
                    priv_protocol="DES",
                    priv_password=pp,
                    security_level="authPriv",
                )
            if net_creds and net_creds.get("access") == "community":
                log.debug("snmp.creds.meraki_network_v2c",
                          host=dev_name, network_id=network_id)
                return SnmpV2Creds(community=net_creds["community"])

        creds = await resolver.resolve(
            device_name=dev_name,
            source_adapter=source_adapter,
            context=SnmpContext.DEVICE,
            device_model=device_model,
        )
        return creds if creds is not None else SnmpV2Creds(community=self._fallback_community)

    # ── Graph-target resolution ──────────────────────────────────────────────

    # IP ranges we will never even *try* to SNMP — strictly the ones that
    # are NEVER routable to any real device (link-local, "this network",
    # IPv6 link-local).  NOTE: 100.64/10 (CG-NAT) is intentionally NOT
    # excluded: many networks use it as a routable overlay for management
    # plane traffic (Meraki cloud devices, CATC fabric, etc.).  If a host
    # in that range isn't reachable from the worker, SNMP will timeout
    # once and the negative cache pins it for `_NEGATIVE_CACHE_TTL`
    # seconds, so steady-state cost is minimal.
    _UNREACHABLE_PREFIXES_DEFAULT = [
        "169.254.0.0/16",  # link-local
        "0.0.0.0/8",       # "this network"
        "fe80::/10",       # IPv6 link-local
    ]

    def _is_pollable(self, ip_str: str) -> bool:
        """Filter out IPs we know we cannot reach via SNMP."""
        if not ip_str:
            return False
        try:
            addr = ipaddress.ip_address(ip_str)
            if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                return False
            for cidr in self._unreachable_prefixes:
                try:
                    if addr in ipaddress.ip_network(cidr, strict=False):
                        return False
                except (ValueError, ipaddress.AddressValueError):
                    continue
            return True
        except ValueError:
            return False

    # How long (seconds) to skip a device after it last failed to respond.
    # Refreshed every cycle for devices that DO respond.  Kept relatively
    # short so we recover quickly after fixing a config / network issue.
    _NEGATIVE_CACHE_TTL = 600

    async def _get_graph_targets(self) -> list[dict[str, str]]:
        """Query Neo4j for all Device nodes with a management IP.

        Filters applied (in order):
          1. RFC-unreachable prefix block (CG-NAT, link-local, etc.)
          2. Negative cache: devices that failed SNMP recently are skipped
             until `_NEGATIVE_CACHE_TTL` has elapsed since the last attempt.

        This prevents a worker from re-polling thousands of guaranteed-dead
        devices every cycle.
        """
        try:
            import time as _time
            from netcortex.graph.client import get_driver
            now = _time.time()
            stale_threshold = now - self._NEGATIVE_CACHE_TTL

            driver = get_driver()
            async with driver.session() as session:
                result = await session.run(
                    """
                    MATCH (d:Device)
                    WHERE (
                        d.mgmt_ip IS NOT NULL AND d.mgmt_ip <> ''
                        OR (d.candidate_ips IS NOT NULL AND size(d.candidate_ips) > 0)
                      )
                      AND (
                        d.snmp_polled IS NULL
                        OR d.snmp_polled = true
                        OR coalesce(d.snmp_polled_at, 0) < $stale_threshold
                      )
                    RETURN coalesce(d.mgmt_ip, '') AS ip,
                           d.id                    AS node_id,
                           d.name                  AS name,
                           d.source_adapter        AS source_adapter,
                           d.snmp_polled           AS last_ok,
                           coalesce(d.model, d.platform, '') AS model,
                           coalesce(d.candidate_ips, []) AS candidate_ips,
                           coalesce(d.networkId, '') AS network_id
                    LIMIT 2000
                    """,
                    stale_threshold=stale_threshold,
                )
                all_targets = []
                async for r in result:
                    # Build the list of IPs to attempt. mgmt_ip is always
                    # first (when present); additional candidate_ips —
                    # e.g. MX WAN/SD-WAN addresses — are tried in order
                    # if the primary fails.
                    ip_list: list[str] = []
                    primary = r["ip"] or ""
                    if primary:
                        ip_list.append(primary)
                    for cand in r["candidate_ips"] or []:
                        if cand and cand not in ip_list:
                            ip_list.append(str(cand))
                    if not ip_list:
                        continue
                    all_targets.append({
                        "ip":             ip_list[0],
                        "candidate_ips":  ip_list,
                        "node_id":        r["node_id"],
                        "name":           r["name"] or ip_list[0],
                        "source_adapter": r["source_adapter"] or "",
                        "last_ok":        r["last_ok"],
                        "model":          r["model"] or "",
                        # network_id is Meraki-specific; needed by the
                        # credential resolver to look up the per-network
                        # SNMP passphrase fetched from the Dashboard API.
                        "network_id":     r["network_id"] or "",
                    })
                pollable = [t for t in all_targets if self._is_pollable(t["ip"])]
                # Order: previously-OK devices first so transient errors don't
                # crowd them out under the cap.
                pollable.sort(key=lambda t: 0 if t.get("last_ok") else 1)
                skipped = len(all_targets) - len(pollable)
                if skipped:
                    log.info("snmp.graph_targets.filtered",
                             total=len(all_targets), pollable=len(pollable),
                             skipped_unreachable_range=skipped)
                return pollable
        except Exception as exc:
            log.warning("snmp.graph_targets.failed", error=str(exc))
            return []

    # ── Meraki cloud SNMP ────────────────────────────────────────────────────

    async def _fetch_meraki_cloud_snmp_endpoints(
        self,
        backend,
    ) -> list[dict]:
        """Query each Meraki adapter instance for its Dashboard SNMP endpoint.

        The Meraki API returns:
          hostname, port, v3User, v3AuthMode, v3PrivMode

        Only passwords come from the secrets backend.

        Returns a list of endpoint dicts:
          { host, port, org_id, instance_name, v3_user, v3_auth_mode, v3_priv_mode }
        """
        import httpx

        instances_cfg = await backend.get_adapter_index()
        meraki_instances = [
            i for i in instances_cfg
            if i.get("type") == "meraki" and i.get("enabled", True)
        ]

        endpoints: list[dict] = []
        for inst in meraki_instances:
            instance_name = inst["name"]
            try:
                cfg = await backend.get_adapter_config("meraki", instance_name)
                api_key = cfg.get("api_key", "")
                base_url = cfg.get("base_url", "https://api.meraki.com/api/v1").rstrip("/")
                org_id = cfg.get("org_id", "")
                verify_ssl = cfg.get("verify_ssl", True)
                if not api_key or not org_id:
                    log.debug("snmp.meraki_cloud.no_api_creds", instance=instance_name)
                    continue

                # follow_redirects=True is required for Meraki Gov and other
                # regional shards — they reply with HTTP 308 to redirect to
                # the assigned shard hostname (e.g. api-gov.meraki.com).
                async with httpx.AsyncClient(
                    verify=verify_ssl,
                    follow_redirects=True,
                    timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                ) as client:
                    r = await client.get(
                        f"{base_url}/organizations/{org_id}/snmp",
                        headers={"X-Cisco-Meraki-API-Key": api_key},
                    )

                if r.status_code != 200:
                    log.warning("snmp.meraki_cloud.api_error",
                                instance=instance_name,
                                status=r.status_code,
                                body=r.text[:200])
                    continue

                snmp_cfg = r.json()
                if not snmp_cfg.get("v3Enabled"):
                    log.debug("snmp.meraki_cloud.v3_disabled", instance=instance_name)
                    continue
                hostname = snmp_cfg.get("hostname", "")
                if not hostname:
                    log.debug("snmp.meraki_cloud.no_hostname", instance=instance_name)
                    continue

                peer_ips = snmp_cfg.get("peerIps") or []
                endpoints.append({
                    "host":         hostname,
                    "port":         snmp_cfg.get("port", 16100),
                    "org_id":       org_id,
                    "instance_name": instance_name,
                    "v3_user":      snmp_cfg.get("v3User", ""),
                    "v3_auth_mode": snmp_cfg.get("v3AuthMode", "SHA"),
                    "v3_priv_mode": snmp_cfg.get("v3PrivMode", "AES128"),
                    "peer_ips":     peer_ips,
                })
                log.info("snmp.meraki_cloud.endpoint_found",
                         instance=instance_name, host=hostname,
                         port=snmp_cfg.get("port", 16100),
                         auth=snmp_cfg.get("v3AuthMode"),
                         priv=snmp_cfg.get("v3PrivMode"),
                         v3_user=snmp_cfg.get("v3User", ""),
                         peer_ip_count=len(peer_ips))
                if not peer_ips:
                    # Empty peerIps in Meraki = no source-IP restriction
                    # (allow all).  Just an informational note.
                    log.info(
                        "snmp.meraki_cloud.peer_ips_unrestricted",
                        instance=instance_name,
                    )
            except Exception as exc:
                log.warning("snmp.meraki_cloud.fetch_failed",
                            instance=instance_name, error=str(exc))

        return endpoints

    async def _fetch_meraki_network_snmp_creds(
        self,
        backend,
    ) -> dict[tuple[str, str], dict[str, str]]:
        """For each Meraki org, fetch per-network direct-SNMP config.

        Meraki devices (MX/MS/MR/MV/MG/MT/CW/Z) accept direct SNMP polling
        when the per-NETWORK config (``GET /networks/{netId}/snmp``) has
        either v2c community or v3 users defined. Crucially, Meraki's per-
        network v3 model uses a SINGLE passphrase for both authentication
        and privacy — with hardcoded SHA-1 (auth) and DES (priv).  This is
        different from the org-level cloud SNMP, which supports AES.

        We fetch live every cycle so a passphrase rotation in the Meraki
        dashboard is picked up automatically without forcing the operator
        to also update AWS Secrets Manager.

        Returns ``{(instance_name, network_id): {access, user, passphrase,
        community}}``.  Only networks with at least one usable credential
        are included; everything else is silently skipped.
        """
        import httpx

        try:
            instances_cfg = await backend.get_adapter_index()
        except Exception as exc:
            log.warning("snmp.meraki_network.index_failed", error=str(exc))
            return {}

        meraki_instances = [
            i for i in (instances_cfg or [])
            if i.get("type") == "meraki" and i.get("enabled", True)
        ]
        creds_map: dict[tuple[str, str], dict[str, str]] = {}

        for inst in meraki_instances:
            instance_name = inst["name"]
            try:
                cfg = await backend.get_adapter_config("meraki", instance_name)
                api_key = cfg.get("api_key", "")
                base_url = cfg.get("base_url",
                                   "https://api.meraki.com/api/v1").rstrip("/")
                org_id = cfg.get("org_id", "")
                verify_ssl = cfg.get("verify_ssl", True)
                if not api_key or not org_id:
                    continue

                async with httpx.AsyncClient(
                    verify=verify_ssl,
                    follow_redirects=True,
                    headers={"X-Cisco-Meraki-API-Key": api_key,
                             "Accept": "application/json"},
                    timeout=httpx.Timeout(connect=5.0, read=20.0,
                                          write=5.0, pool=5.0),
                ) as client:
                    nets_resp = await client.get(
                        f"{base_url}/organizations/{org_id}/networks"
                    )
                    if nets_resp.status_code != 200:
                        log.warning(
                            "snmp.meraki_network.list_failed",
                            instance=instance_name,
                            status=nets_resp.status_code,
                        )
                        continue
                    networks = nets_resp.json() or []

                    # Bound concurrency to be polite to the Meraki API
                    # (default limit is 10 req/s per org).
                    sem = asyncio.Semaphore(5)

                    async def _fetch_one(net: dict) -> None:
                        nid = net.get("id") or ""
                        if not nid:
                            return
                        async with sem:
                            try:
                                r = await client.get(
                                    f"{base_url}/networks/{nid}/snmp"
                                )
                            except Exception as e:
                                log.debug(
                                    "snmp.meraki_network.snmp_failed",
                                    instance=instance_name,
                                    network=nid,
                                    error=str(e),
                                )
                                return
                        if r.status_code != 200:
                            return
                        snmp = r.json() or {}
                        access = (snmp.get("access") or "").lower()
                        if access == "users":
                            users = snmp.get("users") or []
                            if not users:
                                return
                            # Meraki only allows a single user per network
                            # but the API returns a list.  Take the first.
                            u = users[0]
                            user = u.get("username") or ""
                            pp = u.get("passphrase") or ""
                            if not user or not pp:
                                return
                            creds_map[(instance_name, nid)] = {
                                "access":     "users",
                                "user":       user,
                                "passphrase": pp,
                                "community":  "",
                            }
                        elif access == "community":
                            cs = snmp.get("communityString") or ""
                            if not cs:
                                return
                            creds_map[(instance_name, nid)] = {
                                "access":     "community",
                                "user":       "",
                                "passphrase": "",
                                "community":  cs,
                            }
                        # access == "none" or unknown → skip

                    await asyncio.gather(
                        *(_fetch_one(n) for n in networks),
                        return_exceptions=True,
                    )

                log.info(
                    "snmp.meraki_network.creds_loaded",
                    instance=instance_name,
                    org_id=org_id,
                    networks=len(networks),
                    with_creds=sum(
                        1 for k in creds_map if k[0] == instance_name
                    ),
                )
            except Exception as exc:
                log.warning("snmp.meraki_network.fetch_failed",
                            instance=instance_name, error=str(exc))

        return creds_map

    async def _poll_meraki_cloud_org(
        self,
        endpoint: dict,
        resolver: SnmpCredentialResolver,
        semaphore: asyncio.Semaphore,
    ) -> GraphData:
        """Poll one Meraki org via Dashboard SNMP endpoint.

        Uses cloud credentials (AES allowed) overlaid with metadata from the
        Meraki Dashboard API (auth mode, priv mode, security name).
        """
        host = endpoint["host"]
        port = endpoint["port"]
        instance_name = endpoint["instance_name"]
        data = GraphData(adapter_id=self.instance_id)

        async def _run() -> None:
            nonlocal data
            try:
                creds = await resolver.resolve_meraki_cloud_with_api_meta(
                    v3_user=endpoint["v3_user"],
                    v3_auth_mode=endpoint["v3_auth_mode"],
                    v3_priv_mode=endpoint["v3_priv_mode"],
                )
                if creds is None:
                    log.debug("snmp.meraki_cloud.no_creds", host=host)
                    return

                sess = _MerakiCloudSession(host, port, creds, self._timeout)

                # Meraki cloud SNMP does NOT expose the standard IF-MIB,
                # ARP, CAM, or LLDP tables.  Everything is under their
                # proprietary enterprise MIB (1.3.6.1.4.1.29671).  See
                # https://documentation.meraki.com/General_Administration/Monitoring_and_Reporting/SNMP_Overview_and_Configuration
                # We walk the Meraki devTable (29671.1.1.2) — each row
                # represents a device in the org, indexed by its internal
                # Meraki deviceID.  Existence of rows = the org is being
                # successfully polled via cloud SNMP.
                meraki_dev_rows = await asyncio.wait_for(
                    sess.walk("1.3.6.1.4.1.29671.1.1.2.1.2"),  # devName
                    timeout=60.0,
                )
                cloud_devices_seen = len(meraki_dev_rows)

                # Also walk the standard MIBs in case Meraki ever turns
                # them on (some shards do for legacy MX devices). These
                # are best-effort and don't gate coverage marking.
                if_map: dict[str, dict[str, Any]] = {}
                try:
                    if_map = await asyncio.wait_for(
                        _poll_interfaces(sess), timeout=30.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass

                tasks = []
                if self._collect_cam:
                    org_node_id = f"meraki-org:{instance_name}"
                    tasks.append(_poll_cam_table(sess, org_node_id, if_map))
                if self._collect_arp:
                    org_node_id = f"meraki-org:{instance_name}"
                    tasks.append(_poll_arp_table(sess, org_node_id, if_map))
                if self._collect_lldp:
                    org_node_id = f"meraki-org:{instance_name}"
                    tasks.append(_poll_lldp(
                        sess, org_node_id, if_map,
                        discovered_by=f"meraki-cloud-snmp/{instance_name}",
                    ))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for item in results:
                    if isinstance(item, Exception):
                        log.debug("snmp.meraki_cloud.task_failed",
                                  host=host, error=str(item))
                        continue
                    data = data.merge(item)

                # Mark per-device SNMP coverage from Meraki cloud SNMP.
                # The Meraki devTable confirms the org poll worked.  Since
                # the proprietary MIB indexes by an internal deviceID that
                # we don't have in Neo4j, we mark ALL devices belonging to
                # this Meraki adapter instance as snmp_polled=true with
                # snmp_source='meraki_cloud'.  This is honest: we DID
                # successfully poll the org-wide endpoint.
                if cloud_devices_seen > 0:
                    try:
                        await asyncio.wait_for(
                            self._mark_meraki_cloud_org_coverage(
                                instance_name=instance_name,
                                devices_in_org=cloud_devices_seen,
                            ),
                            timeout=15.0,
                        )
                    except asyncio.TimeoutError:
                        log.debug("snmp.meraki_cloud.coverage_timeout",
                                  host=host)
                    except Exception as exc:
                        log.debug("snmp.meraki_cloud.coverage_failed",
                                  host=host, error=str(exc))

                log.info("snmp.meraki_cloud.poll_done",
                         host=host, instance=instance_name,
                         nodes=len(data.nodes), edges=len(data.edges),
                         cloud_devices_seen=cloud_devices_seen)

                # Diagnose silent zero-rows: detect outbound IP so the user
                # can decide whether to add it to peerIps (when non-empty)
                # or look elsewhere (creds, org config).
                if cloud_devices_seen == 0:
                    try:
                        outbound_ip = await _detect_outbound_ip()
                    except Exception:
                        outbound_ip = "unknown"
                    peer_ips_cfg = endpoint.get("peer_ips") or []
                    if peer_ips_cfg and outbound_ip not in peer_ips_cfg:
                        log.warning(
                            "snmp.meraki_cloud.likely_blocked_by_peer_ips",
                            instance=instance_name,
                            configured_peer_ips=peer_ips_cfg,
                            our_outbound_ip=outbound_ip,
                            hint=("Cloud SNMP returned zero device rows. "
                                  "Our outbound IP is NOT in the Dashboard "
                                  "peerIps allow-list — add it under "
                                  "Organization > Settings > SNMP."),
                        )
                    else:
                        log.warning(
                            "snmp.meraki_cloud.zero_rows",
                            instance=instance_name,
                            our_outbound_ip=outbound_ip,
                            peer_ips=peer_ips_cfg or "unrestricted",
                            hint=("Cloud SNMP handshake succeeded but no "
                                  "device rows returned. Verify in Dashboard "
                                  "that this org has switches/APs/MX devices "
                                  "and that SNMP v3 is fully enabled."),
                        )
            except asyncio.TimeoutError:
                log.warning("snmp.meraki_cloud.poll_timeout",
                            host=host, instance=instance_name)
            except Exception as exc:
                log.warning("snmp.meraki_cloud.poll_failed",
                            host=host, instance=instance_name, error=str(exc))

        async with semaphore:
            try:
                await asyncio.wait_for(_run(), timeout=180.0)  # 3-min hard cap per org
            except asyncio.TimeoutError:
                log.warning("snmp.meraki_cloud.org_global_timeout",
                            host=host, instance=instance_name)

        return data

    async def _mark_meraki_cloud_org_coverage(
        self,
        instance_name: str,
        devices_in_org: int,
    ) -> None:
        """Stamp snmp_polled=true on EVERY Meraki device in this instance.

        Used when org-wide cloud SNMP succeeds (the Meraki proprietary
        MIB returns rows).  Since the proprietary MIB indexes devices by
        an internal Meraki deviceID that we don't have in Neo4j, we
        can't do per-device matching.  Instead, the success of the
        org-wide poll is taken as evidence that ALL devices in this
        adapter instance have current SNMP-equivalent telemetry
        available through the cloud endpoint.
        """
        import time as _time
        from netcortex.graph.client import get_driver

        ts = _time.time()
        try:
            driver = get_driver()
            async with driver.session() as session:
                # Track cloud coverage WITHOUT clobbering direct SNMP:
                # snmp_sources is a list that may contain 'direct' AND/OR
                # 'meraki_cloud'. snmp_source (legacy single-string field)
                # is derived from the two booleans so old UI code still
                # renders something sensible.
                result = await session.run(
                    """
                    MATCH (d:Device)
                    WHERE d.source_adapter = $src
                    SET d.snmp_polled = true,
                        d.snmp_polled_at = $ts,
                        d.snmp_last_status = 'cloud_ok',
                        d.snmp_cloud = true,
                        d.snmp_cloud_at = $ts,
                        d.snmp_sources = [src IN coalesce(d.snmp_sources, [])
                                          WHERE src <> 'meraki_cloud']
                                         + ['meraki_cloud'],
                        d.snmp_source = CASE
                            WHEN coalesce(d.snmp_direct, false) THEN 'direct+cloud'
                            ELSE 'meraki_cloud'
                        END,
                        // Cloud-only devices can never be deeply probed —
                        // mark health accordingly so the UI uses the right
                        // pill color and skips the "missing MIBs" tooltip.
                        d.snmp_health = CASE
                            WHEN coalesce(d.snmp_direct, false) THEN d.snmp_health
                            ELSE 'cloud_only'
                        END
                    RETURN count(d) AS n
                    """,
                    src=f"meraki/{instance_name}",
                    ts=ts,
                )
                rec = await result.single()
                marked = (rec["n"] if rec else 0)
            log.info(
                "snmp.meraki_cloud.coverage_marked_org",
                instance=instance_name,
                marked_devices=marked,
                devices_in_org_via_snmp=devices_in_org,
            )
        except Exception as exc:
            log.warning("snmp.meraki_cloud.coverage_failed",
                        instance=instance_name, error=str(exc))

    async def _mark_meraki_cloud_coverage(
        self,
        instance_name: str,
        serials: set[str],
        macs: set[str],
        names: set[str],
    ) -> None:
        """Stamp snmp_polled=true on Meraki devices we just learned about
        via cloud SNMP.

        We try matching by serial first (most reliable), then MAC, then
        name. Any device matching ANY of these gets a `snmp_polled=true,
        snmp_last_status='cloud_ok'` stamp so the inventory UI pill lights
        up exactly the way it does for direct-polled devices.
        """
        import time as _time
        from netcortex.graph.client import get_driver

        ts = _time.time()
        ok_count = 0
        try:
            driver = get_driver()
            async with driver.session() as session:
                # Match Meraki devices in this instance by any of the keys we
                # saw in the cloud SNMP walk.  Source-adapter is filtered to
                # this instance so we don't accidentally stamp CPNGOV
                # devices when polling CPN cloud.
                source_filter = f"meraki/{instance_name}"
                result = await session.run(
                    """
                    MATCH (d:Device)
                    WHERE d.source_adapter = $src
                      AND (
                            d.serial IN $serials
                         OR toLower(coalesce(d.mac, '')) IN $macs
                         OR d.name IN $names
                      )
                    SET d.snmp_polled = true,
                        d.snmp_polled_at = $ts,
                        d.snmp_last_status = 'cloud_ok',
                        d.snmp_cloud = true,
                        d.snmp_cloud_at = $ts,
                        d.snmp_sources = [src IN coalesce(d.snmp_sources, [])
                                          WHERE src <> 'meraki_cloud']
                                         + ['meraki_cloud'],
                        d.snmp_source = CASE
                            WHEN coalesce(d.snmp_direct, false) THEN 'direct+cloud'
                            ELSE 'meraki_cloud'
                        END,
                        d.snmp_health = CASE
                            WHEN coalesce(d.snmp_direct, false) THEN d.snmp_health
                            ELSE 'cloud_only'
                        END
                    RETURN count(d) AS n
                    """,
                    src=source_filter,
                    serials=list(serials),
                    macs=list(macs),
                    names=list(names),
                    ts=ts,
                )
                rec = await result.single()
                ok_count = (rec["n"] if rec else 0)

            log.info(
                "snmp.meraki_cloud.coverage_marked",
                instance=instance_name,
                marked_devices=ok_count,
                serials_seen=len(serials),
                macs_seen=len(macs),
                names_seen=len(names),
            )
        except Exception as exc:
            log.warning("snmp.meraki_cloud.coverage_failed",
                        instance=instance_name, error=str(exc))

    # ── Per-device poll ──────────────────────────────────────────────────────

    async def _poll_device(
        self,
        target: dict[str, str],
        resolver: SnmpCredentialResolver,
    ) -> tuple[GraphData, bool, dict[str, dict[str, Any]]]:
        """Poll one device.  Returns ``(GraphData, snmp_ok, coverage)`` where
        ``snmp_ok=True`` if the device responded to SNMP (at least one
        interface was discovered) and ``coverage`` is the per-MIB-family
        status map (possibly empty)."""
        dev_node_id = target["node_id"]
        dev_name = target.get("name", target.get("ip", ""))
        source_adapter = target.get("source_adapter", "")
        device_model = target.get("model", "")

        creds = await self._resolve_device_creds(
            resolver, dev_name, source_adapter,
            device_model=device_model,
            network_id=target.get("network_id") or None,
        )

        # Resolve polling profile (SC4SNMP-style). Applies per-host tunables
        # for PDU chunking (-Cr), ignore-not-increasing (-Cc), timeouts, and
        # gates which OID groups get walked.
        profile = self._profile_for_target(target)
        walk_timeout = profile.walk_timeout or self._walk_timeout

        # Try the primary mgmt_ip first, then any fallback addresses
        # (e.g. WAN/SD-WAN IPs for Meraki MX devices). The first one that
        # returns SNMP data wins; the rest are skipped to keep the cycle
        # bounded.
        candidate_ips = target.get("candidate_ips") or [target.get("ip", "")]
        sess: _SnmpSession | None = None
        if_map: dict[str, dict[str, Any]] = {}
        chosen_ip: str = ""
        for candidate in candidate_ips:
            if not candidate or not self._is_pollable(candidate):
                continue
            trial = _SnmpSession(
                candidate, creds, self._timeout,
                walk_timeout=walk_timeout,
                chunk_repetitions=profile.chunk_repetitions,
                ignore_not_increasing=profile.ignore_not_increasing,
            )
            if profile.includes("interface"):
                trial_if = await _poll_interfaces(trial)
            else:
                # No interface walk in the profile — probe sysName instead.
                trial_if = {"__sys__": {}} if await trial.walk(OID_SYS_NAME) else {}
            if trial_if:
                sess = trial
                if_map = trial_if if profile.includes("interface") else {}
                chosen_ip = candidate
                if candidate != candidate_ips[0]:
                    log.info("snmp.device.fallback_ip_ok",
                             host=dev_name, primary=candidate_ips[0],
                             used=candidate)
                break

        snmp_ok = sess is not None
        combined = GraphData(adapter_id=self.instance_id)
        coverage: dict[str, dict[str, Any]] = {}

        if not snmp_ok:
            log.debug("snmp.device.unreachable",
                      host=target.get("ip", dev_name),
                      profile=profile.name,
                      tried=len([c for c in candidate_ips if c]))
            return combined, False, coverage
        ip = chosen_ip
        assert sess is not None

        # Augment if_map with per-port VLAN membership while we still have
        # a fresh session — _poll_port_vlans is read-only on if_map (writes
        # vlans_access/vlans_allowed/trunk_mode/native_vlan keys) and the
        # Interface node builder picks those up. We do this every cycle,
        # not just on the topology cadence, so the L2 overlay never goes
        # blank in the UI.
        per_vlan_diag: dict[str, Any] = {}
        if profile.includes("interface") or profile.includes("vlan"):
            await _poll_port_vlans(sess, if_map)
            # dev19: backfill allowed-VLAN lists from per-VLAN STP context
            # walks. Cheap on devices configured for per-VLAN SNMP
            # contexts; a one-shot warning on devices that aren't. Skip
            # when nothing trunk-like was found (saves the vtpVlanState
            # walk on access-only edge switches).
            if profile.includes("stp") and self._collect_stp and any(
                v.get("trunk_mode") == "trunk" for v in if_map.values()
            ):
                per_vlan_diag = await _poll_per_vlan_stp(sess, if_map)

        # ── Adaptive cadence: decide whether this cycle should run the
        #    expensive topology walks or only the cheap "interface refresh"
        #    set.  Topology MIBs (LLDP/CDP/STP/CAM/ARP/routing-protocol)
        #    typically change minutes-to-hours apart, so we don't need to
        #    re-walk them on every cycle.
        import time as _time
        now = _time.monotonic()
        last_topo = self._last_topology_poll.get(ip, 0.0)
        do_topology = (now - last_topo) >= self._topology_interval_s
        log.debug(
            "snmp.poll.cadence",
            host=ip,
            do_topology=do_topology,
            since_last_s=round(now - last_topo, 1) if last_topo else None,
            topology_interval_s=self._topology_interval_s,
        )

        tasks = []
        # LLDP + CDP are cheap (3–6 short walks each) and MUST be polled
        # every cycle — the graph ingest layer purges every PHYSICAL_LINK
        # edge tagged with source_adapter=snmp before each ingest, so if
        # we only emit LLDP/CDP on the slow topology cadence the graph
        # would lose all neighbor edges between topology polls.
        # VLAN inventory is also cheap (one walk) and we depend on it for
        # the L2 overlay, so it joins this every-cycle bucket too.
        if self._collect_lldp and profile.includes("lldp"):
            tasks.append(_poll_lldp(sess, dev_node_id, if_map,
                                    discovered_by=self.instance_id))
        if self._collect_cdp and profile.includes("cdp"):
            tasks.append(_poll_cdp(sess, dev_node_id, if_map,
                                   discovered_by=self.instance_id))
        # VLAN inventory handling diverges by hardware family:
        #   * Meraki HW (MS/MX/MR/MV/MG/MT/CW/Z): the device's VLAN table
        #     is a mirror of the Meraki network's org-wide VLAN config
        #     (every MS in a network sees every configured VLAN).
        #     Emitting one VLAN node + Device→VLAN edge per VID per
        #     switch produces a 200-diamond starburst in the topology
        #     for every MS, even though no per-device information is
        #     conveyed.  Walk the same MIB but stash the result as a
        #     plain ``vlans_configured`` property on the Device — the
        #     Explorer / detail panel still see the full footprint.
        #   * Everything else (Catalyst, NX-OS, etc.): a Catalyst's
        #     VLAN database IS per-device state and worth surfacing as
        #     graph nodes — use the regular emitter.
        from netcortex.snmp.credentials import _is_meraki_hardware
        if (profile.includes("vlan") or profile.includes("logical")):
            if _is_meraki_hardware(device_model):
                # Sentinel; populated below before _poll_device returns.
                target["_meraki_vlans_pending"] = True
            else:
                tasks.append(_poll_vlans(sess, dev_node_id, dev_name,
                                         source_adapter=self.instance_id))

        # Heavy MIBs run on the slow topology cadence. CAM and ARP can
        # be hundreds of thousands of rows on a core switch; routing
        # tables can hold tens of thousands of peers; STP also goes here
        # because membership only changes on link-state events.
        if do_topology:
            if self._collect_cam and profile.includes("mac"):
                tasks.append(_poll_cam_table(sess, dev_node_id, if_map))
            if self._collect_arp and profile.includes("arp"):
                tasks.append(_poll_arp_table(sess, dev_node_id, if_map))
            if self._collect_stp and profile.includes("stp"):
                tasks.append(_poll_stp(sess, dev_node_id, dev_name, if_map))
            if self._collect_ospf and profile.includes("routing"):
                tasks.append(_poll_ospf_neighbors(sess, dev_node_id, dev_name))
            if self._collect_bgp and profile.includes("routing"):
                tasks.append(_poll_bgp_peers(sess, dev_node_id, dev_name))
            if self._collect_eigrp and profile.includes("routing"):
                tasks.append(
                    _poll_eigrp_neighbors(sess, dev_node_id, dev_name, if_map)
                )

        # Fast/light MIBs — IP addresses (v4 + v6) change rarely but are
        # very cheap (one walk). Always polled when interface group is on.
        if profile.includes("interface"):
            tasks.append(_poll_ip_addresses(sess, dev_node_id, if_map))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, Exception):
                log.debug("snmp.poll_task_failed", host=ip, error=str(item))
                continue
            combined = combined.merge(item)

        # Meraki HW VLAN summary — runs serially after the parallel task
        # gather because it's cheap (1-2 walks) and we want the result
        # stashed on the target before _write_snmp_status sees it.  See
        # the comment above the task gating for the full rationale.
        if target.pop("_meraki_vlans_pending", False):
            try:
                vids = await _poll_vlans_summary(sess)
            except Exception as exc:
                log.debug("snmp.vlans_summary_error", host=ip, error=str(exc))
                vids = []
            # Empty list is a legitimate signal (some MR APs return no
            # VLAN table at all); the writer skips updates when vids
            # is None, so we use [] to mean "polled, none found".
            target["meraki_vlans"] = vids

        # ── Phase D1: per-interface counters + health metrics ────────────
        # Polled every cycle (cheap — just 6 walks) so utilization and
        # error rates stay fresh between topology refreshes. Skipped if the
        # device profile didn't enable the interface group at all.
        try:
            from netcortex.adapters.snmp_counters import poll_interface_counters
            if not profile.includes("interface"):
                counters = {}
            else:
                try:
                    counters = await asyncio.wait_for(
                        poll_interface_counters(sess, dev_node_id, if_map),
                        timeout=walk_timeout,
                    )
                except asyncio.TimeoutError:
                    log.debug("snmp.counters.timeout", host=ip)
                    counters = {}
                except Exception as exc:
                    log.debug("snmp.counters.failed", host=ip, error=str(exc))
                    counters = {}

            # Build Interface nodes for EVERY ifindex in if_map, even when
            # the counter walk failed or returned empty. Without this the
            # ingest layer's per-adapter purge wipes HAS_INTERFACE edges
            # on the bad cycle and the L2 decoration pass has no Interface
            # nodes to JOIN against — trunk_mode / vlans_allowed get
            # silently lost between cycles.
            if profile.includes("interface") and if_map:
                synth_counters = {ifidx: counters.get(ifidx, {}) for ifidx in if_map}
                health_node = _build_interface_health_nodes(
                    dev_node_id, if_map, synth_counters,
                    source_adapter=self.instance_id,
                )
                combined = combined.merge(health_node)
        except Exception as exc:
            log.debug("snmp.interface_emit.failed", host=ip, error=str(exc))

        # ── MIB coverage probe ───────────────────────────────────────────
        # Cheap (~12 single-OID walks with 2 repetitions each, bounded
        # parallelism), but still run only on the topology cadence so a
        # busy cluster doesn't pay this on every fast cycle.  The full
        # coverage map is stamped on the Device node so the UI can show
        # "we tried, here's exactly what the agent let us see."
        if do_topology:
            try:
                coverage = await asyncio.wait_for(
                    sess.probe_coverage(per_probe_timeout_s=6.0),
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                log.debug("snmp.coverage.probe_timeout", host=ip)
                coverage = {}
            except Exception as exc:
                log.debug("snmp.coverage.probe_error", host=ip, error=str(exc))
                coverage = {}

        # Record successful topology refresh for next cadence decision
        if do_topology:
            self._last_topology_poll[ip] = now

        return combined, snmp_ok, coverage

    # ── Primary discovery ────────────────────────────────────────────────────

    async def _write_snmp_coverage(
        self,
        polled_ok: list[dict[str, str]],
        polled_all: list[dict[str, str]],
        coverage_by_ip: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        """Write snmp_polled / snmp_polled_at + per-MIB coverage onto Device
        nodes.

        Match strategy is order-of-preference per target:
          1. Exact `id` (when target.node_id matches an existing Device.id)
          2. `mgmt_ip` (when the device was registered by another adapter
             at the same IP).  This catches Meraki/CATC-discovered devices.

        Devices that match neither (typically manual SNMP-only targets reaching
        a host nothing else has discovered yet) are skipped here — they get a
        Device node created via :meth:`_ensure_snmp_device_nodes` after polling.

        ``coverage_by_ip`` is the per-MIB-family probe result from
        :meth:`_SnmpSession.probe_coverage`.  When supplied, we additionally
        stamp ``snmp_mib_coverage_json`` (a JSON string for portability across
        the Neo4j Python driver, which cannot store nested dicts as a single
        property), plus the derived enums ``snmp_health`` and
        ``snmp_missing_mibs`` / ``snmp_restricted_mibs`` so the UI can pick
        them up directly without re-parsing the JSON.
        """
        import json
        import time as _time
        from netcortex.graph.client import get_driver

        ts = _time.time()
        coverage_by_ip = coverage_by_ip or {}

        # Build a per-target enriched row.  We compute snmp_health here
        # (rather than as a Cypher CASE statement) because the derivation
        # depends on which families are flagged ``required`` in
        # ``MIB_COVERAGE_PROBES`` — keeping that logic in Python means there
        # is exactly one source of truth.
        ok_rows = []
        for t in polled_ok:
            cov = coverage_by_ip.get(t["ip"], {})
            health, missing, restricted = _derive_snmp_health(cov)
            # ``meraki_vlans`` is populated by _poll_device only for Meraki
            # hardware (where we walk the VLAN table for inventory but
            # deliberately do NOT emit one VLAN node + edge per VID — the
            # list is stamped directly on the Device as vlans_configured
            # so the topology stays clean).  None means "not a Meraki HW
            # poll this cycle"; empty list means "polled, no VLANs".
            meraki_vlans = t.get("meraki_vlans")
            ok_rows.append({
                "node_id": t["node_id"],
                "ip": t["ip"],
                "coverage_json": json.dumps(cov) if cov else "",
                "health": health if cov else "unknown",
                "missing": missing,
                "restricted": restricted,
                "coverage_at": ts if cov else None,
                "meraki_vlans": (
                    [int(v) for v in meraki_vlans]
                    if meraki_vlans is not None else None
                ),
                "has_meraki_vlans": meraki_vlans is not None,
            })

        all_ips = [t["ip"] for t in polled_all]
        ok_ips = {t["ip"] for t in polled_ok}
        failed_ips = [ip for ip in all_ips if ip not in ok_ips]

        try:
            driver = get_driver()
            async with driver.session() as session:
                if ok_rows:
                    # Track direct SNMP success WITHOUT clobbering meraki_cloud
                    # coverage: snmp_sources is a list that may contain both
                    # 'direct' and 'meraki_cloud' simultaneously.
                    await session.run(
                        """
                        UNWIND $rows AS row
                        OPTIONAL MATCH (byid:Device {id: row.node_id})
                        OPTIONAL MATCH (byip:Device {mgmt_ip: row.ip})
                        WITH coalesce(byid, byip) AS d, row
                        WHERE d IS NOT NULL
                        SET d.snmp_polled = true,
                            d.snmp_polled_at = $ts,
                            d.snmp_last_status = 'ok',
                            d.snmp_direct = true,
                            d.snmp_direct_at = $ts,
                            d.snmp_sources = [src IN coalesce(d.snmp_sources, [])
                                              WHERE src <> 'direct'] + ['direct'],
                            d.snmp_source = CASE
                                WHEN coalesce(d.snmp_cloud, false) THEN 'direct+cloud'
                                ELSE 'direct'
                            END
                        // Only stamp the coverage map when we actually ran a
                        // probe this cycle.  Otherwise we keep the last known
                        // map intact so the UI doesn't blink between
                        // "restricted" and "unknown" on fast cycles.
                        FOREACH (_ IN CASE WHEN row.coverage_json <> '' THEN [1] ELSE [] END |
                            SET d.snmp_mib_coverage_json = row.coverage_json,
                                d.snmp_mib_coverage_at   = row.coverage_at,
                                d.snmp_health            = row.health,
                                d.snmp_missing_mibs      = row.missing,
                                d.snmp_restricted_mibs   = row.restricted
                        )
                        // For Meraki hardware we stamp the SNMP-walked VLAN
                        // inventory directly on the Device node so the
                        // Explorer / detail panel sees the full footprint
                        // without us needing to emit hundreds of
                        // Device→VLAN edges that would explode the
                        // topology view.  See _poll_vlans_summary.
                        FOREACH (_ IN CASE WHEN row.has_meraki_vlans THEN [1] ELSE [] END |
                            SET d.vlans_configured = row.meraki_vlans,
                                d.vlan_count       = size(row.meraki_vlans),
                                d.vlans_source     = 'snmp_meraki'
                        )
                        """,
                        rows=ok_rows, ts=ts,
                    )
                if failed_ips:
                    # On direct-poll failure, REMOVE 'direct' from snmp_sources
                    # but preserve any cloud coverage marker.  We also stamp
                    # snmp_health=unreachable so the UI can color the pill
                    # red immediately, even on the first failed cycle (before
                    # a probe runs again).
                    await session.run(
                        """
                        UNWIND $ips AS ip
                        MATCH (d:Device {mgmt_ip: ip})
                        SET d.snmp_direct = false,
                            d.snmp_direct_at = $ts,
                            d.snmp_sources = [src IN coalesce(d.snmp_sources, [])
                                              WHERE src <> 'direct'],
                            d.snmp_polled = coalesce(d.snmp_cloud, false),
                            d.snmp_polled_at = $ts,
                            d.snmp_last_status = CASE
                                WHEN coalesce(d.snmp_cloud, false) THEN 'cloud_ok'
                                ELSE 'unreachable'
                            END,
                            d.snmp_source = CASE
                                WHEN coalesce(d.snmp_cloud, false) THEN 'meraki_cloud'
                                ELSE null
                            END,
                            d.snmp_health = CASE
                                WHEN coalesce(d.snmp_cloud, false) THEN 'cloud_only'
                                ELSE 'unreachable'
                            END
                        """,
                        ips=failed_ips, ts=ts,
                    )
            log.info(
                "snmp.coverage_write.done",
                ok=len(ok_rows),
                failed=len(failed_ips),
                probed=sum(1 for r in ok_rows if r["coverage_json"]),
            )
        except Exception as exc:
            log.warning("snmp.coverage_write.failed", error=str(exc))

    async def _ensure_snmp_device_nodes(
        self,
        polled_ok: list[dict[str, str]],
    ) -> None:
        """For SNMP-only targets that have no matching Device in the graph, create
        a Device node so they appear in inventory and STP/routing views.

        Idempotent — uses MERGE on (Device {mgmt_ip}).  Skips targets whose IP
        already matches an existing Device.
        """
        if not polled_ok:
            return
        import time as _time
        from netcortex.graph.client import get_driver
        ts = _time.time()
        try:
            driver = get_driver()
            async with driver.session() as session:
                await session.run(
                    """
                    UNWIND $rows AS row
                    // Skip if a device already exists at this management IP
                    OPTIONAL MATCH (existing:Device {mgmt_ip: row.ip})
                    WITH row, existing
                    WHERE existing IS NULL
                    MERGE (d:Device {id: row.node_id})
                    SET d.name = coalesce(d.name, row.name, row.ip),
                        d.mgmt_ip = row.ip,
                        d.source_adapter = coalesce(d.source_adapter, 'snmp'),
                        d.snmp_polled = true,
                        d.snmp_polled_at = $ts,
                        d.snmp_last_status = 'ok',
                        d.dimensions = ['physical']
                    """,
                    rows=[
                        {"node_id": t["node_id"], "ip": t["ip"],
                         "name": t.get("name") or t["ip"]}
                        for t in polled_ok
                    ],
                    ts=ts,
                )
        except Exception as exc:
            log.debug("snmp.ensure_device.failed", error=str(exc))

    async def discover(self) -> GraphData:
        """Poll all devices and Meraki cloud endpoints via SNMP.

        Wraps everything in a global timeout so a stuck SNMP cycle can never
        block the worker indefinitely.  Partial data is returned and ingested
        normally.
        """
        try:
            return await asyncio.wait_for(
                self._discover_inner(), timeout=self._discover_timeout
            )
        except asyncio.TimeoutError:
            log.warning("snmp.discover.global_timeout",
                        instance=self.instance_id, budget_s=self._discover_timeout)
            return GraphData(adapter_id=self.instance_id)

    async def _discover_inner(self) -> GraphData:
        from netcortex.secrets import get_secret_backend
        backend = get_secret_backend()
        resolver = SnmpCredentialResolver(backend)
        semaphore = asyncio.Semaphore(self._max_concurrent)
        data = GraphData(adapter_id=self.instance_id)

        # Refresh per-network Meraki SNMP credentials.  Used by
        # _resolve_device_creds() to authenticate directly to Meraki
        # hardware (MX/MS/MR/MV/MG/MT/CW/Z) without forcing the operator
        # to keep AWS Secrets Manager in sync with the Dashboard.  Cached
        # only for the duration of this discover() cycle.  Best-effort:
        # any failure leaves the map empty and we fall back to the
        # existing secret-based resolution.
        try:
            self._meraki_network_snmp_creds = (
                await self._fetch_meraki_network_snmp_creds(backend)
            )
        except Exception as exc:
            log.warning("snmp.meraki_network.creds_refresh_failed",
                        error=str(exc))
            self._meraki_network_snmp_creds = {}

        # Refresh polling profiles each cycle (cheap; secret backend is cached).
        # A failure here is non-fatal — we fall back to the catch-all default
        # which preserves pre-profile behavior.
        try:
            await self._refresh_profile_matcher(backend)
        except Exception as exc:
            log.warning("snmp.profiles.load_failed", error=str(exc))

        # ── Pass 1: device-level polling ────────────────────────────────────
        # Manual targets always take precedence.  Graph-discovered targets are
        # only used when `only_manual_targets=false` is set in the secret.
        targets: list[dict[str, str]] = []
        if self._targets:
            targets.extend([
                {"ip": ip, "node_id": f"snmp-device:{ip}",
                 "name": ip, "source_adapter": ""}
                for ip in self._targets
            ])
        if not self._only_manual_targets:
            graph_targets = await self._get_graph_targets()
            manual_ips = {t["ip"] for t in targets}
            extras = [t for t in graph_targets if t["ip"] not in manual_ips]
            if len(extras) > self._max_graph_targets:
                log.info("snmp.graph_targets.capped",
                         total=len(extras), kept=self._max_graph_targets)
                extras = extras[:self._max_graph_targets]
            targets.extend(extras)

        polled_ok: list[dict[str, str]] = []

        # Coverage map collected from probe walks this cycle, keyed by ip
        # (which uniquely identifies a polled target).
        coverage_by_ip: dict[str, dict[str, dict[str, Any]]] = {}

        if targets:
            async def _bounded_poll(
                target: dict[str, str],
            ) -> tuple[GraphData, bool, dict[str, str], dict[str, dict[str, Any]]]:
                async with semaphore:
                    try:
                        gd, ok, cov = await asyncio.wait_for(
                            self._poll_device(target, resolver),
                            timeout=self._device_timeout,
                        )
                    except asyncio.TimeoutError:
                        log.warning("snmp.device.poll_timeout", host=target.get("ip"))
                        gd, ok, cov = (
                            GraphData(adapter_id=self.instance_id),
                            False,
                            {},
                        )
                    return gd, ok, target, cov

            # Cap the entire pass-1 device polling so a stuck batch can't
            # block the discover loop forever, but COLLECT results as they
            # complete so partial coverage is preserved if pass1 times out.
            pass1_timeout = min(
                self._pass1_max_seconds,
                max(
                    120.0,
                    self._device_timeout
                    * (len(targets) / max(self._max_concurrent, 1))
                    * 1.3,
                ),
            )
            tasks = [asyncio.create_task(_bounded_poll(t)) for t in targets]
            try:
                async with asyncio.timeout(pass1_timeout):  # type: ignore[attr-defined]
                    for fut in asyncio.as_completed(tasks):
                        try:
                            gd, ok, tgt, cov = await fut
                        except Exception as exc:
                            log.warning("snmp.discover.device_poll_failed", error=str(exc))
                            continue
                        data = data.merge(gd)
                        if ok:
                            polled_ok.append(tgt)
                        if cov:
                            coverage_by_ip[tgt.get("ip", "")] = cov
            except (asyncio.TimeoutError, TimeoutError):
                # Cancel anything still running; partial polled_ok is preserved.
                still_pending = [t for t in tasks if not t.done()]
                log.warning(
                    "snmp.pass1.global_timeout",
                    elapsed_cap_s=pass1_timeout,
                    targets=len(targets),
                    completed=len(targets) - len(still_pending),
                    still_pending=len(still_pending),
                    polled_ok_so_far=len(polled_ok),
                )
                for t in still_pending:
                    t.cancel()
                # Brief wait for cancellation to propagate.
                await asyncio.sleep(0.5)
        else:
            log.info("snmp.discover.no_device_targets", instance=self.instance_id)

        # Persist SNMP coverage back to Neo4j (best-effort, 30s timeout)
        if targets:
            try:
                await asyncio.wait_for(
                    self._write_snmp_coverage(
                        polled_ok, targets, coverage_by_ip,
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                log.warning("snmp.coverage_write.timeout")

            # For SNMP-only targets that don't match any existing Device,
            # MERGE in a Device node so they show up in inventory.
            try:
                await asyncio.wait_for(
                    self._ensure_snmp_device_nodes(polled_ok),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                log.debug("snmp.ensure_device.timeout")

        # ── Pass 2: Meraki cloud SNMP polling ────────────────────────────────
        if self._meraki_cloud and not self._targets:
            try:
                cloud_endpoints = await asyncio.wait_for(
                    self._fetch_meraki_cloud_snmp_endpoints(backend),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                log.warning("snmp.meraki_cloud.fetch_timeout")
                cloud_endpoints = []
            except Exception as exc:
                log.warning("snmp.meraki_cloud.fetch_error", error=str(exc))
                cloud_endpoints = []

            if cloud_endpoints:
                try:
                    cloud_results = await asyncio.wait_for(
                        asyncio.gather(
                            *[self._poll_meraki_cloud_org(ep, resolver, semaphore)
                              for ep in cloud_endpoints],
                            return_exceptions=True,
                        ),
                        timeout=300.0,  # 5 min hard cap on Meraki cloud passes
                    )
                except asyncio.TimeoutError:
                    log.warning("snmp.meraki_cloud.global_timeout",
                                orgs=len(cloud_endpoints))
                    cloud_results = []
                for item in cloud_results:
                    if isinstance(item, Exception):
                        log.warning("snmp.discover.cloud_poll_failed", error=str(item))
                        continue
                    data = data.merge(item)
                log.info("snmp.meraki_cloud.done",
                         orgs=len(cloud_endpoints))
            else:
                log.debug("snmp.meraki_cloud.no_endpoints")

        # Force every node/edge to be tagged with this exact adapter instance
        # so the ingest layer can correctly purge stale rows on the next cycle.
        # Helpers historically set source_adapter="snmp" (family), which made
        # the per-instance purge miss them and let junk accumulate forever.
        for n in data.nodes:
            n.source_adapter = self.instance_id
        for e in data.edges:
            e.source_adapter = self.instance_id

        log.info(
            "snmp.discover.done",
            instance=self.instance_id,
            device_targets=len(targets) if targets else 0,
            snmp_ok=len(polled_ok),
            nodes=len(data.nodes),
            edges=len(data.edges),
        )
        return data
