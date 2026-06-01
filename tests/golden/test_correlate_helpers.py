"""Golden snapshots for pure helpers in ``netcortex.graph.correlate``.

These four helpers encode load-bearing graph correlation policy:

* ``_port_tail`` is the canonical Cisco port-name tail extractor used
  whenever the correlator reasons about port identity.
* ``_l2_rank`` is the L2 authoritativeness scoring matrix used to pick the
  right sibling when a physical link could anchor to one of several
  speed-variant siblings of a port.
* ``_resolve_active_iface`` is the composite decision that uses
  ``_port_tail`` and ``_l2_rank`` together to pick the actual interface for
  one side of a link.
* ``_is_public_asn`` defines the IANA boundary between public and private
  ASN space — fencepost bugs here mis-classify WAN BGP peering.

The brain-architecture refactor (``docs/architecture/brain.md``) moves
``netcortex/graph/correlate.py`` to ``netcortex/association/``. These goldens
prove the move did not change decisions.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netcortex.graph.correlate import (
    _is_public_asn,
    _l2_rank,
    _port_tail,
    _resolve_active_iface,
)


# ---------------------------------------------------------------------------
# _port_tail — Cisco port-name tail extraction.
# ---------------------------------------------------------------------------

_PORT_TAIL_CASES: list[tuple[str, str | None]] = [
    ("port_tail/none", None),
    ("port_tail/empty", ""),
    ("port_tail/ten_gig_3_slot", "TenGigabitEthernet1/1/5"),
    ("port_tail/twenty_five_gig", "TwentyFiveGigE1/1/5"),
    ("port_tail/forty_gig", "FortyGigabitEthernet1/0/49"),
    ("port_tail/hundred_gig", "HundredGigE1/0/49"),
    ("port_tail/four_hundred_gig", "FourHundredGigE1/0/49"),
    ("port_tail/ethernet_short", "Ethernet1/46"),
    ("port_tail/eth_lowercase", "eth1/46"),
    ("port_tail/gi_short", "Gi0/1"),
    ("port_tail/te_short", "Te1/1/5"),
    ("port_tail/port_channel", "Port-channel1"),
    ("port_tail/port_channel_high", "Port-channel256"),
    ("port_tail/vlan_svi", "Vlan80"),
    ("port_tail/loopback", "Loopback0"),
    ("port_tail/management", "Management1"),
    ("port_tail/no_digits", "Console"),
]


def test_port_tail_golden(
    assert_snapshot: Callable[[str, object], None],
) -> None:
    actual = {label: _port_tail(value) for label, value in _PORT_TAIL_CASES}
    assert_snapshot("correlate/_port_tail/exhaustive", actual)


# ---------------------------------------------------------------------------
# _l2_rank — L2 authoritativeness scoring matrix.
# ---------------------------------------------------------------------------

_L2_RANK_CASES: list[tuple[str, dict | None]] = [
    ("l2_rank/none_input", None),
    ("l2_rank/empty_dict", {}),
    ("l2_rank/trunk_with_allowed", {"trunk_mode": "trunk", "vlans_allowed": [10, 20, 30]}),
    ("l2_rank/trunk_with_empty_allowed", {"trunk_mode": "trunk", "vlans_allowed": []}),
    ("l2_rank/trunk_no_allowed", {"trunk_mode": "trunk"}),
    ("l2_rank/trunk_allowed_none", {"trunk_mode": "trunk", "vlans_allowed": None}),
    ("l2_rank/access_vlan_10", {"trunk_mode": "access", "vlans_access": 10}),
    ("l2_rank/access_vlan_1", {"trunk_mode": "access", "vlans_access": 1}),
    ("l2_rank/access_no_vlan", {"trunk_mode": "access"}),
    ("l2_rank/access_vlan_none", {"trunk_mode": "access", "vlans_access": None}),
    ("l2_rank/unknown_mode", {"trunk_mode": "dot1q-tunnel"}),
    ("l2_rank/no_trunk_mode_with_other_fields", {"name": "Gi0/1", "oper_status": "up"}),
]


def test_l2_rank_golden(
    assert_snapshot: Callable[[str, object], None],
) -> None:
    actual = {label: _l2_rank(value) for label, value in _L2_RANK_CASES}
    assert_snapshot("correlate/_l2_rank/exhaustive", actual)


# ---------------------------------------------------------------------------
# _resolve_active_iface — composite decision.
# ---------------------------------------------------------------------------

# Realistic fixture: a switch with two speed-variants of port 1/1/5
# (a TwentyFiveGigE that is the active member and a TenGigabitEthernet
# shadow that is inactive). The correlator must pick the trunk-with-allowed
# variant.
_FIXTURE_DEVICE_IFACES = [
    {
        "name": "TenGigabitEthernet1/1/5",
        "trunk_mode": "access",
        "vlans_access": 1,
    },
    {
        "name": "TwentyFiveGigE1/1/5",
        "trunk_mode": "trunk",
        "vlans_allowed": [10, 20, 30],
    },
    {
        "name": "GigabitEthernet0/0/1",
        "trunk_mode": "access",
        "vlans_access": 100,
    },
    {
        "name": "Vlan10",
        "trunk_mode": None,
    },
]


@pytest.mark.parametrize(
    "case,anchor",
    [
        ("exact_match_one_speed_variant", "TwentyFiveGigE1/1/5"),
        ("siblings_pick_strongest", "TenGigabitEthernet1/1/5"),
        ("unknown_anchor", "NotARealPort99"),
        ("svi_anchor", "Vlan10"),
        ("empty_anchor", ""),
        ("differently_cased", "gigabitethernet0/0/1"),
    ],
)
def test_resolve_active_iface_golden(
    assert_snapshot: Callable[[str, object], None],
    case: str,
    anchor: str,
) -> None:
    result = _resolve_active_iface(anchor, _FIXTURE_DEVICE_IFACES)
    assert_snapshot(f"correlate/_resolve_active_iface/{case}", {
        "anchor": anchor,
        "result": result,
    })


def test_resolve_active_iface_empty_device(
    assert_snapshot: Callable[[str, object], None],
) -> None:
    assert_snapshot(
        "correlate/_resolve_active_iface/empty_device",
        _resolve_active_iface("Gi0/1", []),
    )


# ---------------------------------------------------------------------------
# _is_public_asn — IANA boundaries.
# ---------------------------------------------------------------------------

_PUBLIC_ASN_CASES: list[tuple[str, object]] = [
    ("public_asn/none", None),
    ("public_asn/zero", 0),
    ("public_asn/negative", -1),
    ("public_asn/one", 1),
    ("public_asn/cisco_16bit_public", 109),
    ("public_asn/just_below_doc16", 64495),
    ("public_asn/doc16_lo", 64496),
    ("public_asn/doc16_hi", 64511),
    ("public_asn/doc16_above", 64512),
    ("public_asn/priv16_lo", 64512),
    ("public_asn/priv16_hi", 65534),
    ("public_asn/priv16_above", 65535),
    ("public_asn/reserved_16bit", 65535),
    ("public_asn/doc32_lo", 65536),
    ("public_asn/doc32_hi", 65551),
    ("public_asn/doc32_above", 65552),
    ("public_asn/google_32bit_public", 396982),
    ("public_asn/priv32_lo", 4200000000),
    ("public_asn/priv32_hi", 4294967294),
    ("public_asn/reserved_32bit_max", 4294967295),
    ("public_asn/string_input_numeric", "109"),
    ("public_asn/string_input_garbage", "AS109"),
    ("public_asn/float_input", 109.0),
]


def test_is_public_asn_golden(
    assert_snapshot: Callable[[str, object], None],
) -> None:
    actual = {label: _is_public_asn(value) for label, value in _PUBLIC_ASN_CASES}  # type: ignore[arg-type]
    assert_snapshot("correlate/_is_public_asn/exhaustive", actual)
