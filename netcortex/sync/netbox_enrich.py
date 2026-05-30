"""Enrich graph Device nodes with NetBox site information.

For each Device node that carries a serial number, we look it up in NetBox and:
  1. Set netbox_id, netbox_site_slug, netbox_site_name on the node.
  2. Record the current PlatformSite name in platform_container so the
     UI can still show which platform-derived container the device belongs to.
  3. If multiple graph nodes share the same NetBox device (same serial),
     mark non-canonical ones with canonical_id pointing to the preferred node.

The graph query layer consumes these properties to:
  - Override the visual container with a NetBox-site compound node.
  - Hide duplicate (non-canonical) nodes from the topology view.
"""

from __future__ import annotations

import json
import httpx
import structlog

from netcortex.graph.client import get_driver

log = structlog.get_logger(__name__)

# Adapter name prefixes ordered from lowest to highest preference.
# When two graph nodes have the same serial, the one from the highest-ranked
# adapter type is kept as canonical; the others get canonical_id set.
_ADAPTER_PRIORITY: list[str] = [
    "snmp",
    "meraki",
    "nexus_dashboard",
    "intersight",
    "catalyst_center",
]


def _adapter_rank(source_adapter: str | None) -> int:
    if not source_adapter:
        return 0
    for rank, prefix in enumerate(_ADAPTER_PRIORITY, start=1):
        if source_adapter.startswith(prefix):
            return rank
    return 0


def _normalize_name(name: str | None) -> str:
    """Normalize a hostname so an FQDN matches its short form.

    NetBox typically stores devices as short names (``cpn-ful-n9k1``) while
    some platforms report FQDNs (``cpn-ful-n9k1.ciscops.net``).  We lower
    case and strip everything after the first dot so both forms collide on
    the same key.  An empty string means "don't try to match by name".
    """
    n = (name or "").strip().lower()
    if not n:
        return ""
    if "." in n:
        n = n.split(".", 1)[0]
    return n


def _compute_netbox_delta(
    netbox_name: str,
    netbox_serial: str,
    current_name: str,
    current_serial: str,
) -> dict:
    """Diff an observed (canonical) Device against a NetBox device record.

    Returns an empty dict when there is nothing to record, or a structured
    delta dict when at least one field disagrees::

        {
            "type": "field_mismatch",
            "fields": {
                "name":   {"intent": <netbox_verbatim>, "current": <observed_verbatim>},
                "serial": {"intent": <UPPER_SERIAL>,    "current": <UPPER_SERIAL>},
            }
        }

    ``name`` values are stored verbatim (pre-trim) so the UI can show both
    forms without re-normalising.  ``serial`` values are stored uppercased
    because case is not meaningful for Cisco serials and upstream sources
    disagree on capitalisation.

    Per the NetCortex design philosophy, NetCortex is authoritative for
    current state; NetBox is intent.  We never mutate either side here —
    we surface the gap so a future reconciliation UI can flag it to the
    operator.  Inputs are pre-trimmed; the caller is responsible for
    case/format normalisation where appropriate.
    """
    fields: dict[str, dict[str, str]] = {}

    nb_name_norm   = _normalize_name(netbox_name)
    cur_name_norm  = _normalize_name(current_name)
    if netbox_name and current_name and nb_name_norm != cur_name_norm:
        fields["name"] = {"intent": netbox_name, "current": current_name}

    nb_serial_norm  = (netbox_serial or "").strip().upper()
    cur_serial_norm = (current_serial or "").strip().upper()
    if nb_serial_norm and cur_serial_norm and nb_serial_norm != cur_serial_norm:
        fields["serial"] = {"intent": nb_serial_norm, "current": cur_serial_norm}

    if not fields:
        return {}
    return {"type": "field_mismatch", "fields": fields}


async def enrich_devices_from_netbox(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
) -> dict[str, int]:
    """Query NetBox for all devices and enrich matching graph nodes.

    Returns:
        {"enriched": <n>, "duplicates_marked": <n>} counts.
    """
    driver = get_driver()

    headers = {
        "Authorization": f"Token {netbox_token}",
        "Accept": "application/json",
    }

    # ── 1. Paginate through NetBox devices, index by serial AND by name ───────
    #
    # Some NetBox records have an empty ``serial`` field (the NX-OS chassis at
    # cpn-ful, for example).  Without a name-based fallback those devices
    # never get a NetBox site assigned even though their hostname matches
    # exactly.  We therefore build two indices in one pass and try them in
    # order: serial first (highest confidence), then normalized name.
    nb_by_serial: dict[str, dict] = {}   # UPPER_SERIAL → {netbox_id, site_slug, site_name}
    nb_by_name: dict[str, dict] = {}     # normalized name → same payload

    async with httpx.AsyncClient(
        base_url=netbox_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
        verify=verify_ssl,
    ) as client:
        offset = 0
        limit = 500
        while True:
            try:
                resp = await client.get(
                    "/api/dcim/devices/",
                    params={"limit": limit, "offset": offset},
                )
                resp.raise_for_status()
            except Exception as exc:
                log.error("netbox_enrich.fetch_failed", error=str(exc))
                break

            payload = resp.json()
            for dev in payload.get("results", []):
                serial = (dev.get("serial") or "").strip().upper()
                name = _normalize_name(dev.get("name"))
                site = dev.get("site") or {}
                info = {
                    "netbox_id":     str(dev["id"]),
                    "site_slug":     site.get("slug", ""),
                    "site_name":     site.get("name", ""),
                    "netbox_name":   (dev.get("name") or "").strip(),
                    "netbox_serial": (dev.get("serial") or "").strip(),
                }
                if serial:
                    nb_by_serial[serial] = info
                if name:
                    # If two NetBox devices share a normalized name (rare),
                    # the last one wins — but they almost certainly belong
                    # to the same site anyway, so it doesn't change the
                    # outcome materially.
                    nb_by_name[name] = info

            if not payload.get("next"):
                break
            offset += limit

    if not nb_by_serial and not nb_by_name:
        log.warning("netbox_enrich.no_devices_in_netbox")
        return {
            "enriched": 0,
            "duplicates_marked": 0,
            "deltas_recorded": 0,
            "absent_in_netbox": 0,
            "stubs_absorbed": 0,
        }

    log.info("netbox_enrich.fetched_from_netbox",
             by_serial=len(nb_by_serial),
             by_name=len(nb_by_name))

    # ── 2. Query graph for Device nodes (serial OR name) ──────────────────────
    #
    # Stubs (`lldp-neighbor:` / `cdp-neighbor:`) ARE included here on
    # purpose: this matcher is the ONLY place in the platform that has
    # the (stub-hostname → NetBox device → NetBox serial → real Device)
    # chain needed to canonicalise LLDP/CDP neighbors whose canonical
    # form is named by serial (e.g. an Intersight Fabric Interconnect
    # canonical id `intersight-fi:CPN:...` with name `FI-A-FCH2903782Y`,
    # whose NetBox device record is named `cpn-ful-aipod-fi-A` — the
    # exact same hostname the CDP stub carries).  Without including
    # stubs, those FIs never get merged.
    #
    # However: simply stamping `canonical_id` on a stub is NOT enough.
    # The topology query hides any device with `canonical_id` set AND
    # drops every PHYSICAL_LINK edge incident to it — so if we leave
    # the stub un-absorbed even briefly, its cables vanish from the
    # rendered graph (that's how n9k1 lost ~14 of its 17 LLDP/CDP
    # neighbors before).  The atomic absorption is done at the bottom
    # of this function via `_absorb_stubs_with_canonical_id`, so the
    # stamp-then-absorb is a single transactional unit from the
    # operator's point of view.
    async with driver.session() as session:
        result = await session.run(
            "MATCH (d:Device) "
            "WHERE (d.serial IS NOT NULL AND d.serial <> '') "
            "   OR (d.name   IS NOT NULL AND d.name   <> '') "
            "OPTIONAL MATCH (d)-[:LOCATED_AT]->(ps:PlatformSite) "
            "RETURN d.id          AS node_id, "
            "       d.serial      AS serial, "
            "       d.name        AS name, "
            "       d.source_adapter AS source_adapter, "
            "       coalesce(ps.name, '') AS platform_container"
        )
        records = await result.data()

    # ── 3. Match each graph node to a NetBox device id ────────────────────────
    #
    # We group by NetBox device id (not by serial) so that duplicates spanning
    # the serial AND the name path collapse into a single canonical group.
    nb_id_to_nodes: dict[str, tuple[dict, list[dict]]] = {}

    for rec in records:
        serial = (rec.get("serial") or "").strip().upper()
        name_norm = _normalize_name(rec.get("name"))
        nb_info: dict | None = None
        if serial and serial in nb_by_serial:
            nb_info = nb_by_serial[serial]
        elif name_norm and name_norm in nb_by_name:
            nb_info = nb_by_name[name_norm]
        if not nb_info:
            continue
        nb_id = nb_info["netbox_id"]
        bucket = nb_id_to_nodes.setdefault(nb_id, (nb_info, []))
        bucket[1].append(rec)

    # ── 4. Build update list, picking one canonical graph node per NetBox id ──
    updates: list[dict] = []
    enriched = 0
    duplicates_marked = 0
    deltas_recorded = 0

    for _nb_id, (nb_info, graph_nodes) in nb_id_to_nodes.items():
        # Sort canonical-priority high-to-low. LLDP/CDP discovery stubs
        # are forced to the bottom so they can never be chosen as the
        # canonical for a (NetBox-id) group; otherwise a stub picked as
        # canonical would orphan the real Device by stamping IT with
        # canonical_id pointing at the stub.
        def _sort_key(n: dict) -> int:
            nid = n.get("node_id") or ""
            if nid.startswith("lldp-neighbor:") or nid.startswith("cdp-neighbor:"):
                return -1
            return _adapter_rank(n.get("source_adapter"))

        ranked = sorted(graph_nodes, key=_sort_key, reverse=True)
        canonical_id = ranked[0]["node_id"]
        canonical_rec = ranked[0]

        # ── NetBox-delta marker (per design philosophy) ────────────────
        # NetCortex is authoritative for the CURRENT state of the
        # network; NetBox is INTENT.  When the two disagree on
        # observable attributes, we don't override either side —
        # we surface the delta on the canonical node so a future
        # reconciliation UI can flag the mismatch to the operator.
        #
        # ``netbox_delta`` is intentionally rebuilt every run so it
        # reflects current observation, not a stale snapshot.  It is
        # serialised as a JSON string because Neo4j doesn't store
        # nested maps as native node properties.
        #
        # Sites are deliberately NOT included here — the whole point
        # of this pass is to STAMP the NetBox site onto the node, so
        # a "delta" on site is tautologically zero immediately after
        # the write.
        delta_fields = _compute_netbox_delta(
            netbox_name=(nb_info.get("netbox_name") or "").strip(),
            netbox_serial=(nb_info.get("netbox_serial") or "").strip(),
            current_name=(canonical_rec.get("name") or "").strip(),
            current_serial=(canonical_rec.get("serial") or "").strip(),
        )

        if delta_fields:
            delta_payload = json.dumps(delta_fields, sort_keys=True)
            deltas_recorded += 1
        else:
            delta_payload = ""   # empty string = no delta (avoids null)

        nb_name = nb_info.get("netbox_name", "")

        for node in ranked:
            nid = node["node_id"]
            # Only the canonical node carries the delta — the
            # duplicates are by definition hidden, so stamping them
            # would just clutter the graph with redundant data.
            node_delta = delta_payload if nid == canonical_id else ""
            updates.append({
                "node_id":           nid,
                "netbox_id":         nb_info["netbox_id"],
                "netbox_site_slug":  nb_info["site_slug"],
                "netbox_site_name":  nb_info["site_name"],
                "netbox_name":       nb_name,
                # When a NetBox name is available, use it as the display
                # label everywhere — NetBox is the operator's canonical
                # naming authority for matched devices.  Unmatched devices
                # (absent_in_netbox) keep their adapter-observed name.
                "display_name":      nb_name,
                "platform_container": node.get("platform_container") or "",
                "canonical_id":      None if nid == canonical_id else canonical_id,
                "netbox_delta":      node_delta,
            })
            enriched += 1
            if updates[-1]["canonical_id"]:
                duplicates_marked += 1

    # ── 4. Write back to Neo4j in one UNWIND pass ─────────────────────────────
    if updates:
        async with driver.session() as session:
            await session.run(
                "UNWIND $updates AS upd "
                "MATCH (d:Device {id: upd.node_id}) "
                "SET d.netbox_id          = upd.netbox_id, "
                "    d.netbox_site_slug   = upd.netbox_site_slug, "
                "    d.netbox_site_name   = upd.netbox_site_name, "
                "    d.netbox_name        = upd.netbox_name, "
                "    d.display_name       = CASE "
                "        WHEN upd.display_name <> '' THEN upd.display_name "
                "        ELSE d.name END, "
                "    d.platform_container = upd.platform_container, "
                "    d.canonical_id       = upd.canonical_id, "
                "    d.netbox_delta       = upd.netbox_delta",
                updates=updates,
            )

    # ── 4b. Mark devices NetBox doesn't know about ───────────────────────────
    #
    # Per the NetCortex design philosophy, current state is
    # authoritative — a real Device that NetBox is missing should
    # still appear in the graph.  We don't change anything except
    # stamping a small ``netbox_delta`` marker so a future
    # reconciliation UI can flag "intent missing" devices.
    #
    # Stubs (lldp-/cdp-neighbor) are excluded because they're not
    # "real" inventory items — they're either going to be absorbed
    # onto a canonical Device by the very next pass or live as
    # unresolved neighbors until correlation can place them.  Either
    # way, having NetBox warn about them is noise, not signal.
    matched_node_ids: set[str] = set()
    for _nb_info, gnodes in nb_id_to_nodes.values():
        for gn in gnodes:
            matched_node_ids.add(gn.get("node_id") or "")

    absent_updates: list[dict] = []
    absent_in_netbox = 0
    for rec in records:
        nid = rec.get("node_id") or ""
        if not nid or nid in matched_node_ids:
            continue
        if nid.startswith("lldp-neighbor:") or nid.startswith("cdp-neighbor:"):
            continue
        absent_updates.append({
            "node_id":      nid,
            "netbox_delta": '{"type": "absent_in_netbox"}',
        })
        absent_in_netbox += 1

    if absent_updates:
        async with driver.session() as session:
            await session.run(
                "UNWIND $updates AS upd "
                "MATCH (d:Device {id: upd.node_id}) "
                "SET d.netbox_delta = upd.netbox_delta",
                updates=absent_updates,
            )

    # ── 5. Immediately absorb any stub we just stamped with canonical_id ─────
    #
    # The topology query hides any Device with `canonical_id` set AND
    # drops every PHYSICAL_LINK edge incident to it.  If we leave a
    # stamped stub un-absorbed until the next correlator cycle, its
    # cables would vanish from the rendered graph in the meantime —
    # users would see canonical devices like cpn-ful-n9k1 lose ~14 of
    # their 17 LLDP/CDP neighbors until the correlator fires again.
    # Calling the absorb pass here makes the (stamp → absorb) sequence
    # atomic from the operator's perspective.
    #
    # Imported lazily to avoid a circular import (correlate.py imports
    # from sync indirectly through other modules).
    from netcortex.graph.correlate import _absorb_stubs_with_canonical_id
    stubs_absorbed = await _absorb_stubs_with_canonical_id()

    log.info(
        "netbox_enrich.done",
        enriched=enriched,
        duplicates_marked=duplicates_marked,
        deltas_recorded=deltas_recorded,
        absent_in_netbox=absent_in_netbox,
        stubs_absorbed=stubs_absorbed,
    )
    return {
        "enriched": enriched,
        "duplicates_marked": duplicates_marked,
        "deltas_recorded": deltas_recorded,
        "absent_in_netbox": absent_in_netbox,
        "stubs_absorbed": stubs_absorbed,
    }


async def enrich_prefixes_from_netbox_ipam(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
) -> dict[str, int]:
    """Stamp existing Prefix nodes with NetBox IPAM info (VLAN vid + site slug).

    NetBox IPAM is the source of truth for "which VLAN does this prefix
    belong to" — discovery sources (SNMP route tables, Meraki configs)
    don't always reveal that linkage explicitly.  For example a
    catalyst's Vlan11 SVI might have both an IPv4 and an IPv6 address,
    but the only IPv4 route on the device reaches the same subnet via
    its OOB management port (``Gi0/0``) — so the SNMP route table
    "loses" the VLAN tag for the v4 prefix.  NetBox, however, knows
    explicitly that ``192.133.162.0/24`` belongs to VLAN 11
    (``cpn-ful-mgmt``) at site ``cpn-ful``.

    We only UPDATE existing Prefix nodes here; we do not create any.
    That keeps the graph free of NetBox-only prefixes that don't
    correspond to anything we've actually discovered on the wire.

    The stamped properties are used by
    :func:`netcortex.graph.correlate._link_vlan_svis_and_prefixes` to
    add a ``HAS_PREFIX`` edge from the matching ``vlan:nb:<slug>:<vid>``
    canonical VLAN — see Path 5 there for the join.
    """
    driver = get_driver()

    headers = {
        "Authorization": f"Token {netbox_token}",
        "Accept": "application/json",
    }

    updates: list[dict] = []

    async with httpx.AsyncClient(
        base_url=netbox_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
        verify=verify_ssl,
    ) as client:
        offset = 0
        limit = 500
        while True:
            try:
                resp = await client.get(
                    "/api/ipam/prefixes/",
                    params={"limit": limit, "offset": offset},
                )
                resp.raise_for_status()
            except Exception as exc:
                log.error("netbox_prefix_enrich.fetch_failed", error=str(exc))
                break

            payload = resp.json()
            for pfx in payload.get("results", []):
                cidr = (pfx.get("prefix") or "").strip()
                vlan = pfx.get("vlan") or {}
                site = pfx.get("site") or {}
                vid = vlan.get("vid")
                slug = site.get("slug") or ""
                if not cidr or vid is None:
                    # We need at least vid; missing site is OK because
                    # the canonical VLAN's netbox_site_slug may also be
                    # missing in mixed environments — the join handles
                    # that case via coalesce.
                    continue
                updates.append({
                    "cidr":     cidr,
                    "vid":      int(vid),
                    "slug":     slug,
                })

            if not payload.get("next"):
                break
            offset += limit

    if not updates:
        log.info("netbox_prefix_enrich.no_prefixes")
        return {"prefixes_seen": 0, "prefixes_tagged": 0}

    # MATCH existing Prefix nodes by CIDR (covering both schema variants:
    # SNMP uses ``prefix``, Meraki uses ``cidr``).  SET the NetBox info.
    async with driver.session() as session:
        result = await session.run(
            """
            UNWIND $updates AS u
            MATCH (p:Prefix)
            WHERE coalesce(p.cidr, p.prefix) = u.cidr
            SET p.netbox_vlan_vid   = u.vid,
                p.netbox_site_slug  = u.slug,
                p.netbox_updated_at = timestamp()
            RETURN count(DISTINCT p) AS n
            """,
            updates=updates,
        )
        rec = await result.single()
        tagged = rec["n"] if rec else 0

    log.info(
        "netbox_prefix_enrich.done",
        prefixes_seen=len(updates),
        prefixes_tagged=tagged,
    )
    return {"prefixes_seen": len(updates), "prefixes_tagged": tagged}


async def enrich_sites_from_netbox(
    netbox_url: str,
    netbox_token: str,
    verify_ssl: bool = True,
) -> dict[str, int]:
    """Map PlatformSite nodes to NetBox sites via the ``meraki_networks`` custom field.

    Each ``dcim.site`` in NetBox carries a ``custom_fields.meraki_networks``
    array — a list of ``{"id": "<meraki_network_id>", "name": "...", "tags": [...]}``
    objects that declares which Meraki networks belong to this site.  This is
    the authoritative N:1 mapping: multiple Meraki networks can be assigned to
    a single NetBox site by listing them all under that site's custom field.

    The function:
      1. Fetches all NetBox sites with their ``meraki_networks`` custom field.
      2. Detects duplicate mappings (same Meraki ``network_id`` on two
         different NetBox sites) and logs a data-quality warning; neither
         conflicting site wins so operators must resolve the ambiguity.
      3. Stamps ``netbox_site_slug``, ``netbox_site_name``, and
         ``netbox_site_id`` onto matching ``PlatformSite`` graph nodes
         (matched on ``ps.network_id`` or ``ps.slug`` == Meraki network ID).
      4. Propagates ``netbox_site_slug`` / ``netbox_site_name`` to child
         ``Device`` nodes that were not directly matched by
         ``enrich_devices_from_netbox`` (e.g. devices with no serial that
         couldn't be matched to a specific NetBox device record).  This
         ensures every device appears in the correct NetBox-site container
         even when the device itself has no NetBox entry.
    """
    driver = get_driver()

    headers = {
        "Authorization": f"Token {netbox_token}",
        "Accept": "application/json",
    }

    # ── 1. Paginate NetBox sites, build network_id → site info index ──────────
    #
    # Collision = same Meraki network_id claimed by two sites.  We skip both
    # rather than picking one arbitrarily — the operator must fix NetBox.
    nb_by_network_id: dict[str, dict] = {}
    collision_ids: set[str] = set()

    async with httpx.AsyncClient(
        base_url=netbox_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
        verify=verify_ssl,
    ) as client:
        offset = 0
        limit = 500
        while True:
            try:
                resp = await client.get(
                    "/api/dcim/sites/",
                    params={
                        "limit": limit,
                        "offset": offset,
                        "fields": "id,name,slug,custom_fields",
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                log.error("netbox_site_enrich.fetch_failed", error=str(exc))
                break

            payload = resp.json()
            for site in payload.get("results", []):
                slug    = (site.get("slug") or "").strip()
                name    = (site.get("name") or "").strip()
                site_id = str(site.get("id") or "")
                cf      = site.get("custom_fields") or {}
                for net in (cf.get("meraki_networks") or []):
                    nid = (net.get("id") or "").strip()
                    if not nid:
                        continue
                    if nid in nb_by_network_id:
                        prev = nb_by_network_id[nid]
                        log.warning(
                            "netbox_site_enrich.duplicate_network_id",
                            network_id=nid,
                            site_a=prev["site_slug"],
                            site_b=slug,
                            action="skipping_both — resolve in NetBox",
                        )
                        collision_ids.add(nid)
                    else:
                        nb_by_network_id[nid] = {
                            "site_slug": slug,
                            "site_name": name,
                            "site_id":   site_id,
                        }

            if not payload.get("next"):
                break
            offset += limit

    # Remove both sides of every collision so neither wins
    for nid in collision_ids:
        nb_by_network_id.pop(nid, None)

    if not nb_by_network_id:
        log.warning("netbox_site_enrich.no_network_mappings")
        return {
            "platform_sites_stamped": 0,
            "devices_site_propagated": 0,
            "collisions_detected": len(collision_ids),
        }

    log.info(
        "netbox_site_enrich.fetched",
        unique_sites=len({v["site_slug"] for v in nb_by_network_id.values()}),
        network_mappings=len(nb_by_network_id),
        collisions=len(collision_ids),
    )

    # ── 2. Stamp PlatformSite nodes ───────────────────────────────────────────
    ps_updates = [
        {"network_id": nid, **info}
        for nid, info in nb_by_network_id.items()
    ]
    async with driver.session() as session:
        result = await session.run(
            "UNWIND $updates AS upd "
            "MATCH (ps:PlatformSite) "
            "WHERE ps.network_id = upd.network_id OR ps.slug = upd.network_id "
            "SET ps.netbox_site_slug = upd.site_slug, "
            "    ps.netbox_site_name = upd.site_name, "
            "    ps.netbox_site_id   = upd.site_id "
            "RETURN count(ps) AS n",
            updates=ps_updates,
        )
        rec = await result.single()
        platform_sites_stamped = int(rec["n"] if rec else 0)

    # ── 3. Propagate slug to unmatched child devices ───────────────────────────
    #
    # Devices matched by enrich_devices_from_netbox already have
    # netbox_site_slug set directly.  Devices that weren't matched (no
    # serial, no name hit) inherit the slug from their PlatformSite so
    # they still appear under the correct NetBox-site container.
    async with driver.session() as session:
        result = await session.run(
            "MATCH (d:Device)-[:LOCATED_AT]->(ps:PlatformSite) "
            "WHERE (d.netbox_site_slug IS NULL OR d.netbox_site_slug = '') "
            "  AND ps.netbox_site_slug IS NOT NULL AND ps.netbox_site_slug <> '' "
            "SET d.netbox_site_slug = ps.netbox_site_slug, "
            "    d.netbox_site_name = ps.netbox_site_name "
            "RETURN count(d) AS n",
        )
        rec = await result.single()
        devices_site_propagated = int(rec["n"] if rec else 0)

    log.info(
        "netbox_site_enrich.done",
        platform_sites_stamped=platform_sites_stamped,
        devices_site_propagated=devices_site_propagated,
        collisions_detected=len(collision_ids),
    )
    return {
        "platform_sites_stamped": platform_sites_stamped,
        "devices_site_propagated": devices_site_propagated,
        "collisions_detected": len(collision_ids),
    }
