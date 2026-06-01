"""Contract suite for :class:`netcortex.contracts.dedup_store.DedupStore`.

Every implementation registered in
``tests/contracts/conftest.py::DEDUP_STORE_IMPLEMENTATIONS`` runs against
the same assertions. A future Redis-backed implementation only needs a
factory function and a row in the registry to gain full coverage.

The cases below are the **observable** behavior the Protocol promises;
anything implementation-specific (LRU cap, eviction sweep, etc.) lives
in the implementation's own unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from netcortex.contracts import DedupStore

pytestmark = pytest.mark.asyncio


async def test_first_call_returns_true(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    store = dedup_store_factory()
    try:
        assert await store.record_unless_duplicate("k1", ttl_seconds=1.0) is True
    finally:
        await store.close()


async def test_second_call_within_ttl_returns_false(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    store = dedup_store_factory()
    try:
        assert await store.record_unless_duplicate("k1", ttl_seconds=5.0) is True
        assert await store.record_unless_duplicate("k1", ttl_seconds=5.0) is False
    finally:
        await store.close()


async def test_different_keys_are_independent(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    store = dedup_store_factory()
    try:
        assert await store.record_unless_duplicate("k1", ttl_seconds=5.0) is True
        assert await store.record_unless_duplicate("k2", ttl_seconds=5.0) is True
        # ``k1`` and ``k2`` did not collide.
        assert await store.record_unless_duplicate("k1", ttl_seconds=5.0) is False
        assert await store.record_unless_duplicate("k2", ttl_seconds=5.0) is False
    finally:
        await store.close()


async def test_call_after_ttl_expiry_returns_true(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    """Once the TTL window closes, the key is treatable as new again."""
    store = dedup_store_factory()
    try:
        assert await store.record_unless_duplicate("k1", ttl_seconds=0.1) is True
        # Sleep ~3x the TTL so the window has definitely closed even on
        # heavily-loaded CI runners. Real-clock dependency is acceptable
        # here because the TTL is short.
        await asyncio.sleep(0.35)
        assert await store.record_unless_duplicate("k1", ttl_seconds=0.1) is True
    finally:
        await store.close()


async def test_empty_key_rejected(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    """An empty fact_key is almost always a publisher bug — fail loud."""
    store = dedup_store_factory()
    try:
        with pytest.raises(ValueError):
            await store.record_unless_duplicate("", ttl_seconds=1.0)
    finally:
        await store.close()


@pytest.mark.parametrize("bad_ttl", [0.0, -1.0, -0.001])
async def test_non_positive_ttl_rejected(
    dedup_store_factory: Callable[[], DedupStore],
    bad_ttl: float,
) -> None:
    """A non-positive TTL is meaningless and likely a config error."""
    store = dedup_store_factory()
    try:
        with pytest.raises(ValueError):
            await store.record_unless_duplicate("k1", ttl_seconds=bad_ttl)
    finally:
        await store.close()


async def test_close_is_idempotent(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    store = dedup_store_factory()
    await store.close()
    await store.close()


async def test_use_after_close_raises(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    """The Protocol requires post-close calls to raise RuntimeError."""
    store = dedup_store_factory()
    await store.close()
    with pytest.raises(RuntimeError):
        await store.record_unless_duplicate("k1", ttl_seconds=1.0)


async def test_concurrent_callers_for_same_key_dedupe(
    dedup_store_factory: Callable[[], DedupStore],
) -> None:
    """The Protocol's atomicity guarantee — only one concurrent caller wins.

    Spawn many coroutines that all race on the same key simultaneously;
    exactly one must observe ``True`` and the rest must observe ``False``.
    A non-atomic implementation would let multiple racers all return True.
    """
    store = dedup_store_factory()
    try:
        results = await asyncio.gather(*[
            store.record_unless_duplicate("contested", ttl_seconds=10.0)
            for _ in range(50)
        ])
        assert sum(1 for r in results if r is True) == 1
        assert sum(1 for r in results if r is False) == 49
    finally:
        await store.close()
