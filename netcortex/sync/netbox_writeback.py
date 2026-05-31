"""NetBox write-back — reconcile observed NetCortex state into NetBox inventory.

Data flows FROM the Neo4j graph TO NetBox.  NetBox remains authoritative
for operator intent (device names, roles, device types, site membership
decisions), so every write here is *additive* or *fill-blank-only*:

  * Serial numbers  — written only when NetBox serial is blank.
  * Interfaces      — created when graph has an interface not in NetBox;
                      existing NetBox interfaces are never deleted or renamed.
  * IP addresses    — created in IPAM and assigned to the owning interface
                      when the address is not already in NetBox.  Never
                      modifies an existing IP record.
  * Cables          — created from high-confidence PHYSICAL_LINK edges
                      (lldp, cdp, catc_topology, meraki_topology,
                      ndfc_topology) when both endpoints are matched NetBox
                      devices and neither interface is already cabled.

The top-level entry point is ``reconcile_to_netbox()``, which calls the
four sub-reconcilers in dependency order (serials → interfaces → IPs →
cables) and returns a combined analysis report.  Pass ``dry_run=True`` to
compute the full diff without making any NetBox changes.

Each sub-reconciler can also be called independently.
"""

from __future__ import annotations

import json as _json
import re
from collections import defaultdict
from typing import Any

import httpx
import structlog

from netcortex.graph.client import get_driver

log = structlog.get_logger(__name__)

# Maximum device IDs per batched NetBox GET (avoids URL-length limits).
_BATCH = 50

# High-confidence discovery protocols: use these as cable evidence.
_CABLE_PROTOS = frozenset({
    "lldp", "cdp",
    "catc_topology",
    "meraki_topology", "meraki",
    "ndfc_topology",
})

# Interface names that should never be pushed to NetBox.  These leak in from
# adapters that couldn't resolve a real port (e.g. unparseable LLDP neighbor,
# missing port field).  Pushing them would pollute NetBox inventory with
# meaningless rows.
_GARBAGE_IFACE_NAMES = frozenset({
    "unknown", "(unknown)", "?", "n/a", "na",
    "none", "null", "-", "<unknown>",
})


def _is_garbage_iface_name(name: str) -> bool:
    """Return True if ``name`` is a known placeholder / unresolved sentinel."""
    return (name or "").strip().lower() in _GARBAGE_IFACE_NAMES


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _make_client(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=netbox_url,
        headers=_auth_headers(netbox_token),
        timeout=30.0,
        follow_redirects=True,
        verify=verify_ssl,
    )


def _nb_iface_type(speed_mbps: int | float | None, name: str | None = None) -> str:
    """Best-effort NetBox interface type slug from observed speed_mbps.

    When ``name`` is supplied, virtual-named interfaces (SVIs, port
    channels, loopbacks, tunnels, BVIs, null) short-circuit to the
    NetBox ``virtual`` type regardless of any speed reported by SNMP
    (e.g. SNMP ifTable will inherit the underlying physical link speed
    onto a Vlan SVI — typing it as ``1000base-t`` then is wrong).

    Multi-rate copper tiers (2.5G, 5G) are explicit so MS130-style
    mGig ports don't get mis-bucketed to 1000base-t — that loses fidelity
    AND causes write-back to under-report the actual link rate.
    """
    if name and _is_virtual_iface_name(name):
        return "virtual"
    if not speed_mbps:
        return "other"
    s = int(speed_mbps)
    if s >= 400_000: return "400gbase-x-qsfpdd"
    if s >= 100_000: return "100gbase-x-qsfp28"
    if s >=  40_000: return "40gbase-x-qsfpp"
    if s >=  25_000: return "25gbase-x-sfp28"
    if s >=  10_000: return "10gbase-x-sfpp"
    if s >=   5_000: return "5gbase-t"
    if s >=   2_500: return "2.5gbase-t"
    if s >=   1_000: return "1000base-t"
    if s >=     100: return "100base-tx"
    return "other"


# Regex that matches the NAME of any virtual (non-physical) interface.
# Covers Cisco IOS/IOS-XE: Vlan/Vl + N, Port-channel/Po + N, Loopback/Lo + N,
# Tunnel/Tu + N, BDI/BD + N, BVI/BV + N, Null + N.  Matched against the
# *normalized* name (lowercase, hyphen-stripped, long→short rewritten)
# so both ``VLAN-1`` and ``Vlan1`` resolve to ``vl1`` and trigger.
_VIRTUAL_NAME_PATTERNS: tuple[str, ...] = (
    r"^vl\d+$",            # Vlan / Vl SVI
    r"^po\d+$",            # Port-channel / Po
    r"^lo\d+$",            # Loopback / Lo
    r"^tu\d+$",            # Tunnel / Tu
    r"^bd\d+$",            # BDI
    r"^bv\d+$",            # BVI
    r"^null\d+$",          # null interface
)
_VIRTUAL_NAME_RE = re.compile("|".join(_VIRTUAL_NAME_PATTERNS))


def _is_virtual_iface_name(name: str) -> bool:
    """True if the (normalized) iface name denotes a virtual interface.

    Used for both:
      * Picking the NetBox interface ``type`` at create / patch time
        (a Vlan SVI is ``virtual`` no matter what speed SNMP reports).
      * Picking the NetBox cable ``type`` (a cable terminating on a
        virtual endpoint is itself ``virtual``).
    """
    if not name:
        return False
    return bool(_VIRTUAL_NAME_RE.match(_normalize_iface_name(name)))


# Cisco IOS short ↔ long interface prefix map.  Adapters expose the short
# form (the way ``show interfaces`` prints it), but NetBox records are
# frequently entered in the long form by operators.  Without this
# normalization the reconciler would treat ``Twe1/1/1`` and
# ``TwentyFiveGigE1/1/1`` as distinct interfaces and duplicate them.
#
# Order matters: longest-first so prefix matching is greedy.  Entries
# without an ambiguous short form (e.g. ``Loopback`` is its own
# canonical) are still listed so the lookup table is exhaustive.
_CISCO_IFACE_PREFIX_PAIRS: list[tuple[str, str]] = [
    ("FortyGigabitEthernet",      "Fo"),
    ("HundredGigE",               "Hu"),
    ("TwentyFiveGigE",            "Twe"),
    ("TenGigabitEthernet",        "Te"),
    ("TwoGigabitEthernet",        "Tw"),
    ("FiveGigabitEthernet",       "Fi"),
    ("GigabitEthernet",           "Gi"),
    ("FastEthernet",              "Fa"),
    ("Ethernet",                  "Eth"),
    ("Vlan",                      "Vl"),
    ("Port-channel",              "Po"),
    ("PortChannel",               "Po"),
    ("Loopback",                  "Lo"),
    ("Tunnel",                    "Tu"),
    ("Management",                "Ma"),
    ("BDI",                       "BD"),
    ("BVI",                       "BV"),
]

# Build a canonical short-form table once at module load.
_LONG_TO_SHORT = {long_.lower(): short for long_, short in _CISCO_IFACE_PREFIX_PAIRS}


def _normalize_iface_name(name: str) -> str:
    """Return a canonical key for interface-name equivalence comparisons.

    The goal is "do these two strings refer to the same logical
    interface?" — so we aggressively collapse cosmetic differences:

    * Lowercase + strip ALL whitespace
      (``Port 1`` ≡ ``Port1``).
    * Map Cisco IOS long-form prefixes to their short form
      (``TwentyFiveGigE1/1/1`` ≡ ``Twe1/1/1`` ≡ ``twe1/1/1``).
    * Strip a single hyphen immediately between an alphabetic prefix
      and a digit, so adapter variants like ``VLAN-13`` collapse with
      ``Vlan13`` / ``Vl13`` / ``vl13`` to the canonical ``vl13``.
      ``Port-channel1`` is unaffected (the long-form prefix rewrite
      already handles it before hyphen-stripping fires).

    The original name is preserved on the wire (we never rewrite NetBox
    records); this key is only used for "is this already in NetBox?"
    membership checks and lookup in the iface map.
    """
    if not name:
        return ""
    n = "".join(name.split()).lower()
    for long_lc, short in _LONG_TO_SHORT.items():
        if n.startswith(long_lc):
            n = short.lower() + n[len(long_lc):]
            break
    # Strip a single hyphen between an alphabetic prefix and a digit:
    # vlan-13 → vl-13 (after prefix rewrite) → vl13; port-2 → port2.
    n = re.sub(r"^([a-z]+)-(\d)", r"\1\2", n)
    return n


# Set of short-form Cisco prefixes considered canonical.  Used to gate
# the auto-rename logic — we ONLY canonicalize when the resulting
# prefix is a known Cisco short form, so generic operator names
# (``Port 1``, ``wan1``, ``mgmt0``) are left alone.
_CISCO_SHORT_PREFIXES: frozenset[str] = frozenset(
    short.lower() for _, short in _CISCO_IFACE_PREFIX_PAIRS
)


# Operator/import-style VLAN SVI name (``VLAN-1``, ``VLAN-10``,
# ``VLAN-1002``).  Cisco platforms natively report SVIs as ``Vlan1`` or
# (in some MIB walks) ``Vl1``; the uppercase-with-hyphen form is only
# ever produced by historical operator entry or by a third-party
# import tool.  Used as the trigger gate for the rename pass — limits
# scope to this *clearly* non-canonical pattern so we never
# accidentally rewrite a platform-native long form.
_NON_CANONICAL_VLAN_NAME_RE = re.compile(r"^VLAN-\d+$")


def _canonical_short_name(name: str) -> str:
    """Return the canonical Cisco short-form interface name.

    Title-cases the short prefix and preserves the suffix exactly as
    ``_normalize_iface_name`` produces it.  Only applies the rewrite
    when the resulting prefix is a recognised Cisco short form
    (``Vl``, ``Gi``, ``Te``, ``Twe``, ``Po``, ``Lo``, ``Tu``,
    ``Fa``, ``Ma``, ``BV``, ``BD``, etc.), so non-Cisco names
    (``Port 1``, ``wan1``, ``MGMT``) are returned unchanged.

    Examples::

        VLAN-10                → Vl10
        Vlan10                 → Vl10
        GigabitEthernet0/0/1   → Gi0/0/1
        TwentyFiveGigE1/1/1    → Twe1/1/1
        Port-channel1          → Po1
        Port 1                 → Port 1   (unchanged — Meraki style)
        wan1                   → wan1     (unchanged — Meraki MX)
    """
    if not name:
        return name
    norm = _normalize_iface_name(name)
    m = re.match(r"^([a-z]+)(.*)$", norm)
    if not m:
        return name
    prefix, suffix = m.group(1), m.group(2)
    if prefix not in _CISCO_SHORT_PREFIXES:
        return name
    return prefix.capitalize() + suffix


# Interface names that we MUST NOT push to NetBox unless the parent
# device is a Meraki-OS device.  These leak in when a Cisco IOS switch
# is also Meraki-Dashboard-enrolled: the Meraki adapter (canonical
# winner over the Cisco-IOS twin in our current canonicalization)
# exposes dashboard port labels like ``"Port 1"`` or
# ``"Port 1_C9300X-NM-8Y_7"`` which collide with — and pollute — the
# real Cisco interface set (``Twe1/1/1``, etc.).
#
# This is a *defensive guard* on the reconciler.  The deeper fix is in
# the canonicalization (Cisco-IOS adapter should win for IOS devices);
# until that lands we won't ship garbage to NetBox.
_MERAKI_DEVICE_MODEL_PREFIXES: tuple[str, ...] = (
    "MS", "MR", "MX", "MV", "MT", "MG", "CW",
)


def _is_meraki_style_iface_name(name: str) -> bool:
    """Return True for Meraki-dashboard-style port labels that should
    only be pushed to actual Meraki-OS devices.

    Pattern: ``port`` (case-insensitive) optionally followed by
    whitespace and/or a single hyphen, then digits, then optionally a
    trailing slot/module annotation separated by any of
    ``_`` / ``/`` / ``:`` / whitespace.  Examples that match:

        Port 1, Port1, Port-2, port-2, Port 12, Port 1_C9300X-NM-8Y_7,
        Port 1::C9300x-NM-8Y::1, Port 1/C9300X-NM-8Y/2

    Examples that do NOT match (real interface names):

        Ethernet1, Gi1/0/1, Port-channel1, Vlan80, Portland,
        PortChannel1
    """
    n = (name or "").strip().lower()
    # Must start with literal "port" + optional whitespace + optional
    # hyphen + a digit (alphabetic character after the hyphen disqualifies
    # — that's "Port-channel...", "Portland", etc.).
    return bool(re.match(r"^port[\s]*-?[\s]*\d+(?:$|[\s_/:].*)$", n))


# Meraki MX-specific uplink names.  ``wan1`` / ``wan2`` are dashboard
# labels for the WAN-facing uplinks on Meraki MX appliances; they make
# no sense on a Cisco IOS-XE switch / router or a Nexus and should be
# treated as wrong-platform (operator typo or stale Dashboard import).
_MX_WAN_NAME_RE = re.compile(r"^wan\d+$", re.IGNORECASE)


def _is_mx_wan_style_iface_name(name: str) -> bool:
    """True for Meraki MX uplink port labels (``wan1``, ``wan2``, ...)."""
    return bool(_MX_WAN_NAME_RE.match((name or "").strip()))


def _is_wrong_platform_iface_name(name: str) -> bool:
    """Catch-all for interface names that *only* make sense on a
    Meraki-OS device.  Used to delete stale operator entries from
    non-Meraki devices when they're uncabled, unassigned, and have no
    platform-native graph backing.
    """
    return _is_meraki_style_iface_name(name) or _is_mx_wan_style_iface_name(name)


def _ensure_cidr(addr: str) -> str:
    """Guarantee CIDR notation; default to /32 (IPv4) or /128 (IPv6)."""
    addr = addr.strip()
    if "/" in addr:
        return addr
    return f"{addr}/128" if ":" in addr else f"{addr}/32"


async def _paginate(client: httpx.AsyncClient, path: str, params: list) -> list[dict]:
    """Exhaustively page a NetBox list endpoint.  params is a list of (k, v) tuples."""
    results: list[dict] = []
    offset = 0
    limit = 500
    while True:
        try:
            resp = await client.get(path, params=[*params, ("limit", limit), ("offset", offset)])
            resp.raise_for_status()
        except Exception as exc:
            log.error("netbox_writeback.paginate_failed", path=path, error=str(exc))
            break
        payload = resp.json()
        results.extend(payload.get("results", []))
        if not payload.get("next"):
            break
        offset += limit
    return results


# ── Meraki-network site-collision helpers ─────────────────────────────────────
#
# A "collision" is two (or more) NetBox dcim.site records that both list the
# same Meraki ``network_id`` inside their ``custom_fields.meraki_networks``
# array.  That is an operator misconfiguration in NetBox — the field is the
# authoritative N:1 (many Meraki networks → 1 NetBox site) map and putting the
# same network ID under two sites duplicates every device in that network into
# two NetBox device records, one per site.  See ``enrich_sites_from_netbox`` in
# ``netbox_enrich.py`` for the read side: it logs a WARN per collision and
# refuses to map either side to avoid making things worse.
#
# These helpers + ``reconcile_duplicate_meraki_sites`` below are the *fix*
# side: pick a deterministic winner, delete the loser's duplicate device
# records (matched by serial), and strip the colliding entry from the loser's
# custom field so the collision stops recurring.

# Auto-generated site-name suffix from an old Meraki bring-up pass that used
# the network's display name verbatim as the NetBox site name.  Treated as a
# strong negative signal when picking a winner — if one candidate carries this
# suffix and another doesn't, the suffix-less one wins.
_MERAKI_DEMO_DAY_SUFFIX = "-meraki-demo-day"


def _pick_winner_for_meraki_collision(
    network_id: str,
    candidate_sites: list[dict],
) -> dict | None:
    """Choose the authoritative NetBox site among several that claim the
    same Meraki ``network_id`` in their ``meraki_networks`` custom field.

    Priority ladder (each step is applied to the survivors of the
    previous step; first step that yields a single survivor wins):

      1. **Inner-name match** — site whose ``name`` equals the
         ``meraki_networks[*].name`` value associated with this
         ``network_id``.  This is the strongest signal: it means the
         operator explicitly aligned the NetBox site name with the
         Meraki network display name for this entry.
      2. **Suffix filter** — drop any candidate whose name ends with
         ``-Meraki-Demo-Day`` (case-insensitive).  That suffix is a
         tell-tale of an auto-generated site from a legacy bring-up
         pass that the operator has since superseded.
      3. **Both filters combined** — survivors of #1 with #2 also
         applied.  Useful when #2 alone leaves multiple but the
         intersection collapses to one.
      4. **Deterministic tiebreak** — shortest name, then
         lexicographically smallest name, then smallest site ``id``.
         Pure ordering, so repeated invocations always pick the same
         winner.

    Returns ``None`` only if ``candidate_sites`` is empty.
    """
    if not candidate_sites:
        return None
    if len(candidate_sites) == 1:
        return candidate_sites[0]

    def _inner_name_matches(site: dict) -> bool:
        for net in (site.get("custom_fields") or {}).get("meraki_networks") or []:
            if (net.get("id") or "").strip() == network_id:
                inner = (net.get("name") or "").strip()
                if inner and inner == (site.get("name") or "").strip():
                    return True
        return False

    def _no_suffix(site: dict) -> bool:
        return not (site.get("name") or "").strip().lower().endswith(
            _MERAKI_DEMO_DAY_SUFFIX
        )

    inner = [s for s in candidate_sites if _inner_name_matches(s)]
    if len(inner) == 1:
        return inner[0]

    no_suffix = [s for s in (inner or candidate_sites) if _no_suffix(s)]
    if len(no_suffix) == 1:
        return no_suffix[0]

    both = [s for s in inner if _no_suffix(s)]
    if len(both) == 1:
        return both[0]

    pool = both or no_suffix or inner or candidate_sites
    return sorted(
        pool,
        key=lambda s: (
            len((s.get("name") or "")),
            (s.get("name") or ""),
            int(s.get("id") or 0),
        ),
    )[0]


# ── Graph queries ──────────────────────────────────────────────────────────────

async def _graph_matched_devices() -> list[dict]:
    """All canonical, NetBox-matched (netbox_id-bearing) Device nodes."""
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND (d.tombstoned IS NULL OR d.tombstoned = false)
              AND (d.stub IS NULL OR d.stub = false)
            RETURN d.netbox_id    AS netbox_id,
                   d.id           AS graph_id,
                   d.display_name AS display_name,
                   d.name         AS name,
                   d.serial       AS serial,
                   d.model        AS model,
                   d.mgmt_ip      AS mgmt_ip,
                   d.netbox_site_slug AS netbox_site_slug
            """
        )
        return await result.data()


async def _graph_interfaces() -> list[dict]:
    """Interfaces attached to matched (non-stub, canonical) Devices.

    Returns one row per ``HAS_INTERFACE`` edge.  Includes ``iface_id``
    so callers can identify the *source adapter* of an interface (the
    node-id prefix is the adapter — ``meraki-if``, ``snmp-if``,
    ``catc-if``, ``ndfc-if``, etc.).  This matters because for a Cisco
    IOS-XE switch that's also enrolled in Meraki Dashboard, the same
    canonical Device carries both:

      * ``snmp-if:meraki:<serial>:Te1/0/27`` — real OS-native name from
        SNMP polling.
      * ``meraki-if:<serial>:1`` — Meraki Dashboard dashboard label
        (``"Port 1"``) with the API ``port_id`` we want for webhook
        callbacks.

    The reconciler picks the right source per device based on the
    NetBox device-type model (see ``reconcile_interfaces``).
    """
    async with get_driver().session() as session:
        # dev62: also surface the per-port admin/VLAN/STP attributes
        # populated by the Meraki adapter (get_switch_port_configs) and
        # SNMP (ifAdminStatus + CISCO-STP-EXTENSIONS-MIB). Also pull
        # the parent device's netbox_site_slug so the interface pass
        # can resolve VLAN ids via the per-site VLAN map.
        result = await session.run(
            """
            MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            RETURN d.netbox_id        AS netbox_id,
                   d.display_name     AS device_name,
                   d.id               AS device_node_id,
                   d.netbox_site_slug AS netbox_site_slug,
                   i.id               AS iface_id,
                   i.name             AS name,
                   i.mac              AS mac,
                   i.speed_mbps       AS speed_mbps,
                   i.mtu              AS mtu,
                   i.description      AS description,
                   i.enabled          AS enabled,
                   i.port_id          AS port_id,
                   i.mode             AS mode,
                   i.vlans_access     AS vlans_access,
                   i.vlans_allowed    AS vlans_allowed,
                   i.native_vlan      AS native_vlan,
                   i.voice_vlan       AS voice_vlan,
                   i.stp_portfast     AS stp_portfast,
                   i.stp_bpdu_guard   AS stp_bpdu_guard,
                   i.stp_bpdu_filter  AS stp_bpdu_filter,
                   i.stp_root_guard   AS stp_root_guard,
                   i.stp_loop_guard   AS stp_loop_guard
            """
        )
        return await result.data()


async def _graph_site_vlans() -> list[dict]:
    """Canonical (site, vid) VLAN inventory for NetBox VLAN writeback.

    Returns one row per canonical VLAN node produced by
    ``_canonicalize_vlans_per_fabric``, i.e. nodes whose id starts with
    ``vlan:nb:`` (NetBox-site scoped). Each row carries:

      * ``slug``        — NetBox site slug (group key for ipam.vlan.site)
      * ``vid``         — int, 1-4094
      * ``name``        — canonical VLAN name (prefer the most descriptive)
      * ``description`` — optional
      * ``source``      — first contributing source_adapter (meraki / snmp / ndfc / catc)

    Multiple PlatformSites (e.g. several Meraki networks at one
    NetBox site) collapse onto one row per (slug, vid) — see the
    correlator's ``platform_site_ids`` list for the contributing
    sources. The writeback uses this as its source of truth for
    "what VLANs should exist per NetBox site".
    """
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (v:VLAN)
            WHERE v.id STARTS WITH 'vlan:nb:'
              AND v.vid IS NOT NULL
              AND v.netbox_site_slug IS NOT NULL
              AND v.netbox_site_slug <> ''
            RETURN v.netbox_site_slug AS slug,
                   v.vid              AS vid,
                   coalesce(v.name, 'VLAN' + toString(v.vid)) AS name,
                   v.description      AS description,
                   coalesce(v.source_adapter, 'correlator') AS source
            """
        )
        return await result.data()


def _iface_source(iface_id: str | None) -> str:
    """Return the adapter source for an Interface node id.

    Node ids look like ``meraki-if:<serial>:<port_id>``,
    ``snmp-if:<device_id>:<ifname>``, ``catc-if:<uuid>:<ifname>``, etc.
    The portion before the first ``:`` is the adapter prefix.
    """
    if not iface_id or ":" not in iface_id:
        return "unknown"
    return iface_id.split(":", 1)[0]


async def _graph_ips() -> list[dict]:
    """IPs assigned to interfaces of matched devices (via ASSIGNED_IP edges)."""
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface)-[:ASSIGNED_IP]->(ip:IPAddress)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            RETURN d.netbox_id    AS netbox_id,
                   d.display_name AS device_name,
                   i.name         AS iface_name,
                   ip.address     AS address,
                   ip.version     AS version,
                   coalesce(ip.prefix, ip.subnet, '') AS prefix_hint
            """
        )
        return await result.data()


async def _graph_physical_links() -> list[dict]:
    """PHYSICAL_LINK edges between two matched devices (both have netbox_id)."""
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (a:Device)-[l:PHYSICAL_LINK]-(b:Device)
            WHERE a.netbox_id IS NOT NULL AND b.netbox_id IS NOT NULL
              AND a.canonical_id IS NULL AND b.canonical_id IS NULL
              AND (a.stub IS NULL OR a.stub = false)
              AND (b.stub IS NULL OR b.stub = false)
              AND l.discovery_proto IN
                  ['lldp','cdp','catc_topology','meraki_topology','ndfc_topology','meraki']
            RETURN a.netbox_id          AS nb_id_a,
                   b.netbox_id          AS nb_id_b,
                   coalesce(a.display_name, a.name) AS name_a,
                   coalesce(b.display_name, b.name) AS name_b,
                   l.interface_a        AS port_a_norm,
                   l.interface_b        AS port_b_norm,
                   l.interface_a_raw    AS port_a_raw,
                   l.interface_b_raw    AS port_b_raw,
                   l.interface_a_active AS port_a_active,
                   l.interface_b_active AS port_b_active,
                   l.discovery_proto    AS proto
            """
        )
        return await result.data()


# ── NetBox batch fetchers ──────────────────────────────────────────────────────

async def _fetch_nb_interface_map(
    client: httpx.AsyncClient,
    nb_ids: list[str],
) -> dict[str, dict[str, dict]]:
    """Return ``{nb_device_id: {canonical_name_key: {id, name, cable}}}``.

    The ``canonical_name_key`` is produced by ``_normalize_iface_name``
    (whitespace collapsed, Cisco long → short, lowercased) so the
    reconciler's "does this interface already exist?" check works even
    when adapter naming and operator naming use different conventions
    (e.g. ``Port 1`` vs ``Port1``, or ``Twe1/1/1`` vs
    ``TwentyFiveGigE1/1/1``).
    """
    result: dict[str, dict[str, dict]] = {}
    for i in range(0, len(nb_ids), _BATCH):
        chunk = nb_ids[i : i + _BATCH]
        params = [("device_id", nid) for nid in chunk]
        for rec in await _paginate(client, "/api/dcim/interfaces/", params):
            dev_id = str(rec["device"]["id"])
            key = _normalize_iface_name(rec["name"])
            existing = result.setdefault(dev_id, {})
            # Defensive: NetBox itself can hold both ``Port1`` and
            # ``Port 1`` as separate records (legacy operator data); we
            # keep the first one we see (typically the older / cabled
            # one) and surface the collision so a follow-up clean-up
            # script can dedupe.  This means we will *match* against
            # whichever one we kept and decline to create a duplicate.
            if key in existing:
                log.warning(
                    "netbox_writeback.interface.netbox_dup",
                    device_id=dev_id, canonical_key=key,
                    kept=existing[key]["name"], duplicate=rec["name"],
                )
                continue
            # dev62: surface the L2/STP fields too so the patch pass
            # can do a conservative diff against operator-set values.
            untagged = rec.get("untagged_vlan")
            existing[key] = {
                "id":    rec["id"],
                "name":  rec["name"],
                "cable": rec.get("cable"),
                "enabled":       rec.get("enabled"),
                "mode":          (rec.get("mode") or {}).get("value")
                                 if isinstance(rec.get("mode"), dict)
                                 else rec.get("mode"),
                "untagged_vlan": untagged.get("id") if isinstance(untagged, dict) else None,
                "tagged_vlans":  sorted(
                    int(v["id"]) for v in (rec.get("tagged_vlans") or [])
                    if isinstance(v, dict) and v.get("id") is not None
                ),
                "custom_fields": rec.get("custom_fields") or {},
            }
    return result


async def _fetch_nb_existing_ips(
    client: httpx.AsyncClient,
    nb_ids: list[str],
) -> set[str]:
    """Return the set of CIDR strings already in NetBox IPAM for these devices."""
    existing: set[str] = set()
    for i in range(0, len(nb_ids), _BATCH):
        chunk = nb_ids[i : i + _BATCH]
        params = [("device_id", nid) for nid in chunk]
        for rec in await _paginate(client, "/api/ipam/ip-addresses/", params):
            if rec.get("address"):
                existing.add(rec["address"].strip())
    return existing


async def _fetch_unassigned_ips_by_host(
    client: httpx.AsyncClient,
    hosts: list[str],
) -> dict[str, list[dict]]:
    """Look up unassigned NetBox IP records by bare host address.

    For each ``host`` (e.g. ``"192.133.178.1"`` — no prefix), returns any
    NetBox ``ipam/ip-addresses`` records that share that host but are not
    yet attached to a device/interface (``assigned_object_id`` is null).

    This lets the assignment-fill pass discover records like
    ``192.133.178.1/19`` (operator-entered subnet hint) and bind them to
    the interface the graph identifies as the owner.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    if not hosts:
        return out
    # NetBox accepts ``q=`` for partial address searches; chunk to keep
    # request URLs short.
    for i in range(0, len(hosts), _BATCH):
        chunk = hosts[i : i + _BATCH]
        for host in chunk:
            # Exact-host lookup via the ``address`` filter is prefix-aware,
            # so we use ``q`` (full-text) and post-filter to host==host.
            params = [("q", host)]
            for rec in await _paginate(client, "/api/ipam/ip-addresses/", params):
                addr = (rec.get("address") or "").strip()
                if not addr:
                    continue
                if addr.split("/")[0] != host:
                    continue
                if rec.get("assigned_object_id"):
                    continue
                out[host].append(rec)
    return out


async def _graph_absent_devices() -> list[dict]:
    """Devices in the graph but not in NetBox, with the fields needed to create them.

    Returns rows with ``adapter_name`` (the platform-observed name we will
    use as the new NetBox device name), ``serial``, ``model``, ``platform``
    (e.g. "meraki", "fmc"), and the ``netbox_site_slug`` stamped by the
    ``enrich_sites_from_netbox`` pass via the ``meraki_networks`` custom
    field mapping.
    """
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.netbox_delta = '{"type": "absent_in_netbox"}'
              AND d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite)
            RETURN d.name              AS adapter_name,
                   d.display_name      AS display_name,
                   d.id                AS graph_id,
                   d.serial            AS serial,
                   d.model             AS model,
                   d.mgmt_ip           AS mgmt_ip,
                   d.platform          AS platform,
                   d.netbox_site_slug  AS netbox_site_slug,
                   d.netbox_site_id    AS netbox_site_id,
                   ps.name             AS platform_site
            """
        )
        return await result.data()


# ── Device-create helpers (look up NetBox type/role slugs by name/model) ──────

# Platform → NetBox role-slug mapping for absent device creates.  These
# slugs must already exist in NetBox; if a role is missing the create is
# skipped and reported.  Operator can extend NetBox roles + this table.
#
# Model-prefix overrides (e.g. Meraki "MR"/"CW" → wireless-ap) take
# precedence over the platform default.
_PLATFORM_ROLE_DEFAULT: dict[str, str] = {
    "meraki": "switch",         # most common; APs/cams/sensors override below
    "fmc":    "firewall",
    "ise":    "policy-engine",
    "catc":   "switch",
    "dnac":   "switch",
    "ndfc":   "switch",
}

_MODEL_PREFIX_ROLE: list[tuple[str, str]] = [
    ("MR",     "wireless-ap"),
    ("MV",     "camera"),
    ("MT",     "sensor"),
    ("MG",     "router"),
    ("MX",     "firewall"),
    ("MS",     "switch"),
    ("CW",     "wireless-ap"),
]


def _resolve_role_slug(platform: str, model: str) -> str:
    """Return the NetBox role-slug we should assign to a new device.

    Order of precedence: model-prefix match → platform default → "switch".
    """
    m = (model or "").strip().upper()
    for prefix, role in _MODEL_PREFIX_ROLE:
        if m.startswith(prefix):
            return role
    return _PLATFORM_ROLE_DEFAULT.get((platform or "").strip().lower(), "switch")


async def _fetch_nb_lookup_maps(
    client: httpx.AsyncClient,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Pre-fetch (site, role, device_type) slug→id maps in one pass.

    Returned as three dicts keyed by lowercased slug → integer id.
    """
    sites: dict[str, int] = {}
    roles: dict[str, int] = {}
    types: dict[str, int] = {}

    for rec in await _paginate(client, "/api/dcim/sites/", []):
        slug = (rec.get("slug") or "").strip().lower()
        if slug:
            sites[slug] = rec["id"]
    for rec in await _paginate(client, "/api/dcim/device-roles/", []):
        slug = (rec.get("slug") or "").strip().lower()
        if slug:
            roles[slug] = rec["id"]
    # device-types: index by both slug and model (normalised)
    for rec in await _paginate(client, "/api/dcim/device-types/", []):
        slug = (rec.get("slug") or "").strip().lower()
        if slug:
            types[slug] = rec["id"]
        model = (rec.get("model") or "").strip().lower()
        if model:
            # don't overwrite an explicit slug match
            types.setdefault(model, rec["id"])
    return sites, roles, types


# ── Sub-reconcilers ────────────────────────────────────────────────────────────

async def reconcile_device_serials(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Patch blank NetBox serial numbers with observed graph values.

    Rules
    -----
    * Only writes when the NetBox serial field is empty (null / "").
    * Never overwrites a populated serial — NetBox is authoritative.
    * Skips devices with no serial in the graph.
    """
    devices = await _graph_matched_devices()
    if not devices:
        return {"checked": 0, "patched": 0, "skipped": 0, "errors": 0, "changes": []}

    # Index graph serials: only keep rows where graph has a serial
    graph_by_nbid: dict[str, dict] = {
        str(d["netbox_id"]): d
        for d in devices
        if (d.get("serial") or "").strip()
    }
    if not graph_by_nbid:
        return {"checked": 0, "patched": 0, "skipped": len(devices), "errors": 0, "changes": []}

    checked = patched = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # Fetch current NetBox serials for these devices
        nb_ids = list(graph_by_nbid.keys())
        nb_serial: dict[str, str] = {}
        for i in range(0, len(nb_ids), _BATCH):
            chunk = nb_ids[i : i + _BATCH]
            params = [("id", nid) for nid in chunk]
            for rec in await _paginate(client, "/api/dcim/devices/", params):
                nb_serial[str(rec["id"])] = (rec.get("serial") or "").strip()

        for nb_id, dev in graph_by_nbid.items():
            graph_sn = dev["serial"].strip().upper()
            nb_sn    = nb_serial.get(nb_id)
            display  = dev.get("display_name") or dev.get("name") or nb_id
            checked += 1

            if nb_sn is None:          # fetch failed for this device
                skipped += 1
                continue
            if nb_sn:                  # already populated — never overwrite
                skipped += 1
                continue

            entry: dict[str, Any] = {
                "device": display, "netbox_id": nb_id,
                "serial": graph_sn, "applied": False,
            }
            if dry_run:
                entry["dry_run"] = True
                patched += 1
            else:
                try:
                    resp = await client.patch(
                        f"/api/dcim/devices/{nb_id}/",
                        content=_json.dumps({"serial": graph_sn}),
                    )
                    resp.raise_for_status()
                    entry["applied"] = True
                    patched += 1
                    log.info("netbox_writeback.serial.patched",
                             device=display, serial=graph_sn)
                except Exception as exc:
                    entry["error"] = str(exc)
                    errors += 1
                    log.error("netbox_writeback.serial.failed",
                              device=display, error=str(exc))
            changes.append(entry)

    return {
        "checked": checked, "patched": patched,
        "skipped": skipped, "errors": errors,
        "changes": changes,
    }


# dev62: a NetBox value of None/empty/0 means "operator hasn't set this", so
# netcortex IS allowed to populate it. A value that's already set (non-empty,
# non-zero, non-default) is considered operator intent and left alone for the
# non-namespaced fields (enabled, mode, untagged_vlan, tagged_vlans). The
# nc_* custom-field namespace is owned by netcortex outright and always gets
# overwritten when the source value differs.
def _nb_iface_value_is_blank(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if isinstance(val, (list, tuple)) and len(val) == 0:
        return True
    return False


def _build_iface_l2_patch(
    *,
    iface: dict,
    nb_row: dict,
    vlan_map: dict[tuple[int, int], int] | None,
    parent_site_id: int | None,
    iface_source: str,
    parent_is_meraki: bool,
    parent_serial: str,
) -> dict[str, Any]:
    """Compute a minimal PATCH body to update an existing NetBox interface
    with newly observed L2 / STP state, WITHOUT clobbering operator intent.

    Rules
    -----
    * Operator-set values (non-blank) for the *native* NetBox fields
      (``enabled``, ``mode``, ``untagged_vlan``, ``tagged_vlans``) are
      left alone — netcortex only fills in when the field is blank.
    * ``custom_fields`` in the ``nc_*`` namespace are owned by
      netcortex; we always set them to the observed value when it
      differs from what's there. Same for the existing
      ``meraki_port_id`` / ``meraki_serial`` / ``nc_platform[_id]``
      fields (they were already part of the create-side payload pre-dev62).
    * Returns an empty dict when there's nothing to change, so the
      caller can skip the PATCH entirely.
    """
    patch: dict[str, Any] = {}

    # ── enabled (admin status) ────────────────────────────────────────
    src_enabled = iface.get("enabled")
    if src_enabled is not None:
        cur_enabled = nb_row.get("enabled")
        if cur_enabled is None and bool(src_enabled) != cur_enabled:
            patch["enabled"] = bool(src_enabled)

    # ── L2 (mode + VLANs) ─────────────────────────────────────────────
    _mode = (iface.get("mode") or "").strip().lower()
    if (
        vlan_map
        and parent_site_id is not None
        and _mode in ("access", "trunk", "tagged-all")
    ):
        cur_mode    = nb_row.get("mode")
        cur_untag   = nb_row.get("untagged_vlan")
        cur_tagged  = nb_row.get("tagged_vlans") or []

        # Desired mode + untagged
        desired_mode: str | None = None
        desired_untag: int | None = None
        desired_tagged: list[int] | None = None

        if _mode == "access":
            desired_mode = "access"
            try:
                vid_int = int(iface.get("vlans_access")) if iface.get("vlans_access") is not None else None
            except (TypeError, ValueError):
                vid_int = None
            if vid_int is not None:
                nb_vlan_id = vlan_map.get((parent_site_id, vid_int))
                if nb_vlan_id is not None:
                    desired_untag = int(nb_vlan_id)
        else:  # trunk / tagged-all
            try:
                native_int = int(iface.get("native_vlan")) if iface.get("native_vlan") is not None else None
            except (TypeError, ValueError):
                native_int = None
            if native_int is not None:
                nb_vlan_id = vlan_map.get((parent_site_id, native_int))
                if nb_vlan_id is not None:
                    desired_untag = int(nb_vlan_id)
            allowed = iface.get("vlans_allowed")
            if allowed == "all":
                desired_mode = "tagged-all"
            elif isinstance(allowed, (list, tuple)) and allowed:
                tids: list[int] = []
                for v in allowed:
                    try:
                        vint = int(v)
                    except (TypeError, ValueError):
                        continue
                    nbid = vlan_map.get((parent_site_id, vint))
                    if nbid is not None:
                        tids.append(int(nbid))
                if tids:
                    desired_mode = "tagged"
                    desired_tagged = sorted(set(tids))
                else:
                    desired_mode = "tagged-all"
            else:
                desired_mode = "tagged-all"

        if desired_mode and _nb_iface_value_is_blank(cur_mode):
            patch["mode"] = desired_mode
        if desired_untag is not None and _nb_iface_value_is_blank(cur_untag):
            patch["untagged_vlan"] = desired_untag
        if desired_tagged is not None and not cur_tagged:
            patch["tagged_vlans"] = desired_tagged

    # ── Custom fields (nc_* namespace + meraki_*) ─────────────────────
    cur_cf = nb_row.get("custom_fields") or {}
    want_cf: dict[str, Any] = {}

    # nc_platform / nc_platform_id — keep in sync with create payload
    if cur_cf.get("nc_platform") != iface_source:
        want_cf["nc_platform"] = iface_source
    pid = iface.get("port_id")
    if pid is not None:
        want_pid = str(pid)
        if cur_cf.get("nc_platform_id") != want_pid:
            want_cf["nc_platform_id"] = want_pid

    if parent_is_meraki:
        if pid is not None:
            if cur_cf.get("meraki_port_id") != str(pid):
                want_cf["meraki_port_id"] = str(pid)
        if parent_serial and cur_cf.get("meraki_serial") != parent_serial:
            want_cf["meraki_serial"] = parent_serial

    # Voice VLAN — bool/int field, not nc-namespaced by NetBox convention
    # but we own it via nc_voice_vlan.
    if iface.get("voice_vlan") is not None:
        try:
            vv = int(iface["voice_vlan"])
        except (TypeError, ValueError):
            vv = None
        if vv is not None and cur_cf.get("nc_voice_vlan") != vv:
            want_cf["nc_voice_vlan"] = vv

    # STP per-port settings — nc_* namespace, always synced when source has value
    for src_key, cf_key in (
        ("stp_portfast",    "nc_stp_portfast"),
        ("stp_bpdu_guard",  "nc_stp_bpdu_guard"),
        ("stp_bpdu_filter", "nc_stp_bpdu_filter"),
        ("stp_root_guard",  "nc_stp_root_guard"),
        ("stp_loop_guard",  "nc_stp_loop_guard"),
    ):
        v = iface.get(src_key)
        if v is None:
            continue
        if cur_cf.get(cf_key) != bool(v):
            want_cf[cf_key] = bool(v)

    if want_cf:
        patch["custom_fields"] = want_cf

    return patch


async def reconcile_interfaces(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
    vlan_map: dict[tuple[int, int], int] | None = None,
) -> tuple[dict, dict]:
    """Create interfaces in NetBox that exist in the graph but not in NetBox.

    Returns ``(report, nb_iface_map, cabled_ids)``.  ``nb_iface_map`` is
    keyed by the *canonical* interface-name key (whitespace stripped,
    Cisco long → short, lowercased) so downstream passes (IP, cable)
    can look up interfaces by the same key the reconciler uses.

    Extras (dev62)
    --------------
    * ``vlan_map`` (optional): result of :func:`reconcile_site_vlans`
      shaped as ``{(site_id, vid): nb_vlan_id}``. When supplied, the
      payload includes NetBox-native L2 attributes:

      * ``enabled``        — from ``ifAdminStatus`` (SNMP) or Meraki API
      * ``mode``           — ``access`` / ``tagged`` / ``tagged-all``
      * ``untagged_vlan``  — resolved from ``vlans_access`` via vlan_map
      * ``tagged_vlans``   — resolved from ``vlans_allowed`` via vlan_map

      And custom fields:

      * ``nc_voice_vlan``     — voice VLAN id
      * ``nc_stp_portfast``   — PortFast / Meraki edge-port state
      * ``nc_stp_bpdu_guard`` — BPDU Guard
      * ``nc_stp_bpdu_filter``— BPDU Filter (Cisco only)
      * ``nc_stp_root_guard`` — Root Guard
      * ``nc_stp_loop_guard`` — Loop Guard

      When ``vlan_map`` is ``None`` or empty, those payload fields are
      skipped (existing behaviour is preserved).

    Rules
    -----
    * Only creates; never deletes or renames existing NetBox interfaces.
    * Skips interfaces with no name or names that hit a quality gate.
    * Quality gates (each is "skip + report", never an error):
        - ``placeholder_name``: name is in ``_GARBAGE_IFACE_NAMES``
          (``"unknown"``, ``"n/a"``, ...).
        - ``meraki_naming_on_non_meraki_device``: name looks like a
          Meraki dashboard label (``"Port N"`` / ``"Port N_..."``) but
          the parent NetBox device's model is not a Meraki-OS device.
          This prevents the well-known leak where a Cisco IOS switch
          (e.g. C9300) is *also* enrolled in Meraki Dashboard and the
          Meraki-platform graph twin's dashboard labels get pushed onto
          the NetBox C9300 device, where they don't belong.
    * Map ``speed_mbps`` to a NetBox type slug with the 2.5G/5G tiers
      included (mGig ports won't be mis-bucketed to ``1000base-t``).
    * Name equivalence uses ``_normalize_iface_name`` so we don't create
      ``Port 1`` when NetBox already has ``Port1`` (or
      ``TwentyFiveGigE1/1/1`` when graph reports ``Twe1/1/1``).
    """
    graph_ifaces = await _graph_interfaces()
    if not graph_ifaces:
        return (
            {"checked": 0, "created": 0, "skipped": 0, "errors": 0,
             "quality_filtered": 0, "skip_reasons": {}, "changes": []},
            {},
            set(),
        )

    by_nb_id: dict[str, list[dict]] = defaultdict(list)
    for iface in graph_ifaces:
        by_nb_id[str(iface["netbox_id"])].append(iface)

    nb_ids = list(by_nb_id.keys())
    created = skipped = errors = quality_filtered = 0
    patched_l2 = 0  # dev62: existing-interface L2/STP patches
    skip_reasons: dict[str, int] = defaultdict(int)
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # dev62: make sure our custom fields exist before we try to
        # set them on POST/PATCH (NetBox silently drops unknown CFs).
        cf_ensure_counts = await _ensure_custom_fields(
            client, _NC_INTERFACE_CUSTOM_FIELDS, dry_run=dry_run,
        )
        log.info(
            "netbox_writeback.interfaces.cf_ensured", **cf_ensure_counts,
        )

        nb_iface_map = await _fetch_nb_interface_map(client, nb_ids)

        # Pre-fetch each parent device's device_type model + serial + site so:
        #   * the platform-source filter knows which graph adapter to
        #     trust for naming (Meraki vs Cisco IOS),
        #   * the meraki_serial custom field can be populated from the
        #     authoritative NetBox serial (not a graph-inferred one),
        #   * and the L2 enrichment can resolve VLAN refs through the
        #     vlan_map (which is keyed by (site_id, vid)).
        nb_device_model:  dict[str, str] = {}
        nb_device_serial: dict[str, str] = {}
        nb_device_site:   dict[str, int] = {}
        for i in range(0, len(nb_ids), _BATCH):
            chunk = nb_ids[i : i + _BATCH]
            params = [("id", nid) for nid in chunk]
            for rec in await _paginate(client, "/api/dcim/devices/", params):
                model = (rec.get("device_type") or {}).get("model") or ""
                nb_device_model[str(rec["id"])] = model.upper()
                nb_device_serial[str(rec["id"])] = (rec.get("serial") or "").strip()
                site = rec.get("site")
                if isinstance(site, dict) and site.get("id") is not None:
                    nb_device_site[str(rec["id"])] = int(site["id"])

        for nb_id, ifaces in by_nb_id.items():
            existing = nb_iface_map.get(nb_id, {})
            parent_model = nb_device_model.get(nb_id, "")
            parent_serial = nb_device_serial.get(nb_id, "")
            parent_is_meraki = any(
                parent_model.startswith(p)
                for p in _MERAKI_DEVICE_MODEL_PREFIXES
            )

            # ── Platform-source filter ──────────────────────────────
            # A Cisco IOS-XE switch enrolled in Meraki Dashboard ends
            # up with both ``snmp-if:*`` and ``meraki-if:*`` Interface
            # nodes on the same canonical Device.  The two name them
            # differently (``Te1/0/27`` vs ``Port 1_C9300X-NM-8Y_7``);
            # we want the OS-native naming on Cisco devices and the
            # Meraki Dashboard naming on Meraki-OS devices.  For
            # Meraki-OS devices we additionally prefer ``meraki-if:*``
            # because it carries the ``port_id`` we need for webhook
            # callbacks, and enrich speed from the ``snmp-if:*``
            # sibling (the Meraki API often reports speed=None).
            preferred_source = "meraki-if" if parent_is_meraki else "snmp-if"

            # Index sibling snmp-if rows by canonical name for speed enrichment.
            snmp_by_name: dict[str, dict] = {}
            if parent_is_meraki:
                for sib in ifaces:
                    if _iface_source(sib.get("iface_id")) == "snmp-if":
                        snmp_by_name[_normalize_iface_name(sib.get("name") or "")] = sib

            for iface in ifaces:
                iname = (iface.get("name") or "").strip()
                src = _iface_source(iface.get("iface_id"))
                device_name = iface.get("device_name") or nb_id
                entry: dict[str, Any] = {
                    "device": device_name, "interface": iname,
                    "netbox_device_id": nb_id,
                    "iface_source": src, "applied": False,
                }

                if not iname:
                    skipped += 1
                    skip_reasons["empty_name"] += 1
                    continue

                # Quality gate 0: skip wrong-source rows.  This is the
                # main defence against the Cisco-IOS-also-on-Meraki
                # data leak; we silently ignore the other source's
                # interfaces instead of failing.  For non-Meraki and
                # non-Cisco devices (e.g. NDFC/Intersight discoveries
                # where the source attribution differs) fall back to
                # keeping everything that isn't a known mismatch.
                if parent_is_meraki and src == "snmp-if":
                    skipped += 1
                    skip_reasons["wrong_source_for_meraki_device"] += 1
                    continue
                if (not parent_is_meraki and src == "meraki-if"):
                    skipped += 1
                    skip_reasons["wrong_source_for_non_meraki_device"] += 1
                    continue

                # Quality gate 1: never push placeholder / unresolved names.
                if _is_garbage_iface_name(iname):
                    quality_filtered += 1
                    skip_reasons["placeholder_name"] += 1
                    log.info(
                        "netbox_writeback.interface.quality_filtered",
                        device=device_name, interface=iname,
                        reason="placeholder_name",
                    )
                    continue

                # Quality gate 2: defence-in-depth.  Even with the
                # source filter above, a stray meraki-style name that
                # somehow leaked into a non-Meraki device's interface
                # set (e.g. operator-entered via SNMP MIB description)
                # is refused at write-time.
                if (not parent_is_meraki
                        and _is_meraki_style_iface_name(iname)):
                    quality_filtered += 1
                    skip_reasons["meraki_naming_on_non_meraki_device"] += 1
                    log.warning(
                        "netbox_writeback.interface.quality_filtered",
                        device=device_name,
                        interface=iname,
                        parent_model=parent_model,
                        reason="meraki_naming_on_non_meraki_device",
                    )
                    entry["skipped"] = True
                    entry["skip_reason"] = "meraki_naming_on_non_meraki_device"
                    entry["parent_model"] = parent_model
                    changes.append(entry)
                    continue

                # Existing-interface check uses the canonical key so
                # ``Port 1`` matches ``Port1`` and ``Twe1/1/1`` matches
                # ``TwentyFiveGigE1/1/1``.
                key = _normalize_iface_name(iname)
                if key in existing:
                    # dev62: existing interface — diff and PATCH only
                    # the L2/STP fields netcortex owns. Skips quietly
                    # when there's nothing to change. Operator-set
                    # values are preserved (see _build_l2_patch).
                    nb_row = existing[key]
                    parent_site_id = nb_device_site.get(nb_id)
                    patch_body = _build_iface_l2_patch(
                        iface=iface,
                        nb_row=nb_row,
                        vlan_map=vlan_map,
                        parent_site_id=parent_site_id,
                        iface_source=src,
                        parent_is_meraki=parent_is_meraki,
                        parent_serial=parent_serial,
                    )
                    if not patch_body:
                        skipped += 1
                        skip_reasons["already_exists"] += 1
                        continue

                    if dry_run:
                        entry["dry_run"] = True
                        entry["proposed_patch"] = patch_body
                        entry["nb_interface_id"] = int(nb_row["id"])
                        patched_l2 += 1
                        changes.append(entry)
                        continue
                    try:
                        resp = await client.patch(
                            f"/api/dcim/interfaces/{nb_row['id']}/",
                            content=_json.dumps(patch_body),
                        )
                        resp.raise_for_status()
                        patched_l2 += 1
                        entry["applied"] = True
                        entry["nb_interface_id"] = int(nb_row["id"])
                        entry["patched_fields"] = sorted(patch_body.keys())
                        log.info(
                            "netbox_writeback.interface.l2_patched",
                            device=device_name,
                            interface=iname,
                            fields=sorted(patch_body.keys()),
                        )
                    except Exception as exc:
                        errors += 1
                        entry["error"] = str(exc)
                        log.error(
                            "netbox_writeback.interface.l2_patch_failed",
                            device=device_name,
                            interface=iname,
                            error=str(exc),
                        )
                    changes.append(entry)
                    continue

                # Speed: prefer this iface's speed; if missing, fall
                # back to the sibling snmp-if row (Meraki API often
                # returns speed=None on the API record but the SNMP
                # poll has accurate link rate).
                speed_mbps = iface.get("speed_mbps")
                if not speed_mbps and parent_is_meraki:
                    sib = snmp_by_name.get(key)
                    if sib:
                        speed_mbps = sib.get("speed_mbps")

                # Write the SNMP-/API-provided name as-is so we don't
                # silently rewrite platform-native long forms.  On
                # Cisco NX-OS and UCS Fabric Interconnects, the
                # canonical interface name *is* ``Ethernet1/1`` (not
                # ``Eth1/1``); on Cisco IOS-XE the running-config form
                # is ``GigabitEthernet0/0/1`` (not ``Gi0/0/1``).  The
                # iface-naming pass handles only the demonstrably
                # non-canonical ``VLAN-N`` operator/import style.
                payload: dict[str, Any] = {
                    "device": int(nb_id),
                    "name":   iname,
                    "type":   _nb_iface_type(speed_mbps, iname),
                }
                if iface.get("mac"):
                    payload["mac_address"] = iface["mac"]
                if iface.get("mtu"):
                    payload["mtu"] = int(iface["mtu"])
                if iface.get("description"):
                    payload["description"] = str(iface["description"])[:200]
                if iface.get("enabled") is not None:
                    payload["enabled"] = bool(iface["enabled"])

                # ── L2 (mode + VLANs) enrichment, dev62 ──────────
                # Only attempted when the caller supplied a vlan_map
                # (i.e. reconcile_site_vlans ran first and produced
                # ``{(site_id, vid): nb_vlan_id}``). The device's site
                # must be known too, since NetBox VLANs are scoped by
                # site.
                parent_site_id = nb_device_site.get(nb_id)
                _mode = (iface.get("mode") or "").strip().lower()
                if vlan_map and parent_site_id is not None and _mode in ("access", "trunk", "tagged-all"):
                    # Untagged / access VLAN. For access ports this is
                    # ``vlans_access``; for trunks it's ``native_vlan``.
                    untagged_vid = (
                        iface.get("vlans_access")
                        if _mode == "access"
                        else iface.get("native_vlan")
                    )
                    if untagged_vid is not None:
                        try:
                            untagged_vid_int = int(untagged_vid)
                        except (TypeError, ValueError):
                            untagged_vid_int = None
                        if untagged_vid_int is not None:
                            nb_vlan_id = vlan_map.get(
                                (parent_site_id, untagged_vid_int)
                            )
                            if nb_vlan_id is not None:
                                payload["untagged_vlan"] = int(nb_vlan_id)

                    # Tagged VLANs only for trunk modes. Meraki's
                    # ``allowedVlans = "all"`` collapses to NetBox
                    # mode=tagged-all (no explicit list). A concrete
                    # list collapses to mode=tagged.
                    if _mode in ("trunk", "tagged-all"):
                        allowed = iface.get("vlans_allowed")
                        if allowed == "all":
                            payload["mode"] = "tagged-all"
                        elif isinstance(allowed, (list, tuple)) and allowed:
                            tagged_ids: list[int] = []
                            for v in allowed:
                                try:
                                    vint = int(v)
                                except (TypeError, ValueError):
                                    continue
                                nb_id_for_v = vlan_map.get(
                                    (parent_site_id, vint)
                                )
                                if nb_id_for_v is not None:
                                    tagged_ids.append(int(nb_id_for_v))
                            if tagged_ids:
                                payload["mode"] = "tagged"
                                payload["tagged_vlans"] = tagged_ids
                            else:
                                # No usable VLAN refs — fall through to
                                # tagged-all so the port is at least
                                # marked as trunk in NetBox.
                                payload["mode"] = "tagged-all"
                        else:
                            payload["mode"] = "tagged-all"
                    else:
                        payload["mode"] = "access"

                # Custom-field stamping.  These mirror the NetBox
                # custom fields already defined on dcim.interface:
                #   * meraki_port_id   — set on Meraki-OS interfaces,
                #     enables NetBox-webhook → Meraki-API change sync
                #   * meraki_serial    — the parent device's Meraki
                #     serial (target for the webhook)
                #   * nc_platform      — adapter source prefix
                #     (meraki-if / snmp-if / catc-if / ...)
                #   * nc_platform_id   — adapter-native interface id
                #     (port_id for Meraki; left empty otherwise)
                #   * nc_voice_vlan    — voice VLAN id (Meraki + Cisco)
                #   * nc_stp_*         — per-port STP extension state
                cf: dict[str, Any] = {}
                if parent_is_meraki and iface.get("port_id"):
                    cf["meraki_port_id"] = str(iface["port_id"])
                if parent_is_meraki and parent_serial:
                    cf["meraki_serial"] = parent_serial
                cf["nc_platform"] = src
                if iface.get("port_id"):
                    cf["nc_platform_id"] = str(iface["port_id"])
                # dev62 STP / voice — only stamp when source actually
                # gave us a value (don't write False over a True the
                # operator might have set manually in NetBox).
                if iface.get("voice_vlan") is not None:
                    try:
                        cf["nc_voice_vlan"] = int(iface["voice_vlan"])
                    except (TypeError, ValueError):
                        pass
                for src_key, cf_key in (
                    ("stp_portfast",    "nc_stp_portfast"),
                    ("stp_bpdu_guard",  "nc_stp_bpdu_guard"),
                    ("stp_bpdu_filter", "nc_stp_bpdu_filter"),
                    ("stp_root_guard",  "nc_stp_root_guard"),
                    ("stp_loop_guard",  "nc_stp_loop_guard"),
                ):
                    val = iface.get(src_key)
                    if val is not None:
                        cf[cf_key] = bool(val)
                if cf:
                    payload["custom_fields"] = cf

                if dry_run:
                    entry["dry_run"] = True
                    created += 1
                else:
                    try:
                        resp = await client.post(
                            "/api/dcim/interfaces/",
                            content=_json.dumps(payload),
                        )
                        resp.raise_for_status()
                        new_id = resp.json().get("id")
                        # Index under the canonical key so subsequent
                        # rows in the same run see it as existing.
                        nb_iface_map.setdefault(nb_id, {})[key] = {
                            "id": new_id, "name": iname, "cable": None,
                        }
                        entry["nb_interface_id"] = new_id
                        entry["applied"] = True
                        created += 1
                        log.info("netbox_writeback.interface.created",
                                 device=device_name, interface=iname)
                    except Exception as exc:
                        entry["error"] = str(exc)
                        errors += 1
                        log.error("netbox_writeback.interface.failed",
                                  device=device_name, interface=iname,
                                  error=str(exc))
                changes.append(entry)

    # Flatten map for callers: {nb_dev_id: {canonical_key: int_id}}.
    # Callers (reconcile_ip_addresses, reconcile_ip_assignments,
    # reconcile_cables) MUST look up by ``_normalize_iface_name(name)``.
    flat_map: dict[str, dict[str, int]] = {
        dev_id: {name_key: info["id"] for name_key, info in ifaces.items()}
        for dev_id, ifaces in nb_iface_map.items()
    }
    cabled_ids: set[int] = {
        info["id"]
        for ifaces in nb_iface_map.values()
        for info in ifaces.values()
        if info.get("cable")
    }

    report = {
        "checked": len(graph_ifaces), "created": created,
        "patched_l2": patched_l2,
        "skipped": skipped, "errors": errors,
        "quality_filtered": quality_filtered,
        "skip_reasons": dict(skip_reasons),
        "cf_ensured": cf_ensure_counts,
        "changes": changes,
    }
    return report, flat_map, cabled_ids  # type: ignore[return-value]


async def reconcile_ip_addresses(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
    nb_iface_map: dict[str, dict[str, int]] | None = None,
) -> dict:
    """Create missing IP addresses in NetBox IPAM and assign them to interfaces.

    Rules
    -----
    * Only creates; never modifies existing IP records.
    * Skips IPs that already exist (matched by CIDR string) in NetBox.
    * Assigns to the owning interface when the interface ID is known.
    * Assumes /32 (IPv4) or /128 (IPv6) for bare addresses without prefix.
    """
    graph_ips = await _graph_ips()
    if not graph_ips:
        return {"checked": 0, "created": 0, "skipped": 0, "errors": 0, "changes": []}

    by_nb_id: dict[str, list[dict]] = defaultdict(list)
    for row in graph_ips:
        by_nb_id[str(row["netbox_id"])].append(row)

    nb_ids = list(by_nb_id.keys())
    created = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        if nb_iface_map is None:
            flat = await _fetch_nb_interface_map(client, nb_ids)
            nb_iface_map = {
                dev_id: {name: info["id"] for name, info in ifaces.items()}
                for dev_id, ifaces in flat.items()
            }

        existing_ips = await _fetch_nb_existing_ips(client, nb_ids)

        for nb_id, rows in by_nb_id.items():
            iface_lookup = nb_iface_map.get(nb_id, {})
            for row in rows:
                raw_addr = (row.get("address") or "").strip()
                if not raw_addr:
                    skipped += 1
                    continue

                cidr = _ensure_cidr(raw_addr)
                # Also check bare address
                bare = cidr.split("/")[0]
                if cidr in existing_ips or any(e.startswith(bare + "/") for e in existing_ips):
                    skipped += 1
                    continue

                nb_iface_id = iface_lookup.get(_normalize_iface_name(row.get("iface_name") or ""))
                payload: dict[str, Any] = {"address": cidr, "status": "active"}
                if nb_iface_id:
                    payload["assigned_object_type"] = "dcim.interface"
                    payload["assigned_object_id"]   = nb_iface_id

                device_name = row.get("device_name") or nb_id
                entry: dict[str, Any] = {
                    "device": device_name,
                    "interface": row.get("iface_name", ""),
                    "address": cidr, "applied": False,
                }

                if dry_run:
                    entry["dry_run"] = True
                    created += 1
                else:
                    try:
                        resp = await client.post(
                            "/api/ipam/ip-addresses/",
                            content=_json.dumps(payload),
                        )
                        resp.raise_for_status()
                        existing_ips.add(cidr)
                        entry["applied"] = True
                        created += 1
                        log.info("netbox_writeback.ip.created",
                                 device=device_name, address=cidr)
                    except httpx.HTTPStatusError as http_exc:
                        # Idempotency: NetBox enforces global host-IP
                        # uniqueness across prefix lengths. If the same host
                        # IP exists at a different prefix (e.g. graph has
                        # /32 but NetBox has /19), NetBox returns 400 with
                        # "Duplicate IP address found in global table".
                        # Treat that as a benign skip, not a failure — the
                        # IP is already known to NetBox.
                        body = http_exc.response.text if http_exc.response else ""
                        if (http_exc.response is not None
                                and http_exc.response.status_code == 400
                                and "Duplicate IP" in body):
                            existing_ips.add(cidr)
                            existing_ips.add(bare + "/32")
                            entry["skipped"] = True
                            entry["skip_reason"] = "already_in_global_table"
                            entry["nb_message"] = body[:300]
                            skipped += 1
                            log.info("netbox_writeback.ip.already_exists",
                                     device=device_name, address=cidr,
                                     nb_message=body[:300])
                        else:
                            entry["error"] = (
                                f"HTTP {http_exc.response.status_code}: {body[:300]}"
                                if http_exc.response else str(http_exc)
                            )
                            errors += 1
                            log.error("netbox_writeback.ip.failed",
                                      device=device_name, address=cidr,
                                      status=getattr(http_exc.response, 'status_code', None),
                                      body=body[:300])
                    except Exception as exc:
                        entry["error"] = str(exc)
                        errors += 1
                        log.error("netbox_writeback.ip.failed",
                                  device=device_name, address=cidr,
                                  error=str(exc))
                changes.append(entry)

    return {
        "checked": len(graph_ips), "created": created,
        "skipped": skipped, "errors": errors,
        "changes": changes,
    }


async def reconcile_cables(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
    nb_iface_map: dict[str, dict[str, int]] | None = None,
    cabled_iface_ids: set[int] | None = None,
) -> dict:
    """Create NetBox cables from high-confidence PHYSICAL_LINK edges.

    Only creates a cable when ALL of:
    * Both endpoint devices are matched (have netbox_id).
    * The discovery protocol is in the high-confidence set
      (lldp, cdp, catc_topology, meraki_topology, ndfc_topology).
    * Both port names can be resolved to a NetBox interface ID.
    * Neither endpoint interface is already cabled in NetBox.

    Interface name resolution tries (in order): active variant → raw
    wire-format name → normalised name.  All comparisons are
    case-insensitive.

    Uses the NetBox v4 terminations API:
      {"a_terminations": [{"object_type": "dcim.interface", "object_id": X}], ...}
    """
    links = await _graph_physical_links()
    if not links:
        return {"checked": 0, "created": 0, "skipped": 0, "errors": 0, "changes": []}

    nb_ids: list[str] = sorted({
        str(lnk[k])
        for lnk in links
        for k in ("nb_id_a", "nb_id_b")
        if lnk.get(k)
    })
    checked = created = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # Build full interface map with cable info if not supplied
        if nb_iface_map is None or cabled_iface_ids is None:
            raw_map = await _fetch_nb_interface_map(client, nb_ids)
            nb_iface_map = {
                dev_id: {name: info["id"] for name, info in ifaces.items()}
                for dev_id, ifaces in raw_map.items()
            }
            cabled_iface_ids = {
                info["id"]
                for ifaces in raw_map.values()
                for info in ifaces.values()
                if info.get("cable")
            }

        def _resolve_iface(nb_id: str, active: str | None, raw: str | None, norm: str | None) -> int | None:
            lookup = nb_iface_map.get(str(nb_id), {})
            for candidate in filter(None, [active, raw, norm]):
                # Use the canonical normalization so ``Twe1/1/1`` matches
                # NetBox's ``TwentyFiveGigE1/1/1`` and ``Port 1`` matches
                # ``Port1`` — keeps cable creation aligned with the iface
                # reconciler's deduplication keys.
                iid = lookup.get(_normalize_iface_name(candidate))
                if iid:
                    return iid
            return None

        seen_pairs: set[frozenset] = set()  # dedup bidirectional links

        self_loops = 0
        for lnk in links:
            nb_id_a = str(lnk.get("nb_id_a") or "")
            nb_id_b = str(lnk.get("nb_id_b") or "")
            if not nb_id_a or not nb_id_b:
                skipped += 1
                continue

            # Quality gate: never push a cable from a device to itself.  These
            # leak in from stale LLDP/CDP records or hairpin loopbacks and
            # would create invalid NetBox cables.
            if nb_id_a == nb_id_b:
                self_loops += 1
                log.info(
                    "netbox_writeback.cable.self_loop_filtered",
                    device=lnk.get("name_a") or nb_id_a,
                    port_a=lnk.get("port_a_raw"),
                    port_b=lnk.get("port_b_raw"),
                    proto=lnk.get("proto"),
                )
                continue

            iid_a = _resolve_iface(
                nb_id_a,
                lnk.get("port_a_active"), lnk.get("port_a_raw"), lnk.get("port_a_norm"),
            )
            iid_b = _resolve_iface(
                nb_id_b,
                lnk.get("port_b_active"), lnk.get("port_b_raw"), lnk.get("port_b_norm"),
            )
            checked += 1

            if not iid_a or not iid_b:
                skipped += 1
                log.debug(
                    "netbox_writeback.cable.no_iface_id",
                    dev_a=lnk.get("name_a"), port_a=lnk.get("port_a_raw"),
                    dev_b=lnk.get("name_b"), port_b=lnk.get("port_b_raw"),
                )
                continue

            pair = frozenset({iid_a, iid_b})
            if pair in seen_pairs:
                skipped += 1
                continue
            seen_pairs.add(pair)

            if iid_a in cabled_iface_ids or iid_b in cabled_iface_ids:
                skipped += 1
                continue

            # NetBox renders the cable's ``label`` field in interface
            # connection columns; if we set label="cdp" the operator
            # only sees "cdp <peer>" and can't tell a cable record
            # was actually created.  Two-step approach:
            #   1) POST with description=proto and NO label, so NetBox
            #      assigns a numeric ID.
            #   2) PATCH the new cable's label to ``cable-{id}`` so
            #      every connection display shows the cable identifier
            #      AND signals "auto-created by netcortex".
            #
            # Cables to virtual interfaces (SVIs, port-channels,
            # loopbacks, ...) are SKIPPED here: NetBox's data model
            # treats cables as strictly physical connections (the only
            # valid ``type`` values are cat5/cat6/fiber/DAC/etc.), and a
            # CDP/LLDP edge to a Vlan SVI is a discovery artifact —
            # the responder resolved the peer by its management IP
            # (which lives on a Vlan), not by a real physical port.
            # Pushing it as a cable would misrepresent the topology.
            proto = (lnk.get("proto") or "discovered").strip()
            port_a_disp = (lnk.get("port_a_raw") or lnk.get("port_a_norm") or "").strip()
            port_b_disp = (lnk.get("port_b_raw") or lnk.get("port_b_norm") or "").strip()

            if _is_virtual_iface_name(port_a_disp) or _is_virtual_iface_name(port_b_disp):
                skipped += 1
                log.info(
                    "netbox_writeback.cable.virtual_endpoint_filtered",
                    dev_a=lnk.get("name_a"), port_a=port_a_disp,
                    dev_b=lnk.get("name_b"), port_b=port_b_disp,
                    proto=proto,
                )
                continue

            payload: dict[str, Any] = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": iid_a}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": iid_b}],
                "status": "connected",
                "description": f"auto-created via netcortex (source: {proto})",
            }

            entry: dict[str, Any] = {
                "device_a": lnk.get("name_a") or nb_id_a,
                "port_a":   port_a_disp,
                "device_b": lnk.get("name_b") or nb_id_b,
                "port_b":   port_b_disp,
                "proto":    proto,
                "applied":  False,
            }

            if dry_run:
                entry["dry_run"] = True
                created += 1
            else:
                try:
                    resp = await client.post(
                        "/api/dcim/cables/",
                        content=_json.dumps(payload),
                    )
                    resp.raise_for_status()
                    cable_id = resp.json().get("id")
                    # Step 2: stamp the label with the assigned ID so
                    # operators can see "cable-{id}" wherever NetBox
                    # renders the cable.
                    if cable_id:
                        try:
                            patch_resp = await client.patch(
                                f"/api/dcim/cables/{cable_id}/",
                                content=_json.dumps({
                                    "label": f"cable-{cable_id}",
                                }),
                            )
                            patch_resp.raise_for_status()
                        except Exception as patch_exc:
                            # Cable still exists; just couldn't stamp
                            # the label.  Log + continue.
                            log.warning(
                                "netbox_writeback.cable.label_patch_failed",
                                cable_id=cable_id, error=str(patch_exc),
                            )
                    entry["nb_cable_id"] = cable_id
                    cabled_iface_ids.add(iid_a)
                    cabled_iface_ids.add(iid_b)
                    entry["applied"] = True
                    created += 1
                    log.info(
                        "netbox_writeback.cable.created",
                        nb_cable_id=cable_id,
                        dev_a=entry["device_a"], port_a=entry["port_a"],
                        dev_b=entry["device_b"], port_b=entry["port_b"],
                        proto=entry["proto"],
                    )
                except Exception as exc:
                    entry["error"] = str(exc)
                    errors += 1
                    log.error(
                        "netbox_writeback.cable.failed",
                        dev_a=entry["device_a"], dev_b=entry["device_b"],
                        error=str(exc),
                    )
            changes.append(entry)

    return {
        "checked": checked, "created": created,
        "skipped": skipped, "errors": errors,
        "self_loops_filtered": self_loops,
        "changes": changes,
    }


# Set of well-known protocol labels we used to stamp on cables before we
# moved the protocol into ``description`` and the cable ID into ``label``.
# Used by the backfill pass to identify *our* cables for migration.
_LEGACY_CABLE_PROTO_LABELS: frozenset[str] = frozenset({
    "cdp", "lldp", "catc_topology", "mac_arp", "discovered",
})

# First-generation cable-ID label scheme (``nc-cable-123``).  Migrated to
# the cleaner ``cable-123`` form per operator request.
_NC_CABLE_LABEL_RE = re.compile(r"^nc-cable-(\d+)$", re.IGNORECASE)


async def reconcile_cable_labels(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Cable-record hygiene: label backfill + virtual-endpoint cleanup.

    NetBox cables model strictly **physical** connections; the only
    valid ``type`` values are copper (cat3-cat8), fiber (mmf/smf), DAC,
    coax, power, USB.  There is no ``virtual`` type — a cable that
    terminates on a Vlan SVI, Port-channel, Loopback, Tunnel, BVI/BDI,
    or Null interface is a CDP/LLDP discovery artifact (the responder
    resolved the peer by its management IP, which happens to live on a
    Vlan, instead of by direct port observation).  Those cables don't
    reflect real wiring and are deleted here.

    Earlier versions stamped the discovery protocol (``cdp``, ``lldp``,
    ``catc_topology``, ``mac_arp``) directly into the cable's ``label``
    field, which NetBox renders inline next to every connected port.
    Operators reported it looked like a free-text annotation rather than
    a real cable record.  This pass also migrates two flavours of label
    to the current ``cable-{id}`` convention:

      * Legacy proto-labelled (``cdp``/``lldp``/``catc_topology``/
        ``mac_arp``/``discovered``) → moves the proto into
        ``description`` (only when ``description`` is empty) and stamps
        ``label = cable-{id}``.
      * First-generation netcortex labels (``nc-cable-{id}``) →
        rewrites to the simpler ``cable-{id}``.

    Cables with any other label (operator-set, manually created) are
    left untouched.
    """
    checked = label_patched = virtual_deleted = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        cables = await _paginate(client, "/api/dcim/cables/", [])
        for cable in cables:
            checked += 1
            cid = cable.get("id")
            label = (cable.get("label") or "").strip()
            desc = (cable.get("description") or "").strip()

            # ── (a) virtual-endpoint deletion ────────────────────────
            # Pull the endpoint names; if any is virtual, this isn't a
            # real physical cable — delete it.
            terms = list(cable.get("a_terminations") or []) + list(
                cable.get("b_terminations") or []
            )
            names = [
                (t.get("object") or {}).get("display") or
                (t.get("object") or {}).get("name") or ""
                for t in terms
            ]
            if any(_is_virtual_iface_name(n) for n in names):
                entry: dict[str, Any] = {
                    "cable_id": cid,
                    "old_label": label,
                    "action": "delete_virtual_endpoint",
                    "endpoint_names": names,
                    "applied": False,
                }
                if dry_run:
                    entry["dry_run"] = True
                    virtual_deleted += 1
                else:
                    try:
                        resp = await client.delete(f"/api/dcim/cables/{cid}/")
                        if resp.status_code in (200, 204, 404):
                            virtual_deleted += 1
                            entry["applied"] = True
                            log.info(
                                "netbox_writeback.cable_label.virtual_deleted",
                                cable_id=cid, endpoints=names,
                            )
                        else:
                            errors += 1
                            entry["error"] = f"HTTP {resp.status_code}"
                    except Exception as exc:
                        errors += 1
                        entry["error"] = str(exc)
                changes.append(entry)
                continue

            # ── (b) label migration ──────────────────────────────────
            patch: dict[str, Any] = {}
            target_label = f"cable-{cid}"
            label_lc = label.lower()
            nc_match = _NC_CABLE_LABEL_RE.match(label)
            if label == target_label:
                pass  # already correct
            elif nc_match:
                # nc-cable-NNN → cable-NNN (rewrite only when the
                # embedded id actually matches this cable; otherwise
                # leave it alone — could be operator-set).
                if nc_match.group(1) == str(cid):
                    patch["label"] = target_label
            elif label_lc in _LEGACY_CABLE_PROTO_LABELS:
                patch["label"] = target_label
                if not desc:
                    patch["description"] = (
                        f"auto-created via netcortex (source: {label_lc})"
                    )

            if not patch:
                skipped += 1
                continue

            entry = {
                "cable_id": cid,
                "old_label": label,
                "new_label": patch.get("label"),
                "description_set": "description" in patch,
                "applied": False,
            }

            if dry_run:
                entry["dry_run"] = True
                label_patched += 1
            else:
                try:
                    resp = await client.patch(
                        f"/api/dcim/cables/{cid}/",
                        content=_json.dumps(patch),
                    )
                    resp.raise_for_status()
                    entry["applied"] = True
                    label_patched += 1
                    log.info(
                        "netbox_writeback.cable_label.patched",
                        cable_id=cid, old=label,
                        new_label=patch.get("label"),
                    )
                except Exception as exc:
                    entry["error"] = str(exc)
                    errors += 1
                    log.error(
                        "netbox_writeback.cable_label.failed",
                        cable_id=cid, error=str(exc),
                    )
            changes.append(entry)

    return {
        "checked": checked,
        # Backwards-compat alias so the orchestrator's ``patched``
        # counter keeps working; equivalent to ``label_patched``.
        "patched": label_patched,
        "label_patched": label_patched,
        "virtual_deleted": virtual_deleted,
        "skipped": skipped, "errors": errors,
        "changes": changes,
    }


async def reconcile_duplicate_meraki_sites(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Resolve NetBox sites that claim the same Meraki ``network_id``.

    Background
    ----------
    The ``dcim.site.custom_fields.meraki_networks`` field is the
    authoritative N:1 mapping from Meraki networks to NetBox sites:
    a single NetBox site can list many Meraki network IDs, but a given
    Meraki network ID must appear in exactly ONE NetBox site.
    When two NetBox sites both list the same network ID (typically a
    legacy ``<name>`` vs auto-generated ``<name>-Meraki-Demo-Day``
    pair) every device in that network ends up duplicated — once per
    site — making the topology view show the same devices under both
    site containers and confusing every downstream NetBox query.

    What this pass does
    -------------------
    1. Pages all ``dcim.site`` records (``custom_fields`` included)
       and bucketizes them by Meraki ``network_id``.
    2. For each bucket with two or more sites, picks a winner via
       :func:`_pick_winner_for_meraki_collision`.
    3. Lists the winner's and each loser's devices, then for every
       loser device whose serial collides with a winner device,
       DELETES the loser device.  NetBox cascade-deletes the loser's
       interfaces and any IPs/cables that hang off them, which is
       precisely what we want — the canonical (winner-side) device
       already has all of these.
    4. PATCHes the loser site's ``meraki_networks`` custom field to
       remove just the colliding entry (other entries on that site
       are preserved).  The loser site itself is **NOT** deleted —
       that's an operator decision left to manual cleanup.

    Idempotent: a second run with no collisions is a no-op.  Devices
    on the loser side with no serial (or with a serial that the winner
    doesn't have) are intentionally **left alone** — we can't safely
    say "this is a duplicate" without a definitive id match.

    Returns
    -------
    ``{
        "collisions_detected":       int,    # total network_id collisions
        "collisions_resolved":       int,    # how many had a winner picked
        "loser_sites_cleared":       int,    # CF entries successfully stripped
        "duplicate_devices_deleted": int,    # device records cascade-deleted
        "delete_errors":             int,    # NetBox refused to delete
        "errors":                    int,    # alias of delete_errors + cf_errors
        "skipped_no_serial":         int,    # loser devices left in place
        "changes": [{
            "network_id":      str,
            "winner":          {"id": int, "name": str},
            "losers": [{
                "id":   int,
                "name": str,
                "duplicate_devices_deleted": int,
                "delete_errors":             int,
                "skipped_no_serial":         int,
                "cf_stripped":               bool,
                "cf_error":                  str | None,
            }, ...],
        }, ...],
    }``
    """
    collisions_detected = 0
    collisions_resolved = 0
    loser_sites_cleared = 0
    duplicate_devices_deleted = 0
    delete_errors = 0
    cf_errors = 0
    skipped_no_serial = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        sites = await _paginate(
            client,
            "/api/dcim/sites/",
            params=[("fields", "id,name,slug,custom_fields")],
        )

        # Bucketize by Meraki network_id present in custom_fields.meraki_networks.
        # A single site can appear in multiple buckets (one per network entry).
        buckets: dict[str, list[dict]] = defaultdict(list)
        for site in sites:
            cf = site.get("custom_fields") or {}
            for net in cf.get("meraki_networks") or []:
                nid = (net.get("id") or "").strip()
                if nid:
                    buckets[nid].append(site)

        for nid, candidate_sites in buckets.items():
            # Deduplicate by site id — a single site can have the same network
            # listed twice in its array (operator typo) and that's a separate
            # bug we don't try to fix here.
            seen_ids: set[int] = set()
            unique_candidates: list[dict] = []
            for s in candidate_sites:
                sid = int(s.get("id") or 0)
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    unique_candidates.append(s)

            if len(unique_candidates) < 2:
                continue   # no collision

            collisions_detected += 1
            winner = _pick_winner_for_meraki_collision(nid, unique_candidates)
            if winner is None:
                log.warning(
                    "netbox_writeback.collision.no_winner",
                    network_id=nid,
                    candidates=[s.get("name") for s in unique_candidates],
                )
                continue
            collisions_resolved += 1

            losers = [s for s in unique_candidates
                      if int(s.get("id") or 0) != int(winner.get("id") or 0)]

            entry: dict[str, Any] = {
                "network_id": nid,
                "winner": {
                    "id":   int(winner.get("id") or 0),
                    "name": winner.get("name") or "",
                },
                "losers": [],
            }

            # Build serial → device_id map for the winner.  Devices without a
            # serial are uniqued by lowercased name as a defensive secondary
            # key — but only for the winner side (the loser-side key is
            # always "serial only" because name-based dedupe across sites is
            # unreliable for operator-renamed devices).
            winner_devs = await _paginate(
                client,
                "/api/dcim/devices/",
                params=[("site_id", winner["id"]),
                        ("fields", "id,name,serial")],
            )
            winner_serials: set[str] = set()
            for d in winner_devs:
                ser = (d.get("serial") or "").strip()
                if ser:
                    winner_serials.add(ser)

            for loser in losers:
                lid = int(loser.get("id") or 0)
                lname = loser.get("name") or ""
                loser_entry: dict[str, Any] = {
                    "id":   lid,
                    "name": lname,
                    "duplicate_devices_deleted": 0,
                    "delete_errors":             0,
                    "skipped_no_serial":         0,
                    "cf_stripped":               False,
                    "cf_error":                  None,
                }

                loser_devs = await _paginate(
                    client,
                    "/api/dcim/devices/",
                    params=[("site_id", lid),
                            ("fields", "id,name,serial")],
                )

                for d in loser_devs:
                    dev_id = int(d.get("id") or 0)
                    ser = (d.get("serial") or "").strip()
                    if not ser:
                        loser_entry["skipped_no_serial"] += 1
                        skipped_no_serial += 1
                        log.info(
                            "netbox_writeback.collision.loser_device.skip_no_serial",
                            network_id=nid,
                            loser_site=lname,
                            device_id=dev_id,
                            device_name=d.get("name"),
                        )
                        continue
                    if ser not in winner_serials:
                        # Loser-only device — don't delete; operator may have
                        # moved hardware between sites and the winner just
                        # hasn't seen it yet.
                        loser_entry["skipped_no_serial"] += 1
                        skipped_no_serial += 1
                        log.info(
                            "netbox_writeback.collision.loser_device.skip_unique_serial",
                            network_id=nid,
                            loser_site=lname,
                            device_id=dev_id,
                            device_name=d.get("name"),
                            serial=ser,
                        )
                        continue

                    if dry_run:
                        loser_entry["duplicate_devices_deleted"] += 1
                        duplicate_devices_deleted += 1
                        continue

                    try:
                        resp = await client.delete(f"/api/dcim/devices/{dev_id}/")
                        if resp.status_code in (200, 202, 204):
                            loser_entry["duplicate_devices_deleted"] += 1
                            duplicate_devices_deleted += 1
                            log.info(
                                "netbox_writeback.collision.loser_device.deleted",
                                network_id=nid,
                                loser_site=lname,
                                device_id=dev_id,
                                device_name=d.get("name"),
                                serial=ser,
                            )
                        else:
                            loser_entry["delete_errors"] += 1
                            delete_errors += 1
                            log.warning(
                                "netbox_writeback.collision.loser_device.delete_failed",
                                network_id=nid,
                                loser_site=lname,
                                device_id=dev_id,
                                status=resp.status_code,
                                body=resp.text[:300],
                            )
                    except Exception as exc:
                        loser_entry["delete_errors"] += 1
                        delete_errors += 1
                        log.warning(
                            "netbox_writeback.collision.loser_device.delete_failed",
                            network_id=nid,
                            loser_site=lname,
                            device_id=dev_id,
                            error=str(exc),
                        )

                # Strip the colliding entry from the loser's
                # ``meraki_networks`` custom field so the collision stops
                # firing on every poll cycle.  Other entries (if any) are
                # preserved.  We patch the *whole* custom field because
                # NetBox treats list custom fields as opaque blobs.
                cf = loser.get("custom_fields") or {}
                mn = cf.get("meraki_networks") or []
                new_mn = [n for n in mn if (n.get("id") or "").strip() != nid]
                if len(new_mn) != len(mn):
                    if dry_run:
                        loser_entry["cf_stripped"] = True
                        loser_sites_cleared += 1
                    else:
                        try:
                            resp = await client.patch(
                                f"/api/dcim/sites/{lid}/",
                                content=_json.dumps(
                                    {"custom_fields": {"meraki_networks": new_mn}}
                                ),
                            )
                            resp.raise_for_status()
                            loser_entry["cf_stripped"] = True
                            loser_sites_cleared += 1
                            log.info(
                                "netbox_writeback.collision.loser_site.cf_stripped",
                                network_id=nid,
                                loser_site=lname,
                                remaining_networks=len(new_mn),
                            )
                        except Exception as exc:
                            body = ""
                            if isinstance(exc, httpx.HTTPStatusError) and exc.response:
                                body = exc.response.text[:300]
                            loser_entry["cf_error"] = f"{exc}: {body}".strip(": ")
                            cf_errors += 1
                            log.warning(
                                "netbox_writeback.collision.loser_site.cf_patch_failed",
                                network_id=nid,
                                loser_site=lname,
                                error=str(exc),
                                body=body,
                            )

                entry["losers"].append(loser_entry)

            changes.append(entry)

    return {
        "collisions_detected":       collisions_detected,
        "collisions_resolved":       collisions_resolved,
        "loser_sites_cleared":       loser_sites_cleared,
        "duplicate_devices_deleted": duplicate_devices_deleted,
        "delete_errors":             delete_errors,
        "skipped_no_serial":         skipped_no_serial,
        "errors":                    delete_errors + cf_errors,
        "changes":                   changes,
    }


async def reconcile_devices_create(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Create devices in NetBox that exist in the graph but not in NetBox.

    Per operator policy:
      * The new NetBox device uses the platform-observed name (``Device.name``)
        verbatim — NOT the NetBox-authoritative display name (which doesn't
        exist yet for an absent device).
      * Devices already in NetBox (matched by serial / native ID via
        ``enrich_devices_from_netbox``) keep their NetBox name; this pass
        never touches them.
      * Site assignment uses the ``netbox_site_slug`` stamped by
        ``enrich_sites_from_netbox`` (which resolves through the
        ``meraki_networks`` custom field).  Devices whose site cannot be
        resolved are skipped with ``skip_reason=site_unresolved``.
      * ``device_type`` is resolved by the observed model.  Missing types
        result in ``skip_reason=device_type_missing`` — the operator can
        add the type in NetBox and re-run.

    Skipped devices show up in the report so operators can see exactly
    what needs to happen (add device-type, fix site mapping, etc.).
    """
    absent = await _graph_absent_devices()
    if not absent:
        return {
            "checked": 0, "created": 0, "skipped": 0, "errors": 0,
            "skip_reasons": {}, "changes": [],
        }

    checked = created = errors = 0
    skipped_by_reason: dict[str, int] = defaultdict(int)
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        sites_idx, roles_idx, types_idx = await _fetch_nb_lookup_maps(client)

        for dev in absent:
            checked += 1
            adapter_name = (dev.get("adapter_name") or "").strip()
            serial       = (dev.get("serial") or "").strip()
            model        = (dev.get("model") or "").strip()
            platform     = (dev.get("platform") or "").strip().lower()
            site_slug    = (dev.get("netbox_site_slug") or "").strip().lower()

            entry: dict[str, Any] = {
                "adapter_name": adapter_name,
                "serial":       serial,
                "model":        model,
                "platform":     platform,
                "site_slug":    site_slug,
                "applied":      False,
            }

            if not adapter_name:
                # Shouldn't happen — every Device has a name — but be defensive.
                entry["skipped"] = True
                entry["skip_reason"] = "no_adapter_name"
                skipped_by_reason["no_adapter_name"] += 1
                changes.append(entry)
                continue

            site_id = sites_idx.get(site_slug)
            if not site_id:
                entry["skipped"] = True
                entry["skip_reason"] = "site_unresolved"
                skipped_by_reason["site_unresolved"] += 1
                changes.append(entry)
                continue

            # device_type lookup: try slug-normalised model, then raw model.
            model_key = model.lower()
            type_id = (
                types_idx.get(model_key)
                or types_idx.get(model_key.replace(" ", "-"))
                or types_idx.get(model_key.replace("_", "-"))
            )
            if not type_id:
                entry["skipped"] = True
                entry["skip_reason"] = "device_type_missing"
                entry["hint"] = f"add device-type slug or model '{model}' in NetBox"
                skipped_by_reason["device_type_missing"] += 1
                changes.append(entry)
                continue

            role_slug = _resolve_role_slug(platform, model)
            role_id   = roles_idx.get(role_slug)
            if not role_id:
                entry["skipped"] = True
                entry["skip_reason"] = "role_missing"
                entry["hint"] = f"add device-role slug '{role_slug}' in NetBox"
                skipped_by_reason["role_missing"] += 1
                changes.append(entry)
                continue

            payload: dict[str, Any] = {
                "name":        adapter_name,
                "device_type": type_id,
                "role":        role_id,
                "site":        site_id,
                "status":      "active",
            }
            if serial:
                payload["serial"] = serial[:50]   # NetBox max length

            entry["role_slug"]   = role_slug
            entry["device_type"] = model

            if dry_run:
                entry["dry_run"] = True
                created += 1
                changes.append(entry)
                continue

            try:
                resp = await client.post(
                    "/api/dcim/devices/",
                    content=_json.dumps(payload),
                )
                resp.raise_for_status()
                new_id = resp.json().get("id")
                entry["netbox_id"] = new_id
                entry["applied"] = True
                created += 1
                log.info(
                    "netbox_writeback.device.created",
                    name=adapter_name, model=model, role=role_slug,
                    site=site_slug, netbox_id=new_id,
                )
            except httpx.HTTPStatusError as http_exc:
                body = http_exc.response.text if http_exc.response else ""
                # Idempotency: NetBox returns 400 with a "name" or "serial"
                # uniqueness error if a device with the same name+site or
                # the same serial already exists.  Treat that as a benign
                # skip — the device IS in NetBox, we just didn't match it
                # during enrichment (likely a serial casing or whitespace
                # quirk that will resolve on the next enrich pass).
                if (http_exc.response is not None
                        and http_exc.response.status_code == 400
                        and ("already exists" in body
                             or "unique set" in body)):
                    entry["skipped"] = True
                    entry["skip_reason"] = "already_exists"
                    entry["nb_message"] = body[:300]
                    skipped_by_reason["already_exists"] += 1
                    log.info(
                        "netbox_writeback.device.already_exists",
                        name=adapter_name, nb_message=body[:300],
                    )
                else:
                    entry["error"] = (
                        f"HTTP {http_exc.response.status_code}: {body[:300]}"
                        if http_exc.response else str(http_exc)
                    )
                    errors += 1
                    log.error(
                        "netbox_writeback.device.failed",
                        name=adapter_name,
                        status=getattr(http_exc.response, "status_code", None),
                        body=body[:300],
                    )
            except Exception as exc:
                entry["error"] = str(exc)
                errors += 1
                log.error("netbox_writeback.device.failed",
                          name=adapter_name, error=str(exc))
            changes.append(entry)

    return {
        "checked": checked, "created": created,
        "skipped": sum(skipped_by_reason.values()),
        "errors": errors,
        "skip_reasons": dict(skipped_by_reason),
        "changes": changes,
    }


async def reconcile_ip_assignments(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    nb_iface_map: dict[str, dict[str, int]] | None = None,
    dry_run: bool = False,
) -> dict:
    """Attach existing-but-unassigned NetBox IPs to their owning interfaces.

    This catches the common case where NetBox already has an IP record
    (e.g., entered by an operator, or auto-discovered by an nmap scan
    long ago) but its ``assigned_object`` is blank.  The reconciler's
    create pass correctly recognises the IP as already-present and
    skips creation; this pass goes one step further and fills in the
    assignment when the graph knows the owning ``(device, interface)``.

    Rules
    -----
    * Only modifies records where ``assigned_object_id`` is currently null.
    * Matches NetBox records to graph IPs by **bare host address** (the
      prefix length may differ — NetBox might store /19 while the graph
      reports /32 — that's the operator's prefix-hint convention).
    * Never changes the address or prefix length.
    * Never overwrites an existing assignment (NetBox is authoritative
      for intentional bindings).
    """
    graph_ips = await _graph_ips()
    if not graph_ips:
        return {
            "checked": 0, "assigned": 0, "skipped": 0, "errors": 0,
            "changes": [],
        }

    # Group graph IPs by the owning matched device.
    by_nb_id: dict[str, list[dict]] = defaultdict(list)
    for row in graph_ips:
        by_nb_id[str(row["netbox_id"])].append(row)

    # Collect every bare host the graph claims a matched device owns; we'll
    # ask NetBox if it has unassigned records for any of these.
    bare_hosts = sorted({
        (row.get("address") or "").strip().split("/")[0]
        for row in graph_ips
        if (row.get("address") or "").strip()
    })
    bare_hosts = [h for h in bare_hosts if h]

    nb_ids   = list(by_nb_id.keys())
    assigned = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # Need an iface map even if the caller pre-computed one for
        # reconcile_ip_addresses — that's fine, just reuse.
        if nb_iface_map is None:
            flat = await _fetch_nb_interface_map(client, nb_ids)
            nb_iface_map = {
                dev_id: {name: info["id"] for name, info in ifaces.items()}
                for dev_id, ifaces in flat.items()
            }

        unassigned_by_host = await _fetch_unassigned_ips_by_host(client, bare_hosts)
        if not unassigned_by_host:
            return {
                "checked": len(graph_ips), "assigned": 0,
                "skipped": 0, "errors": 0, "changes": [],
            }

        for nb_id, rows in by_nb_id.items():
            iface_lookup = nb_iface_map.get(nb_id, {})
            for row in rows:
                bare = (row.get("address") or "").strip().split("/")[0]
                iface_name = (row.get("iface_name") or "").strip()
                if not bare or not iface_name:
                    continue

                candidates = unassigned_by_host.get(bare) or []
                if not candidates:
                    continue

                iface_id = iface_lookup.get(_normalize_iface_name(iface_name))
                if not iface_id:
                    skipped += 1
                    continue

                device_name = row.get("device_name") or nb_id
                for rec in candidates:
                    ip_id = rec["id"]
                    addr  = rec.get("address") or bare
                    entry: dict[str, Any] = {
                        "device":     device_name,
                        "interface":  iface_name,
                        "ip_id":      ip_id,
                        "address":    addr,
                        "applied":    False,
                    }

                    if dry_run:
                        entry["dry_run"] = True
                        assigned += 1
                        changes.append(entry)
                        continue

                    try:
                        resp = await client.patch(
                            f"/api/ipam/ip-addresses/{ip_id}/",
                            content=_json.dumps({
                                "assigned_object_type": "dcim.interface",
                                "assigned_object_id":   iface_id,
                            }),
                        )
                        resp.raise_for_status()
                        entry["applied"] = True
                        assigned += 1
                        log.info(
                            "netbox_writeback.ip_assign.attached",
                            device=device_name, interface=iface_name,
                            ip_id=ip_id, address=addr,
                        )
                    except Exception as exc:
                        entry["error"] = str(exc)
                        errors += 1
                        log.error(
                            "netbox_writeback.ip_assign.failed",
                            device=device_name, interface=iface_name,
                            ip_id=ip_id, error=str(exc),
                        )
                    changes.append(entry)

    return {
        "checked":  len(graph_ips),
        "assigned": assigned,
        "skipped":  skipped,
        "errors":   errors,
        "changes":  changes,
    }


# ── Data-quality analysis (read-only, no writes) ───────────────────────────────

async def analyse_field_mismatches() -> list[dict]:
    """Return devices where a real field (e.g., serial) disagrees with NetBox.

    Name divergences are NOT included here per operator policy: the
    platform-observed name and the NetBox name are allowed to differ
    (NetBox is authoritative for display via ``Device.display_name``,
    but the underlying ``Device.name`` retains the adapter-observed
    label).  See ``_compute_netbox_delta`` in ``netbox_enrich.py``.

    This is informational only — neither side is mutated here.  The
    report goes into ``reconcile_to_netbox()`` so operators know which
    devices to investigate (typically a serial typo on one side).
    """
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND d.netbox_delta IS NOT NULL
              AND d.netbox_delta <> ''
              AND d.netbox_delta CONTAINS '"type": "field_mismatch"'
            RETURN coalesce(d.display_name, d.name) AS device,
                   d.netbox_id        AS netbox_id,
                   d.netbox_delta     AS delta_json,
                   d.netbox_site_slug AS observed_site
            LIMIT 500
            """
        )
        rows = await result.data()

    mismatches = []
    for row in rows:
        try:
            delta = _json.loads(row.get("delta_json") or "{}")
        except Exception:
            delta = {}
        # The Cypher CONTAINS filter is intentionally loose; defensively
        # re-check the parsed payload so a stray "field_mismatch" string
        # in a different shape never sneaks through.
        if delta.get("type") != "field_mismatch":
            continue
        # Drop both the synthetic _name_divergence companion key (new
        # schema) and the legacy ``name`` key (deltas computed by older
        # netcortex revisions, which haven't been refreshed by an
        # enrichment cycle yet).  Operators only care about real
        # data-integrity issues like serial mismatch.
        flagged_fields = {
            k: v for k, v in (delta.get("fields") or {}).items()
            if k not in ("_name_divergence", "name")
        }
        if not flagged_fields:
            continue
        mismatches.append({
            "device":        row["device"],
            "netbox_id":     row["netbox_id"],
            "delta":         {"type": "field_mismatch", "fields": flagged_fields},
            "observed_site": row.get("observed_site"),
        })
    return mismatches


# Backward-compatible alias — old callers that still import the previous
# name should keep working until they're migrated to the new label.
analyse_site_mismatches = analyse_field_mismatches


async def analyse_absent_devices() -> list[dict]:
    """Return devices present in the graph but absent from NetBox."""
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.netbox_delta = '{"type": "absent_in_netbox"}'
              AND d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite)
            RETURN coalesce(d.display_name, d.name) AS device,
                   d.id          AS graph_id,
                   d.serial      AS serial,
                   d.model       AS model,
                   d.mgmt_ip     AS mgmt_ip,
                   d.platform    AS platform,
                   ps.name       AS platform_site
            LIMIT 500
            """
        )
        return await result.data()


# ── Main entry point ───────────────────────────────────────────────────────────

async def reconcile_interface_naming(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Bring NetBox interface naming in line with the device's platform.

    Two responsibilities, both *only* affecting interfaces whose name
    disagrees with the device's platform convention:

      1. **Delete wrong-platform interfaces**.  On a device whose
         ``device_type.model`` is *not* Meraki (MS/MR/MX/MV/MT/MG/CW),
         any NetBox interface whose name matches the Meraki-dashboard
         pattern (``Port 1``, ``Port1_C9300X-NM-8Y_7``, ``port-2``,
         ``Port 1::xyz::1``, ...) is treated as a stale dashboard
         label that doesn't belong.  We delete it iff:
             * it is not cabled, AND
             * it has no IP addresses assigned, AND
             * the canonical-key form of the same name is *not* present
               in the graph's **platform-preferred** source (``snmp-if``
               for Cisco devices, ``meraki-if`` for Meraki).  Counting
               only the preferred source means a Cisco IOS device that
               is also Meraki-Dashboard-enrolled actually gets cleaned
               up — its ``meraki-if`` graph rows don't act as a false
               positive for "this is a real interface".

      2. **Backfill `meraki_port_id` and friends on existing Meraki
         interfaces**.  For each NetBox interface on a Meraki-OS
         device, find the matching graph ``meraki-if:*`` Interface by
         canonical name key and PATCH ``custom_fields.meraki_port_id``
         (+ ``meraki_serial``, ``nc_platform``, ``nc_platform_id``)
         when the existing NetBox values are missing.  We never
         overwrite a non-empty operator-set value.

    Returns ``{"checked", "deleted", "patched", "skipped", "errors",
    "skip_reasons", "changes"}``.
    """
    graph_ifaces = await _graph_interfaces()
    by_nb_id: dict[str, list[dict]] = defaultdict(list)
    for it in graph_ifaces:
        by_nb_id[str(it["netbox_id"])].append(it)

    nb_ids = list(by_nb_id.keys())
    deleted = patched = skipped = errors = 0
    skip_reasons: dict[str, int] = defaultdict(int)
    changes: list[dict] = []

    if not nb_ids:
        return {
            "checked": 0, "deleted": 0, "patched": 0,
            "skipped": 0, "errors": 0, "skip_reasons": {},
            "changes": [],
        }

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # Pull device metadata (model + serial) for source decisions.
        nb_device_model: dict[str, str] = {}
        nb_device_serial: dict[str, str] = {}
        for i in range(0, len(nb_ids), _BATCH):
            chunk = nb_ids[i : i + _BATCH]
            params = [("id", nid) for nid in chunk]
            for rec in await _paginate(client, "/api/dcim/devices/", params):
                nb_device_model[str(rec["id"])] = (
                    (rec.get("device_type") or {}).get("model") or ""
                ).upper()
                nb_device_serial[str(rec["id"])] = (rec.get("serial") or "").strip()

        # Pull all NetBox interfaces for these devices (with cable / IP
        # counts + custom_fields).  We can't reuse _fetch_nb_interface_map
        # because it strips custom_fields; do it manually here.
        nb_full: dict[str, list[dict]] = defaultdict(list)
        for i in range(0, len(nb_ids), _BATCH):
            chunk = nb_ids[i : i + _BATCH]
            params = [("device_id", nid) for nid in chunk]
            for rec in await _paginate(client, "/api/dcim/interfaces/", params):
                nb_full[str(rec["device"]["id"])].append(rec)

        checked = 0
        for nb_id, ifaces in by_nb_id.items():
            parent_model  = nb_device_model.get(nb_id, "")
            parent_serial = nb_device_serial.get(nb_id, "")
            parent_is_meraki = any(
                parent_model.startswith(p)
                for p in _MERAKI_DEVICE_MODEL_PREFIXES
            )

            # Build canonical-key sets from the graph for this device.
            # The safety-belt key set must use only the *platform-preferred*
            # source — otherwise a Cisco-IOS-also-Meraki box would never get
            # cleaned up (its meraki-if rows always provide a "match" for the
            # very names we want to delete).  For non-Meraki devices the
            # preferred source is snmp-if; for Meraki it's meraki-if.
            preferred_src = "meraki-if" if parent_is_meraki else "snmp-if"
            graph_keys_preferred: set[str] = set()
            meraki_iface_by_key: dict[str, dict] = {}
            for it in ifaces:
                key = _normalize_iface_name(it.get("name") or "")
                src = _iface_source(it.get("iface_id"))
                if src == preferred_src:
                    graph_keys_preferred.add(key)
                if src == "meraki-if":
                    meraki_iface_by_key[key] = it

            # ── Part 0: detect normalization-collision duplicates ──────
            # SNMP polling sometimes returns the SAME logical interface
            # under two different names (``Vlan1`` AND ``VLAN-1`` from
            # two different MIB walks; ``Twe1/1/1`` AND
            # ``TwentyFiveGigE1/1/1`` from short/long-form mismatches),
            # so the iface reconciler ended up creating two NetBox
            # records.  The current iface map keys on the canonical
            # form and skips creates, but historical duplicates remain.
            #
            # For each device we group NetBox interfaces by canonical
            # key and pick a "winner" — preferring entries with IPs,
            # then cables, then platform-preferred graph backing, then
            # the shorter / less-mangled name.  Losers are migrated
            # (cable moved to winner if winner is uncabled, etc.) and
            # deleted.
            by_key: dict[str, list[dict]] = defaultdict(list)
            for nb_iface in nb_full.get(nb_id, []):
                by_key[_normalize_iface_name(nb_iface["name"])].append(nb_iface)

            dedupe_skip_ids: set[int] = set()
            for key, group in by_key.items():
                if len(group) < 2:
                    continue

                # Score each entry; highest score wins.
                def _score(it: dict) -> tuple:
                    has_ip = (it.get("count_ipaddresses") or 0) > 0
                    cabled = it.get("cable") is not None
                    # Prefer the entry whose name already matches what
                    # the graph's preferred source carries (e.g. Vl1
                    # over VLAN-1 when SNMP returned Vl1).
                    pref_match = (
                        _normalize_iface_name(it["name"]) in graph_keys_preferred
                        and not _is_wrong_platform_iface_name(it["name"])
                    )
                    # Tie-break: shorter name (Vl1 < VLAN-1), then
                    # smaller id (older / more established record).
                    return (
                        int(has_ip),
                        int(cabled),
                        int(pref_match),
                        -len(it["name"]),
                        -int(it["id"]),
                    )

                group_sorted = sorted(group, key=_score, reverse=True)
                winner = group_sorted[0]
                losers = group_sorted[1:]

                for loser in losers:
                    dedupe_skip_ids.add(loser["id"])
                    loser_cabled = loser.get("cable") is not None
                    loser_has_ip = (loser.get("count_ipaddresses") or 0) > 0
                    entry: dict[str, Any] = {
                        "device": loser["device"]["name"],
                        "interface": loser["name"],
                        "netbox_device_id": nb_id,
                        "action": "dedupe_loser",
                        "winner_id": winner["id"],
                        "winner_name": winner["name"],
                        "loser_cabled": loser_cabled,
                        "loser_has_ip": loser_has_ip,
                        "applied": False,
                    }
                    checked += 1

                    # Refuse to migrate IPs (complex + risky).  If the
                    # loser has IPs, flag for operator review and skip.
                    if loser_has_ip:
                        skipped += 1
                        skip_reasons["dedupe_loser_has_ips"] += 1
                        entry["skipped"] = "loser_has_ips"
                        changes.append(entry)
                        continue

                    # If loser is cabled and winner isn't, move the
                    # cable's termination from loser → winner so we
                    # preserve the link.  Then delete the loser.
                    #
                    # SPECIAL CASE: if the winner is a virtual
                    # interface (Vlan SVI, Port-channel, ...), the
                    # cable is a discovery artifact — NetBox cables
                    # are strictly physical; ``reconcile_cable_labels``
                    # will delete it on its next pass.  Don't even try
                    # to migrate it; just let the loser deletion drop
                    # the orphan termination, and let the cable-hygiene
                    # pass clean up the cable itself.
                    if loser_cabled and winner.get("cable") is None:
                        if _is_virtual_iface_name(winner["name"]):
                            entry["cable_id"] = loser["cable"]["id"]
                            entry["cable_orphaned_for_hygiene"] = True
                        else:
                            cab = loser["cable"]
                            cab_id = cab["id"]
                            entry["cable_id"] = cab_id

                            if dry_run:
                                entry["dry_run_cable_move"] = True
                            else:
                                try:
                                    # Read the cable to know which side
                                    # the loser is on.
                                    cresp = await client.get(
                                        f"/api/dcim/cables/{cab_id}/"
                                    )
                                    cresp.raise_for_status()
                                    cab_full = cresp.json()
                                    patched_terms: dict[str, Any] = {}
                                    for side in ("a_terminations", "b_terminations"):
                                        new_terms = []
                                        for t in cab_full.get(side) or []:
                                            if (t.get("object_type") == "dcim.interface"
                                                    and (t.get("object") or {}).get("id") == loser["id"]):
                                                new_terms.append({
                                                    "object_type": "dcim.interface",
                                                    "object_id": winner["id"],
                                                })
                                            else:
                                                obj = t.get("object") or {}
                                                new_terms.append({
                                                    "object_type": t.get("object_type"),
                                                    "object_id": obj.get("id"),
                                                })
                                        patched_terms[side] = new_terms
                                    presp = await client.patch(
                                        f"/api/dcim/cables/{cab_id}/",
                                        content=_json.dumps(patched_terms),
                                    )
                                    presp.raise_for_status()
                                    entry["cable_moved"] = True
                                except Exception as exc:
                                    errors += 1
                                    entry["error"] = f"cable_move: {exc}"
                                    changes.append(entry)
                                    continue
                    elif loser_cabled and winner.get("cable") is not None:
                        # Both have cables → ambiguous, leave alone.
                        skipped += 1
                        skip_reasons["dedupe_both_cabled"] += 1
                        entry["skipped"] = "both_cabled"
                        changes.append(entry)
                        continue

                    # Now delete the loser.
                    if dry_run:
                        entry["dry_run"] = True
                        deleted += 1
                    else:
                        try:
                            resp = await client.delete(
                                f"/api/dcim/interfaces/{loser['id']}/"
                            )
                            if resp.status_code in (200, 204):
                                deleted += 1
                                entry["applied"] = True
                                log.info(
                                    "netbox_writeback.iface_naming.dedupe_deleted",
                                    device=loser["device"]["name"],
                                    loser=loser["name"], winner=winner["name"],
                                    cable_moved=entry.get("cable_moved", False),
                                )
                            elif resp.status_code == 404:
                                # Already gone — treat as success.
                                deleted += 1
                                entry["applied"] = True
                            else:
                                errors += 1
                                entry["error"] = f"HTTP {resp.status_code}"
                        except Exception as exc:
                            errors += 1
                            entry["error"] = str(exc)
                    changes.append(entry)

            # ── Part 0.5: rename demonstrably-non-canonical names ───
            # The ONLY pattern we currently rewrite is the operator/
            # import-style ``VLAN-N`` (all caps, hyphenated digit) —
            # it's never produced by a Cisco platform natively and
            # always appears alongside a real platform name on the
            # same device (``Vl1``/``Vlan1`` from SNMP).  Other
            # variants — ``GigabitEthernet0/0/1`` (IOS-XE running-
            # config canonical), ``Ethernet1/1`` (NX-OS / UCS
            # canonical), ``Port 1`` (Meraki MS), ``wan1`` (Meraki MX)
            # — are platform-native and MUST NOT be silently rewritten.
            live_names: set[str] = {
                nb_iface["name"]
                for nb_iface in nb_full.get(nb_id, [])
                if nb_iface["id"] not in dedupe_skip_ids
            }

            for nb_iface in nb_full.get(nb_id, []):
                if nb_iface["id"] in dedupe_skip_ids:
                    continue
                name = nb_iface["name"]
                if not _NON_CANONICAL_VLAN_NAME_RE.match(name):
                    continue
                target = _canonical_short_name(name)
                if target == name:
                    continue
                if target in live_names:
                    # Already an interface with the canonical name —
                    # don't double-create; let the dedupe pass on the
                    # NEXT run pick a winner.
                    skip_reasons["rename_target_exists"] += 1
                    continue

                entry: dict[str, Any] = {
                    "device": nb_iface["device"]["name"],
                    "interface": name,
                    "netbox_device_id": nb_id,
                    "action": "rename_to_canonical",
                    "from_name": name,
                    "to_name": target,
                    "applied": False,
                }
                checked += 1
                if dry_run:
                    entry["dry_run"] = True
                    patched += 1
                    # Reflect the rename locally so subsequent renames
                    # in this same run see the collision correctly.
                    live_names.discard(name)
                    live_names.add(target)
                    nb_iface["name"] = target  # so type-patch sees it
                else:
                    try:
                        resp = await client.patch(
                            f"/api/dcim/interfaces/{nb_iface['id']}/",
                            json={"name": target},
                        )
                        resp.raise_for_status()
                        patched += 1
                        entry["applied"] = True
                        live_names.discard(name)
                        live_names.add(target)
                        nb_iface["name"] = target
                        log.info(
                            "netbox_writeback.iface_naming.renamed",
                            device=entry["device"], old=name, new=target,
                        )
                    except Exception as exc:
                        errors += 1
                        entry["error"] = str(exc)
                changes.append(entry)

            for nb_iface in nb_full.get(nb_id, []):
                # Already handled as a dedupe loser?  Skip.
                if nb_iface["id"] in dedupe_skip_ids:
                    continue
                checked += 1
                name = nb_iface["name"]
                key  = _normalize_iface_name(name)
                cabled = nb_iface.get("cable") is not None
                has_ip = (nb_iface.get("count_ipaddresses") or 0) > 0
                device_name = nb_iface["device"]["name"]
                entry: dict[str, Any] = {
                    "device": device_name, "interface": name,
                    "netbox_device_id": nb_id,
                    "applied": False,
                }

                # ── Part 1: delete wrong-platform interfaces ────────
                if (not parent_is_meraki
                        and _is_wrong_platform_iface_name(name)
                        and not cabled and not has_ip
                        and key not in graph_keys_preferred):
                    # Wrong-platform AND safe to delete AND no
                    # platform-native graph interface has the same key
                    # (that would mean it's a legitimate alias).
                    entry["action"] = "delete_wrong_platform"
                    entry["parent_model"] = parent_model
                    if dry_run:
                        entry["dry_run"] = True
                        deleted += 1
                    else:
                        try:
                            resp = await client.delete(
                                f"/api/dcim/interfaces/{nb_iface['id']}/"
                            )
                            if resp.status_code in (200, 204):
                                deleted += 1
                                entry["applied"] = True
                                log.info(
                                    "netbox_writeback.iface_naming.deleted",
                                    device=device_name, interface=name,
                                    parent_model=parent_model,
                                )
                            else:
                                errors += 1
                                entry["error"] = f"HTTP {resp.status_code}"
                        except Exception as exc:
                            errors += 1
                            entry["error"] = str(exc)
                    changes.append(entry)
                    continue

                # ── Part 1b: patch wrong interface type for SVIs ─────
                # NetBox SVIs commonly come in as ``1000base-t`` (the
                # speed inherited from the underlying physical link
                # via SNMP ifTable) or ``other`` (operator-entered).
                # Force them to ``virtual`` so cable tracing and
                # capacity reports stop double-counting them as 1G
                # physical links.
                cur_type = (nb_iface.get("type") or {}).get("value") or ""
                if _is_virtual_iface_name(name) and cur_type != "virtual":
                    entry["action"] = "patch_iface_type"
                    entry["from_type"] = cur_type
                    entry["to_type"] = "virtual"
                    if dry_run:
                        entry["dry_run"] = True
                        patched += 1
                    else:
                        try:
                            resp = await client.patch(
                                f"/api/dcim/interfaces/{nb_iface['id']}/",
                                json={"type": "virtual"},
                            )
                            resp.raise_for_status()
                            patched += 1
                            entry["applied"] = True
                            log.info(
                                "netbox_writeback.iface_naming.type_patched",
                                device=device_name, interface=name,
                                old=cur_type, new="virtual",
                            )
                        except Exception as exc:
                            errors += 1
                            entry["error"] = str(exc)
                    changes.append(entry)
                    continue

                # ── Part 2: stamp meraki_port_id + related custom
                # fields on Meraki-OS device interfaces.  These are
                # computed by netcortex from authoritative adapter
                # data (Meraki API), so we always sync them to the
                # graph value — they're not operator-maintained.  We
                # only emit a PATCH when at least one field actually
                # differs from what NetBox currently holds.
                if not parent_is_meraki:
                    continue
                g = meraki_iface_by_key.get(key)
                if not g or not g.get("port_id"):
                    continue

                graph_port_id = str(g["port_id"])
                desired: dict[str, str] = {
                    "meraki_port_id": graph_port_id,
                    "nc_platform":    "meraki-if",
                    "nc_platform_id": graph_port_id,
                }
                if parent_serial:
                    desired["meraki_serial"] = parent_serial

                cf = nb_iface.get("custom_fields") or {}
                want: dict[str, Any] = {}
                for fname, fval in desired.items():
                    existing = cf.get(fname)
                    existing_str = (str(existing).strip()
                                    if existing is not None else "")
                    if existing_str != fval:
                        want[fname] = fval

                if not want:
                    skipped += 1
                    skip_reasons["already_stamped"] += 1
                    continue

                entry["action"] = "patch_custom_fields"
                entry["custom_fields"] = want
                if dry_run:
                    entry["dry_run"] = True
                    patched += 1
                else:
                    try:
                        resp = await client.patch(
                            f"/api/dcim/interfaces/{nb_iface['id']}/",
                            json={"custom_fields": want},
                        )
                        resp.raise_for_status()
                        patched += 1
                        entry["applied"] = True
                        log.info(
                            "netbox_writeback.iface_naming.patched",
                            device=device_name, interface=name,
                            fields=list(want.keys()),
                        )
                    except Exception as exc:
                        errors += 1
                        entry["error"] = str(exc)
                changes.append(entry)

    return {
        "checked": checked,
        "deleted": deleted,
        "patched": patched,
        "skipped": skipped,
        "errors": errors,
        "skip_reasons": dict(skip_reasons),
        "changes": changes,
    }


# ── Custom-field ensure (dev62) ───────────────────────────────────────────────
#
# Custom fields netcortex relies on for write-back. Operators don't have to
# pre-create these in the NetBox admin UI — we create them idempotently on the
# first reconcile run with a `nc_*` namespace so they're easy to identify and
# easy to remove if the operator ever uninstalls netcortex.
#
# Each definition is the JSON body POST-ed to `/api/extras/custom-fields/`.
# All fields are non-required, loose-filter, and live in `nc_*` so they sort
# together in the NetBox UI.
#
# NetBox 4.x switched the schema field name from ``content_types`` to
# ``object_types`` (strings like ``"dcim.interface"`` instead of integer
# content-type ids). ``_ensure_custom_fields`` tries the modern shape first
# and falls back to ``content_types`` on a 400/422 so the same code works
# against NetBox 3.x and 4.x.
_NC_INTERFACE_CUSTOM_FIELDS = [
    {
        "name": "nc_stp_portfast",
        "label": "STP PortFast (netcortex)",
        "type": "boolean",
        "description": "PortFast / Edge-port state. Synced from CISCO-STP-EXTENSIONS-MIB.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
    {
        "name": "nc_stp_bpdu_guard",
        "label": "STP BPDU Guard (netcortex)",
        "type": "boolean",
        "description": "BPDU Guard per-port state. SNMP (Cisco) or Meraki Dashboard.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
    {
        "name": "nc_stp_bpdu_filter",
        "label": "STP BPDU Filter (netcortex)",
        "type": "boolean",
        "description": "BPDU Filter per-port state. SNMP (Cisco) only.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
    {
        "name": "nc_stp_root_guard",
        "label": "STP Root Guard (netcortex)",
        "type": "boolean",
        "description": "Root Guard per-port state. SNMP (Cisco) or Meraki Dashboard.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
    {
        "name": "nc_stp_loop_guard",
        "label": "STP Loop Guard (netcortex)",
        "type": "boolean",
        "description": "Loop Guard per-port state. SNMP (Cisco) or Meraki Dashboard.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
    {
        "name": "nc_voice_vlan",
        "label": "Voice VLAN (netcortex)",
        "type": "integer",
        "description": "Voice VLAN id on access ports. From SNMP / Meraki.",
        "object_types": ["dcim.interface"],
        "required": False,
        "filter_logic": "loose",
    },
]

_NC_VLAN_CUSTOM_FIELDS = [
    {
        "name": "nc_source",
        "label": "Sourced by (netcortex)",
        "type": "text",
        "description": (
            "Adapter that contributed this VLAN. Set by netcortex so we can "
            "tell operator-authored rows apart from auto-discovered ones and "
            "never clobber operator state."
        ),
        "object_types": ["ipam.vlan"],
        "required": False,
        "filter_logic": "loose",
    },
]


async def _ensure_custom_fields(
    client: httpx.AsyncClient,
    definitions: list[dict],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Idempotently create the listed NetBox custom-field definitions.

    Returns a count dict ``{"existing": N, "created": M, "errors": E}``.
    Safe to call on every reconcile run — existing fields short-circuit on
    a single GET that matches by ``name``.

    Compatibility: tries the modern (NetBox 4.x) ``object_types`` schema
    first; on 400/422 (older NetBox rejected the unknown key) it retries
    with the legacy ``content_types`` schema, which expects a list of
    ``app_label.model`` strings (NetBox 3.x accepts both forms).
    """
    counts = {"existing": 0, "created": 0, "errors": 0}
    for defn in definitions:
        name = defn["name"]
        try:
            resp = await client.get(
                "/api/extras/custom-fields/",
                params=[("name", name), ("limit", 1)],
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                counts["existing"] += 1
                continue
        except Exception as exc:
            log.warning(
                "netbox_writeback.custom_field.lookup_failed",
                name=name, error=str(exc),
            )
            counts["errors"] += 1
            continue

        if dry_run:
            counts["created"] += 1
            log.info("netbox_writeback.custom_field.would_create", name=name)
            continue

        try:
            resp = await client.post(
                "/api/extras/custom-fields/",
                content=_json.dumps(defn),
            )
            if resp.status_code in (400, 422):
                # Legacy NetBox: retry with ``content_types`` (strings
                # work on 3.x too, just renamed in 4.x).
                legacy = dict(defn)
                legacy["content_types"] = legacy.pop("object_types")
                resp = await client.post(
                    "/api/extras/custom-fields/",
                    content=_json.dumps(legacy),
                )
            resp.raise_for_status()
            counts["created"] += 1
            log.info("netbox_writeback.custom_field.created", name=name)
        except Exception as exc:
            counts["errors"] += 1
            log.error(
                "netbox_writeback.custom_field.create_failed",
                name=name, error=str(exc),
            )
    return counts


# ── Site VLAN sync (dev62) ────────────────────────────────────────────────────

# Stub VLAN names that operators rarely set intentionally — we treat them as
# "no real name" so a more descriptive name from a different network/adapter
# can win. Match is case-insensitive. ``vlan0010`` / ``VLAN10`` / blank all
# qualify.
_STUB_VLAN_NAME_RE = re.compile(r"^\s*(vlan)?0*(\d{1,4})?\s*$", re.IGNORECASE)


def _is_stub_vlan_name(name: str | None, vid: int) -> bool:
    if not name:
        return True
    s = name.strip()
    if not s:
        return True
    if _STUB_VLAN_NAME_RE.match(s):
        return True
    return False


async def _list_all_site_scoped_vlan_groups(
    client: httpx.AsyncClient,
) -> dict[int, list[dict]]:
    """Return ``{site_id: [groups scoped to that site, oldest first]}``.

    One paginate over ``/api/ipam/vlan-groups/`` (VLAN groups are
    typically small in number — at most a few per site). The map is
    used to decide whether a site already has an operator-managed
    group we should adopt instead of creating our own.
    """
    out: dict[int, list[dict]] = {}
    for grp in await _paginate(client, "/api/ipam/vlan-groups/", []):
        if grp.get("scope_type") != "dcim.site":
            continue
        scope = grp.get("scope")
        if not isinstance(scope, dict):
            continue
        try:
            sid = int(scope["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(sid, []).append(grp)
    for sid in out:
        out[sid].sort(key=lambda g: int(g.get("id") or 0))
    return out


async def _resolve_site_vlan_group(
    client: httpx.AsyncClient,
    *,
    site_id: int,
    site_slug: str,
    site_name: str,
    groups_at_site: list[dict],
    dry_run: bool = False,
) -> tuple[int | None, str]:
    """Find — or create — the canonical VLAN group for a NetBox site.

    Resolution policy
    -----------------
    1. **Adopt any existing group scoped to this site.** Operators
       create per-site VLAN groups for their own organisational
       reasons; we use what's already there rather than competing.
       If more than one is scoped to the same site, we pick the
       lowest-id one (typically the oldest / canonical choice) and
       log a notice so the operator can clean up the duplicates.
    2. **Otherwise create one with operator-friendly naming.** Slug
       defaults to the site's slug, name defaults to the site's
       name. On slug collision (some unrelated group already has
       that slug) we fall back to ``<site_slug>-vlans``.

    Returns ``(group_id, status)`` where ``status`` is one of
    ``"existing"``, ``"created"``, ``"dry_run"`` (would create), or
    ``"error"``.
    """
    if groups_at_site:
        chosen = groups_at_site[0]
        if len(groups_at_site) > 1:
            log.info(
                "netbox_writeback.vlan_group.multiple_at_site",
                site_slug=site_slug,
                count=len(groups_at_site),
                slugs=[g.get("slug") for g in groups_at_site],
                chose=chosen.get("slug"),
            )
        return int(chosen["id"]), "existing"

    if dry_run:
        log.info(
            "netbox_writeback.vlan_group.would_create",
            slug=site_slug, site_id=site_id,
        )
        return None, "dry_run"

    body = {
        "name":        site_name or site_slug,
        "slug":        site_slug,
        "scope_type":  "dcim.site",
        "scope_id":    site_id,
        "description": f"Per-site VLAN namespace for {site_name or site_slug}.",
    }
    try:
        resp = await client.post(
            "/api/ipam/vlan-groups/",
            content=_json.dumps(body),
        )
        if resp.status_code in (400, 409):
            # Slug collision elsewhere — fall back to a less generic slug.
            body["slug"] = f"{site_slug}-vlans"
            resp = await client.post(
                "/api/ipam/vlan-groups/",
                content=_json.dumps(body),
            )
        resp.raise_for_status()
        new_id = int(resp.json()["id"])
        log.info(
            "netbox_writeback.vlan_group.created",
            site_slug=site_slug, slug=body["slug"],
            site_id=site_id, group_id=new_id,
        )
        return new_id, "created"
    except Exception as exc:
        log.error(
            "netbox_writeback.vlan_group.create_failed",
            site_slug=site_slug, site_id=site_id, error=str(exc),
        )
        return None, "error"


async def _delete_duplicate_vlan(
    client: httpx.AsyncClient,
    *,
    src_vlan_id: int,
    src_vid: int,
    target_group_id: int,
    dry_run: bool = False,
) -> tuple[bool, int]:
    """When moving a VLAN into the operator's group fails on a VID
    collision, the operator's VLAN is the canonical row and ours is a
    duplicate that shouldn't exist. DELETE our duplicate — NetBox's
    ``Interface.untagged_vlan`` uses ``on_delete=SET_NULL`` and
    ``tagged_vlans`` is a M2M, so any referencing interfaces will be
    cleaned up automatically by the framework. The next reconcile
    cycle's interface pass will then set those references to the
    operator's VLAN via the now-correct ``(site_id, vid)`` lookup.

    Pre-patching the interface FKs to the operator's VLAN was tried
    in dev65 first but failed in practice: many interfaces have no
    ``mode`` set, and NetBox refuses ``untagged_vlan`` on a mode-less
    interface (``"Interface mode does not support untagged vlan"``).
    SET_NULL has no such constraint.

    Returns ``(deleted_ok, refs_cleared)`` where ``refs_cleared`` is
    the number of interface FK references the framework will null on
    delete (counted by pre-query so we can report the work). Returns
    ``(False, 0)`` when no operator VLAN exists at the same VID in
    the target group — that means the original 400 was not a VID
    conflict and the caller should fall through to error reporting.
    """
    try:
        r = await client.get(
            "/api/ipam/vlans/",
            params=[
                ("group_id", target_group_id),
                ("vid",      src_vid),
                ("limit",    1),
            ],
        )
        r.raise_for_status()
    except Exception:
        return False, 0
    rows = r.json().get("results") or []
    if not rows:
        return False, 0
    target_id = int(rows[0]["id"])

    # NetBox 4.x doesn't expose dedicated ``untagged_vlan_id`` /
    # ``tagged_vlan_id`` filters on the interface endpoint; the
    # combined ``vlan_id`` filter matches both. Use a count-only query
    # (``limit=1``) so we don't pull the full result set just to
    # report a number — the deletion itself is what actually clears
    # the references via NetBox's cascade.
    refs = 0
    try:
        r = await client.get(
            "/api/dcim/interfaces/",
            params=[("vlan_id", src_vlan_id), ("limit", 1)],
        )
        r.raise_for_status()
        refs = int(r.json().get("count") or 0)
    except Exception:
        refs = 0

    if dry_run:
        return True, refs
    try:
        dr = await client.delete(f"/api/ipam/vlans/{src_vlan_id}/")
        dr.raise_for_status()
        log.info(
            "netbox_writeback.vlan.duplicate_deleted",
            src_vlan_id=src_vlan_id, vid=src_vid,
            kept=target_id, refs_cleared_by_cascade=refs,
        )
        return True, refs
    except Exception as exc:
        log.error(
            "netbox_writeback.vlan.duplicate_delete_failed",
            src_vlan_id=src_vlan_id, error=str(exc),
        )
        return False, refs


async def _cleanup_legacy_nc_vlan_groups(
    client: httpx.AsyncClient,
    *,
    site_by_id: dict[int, dict[str, Any]],
    groups_by_site: dict[int, list[dict]],
    dry_run: bool = False,
) -> tuple[dict[str, int], dict[int, list[dict]]]:
    """One-shot migration of pre-dev64 ``nc-<slug>`` VLAN groups.

    dev63 unconditionally created a parallel ``nc-<slug>`` VLAN group
    for every site, even when the operator already had a per-site
    group. This duplicates groups in the NetBox UI and is exactly the
    kind of "netcortex obsessed with its own namespace" behaviour we
    want to stop. This cleanup runs every reconcile cycle (cheap when
    there's nothing to do) and resolves the duplication two ways:

    * **Site already has another group** → migrate every VLAN that's
      currently in the ``nc-*`` group to the canonical operator
      group (lowest-id non-nc group at that site), then DELETE the
      ``nc-*`` group.
    * **Site has only the ``nc-*`` group** → RENAME it in place to
      operator-friendly naming (``slug = <site_slug>``, ``name =
      <site_name>``, description without the netcortex marker). On
      slug collision, fall back to ``<site_slug>-vlans``.

    Returns ``(report, groups_by_site)`` where ``groups_by_site`` has
    been refreshed in-memory so the resolver pass sees the new state
    without an extra round-trip.
    """
    report = {
        "scanned":           0,
        "renamed":           0,
        "deleted":           0,
        "vlans_moved":       0,
        "duplicates_pruned": 0,
        "refs_cleared":      0,
        "skipped":           0,
        "errors":            0,
    }

    nc_groups: list[dict] = []
    for sid, groups in groups_by_site.items():
        for g in groups:
            if (g.get("slug") or "").startswith("nc-"):
                nc_groups.append(g)
    report["scanned"] = len(nc_groups)

    if not nc_groups:
        return report, groups_by_site

    log.info(
        "netbox_writeback.vlan_group.cleanup_start",
        legacy_groups=len(nc_groups),
        slugs=[g.get("slug") for g in nc_groups[:10]],
    )

    for grp in nc_groups:
        gid = int(grp["id"])
        slug = grp.get("slug", "")
        scope = grp.get("scope") or {}
        try:
            sid = int(scope.get("id"))
        except (TypeError, ValueError):
            report["skipped"] += 1
            continue

        site_meta = site_by_id.get(sid) or {}
        site_slug = site_meta.get("slug") or scope.get("slug") or ""
        site_name = site_meta.get("name") or scope.get("name") or site_slug
        if not site_slug:
            report["skipped"] += 1
            continue

        siblings = [
            g for g in groups_by_site.get(sid, [])
            if int(g["id"]) != gid
        ]

        if siblings:
            target = siblings[0]
            target_id = int(target["id"])
            target_slug = target.get("slug") or "<unknown>"

            moved_here = 0
            pruned_here = 0
            refs_here = 0
            residual = 0
            for v in await _paginate(
                client, "/api/ipam/vlans/", [("group_id", gid)],
            ):
                vid = v.get("vid")
                if dry_run:
                    moved_here += 1
                    continue
                try:
                    pr = await client.patch(
                        f"/api/ipam/vlans/{v['id']}/",
                        content=_json.dumps({"group": target_id}),
                    )
                    if pr.status_code == 400:
                        # Most likely a VID collision with an existing
                        # operator VLAN already in the target group. Our
                        # row is the duplicate, not theirs.
                        ok, refs = await _delete_duplicate_vlan(
                            client,
                            src_vlan_id=int(v["id"]),
                            src_vid=int(vid) if vid is not None else 0,
                            target_group_id=target_id,
                            dry_run=dry_run,
                        )
                        if ok:
                            pruned_here += 1
                            refs_here += refs
                            continue
                        pr.raise_for_status()
                    else:
                        pr.raise_for_status()
                        moved_here += 1
                except Exception as exc:
                    residual += 1
                    report["errors"] += 1
                    log.warning(
                        "netbox_writeback.vlan_group.migrate_failed",
                        vlan_id=v["id"], vid=vid,
                        from_slug=slug, to_slug=target_slug,
                        error=str(exc),
                    )

            report["vlans_moved"] += moved_here
            report["duplicates_pruned"] += pruned_here
            report["refs_cleared"] += refs_here

            if residual > 0:
                # Can't delete the group while VLANs remain in it.
                log.warning(
                    "netbox_writeback.vlan_group.delete_deferred",
                    slug=slug, group_id=gid,
                    residual_vlans=residual,
                )
                continue

            if dry_run:
                report["deleted"] += 1
            else:
                try:
                    dr = await client.delete(
                        f"/api/ipam/vlan-groups/{gid}/",
                    )
                    dr.raise_for_status()
                    report["deleted"] += 1
                    log.info(
                        "netbox_writeback.vlan_group.deleted",
                        slug=slug, group_id=gid,
                        migrated_to=target_slug,
                        vlans_moved=moved_here,
                        duplicates_pruned=pruned_here,
                    )
                    groups_by_site[sid] = [
                        g for g in groups_by_site.get(sid, [])
                        if int(g["id"]) != gid
                    ]
                except Exception as exc:
                    report["errors"] += 1
                    log.error(
                        "netbox_writeback.vlan_group.delete_failed",
                        slug=slug, error=str(exc),
                    )
            continue

        if dry_run:
            report["renamed"] += 1
            continue

        new_slug = site_slug
        new_name = site_name
        patch_body = {
            "slug": new_slug,
            "name": new_name,
            "description": f"Per-site VLAN namespace for {new_name}.",
        }
        try:
            pr = await client.patch(
                f"/api/ipam/vlan-groups/{gid}/",
                content=_json.dumps(patch_body),
            )
            if pr.status_code in (400, 409):
                patch_body["slug"] = f"{new_slug}-vlans"
                pr = await client.patch(
                    f"/api/ipam/vlan-groups/{gid}/",
                    content=_json.dumps(patch_body),
                )
            pr.raise_for_status()
            report["renamed"] += 1
            log.info(
                "netbox_writeback.vlan_group.renamed",
                old_slug=slug, new_slug=patch_body["slug"],
                site_slug=site_slug,
            )
            grp["slug"] = patch_body["slug"]
            grp["name"] = patch_body["name"]
        except Exception as exc:
            report["errors"] += 1
            log.error(
                "netbox_writeback.vlan_group.rename_failed",
                slug=slug, error=str(exc),
            )

    return report, groups_by_site


async def reconcile_site_vlans(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> tuple[dict, dict[tuple[int, int], int]]:
    """Sync (site, vid) VLAN inventory from graph → NetBox ``ipam.vlan``.

    For each canonical ``vlan:nb:<slug>:<vid>`` graph node, ensure a matching
    NetBox VLAN exists at the corresponding site, and that the VLAN belongs
    to whichever ``ipam.vlangroup`` is scoped to that site so VID uniqueness
    is scoped per site the way operators expect. If the operator has not
    created a per-site VLAN group we create one ourselves with
    operator-friendly naming (``slug = <site_slug>``, ``name = <site_name>``,
    no ``nc-`` prefix) — we do NOT create a parallel netcortex-owned group
    next to an existing operator group. Returns
    ``(report, vlan_map)`` where ``vlan_map[(site_id, vid)] = nb_vlan_id``;
    downstream ``reconcile_interfaces`` uses the map to resolve
    ``untagged_vlan`` and ``tagged_vlans`` references.

    Conflict policy
    ---------------
    1. **Never clobber operator data.** A NetBox VLAN without ``nc_source``
       custom field is considered operator-authored — we never modify its
       ``name``, ``description``, or ``group``, only record its id in the
       map. New VLANs we create get ``nc_source = <adapter>`` so the next
       run can tell them apart.
    2. **Stub names lose.** "VLAN10", "vlan0010", blank → considered
       absent. Any real name from another network at the same site wins.
    3. **Multi-network deduplication.** When several Meraki networks at one
       NetBox site advertise the same VID with different names, the
       canonical VLAN node already collapsed them into one (the correlator
       picked the most-common slug); the writeback honours that and
       creates exactly one NetBox VLAN row per (site, vid).
    4. **Only sites NetBox knows about.** Graph slugs that don't match
       any NetBox site are reported under ``skipped_unknown_site`` and
       skipped — site creation is out of scope for this pass.
    5. **VLAN groups are per-site, and the operator's group wins.**
       For each site we write to we use whichever ``ipam.vlangroup``
       is already scoped to that site (lowest-id one if several
       exist). Only if no group is scoped to the site do we create
       one, using the site's own slug/name. nc-authored VLANs are
       placed in the resolved group; operator-authored VLANs are
       left in whichever group (or none) the operator chose.

    Pre-flight: ``nc_source`` custom field is auto-created on first run
    via ``_ensure_custom_fields``.
    """
    log.info("netbox_writeback.site_vlans.start", dry_run=dry_run)

    graph_vlans = await _graph_site_vlans()
    if not graph_vlans:
        return (
            {
                "checked": 0, "created": 0, "patched": 0,
                "skipped": 0, "skipped_unknown_site": 0,
                "errors": 0, "changes": [],
            },
            {},
        )

    # Deduplicate input. Canonical VLANs are already one-per-(slug, vid),
    # but defensive: if two rows arrive for the same key (shouldn't happen
    # post-correlation), prefer the one with the non-stub name.
    by_key: dict[tuple[str, int], dict] = {}
    for row in graph_vlans:
        slug = (row.get("slug") or "").strip()
        try:
            vid = int(row["vid"])
        except (TypeError, ValueError, KeyError):
            continue
        if not slug or vid < 1 or vid > 4094:
            continue
        key = (slug, vid)
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = row
            continue
        cur_stub  = _is_stub_vlan_name(cur.get("name"),  vid)
        new_stub  = _is_stub_vlan_name(row.get("name"),  vid)
        if cur_stub and not new_stub:
            by_key[key] = row

    created = patched = skipped = skipped_unknown_site = errors = 0
    skipped_unknown_site_slugs: set[str] = set()
    changes: list[dict] = []
    vlan_map: dict[tuple[int, int], int] = {}

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        # Make sure the ``nc_source`` custom field exists before we try
        # to set it on POST/PATCH (NetBox silently drops unknown CFs).
        ensure_counts = await _ensure_custom_fields(
            client, _NC_VLAN_CUSTOM_FIELDS, dry_run=dry_run,
        )
        log.info("netbox_writeback.site_vlans.cf_ensured", **ensure_counts)

        # Build slug → (site_id, site_name) map. NetBox sites endpoint
        # is small (10s of sites typical) so one paginate is fine.
        site_by_slug: dict[str, dict[str, Any]] = {}
        for site in await _paginate(client, "/api/dcim/sites/", []):
            slug = (site.get("slug") or "").strip()
            if slug:
                site_by_slug[slug] = {
                    "id":   int(site["id"]),
                    "name": (site.get("name") or slug).strip(),
                }
        slug_to_site_id = {s: v["id"] for s, v in site_by_slug.items()}
        site_by_id = {v["id"]: {**v, "slug": s} for s, v in site_by_slug.items()}

        # Pre-fetch every site-scoped VLAN group in one paginate. This
        # is the source of truth for "does the operator already have a
        # group at this site?" — we adopt whatever's there instead of
        # making a parallel ``nc-*`` group.
        groups_by_site = await _list_all_site_scoped_vlan_groups(client)

        site_ids_needed: set[int] = set()
        for (slug, _vid) in by_key:
            sid = slug_to_site_id.get(slug)
            if sid is not None:
                site_ids_needed.add(sid)

        # Pre-fetch all existing NetBox VLANs that could collide with
        # what we're about to write. A VLAN at a site can be discovered
        # two ways:
        #   * vlan.site == sid                              → ?site_id=sid
        #   * vlan.group ∈ groups whose scope is sid        → ?group_id=
        # The second case matters because operators often create VLANs
        # with ``site=None`` and rely on the group's site scope to
        # locate them; missing those leads to duplicate creation.
        def _record(row: dict, sid: int) -> None:
            try:
                vid = int(row["vid"])
            except (KeyError, TypeError, ValueError):
                return
            key = (sid, vid)
            # Prefer rows that already have a group set (more canonical).
            cur = existing_by_key.get(key)
            new_has_group = bool(row.get("group"))
            cur_has_group = bool(cur and cur.get("group"))
            if cur is None or (new_has_group and not cur_has_group):
                existing_by_key[key] = row

        existing_by_key: dict[tuple[int, int], dict] = {}
        for sid in site_ids_needed:
            for row in await _paginate(
                client, "/api/ipam/vlans/", [("site_id", sid)],
            ):
                _record(row, sid)
            for grp in groups_by_site.get(sid, []):
                for row in await _paginate(
                    client, "/api/ipam/vlans/", [("group_id", int(grp["id"]))],
                ):
                    _record(row, sid)

        # Heal pre-dev64 ``nc-<slug>`` leftovers: migrate VLANs into
        # the operator's group when one exists at the same site and
        # delete the duplicate; otherwise rename the leftover group to
        # operator-friendly naming. Idempotent — no-op on subsequent
        # runs once the cleanup has converged.
        cleanup_report, groups_by_site = await _cleanup_legacy_nc_vlan_groups(
            client,
            site_by_id=site_by_id,
            groups_by_site=groups_by_site,
            dry_run=dry_run,
        )
        log.info(
            "netbox_writeback.vlan_group.cleanup_done", **cleanup_report,
        )

        # Resolve "the" VLAN group per site: adopt existing or create
        # one with the site's own slug/name. Failures here are
        # reported but don't block VLAN writeback; the affected sites
        # just lose the ``group`` reference on their payloads (VLANs
        # still land at the site).
        site_id_to_group_id: dict[int, int] = {}
        groups_created = groups_existing = groups_errored = 0
        for slug, meta in site_by_slug.items():
            sid = meta["id"]
            if sid not in site_ids_needed:
                continue
            gid, status = await _resolve_site_vlan_group(
                client,
                site_id=sid,
                site_slug=slug,
                site_name=meta["name"],
                groups_at_site=groups_by_site.get(sid, []),
                dry_run=dry_run,
            )
            if status == "existing":
                groups_existing += 1
            elif status == "created":
                groups_created += 1
            elif status == "error":
                groups_errored += 1
            if gid is not None:
                site_id_to_group_id[sid] = gid

        for (slug, vid), row in by_key.items():
            site_id = slug_to_site_id.get(slug)
            entry: dict[str, Any] = {
                "slug": slug, "vid": vid,
                "name": row.get("name"),
                "source": row.get("source"),
                "applied": False,
            }
            if site_id is None:
                skipped_unknown_site += 1
                skipped_unknown_site_slugs.add(slug)
                entry["skipped"] = True
                entry["skip_reason"] = "unknown_site_slug"
                changes.append(entry)
                continue

            existing = existing_by_key.get((site_id, vid))
            graph_name = (row.get("name") or "").strip() or f"VLAN{vid}"
            graph_desc = (row.get("description") or "")[:200] or None
            source = row.get("source") or "correlator"

            group_id = site_id_to_group_id.get(site_id)

            if existing:
                vlan_map[(site_id, vid)] = int(existing["id"])
                existing_cfs = existing.get("custom_fields") or {}
                already_nc = bool(existing_cfs.get("nc_source"))
                if not already_nc:
                    # Operator-authored — touch nothing.
                    skipped += 1
                    entry["skipped"] = True
                    entry["skip_reason"] = "operator_authored"
                    entry["nb_vlan_id"] = int(existing["id"])
                    changes.append(entry)
                    continue

                # netcortex-authored — refresh name/desc only if we have
                # something more descriptive than what's there now. Don't
                # downgrade a real name to "VLAN10". Backfill the group
                # reference when missing or wrong (nc-authored rows from
                # pre-dev62 runs lack a group entirely).
                wanted_changes: dict[str, Any] = {}
                cur_name = (existing.get("name") or "").strip()
                cur_group_id = None
                cur_group = existing.get("group")
                if isinstance(cur_group, dict) and cur_group.get("id") is not None:
                    try:
                        cur_group_id = int(cur_group["id"])
                    except (TypeError, ValueError):
                        cur_group_id = None
                if (graph_name != cur_name
                        and not _is_stub_vlan_name(graph_name, vid)):
                    wanted_changes["name"] = graph_name
                if graph_desc and graph_desc != (existing.get("description") or ""):
                    wanted_changes["description"] = graph_desc
                if group_id is not None and cur_group_id != group_id:
                    wanted_changes["group"] = group_id
                new_cf_source = source
                if new_cf_source != existing_cfs.get("nc_source"):
                    wanted_changes.setdefault("custom_fields", {})
                    wanted_changes["custom_fields"]["nc_source"] = new_cf_source

                if not wanted_changes:
                    skipped += 1
                    entry["skipped"] = True
                    entry["skip_reason"] = "up_to_date"
                    entry["nb_vlan_id"] = int(existing["id"])
                    changes.append(entry)
                    continue

                if dry_run:
                    patched += 1
                    entry["dry_run"] = True
                    entry["proposed"] = wanted_changes
                    entry["nb_vlan_id"] = int(existing["id"])
                else:
                    try:
                        resp = await client.patch(
                            f"/api/ipam/vlans/{existing['id']}/",
                            content=_json.dumps(wanted_changes),
                        )
                        resp.raise_for_status()
                        patched += 1
                        entry["applied"] = True
                        entry["nb_vlan_id"] = int(existing["id"])
                    except Exception as exc:
                        errors += 1
                        entry["error"] = str(exc)
                changes.append(entry)
                continue

            # No existing row → create. Place the new VLAN in our
            # per-site group when one is available; falling back to
            # site-only scoping if group creation failed (operator can
            # still see the VLAN under the site).
            payload: dict[str, Any] = {
                "vid":  vid,
                "name": graph_name,
                "site": site_id,
                "status": "active",
                "custom_fields": {"nc_source": source},
            }
            if group_id is not None:
                payload["group"] = group_id
            if graph_desc:
                payload["description"] = graph_desc

            if dry_run:
                created += 1
                entry["dry_run"] = True
                entry["proposed"] = payload
            else:
                try:
                    resp = await client.post(
                        "/api/ipam/vlans/",
                        content=_json.dumps(payload),
                    )
                    resp.raise_for_status()
                    new_id = int(resp.json()["id"])
                    vlan_map[(site_id, vid)] = new_id
                    created += 1
                    entry["applied"] = True
                    entry["nb_vlan_id"] = new_id
                    log.info(
                        "netbox_writeback.site_vlan.created",
                        slug=slug, vid=vid, name=graph_name,
                        nb_vlan_id=new_id,
                    )
                except Exception as exc:
                    errors += 1
                    entry["error"] = str(exc)
                    log.error(
                        "netbox_writeback.site_vlan.failed",
                        slug=slug, vid=vid, error=str(exc),
                    )
            changes.append(entry)

    return (
        {
            "checked": len(by_key),
            "created": created,
            "patched": patched,
            "skipped": skipped,
            "skipped_unknown_site": skipped_unknown_site,
            "skipped_unknown_site_slugs": sorted(skipped_unknown_site_slugs),
            "errors": errors,
            "cf_ensured": ensure_counts,
            "groups_created":  groups_created,
            "groups_existing": groups_existing,
            "groups_errored":  groups_errored,
            "legacy_cleanup":  cleanup_report,
            "changes": changes,
        },
        vlan_map,
    )


async def reconcile_to_netbox(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Run all NetBox reconciliation passes and return a combined analysis report.

    Pass order (each later pass reuses work from earlier passes):
      0a. Site collisions — DELETE duplicate device records when two NetBox
                            sites claim the same Meraki ``network_id`` in
                            their ``meraki_networks`` custom field; STRIP
                            the colliding entry from the loser site so the
                            misconfig stops recurring.  Runs FIRST so later
                            passes never operate on duplicated rows.
      0. Devices create   — POST devices the graph sees but NetBox doesn't
                            (uses platform-observed name; matched devices
                            keep their NetBox name)
      1. Serial fill-in   — PATCH blank serials on matched devices
      1b. Site VLANs      — POST per-site ipam.vlan records from canonical
                            VLAN nodes (one per (site_slug, vid)); returns
                            a ``vlan_map`` the interface pass uses to
                            resolve ``untagged_vlan`` / ``tagged_vlans``
                            references. Never overwrites operator-authored
                            VLANs (distinguished by the absence of the
                            ``nc_source`` custom field).
      2. Interface sync   — POST missing interfaces; PATCH existing ones
                            with L2 (enabled / mode / untagged_vlan /
                            tagged_vlans) + STP custom fields, but only
                            when the corresponding NetBox field is blank
                            (operator-set values are preserved). Builds
                            name→id map for downstream passes.
      3. IP addresses     — POST missing IPs; assign to owning interface
      4. IP assignments   — PATCH existing-but-unassigned NetBox IPs to
                            bind them to the owning interface (fills the
                            common "nmap-discovered, never assigned" case)
      5. Cables           — POST cables from PHYSICAL_LINK (LLDP/CDP);
                            stamps ``label=nc-cable-{id}`` and moves the
                            discovery protocol into ``description`` so
                            NetBox renders the cable ID in connection
                            columns
      5b. Cable labels    — Backfill legacy cables whose label is still a
                            raw protocol token (``cdp``/``lldp``/...) to
                            the ``nc-cable-{id}`` convention
      6. Iface naming     — DELETE wrong-platform interfaces (Meraki labels
                            on Cisco IOS devices, etc.) when uncabled and
                            unassigned; PATCH meraki_port_id +
                            meraki_serial + nc_platform[_id] on existing
                            Meraki interfaces (for webhook-driven sync)
      7. Analysis         — report field mismatches and absent devices
                            (read-only, appended for operator review)

    When ``dry_run=True`` all changes are computed but no NetBox API writes
    are made.  The returned report is identical in structure regardless of
    dry_run so callers can preview before committing.

    Note: devices created by pass 0 are NOT immediately visible to passes
    1-4 in the same run because the graph's ``netbox_id`` attribute is
    stamped by the next enrichment cycle.  Their interfaces/IPs/cables
    will land on the following reconcile run.  This keeps each pass
    referentially consistent within a run.
    """
    log.info("netbox_writeback.start", dry_run=dry_run)

    # ── Pass 0a: Meraki-network site collisions ──────────────────────────────
    # Runs BEFORE every other pass so that interfaces/IPs/cables are not
    # spuriously created on the loser side of a collision during the same
    # run.  Deletes loser-side duplicate device records (cascade), strips
    # the colliding network from the loser's meraki_networks custom field.
    collision_report = await reconcile_duplicate_meraki_sites(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.collisions_done",
        **{k: v for k, v in collision_report.items() if k != "changes"},
    )

    # ── Pass 0: device creates ────────────────────────────────────────────────
    device_report = await reconcile_devices_create(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.devices_done",
        **{k: v for k, v in device_report.items() if k != "changes"},
    )

    # ── Pass 1: serials ───────────────────────────────────────────────────────
    serial_report = await reconcile_device_serials(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.serials_done",
        **{k: v for k, v in serial_report.items() if k != "changes"},
    )

    # ── Pass 1b: site VLANs (dev62) ──────────────────────────────────────────
    # Must run BEFORE interfaces so the vlan_map exists when the
    # interface pass tries to resolve ``untagged_vlan`` / ``tagged_vlans``
    # references. VLAN records that already exist as operator-authored
    # rows are left untouched (we recognise them by the absence of the
    # ``nc_source`` custom field).
    site_vlan_report, vlan_map = await reconcile_site_vlans(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.site_vlans_done",
        **{k: v for k, v in site_vlan_report.items() if k != "changes"},
    )

    # ── Pass 2: interfaces ────────────────────────────────────────────────────
    iface_result = await reconcile_interfaces(
        netbox_url, netbox_token, verify_ssl, dry_run,
        vlan_map=vlan_map,
    )
    # reconcile_interfaces returns (report, flat_map, cabled_set)
    if len(iface_result) == 3:
        iface_report, nb_iface_map, cabled_iface_ids = iface_result
    else:
        iface_report, nb_iface_map = iface_result  # type: ignore[misc]
        cabled_iface_ids = set()
    log.info(
        "netbox_writeback.interfaces_done",
        **{k: v for k, v in iface_report.items() if k != "changes"},
    )

    # ── Pass 3: IPs (create missing) ──────────────────────────────────────────
    ip_report = await reconcile_ip_addresses(
        netbox_url, netbox_token, verify_ssl, dry_run,
        nb_iface_map=nb_iface_map,
    )
    log.info(
        "netbox_writeback.ips_done",
        **{k: v for k, v in ip_report.items() if k != "changes"},
    )

    # ── Pass 4: IP assignments (fill blank assignments) ──────────────────────
    ip_assign_report = await reconcile_ip_assignments(
        netbox_url, netbox_token, verify_ssl,
        nb_iface_map=nb_iface_map,
        dry_run=dry_run,
    )
    log.info(
        "netbox_writeback.ip_assignments_done",
        **{k: v for k, v in ip_assign_report.items() if k != "changes"},
    )

    # ── Pass 5: cables ────────────────────────────────────────────────────────
    cable_report = await reconcile_cables(
        netbox_url, netbox_token, verify_ssl, dry_run,
        nb_iface_map=nb_iface_map,
        cabled_iface_ids=cabled_iface_ids,
    )
    log.info(
        "netbox_writeback.cables_done",
        **{k: v for k, v in cable_report.items() if k != "changes"},
    )

    # ── Pass 5b: cable label backfill ────────────────────────────────────────
    # Migrates legacy protocol-named labels (``cdp``, ``lldp``,
    # ``catc_topology``, ``mac_arp``) to the ``nc-cable-{id}`` convention
    # so NetBox renders the cable ID in connection columns.  Idempotent
    # and only touches cables that bear the legacy tokens.
    cable_label_report = await reconcile_cable_labels(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.cable_labels_done",
        **{k: v for k, v in cable_label_report.items() if k != "changes"},
    )

    # ── Pass 6: iface naming hygiene ─────────────────────────────────────────
    # Deletes Meraki-style names on Cisco devices (safe-belt: only when
    # uncabled, unassigned, AND no platform-native graph interface has
    # the same canonical-key); backfills meraki_port_id custom field on
    # existing Meraki interfaces so a NetBox webhook can push changes
    # back to the Meraki Dashboard API.
    iface_naming_report = await reconcile_interface_naming(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.iface_naming_done",
        **{k: v for k, v in iface_naming_report.items() if k != "changes"},
    )

    # ── Pass 7: analysis (read-only) ─────────────────────────────────────────
    field_mismatches = await analyse_field_mismatches()
    absent_devices   = await analyse_absent_devices()

    total_changes = (
        collision_report["duplicate_devices_deleted"]
        + collision_report["loser_sites_cleared"]
        + device_report["created"]
        + serial_report["patched"]
        + site_vlan_report["created"]
        + site_vlan_report["patched"]
        + iface_report["created"]
        + iface_report.get("patched_l2", 0)
        + ip_report["created"]
        + ip_assign_report["assigned"]
        + cable_report["created"]
        + cable_label_report["patched"]
        + iface_naming_report["deleted"]
        + iface_naming_report["patched"]
    )
    total_errors = (
        collision_report["errors"]
        + device_report["errors"]
        + serial_report["errors"]
        + site_vlan_report["errors"]
        + iface_report["errors"]
        + ip_report["errors"]
        + ip_assign_report["errors"]
        + cable_report["errors"]
        + cable_label_report["errors"]
        + iface_naming_report["errors"]
    )

    log.info(
        "netbox_writeback.done",
        dry_run=dry_run,
        total_changes=total_changes,
        total_errors=total_errors,
        field_mismatches=len(field_mismatches),
        absent_devices=len(absent_devices),
    )

    quality_filtered_total = (
        iface_report.get("quality_filtered", 0)
        + cable_report.get("self_loops_filtered", 0)
    )

    return {
        "dry_run": dry_run,
        "summary": {
            "collisions_resolved":     collision_report["collisions_resolved"],
            "duplicate_devices_deleted": collision_report["duplicate_devices_deleted"],
            "loser_sites_cleared":     collision_report["loser_sites_cleared"],
            "devices_created":      device_report["created"],
            "serials_patched":      serial_report["patched"],
            "site_vlans_created":   site_vlan_report["created"],
            "site_vlans_patched":   site_vlan_report["patched"],
            "interfaces_created":   iface_report["created"],
            "interfaces_l2_patched": iface_report.get("patched_l2", 0),
            "ips_created":          ip_report["created"],
            "ips_assigned":         ip_assign_report["assigned"],
            "cables_created":       cable_report["created"],
            "cable_labels_patched": cable_label_report["patched"],
            "ifaces_deleted":       iface_naming_report["deleted"],
            "ifaces_cf_patched":    iface_naming_report["patched"],
            "total_changes":        total_changes,
            "total_errors":         total_errors,
            "quality_filtered":     quality_filtered_total,
            "field_mismatches":     len(field_mismatches),
            "absent_devices":       len(absent_devices),
        },
        "site_collisions":  collision_report,
        "devices":          device_report,
        "serials":          serial_report,
        "site_vlans":       site_vlan_report,
        "interfaces":       iface_report,
        "ips":              ip_report,
        "ip_assignments":   ip_assign_report,
        "cables":           cable_report,
        "cable_labels":     cable_label_report,
        "iface_naming":     iface_naming_report,
        "analysis": {
            # Renamed from "site_mismatches" — these are field-level
            # divergences (e.g., serial mismatch), not site assignment
            # disagreements.  Name-only divergences are intentionally
            # filtered out (operator policy: platform name and NetBox
            # name may differ).  Kept old key for one release as alias.
            "field_mismatches": field_mismatches,
            "site_mismatches":  field_mismatches,  # deprecated alias
            "absent_devices":   absent_devices,
        },
    }
