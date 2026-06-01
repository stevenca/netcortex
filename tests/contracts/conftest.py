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

import os
from collections.abc import Callable, Iterable
from typing import Any

import pytest

from netcortex.contracts import DedupStore, EventBus, Policy, SensoryAdapter

# ---------------------------------------------------------------------------
# EventBus registry.
# ---------------------------------------------------------------------------


def _make_in_memory_event_bus() -> EventBus:
    # Local import: keeps the contracts package free of implementation imports.
    from tests.contracts.event_bus.in_memory import InMemoryEventBus

    return InMemoryEventBus()


def _make_nats_event_bus() -> EventBus:
    """Build a :class:`NatsEventBus` against a real server, or skip if absent.

    The contract suite runs against every registered implementation. For the
    NATS implementation we need an actual running broker (a service container
    in CI, ``docker run -d -p 4222:4222 nats:2.11-alpine -js`` locally). When
    ``NATS_URL`` is not set we skip rather than fail — the in-memory backend
    still verifies the Protocol surface, and a developer without docker can
    still run the contract suite.
    """
    url = os.environ.get("NATS_URL")
    if not url:
        pytest.skip(
            "NATS_URL not set; skipping real-NATS contract tests "
            "(set NATS_URL=nats://localhost:4222 to run them)"
        )
    from netcortex.thalamus import NatsEventBus

    # Tests should fail fast, not retry forever like production would.
    return NatsEventBus(url, max_reconnect_attempts=2, connect_timeout=2.0)


EVENT_BUS_IMPLEMENTATIONS: list[tuple[str, Callable[[], EventBus]]] = [
    ("in_memory", _make_in_memory_event_bus),
    ("nats", _make_nats_event_bus),
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


# ---------------------------------------------------------------------------
# DedupStore registry.
# ---------------------------------------------------------------------------


def _make_in_memory_dedup_store() -> DedupStore:
    from netcortex.working.dedup import InMemoryDedupStore

    return InMemoryDedupStore()


# Redis-backed store lands in 0.9.0; when it does, add a
# _make_redis_dedup_store factory here that pytest.skips when REDIS_URL is
# absent, exactly the same pattern as _make_nats_event_bus above.
DEDUP_STORE_IMPLEMENTATIONS: list[tuple[str, Callable[[], DedupStore]]] = [
    ("in_memory", _make_in_memory_dedup_store),
]


@pytest.fixture(params=[name for name, _ in DEDUP_STORE_IMPLEMENTATIONS], ids=lambda n: n)
def dedup_store_factory(request: pytest.FixtureRequest) -> Callable[[], DedupStore]:
    name: str = request.param
    for n, factory in DEDUP_STORE_IMPLEMENTATIONS:
        if n == name:
            return factory
    raise RuntimeError(f"unknown dedup store impl: {name}")


__all__: Iterable[str] = (
    "DEDUP_STORE_IMPLEMENTATIONS",
    "EVENT_BUS_IMPLEMENTATIONS",
    "SENSORY_ADAPTER_IMPLEMENTATIONS",
    "POLICY_IMPLEMENTATIONS",
    "dedup_store_factory",
    "event_bus_factory",
    "sensory_adapter_factory",
    "policy_factory",
)
