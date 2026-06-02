"""Unit tests for the SNMP link-state change publisher.

These tests don't touch real Neo4j or real SNMP — they inject a fake
async driver/session that returns a controlled prior-state map and
assert which events the publisher emits.
"""

from __future__ import annotations

from typing import Any

import pytest

from netcortex.adapters.snmp_link_state_publisher import (
    emit_link_state_transitions,
)
from netcortex.thalamus.sensory_publisher import SensoryPublisher
from tests.contracts.event_bus.in_memory import InMemoryEventBus


# ---------------------------------------------------------------------------
# Fake Neo4j driver/session — async iterators that mimic neo4j-driver shape.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = list(records)

    def __aiter__(self) -> "_FakeResult":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._records:
            raise StopAsyncIteration
        return self._records.pop(0)


class _FakeSession:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = records

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def run(self, _cypher: str, **_params: Any) -> _FakeResult:
        return _FakeResult(self._records)


class _FakeDriver:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = records

    def session(self) -> _FakeSession:
        return _FakeSession(self._records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _if(name: str, oper: str) -> dict[str, Any]:
    return {"name": name, "oper_status": oper}


async def _collect_published(bus: InMemoryEventBus, n: int) -> list[Any]:
    """Drain up to n messages published on sensory.> in a bounded time."""
    import asyncio
    seen: list[Any] = []

    async def consume() -> None:
        async for msg in bus.subscribe("sensory.>"):
            seen.append(msg)
            if len(seen) >= n:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    return task, seen  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_publisher_is_silent_noop() -> None:
    result = await emit_link_state_transitions(
        publisher=None,
        driver=_FakeDriver([]),
        device_node_id="dev:1",
        device_name="r1",
        if_map={"1": _if("Gi0/1", "down")},
    )
    assert result == 0


@pytest.mark.asyncio
async def test_no_driver_is_silent_noop() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=None,
        device_node_id="dev:1",
        device_name="r1",
        if_map={"1": _if("Gi0/1", "down")},
    )
    assert result == 0


@pytest.mark.asyncio
async def test_empty_if_map_is_silent_noop() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver([]),
        device_node_id="dev:1",
        device_name="r1",
        if_map={},
    )
    assert result == 0


@pytest.mark.asyncio
async def test_no_transition_emits_nothing() -> None:
    """Prior state equals fresh state — no events should publish."""
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    prior = [
        {"id": "snmp-if:dev:1:Gi0/1", "name": "Gi0/1", "oper": "up"},
        {"id": "snmp-if:dev:1:Gi0/2", "name": "Gi0/2", "oper": "up"},
    ]
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver(prior),
        device_node_id="dev:1",
        device_name="r1",
        if_map={
            "1": _if("Gi0/1", "up"),
            "2": _if("Gi0/2", "up"),
        },
    )
    assert result == 0


@pytest.mark.asyncio
async def test_up_to_down_emits_link_down() -> None:
    import asyncio
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    prior = [
        {"id": "snmp-if:dev:1:Gi0/1", "name": "Gi0/1", "oper": "up"},
    ]

    task, seen = await _collect_published(bus, 1)
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver(prior),
        device_node_id="dev:1",
        device_name="r1",
        if_map={"1": _if("Gi0/1", "down")},
    )
    await asyncio.wait_for(task, timeout=1.0)

    assert result == 1
    assert len(seen) == 1
    msg = seen[0]
    assert msg.subject == "sensory.link_down.snmp_poll.r1|Gi0/1"
    assert msg.payload["device"] == "r1"
    assert msg.payload["interface"] == "Gi0/1"
    assert msg.payload["oper_status"] == "down"
    assert msg.payload["prior_oper_status"] == "up"


@pytest.mark.asyncio
async def test_down_to_up_emits_link_up() -> None:
    import asyncio
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    prior = [
        {"id": "snmp-if:dev:1:Gi0/1", "name": "Gi0/1", "oper": "down"},
    ]

    task, seen = await _collect_published(bus, 1)
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver(prior),
        device_node_id="dev:1",
        device_name="r1",
        if_map={"1": _if("Gi0/1", "up")},
    )
    await asyncio.wait_for(task, timeout=1.0)

    assert result == 1
    assert seen[0].subject == "sensory.link_up.snmp_poll.r1|Gi0/1"


@pytest.mark.asyncio
async def test_first_observation_only_emits_for_down() -> None:
    """A 96-port switch on first poll must not emit 96 link_up events."""
    import asyncio
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    # Empty prior — first-ever poll.
    task, seen = await _collect_published(bus, 1)
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver([]),
        device_node_id="dev:1",
        device_name="r1",
        if_map={
            "1": _if("Gi0/1", "up"),
            "2": _if("Gi0/2", "up"),
            "3": _if("Gi0/3", "down"),
            "4": _if("Gi0/4", "up"),
        },
    )
    await asyncio.wait_for(task, timeout=1.0)

    # Only the one currently-down interface should emit.
    assert result == 1
    assert seen[0].subject == "sensory.link_down.snmp_poll.r1|Gi0/3"
    assert seen[0].payload["prior_oper_status"] is None


@pytest.mark.asyncio
async def test_neo4j_read_failure_is_swallowed() -> None:
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")

    class _BrokenDriver:
        def session(self) -> Any:
            class _S:
                async def __aenter__(self_inner) -> Any:
                    raise RuntimeError("neo4j down")

                async def __aexit__(self_inner, *_a: Any) -> None:
                    return None
            return _S()

    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_BrokenDriver(),
        device_node_id="dev:1",
        device_name="r1",
        if_map={"1": _if("Gi0/1", "down")},
    )
    assert result == 0


@pytest.mark.asyncio
async def test_unknown_oper_status_is_ignored() -> None:
    """ifOperStatus values like 'testing', 'dormant' are not in our taxonomy."""
    bus = InMemoryEventBus()
    pub = SensoryPublisher(bus, source="snmp_poll")
    result = await emit_link_state_transitions(
        publisher=pub,
        driver=_FakeDriver([]),
        device_node_id="dev:1",
        device_name="r1",
        if_map={
            "1": {"name": "Gi0/1", "oper_status": "testing"},
            "2": {"name": "Gi0/2"},  # missing oper_status entirely
        },
    )
    assert result == 0
