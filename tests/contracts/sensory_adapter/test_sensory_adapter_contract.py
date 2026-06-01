"""Contract tests every ``SensoryAdapter`` implementation must pass."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netcortex.contracts import SensoryAdapter, SensoryEvent

pytestmark = pytest.mark.asyncio


async def test_emits_sensory_event_instances(
    sensory_adapter_factory: Callable[[], SensoryAdapter],
) -> None:
    adapter = sensory_adapter_factory()
    seen = 0
    async for event in adapter.discover():
        assert isinstance(event, SensoryEvent)
        seen += 1
        if seen >= 1:
            break
    assert seen >= 1


async def test_each_event_has_required_fields(
    sensory_adapter_factory: Callable[[], SensoryAdapter],
) -> None:
    adapter = sensory_adapter_factory()
    async for event in adapter.discover():
        assert event.modality, "modality must be a non-empty string"
        assert isinstance(event.target, dict) and event.target, (
            "target must be a non-empty dict identifying what the event is about"
        )
        assert isinstance(event.payload, dict), "payload must be a dict"
        assert 0.0 <= event.confidence <= 1.0
        # Provenance must start with the adapter id.
        assert event.provenance, "provenance must include at least the adapter id"
        assert event.provenance[0] == adapter.adapter_id
        break


async def test_modality_is_stable_across_iterations(
    sensory_adapter_factory: Callable[[], SensoryAdapter],
) -> None:
    """Subscribers depend on stable modality strings; flapping breaks routing."""
    adapter = sensory_adapter_factory()
    modalities: set[str] = set()
    seen = 0
    async for event in adapter.discover():
        modalities.add(event.modality)
        seen += 1
        if seen >= 3:
            break
    # All events from one adapter sweep should share one or a small fixed
    # set of modalities — never one-per-event.
    assert len(modalities) <= 2, (
        f"adapter emitted too many distinct modalities: {modalities}"
    )
