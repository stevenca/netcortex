"""Coalescing scheduler for webhook-triggered adapter syncs (F4).

Webhooks are an *unauthenticated-amplification* vector before this layer:
a flood of webhook deliveries (or a single noisy device flapping) each
scheduled an independent ``adapter.discover()`` background task. A burst
could spawn unbounded concurrent discoveries, hammering the upstream
vendor API and exhausting the event loop / connection pools.

This module funnels every webhook-driven sync through a small scheduler
that guarantees, per adapter instance:

* **Single-flight** — at most one sync runs at a time.
* **Trailing coalesce** — deliveries that arrive while a sync is in
  flight collapse into a single follow-up run (we only ever need the
  latest authoritative state).
* **Minimum interval** — a given instance syncs at most once per
  ``_MIN_INTERVAL_S``; extra triggers are debounced.
* **Global concurrency cap** — across *all* instances, no more than
  ``_MAX_CONCURRENT`` syncs run simultaneously.

Handlers call :func:`schedule_sync` with the instance id and a zero-arg
factory that returns the sync coroutine.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

# Don't sync the same adapter instance more than once per this many seconds.
_MIN_INTERVAL_S = 10.0
# Hard ceiling on concurrent syncs across all instances.
_MAX_CONCURRENT = 4

_SyncFactory = Callable[[], Awaitable[None]]

_sem: asyncio.Semaphore | None = None
_inflight: dict[str, asyncio.Task] = {}
_pending: dict[str, _SyncFactory] = {}
_last_run: dict[str, float] = {}


def _semaphore() -> asyncio.Semaphore:
    # Created lazily so the module imports without a running event loop.
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(_MAX_CONCURRENT)
    return _sem


def schedule_sync(instance_id: str, factory: _SyncFactory) -> None:
    """Schedule a coalesced, rate-limited sync for ``instance_id``.

    Safe to call on every webhook delivery. If a sync is already running
    for this instance, the latest factory is stored and run once after
    the current one finishes (trailing coalesce).
    """
    if instance_id in _inflight:
        _pending[instance_id] = factory
        log.debug("webhook.sync.coalesced", instance_id=instance_id)
        return
    try:
        _inflight[instance_id] = asyncio.create_task(_run(instance_id, factory))
    except RuntimeError:
        # No running loop (e.g. a unit test calling a handler synchronously
        # without an event loop). Nothing to schedule.
        log.debug("webhook.sync.no_loop", instance_id=instance_id)


async def _run(instance_id: str, factory: _SyncFactory) -> None:
    try:
        elapsed = time.monotonic() - _last_run.get(instance_id, 0.0)
        if elapsed < _MIN_INTERVAL_S:
            await asyncio.sleep(_MIN_INTERVAL_S - elapsed)
        async with _semaphore():
            _last_run[instance_id] = time.monotonic()
            try:
                await factory()
            except Exception as exc:
                log.error("webhook.sync.failed", instance_id=instance_id, error=str(exc))
    finally:
        _inflight.pop(instance_id, None)
        nxt = _pending.pop(instance_id, None)
        if nxt is not None:
            schedule_sync(instance_id, nxt)


def _reset_for_tests() -> None:
    """Clear all scheduler state. Test-only."""
    global _sem
    _sem = None
    _inflight.clear()
    _pending.clear()
    _last_run.clear()


__all__ = ["schedule_sync"]
