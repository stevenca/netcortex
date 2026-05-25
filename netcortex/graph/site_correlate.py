"""Site correlation — link PlatformSite nodes to canonical NetBox Site nodes.

Two strategies (applied in order):
  1. Explicit mapping  — from a "site_mappings" key in the core secret, e.g.
         {"site_mappings": {"meraki-network:Q3LB...": "nb-site:fulton"}}
     The key is the PlatformSite graph id; the value is the canonical Site id.

  2. Name-match — normalize the PlatformSite.name vs Site.normalized_name
     (both lowercased, non-alphanumeric stripped).  A match creates a
     MAPS_TO_SITE edge with confidence 0.9 and method "name_match".

After this runs, Cytoscape.js uses the MAPS_TO_SITE edges to nest PlatformSite
nodes inside canonical Site compound containers.
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
                           These override name-match for covered entries.

    Returns:
        {"explicit": <n>, "name_match": <n>}
    """
    driver = get_driver()
    explicit_count = 0
    name_match_count = 0

    async with driver.session() as session:
        # 1. Explicit mappings
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

        # 2. Name-match for PlatformSite nodes not yet correlated
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
        name_match_count += rec["n"] if rec else 0

    log.info(
        "site_correlate.done",
        explicit=explicit_count,
        name_match=name_match_count,
    )
    return {"explicit": explicit_count, "name_match": name_match_count}


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
