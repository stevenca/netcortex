"""CLI access via SSH/Telnet using Netmiko."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

log = structlog.get_logger(__name__)

# Maps NetBox platform.slug → Netmiko device_type
PLATFORM_DRIVER_MAP: dict[str, str] = {
    "ios": "cisco_ios",
    "ios-xe": "cisco_ios",
    "ios-xr": "cisco_xr",
    "nxos": "cisco_nxos",
    "nxos-ssh": "cisco_nxos_ssh",
    "eos": "arista_eos",
    "junos": "juniper_junos",
    "asa": "cisco_asa",
    "ftd": "cisco_ftd",
    "panos": "paloalto_panos",
    "f5-tmsh": "f5_tmsh",
    "linux": "linux",
}


@dataclass
class CLIResult:
    device: str
    command: str
    raw: str
    parsed: list[dict] | None
    template_used: str | None
    elapsed_ms: int
    access_method: str = "ssh"
    error: str | None = None


class CLIAccessError(Exception):
    pass


async def run_command(
    host: str,
    username: str,
    password: str,
    command: str,
    platform_slug: str,
    secret: str = "",
    port: int = 22,
    timeout: int = 30,
    use_textfsm: bool = True,
) -> CLIResult:
    """Run a single CLI command on a device via SSH. Returns raw and optionally parsed output."""
    import time

    device_type = PLATFORM_DRIVER_MAP.get(platform_slug, "autodetect")
    connection_params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "secret": secret,
        "port": port,
        "timeout": timeout,
        "session_timeout": timeout,
    }

    start = time.monotonic()
    try:
        raw, template_used = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _run_sync(connection_params, command, use_textfsm),
        )
        elapsed = int((time.monotonic() - start) * 1000)
        parsed, tpl = _attempt_parse(raw, command, device_type) if use_textfsm else (None, None)
        return CLIResult(
            device=host,
            command=command,
            raw=raw,
            parsed=parsed,
            template_used=tpl,
            elapsed_ms=elapsed,
        )
    except NetmikoAuthenticationException as exc:
        raise CLIAccessError(f"Authentication failed for {host}: {exc}") from exc
    except NetmikoTimeoutException as exc:
        raise CLIAccessError(f"Connection timed out for {host}: {exc}") from exc
    except Exception as exc:
        raise CLIAccessError(f"CLI error on {host}: {exc}") from exc


def _run_sync(params: dict, command: str, use_textfsm: bool) -> tuple[str, str | None]:
    with ConnectHandler(**params) as conn:
        output = conn.send_command(command, use_textfsm=False)
    return output, None


def _attempt_parse(raw: str, command: str, device_type: str) -> tuple[list[dict] | None, str | None]:
    """Try TextFSM parsing via ntc-templates. Returns (parsed, template_name) or (None, None)."""
    try:
        from ntc_templates.parse import parse_output
        result = parse_output(platform=device_type, command=command, data=raw)
        if result:
            template_name = f"{device_type}_{command.replace(' ', '_')}.textfsm"
            return result, template_name
    except Exception:
        pass
    return None, None
