"""Golden snapshot for the Meraki adapter's MAC normalization helper.

``_norm_mac`` is the canonical MAC normalization used everywhere the graph
joins Meraki-reported device data to NetBox/SNMP/ARP observations. A
fencepost or regex tweak that silently changes its output breaks every
cross-system join in the graph.

The brain-architecture refactor moves the Meraki poll adapter from
``netcortex/adapters/meraki.py`` to ``netcortex/sensory/poll/meraki.py``.
This golden proves the move did not change normalization behavior.
"""

from __future__ import annotations

from collections.abc import Callable

from netcortex.adapters.meraki import _norm_mac

_NORM_MAC_CASES: list[tuple[str, str | None]] = [
    ("norm_mac/none", None),
    ("norm_mac/empty", ""),
    ("norm_mac/colon_lower", "aa:bb:cc:dd:ee:ff"),
    ("norm_mac/colon_upper", "AA:BB:CC:DD:EE:FF"),
    ("norm_mac/colon_mixed", "Aa:Bb:Cc:Dd:Ee:Ff"),
    ("norm_mac/dash_separated", "aa-bb-cc-dd-ee-ff"),
    ("norm_mac/cisco_dotted", "aabb.ccdd.eeff"),
    ("norm_mac/cisco_dotted_upper", "AABB.CCDD.EEFF"),
    ("norm_mac/no_separators", "aabbccddeeff"),
    ("norm_mac/with_spaces", "aa bb cc dd ee ff"),
    ("norm_mac/short_input", "aa:bb:cc"),
    ("norm_mac/long_input", "aa:bb:cc:dd:ee:ff:00"),
    ("norm_mac/garbage", "not-a-mac"),
    ("norm_mac/partial_garbage", "aa:bb:cc:dd:ee:zz"),
    ("norm_mac/with_unicode", "aa:bb:cc:dd:ee:fé"),
    ("norm_mac/leading_trailing_whitespace", "   aa:bb:cc:dd:ee:ff   "),
    ("norm_mac/multicast_bit_set", "01:00:5e:00:00:fb"),
    ("norm_mac/broadcast", "ff:ff:ff:ff:ff:ff"),
    ("norm_mac/all_zeros", "00:00:00:00:00:00"),
]


def test_norm_mac_golden(
    assert_snapshot: Callable[[str, object], None],
) -> None:
    actual = {label: _norm_mac(value) for label, value in _NORM_MAC_CASES}
    assert_snapshot("meraki/_norm_mac/exhaustive", actual)
