"""RESTCONF access (RFC 8040) via httpx."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

RESTCONF_ROOT = "/restconf"
YANG_LIBRARY_PATH = "/restconf/data/ietf-yang-library:modules-state"
HEADERS = {
    "Accept": "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
}


@dataclass
class RestconfResult:
    device: str
    path: str
    datastore: str
    data: dict
    status_code: int


class RestconfError(Exception):
    pass


async def get(
    host: str,
    path: str,
    username: str,
    password: str,
    datastore: str = "running",
    port: int = 443,
    verify_ssl: bool = True,
) -> RestconfResult:
    """Fetch a YANG path from a device via RESTCONF GET."""
    datastore_segment = "" if datastore == "operational" else f"ds/ietf-datastores:{datastore}/"
    url = f"https://{host}:{port}{RESTCONF_ROOT}/data/{datastore_segment}{path}"

    async with httpx.AsyncClient(verify=verify_ssl, timeout=15) as client:
        try:
            resp = await client.get(url, headers=HEADERS, auth=(username, password))
            resp.raise_for_status()
            return RestconfResult(
                device=host,
                path=path,
                datastore=datastore,
                data=resp.json(),
                status_code=resp.status_code,
            )
        except httpx.HTTPStatusError as exc:
            raise RestconfError(
                f"RESTCONF GET {path} on {host} failed: HTTP {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            raise RestconfError(f"RESTCONF error on {host}: {exc}") from exc


async def put(
    host: str,
    path: str,
    data: dict,
    username: str,
    password: str,
    method: str = "PUT",
    port: int = 443,
    verify_ssl: bool = True,
) -> RestconfResult:
    """Push configuration to a device via RESTCONF PUT/PATCH/POST."""
    url = f"https://{host}:{port}{RESTCONF_ROOT}/data/{path}"
    method = method.upper()
    if method not in {"PUT", "PATCH", "POST"}:
        raise RestconfError(f"Invalid RESTCONF method: {method}")

    async with httpx.AsyncClient(verify=verify_ssl, timeout=15) as client:
        try:
            resp = await client.request(
                method, url, headers=HEADERS, json=data, auth=(username, password)
            )
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
            return RestconfResult(
                device=host,
                path=path,
                datastore="running",
                data=body,
                status_code=resp.status_code,
            )
        except httpx.HTTPStatusError as exc:
            raise RestconfError(
                f"RESTCONF {method} {path} on {host} failed: HTTP {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            raise RestconfError(f"RESTCONF error on {host}: {exc}") from exc


async def get_yang_capabilities(
    host: str,
    username: str,
    password: str,
    port: int = 443,
    verify_ssl: bool = True,
) -> list[dict]:
    """Discover YANG modules supported by the device via ietf-yang-library."""
    url = f"https://{host}:{port}{YANG_LIBRARY_PATH}"
    async with httpx.AsyncClient(verify=verify_ssl, timeout=10) as client:
        try:
            resp = await client.get(url, headers=HEADERS, auth=(username, password))
            resp.raise_for_status()
            data = resp.json()
            modules = (
                data.get("ietf-yang-library:modules-state", {})
                .get("module", [])
            )
            return modules
        except Exception:
            return []
