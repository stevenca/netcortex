"""Neo4j schema setup — constraints and indexes.

Call setup_schema() once at startup after the driver is initialised.
All constraints use CREATE CONSTRAINT IF NOT EXISTS so re-runs are idempotent.
"""

from __future__ import annotations

import structlog

from netcortex.graph.client import get_driver

log = structlog.get_logger(__name__)

# (label, property) pairs that must be unique
_UNIQUE_CONSTRAINTS: list[tuple[str, str]] = [
    ("Device", "id"),
    ("Interface", "id"),
    ("VLAN", "id"),
    ("VNI", "id"),
    ("VRF", "id"),
    ("Prefix", "id"),
    ("IPAddress", "id"),
    ("MACAddress", "id"),
    ("ARPEntry", "id"),
    ("BGPSession", "id"),
    ("SDWANTunnel", "id"),
    ("SDWANPolicy", "id"),
    ("Site", "id"),         # canonical NetBox site
    ("Location", "id"),     # NetBox hierarchical location (optional)
    ("PlatformSite", "id"), # adapter-specific container (Meraki network, CATC site…)
]

# (label, property) pairs to index for fast lookups
_INDEXES: list[tuple[str, str]] = [
    ("Device", "name"),
    ("Device", "netbox_id"),
    ("Device", "platform_id"),
    ("Device", "mgmt_ip"),           # SNMP coverage match-by-IP
    ("Device", "snmp_polled"),       # status panel filter
    ("Device", "stub"),              # inventory excludes stubs
    ("Device", "canonical_id"),      # duplicate-device filter (used in every topology query)
    ("Device", "netbox_site_slug"),  # site-filter fallback for unassigned-bucket devices
    ("Device", "source_adapter"),    # per-adapter scoped queries
    ("Interface", "device_id"),
    ("Interface", "mac"),
    ("VLAN", "vid"),
    ("VNI", "vni_id"),
    ("BGPSession", "remote_ip"),
    ("Site", "slug"),
    ("Site", "normalized_name"),
    ("Location", "netbox_id"),
    ("Location", "normalized_name"),
    ("PlatformSite", "normalized_name"),
    ("MACAddress", "mac"),
    ("MACAddress", "vendor"),
    ("ARPEntry", "ip"),
    ("ARPEntry", "mac"),
    ("IPAddress", "address"),
    ("IPAddress", "version"),
    ("Prefix", "prefix"),
    ("Prefix", "version"),
    ("STPDomain", "root_bridge_mac"),
    ("STPDomain", "vlan"),
    ("RoutingPeer", "peer_ip"),
    ("RoutingPeer", "protocol"),
    ("RoutingPeer", "stub"),
]


async def setup_schema() -> None:
    """Create all constraints and indexes idempotently."""
    driver = get_driver()

    # Constraints and indexes in one session
    async with driver.session() as session:
        for label, prop in _UNIQUE_CONSTRAINTS:
            constraint_name = f"unique_{label.lower()}_{prop}"
            cypher = (
                f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            await session.run(cypher)
            log.debug("neo4j.constraint_ensured", label=label, property=prop)

        for label, prop in _INDEXES:
            index_name = f"idx_{label.lower()}_{prop}"
            cypher = (
                f"CREATE INDEX {index_name} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.{prop})"
            )
            await session.run(cypher)
            log.debug("neo4j.index_ensured", label=label, property=prop)

    # Migration in a fresh session: relabel old platform Site nodes → PlatformSite.
    # NetBox-canonical sites start with "nb-site:"; everything else is a platform container.
    # This is idempotent: nodes already labelled PlatformSite won't match :Site.
    # Runs in its own session and try/except so a constraint conflict never
    # causes setup_schema() to raise and poison the overall Neo4j status.
    try:
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n:Site) WHERE NOT n.id STARTS WITH 'nb-site:' "
                "REMOVE n:Site SET n:PlatformSite "
                "RETURN count(n) AS migrated"
            )
            rec = await result.single()
            migrated = rec["migrated"] if rec else 0
            if migrated:
                log.info("neo4j.migration_platform_sites", migrated=migrated)
    except Exception as exc:
        log.warning("neo4j.migration_skipped", error=str(exc))

    log.info("neo4j.schema_ready", constraints=len(_UNIQUE_CONSTRAINTS), indexes=len(_INDEXES))
