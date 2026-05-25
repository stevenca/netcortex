"""Investigate why MX devices don't show the user-expected mgmt_ip.

Path traced:
  1. What's currently in Neo4j for MX devices?
  2. What did the Meraki adapter compute (candidate_ips, wan_ip, vpn_ip,
     on_sdwan)?
  3. What does the live Meraki API return for one MX network's appliance
     vlans + uplink statuses + AutoVPN membership?
  4. Where exactly does the rule break — is the data missing, or are we
     not consulting it correctly?
"""
import asyncio
import json
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
        print("=== Step 1: MX devices in Neo4j ===")
        r = await sess.run(
            """
            MATCH (d:Device)
            WHERE d.platform_metadata_productType = 'appliance'
               OR d.role = 'firewall'
               OR d.name STARTS WITH 'cpn-' AND d.name ENDS WITH '-mx1'
               OR d.platform_id IN ['Q5TY-EB22-LPZG']  // fallback
               OR d.id CONTAINS 'meraki:' AND (d.role = 'firewall' OR d.name CONTAINS 'mx')
            RETURN d.name AS name,
                   d.id AS id,
                   d.role AS role,
                   d.mgmt_ip AS mgmt_ip,
                   d.candidate_ips AS candidate_ips,
                   d.wan1_ip AS wan1_ip,
                   d.wan2_ip AS wan2_ip,
                   d.wan1_public_ip AS wan1_public_ip,
                   d.vpn_ip AS vpn_ip,
                   d.on_sdwan AS on_sdwan,
                   d.platform AS platform,
                   d.productType AS productType,
                   d.networkId AS networkId,
                   d._content_hash AS content_hash
            ORDER BY d.name
            """
        )
        mx_rows = await r.data()
        # Filter to ones that look like MXs
        mx_rows = [
            row for row in mx_rows
            if (row.get("role") == "firewall"
                or (row.get("name") or "").lower().endswith(("-mx1", "-mx2"))
                or (row.get("name") or "").lower().startswith("mx")
                or row.get("productType") == "appliance"
                or "MX" in (row.get("name") or "").upper())
        ]
        if not mx_rows:
            print("  (no MX devices matched)")
        # Focus on cpn-* MXs (the user's main concern)
        cpn_mx = [r for r in mx_rows if (r.get("name") or "").lower().startswith("cpn-")]
        empty_mgmt = [r for r in mx_rows if not r.get("mgmt_ip")]
        wrong_rule = []
        for r in mx_rows:
            if not r.get("mgmt_ip"):
                continue
            mgmt = r.get("mgmt_ip")
            on = r.get("on_sdwan")
            wan1 = r.get("wan1_ip")
            wan2 = r.get("wan2_ip")
            vpn = r.get("vpn_ip")
            if on:
                # SDWAN MX → mgmt should match vpnIp OR a LAN applianceIp
                # (vpnIp is the easiest proxy when we don't have vlans handy)
                if vpn and mgmt != vpn:
                    wrong_rule.append((r["name"], "SDWAN", f"mgmt={mgmt} but vpnIp={vpn}"))
            else:
                # Non-SDWAN MX → mgmt should be wan1Ip or wan2Ip (NOT public)
                if mgmt not in (wan1, wan2) and (wan1 or wan2):
                    wrong_rule.append((r["name"], "NON-SDWAN", f"mgmt={mgmt} but wan1={wan1} wan2={wan2}"))

        print(f"\n  Total MXs found: {len(mx_rows)}")
        print(f"  cpn-* prefix:    {len(cpn_mx)}")
        print(f"  mgmt_ip empty:   {len(empty_mgmt)}")
        print(f"  rule violations: {len(wrong_rule)}")
        if cpn_mx:
            print("\n  -- cpn-* MX details --")
            for row in cpn_mx:
                print(f"    {row['name']}  on_sdwan={row.get('on_sdwan')}  mgmt_ip={row.get('mgmt_ip')!r}  vpn_ip={row.get('vpn_ip')!r}  wan1={row.get('wan1_ip')!r}  wan2={row.get('wan2_ip')!r}")
        if empty_mgmt:
            print("\n  -- mgmt_ip='' MXs --")
            for row in empty_mgmt:
                print(f"    {row['name']}  on_sdwan={row.get('on_sdwan')}  candidate_ips={row.get('candidate_ips')}  wan1={row.get('wan1_ip')!r}  vpn={row.get('vpn_ip')!r}")
        if wrong_rule:
            print("\n  -- RULE VIOLATIONS --")
            for nm, kind, msg in wrong_rule:
                print(f"    {nm} [{kind}]: {msg}")
        print()
        # Now print full detail for ALL MXs (for completeness)
        # What signals do the LLDP stubs carry?
        print("\n=== Step 2a: LLDP-stub MX nodes — what identifiers do they have? ===")
        r = await sess.run(
            """
            MATCH (stub:Device)
            WHERE stub.id STARTS WITH 'lldp-neighbor:'
              AND stub.name CONTAINS '-mx'
              AND stub.name STARTS WITH 'cpn-'
            RETURN stub.id AS id, stub.name AS name,
                   stub.chassis_mac AS chassis_mac,
                   stub.mgmt_ip AS mgmt_ip,
                   stub.canonical_id AS canonical_id,
                   stub.platform AS platform,
                   stub.stub AS stub_flag,
                   keys(stub) AS keys
            ORDER BY stub.name LIMIT 10
            """
        )
        for row in await r.data():
            print(f"\n  {row['name']}  (id={row['id']})")
            print(f"    chassis_mac  = {row['chassis_mac']!r}")
            print(f"    mgmt_ip      = {row['mgmt_ip']!r}")
            print(f"    canonical_id = {row['canonical_id']!r}")
            print(f"    platform     = {row['platform']!r}")
            print(f"    stub_flag    = {row['stub_flag']!r}")
            print(f"    all keys     = {row['keys']}")

        # Investigate the shadow / duplicate Device nodes
        print("\n\n=== Step 2: Duplicate Device nodes per MX name ===")
        r = await sess.run(
            """
            MATCH (d:Device)
            WHERE d.name STARTS WITH 'cpn-' AND (d.name CONTAINS 'mx' OR d.name CONTAINS 'MX')
            RETURN d.name AS name,
                   count(d) AS variant_count,
                   collect({id: d.id,
                            role: d.role,
                            mgmt_ip: d.mgmt_ip,
                            productType: d.productType,
                            platform: d.platform,
                            source_adapter: d.source_adapter}) AS variants
            ORDER BY variant_count DESC, name
            LIMIT 10
            """
        )
        for row in await r.data():
            print(f"\n  {row['name']}  ({row['variant_count']} variants)")
            for v in row['variants']:
                print(f"    id={v['id']:<55}  role={v['role']:<10}  mgmt_ip={v['mgmt_ip']!r:<20}  source={v['source_adapter']}  productType={v['productType']}")
        return  # short-circuit; raw dump below isn't useful right now
        for row in mx_rows[:30]:
            print(f"\n  {row['name']}  (id={row['id']})")
            print(f"    role           = {row.get('role')}")
            print(f"    platform       = {row.get('platform')}")
            print(f"    productType    = {row.get('productType')}")
            print(f"    networkId      = {row.get('networkId')}")
            print(f"    mgmt_ip        = {row.get('mgmt_ip')!r}")
            print(f"    candidate_ips  = {row.get('candidate_ips')}")
            print(f"    on_sdwan       = {row.get('on_sdwan')}")
            print(f"    wan1_ip        = {row.get('wan1_ip')!r}")
            print(f"    wan2_ip        = {row.get('wan2_ip')!r}")
            print(f"    wan1_public_ip = {row.get('wan1_public_ip')!r}")
            print(f"    vpn_ip         = {row.get('vpn_ip')!r}")
            print(f"    content_hash   = {row.get('content_hash')}")
    await close()


asyncio.run(main())
