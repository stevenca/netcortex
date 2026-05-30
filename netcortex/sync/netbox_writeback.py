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


def _nb_iface_type(speed_mbps: int | float | None) -> str:
    """Best-effort NetBox interface type slug from observed speed_mbps."""
    if not speed_mbps:
        return "other"
    s = int(speed_mbps)
    if s >= 400_000: return "400gbase-x-qsfpdd"
    if s >= 100_000: return "100gbase-x-qsfp28"
    if s >=  40_000: return "40gbase-x-qsfpp"
    if s >=  25_000: return "25gbase-x-sfp28"
    if s >=  10_000: return "10gbase-x-sfpp"
    if s >=   1_000: return "1000base-t"
    if s >=     100: return "100base-tx"
    return "other"


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
    """Interfaces attached to matched devices."""
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND (d.stub IS NULL OR d.stub = false)
            RETURN d.netbox_id    AS netbox_id,
                   d.display_name AS device_name,
                   i.name         AS name,
                   i.mac          AS mac,
                   i.speed_mbps   AS speed_mbps,
                   i.mtu          AS mtu,
                   i.description  AS description,
                   i.enabled      AS enabled
            """
        )
        return await result.data()


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
    """Return {nb_device_id: {lowercase_iface_name: {id, cable, ...}}}."""
    result: dict[str, dict[str, dict]] = {}
    for i in range(0, len(nb_ids), _BATCH):
        chunk = nb_ids[i : i + _BATCH]
        params = [("device_id", nid) for nid in chunk]
        for rec in await _paginate(client, "/api/dcim/interfaces/", params):
            dev_id = str(rec["device"]["id"])
            result.setdefault(dev_id, {})[rec["name"].lower()] = {
                "id":    rec["id"],
                "name":  rec["name"],
                "cable": rec.get("cable"),
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


async def reconcile_interfaces(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> tuple[dict, dict]:
    """Create interfaces in NetBox that exist in the graph but not in NetBox.

    Returns ``(report, nb_iface_map)`` where ``nb_iface_map`` is
    ``{nb_device_id: {lower_iface_name: nb_interface_id}}`` — reused by
    the IP and cable reconcilers to avoid re-fetching.

    Rules
    -----
    * Only creates; never deletes or renames existing NetBox interfaces.
    * Skips interfaces with no name.
    * Maps speed_mbps to a best-effort NetBox type slug; uses "other" when
      speed is unknown or doesn't match a recognised tier.
    """
    graph_ifaces = await _graph_interfaces()
    if not graph_ifaces:
        return (
            {"checked": 0, "created": 0, "skipped": 0, "errors": 0, "changes": []},
            {},
        )

    by_nb_id: dict[str, list[dict]] = defaultdict(list)
    for iface in graph_ifaces:
        by_nb_id[str(iface["netbox_id"])].append(iface)

    nb_ids = list(by_nb_id.keys())
    created = skipped = errors = 0
    changes: list[dict] = []

    async with _make_client(netbox_url, netbox_token, verify_ssl) as client:
        nb_iface_map = await _fetch_nb_interface_map(client, nb_ids)

        for nb_id, ifaces in by_nb_id.items():
            existing = nb_iface_map.get(nb_id, {})
            for iface in ifaces:
                iname = (iface.get("name") or "").strip()
                if not iname:
                    skipped += 1
                    continue
                if iname.lower() in existing:
                    skipped += 1
                    continue

                payload: dict[str, Any] = {
                    "device": int(nb_id),
                    "name":   iname,
                    "type":   _nb_iface_type(iface.get("speed_mbps")),
                }
                if iface.get("mac"):
                    payload["mac_address"] = iface["mac"]
                if iface.get("mtu"):
                    payload["mtu"] = int(iface["mtu"])
                if iface.get("description"):
                    payload["description"] = str(iface["description"])[:200]
                if iface.get("enabled") is not None:
                    payload["enabled"] = bool(iface["enabled"])

                device_name = iface.get("device_name") or nb_id
                entry: dict[str, Any] = {
                    "device": device_name, "interface": iname,
                    "netbox_device_id": nb_id, "applied": False,
                }

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
                        nb_iface_map.setdefault(nb_id, {})[iname.lower()] = {
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

    # Flatten map for callers: {nb_dev_id: {lower_name: int_id}}
    flat_map: dict[str, dict[str, int]] = {
        dev_id: {name: info["id"] for name, info in ifaces.items()}
        for dev_id, ifaces in nb_iface_map.items()
    }
    # Track cabled interfaces separately for the cable reconciler
    cabled_ids: set[int] = {
        info["id"]
        for ifaces in nb_iface_map.values()
        for info in ifaces.values()
        if info.get("cable")
    }

    report = {
        "checked": len(graph_ifaces), "created": created,
        "skipped": skipped, "errors": errors,
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

                nb_iface_id = iface_lookup.get((row.get("iface_name") or "").lower())
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
                iid = lookup.get(candidate.lower())
                if iid:
                    return iid
            return None

        seen_pairs: set[frozenset] = set()  # dedup bidirectional links

        for lnk in links:
            nb_id_a = str(lnk.get("nb_id_a") or "")
            nb_id_b = str(lnk.get("nb_id_b") or "")
            if not nb_id_a or not nb_id_b:
                skipped += 1
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

            payload = {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": iid_a}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": iid_b}],
                "status": "connected",
                "label":  lnk.get("proto", "discovered"),
            }
            entry: dict[str, Any] = {
                "device_a": lnk.get("name_a") or nb_id_a,
                "port_a":   lnk.get("port_a_raw") or lnk.get("port_a_norm", ""),
                "device_b": lnk.get("name_b") or nb_id_b,
                "port_b":   lnk.get("port_b_raw") or lnk.get("port_b_norm", ""),
                "proto":    lnk.get("proto"),
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
                    cabled_iface_ids.add(iid_a)
                    cabled_iface_ids.add(iid_b)
                    entry["applied"] = True
                    created += 1
                    log.info(
                        "netbox_writeback.cable.created",
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
        "changes": changes,
    }


# ── Data-quality analysis (read-only, no writes) ───────────────────────────────

async def analyse_site_mismatches() -> list[dict]:
    """Return devices where observed netbox_site_slug differs from NetBox's site.

    This is informational only — site membership is operator-controlled in
    NetBox.  The list is included in the reconcile_to_netbox report so
    operators know which devices to investigate.
    """
    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (d:Device)
            WHERE d.netbox_id IS NOT NULL
              AND d.canonical_id IS NULL
              AND d.netbox_delta IS NOT NULL
              AND d.netbox_delta <> ''
            RETURN coalesce(d.display_name, d.name) AS device,
                   d.netbox_id      AS netbox_id,
                   d.netbox_delta   AS delta_json,
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
        mismatches.append({
            "device":        row["device"],
            "netbox_id":     row["netbox_id"],
            "delta":         delta,
            "observed_site": row.get("observed_site"),
        })
    return mismatches


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

async def reconcile_to_netbox(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
    dry_run: bool = False,
) -> dict:
    """Run all NetBox reconciliation passes and return a combined analysis report.

    Pass order (each later pass reuses work from earlier passes):
      1. Serial fill-in  — PATCH blank serials on matched devices
      2. Interface sync  — POST missing interfaces; build name→id map
      3. IP addresses    — POST missing IPs; assign to owning interface
      4. Cables          — POST cables from PHYSICAL_LINK (LLDP/CDP)
      5. Analysis        — report site mismatches and absent devices
                           (read-only, appended for operator review)

    When ``dry_run=True`` all changes are computed but no NetBox API writes
    are made.  The returned report is identical in structure regardless of
    dry_run so callers can preview before committing.
    """
    log.info("netbox_writeback.start", dry_run=dry_run)

    # ── Pass 1: serials ───────────────────────────────────────────────────────
    serial_report = await reconcile_device_serials(
        netbox_url, netbox_token, verify_ssl, dry_run,
    )
    log.info(
        "netbox_writeback.serials_done",
        **{k: v for k, v in serial_report.items() if k != "changes"},
    )

    # ── Pass 2: interfaces ────────────────────────────────────────────────────
    iface_result = await reconcile_interfaces(
        netbox_url, netbox_token, verify_ssl, dry_run,
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

    # ── Pass 3: IPs ───────────────────────────────────────────────────────────
    ip_report = await reconcile_ip_addresses(
        netbox_url, netbox_token, verify_ssl, dry_run,
        nb_iface_map=nb_iface_map,
    )
    log.info(
        "netbox_writeback.ips_done",
        **{k: v for k, v in ip_report.items() if k != "changes"},
    )

    # ── Pass 4: cables ────────────────────────────────────────────────────────
    cable_report = await reconcile_cables(
        netbox_url, netbox_token, verify_ssl, dry_run,
        nb_iface_map=nb_iface_map,
        cabled_iface_ids=cabled_iface_ids,
    )
    log.info(
        "netbox_writeback.cables_done",
        **{k: v for k, v in cable_report.items() if k != "changes"},
    )

    # ── Pass 5: analysis (read-only) ─────────────────────────────────────────
    site_mismatches = await analyse_site_mismatches()
    absent_devices  = await analyse_absent_devices()

    total_changes = (
        serial_report["patched"]
        + iface_report["created"]
        + ip_report["created"]
        + cable_report["created"]
    )
    total_errors = (
        serial_report["errors"]
        + iface_report["errors"]
        + ip_report["errors"]
        + cable_report["errors"]
    )

    log.info(
        "netbox_writeback.done",
        dry_run=dry_run,
        total_changes=total_changes,
        total_errors=total_errors,
        site_mismatches=len(site_mismatches),
        absent_devices=len(absent_devices),
    )

    return {
        "dry_run": dry_run,
        "summary": {
            "serials_patched":      serial_report["patched"],
            "interfaces_created":   iface_report["created"],
            "ips_created":          ip_report["created"],
            "cables_created":       cable_report["created"],
            "total_changes":        total_changes,
            "total_errors":         total_errors,
            "site_mismatches":      len(site_mismatches),
            "absent_devices":       len(absent_devices),
        },
        "serials":          serial_report,
        "interfaces":       iface_report,
        "ips":              ip_report,
        "cables":           cable_report,
        "analysis": {
            "site_mismatches":  site_mismatches,
            "absent_devices":   absent_devices,
        },
    }
