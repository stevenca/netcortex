"""Web-process sensory publisher coordination.

The web process needs to publish ``sensory.*`` events when webhooks
arrive from upstream platforms (Meraki, ThousandEyes, Nexus Dashboard,
cdFMC, …). The worker process owns the :class:`ReflexRunner` that
*consumes* those events; web only ever publishes. Keeping the two
responsibilities split means:

* the web process stays a thin HTTP front (no dedup state, no Neo4j
  sink, no handler scheduling — those all live in worker)
* webhook latency stays low: publish, return 200, done. The reflex
  pipeline runs asynchronously on the worker subscription side.
* a worker outage does not lose webhook events — NATS holds them until
  the worker reconnects (or, with JetStream later, persists them durably)

Design
------
One :class:`NatsEventBus` per process, cached on ``app.state.event_bus``.
One :class:`SensoryPublisher` per source token, lazily created and
cached on ``app.state._sensory_publishers``. A typo in a source name
fails at the first call (validated against ``SENSORY_SOURCES``) rather
than silently producing unsubscribable subjects.

If ``NATS_URL`` is not configured the bus is ``None`` and
:func:`get_publisher` returns ``None``. Routes that try to publish in
that case should degrade silently (the existing sync-trigger behavior
remains intact — losing the sensory side is a recoverable annoyance,
not a data-loss bug). This mirrors the worker's
``_start_reflex_pipeline`` behavior from 0.8.0-dev5.

Shutdown semantics
------------------
:func:`close_event_bus` flushes pending publishes and closes the NATS
connection. Called from ``main.py``'s lifespan teardown. Idempotent.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

from netcortex.contracts.subjects import SENSORY_SOURCES

if TYPE_CHECKING:
    from fastapi import FastAPI
    from netcortex.thalamus import NatsEventBus, SensoryPublisher

log = structlog.get_logger(__name__)


async def init_event_bus(app: "FastAPI", nats_url: str | None = None) -> "NatsEventBus | None":
    """Initialize the web process's NATS publisher bus.

    Resolves ``nats_url`` in this order:
      1. explicit ``nats_url`` argument (test injection)
      2. ``NATS_URL`` environment variable (Helm-provided)

    Stores the bus on ``app.state.event_bus`` so route handlers and the
    teardown path can reach it. Returns the bus (or ``None`` if NATS is
    not configured / unreachable).

    Failure here logs and returns ``None`` rather than raising —
    the web process must boot even when NATS is down. The reflex/sensory
    feature degrades; the rest of the API keeps working.
    """
    resolved = nats_url or os.environ.get("NATS_URL", "")
    if not resolved:
        log.info("web.sensory_publisher_disabled", reason="NATS_URL not set")
        app.state.event_bus = None
        app.state._sensory_publishers = {}
        return None
    try:
        from netcortex.thalamus import NatsEventBus
        bus = NatsEventBus(resolved)
        app.state.event_bus = bus
        app.state._sensory_publishers = {}
        log.info("web.sensory_publisher_ready", nats_url=resolved)
        return bus
    except Exception as exc:
        log.warning("web.sensory_publisher_init_failed", error=str(exc))
        app.state.event_bus = None
        app.state._sensory_publishers = {}
        return None


def get_publisher(app: Any, source: str) -> "SensoryPublisher | None":
    """Return a cached :class:`SensoryPublisher` for ``source``, or None.

    Cheap to call per-request: the first call for a given source builds
    the publisher (which validates ``source`` against ``SENSORY_SOURCES``
    at construction); subsequent calls return the cached instance.

    Returns ``None`` when the bus isn't initialized — callers should
    handle that as "publishing disabled, fall through to other side-
    effects" rather than as an error condition.

    ``source`` is validated against the closed vocabulary even if the
    bus is unavailable, so a typo in a webhook route still fails fast
    in unit tests (we don't want "all webhook routes silently skip
    publishing because of a typo nobody noticed").
    """
    if source not in SENSORY_SOURCES:
        raise ValueError(
            f"unknown sensory source {source!r}; "
            f"add to SENSORY_SOURCES in netcortex.contracts.subjects "
            f"and update docs/architecture/subjects.md"
        )
    bus = getattr(app.state, "event_bus", None)
    if bus is None:
        return None
    publishers: dict[str, Any] = getattr(app.state, "_sensory_publishers", None) or {}
    if source not in publishers:
        from netcortex.thalamus import SensoryPublisher
        publishers[source] = SensoryPublisher(bus, source=source)
        app.state._sensory_publishers = publishers
    return publishers[source]


async def close_event_bus(app: Any) -> None:
    """Flush and close the publisher bus. Safe to call multiple times.

    Called from the FastAPI lifespan teardown. Errors are logged and
    swallowed — a connection that already died will fail to close
    cleanly, and that should not block the shutdown sequence.
    """
    bus = getattr(app.state, "event_bus", None)
    if bus is None:
        return
    try:
        await bus.close()
        log.info("web.sensory_publisher_closed")
    except Exception as exc:
        log.warning("web.sensory_publisher_close_failed", error=str(exc))
    finally:
        app.state.event_bus = None
        app.state._sensory_publishers = {}


__all__ = ["init_event_bus", "get_publisher", "close_event_bus"]
