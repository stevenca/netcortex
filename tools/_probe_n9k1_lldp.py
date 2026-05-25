"""Run _poll_lldp / _poll_cdp directly against cpn-ful-n9k1 and dump
the resulting GraphData so we can see whether the SNMP adapter is
actually emitting the LLDP/CDP edges or silently dropping them.

    docker exec netcortex-worker python /app/tools/_probe_n9k1_lldp.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from netcortex.config import init_settings
from netcortex.secrets import get_secret_backend
from netcortex.snmp.credentials import SnmpCredentialResolver, SnmpContext
from netcortex.adapters.snmp import (
    _SnmpSession,
    _poll_interfaces,
    _poll_lldp,
    _poll_cdp,
)


HOST = "192.133.162.80"
DEV_ID = "ndfc:cpn-ful-nd1:FDO28030W6Q"
DEV_NAME = "cpn-ful-n9k1"


async def main() -> None:
    await init_settings()
    backend = get_secret_backend()
    resolver = SnmpCredentialResolver(backend)
    cred = await resolver.resolve(
        device_name=DEV_NAME,
        source_adapter="snmp/default",
        context=SnmpContext.DEVICE,
    )

    sess = _SnmpSession(HOST, cred, timeout=5.0, walk_timeout=30.0)
    if_map = await _poll_interfaces(sess)
    print(f"interface entries: {len(if_map)}; sample keys:",
          list(if_map.keys())[:5])

    import inspect
    print("RUNTIME _poll_lldp sig:", inspect.signature(_poll_lldp))
    print("RUNTIME _poll_lldp file:", inspect.getsourcefile(_poll_lldp))

    lldp = await _poll_lldp(sess, DEV_ID, if_map, discovered_by="snmp/default")
    print(f"\nLLDP nodes: {len(lldp.nodes)}  edges: {len(lldp.edges)}")
    for n in lldp.nodes[:20]:
        print(f"  NODE id={n.id!r} name={n.properties.get('name')!r}")
    for e in lldp.edges[:20]:
        print(f"  EDGE {e.source_id!r} -[{e.type.value}]-> {e.target_id!r} "
              f"if_a={e.properties.get('interface_a')!r} "
              f"if_b={e.properties.get('interface_b')!r}")

    cdp = await _poll_cdp(sess, DEV_ID, if_map, discovered_by="snmp/default")
    print(f"\nCDP nodes: {len(cdp.nodes)}  edges: {len(cdp.edges)}")
    for n in cdp.nodes[:20]:
        print(f"  NODE id={n.id!r} name={n.properties.get('name')!r}")
    for e in cdp.edges[:20]:
        print(f"  EDGE {e.source_id!r} -[{e.type.value}]-> {e.target_id!r} "
              f"if_a={e.properties.get('interface_a')!r} "
              f"if_b={e.properties.get('interface_b')!r}")


if __name__ == "__main__":
    asyncio.run(main())
