"""Reference in-memory ``EventBus`` implementation used by contract tests.

This is intentionally minimal. It exists so the contract test suite can run
from day one — before the real ``NatsEventBus`` lands. It is also useful for
single-process developer runs and unit tests that exercise downstream
consumers without standing up real NATS.

Semantics
---------
* At-least-once within a subscription session.
* No replay: subscribers joining after a publish do not see prior events.
* Subject wildcards: ``*`` matches exactly one token, ``>`` matches
  one-or-more remaining tokens.
* Independent subscribers — a slow consumer's per-subscription queue may
  fill (queue size 1024 by default) and additional publishes for that
  subscriber are dropped with a warning. Other subscribers are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from netcortex.contracts.event_bus import (
    EventBus,
    EventBusValidationError,
    EventMessage,
)

_LOG = logging.getLogger(__name__)
# NATS subject grammar: dot-separated tokens where each token is one or
# more printable, non-whitespace characters EXCLUDING the three reserved
# meta-characters: '.' (token separator), '*' (single-token wildcard),
# and '>' (multi-token wildcard). This admits real-world identifier
# characters like ':' (MACs), '|' (compound NetCortex targets), '/'
# (interface names like Gi0/1), and '+' (URL-encoded payloads).
#
# Earlier revisions of this validator only allowed [A-Za-z0-9_-] which
# silently rejected publishes like
# 'sensory.link_down.snmp_trap.r1|Gi0/1', leading to subjects-look-
# correct-but-publish-throws bugs. Match the canonical taxonomy in
# docs/architecture/subjects.md.
_VALID_PUBLISH_SUBJECT = re.compile(r"^[^.\s*>]+(?:\.[^.\s*>]+)*$")


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a NATS-style subject pattern into a regex."""
    if not pattern:
        raise EventBusValidationError("empty subscription pattern")
    tokens = pattern.split(".")
    parts: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == "*":
            parts.append(r"[^.]+")
        elif tok == ">":
            if i != len(tokens) - 1:
                raise EventBusValidationError("'>' must be the last token of a pattern")
            parts.append(r".+")
        elif re.fullmatch(r"[A-Za-z0-9_\-]+", tok):
            parts.append(re.escape(tok))
        else:
            raise EventBusValidationError(f"invalid pattern token: {tok!r}")
    return re.compile(r"^" + r"\.".join(parts) + r"$")


class _Subscription:
    __slots__ = ("pattern", "regex", "queue", "_closed")

    def __init__(self, pattern: str, *, max_queue: int = 1024) -> None:
        self.pattern = pattern
        self.regex = _compile_pattern(pattern)
        self.queue: asyncio.Queue[EventMessage] = asyncio.Queue(maxsize=max_queue)
        self._closed = False

    def deliver(self, message: EventMessage) -> None:
        if self._closed or not self.regex.match(message.subject):
            return
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            _LOG.warning(
                "in_memory_event_bus subscriber for %s dropped a message "
                "(queue full); slow consumer protection engaged.",
                self.pattern,
            )

    async def aiter(self) -> AsyncIterator[EventMessage]:
        try:
            while not self._closed:
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                yield item
        finally:
            self._closed = True

    def close(self) -> None:
        self._closed = True


class InMemoryEventBus(EventBus):
    """Process-local event bus suitable for tests and dev loops."""

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self._closed = False
        self._lock = asyncio.Lock()

    async def publish(
        self,
        subject: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self._closed:
            raise EventBusValidationError("event bus is closed")
        if not subject or not _VALID_PUBLISH_SUBJECT.match(subject):
            raise EventBusValidationError(
                f"invalid publish subject: {subject!r} "
                "(must be dot-separated [A-Za-z0-9_-]+ tokens, no wildcards)"
            )
        if not isinstance(payload, dict):
            raise EventBusValidationError(
                f"payload must be a dict, got {type(payload).__name__}"
            )
        message = EventMessage(
            subject=subject,
            payload=dict(payload),
            headers=dict(headers or {}),
            ts=datetime.now(tz=timezone.utc),
        )
        # Snapshot the subscriber list under the lock so newly-added
        # subscribers don't receive past events (no-replay guarantee).
        async with self._lock:
            subs = list(self._subscriptions)
        for sub in subs:
            sub.deliver(message)

    def subscribe(self, pattern: str) -> AsyncIterator[EventMessage]:
        sub = _Subscription(pattern)
        # Synchronous append is fine — list is process-local. The publish
        # path takes a lock to snapshot it.
        self._subscriptions.append(sub)
        return sub.aiter()

    async def close(self) -> None:
        # Idempotent per the Protocol contract.
        if self._closed:
            return
        self._closed = True
        for sub in self._subscriptions:
            sub.close()
        self._subscriptions.clear()
