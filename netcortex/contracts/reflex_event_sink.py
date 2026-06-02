"""``ReflexEventSink`` Protocol — persistence target for reflex outcomes.

A reflex handler returns a :class:`~netcortex.reflex.protocol.ReflexOutcome`.
The runner then needs to write that outcome somewhere durable so operators
can query reflex history, dashboards can show recent firings, and the
consolidation cycle (0.10.0) can fold reflexes into semantic memory.

This Protocol abstracts the destination. The default in-cluster sink writes
``:ReflexEvent`` nodes to Neo4j (the brain's episodic-memory layer). Tests
use an in-memory list. Future sinks may include:

* NetBox journal entries (mirror the outcome on the affected entity)
* OpenSearch / Splunk (so SOC teams can correlate with their existing tools)
* a fan-out sink that writes to several at once

The Protocol is intentionally narrow — one ``record`` method — so that
the runner can be passed any of those without knowing which backend is
underneath. Adding a new sink means writing a class that satisfies this
Protocol and running it through the contract test suite.

Idempotency
-----------
``record`` is expected to be **at-least-once safe**. A reflex outcome
may arrive at the sink more than once if the runner retries after a
transient sink failure; the sink MUST NOT double-count. Sinks achieve
this by deriving a stable id from the outcome (handler + subject +
occurred_at is the canonical recipe) and MERGE-ing (or upserting) on it.

The Protocol does not surface a query interface. Reading reflex history
back from the sink is the operator UI's job, and it talks to each backend
directly because the query languages differ too much to be usefully
abstracted at this layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from netcortex.reflex.protocol import ReflexOutcome


@runtime_checkable
class ReflexEventSink(Protocol):
    """Minimum surface every reflex outcome sink must implement.

    Sinks are constructed once at process startup and reused for the
    lifetime of the :class:`~netcortex.reflex.runner.ReflexRunner` that
    owns them. They MUST be safe to call concurrently from multiple
    handler tasks — internal locking, batched writes, or async queues
    are an implementation detail.
    """

    async def record(self, outcome: ReflexOutcome) -> None:
        """Persist one reflex outcome.

        MUST be idempotent: calling ``record`` twice with the same outcome
        (same handler, subject, occurred_at) MUST NOT create two entries.

        MUST NOT raise on transient backend failures — the sink owns its
        own retry / dead-letter policy. Raising propagates the exception
        into the runner's dispatch wrapper and degrades to an ``errored``
        outcome on the *next* event, which is not useful operator signal.
        Implementations should log at WARNING and drop, or buffer.

        Raising is reserved for *programmer* errors (malformed outcome,
        misconfigured sink) — the kinds of bugs you want a loud crash for.
        """
        ...

    async def close(self) -> None:
        """Flush pending writes and release backend resources. Idempotent."""
        ...


__all__ = ["ReflexEventSink"]
