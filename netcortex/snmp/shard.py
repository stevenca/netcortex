"""Consistent-hash sharding and Redis-backed liveness for SNMP pollers.

Each poller process registers itself in Redis with a heartbeat TTL.  A
target IP is owned by a poller iff:

    consistent_hash(ip) % total_live_pollers == my_shard_index

`my_shard_index` is reassigned dynamically as pollers join/leave, so the
total target population is always divided evenly with no manual config.

This is intentionally a lightweight DHT-style approach — we don't need
Raft, Zookeeper, etc.  Redis is already in the stack and gives us atomic
heartbeats with TTLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import time

import structlog

log = structlog.get_logger(__name__)

_KEY_PREFIX = "netcortex:snmp:poller"
_HEARTBEAT_TTL_S = 30
_HEARTBEAT_INTERVAL_S = 10


def _consistent_hash_int(value: str) -> int:
    """Stable hash of an arbitrary string to a non-negative int."""
    return int.from_bytes(
        hashlib.md5(value.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=False,
    )


def owns_target(poller_id: str, total: int, target: str,
                 cohort: list[str] | None = None) -> bool:
    """Decide whether this poller is responsible for `target`.

    Uses ordinal-modulo sharding: each poller in `cohort` has a stable
    position (its index in the sorted list), and a target is owned by the
    poller at `hash(target) % total`.  This gives perfectly even
    distribution as long as `cohort` is provided.

    `cohort` defaults to `[poller_id]` (single-node).  When called from
    `ShardRegistry.owns()` the registry passes its sorted live cohort
    so the assignment is stable across all pollers.
    """
    if total <= 0:
        return True  # single-node fallback
    if cohort is None:
        # Fallback when no cohort is known — compare poller ordinal vs target.
        # Two different pollers calling this with the same `total` get the
        # same `target % total` so only one returns True per target only when
        # ordinals are consistent; this fallback is only used in unit tests
        # via a synthetic cohort below.
        cohort = [poller_id]
    sorted_cohort = sorted(cohort)
    try:
        my_idx = sorted_cohort.index(poller_id)
    except ValueError:
        return True  # not yet registered → take everything until we are
    target_hash = _consistent_hash_int(target)
    return (target_hash % len(sorted_cohort)) == my_idx


def default_poller_id() -> str:
    """Generate a stable poller id for this process."""
    return os.environ.get(
        "SNMP_POLLER_ID",
        f"{socket.gethostname()}-{os.getpid()}",
    )


class ShardRegistry:
    """Heartbeat-based liveness tracking for SNMP pollers.

    Usage:
        reg = ShardRegistry()
        await reg.start()
        ...
        if reg.owns(target_ip):
            poll(target_ip)
        ...
        await reg.stop()
    """

    def __init__(self, poller_id: str | None = None) -> None:
        self.poller_id = poller_id or default_poller_id()
        self._task: asyncio.Task | None = None
        self._cli = None
        self._stopped = False
        self._live_pollers: list[str] = [self.poller_id]

    async def start(self) -> None:
        from netcortex.ingest.queue import _client
        try:
            self._cli = await _client()
        except Exception as exc:
            log.warning("snmp.shard.no_redis", error=str(exc))
            return
        self._task = asyncio.create_task(self._heartbeat_loop(),
                                          name=f"shard-hb-{self.poller_id}")
        # Best-effort initial registration so owns() is correct on first call
        await self._beat()

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # Best-effort: explicitly remove our key so other pollers re-shard fast
        if self._cli:
            try:
                await self._cli.delete(f"{_KEY_PREFIX}:{self.poller_id}")
            except Exception:
                pass

    async def _beat(self) -> None:
        """Renew heartbeat + refresh live-poller list."""
        if self._cli is None:
            return
        try:
            await self._cli.setex(
                f"{_KEY_PREFIX}:{self.poller_id}",
                _HEARTBEAT_TTL_S,
                str(int(time.time())),
            )
            # SCAN is O(N) but tiny — pollers in a shop are <100.
            pollers: list[str] = []
            cursor = 0
            while True:
                cursor, keys = await self._cli.scan(
                    cursor=cursor, match=f"{_KEY_PREFIX}:*", count=100,
                )
                for k in keys:
                    ks = k.decode() if isinstance(k, bytes) else k
                    pid = ks.rsplit(":", 1)[-1]
                    if pid:
                        pollers.append(pid)
                if cursor == 0:
                    break
            pollers.sort()
            if pollers and pollers != self._live_pollers:
                log.info("snmp.shard.cohort_changed",
                          old=self._live_pollers, new=pollers,
                          poller_id=self.poller_id)
            if pollers:
                self._live_pollers = pollers
        except Exception as exc:
            log.debug("snmp.shard.beat_failed", error=str(exc))

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopped:
                await self._beat()
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    @property
    def total(self) -> int:
        return len(self._live_pollers)

    @property
    def cohort(self) -> list[str]:
        return list(self._live_pollers)

    def owns(self, target: str) -> bool:
        """Return True iff this poller is responsible for `target`."""
        if not self._live_pollers:
            return True
        return owns_target(
            self.poller_id,
            len(self._live_pollers),
            target,
            cohort=self._live_pollers,
        )
