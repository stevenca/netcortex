"""Shared parametrization for contract test suites.

Each contract suite under ``tests/contracts/<name>/`` consumes one of the
fixtures below to receive a factory for every registered implementation of
that contract. Adding a new implementation is a one-line change to the
appropriate registry list.

The registries currently contain only the reference in-memory implementations
because no production backends have landed yet. As ``NatsEventBus``,
``SplunkEpisodicStore`` and friends land, they get appended here and the
contract tests automatically cover them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from netcortex.contracts import EventBus, Policy, SensoryAdapter

# ---------------------------------------------------------------------------
# EventBus registry.
# ---------------------------------------------------------------------------


def _make_in_memory_event_bus() -> EventBus:
    # Local import: keeps the contracts package free of implementation imports.
    from tests.contracts.event_bus.in_memory import InMemoryEventBus

    return InMemoryEventBus()


EVENT_BUS_IMPLEMENTATIONS: list[tuple[str, Callable[[], EventBus]]] = [
    ("in_memory", _make_in_memory_event_bus),
    # Future: ("nats", lambda: NatsEventBus.connect("nats://localhost:4222")),
]


@pytest.fixture(params=[name for name, _ in EVENT_BUS_IMPLEMENTATIONS], ids=lambda n: n)
def event_bus_factory(request: pytest.FixtureRequest) -> Callable[[], EventBus]:
    name: str = request.param
    for n, factory in EVENT_BUS_IMPLEMENTATIONS:
        if n == name:
            return factory
    raise RuntimeError(f"unknown event bus impl: {name}")


# ---------------------------------------------------------------------------
# SensoryAdapter registry.
# ---------------------------------------------------------------------------


def _make_stub_sensory_adapter() -> SensoryAdapter:
    from tests.contracts.sensory_adapter.stub_adapter import StubSensoryAdapter

    return StubSensoryAdapter()


SENSORY_ADAPTER_IMPLEMENTATIONS: list[tuple[str, Callable[[], SensoryAdapter]]] = [
    ("stub", _make_stub_sensory_adapter),
]


@pytest.fixture(params=[name for name, _ in SENSORY_ADAPTER_IMPLEMENTATIONS], ids=lambda n: n)
def sensory_adapter_factory(request: pytest.FixtureRequest) -> Callable[[], SensoryAdapter]:
    name: str = request.param
    for n, factory in SENSORY_ADAPTER_IMPLEMENTATIONS:
        if n == name:
            return factory
    raise RuntimeError(f"unknown sensory adapter impl: {name}")


# ---------------------------------------------------------------------------
# Policy registry.
# ---------------------------------------------------------------------------


def _make_constant_policy() -> Policy:
    from tests.contracts.policy.constant_policy import ConstantPolicy

    return ConstantPolicy()


POLICY_IMPLEMENTATIONS: list[tuple[str, Callable[[], Policy]]] = [
    ("constant", _make_constant_policy),
]


@pytest.fixture(params=[name for name, _ in POLICY_IMPLEMENTATIONS], ids=lambda n: n)
def policy_factory(request: pytest.FixtureRequest) -> Callable[[], Policy]:
    name: str = request.param
    for n, factory in POLICY_IMPLEMENTATIONS:
        if n == name:
            return factory
    raise RuntimeError(f"unknown policy impl: {name}")


__all__: Iterable[str] = (
    "EVENT_BUS_IMPLEMENTATIONS",
    "SENSORY_ADAPTER_IMPLEMENTATIONS",
    "POLICY_IMPLEMENTATIONS",
    "event_bus_factory",
    "sensory_adapter_factory",
    "policy_factory",
)
