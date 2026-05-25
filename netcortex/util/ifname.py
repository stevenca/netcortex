"""Cisco-family interface name normalization.

Different platforms and discovery protocols report the same physical port
under different aliases. Two common forms exist for every Cisco interface
type:

    short form: "Gi1/0/1", "Te1/0/24", "Twe1/1/5", "Fo1/1/1", "Hu1/1/49",
                "Po10", "Eth1/1"
    long form:  "GigabitEthernet1/0/1", "TenGigE1/0/24",
                "TwentyFiveGigE1/1/5", "FortyGigE1/1/1",
                "HundredGigE1/1/49", "Port-channel10", "Ethernet1/1"

LLDP often returns the short form on Cisco IOS-XE while CDP returns the
long form, which causes the same physical link to appear as two separate
edges in NetCortex.

`normalize_ifname()` returns the canonical (long) form so callers can use
it as a deterministic dedup key. The original value the device reported is
preserved by callers in a separate property (e.g. `interface_a_raw`).

The mapping below is intentionally conservative — only well-known
Cisco/Nexus prefixes are rewritten. Unknown strings are returned unchanged
(after trimming/case-folding the alpha part) so we don't accidentally
collide unrelated names like vendor-specific "Slot1/Eth1".
"""

from __future__ import annotations

import re

# (short prefix, canonical long prefix). Order matters: longest short
# prefix first so "Twe" wins over "Te" when both could match.
_PREFIX_MAP: list[tuple[str, str]] = [
    # 400G
    ("FourHundredGigE",     "FourHundredGigE"),
    ("FoH",                  "FourHundredGigE"),
    # 200G
    ("TwoHundredGigE",      "TwoHundredGigE"),
    ("TwH",                  "TwoHundredGigE"),
    # 100G
    ("HundredGigE",         "HundredGigE"),
    ("Hu",                   "HundredGigE"),
    # 50G
    ("FiftyGigE",           "FiftyGigE"),
    ("Fi",                   "FiftyGigE"),
    # 40G
    ("FortyGigE",           "FortyGigE"),
    ("Fo",                   "FortyGigE"),
    # 25G — must come before "Te" because "Twe" shares first letter
    ("TwentyFiveGigE",      "TwentyFiveGigE"),
    ("Twe",                  "TwentyFiveGigE"),
    # 10G
    ("TenGigabitEthernet",  "TenGigabitEthernet"),
    ("TenGigE",             "TenGigabitEthernet"),
    ("Te",                   "TenGigabitEthernet"),
    # 5G
    ("FiveGigabitEthernet", "FiveGigabitEthernet"),
    ("Fiv",                  "FiveGigabitEthernet"),
    # 2.5G
    ("TwoGigabitEthernet",  "TwoGigabitEthernet"),
    ("Tw",                   "TwoGigabitEthernet"),
    # 1G
    ("GigabitEthernet",     "GigabitEthernet"),
    ("Gi",                   "GigabitEthernet"),
    # 100M
    ("FastEthernet",        "FastEthernet"),
    ("Fa",                   "FastEthernet"),
    # 10M
    ("Ethernet",            "Ethernet"),    # Nexus uses long form by default
    ("Eth",                  "Ethernet"),
    ("Et",                   "Ethernet"),
    # Mgmt
    ("Management",          "Management"),
    ("Mgmt",                 "Management"),
    ("Ma",                   "Management"),
    # Port-channel
    ("Port-channel",        "Port-channel"),
    ("Port-Channel",        "Port-channel"),
    ("Po",                   "Port-channel"),
    # Tunnel / Loopback / Vlan / Bundle-Ether
    ("Loopback",            "Loopback"),
    ("Lo",                   "Loopback"),
    ("Tunnel",              "Tunnel"),
    ("Tu",                   "Tunnel"),
    ("Vlan",                "Vlan"),
    ("Vl",                   "Vlan"),
    ("Bundle-Ether",        "Bundle-Ether"),
    ("BE",                   "Bundle-Ether"),
    # Serial / async
    ("Serial",              "Serial"),
    ("Se",                   "Serial"),
    # Null
    ("Null",                "Null"),
    ("Nu",                   "Null"),
]

# Pre-compile a single regex that splits "<alpha-prefix><suffix>" where
# suffix is anything from a digit onward (slot/unit numbering).
_SPLIT_RE = re.compile(r"^([A-Za-z\-]+)\s*(.*)$")


def normalize_ifname(name: str | None) -> str:
    """Return a canonical long-form interface name.

    Behavior:
      - ``None`` / empty / whitespace-only → empty string
      - Strips surrounding whitespace
      - Maps known short prefixes to their canonical long form
      - Leaves the numeric suffix (e.g., ``1/1/5``) untouched
      - Unknown prefixes are returned unchanged (after strip)
      - Comparison of the alpha prefix is case-insensitive; the canonical
        long form is returned with its documented capitalization

    Examples:
      >>> normalize_ifname("Twe1/1/5")
      'TwentyFiveGigE1/1/5'
      >>> normalize_ifname("TwentyFiveGigE1/1/5")
      'TwentyFiveGigE1/1/5'
      >>> normalize_ifname("Gi0/0")
      'GigabitEthernet0/0'
      >>> normalize_ifname("Po10")
      'Port-channel10'
      >>> normalize_ifname("Eth1/1")
      'Ethernet1/1'
    """
    if not name:
        return ""
    s = str(name).strip()
    if not s:
        return ""

    m = _SPLIT_RE.match(s)
    if not m:
        return s
    prefix, suffix = m.group(1), m.group(2)

    plower = prefix.lower()
    for short, canonical in _PREFIX_MAP:
        if plower == short.lower():
            return f"{canonical}{suffix}"

    # Unknown prefix — return original (preserves case).
    return s


def ifname_equal(a: str | None, b: str | None) -> bool:
    """Return True if two interface names refer to the same logical port."""
    return normalize_ifname(a) == normalize_ifname(b)
