"""Neo4j async driver singleton.

Usage:
    from netcortex.graph.client import init_client, get_driver, check_connectivity

    await init_client(uri, user, password)
    driver = get_driver()
    result = await check_connectivity()
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

_driver: Any | None = None  # neo4j.AsyncDriver


def get_driver() -> Any:
    """Return the active Neo4j driver. Raises RuntimeError if not initialised."""
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialised — call init_client() first")
    return _driver


async def init_client(uri: str, user: str, password: str) -> None:
    """Create and verify the global Neo4j async driver."""
    global _driver
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError as exc:
        raise RuntimeError(
            "neo4j driver not installed. Add 'neo4j>=5.0' to dependencies."
        ) from exc

    if _driver is not None:
        await _driver.close()

    _driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    # Verify connectivity immediately
    await _driver.verify_connectivity()
    log.info("neo4j.connected", uri=uri)


async def check_connectivity(uri: str, user: str, password: str) -> dict:
    """Probe Neo4j without storing a driver; used for health checks."""
    try:
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        try:
            await driver.verify_connectivity()
            async with driver.session() as session:
                result = await session.run("CALL dbms.components() YIELD versions RETURN versions[0] AS version")
                record = await result.single()
                version = record["version"] if record else "unknown"
            return {"status": "connected", "neo4j_version": version}
        finally:
            await driver.close()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def close() -> None:
    """Close the driver on shutdown."""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
        log.info("neo4j.disconnected")
