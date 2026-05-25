"""Ask the Meraki Dashboard API what SNMP config each network has.

Meraki has two distinct SNMP surfaces:
  1. Organization-level SNMP (snmp.meraki.com)  → already working as cloud SNMP
  2. Network-level SNMP             → controls direct device SNMP
     Endpoint: GET /networks/{networkId}/snmp

We want #2 — that determines whether direct device SNMP is even
enabled, and what creds the devices expect.
"""
import asyncio
import sys

sys.path.insert(0, "/app")
import httpx

from netcortex.config import init_settings
from netcortex.secrets import get_secret_backend


async def main():
    await init_settings()
    backend = get_secret_backend()
    index = await backend.get_adapter_index()
    cpn_entries = [e for e in (index or []) if e.get("type") == "meraki" and e.get("name") in ("CPN", "CPNGOV")]
    for entry in cpn_entries:
        name = entry["name"]
        cfg = await backend.get_adapter_config("meraki", name)
        api_key = cfg.get("api_key", "")
        org_id = cfg.get("org_id", "")
        print(f"\n=== meraki/{name}  org={org_id} ===")
        async with httpx.AsyncClient(
            headers={"X-Cisco-Meraki-API-Key": api_key, "Accept": "application/json"},
            timeout=30.0,
        ) as client:
            # List networks for the org
            resp = await client.get(f"https://api.meraki.com/api/v1/organizations/{org_id}/networks")
            nets = resp.json() if resp.is_success else []

            # Focus on cpn-* networks
            target_nets = [n for n in nets if (n.get("name") or "").lower().startswith("cpn-ful")]
            if not target_nets:
                # Fall back to first 5
                target_nets = nets[:5]
            print(f"  inspecting {len(target_nets)} network(s):")
            for net in target_nets:
                nid = net["id"]
                nname = net["name"]
                snmp_resp = await client.get(f"https://api.meraki.com/api/v1/networks/{nid}/snmp")
                if not snmp_resp.is_success:
                    print(f"  {nname:<22}  HTTP {snmp_resp.status_code}: {snmp_resp.text[:80]}")
                    continue
                snmp = snmp_resp.json()
                # Mask passphrase
                users = snmp.get("users", [])
                masked_users = []
                for u in users:
                    pp = u.get("passphrase") or ""
                    masked_users.append({"username": u.get("username"),
                                          "passphrase": (pp[:2] + "***" + pp[-1:]) if len(pp) > 4 else "***"})
                cs = snmp.get("communityString") or ""
                print(f"  {nname:<22}  access={snmp.get('access')!r}  "
                      f"community={(cs[:2] + '***' + cs[-1:]) if len(cs) > 4 else ('(empty)' if not cs else '***')}  "
                      f"users={masked_users}")
asyncio.run(main())
