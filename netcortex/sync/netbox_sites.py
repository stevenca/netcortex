"""Sync NetBox Sites and Locations into the Neo4j graph.

Sites (NodeType.SITE, id prefix "nb-site:<slug>") are the canonical containers.
Locations (NodeType.LOCATION, id prefix "nb-loc:<id>") are optional sub-containers
that mirror NetBox's hierarchical location model (Building > Floor > Room, etc.).

Platform adapters emit PlatformSite nodes keyed by platform IDs.
The site_correlate module links those to canonical Sites via MAPS_TO_SITE edges.

Locations are fetched opportunistically; if the NetBox instance has none the sync
still succeeds and only Site nodes are created.
"""

from __future__ import annotations

import re

import httpx
import structlog

from netcortex.graph.ingest import ingest_graph_data
from netcortex.graph.models import (
    Dimension,
    EdgeType,
    GraphData,
    GraphEdge,
    GraphNode,
    NodeType,
)

log = structlog.get_logger(__name__)

_ADAPTER_ID = "netbox-sync"


def _norm(text: str) -> str:
    """Normalize a name for fuzzy matching: lowercase, strip non-alphanumeric."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


async def sync_netbox_sites(netbox_url: str, netbox_token: str) -> dict[str, int]:
    """Pull Sites and Locations from NetBox and upsert into Neo4j.

    Returns:
        {"sites": <n>, "locations": <n>} counts of upserted objects.
    """
    data = GraphData(adapter_id=_ADAPTER_ID)

    headers = {
        "Authorization": f"Token {netbox_token}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(
        base_url=netbox_url,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        sites_by_nb_id: dict[int, str] = {}   # netbox_id → graph node id
        locs_by_nb_id:  dict[int, str] = {}   # netbox_id → graph node id

        # ── Sites ──────────────────────────────────────────────────────────
        try:
            resp = await client.get("/api/dcim/sites/", params={"limit": 1000})
            resp.raise_for_status()
            for site in resp.json().get("results", []):
                node_id = f"nb-site:{site['slug']}"
                sites_by_nb_id[site["id"]] = node_id
                data.nodes.append(GraphNode(
                    id=node_id,
                    type=NodeType.SITE,
                    netbox_id=site["id"],
                    netbox_type="dcim.site",
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=_ADAPTER_ID,
                    properties={
                        "name":             site["name"],
                        "slug":             site["slug"],
                        "status":           (site.get("status") or {}).get("value", ""),
                        "region":           ((site.get("region") or {}).get("name") or ""),
                        "tenant":           ((site.get("tenant") or {}).get("name") or ""),
                        "description":      site.get("description") or "",
                        "physical_address": site.get("physical_address") or "",
                        "latitude":         site.get("latitude"),
                        "longitude":        site.get("longitude"),
                        "normalized_name":  _norm(site["name"]),
                    },
                ))
        except Exception as exc:
            log.error("netbox_sync.sites_failed", error=str(exc))
            return {"sites": 0, "locations": 0}

        # ── Locations (optional) ────────────────────────────────────────────
        try:
            resp = await client.get("/api/dcim/locations/", params={"limit": 1000})
            resp.raise_for_status()
            for loc in resp.json().get("results", []):
                node_id = f"nb-loc:{loc['id']}"
                locs_by_nb_id[loc["id"]] = node_id
                data.nodes.append(GraphNode(
                    id=node_id,
                    type=NodeType.LOCATION,
                    netbox_id=loc["id"],
                    netbox_type="dcim.location",
                    dimensions=[Dimension.PHYSICAL],
                    source_adapter=_ADAPTER_ID,
                    properties={
                        "name":            loc["name"],
                        "slug":            loc.get("slug") or "",
                        "description":     loc.get("description") or "",
                        "site_nb_id":      (loc.get("site") or {}).get("id"),
                        "parent_nb_id":    (loc.get("parent") or {}).get("id"),
                        "normalized_name": _norm(loc["name"]),
                    },
                ))
        except Exception as exc:
            log.info("netbox_sync.locations_skipped", reason=str(exc))

    # ── WITHIN_LOCATION edges ───────────────────────────────────────────────
    # Build after all nodes are collected so IDs are available.
    for node in data.nodes:
        if node.type != NodeType.LOCATION:
            continue
        parent_nb_id = node.properties.get("parent_nb_id")
        site_nb_id   = node.properties.get("site_nb_id")

        if parent_nb_id and parent_nb_id in locs_by_nb_id:
            # Location → parent Location
            data.edges.append(GraphEdge(
                source_id=node.id,
                target_id=locs_by_nb_id[parent_nb_id],
                type=EdgeType.WITHIN_LOCATION,
                dimension=Dimension.PHYSICAL,
                source_adapter=_ADAPTER_ID,
            ))
        elif site_nb_id and site_nb_id in sites_by_nb_id:
            # Top-level Location → Site
            data.edges.append(GraphEdge(
                source_id=node.id,
                target_id=sites_by_nb_id[site_nb_id],
                type=EdgeType.WITHIN_LOCATION,
                dimension=Dimension.PHYSICAL,
                source_adapter=_ADAPTER_ID,
            ))

    await ingest_graph_data(data)

    log.info(
        "netbox_sync.sites_done",
        sites=len(sites_by_nb_id),
        locations=len(locs_by_nb_id),
    )
    return {"sites": len(sites_by_nb_id), "locations": len(locs_by_nb_id)}
