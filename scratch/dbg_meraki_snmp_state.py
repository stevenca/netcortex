"""Inventory Meraki devices and their current SNMP polling state.

Goal: understand which Meraki product types (MX, MS, MR, CW, MV, etc.)
have direct SNMP working vs only cloud SNMP, broken down by:
  - product type
  - reachable via mgmt_ip (could we even try?)
  - direct status (succeeded/failed/never tried)
  - last status message
  - which credentials would be tried
"""
import asyncio
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
from netcortex.config import init_settings
from netcortex.graph.client import init_client, get_driver, close


async def main():
    s = await init_settings()
    pwd = s.neo4j_password.get_secret_value() if hasattr(s.neo4j_password, "get_secret_value") else s.neo4j_password
    await init_client(str(s.neo4j_uri), s.neo4j_user, pwd)
    drv = get_driver()
    async with drv.session() as sess:
        r = await sess.run(
            """
            MATCH (d:Device)
            WHERE d.platform = 'meraki'
              AND coalesce(d.tombstoned, false) = false
              AND d.canonical_id IS NULL
            RETURN d.name AS name,
                   d.role AS role,
                   d.productType AS pt,
                   d.model AS model,
                   d.mgmt_ip AS mgmt_ip,
                   d.snmp_direct AS direct,
                   d.snmp_cloud AS cloud,
                   d.snmp_source AS source,
                   d.snmp_sources AS sources,
                   d.snmp_health AS health,
                   d.snmp_last_status AS last_status,
                   d.snmp_polled AS polled,
                   d.snmp_last_error AS last_error,
                   d.netbox_site_slug AS site
            ORDER BY d.productType, d.name
            """
        )
        rows = await r.data()

    # Breakdown by product type
    print(f"=== {len(rows)} Meraki devices total ===\n")
    by_pt: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_pt[row.get("pt") or "unknown"].append(row)
    print(f"{'productType':<14} {'total':>6} {'has mgmt_ip':>12} {'direct=T':>9} {'cloud=T':>8} {'no SNMP':>8}")
    print("-" * 60)
    for pt in sorted(by_pt.keys()):
        items = by_pt[pt]
        total = len(items)
        with_ip = sum(1 for x in items if x.get("mgmt_ip"))
        direct = sum(1 for x in items if x.get("direct"))
        cloud = sum(1 for x in items if x.get("cloud"))
        no_snmp = sum(1 for x in items if not x.get("direct") and not x.get("cloud"))
        print(f"{pt:<14} {total:>6} {with_ip:>12} {direct:>9} {cloud:>8} {no_snmp:>8}")
    print()

    # Drill into cpn-ful (the site we care about most)
    print("=== cpn-ful Meraki devices in detail ===\n")
    cpn_ful = [r for r in rows if r.get("site") == "cpn-ful"]
    for row in cpn_ful:
        print(f"  {row['name']:<28}  pt={row.get('pt'):<10}  model={row.get('model') or '?':<10}  mgmt={row.get('mgmt_ip') or '?':<18}  "
              f"direct={str(row.get('direct') or False):<5}  cloud={str(row.get('cloud') or False):<5}  "
              f"health={row.get('health') or '?'}  last={row.get('last_status') or '?'}")
        if row.get("last_error"):
            print(f"    last_error: {row['last_error']}")
    print()

    await close()


asyncio.run(main())
