"""Run the new Meraki-aware mgmt_ip rule and report the diff."""
import asyncio
import sys

sys.path.insert(0, "/app")
from netcortex.config import init_settings
from netcortex.graph.client import init_client, get_driver, close


async def main():
    s = await init_settings()
    pwd = s.neo4j_password.get_secret_value() if hasattr(s.neo4j_password, "get_secret_value") else s.neo4j_password
    await init_client(str(s.neo4j_uri), s.neo4j_user, pwd)
    drv = get_driver()
    async with drv.session() as sess:
        # Dry-run first: show every Meraki firewall + computed rule_ip
        # alongside the currently-stored mgmt_ip so we can spot any
        # divergences before write.
        r = await sess.run(
            """
            MATCH (d:Device)
            WHERE d.platform = 'meraki' AND d.role = 'firewall'
            WITH d,
                 CASE
                   WHEN d.on_sdwan = true THEN
                     coalesce(
                       CASE WHEN d.vpn_ip  IS NOT NULL AND d.vpn_ip  <> '' THEN d.vpn_ip  END,
                       CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                       CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                     )
                   ELSE
                     coalesce(
                       CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                       CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                     )
                 END AS rule_ip
            RETURN d.name AS name,
                   d.on_sdwan AS on_sdwan,
                   d.mgmt_ip AS mgmt_ip,
                   d.vpn_ip AS vpn_ip,
                   d.wan1_ip AS wan1_ip,
                   d.wan2_ip AS wan2_ip,
                   rule_ip AS rule_ip
            ORDER BY d.name
            """
        )
        rows = await r.data()
        diverge = [row for row in rows if row["rule_ip"] and row["mgmt_ip"] != row["rule_ip"]]
        empty = [row for row in rows if not row["rule_ip"]]
        ok = [row for row in rows if row["rule_ip"] and row["mgmt_ip"] == row["rule_ip"]]

        print(f"Meraki appliances: {len(rows)}")
        print(f"  ✓ mgmt_ip already matches rule:     {len(ok)}")
        print(f"  → rule says different:               {len(diverge)} (will be repaired)")
        print(f"  ⚠ rule produces no candidate:        {len(empty)} (no fix possible — API returned nothing)")

        if diverge:
            print("\n  Repair candidates:")
            for row in diverge[:20]:
                print(f"    {row['name']:<35}  sdwan={row['on_sdwan']:<5}  current={row['mgmt_ip']!r:<22}  rule={row['rule_ip']!r}")

        if empty:
            print("\n  No-fix MXs (API returned no usable IP):")
            for row in empty[:20]:
                cn = (row['name'] or '')[:35]
                print(f"    {cn:<35}  sdwan={row['on_sdwan']}  vpn={row['vpn_ip']!r}  wan1={row['wan1_ip']!r}  wan2={row['wan2_ip']!r}")

        # Now actually apply the repair
        print("\n  Applying repair...")
        fix = await sess.run(
            """
            MATCH (d:Device)
            WHERE d.platform = 'meraki' AND d.role = 'firewall'
            WITH d,
                 CASE
                   WHEN d.on_sdwan = true THEN
                     coalesce(
                       CASE WHEN d.vpn_ip  IS NOT NULL AND d.vpn_ip  <> '' THEN d.vpn_ip  END,
                       CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                       CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                     )
                   ELSE
                     coalesce(
                       CASE WHEN d.wan1_ip IS NOT NULL AND d.wan1_ip <> '' THEN d.wan1_ip END,
                       CASE WHEN d.wan2_ip IS NOT NULL AND d.wan2_ip <> '' THEN d.wan2_ip END
                     )
                 END AS rule_ip
            WHERE rule_ip IS NOT NULL AND coalesce(d.mgmt_ip, '') <> rule_ip
            WITH d, rule_ip
            SET d.mgmt_ip = rule_ip
            REMOVE d._content_hash
            RETURN count(d) AS n
            """
        )
        rec = await fix.single()
        print(f"  → repaired {rec['n']} Meraki MX(s)")

    await close()


asyncio.run(main())
