"""Contract tests every ``EventBus`` implementation must pass.

These tests are written against the Protocol, not against any one concrete
class. The ``event_bus_factory`` fixture in ``tests/contracts/conftest.py``
parametrizes every test over the registered implementations — adding a new
implementation requires zero changes here.

If you find yourself wanting to add an assertion that one specific
implementation makes but another doesn't, you have probably found a
contract violation. Either tighten the Protocol docstring or relax the
implementation; do not weaken the contract test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from netcortex.contracts import EventBus, EventBusValidationError


pytestmark = pytest.mark.asyncio


async def _collect(
    bus: EventBus,
    pattern: str,
    n: int,
    *,
    timeout: float = 2.0,
) -> list[str]:
    """Read up to ``n`` messages from a subscription and return subjects."""
    subjects: list[str] = []
    sub_iter = bus.subscribe(pattern)

    async def _consume() -> None:
        async for msg in sub_iter:
            subjects.append(msg.subject)
            if len(subjects) >= n:
                return

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return subjects


async def test_publish_subscribe_roundtrip(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    try:
        # Subscribe before publish — at-least-once delivery.
        consumer = asyncio.create_task(_collect(bus, "sensory.test.>", n=1))
        await asyncio.sleep(0.05)
        await bus.publish("sensory.test.event", {"hello": "world"})
        subjects = await consumer
        assert subjects == ["sensory.test.event"]
    finally:
        await bus.close()


async def test_subjects_filtered_by_wildcards(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    try:
        # ``*`` matches exactly one token.
        consumer = asyncio.create_task(_collect(bus, "sensory.*.event", n=2))
        await asyncio.sleep(0.05)
        await bus.publish("sensory.a.event", {})
        await bus.publish("sensory.b.event", {})
        # Should NOT match — three tokens between sensory and event.
        await bus.publish("sensory.a.b.event", {})
        subjects = await consumer
        assert sorted(subjects) == ["sensory.a.event", "sensory.b.event"]
    finally:
        await bus.close()


async def test_no_replay_for_late_subscribers(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    try:
        # Publish before any subscriber exists.
        await bus.publish("sensory.early.event", {})
        # Late subscriber must NOT see the early event.
        consumer = asyncio.create_task(_collect(bus, "sensory.early.event", n=1, timeout=0.3))
        await asyncio.sleep(0.05)
        await bus.publish("sensory.early.event", {"second": True})
        subjects = await consumer
        # Only the post-subscription event should be visible.
        assert len(subjects) == 1
    finally:
        await bus.close()


async def test_independent_subscribers_isolated(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    try:
        a = asyncio.create_task(_collect(bus, "sensory.shared", n=2))
        b = asyncio.create_task(_collect(bus, "sensory.shared", n=2))
        await asyncio.sleep(0.05)
        await bus.publish("sensory.shared", {"i": 0})
        await bus.publish("sensory.shared", {"i": 1})
        a_subjects = await a
        b_subjects = await b
        # Both subscribers see both events.
        assert len(a_subjects) == 2
        assert len(b_subjects) == 2
    finally:
        await bus.close()


@pytest.mark.parametrize(
    "subject",
    ["", " ", "no spaces allowed", "wild.*.card", "wild.>", "trailing.", ".leading"],
)
async def test_publish_rejects_invalid_subjects(
    event_bus_factory: Callable[[], EventBus],
    subject: str,
) -> None:
    bus = event_bus_factory()
    try:
        with pytest.raises(EventBusValidationError):
            await bus.publish(subject, {})
    finally:
        await bus.close()


async def test_publish_rejects_non_dict_payload(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    try:
        with pytest.raises(EventBusValidationError):
            await bus.publish("ok.subject", "not a dict")  # type: ignore[arg-type]
    finally:
        await bus.close()


async def test_close_is_idempotent(
    event_bus_factory: Callable[[], EventBus],
) -> None:
    bus = event_bus_factory()
    await bus.close()
    # A second close must not raise.
    await bus.close()
