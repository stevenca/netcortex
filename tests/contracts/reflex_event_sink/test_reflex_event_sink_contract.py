"""Protocol-conformance tests for every registered :class:`ReflexEventSink`.

Each implementation registered in
:data:`tests.contracts.conftest.REFLEX_EVENT_SINK_IMPLEMENTATIONS` runs through
the same cases here. Adding a Neo4j-backed sink is a one-line addition to
the registry plus a service-container fixture in CI; the assertions stay
the same.

Contract invariants exercised
-----------------------------
1. ``record(outcome)`` returns without raising for any well-formed outcome.
2. Recording the **same outcome twice** is idempotent — the sink must
   collapse via the stable id derived from
   ``(handler, subject, target, occurred_at)``.
3. Two outcomes that differ only in ``payload`` / ``diagnostic`` MUST
   collide (the sink uses identity fields, not full content, for dedup —
   otherwise a retry with slightly different upstream_keys would create
   a second event).
4. Two outcomes from the same handler at different ``occurred_at``
   timestamps MUST be persisted separately.
5. ``close()`` is idempotent; subsequent ``record()`` raises so callers
   notice they are using a torn-down sink.
6. The sink survives ``record`` calls from multiple asyncio tasks
   concurrently (required because the runner spawns one task per handler).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from netcortex.contracts import ReflexEventSink
from netcortex.reflex.protocol import ReflexOutcome

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _outcome(
    *,
    handler: str = "link_down",
    subject: str = "sensory.link_down.snmp_poll.r1|Gi0/1",
    target: str = "r1|Gi0/1",
    occurred_at: datetime = _T0,
    payload: dict | None = None,
    diagnostic: dict | None = None,
) -> ReflexOutcome:
    return ReflexOutcome(
        handler=handler,
        subject=subject,
        target=target,
        severity="high",
        occurred_at=occurred_at,
        payload=payload or {"source": "snmp_poll"},
        outcome="logged",
        rationale="contract test",
        diagnostic=diagnostic or {},
    )


@pytest.mark.asyncio
async def test_record_well_formed_outcome_returns(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    try:
        await sink.record(_outcome())
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_record_is_idempotent_on_identity_fields(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    try:
        outcome = _outcome()
        await sink.record(outcome)
        await sink.record(outcome)
        # InMemory sink lets us assert directly; other sinks would need
        # their own introspection in their own test suite (this contract
        # only asserts the no-raise behavior + lack of duplicate growth
        # via the introspection that's safe to access on all backends).
        if hasattr(sink, "__len__"):
            assert len(sink) == 1  # type: ignore[arg-type]
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_record_collapses_outcomes_that_differ_only_in_payload(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    try:
        await sink.record(_outcome(payload={"source": "snmp_poll", "v": 1}))
        await sink.record(_outcome(payload={"source": "snmp_poll", "v": 2}))
        if hasattr(sink, "__len__"):
            assert len(sink) == 1  # type: ignore[arg-type]
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_record_separates_outcomes_at_different_occurred_at(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    try:
        await sink.record(_outcome(occurred_at=_T0))
        await sink.record(_outcome(occurred_at=_T0 + timedelta(seconds=1)))
        if hasattr(sink, "__len__"):
            assert len(sink) == 2  # type: ignore[arg-type]
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    await sink.close()
    await sink.close()


@pytest.mark.asyncio
async def test_record_after_close_raises(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    await sink.close()
    with pytest.raises(Exception):
        await sink.record(_outcome())


@pytest.mark.asyncio
async def test_concurrent_record_from_multiple_tasks_is_safe(
    reflex_event_sink_factory: Callable[[], ReflexEventSink],
) -> None:
    sink = reflex_event_sink_factory()
    try:
        # Fan out 20 distinct outcomes across 20 tasks; assert all 20 land.
        outcomes = [
            _outcome(
                target=f"r{i}|Gi0/1",
                subject=f"sensory.link_down.snmp_poll.r{i}|Gi0/1",
                occurred_at=_T0 + timedelta(seconds=i),
            )
            for i in range(20)
        ]
        await asyncio.gather(*(sink.record(o) for o in outcomes))
        if hasattr(sink, "__len__"):
            assert len(sink) == 20  # type: ignore[arg-type]
    finally:
        await sink.close()
