"""Implementation-specific tests for :class:`InMemoryDedupStore`.

The Protocol-level behavior (atomic check-and-record, TTL expiry,
empty-key rejection, etc.) is covered by the contract suite in
``tests/contracts/dedup_store/``. This module covers the bits that are
**specific** to the in-memory implementation: LRU eviction at the size
cap, lazy expired-entry sweep, controllable-clock fast-forward, the
constructor validation, and the ``size()`` helper.

The use of a controllable monotonic clock is the key idea: we don't want
unit tests to depend on real-clock ``asyncio.sleep`` for the dedup-window
logic (slow + flaky), so the store accepts a ``clock`` callable that the
tests advance manually.
"""

from __future__ import annotations

import pytest

from netcortex.working.dedup import InMemoryDedupStore

pytestmark = pytest.mark.asyncio


class _FakeClock:
    """Monotonically-non-decreasing time source we drive from the test."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock must not move backward")
        self.now += seconds


async def test_constructor_rejects_non_positive_max_entries() -> None:
    with pytest.raises(ValueError):
        InMemoryDedupStore(max_entries=0)
    with pytest.raises(ValueError):
        InMemoryDedupStore(max_entries=-5)


async def test_size_starts_at_zero_and_tracks_inserts() -> None:
    store = InMemoryDedupStore()
    try:
        assert store.size() == 0
        await store.record_unless_duplicate("a", ttl_seconds=10)
        await store.record_unless_duplicate("b", ttl_seconds=10)
        assert store.size() == 2
        # Duplicate insert does not grow size.
        await store.record_unless_duplicate("a", ttl_seconds=10)
        assert store.size() == 2
    finally:
        await store.close()


async def test_fake_clock_drives_ttl_expiry_without_real_sleep() -> None:
    """Whole point of accepting a clock parameter — fast deterministic tests."""
    clk = _FakeClock()
    store = InMemoryDedupStore(clock=clk)
    try:
        assert await store.record_unless_duplicate("k", ttl_seconds=60) is True
        clk.advance(30)
        # Still within TTL — dup.
        assert await store.record_unless_duplicate("k", ttl_seconds=60) is False
        clk.advance(31)
        # Now past TTL — fresh again.
        assert await store.record_unless_duplicate("k", ttl_seconds=60) is True
    finally:
        await store.close()


async def test_lru_eviction_when_cap_exceeded() -> None:
    """At capacity, the LRU entry is evicted to make room for a new one.

    The "least recently used" definition includes duplicate hits: a key
    that gets hit (even as a dup) is moved to the tail. This test checks
    that touching ``oldest`` mid-flow rescues it from eviction.
    """
    clk = _FakeClock()
    store = InMemoryDedupStore(max_entries=3, clock=clk)
    try:
        await store.record_unless_duplicate("oldest", ttl_seconds=1000)
        await store.record_unless_duplicate("middle", ttl_seconds=1000)
        await store.record_unless_duplicate("newest", ttl_seconds=1000)
        assert store.size() == 3

        # Touch "oldest" — dup hit — should move it to the tail.
        assert (
            await store.record_unless_duplicate("oldest", ttl_seconds=1000)
            is False
        )

        # Insert a fourth — should evict "middle" (now the LRU), not "oldest".
        await store.record_unless_duplicate("fourth", ttl_seconds=1000)
        assert store.size() == 3

        # "middle" gone — a fresh insert should succeed.
        assert (
            await store.record_unless_duplicate("middle", ttl_seconds=1000)
            is True
        )
        # "oldest" should still be deduped (was rescued).
        assert (
            await store.record_unless_duplicate("oldest", ttl_seconds=1000)
            is False
        )
    finally:
        await store.close()


async def test_expired_entries_are_swept_lazily() -> None:
    """Stale head-of-LRU entries are reclaimed when new inserts happen.

    We don't promise eager cleanup — the sweep runs when ``record`` is
    called, bounded so worst-case latency stays predictable. After many
    inserts, expired-and-stale entries should be gone.
    """
    clk = _FakeClock()
    store = InMemoryDedupStore(clock=clk)
    try:
        # Plant some short-TTL keys, then age them all out.
        for i in range(10):
            await store.record_unless_duplicate(f"old{i}", ttl_seconds=1.0)
        assert store.size() == 10

        clk.advance(60)

        # New inserts trigger the sweep; the old keys should drop out
        # after enough inserts to exhaust the per-call sweep budget.
        for i in range(40):
            await store.record_unless_duplicate(f"new{i}", ttl_seconds=10)

        # Only "new*" keys remain (40 of them, all within TTL).
        assert store.size() == 40
    finally:
        await store.close()


async def test_close_clears_state() -> None:
    store = InMemoryDedupStore()
    await store.record_unless_duplicate("k1", ttl_seconds=10)
    assert store.size() == 1
    await store.close()
    assert store.size() == 0
