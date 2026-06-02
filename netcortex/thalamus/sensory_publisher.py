"""``SensoryPublisher`` — thin convenience wrapper over the :class:`EventBus`.

The bus Protocol is intentionally minimal (publish/subscribe/close). Every
adapter that needs to emit sensory events ends up wanting the same handful
of conveniences on top:

* build the subject from ``(event_class, source, *target_parts)`` using
  the validated :func:`sensory_subject` helper, so a typo in a class name
  fails at startup not at runtime
* tag the payload with ``recorded_at`` and ``source`` for downstream
  fusion / dedup without each publisher reinventing the same code
* emit a structured ``bus.published`` log line so an operator can see the
  full event flow without a debugger
* swallow-and-log errors so a transient NATS hiccup doesn't crash the
  poll loop that produced the event (the adapter still writes its
  observation to the graph — losing the corresponding sensory event is
  a recoverable annoyance, not a data-loss bug)

The class is intentionally *not* a Protocol — it's a concrete utility
that depends on the :class:`EventBus` Protocol underneath. Tests that
want to assert "this code path published these events" inject an
:class:`InMemoryEventBus` and read its tap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from netcortex.contracts.event_bus import EventBus
from netcortex.contracts.subjects import SENSORY_SOURCES, sensory_subject

_LOG = logging.getLogger(__name__)


class SensoryPublisher:
    """Adapter-friendly facade over an :class:`EventBus`.

    Parameters
    ----------
    bus:
        The bus to publish through. The publisher does not own the bus.
    source:
        One of :data:`SENSORY_SOURCES`. Validated at construction so a
        typo in an adapter's wiring fails on import, not on first
        link-state change at 3am.
    """

    def __init__(self, bus: EventBus, *, source: str) -> None:
        if source not in SENSORY_SOURCES:
            raise ValueError(
                f"unknown sensory source {source!r}; "
                f"add to SENSORY_SOURCES in netcortex.contracts.subjects"
            )
        self._bus = bus
        self._source = source

    @property
    def source(self) -> str:
        """The wired source token (e.g. ``'snmp_poll'``)."""
        return self._source

    async def publish(
        self,
        event_class: str,
        *target_parts: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Publish one sensory event.

        Returns silently on bus failure — the caller's correctness must
        not depend on the event reaching subscribers. (Reflex handlers
        run idempotently against persistent state changes; missing one
        publish is recoverable on the next poll cycle.)

        Programmer errors (unknown event_class, malformed target) raise
        immediately so they surface in unit tests rather than in
        production logs.
        """
        subject = sensory_subject(event_class, self._source, *target_parts)
        full_payload = dict(payload or {})
        # `source` echoed into the payload so downstream consumers that
        # don't parse the subject (or that re-emit on a derived subject)
        # still know where the observation came from.
        full_payload.setdefault("source", self._source)
        full_payload.setdefault(
            "recorded_at",
            datetime.now(tz=timezone.utc).isoformat(),
        )
        try:
            await self._bus.publish(subject, full_payload)
        except Exception as exc:
            _LOG.warning(
                "sensory_publisher.publish_failed subject=%s source=%s error=%s",
                subject, self._source, exc,
            )
            return
        _LOG.info(
            "bus.published subject=%s source=%s event_class=%s target_parts=%d",
            subject, self._source, event_class, len(target_parts),
        )


__all__ = ["SensoryPublisher"]
