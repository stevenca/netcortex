"""Unit tests for the :class:`SensoryPublisher` convenience facade.

These tests use the in-memory event bus from the contract suite so we
exercise real publish semantics without a NATS dependency.
"""

from __future__ import annotations

from typing import Any

import pytest

from netcortex.thalamus.sensory_publisher import SensoryPublisher
from tests.contracts.event_bus.in_memory import InMemoryEventBus


def test_constructor_rejects_unknown_source() -> None:
    bus = InMemoryEventBus()
    with pytest.raises(ValueError, match="unknown sensory source"):
        SensoryPublisher(bus, source="not_a_real_source")


def test_source_property_round_trips() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    assert pub.source == "snmp_poll"


@pytest.mark.asyncio
async def test_publish_builds_validated_subject_and_emits() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    seen: list[Any] = []

    async def consume() -> None:
        async for msg in bus.subscribe("sensory.link_down.>"):
            seen.append(msg)
            break

    import asyncio
    task = asyncio.create_task(consume())
    # Tiny grace to ensure subscription is in place before the publish.
    await asyncio.sleep(0.01)

    await pub.publish(
        "link_down", "r1|Gi0/1",
        payload={"device": "r1", "interface": "Gi0/1"},
    )
    await asyncio.wait_for(task, timeout=1.0)

    assert len(seen) == 1
    msg = seen[0]
    assert msg.subject == "sensory.link_down.snmp_poll.r1|Gi0/1"
    assert msg.payload["device"] == "r1"
    assert msg.payload["interface"] == "Gi0/1"
    # Publisher injects defaults for fields the caller didn't supply.
    assert msg.payload["source"] == "snmp_poll"
    assert "recorded_at" in msg.payload


@pytest.mark.asyncio
async def test_publish_rejects_unknown_event_class() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    with pytest.raises(ValueError, match="unknown sensory event class"):
        await pub.publish("not_a_class", "r1|Gi0/1")


@pytest.mark.asyncio
async def test_publish_swallows_bus_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient bus failure must not bubble up to the caller.

    The adapter still wrote its observation to the graph via ingest; the
    sensory event is best-effort. Raising here would crash whatever poll
    loop produced the observation, which is much worse than losing one
    reflex hook.
    """
    bus = InMemoryEventBus()

    async def broken_publish(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated nats outage")

    monkeypatch.setattr(bus, "publish", broken_publish)
    pub = SensoryPublisher(bus, source="snmp_poll")
    # Must NOT raise.
    await pub.publish("link_down", "r1|Gi0/1")


@pytest.mark.asyncio
async def test_publish_sanitizes_whitespace_in_target_parts() -> None:
    """Vendor identifiers like Meraki's 'Port 3' must round-trip cleanly.

    Real bug caught in the dev5 deploy: NATS subjects forbid whitespace,
    so without sanitization every Meraki MS port (Port 1, Port 2, ...)
    drops its link-state events. The publisher collapses whitespace runs
    to '_' so the subject is valid; the payload retains the original
    interface name for consumers that care about the human-readable form.
    """
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    seen: list[Any] = []

    async def consume() -> None:
        async for msg in bus.subscribe("sensory.link_down.>"):
            seen.append(msg)
            break

    import asyncio
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await pub.publish(
        "link_down", "cpn-arlington-ms1|Port 3",
        payload={"device": "cpn-arlington-ms1", "interface": "Port 3"},
    )
    await asyncio.wait_for(task, timeout=1.0)

    assert seen[0].subject == "sensory.link_down.snmp_poll.cpn-arlington-ms1|Port_3"
    # Original whitespace preserved in payload for human-readable display.
    assert seen[0].payload["interface"] == "Port 3"


@pytest.mark.asyncio
async def test_publish_collapses_multiple_whitespace_runs() -> None:
    """Tabs, NBSP, and multi-space runs all collapse to a single '_'."""
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    seen: list[Any] = []

    async def consume() -> None:
        async for msg in bus.subscribe("sensory.link_down.>"):
            seen.append(msg)
            break

    import asyncio
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await pub.publish(
        "link_down", "r1|MS130-  12X\tport",
    )
    await asyncio.wait_for(task, timeout=1.0)

    assert seen[0].subject == "sensory.link_down.snmp_poll.r1|MS130-_12X_port"


@pytest.mark.asyncio
async def test_publish_still_rejects_dots_in_target_parts() -> None:
    """Dots are the NATS token separator — they must remain a hard error.

    Whitespace gets sanitized, dots do NOT. A caller passing 'a.b' as
    one target part almost certainly meant 'a' and 'b' as two parts
    and silently merging would mask the bug.
    """
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    with pytest.raises(ValueError, match="contains '.'"):
        await pub.publish("link_down", "device.with.dots")


@pytest.mark.asyncio
async def test_publish_preserves_caller_supplied_source_and_recorded_at() -> None:
    """Caller-supplied source/recorded_at win over the defaults.

    This matters for adapters that re-publish someone else's
    observation (e.g. a webhook receiver forwarding a vendor event with
    its own timestamp).
    """
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    seen: list[Any] = []

    async def consume() -> None:
        async for msg in bus.subscribe("sensory.link_down.>"):
            seen.append(msg)
            break

    import asyncio
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await pub.publish(
        "link_down", "r1|Gi0/1",
        payload={
            "source": "meraki_webhook",
            "recorded_at": "2026-06-01T00:00:00+00:00",
        },
    )
    await asyncio.wait_for(task, timeout=1.0)

    msg = seen[0]
    assert msg.payload["source"] == "meraki_webhook"
    assert msg.payload["recorded_at"] == "2026-06-01T00:00:00+00:00"
