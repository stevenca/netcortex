"""``EventBus`` Protocol — the thalamus contract.

The event bus is the single nervous system of NetCortex. Every sensory event
(poll, webhook, SNMP trap, streaming telemetry) is normalized and published
here; every downstream consumer (reflex, association, conductor, subscription
bridge) reads from here.

The Protocol is intentionally small. Backends we expect to implement it:

* ``NatsEventBus`` — the production default (NATS JetStream).
* ``InMemoryEventBus`` — for tests and single-process developer runs.
* ``RedisEventBus``, ``KafkaEventBus`` — possible alternatives if operational
  constraints make NATS unworkable in a given deployment.

The contract test suite at ``tests/contracts/event_bus/`` runs identically
against all of them.

Subject naming convention
-------------------------
Subjects are dot-separated and hierarchical. They mirror the brain regions:

    sensory.<modality>.<source>.<event_type>
    reflex.<handler>.<outcome>
    motor.<target>.<action>.<outcome>

Subscribers may use wildcards: ``sensory.>`` matches every sensory subject;
``sensory.meraki.>`` matches only Meraki-sourced events. Wildcard semantics
follow the NATS convention so the in-memory implementation behaves the same
as production.

Delivery semantics
------------------
At-least-once delivery within a subscription session. A subscriber that joins
after a publish does NOT see prior events (this is not replay — for replay,
read from episodic memory). Implementations MAY offer durable consumers as
an opt-in extension, but the Protocol itself stays at-least-once for
portability.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class EventBusValidationError(ValueError):
    """Raised when publish() is called with a malformed subject or payload."""


@dataclass(frozen=True)
class EventMessage:
    """A single event traversing the bus.

    Subject and payload are required. ``headers`` carry framing metadata
    (correlation id, source adapter, schema version, etc.) and are passed
    through to subscribers verbatim.

    ``ts`` is the publish timestamp; clock is the bus's monotonic clock, not
    the subscriber's. Subscribers that need wall-clock authoritativeness
    should consult the payload's own ``occurred_at`` field.
    """

    subject: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    ts: datetime | None = None


@runtime_checkable
class EventBus(Protocol):
    """Minimum surface every event bus implementation must support."""

    async def publish(self, subject: str, payload: dict[str, Any], *,
                      headers: dict[str, str] | None = None) -> None:
        """Publish ``payload`` to ``subject``.

        ``subject`` must be a non-empty, dot-separated string. Implementations
        may further validate subject syntax and raise
        :class:`EventBusValidationError` on rejection. Wildcards (``*``,
        ``>``) MUST NOT appear in a publish subject.

        ``payload`` is a JSON-serializable mapping. Implementations may
        enforce additional schema constraints; reject with
        :class:`EventBusValidationError` rather than silently truncating.

        Returns when the publish is accepted by the bus. Does not wait for
        subscriber delivery.
        """
        ...

    def subscribe(self, pattern: str) -> AsyncIterator[EventMessage]:
        """Subscribe to all messages matching ``pattern``.

        ``pattern`` follows NATS-style wildcards (``*`` matches one token,
        ``>`` matches one-or-more tokens). Returns an async iterator that
        yields :class:`EventMessage` instances until the subscription is
        closed via the iterator's ``aclose()``.

        Subscriptions are independent: closing one does not affect others.
        A slow subscriber MUST NOT block other subscribers (implementations
        provide per-subscription buffering or drop policies).
        """
        ...

    async def close(self) -> None:
        """Release all bus resources. Idempotent."""
        ...
