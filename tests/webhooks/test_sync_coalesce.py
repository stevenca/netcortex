"""Tests for the webhook sync coalescing scheduler (0.8.0-dev10, F4)."""

from __future__ import annotations

import asyncio

import pytest

from netcortex.webhooks import sync_coalesce as sc


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch: pytest.MonkeyPatch):
    # Remove the inter-run debounce delay so tests don't sleep.
    monkeypatch.setattr(sc, "_MIN_INTERVAL_S", 0.0)
    sc._reset_for_tests()
    yield
    sc._reset_for_tests()


async def test_single_schedule_runs_once() -> None:
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)

    sc.schedule_sync("a", factory)
    await asyncio.sleep(0.05)
    assert calls == [1]


async def test_bursts_coalesce_to_one_trailing_run() -> None:
    """While one sync is in flight, a burst of further triggers collapses
    into a single trailing run — not one run per trigger."""
    calls: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> None:
        calls.append(1)
        started.set()
        await release.wait()

    sc.schedule_sync("a", factory)        # run #1 begins
    await started.wait()                   # confirm it's in flight
    for _ in range(5):
        sc.schedule_sync("a", factory)     # 5 triggers → 1 coalesced trailing
    release.set()
    await asyncio.sleep(0.1)               # let #1 finish + trailing run

    assert calls == [1, 1]                  # exactly two executions


async def test_distinct_instances_run_independently() -> None:
    calls: list[str] = []

    async def make(name: str):
        async def factory() -> None:
            calls.append(name)
        return factory

    sc.schedule_sync("a", await make("a"))
    sc.schedule_sync("b", await make("b"))
    await asyncio.sleep(0.05)
    assert sorted(calls) == ["a", "b"]
