"""Throw-away helper to compare LLDP/CDP/sysName visibility across devices.

Run from inside the worker container:

    docker exec netcortex-worker python /app/tools/_probe_lldp.py
"""
from __future__ import annotations

import asyncio
import subprocess

from netcortex.config import init_settings
from netcortex.secrets import get_secret_backend
from netcortex.snmp.credentials import SnmpCredentialResolver, SnmpContext


HOSTS = [
    ("ash-cat9k1", "192.133.176.130"),
    ("ash-cat8k1", "192.133.176.129"),
    ("ash-cat8k2", "192.133.176.150"),
    ("ful-cat9k1", "192.133.161.131"),
    ("ful-n9k1",   "192.133.161.130"),
]
OIDS = [
    ("LLDP-LOC-SYSNAME", "1.0.8802.1.1.2.1.3.3.0"),
    ("LLDP-REM-SYSNAME", "1.0.8802.1.1.2.1.4.1.1.9"),
    ("LLDP-STATS-RX",    "1.0.8802.1.1.2.1.2.7.1.5"),
    ("CDP-RUNNING",      "1.3.6.1.4.1.9.9.23.1.3.1.0"),
    ("CDP-NEIGHBOR-NAME","1.3.6.1.4.1.9.9.23.1.2.1.1.6"),
    ("SYS-NAME",         "1.3.6.1.2.1.1.5.0"),
]


async def main() -> None:
    await init_settings()
    backend = get_secret_backend()
    resolver = SnmpCredentialResolver(backend)
    cred = await resolver.resolve(
        device_name="cpn-ash-cat9k1",
        source_adapter="snmp/default",
        context=SnmpContext.DEVICE,
    )
    if cred is None or not hasattr(cred, "username"):
        print(f"No usable v3 credential: {cred!r}")
        return

    user = cred.username
    auth_proto = cred.auth_protocol.upper()
    priv_proto = "AES" if cred.priv_protocol.upper().startswith("AES") else "DES"

    for host_label, host in HOSTS:
        print(f"\n=== {host_label} ({host}) ===")
        for oid_label, oid in OIDS:
            cmd = [
                "snmpwalk", "-v3", "-l", "authPriv",
                "-u", user,
                "-a", auth_proto, "-A", cred.auth_password,
                "-x", priv_proto, "-X", cred.priv_password,
                "-On", "-t", "5", "-r", "1",
                host, oid,
            ]
            try:
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15
                )
            except subprocess.TimeoutExpired:
                print(f"  {oid_label:18} TIMEOUT")
                continue

            stdout = res.stdout.strip().splitlines()
            stderr = res.stderr.strip().splitlines()
            first_stdout = stdout[0][:100] if stdout else ""
            last_stderr = stderr[-1][:120] if stderr else ""
            print(
                f"  {oid_label:18} rc={res.returncode} "
                f"rows={len(stdout):3}  "
                f"first={first_stdout!r}  err={last_stderr!r}"
            )


if __name__ == "__main__":
    asyncio.run(main())
