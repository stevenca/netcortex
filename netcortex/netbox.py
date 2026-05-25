"""NetBox client wrapper using pynetbox."""

from __future__ import annotations

import httpx
import pynetbox
import structlog

log = structlog.get_logger(__name__)

_client: pynetbox.api | None = None


def get_client() -> pynetbox.api:
    """Return the singleton pynetbox client. Raises if not initialised."""
    if _client is None:
        raise RuntimeError("NetBox client not initialised — call init_client() at startup.")
    return _client


async def init_client(url: str, token: str) -> pynetbox.api:
    """Initialise the pynetbox client and verify connectivity."""
    global _client
    nb = pynetbox.api(url, token=token)
    # pynetbox is sync; use httpx for the connectivity probe
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/",
                headers={"Authorization": f"Token {token}"},
            )
            resp.raise_for_status()
    except Exception as exc:
        raise ConnectionError(f"Cannot reach NetBox at {url}: {exc}") from exc
    _client = nb
    log.info("netbox.connected", url=url)
    return _client


async def check_connectivity(url: str, token: str) -> dict:
    """Return a health dict without raising."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/",
                headers={"Authorization": f"Token {token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "connected",
                "netbox_version": data.get("netbox-version", "unknown"),
            }
    except httpx.HTTPStatusError as exc:
        return {"status": "error", "message": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
