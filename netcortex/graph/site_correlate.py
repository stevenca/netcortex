"""Site correlation — link PlatformSite nodes to canonical NetBox Site nodes.

Three strategies (applied in order):

  1. Explicit mapping  — from a "site_mappings" key in the core secret, e.g.
         {"site_mappings": {"meraki-network:Q3LB...": "nb-site:fulton"}}
     The key is the PlatformSite graph id; the value is the canonical Site id.

  2. NetBox slug match — PlatformSite nodes already stamped with
     ``netbox_site_slug`` by ``enrich_sites_from_netbox`` are linked to
     the Site node with that slug.  This is the primary path for Meraki
     networks and is the only correct approach for N:1 (multiple Meraki
     networks → one NetBox site) because the mapping is read directly from
     NetBox's ``meraki_networks`` custom field rather than inferred from name
     similarity.  Confidence 1.0.

  3. Name-match fallback — for PlatformSite nodes still unmatched after
     strategies 1 & 2 (e.g. non-Meraki platforms such as Intersight, NDFC,
     Catalyst Center that are not in the ``meraki_networks`` custom field),
     normalize PlatformSite.name vs Site.normalized_name.  Confidence 0.9.

After this runs the topology query layer uses ``d.netbox_site_slug`` on
Device nodes for visual container assignment.  The ``MAPS_TO_SITE`` edges
created here are used for graph-level enrichment queries and the site stats
endpoint; they are intentionally excluded from topology rendering.
"""

from __future__ import annotations

import structlog

from netcortex.graph.client import get_driver

log = structlog.get_logger(__name__)


async def run_site_correlation(
    explicit_mappings: dict[str, str] | None = None,
) -> dict[str, int]:
    """Create MAPS_TO_SITE edges from PlatformSite → canonical Site.

    Args:
        explicit_mappings: Optional {platform_site_id: canonical_site_id}.
                           These override all other strategies for covered entries.

    Returns:
        {"explicit": <n>, "slug_match": <n>, "name_match": <n>}
    """
    driver = get_driver()
    explicit_count = 0
    slug_match_count = 0
    name_match_count = 0

    async with driver.session() as session:
        # ── Strategy 1: Explicit mappings from operator config ────────────────
        if explicit_mappings:
            for ps_id, site_id in explicit_mappings.items():
                result = await session.run(
                    """
                    MATCH (ps:PlatformSite {id: $ps_id})
                    MATCH (s:Site {id: $site_id})
                    MERGE (ps)-[r:MAPS_TO_SITE]->(s)
                    ON CREATE SET r.method = 'explicit', r.created_at = timestamp()
                    RETURN count(r) AS n
                    """,
                    ps_id=ps_id,
                    site_id=site_id,
                )
                rec = await result.single()
                explicit_count += rec["n"] if rec else 0

        # ── Strategy 2: netbox_site_slug — set by enrich_sites_from_netbox ────
        #
        # This is the authoritative path.  enrich_sites_from_netbox reads the
        # meraki_networks custom field from NetBox sites and stamps
        # ps.netbox_site_slug on each matching PlatformSite.  We turn that
        # stamp into a graph edge here.  Explicit mappings are not overridden
        # (an operator who explicitly mapped a PlatformSite wins).
        result = await session.run(
            """
            MATCH (ps:PlatformSite)
            WHERE ps.netbox_site_slug IS NOT NULL
              AND ps.netbox_site_slug <> ''
              AND NOT (ps)-[:MAPS_TO_SITE {method: 'explicit'}]->(:Site)
            MATCH (s:Site {slug: ps.netbox_site_slug})
            MERGE (ps)-[r:MAPS_TO_SITE]->(s)
            ON CREATE SET r.method     = 'netbox_slug',
                          r.confidence = 1.0,
                          r.created_at = timestamp()
            ON MATCH  SET r.method     = 'netbox_slug',
                          r.confidence = 1.0
            RETURN count(r) AS n
            """
        )
        rec = await result.single()
        slug_match_count = rec["n"] if rec else 0

        # ── Strategy 3: Name-match fallback ───────────────────────────────────
        #
        # For PlatformSite nodes not yet correlated by strategies 1 or 2
        # (typically non-Meraki platforms: Intersight domains, NDFC fabrics,
        # Catalyst Center).  Both sides are lowercased and stripped of
        # non-alphanumeric characters for comparison.
        result = await session.run(
            """
            MATCH (ps:PlatformSite)
            WHERE NOT (ps)-[:MAPS_TO_SITE]->(:Site)
              AND ps.normalized_name IS NOT NULL
              AND ps.normalized_name <> ''
            MATCH (s:Site)
            WHERE s.normalized_name = ps.normalized_name
            MERGE (ps)-[r:MAPS_TO_SITE]->(s)
            ON CREATE SET r.method     = 'name_match',
                          r.confidence = 0.9,
                          r.created_at = timestamp()
            RETURN count(r) AS n
            """
        )
        rec = await result.single()
        name_match_count = rec["n"] if rec else 0

    log.info(
        "site_correlate.done",
        explicit=explicit_count,
        slug_match=slug_match_count,
        name_match=name_match_count,
    )
    return {
        "explicit": explicit_count,
        "slug_match": slug_match_count,
        "name_match": name_match_count,
    }


async def get_site_correlation_stats() -> dict[str, int]:
    """Return counts of correlated vs unmatched PlatformSite nodes."""
    driver = get_driver()
    async with driver.session() as session:
        r1 = await session.run(
            "MATCH (:PlatformSite)-[:MAPS_TO_SITE]->(:Site) RETURN count(*) AS n"
        )
        r2 = await session.run(
            "MATCH (ps:PlatformSite) WHERE NOT (ps)-[:MAPS_TO_SITE]->() RETURN count(ps) AS n"
        )
        rec1 = await r1.single()
        rec2 = await r2.single()
    return {
        "correlated": rec1["n"] if rec1 else 0,
        "unmatched":  rec2["n"] if rec2 else 0,
    }
