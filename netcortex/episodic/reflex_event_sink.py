"""Concrete :class:`ReflexEventSink` implementations.

Two ship in 0.8.0-dev5:

* :class:`InMemoryReflexEventSink` — used by tests and any one-off script
  that wants reflex outcomes captured but doesn't want a Neo4j dependency.
* :class:`Neo4jReflexEventSink` — production default. Writes ``:ReflexEvent``
  nodes and optional ``:AFFECTS`` edges into the cluster Neo4j.

Both satisfy :class:`netcortex.contracts.reflex_event_sink.ReflexEventSink`
and run through the same contract test suite at
``tests/contracts/reflex_event_sink/``.

Why a separate ``ReflexEvent`` label
------------------------------------
Reflex events are observations about state changes, not state. Keeping
them on their own label avoids polluting the existing
Device/Interface/IPAddress graph with millions of historical nodes and
makes time-windowed queries trivial:

    MATCH (e:ReflexEvent)
    WHERE e.observed_at >= timestamp() - 3600000
    RETURN e ORDER BY e.observed_at DESC
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from netcortex.reflex.protocol import ReflexOutcome

_LOG = logging.getLogger(__name__)


def _stable_outcome_id(outcome: ReflexOutcome) -> str:
    """Derive a stable id for at-least-once-safe MERGEs.

    Two outcomes that came from the same handler, the same subject, and
    the same wall-clock observation MUST hash to the same id so the
    sink's MERGE collapses duplicates. We deliberately do NOT include
    ``payload`` or ``diagnostic`` — those may legitimately change between
    a retry and the original attempt (e.g. a slightly different cap of
    upstream_keys), and including them would break idempotency.
    """
    basis = "|".join([
        outcome.handler,
        outcome.subject,
        outcome.target or "",
        outcome.occurred_at.isoformat(),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class InMemoryReflexEventSink:
    """Append-only in-memory sink. Tests use this exclusively.

    Idempotency is enforced by deduplicating on the stable outcome id —
    a retry of the same outcome is silently absorbed, exactly like the
    Neo4j sink's MERGE.

    Threadsafe across asyncio tasks via an internal :class:`asyncio.Lock`;
    safe to share between handler tasks without external coordination.
    """

    def __init__(self) -> None:
        self._records: dict[str, ReflexOutcome] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()
        self._closed = False

    async def record(self, outcome: ReflexOutcome) -> None:
        if self._closed:
            raise RuntimeError("InMemoryReflexEventSink is closed")
        async with self._lock:
            key = _stable_outcome_id(outcome)
            if key in self._records:
                # Idempotent — silently absorb the retry.
                return
            self._records[key] = outcome
            self._order.append(key)

    async def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Test introspection (not part of the Protocol)
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._records)

    @property
    def outcomes(self) -> list[ReflexOutcome]:
        """Outcomes in insertion order. For test assertions only."""
        return [self._records[k] for k in self._order]

    def clear(self) -> None:
        """Reset to a fresh state. For test teardown only."""
        self._records.clear()
        self._order.clear()


class Neo4jReflexEventSink:
    """Production sink. Writes ``:ReflexEvent`` nodes via the shared driver.

    The sink does NOT own the driver — it takes a driver factory so
    that the singleton in :mod:`netcortex.graph.client` stays the
    one-true-driver for the process. Construction is cheap and safe to
    do at startup before the driver is initialised; the first
    :meth:`record` call lazily resolves the driver.

    Schema
    ------
    ::

        (:ReflexEvent {
          id                : <sha256-derived stable id>
          handler           : 'link_down' | 'bgp_drop' | 'security_alert' | ...
          subject           : 'sensory.link_down.snmp_poll.<target>'
          target            : '<device>|<interface>' or null
          event_class       : 'link_down' (parsed from subject)
          source            : 'snmp_poll' (parsed from subject)
          severity          : 'info' | 'warn' | 'high' | 'critical'
          outcome           : 'logged' | 'applied' | 'skipped' | 'errored'
          rationale         : '<<=200 char operator-facing text>>'
          observed_at       : timestamp in epoch milliseconds (UTC)
          recorded_at       : timestamp in epoch milliseconds (UTC; sink wall-clock)
          payload_json      : JSON-encoded `payload` for forensic queries
          diagnostic_json   : JSON-encoded `diagnostic` for debugging
        })

    The ``id`` is the MERGE key; subsequent ``record`` calls with the
    same outcome are no-ops. ``recorded_at`` is set on first insert and
    preserved on retries (we use ``ON CREATE SET``).

    AFFECTS edges
    -------------
    When the outcome's ``target`` parses cleanly to ``<device>|<interface>``
    AND we can find a matching ``:Device`` node by ``name``, we draw a
    ``(:ReflexEvent)-[:AFFECTS]->(:Device)`` edge. We deliberately do
    NOT block writes when the target can't be resolved — episodic events
    are valuable even without a graph anchor (an interface name we don't
    yet know about is a hint that ingest is behind reflex, which is the
    correct ordering for unfamiliar devices).

    Retention is not implemented in 0.8.0-dev5. A future housekeeping
    task will purge events older than a configurable retention window.
    """

    def __init__(
        self,
        driver_factory: Any | None = None,
    ) -> None:
        """Construct the sink.

        Parameters
        ----------
        driver_factory:
            A zero-arg callable that returns a configured neo4j async
            driver. Defaults to :func:`netcortex.graph.client.get_driver`,
            which is what production wants. Tests that want to use this
            class against a real Neo4j (rare) can pass their own factory.
        """
        if driver_factory is None:
            from netcortex.graph.client import get_driver as _default_factory
            driver_factory = _default_factory
        self._driver_factory = driver_factory
        self._closed = False

    async def record(self, outcome: ReflexOutcome) -> None:
        if self._closed:
            raise RuntimeError("Neo4jReflexEventSink is closed")
        try:
            driver = self._driver_factory()
        except Exception as exc:
            # Driver not ready yet (worker still in startup) — log and
            # drop. Reflex outcomes are not so precious that we should
            # block the handler waiting for Neo4j to come up; the next
            # event will succeed.
            _LOG.warning(
                "reflex_event_sink.driver_unavailable handler=%s error=%s",
                outcome.handler, exc,
            )
            return

        event_class, source = _parse_subject_class_source(outcome.subject)
        props: dict[str, Any] = {
            "id": _stable_outcome_id(outcome),
            "handler": outcome.handler,
            "subject": outcome.subject,
            "target": outcome.target,
            "event_class": event_class,
            "source": source,
            "severity": outcome.severity,
            "outcome": outcome.outcome,
            "rationale": (outcome.rationale or "")[:512],
            # Cypher's `timestamp()` is epoch ms; we match that wire shape
            # for `observed_at` (handler-supplied) and `recorded_at`
            # (sink wall-clock) so time-range queries are arithmetic, not
            # string parsing.
            "observed_at_ms": int(outcome.occurred_at.timestamp() * 1000),
            "payload_json": _json_safe(outcome.payload),
            "diagnostic_json": _json_safe(outcome.diagnostic),
        }
        # Targets of the form `<device>|<interface>` get an extra hint
        # so the AFFECTS resolver can find the Device node by name.
        device_name = _device_name_from_target(outcome.target)

        try:
            async with driver.session() as session:
                await session.execute_write(
                    _merge_reflex_event_tx, props, device_name
                )
        except Exception as exc:
            # Persistence failure — log loudly but don't propagate.
            # See Protocol docstring: sink failures should not become
            # handler-visible errors that change the outcome of the next
            # event in the loop.
            _LOG.warning(
                "reflex_event_sink.write_failed handler=%s subject=%s error=%s",
                outcome.handler, outcome.subject, exc,
            )

    async def close(self) -> None:
        # We don't own the driver — the worker's lifespan does. Just flag
        # ourselves closed so subsequent record() calls raise.
        self._closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_subject_class_source(subject: str) -> tuple[str | None, str | None]:
    """Pull out event_class + source from a `sensory.<class>.<source>.<...>` subject.

    Robust to non-sensory subjects (returns (None, None)) so reflex outcomes
    that someday come from `motor.*` or `reflex.*` subjects still persist.
    """
    parts = subject.split(".")
    if len(parts) >= 4 and parts[0] == "sensory":
        return parts[1], parts[2]
    return None, None


def _device_name_from_target(target: str | None) -> str | None:
    """Extract the device-name portion of a `<device>|<interface>` target.

    Returns ``None`` for targets that aren't compound or are empty so the
    AFFECTS resolver knows to skip the edge.
    """
    if not target or "|" not in target:
        return None
    head = target.split("|", 1)[0].strip()
    return head or None


def _json_safe(value: Any) -> str:
    """JSON-serialize an arbitrary dict for storage. Lossy-but-stable on weird types."""
    with contextlib.suppress(TypeError, ValueError):
        return json.dumps(value, default=str, sort_keys=True)
    return json.dumps({"_unserialisable": str(type(value))})


async def _merge_reflex_event_tx(tx: Any, props: dict[str, Any], device_name: str | None) -> None:
    """Write the :ReflexEvent node and optional :AFFECTS edge in one transaction.

    The MERGE on `id` is what enforces at-least-once safety: a retry of the
    same outcome lands on the same node and just refreshes `recorded_at` /
    the property bag without creating a duplicate.

    ON CREATE preserves `recorded_at` on the original write so we can later
    distinguish "when was this first recorded" from "when was this last
    touched" (e.g., for replays that update the property bag with new
    diagnostic info but should not pretend the event is fresh).

    Note: we pass the merge key (``id``) as a separate parameter rather
    than relying on map-property access inside the MERGE pattern, since
    that syntax has historically varied across Cypher versions. The
    full property bag still goes through ``$props`` for the SET.
    """
    event_id = props["id"]
    await tx.run(
        """
        MERGE (e:ReflexEvent {id: $event_id})
        ON CREATE SET
            e.recorded_at = timestamp(),
            e += $props
        ON MATCH SET
            e += $props
        """,
        event_id=event_id,
        props=props,
    )
    if device_name:
        # Best-effort AFFECTS edge. We MATCH-only (not MERGE) the device
        # so we don't create stub nodes from a typo'd device name in a
        # reflex event — the live state graph stays canonical.
        await tx.run(
            """
            MATCH (e:ReflexEvent {id: $event_id})
            MATCH (d:Device) WHERE d.name = $device_name OR d.platform_id = $device_name
            MERGE (e)-[:AFFECTS]->(d)
            """,
            event_id=event_id,
            device_name=device_name,
        )


__all__ = [
    "InMemoryReflexEventSink",
    "Neo4jReflexEventSink",
]
