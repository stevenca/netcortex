"""Single-process in-memory :class:`DedupStore`.

Asyncio-safe (one ``asyncio.Lock`` serializes mutations), TTL-bounded,
and size-bounded with LRU eviction so a misbehaving publisher cannot OOM
the runner. The implementation is small on purpose — production
multi-replica deployments use the Redis-backed store starting in 0.9.0,
and the in-memory version is meant for single-replica production,
single-process CI, and unit tests.

Operators MUST NOT deploy multi-replica reflex runners against this
store: there is no cross-process visibility, so two replicas would each
fire the reflex for the same event. The runtime config code that wires
the store is responsible for refusing this configuration, not this
class.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Final

_LOG = logging.getLogger(__name__)

#: Hard upper bound on entries we keep. Keys beyond this are LRU-evicted
#: even if not yet expired. The number is large enough that a normal
#: production cardinality (10k devices × 10 event classes × 30 active
#: facts per device) sits comfortably below; small enough that a
#: pathological 1000x explosion still fits in a few MB.
_DEFAULT_MAX_ENTRIES: Final[int] = 100_000

#: Warn if a caller asks for a TTL longer than this — long-lived dedup
#: state tends to mask real flap behavior the operator needs to see.
_LONG_TTL_WARN_SECONDS: Final[float] = 24 * 60 * 60


class InMemoryDedupStore:
    """Time-windowed dedup, single-process, asyncio-safe.

    Parameters
    ----------
    max_entries:
        Cap on stored keys. When exceeded, the LRU entry (the one we
        haven't touched longest) is evicted, even if its TTL has not
        expired. Default sized for normal production workloads — raise
        if you legitimately have more concurrent fact keys.
    clock:
        Override for the monotonic clock; tests pass a controllable
        clock so they can advance time without ``asyncio.sleep``.
        Production code passes nothing.
    """

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: "callable[..., float] | None" = None,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be > 0, got {max_entries}")
        self._max_entries = max_entries
        self._clock = clock or time.monotonic
        # OrderedDict tracks insertion order, which we mutate on every
        # access so dict iteration is LRU. value = expiry_monotonic.
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    async def record_unless_duplicate(
        self,
        fact_key: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Atomically record ``fact_key`` if absent. See Protocol docs."""
        if not fact_key:
            raise ValueError("fact_key must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")
        if self._closed:
            raise RuntimeError("InMemoryDedupStore.record_unless_duplicate after close()")
        if ttl_seconds > _LONG_TTL_WARN_SECONDS:
            _LOG.warning(
                "dedup_store.long_ttl ttl_seconds=%s fact_key=%s",
                ttl_seconds, fact_key,
            )

        async with self._lock:
            now = self._clock()
            existing = self._entries.get(fact_key)
            if existing is not None and existing > now:
                # Touch for LRU — even a duplicate "refreshes interest".
                # This matters under sustained duplicate floods: if we
                # didn't touch, the duplicated key would age out for LRU
                # eviction faster than fresh keys, which is the wrong
                # direction.
                self._entries.move_to_end(fact_key)
                return False

            # Lazy GC: when we're at capacity OR have any stale entries
            # adjacent to insertion, sweep a bounded number of expired
            # heads off the LRU. Bounded so worst-case latency is O(B).
            self._sweep_expired(now, budget=32)

            # Enforce hard cap by evicting LRU(s) if needed.
            while len(self._entries) >= self._max_entries:
                evicted_key, evicted_exp = self._entries.popitem(last=False)
                _LOG.debug(
                    "dedup_store.evicted_lru key=%s expired_in_s=%s",
                    evicted_key, round(evicted_exp - now, 3),
                )

            self._entries[fact_key] = now + ttl_seconds
            return True

    def _sweep_expired(self, now: float, *, budget: int) -> None:
        """Evict up to ``budget`` expired entries from the LRU head.

        Called under lock. We sweep only from the head (oldest entries)
        because that's where expired keys cluster — the move_to_end on
        every touch means recently-touched keys are at the tail. A
        bounded sweep keeps tail latency predictable; the rare
        worst-case (all entries expired in one burst) just means future
        calls each chip away another 32 entries until the head is clean.
        """
        swept = 0
        while swept < budget and self._entries:
            head_key = next(iter(self._entries))
            head_exp = self._entries[head_key]
            if head_exp > now:
                return  # head is fresh — by LRU ordering, nothing behind is expired enough to matter for this sweep
            del self._entries[head_key]
            swept += 1

    async def close(self) -> None:
        """Drop all state. Idempotent."""
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------
    # Test helpers — not on the Protocol, do not depend on them in
    # production code.
    # ------------------------------------------------------------------

    def size(self) -> int:
        """Current number of tracked keys. Lock-free read for inspection."""
        return len(self._entries)


__all__ = ["InMemoryDedupStore"]
