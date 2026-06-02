"""End-to-end tests for :class:`ReflexRunner`.

Exercise the runner against the in-memory event bus so dispatch is
deterministic without standing up NATS. The in-memory bus implements the
same Protocol, so passing here means the runner will behave identically
against the production NATS backend (verified by the contract suite that
NATS satisfies that Protocol).

In dev3 the runner threads a :class:`ReflexContext` to every handler
call. These tests pin the wiring: default-context path (no context
supplied → empty context), explicit-context path (with a dedup store
that the handler can consult), and the unchanged failure-isolation /
lifecycle behavior.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Final

import pytest

from netcortex.contracts.event_bus import EventBus, EventMessage
from netcortex.reflex.protocol import ReflexContext, ReflexOutcome
from netcortex.reflex.runner import ReflexRunner
from netcortex.working.dedup import InMemoryDedupStore
from tests.contracts.event_bus.in_memory import InMemoryEventBus

pytestmark = pytest.mark.asyncio


def _bus() -> EventBus:
    return InMemoryEventBus()


class _RecordingHandler:
    """Handler that records every event it sees for test introspection.

    Also records the context it received so tests can confirm the runner
    threaded a non-default context through.
    """

    def __init__(self, hid: str, pattern: str) -> None:
        self.id: Final[str] = hid
        self.pattern: Final[str] = pattern
        self.seen: list[EventMessage] = []
        self.contexts: list[ReflexContext] = []

    async def handle(
        self, event: EventMessage, ctx: ReflexContext
    ) -> ReflexOutcome | None:
        self.seen.append(event)
        self.contexts.append(ctx)
        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=str(event.payload.get("target") or "") or None,
            severity="info",
            occurred_at=datetime.now(tz=timezone.utc),
            payload=dict(event.payload),
        )


class _RaisingHandler:
    """Handler that raises — used to verify per-handler isolation."""

    id: Final[str] = "boom"
    pattern: Final[str] = "sensory.link_down.test_src.boom"

    async def handle(
        self, event: EventMessage, ctx: ReflexContext
    ) -> ReflexOutcome | None:
        raise RuntimeError("simulated handler failure")


class _SkippingHandler:
    """Handler that returns ``None`` — used to verify None-skips-recording."""

    id: Final[str] = "skip"
    pattern: Final[str] = "sensory.link_up.test_src.skip"

    async def handle(
        self, event: EventMessage, ctx: ReflexContext
    ) -> ReflexOutcome | None:
        return None


async def _wait_for(predicate: Any, timeout: float = 1.5) -> None:
    """Poll until ``predicate()`` is truthy or timeout. Better than fixed sleep."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("predicate did not become true within timeout")


async def test_dispatches_event_to_matching_handler() -> None:
    bus = _bus()
    handler = _RecordingHandler("link_down", "sensory.link_down.>")
    runner = ReflexRunner(bus, handlers=[handler])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish(
            "sensory.link_down.snmp_trap.r1|Gi0/1",
            {"interface": "Gi0/1", "target": "r1"},
        )
        await _wait_for(lambda: len(handler.seen) == 1)
    finally:
        await runner.stop()
        await bus.close()

    assert handler.seen[0].subject == "sensory.link_down.snmp_trap.r1|Gi0/1"
    assert handler.seen[0].payload == {"interface": "Gi0/1", "target": "r1"}
    assert len(runner.outcomes) == 1
    outcome = runner.outcomes[0]
    assert outcome.handler == "link_down"
    assert outcome.target == "r1"


async def test_pattern_filters_out_non_matching_events() -> None:
    bus = _bus()
    handler = _RecordingHandler("only_link_down", "sensory.link_down.>")
    runner = ReflexRunner(bus, handlers=[handler])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.snmp_trap.r1", {"i": 0})
        await bus.publish("sensory.link_up.snmp_trap.r1", {"i": 1})  # not match
        await bus.publish("sensory.link_down.meraki_webhook.r2", {"i": 2})
        await _wait_for(lambda: len(handler.seen) == 2)
    finally:
        await runner.stop()
        await bus.close()

    assert sorted(e.payload["i"] for e in handler.seen) == [0, 2]


async def test_multiple_handlers_fan_out() -> None:
    """One event whose subject matches two handlers reaches both."""
    bus = _bus()
    a = _RecordingHandler("a", "sensory.link_down.>")
    b = _RecordingHandler("b", "sensory.link_down.>")
    runner = ReflexRunner(bus, handlers=[a, b])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.snmp_trap.r1", {"i": 0})
        await _wait_for(lambda: len(a.seen) == 1 and len(b.seen) == 1)
    finally:
        await runner.stop()
        await bus.close()


async def test_handler_exception_does_not_kill_dispatcher() -> None:
    """A raising handler produces an ``errored`` outcome and keeps going."""
    bus = _bus()
    boom = _RaisingHandler()
    runner = ReflexRunner(bus, handlers=[boom])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.test_src.boom", {"n": 1})
        await bus.publish("sensory.link_down.test_src.boom", {"n": 2})
        await _wait_for(lambda: len(runner.outcomes) == 2)
    finally:
        await runner.stop()
        await bus.close()

    assert all(o.outcome == "errored" for o in runner.outcomes)
    assert all("RuntimeError" in o.rationale for o in runner.outcomes)
    assert "traceback" in runner.outcomes[0].diagnostic
    assert (
        "simulated handler failure" in runner.outcomes[0].diagnostic["traceback"]
    )


async def test_none_outcome_is_not_recorded() -> None:
    """``handle() -> None`` is a conscious no-op, NOT an error."""
    bus = _bus()
    handler = _SkippingHandler()
    runner = ReflexRunner(bus, handlers=[handler])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_up.test_src.skip", {})
        await asyncio.sleep(0.2)
    finally:
        await runner.stop()
        await bus.close()

    assert runner.outcomes == []


async def test_start_is_idempotent() -> None:
    bus = _bus()
    handler = _RecordingHandler("x", "sensory.link_down.>")
    runner = ReflexRunner(bus, handlers=[handler])
    await runner.start()
    await runner.start()
    try:
        assert len([t for t in runner._tasks if not t.done()]) == 1  # noqa: SLF001
    finally:
        await runner.stop()
        await bus.close()


async def test_stop_is_idempotent() -> None:
    bus = _bus()
    runner = ReflexRunner(
        bus, handlers=[_RecordingHandler("x", "sensory.link_down.>")]
    )
    await runner.start()
    await runner.stop()
    await runner.stop()
    await bus.close()


async def test_stop_without_start_is_safe() -> None:
    """Calling stop on a runner that never started is a no-op."""
    bus = _bus()
    runner = ReflexRunner(bus, handlers=[_RecordingHandler("x", "sensory.link_down.>")])
    await runner.stop()
    await bus.close()


async def test_runner_enumerates_registry_when_no_handlers_given() -> None:
    """Default-argument path: the runner reads the registry."""
    from netcortex.reflex.registry import (
        all_handlers,
        clear_registry,
        register_handler,
    )

    snapshot = list(all_handlers())
    clear_registry()
    try:
        a = _RecordingHandler("reg-a", "sensory.link_down.>")
        b = _RecordingHandler("reg-b", "sensory.link_up.>")
        register_handler(a)
        register_handler(b)
        bus = _bus()
        runner = ReflexRunner(bus)
        assert {h.id for h in runner.handlers} == {"reg-a", "reg-b"}
        await runner.stop()
        await bus.close()
    finally:
        clear_registry()
        for h in snapshot:
            register_handler(h)


# ---------------------------------------------------------------------------
# ReflexContext threading (dev3)
# ---------------------------------------------------------------------------


async def test_default_context_has_no_dedup_store() -> None:
    """Runners built without an explicit context still pass one through."""
    bus = _bus()
    handler = _RecordingHandler("ctx_default", "sensory.link_down.>")
    runner = ReflexRunner(bus, handlers=[handler])
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.snmp_trap.r1", {})
        await _wait_for(lambda: len(handler.contexts) == 1)
    finally:
        await runner.stop()
        await bus.close()

    assert isinstance(handler.contexts[0], ReflexContext)
    assert handler.contexts[0].dedup_store is None


async def test_explicit_context_is_threaded_to_handler() -> None:
    """A context passed to ReflexRunner reaches every handle() call."""
    bus = _bus()
    handler = _RecordingHandler("ctx_explicit", "sensory.link_down.>")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    runner = ReflexRunner(bus, handlers=[handler], context=ctx)
    try:
        assert runner.context is ctx
        await runner.start()
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.snmp_trap.r1", {})
        await _wait_for(lambda: len(handler.contexts) == 1)
        assert handler.contexts[0] is ctx
        assert handler.contexts[0].dedup_store is store
    finally:
        await runner.stop()
        await store.close()
        await bus.close()


async def test_runner_persists_outcomes_to_event_sink() -> None:
    """Dev5: a context with an event_sink causes the runner to persist outcomes.

    Confirms the dev5 wiring end-to-end on the in-memory bus + in-memory
    sink: publish → handler → outcome → sink. The sink dedup invariant
    (same handler/subject/target/occurred_at collapses) is in the
    contract suite; this test is about the runner doing the call at all.
    """
    from netcortex.episodic import InMemoryReflexEventSink

    bus = _bus()
    handler = _RecordingHandler("link_down", "sensory.link_down.>")
    sink = InMemoryReflexEventSink()
    ctx = ReflexContext(event_sink=sink)
    runner = ReflexRunner(bus, handlers=[handler], context=ctx)
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish(
            "sensory.link_down.snmp_poll.r1|Gi0/1",
            {"interface": "Gi0/1", "target": "r1"},
        )
        await _wait_for(lambda: len(sink) == 1)
    finally:
        await runner.stop()
        await sink.close()
        await bus.close()

    assert sink.outcomes[0].handler == "link_down"
    assert sink.outcomes[0].subject == "sensory.link_down.snmp_poll.r1|Gi0/1"


async def test_runner_sink_failure_does_not_break_dispatch() -> None:
    """A flaky sink must not stop the runner from processing subsequent events.

    Specifically asserts that handler outcomes still accumulate in
    ``runner.outcomes`` even when sink.record() raises, so the operator
    status endpoint stays accurate during a backend outage.
    """

    class _FlakeySink:
        async def record(self, outcome: ReflexOutcome) -> None:
            raise RuntimeError("simulated sink outage")

        async def close(self) -> None:
            return None

    bus = _bus()
    handler = _RecordingHandler("link_down", "sensory.link_down.>")
    ctx = ReflexContext(event_sink=_FlakeySink())
    runner = ReflexRunner(bus, handlers=[handler], context=ctx)
    await runner.start()
    try:
        await asyncio.sleep(0.05)
        await bus.publish("sensory.link_down.snmp_poll.r1|Gi0/1", {"i": 0})
        await bus.publish("sensory.link_down.snmp_poll.r1|Gi0/2", {"i": 1})
        await _wait_for(lambda: len(handler.seen) == 2)
    finally:
        await runner.stop()
        await bus.close()

    # Both events still dispatched and recorded in-process despite
    # the sink failure on each one.
    assert len(handler.seen) == 2
    assert len(runner.outcomes) == 2
