"""Unit tests for Intersight adapter helpers and per-port topology emission.

These tests cover the gap fix introduced for "Nutanix-on-UCS servers
and FI↔N9K cables not visible in topology":

  * ``_fi_port_name`` / ``_host_port_name``  — pure formatters that
    produce the ``interface_a`` / ``interface_b`` labels on the
    PHYSICAL_LINK edges emitted from
    ``adapter/ExtEthInterfaces`` → ``ether/PhysicalPorts``.
    Verified by parametrised value tables.
  * Full ``discover()`` per-port emission — uses a stubbed Intersight
    adapter (no live API) to assert that:
      - server→FI edges are emitted with port-accurate
        ``interface_a`` (vic<slot>/<port>) and ``interface_b``
        (Ethernet<slot>/<port>) using the
        ``adapter.ExtEthInterface → AcknowledgedPeerInterface →
        ether.PhysicalPort`` chain (the same chain works for both
        X-Series blades and standalone-CIMC C-series direct-attach).
      - the PhysicalPort is correctly mapped to its parent FI using
        the ``(RegisteredDevice.Moid, SwitchId)`` key, because
        ``PhysicalPort.NetworkElement`` is not exposed by Intersight.
      - they are tagged ``discovery_proto='intersight'`` and
        ``link_type='server_to_fi'`` (so the dedupe + priority logic
        in the correlator finds them).
      - the generic dual-FI fallback is SUPPRESSED when per-port
        data is available for the server (no parallel duplicates).
      - servers with no ExtEthInterface data and no UCS Domain
        membership (true standalone CIMC without UCS-fabric
        coverage) get no fabric edges — matches prior behaviour.
      - the FI node carries observable identity (``candidate_ips``,
        ``candidate_names``, ``OWNS_MAC`` for OOB MAC) so the
        correlation engine can merge LLDP/CDP stubs onto it
        without going through NetBox.
"""

from __future__ import annotations

import asyncio

import pytest

from netcortex.adapters.intersight import (
    IntersightAdapter,
    _fi_port_name,
    _host_port_name,
)
from netcortex.graph.models import EdgeType, NodeType
from netcortex.models.device import NormalizedDevice

# ── _fi_port_name / _host_port_name ────────────────────────────────────

@pytest.mark.parametrize("slot,port,expected", [
    (1, 3,         "Ethernet1/3"),
    ("1", "3",     "Ethernet1/3"),
    (1, 0,         "Ethernet1/0"),
    (None, 5,      "Ethernet1/5"),     # missing slot defaults to 1
    (2, None,      "Ethernet2/0"),     # missing port defaults to 0
    (None, None,   ""),                 # doubly-missing → empty
    ("", "",       "Ethernet1/0"),     # empty strings treated as missing
])
def test_fi_port_name(slot, port, expected: str) -> None:
    """FI-side ports render as ``Ethernet<slot>/<port>`` for UCS familiarity."""
    assert _fi_port_name(slot, port) == expected


@pytest.mark.parametrize("slot,port,expected", [
    (1, 1,         "vic1/1"),
    (2, 4,         "vic2/4"),
    (None, None,   ""),
    (None, 0,      "vic1/0"),
])
def test_host_port_name(slot, port, expected: str) -> None:
    """Server-side ports render as ``vic<slot>/<port>`` matching UCSM."""
    assert _host_port_name(slot, port) == expected


# ── discover() per-port emission ────────────────────────────────────────

class _StubAdapter(IntersightAdapter):
    """Intersight adapter subclass that bypasses HTTP and yields canned data.

    Each ``list_*`` helper is overridden to return the matching slice of
    the canned payload that the test wires in via ``__init__``.  This
    keeps the test focused on the *logic* in ``discover()`` (port
    resolution, edge emission, FI identity) and avoids needing httpx /
    a live Intersight tenant.
    """

    def __init__(self, payload: dict) -> None:
        # NOTE: deliberately skip the base ``__init__`` (which would
        # require key_id / secret_key) — we only need ``self.name``,
        # ``self.instance_name``, and a placeholder ``instance_id``
        # for node id construction.
        self.instance_name = "test"
        self._payload = payload

    @property
    def instance_id(self) -> str:
        return "intersight:test"

    async def list_devices(self):           return self._payload["devices"]
    async def list_fabric_interconnects(self): return self._payload["fis"]
    async def list_chassis(self):           return self._payload.get("chassis", [])
    async def list_hyperflex_clusters(self): return self._payload.get("hx", [])
    async def list_server_profiles(self):   return self._payload.get("profiles", [])
    async def list_adapters(self):          return self._payload.get("adapters", [])
    async def list_host_eth_interfaces(self): return self._payload.get("host_ifs", [])
    async def list_blades(self):            return self._payload.get("blades", [])
    async def list_server_nodes(self):      return self._payload.get("server_nodes", [])
    async def list_physical_ports(self):    return self._payload.get("phy_ports", [])
    async def list_ext_eth_interfaces(self): return self._payload.get("ext_eth", [])


def _build_canned_payload(*, with_host_ports: bool) -> dict:
    """Return canned Intersight payloads emulating a 2-FI domain + 1 server.

    The single server has two VIC ports plugged into one FI port each
    (mirroring the dual-FI redundancy pattern that Nutanix-on-UCS uses
    in the real environment that motivated this feature).
    """
    fi_a = {
        "Moid": "fi-a-moid",
        "Model": "UCS-FI-6454",
        "Serial": "FCH2903782Y",
        "SwitchId": "A",
        "Name": "FI-A",
        "Dn": "sys/switch-A",
        "ManagementIpAddress": "10.0.0.10",
        "OutOfBandMac": "00:11:22:33:44:01",
        "OperState": "Operable",
        "RegisteredDevice": {"Moid": "dom-1"},
    }
    fi_b = dict(fi_a)
    fi_b.update({
        "Moid": "fi-b-moid",
        "Serial": "FCH2903782Z",
        "SwitchId": "B",
        "Name": "FI-B",
        "Dn": "sys/switch-B",
        "ManagementIpAddress": "10.0.0.11",
        "OutOfBandMac": "00:11:22:33:44:02",
    })

    # The server's RegisteredDevice is its OWN CIMC connector
    # (the standalone-CIMC scenario that the generic dual-FI emitter
    # can't help) — we want the per-port emitter to step in.
    server = NormalizedDevice(
        name="ntnx-fi-1",
        platform="intersight",
        platform_id="srv-1-moid",
        role="server",
        serial="WZP123456",
        mgmt_ip="10.1.1.1",
        platform_metadata={"device_moid": "standalone-cimc-rd"},
    )

    adapter = {
        "Moid": "adp-1-moid",
        "Model": "UCSC-VIC-25Q",
        "Pid": "UCSC-VIC-25Q",
        "ComputeNode": {"Moid": "srv-1-moid"},
    }

    payload: dict = {
        "devices": [server],
        "fis": [fi_a, fi_b],
        "adapters": [adapter],
    }

    if with_host_ports:
        # Two FI physical ports + two server adapter ports cabled into them.
        # PhysicalPort records carry no ``NetworkElement`` MoRef — we
        # resolve them to an FI via ``(RegisteredDevice.Moid, SwitchId)``,
        # matching the actual Intersight payload schema.
        payload["phy_ports"] = [
            {"Moid": "fi-a-port-1",
             "SlotId": 1, "PortId": 17,
             "SwitchId": "A",
             "Dn": "switch-FCH2903782Y/slot-1/switch-ether/port-17",
             "Role": "Server",
             "RegisteredDevice": {"Moid": "dom-1"}},
            {"Moid": "fi-b-port-1",
             "SlotId": 1, "PortId": 17,
             "SwitchId": "B",
             "Dn": "switch-FCH2903782Z/slot-1/switch-ether/port-17",
             "Role": "Server",
             "RegisteredDevice": {"Moid": "dom-1"}},
        ]
        # ExtEthInterfaces are the server-side NIC ports; their
        # AcknowledgedPeerInterface MoRef points at the FI PhysicalPort.
        # MacAddress is plumbed onto the emitted edge for downstream
        # MAC-correlation (e.g. matching against n9k1's CAM table).
        payload["ext_eth"] = [
            {"Moid": "ext-a",
             "SlotId": 1, "PortId": 1,
             "MacAddress": "74:E2:E7:FA:21:D8",
             "AcknowledgedPeerInterface": {
                 "ObjectType": "ether.PhysicalPort",
                 "Moid": "fi-a-port-1"},
             "AdapterUnit": {"Moid": "adp-1-moid"},
             "RegisteredDevice": {"Moid": "standalone-cimc-rd"}},
            {"Moid": "ext-b",
             "SlotId": 1, "PortId": 2,
             "MacAddress": "74:E2:E7:FA:21:D9",
             "AcknowledgedPeerInterface": {
                 "ObjectType": "ether.PhysicalPort",
                 "Moid": "fi-b-port-1"},
             "AdapterUnit": {"Moid": "adp-1-moid"},
             "RegisteredDevice": {"Moid": "standalone-cimc-rd"}},
        ]

    return payload


def _phys_links(data, *, server_node: str, fi_node: str) -> list:
    return [
        e for e in data.edges
        if e.type == EdgeType.PHYSICAL_LINK
        and {e.source_id, e.target_id} == {server_node, fi_node}
    ]


def test_per_port_emission_replaces_generic_fallback() -> None:
    """With acknowledged peer interfaces present, we get one edge per host port
    (with real interface_a/interface_b) and zero generic empty-interface edges.
    """
    adapter = _StubAdapter(_build_canned_payload(with_host_ports=True))
    data = asyncio.run(adapter.discover())

    server_node = "intersight:test:srv-1-moid"
    fi_a_node = "intersight-fi:test:fi-a-moid"
    fi_b_node = "intersight-fi:test:fi-b-moid"

    edges_a = _phys_links(data, server_node=server_node, fi_node=fi_a_node)
    edges_b = _phys_links(data, server_node=server_node, fi_node=fi_b_node)

    assert len(edges_a) == 1, "exactly one per-port edge to FI-A"
    assert len(edges_b) == 1, "exactly one per-port edge to FI-B"

    ea = edges_a[0]
    assert ea.properties["discovery_proto"] == "intersight"
    assert ea.properties["link_type"] == "server_to_fi"
    assert ea.properties["interface_a"] == "vic1/1"
    assert ea.properties["interface_b"] == "Ethernet1/17"
    assert ea.properties.get("mac_address") == "74:E2:E7:FA:21:D8"

    eb = edges_b[0]
    assert eb.properties["interface_a"] == "vic1/2"
    assert eb.properties["interface_b"] == "Ethernet1/17"
    assert eb.properties.get("mac_address") == "74:E2:E7:FA:21:D9"


def test_generic_fallback_when_no_host_ports() -> None:
    """No HostPort / PhysicalPort data → fall back to generic dual-FI edges
    so behaviour matches prior versions for UCSM-managed gear.

    In this canned payload the server is standalone-CIMC (RegisteredDevice
    Moid doesn't match either FI's domain), so the generic emitter
    can't find a UCS Domain to look up FIs through and therefore
    emits NO server→FI edge.  This is the previous behaviour
    deliberately preserved as the fallback contract — the per-port
    path is the one that fixes standalone-CIMC discovery.
    """
    adapter = _StubAdapter(_build_canned_payload(with_host_ports=False))
    data = asyncio.run(adapter.discover())

    server_node = "intersight:test:srv-1-moid"
    fi_a_node = "intersight-fi:test:fi-a-moid"
    fi_b_node = "intersight-fi:test:fi-b-moid"

    assert _phys_links(data, server_node=server_node, fi_node=fi_a_node) == []
    assert _phys_links(data, server_node=server_node, fi_node=fi_b_node) == []


def test_fi_identity_published_for_stub_merge() -> None:
    """FI nodes publish observable identity for correlation-driven stub merge.

    The whole point of fix (C) is that LLDP/CDP stubs on neighboring
    Nexus switches must merge onto the canonical Intersight FI node
    using ONLY observed state — no NetBox round-trip.  That requires
    the FI node to carry:

      * ``candidate_ips``      → drives _merge_neighbor_stubs_by_mgmt_ip
      * ``candidate_names``    → drives _merge_neighbor_stubs_by_name
                                  (covering ``A``, ``B``, ``FI-A``,
                                  ``sys/switch-A`` — whatever the FI
                                  firmware decides to advertise).
      * ``OWNS_MAC`` → MACAddress(``OutOfBandMac``) so that
        _merge_neighbor_stubs_by_chassis_mac can resolve the FI from
        an LLDP chassis-id (subtype 4) match.
    """
    adapter = _StubAdapter(_build_canned_payload(with_host_ports=True))
    data = asyncio.run(adapter.discover())

    fi_a_node = next(n for n in data.nodes
                     if n.id == "intersight-fi:test:fi-a-moid")
    assert fi_a_node.properties["mgmt_ip"] == "10.0.0.10"
    assert fi_a_node.properties["candidate_ips"] == ["10.0.0.10"]
    # All three name variants should be exposed so any sysName the FI
    # decides to advertise via LLDP can resolve back to this node.
    cand_names = fi_a_node.properties["candidate_names"]
    assert "FI-A" in cand_names
    assert "A" in cand_names
    assert "sys/switch-A" in cand_names

    # OOB MAC → MACAddress + OWNS_MAC edge
    mac_node = next(
        (n for n in data.nodes
         if n.type == NodeType.MAC_ADDRESS and
         n.properties.get("mac") == "00:11:22:33:44:01"),
        None,
    )
    assert mac_node is not None, "FI OOB MAC must be materialised as MACAddress"

    owns_edges = [
        e for e in data.edges
        if e.type == EdgeType.OWNS_MAC
        and e.source_id == fi_a_node.id
        and e.target_id == mac_node.id
    ]
    assert len(owns_edges) == 1
