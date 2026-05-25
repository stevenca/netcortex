"""Run the meraki adapter for CPN and print exactly what dev_props
gets sent to the graph for cpn-ful-mx1."""
import asyncio
import sys

sys.path.insert(0, "/app")
from netcortex.config import init_settings
from netcortex.adapters import load_instances
from netcortex.adapters.meraki import MerakiAdapter


async def main():
    s = await init_settings()
    adapters = await load_instances()
    mx_adapter: MerakiAdapter | None = None
    for inst_id, adapter in adapters.items():
        if inst_id == "meraki/CPN":
            mx_adapter = adapter
            break
    if not mx_adapter:
        print("No meraki/CPN adapter loaded")
        return
    print(f"Adapter: {mx_adapter.instance_id}  type={type(mx_adapter).__name__}")
    devs = await mx_adapter.list_devices()
    for d in devs:
        if d.name == "cpn-ful-mx1":
            print(f"\nNormalizedDevice for cpn-ful-mx1:")
            print(f"  name        = {d.name!r}")
            print(f"  platform    = {d.platform!r}")
            print(f"  platform_id = {d.platform_id!r}")
            print(f"  role        = {d.role!r}")
            print(f"  mgmt_ip     = {d.mgmt_ip!r}    ← THE KEY VALUE")
            print(f"  platform_metadata:")
            for k, v in d.platform_metadata.items():
                if isinstance(v, list) and len(v) > 4:
                    v = f"[{len(v)} items: {v[:3]}...]"
                print(f"    {k:<18} = {v!r}")
            break
    else:
        print("cpn-ful-mx1 not in device list")


asyncio.run(main())
