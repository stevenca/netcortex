"""SNMP polling profiles — choose which OID families to walk per device.

Inspired by Splunk-Connect-for-SNMP. A *profile* describes:

  * which OID groups to walk (interface, lldp, cdp, mac, routing, stp, ipv6)
  * which devices it matches (by sysObjectID prefix, name regex, model
    glob, source adapter, or explicit IP / hostname allow-list)
  * per-host SNMP tunables:
      - ``chunk_repetitions`` for PDU sizing
      - ``ignore_not_increasing`` for buggy agents (``-Cc``)
      - ``device_timeout`` override
      - ``walk_timeout`` override

Profiles can be defined in the secrets backend at
``netcortex/snmp/profiles`` as YAML. A *default* profile is always
applied last and replicates the historical "walk everything" behavior so
nothing regresses for installations that don't define profiles.

Schema:

    profiles:
      - name: catalyst-9k-fabric
        match:
          model: "C9*"          # fnmatch
          name: ".*-cat9k.*"    # regex
        oid_groups: [system, interface, lldp, cdp, mac, arp, ipv6]
        chunk_repetitions: 25
        ignore_not_increasing: true
        device_timeout: 90
      - name: nexus-9k
        match:
          sys_object_id: "1.3.6.1.4.1.9.12.3.1.3"  # cevChassisN9K*
        oid_groups: [system, interface, lldp, mac, routing, stp, ipv6]
        chunk_repetitions: 25
      - name: meraki-hardware
        match:
          source_adapter: "meraki"
          model: "M*"
        oid_groups: [system, interface, lldp]
        chunk_repetitions: 10
        device_timeout: 45
      - name: default
        match: {}               # matches everything
        oid_groups: ALL
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Canonical OID group names. The SNMP adapter checks ``profile.oid_groups``
# before invoking each helper so devices only get walked for what their
# profile declares.
OID_GROUPS = (
    "system",     # sysName/sysDescr/sysUptime — always cheap
    "interface",  # IF-MIB
    "lldp",       # LLDP-MIB
    "cdp",        # CISCO-CDP-MIB
    "mac",        # BRIDGE-MIB dot1dTpFdb*
    "arp",        # IP-MIB ipNetToMedia*
    "routing",    # ipCidrRouteTable, ospfNbrTable, bgpPeerTable
    "stp",        # BRIDGE-MIB dot1dStp*
    "ipv6",       # ipAddressTable (IPv6 entries)
    "vlan",       # CISCO-VTP-MIB / Q-BRIDGE
)

ALL_GROUPS = frozenset(OID_GROUPS)


@dataclass(frozen=True)
class _MatchCriteria:
    """Match rule for a profile. All criteria are AND'd."""
    sys_object_id_prefix: str | None = None
    name_regex: re.Pattern[str] | None = None
    model_glob: str | None = None
    source_adapter: str | None = None  # exact family match (e.g. "meraki")
    ip_allowlist: tuple[str, ...] = ()
    name_allowlist: tuple[str, ...] = ()

    def matches(self, ctx: "DeviceContext") -> bool:
        if self.sys_object_id_prefix is not None:
            soid = (ctx.sys_object_id or "").lstrip(".")
            if not soid.startswith(self.sys_object_id_prefix.lstrip(".")):
                return False
        if self.name_regex is not None:
            if not self.name_regex.search(ctx.name or ""):
                return False
        if self.model_glob is not None:
            if not fnmatch.fnmatch((ctx.model or "").upper(),
                                    self.model_glob.upper()):
                return False
        if self.source_adapter is not None:
            family = (ctx.source_adapter or "").split("/", 1)[0]
            if family != self.source_adapter:
                return False
        if self.ip_allowlist and ctx.ip not in self.ip_allowlist:
            return False
        if self.name_allowlist and (ctx.name or "") not in self.name_allowlist:
            return False
        return True


@dataclass(frozen=True)
class SnmpProfile:
    """Resolved polling profile for a class of devices."""
    name: str
    match: _MatchCriteria
    oid_groups: frozenset[str] = field(default_factory=lambda: ALL_GROUPS)
    chunk_repetitions: int = 25
    ignore_not_increasing: bool = False
    device_timeout: float | None = None
    walk_timeout: float | None = None

    def includes(self, group: str) -> bool:
        return group in self.oid_groups


@dataclass(frozen=True)
class DeviceContext:
    """Inputs to profile matching for a single device."""
    ip: str
    name: str = ""
    model: str = ""
    sys_object_id: str = ""
    source_adapter: str = ""


# A "walk everything" default profile that preserves pre-profile behavior.
_DEFAULT_PROFILE = SnmpProfile(
    name="default",
    match=_MatchCriteria(),
    oid_groups=ALL_GROUPS,
)


def _compile_match(raw: dict | None) -> _MatchCriteria:
    """Translate a YAML/JSON match block into a compiled MatchCriteria.

    Any unknown keys are ignored with a debug log so typos don't silently
    promote a profile to "matches everything".
    """
    if not raw or not isinstance(raw, dict):
        return _MatchCriteria()
    try:
        name_re = re.compile(raw["name"]) if "name" in raw else None
    except re.error as exc:
        log.warning("snmp.profile.bad_name_regex", regex=raw["name"], error=str(exc))
        name_re = None
    ip_allow = raw.get("ips") or []
    name_allow = raw.get("hostnames") or []
    return _MatchCriteria(
        sys_object_id_prefix=raw.get("sys_object_id") or None,
        name_regex=name_re,
        model_glob=raw.get("model") or None,
        source_adapter=raw.get("source_adapter") or None,
        ip_allowlist=tuple(str(x) for x in ip_allow),
        name_allowlist=tuple(str(x) for x in name_allow),
    )


def _compile_groups(raw: Any) -> frozenset[str]:
    """Translate the YAML ``oid_groups`` value into a frozenset."""
    if raw is None or raw == "ALL":
        return ALL_GROUPS
    if isinstance(raw, str):
        raw = [raw]
    out: set[str] = set()
    for entry in raw:
        g = str(entry).strip().lower()
        if g == "all":
            return ALL_GROUPS
        if g in ALL_GROUPS:
            out.add(g)
        else:
            log.warning("snmp.profile.unknown_oid_group", group=g)
    return frozenset(out) if out else ALL_GROUPS


def _compile_profile(raw: dict) -> SnmpProfile | None:
    """Build a SnmpProfile from a single YAML dict entry."""
    if not isinstance(raw, dict):
        log.warning("snmp.profile.entry_not_dict", entry=str(raw)[:80])
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        log.warning("snmp.profile.missing_name", entry=str(raw)[:120])
        return None
    return SnmpProfile(
        name=name,
        match=_compile_match(raw.get("match")),
        oid_groups=_compile_groups(raw.get("oid_groups")),
        chunk_repetitions=int(raw.get("chunk_repetitions", 25)),
        ignore_not_increasing=bool(raw.get("ignore_not_increasing", False)),
        device_timeout=(float(raw["device_timeout"])
                        if "device_timeout" in raw else None),
        walk_timeout=(float(raw["walk_timeout"])
                      if "walk_timeout" in raw else None),
    )


class ProfileMatcher:
    """Holds compiled profiles and resolves the best match per device.

    Profile precedence: the FIRST profile (in declaration order) whose
    ``match`` criteria are satisfied wins. A built-in catch-all default
    profile is always available so every device gets at least one match.
    """

    def __init__(self, profiles: list[SnmpProfile]) -> None:
        self._profiles = list(profiles)
        # Always append the default so .resolve() never returns None.
        if not any(p.name == "default" for p in self._profiles):
            self._profiles.append(_DEFAULT_PROFILE)

    @classmethod
    def from_raw(cls, raw: Any) -> "ProfileMatcher":
        """Build a matcher from a YAML/JSON structure.

        Accepts either ``{"profiles": [...]}`` or a top-level list. Empty
        / None input yields a matcher with just the catch-all default.
        """
        entries: list[dict] = []
        if isinstance(raw, dict):
            entries = raw.get("profiles") or []
        elif isinstance(raw, list):
            entries = raw
        compiled: list[SnmpProfile] = []
        for ent in entries:
            p = _compile_profile(ent)
            if p is not None:
                compiled.append(p)
        return cls(compiled)

    def resolve(self, ctx: DeviceContext) -> SnmpProfile:
        for p in self._profiles:
            if p.match.matches(ctx):
                return p
        return _DEFAULT_PROFILE

    @property
    def profiles(self) -> tuple[SnmpProfile, ...]:
        return tuple(self._profiles)
