"""Graph data models — the canonical shapes adapters produce and Neo4j stores."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Dimension(str, Enum):
    """Network graph dimensions — each maps to specific relationship types."""

    PHYSICAL = "physical"
    LOGICAL = "logical"
    ROUTING = "routing"
    SDWAN = "sdwan"
    FABRIC = "fabric"   # EVPN/VXLAN overlay fabrics
    VIRTUAL = "virtual" # VMware vSphere / virtualisation layer
    STP = "stp"         # Spanning-tree domains
    WAN = "wan"         # Site-to-Internet uplink topology (MX uplinks,
                        # default routes, eBGP-to-public-AS adjacencies)


class NodeType(str, Enum):
    DEVICE = "Device"
    INTERFACE = "Interface"
    VLAN = "VLAN"
    VNI = "VNI"
    VRF = "VRF"
    PREFIX = "Prefix"
    IP_ADDRESS = "IPAddress"
    MAC_ADDRESS = "MACAddress"
    ARP_ENTRY = "ARPEntry"
    BGP_SESSION = "BGPSession"
    SDWAN_TUNNEL = "SDWANTunnel"
    SDWAN_POLICY = "SDWANPolicy"
    # Canonical site from NetBox (keyed nb-site:<slug>)
    SITE = "Site"
    # Optional hierarchical sub-location from NetBox (keyed nb-loc:<id>)
    LOCATION = "Location"
    # Platform-specific container: Meraki network, CATC site, NDFC fabric, etc.
    # Correlated to canonical Site via MAPS_TO_SITE edges.
    PLATFORM_SITE = "PlatformSite"
    # A spanning-tree instance (VLAN-based PVST/RPVST or MST instance)
    STP_DOMAIN = "STPDomain"
    # An external routing peer (BGP neighbor with no matching Device node)
    ROUTING_PEER = "RoutingPeer"
    # The public Internet — a single sink node every WAN uplink ultimately
    # transits.  Always has id "internet:0".
    INTERNET = "Internet"
    # An Autonomous System we know about (typically an upstream ISP / cloud
    # provider).  Keyed as "as:<asn>".  Holds the public ASN number, plus
    # name/registry info when available.
    AUTONOMOUS_SYSTEM = "AutonomousSystem"


class EdgeType(str, Enum):
    # Physical
    PHYSICAL_LINK = "PHYSICAL_LINK"
    HAS_INTERFACE = "HAS_INTERFACE"
    LOCATED_AT = "LOCATED_AT"         # Device/Interface → PlatformSite or Location
    # Container hierarchy (structural — used for Cytoscape compound parentage)
    WITHIN_LOCATION = "WITHIN_LOCATION"   # Location → parent Location or canonical Site
    MAPS_TO_SITE = "MAPS_TO_SITE"         # PlatformSite → canonical Site
    # MAC / ARP table
    LEARNED_MAC = "LEARNED_MAC"       # Interface learned a MAC (CAM table entry)
    OWNS_MAC = "OWNS_MAC"             # Device owns a MAC (NIC/burned-in address)
    HAS_ARP = "HAS_ARP"              # Interface → ARPEntry (IP↔MAC binding)
    # Logical
    LOGICAL_MEMBER = "LOGICAL_MEMBER"   # interface carries a VLAN
    HAS_SVI = "HAS_SVI"                # canonical VLAN → SVI Interface
    HAS_PREFIX = "HAS_PREFIX"          # canonical VLAN → Prefix that lives on it
    ASSIGNED_IP = "ASSIGNED_IP"        # interface → IP address
    # Routing
    ROUTES_TO = "ROUTES_TO"            # BGP/OSPF/static adjacency
    BGP_PEER = "BGP_PEER"
    VRF_MEMBER = "VRF_MEMBER"          # interface or device belongs to VRF
    # Faded "we know A & B speak BGP/OSPF/EIGRP but we don't know which
    # physical link or VLAN carries it" edge. UI renders dashed/grey.
    ROUTES_OVER_UNKNOWN = "ROUTES_OVER_UNKNOWN"
    # Fabric (EVPN/VXLAN)
    VNI_EXTENDS = "VNI_EXTENDS"        # VNI maps to VLAN
    FABRIC_PEER = "FABRIC_PEER"        # VTEP-to-VTEP relationship
    VNI_MEMBER = "VNI_MEMBER"          # device participates in VNI
    # Virtualisation (vSphere)
    HAS_VM = "HAS_VM"               # Host → VM (VM runs on this host)
    VM_NETWORK = "VM_NETWORK"       # VM → virtual network / port group
    # SD-WAN
    SDWAN_TUNNEL = "SDWAN_TUNNEL"
    POLICY_APPLIES = "POLICY_APPLIES"
    # Spanning-tree
    STP_MEMBER = "STP_MEMBER"   # Device participates in STP instance
    STP_ROOT = "STP_ROOT"       # Device is root bridge for STP instance
    STP_LINK = "STP_LINK"       # Interface → STPDomain with port_state/port_role props
    # Routing (L3 topology)
    ROUTING_PEER = "ROUTING_PEER"  # Device–Device L3 neighbor; protocol={ospf,bgp,eigrp}
    # WAN topology (Internet uplink discovery)
    # Device → Internet or Device → AutonomousSystem; carries via/iface/public_ip/etc.
    WAN_UPLINK = "WAN_UPLINK"
    # AutonomousSystem → Internet (upstream AS sells transit to the public Internet)
    TRANSITS = "TRANSITS"
    # AutonomousSystem → AutonomousSystem (BGP peering at an AS boundary).
    # Today emitted only between the home AS and each externally-peered
    # upstream AS, with the border device + peer IP attached so the
    # operator can see "AS11017 ↔ AS3356 via cpn-ful-cat8k1".
    AS_PEER = "AS_PEER"


class GraphNode(BaseModel):
    """A node to be merged into the graph."""

    id: str                         # stable unique ID (used as merge key)
    type: NodeType
    properties: dict[str, Any] = Field(default_factory=dict)
    # If this node corresponds to a NetBox object, store its ID here
    netbox_id: int | None = None
    netbox_type: str | None = None  # e.g. "dcim.device", "ipam.vlan"
    # Which dimensions this node participates in
    dimensions: list[Dimension] = Field(default_factory=list)
    # Which adapter instance discovered this node
    source_adapter: str | None = None


class GraphEdge(BaseModel):
    """A directed relationship to be merged into the graph."""

    source_id: str
    target_id: str
    type: EdgeType
    properties: dict[str, Any] = Field(default_factory=dict)
    dimension: Dimension | None = None
    source_adapter: str | None = None


class GraphData(BaseModel):
    """The complete discovery output from one adapter instance."""

    adapter_id: str                         # e.g. "meraki/corp"
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    def merge(self, other: "GraphData") -> "GraphData":
        """Combine two GraphData objects (used to aggregate across adapters)."""
        return GraphData(
            adapter_id=self.adapter_id,
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
        )
