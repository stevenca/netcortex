"""``DedupStore`` Protocol — time-windowed deduplication for sensory events.

When the same real-world event arrives via multiple modalities (trap +
webhook + poll), reflex handlers consult a ``DedupStore`` to avoid firing
once per source. The store's only job is atomic check-and-record over a
TTL window.

Implementations
---------------
* ``netcortex.working.dedup.in_memory.InMemoryDedupStore`` — single-process,
  asyncio-safe, used everywhere in 0.8.0 (reflex runner, tests, dev) and
  the only implementation in CI.
* ``netcortex.working.dedup.redis.RedisDedupStore`` (lands in 0.9.0) —
  multi-replica-safe, uses ``SET key 1 NX EX ttl`` for atomic insert.

Both implementations conform to the same contract tests in
``tests/contracts/dedup_store/`` — Redis-backed tests skip unless
``REDIS_URL`` is set in the environment, matching the pattern dev1
established for ``NatsEventBus``.

Atomicity guarantee
-------------------
``record_unless_duplicate`` MUST be atomic with respect to other concurrent
calls for the same ``fact_key``. Without this, two concurrent observations
could both pass the check and both fire the reflex — which is the exact
race we're trying to suppress.

Redis gives us this for free via ``SET NX``. The in-memory implementation
uses an ``asyncio.Lock`` per call. Future implementations must document
their atomicity story.

Cross-process atomicity is only required of multi-replica stores; the
in-memory store is by definition single-process and operators MUST NOT
deploy multi-replica reflex runners with the in-memory store. The
runtime config that picks the store is responsible for enforcing this.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DedupStore(Protocol):
    """Atomic check-and-record over a TTL window, keyed by string.

    The store is not a general key-value cache — it intentionally exposes
    only the one operation the reflex layer needs, so adding new
    implementations is a small, well-scoped task.
    """

    async def record_unless_duplicate(
        self,
        fact_key: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Atomically record ``fact_key`` if it is not already present.

        Returns ``True`` when this call recorded the key (i.e. the event
        is **new** within the current TTL window — the caller should
        proceed). Returns ``False`` when the key was already present
        (the event is a **duplicate** — the caller should short-circuit).

        ``ttl_seconds`` MUST be > 0. The store rounds sub-millisecond
        precision down. Very long TTLs (> 1 day) are accepted but a
        warning is logged because long-lived dedup state tends to mask
        real flap behavior.

        The atomicity guarantee applies per ``fact_key`` only. Different
        ``fact_key`` values may interleave freely.

        Raises
        ------
        ValueError
            If ``fact_key`` is empty or ``ttl_seconds`` <= 0.
        """
        ...

    async def close(self) -> None:
        """Release any underlying resources.

        Idempotent: a second call is a no-op. After ``close()`` further
        calls to ``record_unless_duplicate`` raise ``RuntimeError``.
        In-memory implementations make this trivial; Redis-backed
        implementations close their connection pool here.
        """
        ...


__all__ = ["DedupStore"]
