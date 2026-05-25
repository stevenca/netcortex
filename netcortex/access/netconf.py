"""NETCONF access (RFC 6241) via ncclient."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
import xmltodict
from ncclient import manager, NCClientError
from ncclient.xml_ import to_ele

log = structlog.get_logger(__name__)


@dataclass
class NetconfResult:
    device: str
    operation: str
    data: dict | str           # dict if format="dict", raw XML string if format="xml"
    raw_xml: str


class NetconfError(Exception):
    pass


def _connect_and_run(
    host: str,
    username: str,
    password: str,
    operation: str,
    port: int,
    source: str,
    filter_type: str | None,
    filter_value: str | None,
    config_xml: str | None,
    target: str,
    commit: bool,
) -> tuple[str, str]:
    """Synchronous ncclient operations — runs in a thread pool executor."""
    mgr_kwargs = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "hostkey_verify": False,  # TODO: integrate with known_hosts
        "device_params": {"name": "default"},
    }

    with manager.connect(**mgr_kwargs) as mgr:
        if operation in ("get-config", "get"):
            nc_filter = None
            if filter_type and filter_value:
                nc_filter = (filter_type, filter_value)

            if operation == "get-config":
                result = mgr.get_config(source=source, filter=nc_filter)
            else:
                result = mgr.get(filter=nc_filter)

            capabilities = list(mgr.server_capabilities)
            return result.xml, "\n".join(capabilities)

        elif operation == "edit-config":
            if not config_xml:
                raise NetconfError("config_xml is required for edit-config")
            mgr.edit_config(target=target, config=to_ele(config_xml))
            if commit and target == "candidate":
                mgr.commit()
            return "<ok/>", ""

        elif operation == "get-schema":
            raise NotImplementedError("get-schema implemented separately")

        else:
            raise NetconfError(f"Unknown NETCONF operation: {operation}")


async def get_config(
    host: str,
    username: str,
    password: str,
    source: str = "running",
    filter_type: str | None = None,
    filter_value: str | None = None,
    port: int = 830,
    output_format: str = "dict",
) -> NetconfResult:
    """Retrieve configuration datastore via NETCONF get-config."""
    try:
        raw_xml, _ = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _connect_and_run(
                host, username, password, "get-config", port,
                source, filter_type, filter_value, None, "running", False,
            ),
        )
        data = xmltodict.parse(raw_xml) if output_format == "dict" else raw_xml
        return NetconfResult(device=host, operation="get-config", data=data, raw_xml=raw_xml)
    except NCClientError as exc:
        raise NetconfError(f"NETCONF get-config on {host} failed: {exc}") from exc


async def get_state(
    host: str,
    username: str,
    password: str,
    filter_type: str | None = None,
    filter_value: str | None = None,
    port: int = 830,
    output_format: str = "dict",
) -> NetconfResult:
    """Retrieve operational state via NETCONF get."""
    try:
        raw_xml, _ = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _connect_and_run(
                host, username, password, "get", port,
                "running", filter_type, filter_value, None, "running", False,
            ),
        )
        data = xmltodict.parse(raw_xml) if output_format == "dict" else raw_xml
        return NetconfResult(device=host, operation="get", data=data, raw_xml=raw_xml)
    except NCClientError as exc:
        raise NetconfError(f"NETCONF get on {host} failed: {exc}") from exc


async def edit_config(
    host: str,
    username: str,
    password: str,
    config_xml: str,
    target: str = "candidate",
    commit: bool = True,
    port: int = 830,
) -> NetconfResult:
    """Push configuration via NETCONF edit-config."""
    try:
        raw_xml, _ = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _connect_and_run(
                host, username, password, "edit-config", port,
                "running", None, None, config_xml, target, commit,
            ),
        )
        return NetconfResult(device=host, operation="edit-config", data={}, raw_xml=raw_xml)
    except NCClientError as exc:
        raise NetconfError(f"NETCONF edit-config on {host} failed: {exc}") from exc
