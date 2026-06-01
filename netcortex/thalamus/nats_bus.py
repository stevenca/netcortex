"""NATS-backed :class:`EventBus` implementation.

This is the production thalamus. It satisfies the
:class:`netcortex.contracts.event_bus.EventBus` Protocol against a running
NATS server (in cluster: the StatefulSet from ``deploy/helm/templates/``;
in tests: a service container on ``nats://localhost:4222``).

Why NATS core, not JetStream
----------------------------
The Protocol guarantees **at-least-once delivery within a subscription
session, no replay for late subscribers**. NATS core pub/sub matches that
semantic exactly. JetStream is enabled at the SERVER level so future
durable consumers (episodic-memory writer in 0.9.0, stream bridge for
external agents in 0.9.x) can opt in via extension methods without
redeploying the cluster, but the basic Protocol surface stays at-least-once
for portability with ``InMemoryEventBus`` and any future
``RedisEventBus`` / ``KafkaEventBus`` backends.

Wire format
-----------
Payloads are JSON-encoded UTF-8 bytes on the wire. Headers (NATS server
2.2+) carry framing metadata (correlation IDs, source adapter, schema
version) and are passed through subscribers verbatim. A subscriber sees
:class:`EventMessage` instances with the same shape regardless of backend.

Lifecycle
---------
The constructor is synchronous and does NOT open a connection — that
matches the ``Callable[[], EventBus]`` factory shape used by the contract
tests and lets a process build a bus before its event loop is running.
The first ``publish()`` or ``subscribe()`` call lazily connects.
``close()`` is idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from netcortex.contracts.event_bus import (
    EventBus,
    EventBusValidationError,
    EventMessage,
)

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient
    from nats.aio.subscription import Subscription as NATSSubscription

_LOG = logging.getLogger(__name__)

# Subject syntax — kept in sync with InMemoryEventBus so both backends reject
# the same set of malformed subjects (the contract tests publish these as
# negative cases and expect rejection from any conforming implementation).
# NATS subject grammar: dot-separated tokens where each token is one or
# more printable, non-whitespace characters EXCLUDING the three reserved
# meta-characters: '.' (token separator), '*' (single-token wildcard),
# and '>' (multi-token wildcard). This admits real-world identifier
# characters like ':' (MAC addresses), '|' (compound NetCortex targets
# like 'device|interface'), '/' (interface names like Gi0/1), and '+'.
# Earlier revisions used [A-Za-z0-9_-] which silently rejected
# realistic subjects; that mismatch is now fixed so the bus accepts
# everything the taxonomy in docs/architecture/subjects.md emits.
_VALID_PUBLISH_SUBJECT = re.compile(r"^[^.\s*>]+(?:\.[^.\s*>]+)*$")


def _validate_pattern(pattern: str) -> None:
    """Validate a subscribe pattern per the NATS subject grammar.

    Allows the two NATS wildcards: ``*`` (single token) and ``>`` (rest of
    subject; must be the last token). Empty patterns and anything with
    whitespace are rejected. Matches the in-memory bus exactly.
    """
    if not pattern:
        raise EventBusValidationError("empty subscription pattern")
    tokens = pattern.split(".")
    for i, tok in enumerate(tokens):
        if tok == "*":
            continue
        if tok == ">":
            if i != len(tokens) - 1:
                raise EventBusValidationError("'>' must be the last token of a pattern")
            continue
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", tok):
            raise EventBusValidationError(f"invalid pattern token: {tok!r}")


def _decode_payload(data: bytes | None) -> dict[str, Any]:
    """Decode a NATS message body into the dict payload our Protocol promises.

    Empty bodies become empty dicts (callers commonly publish ``{}`` as a
    signalling-only message). Bodies that are not valid JSON objects raise a
    runtime warning and are surfaced as ``{"_raw": "<repr>"}`` so the
    subscriber sees something, can log it, and can continue — losing one
    malformed message must not break the consumer loop.
    """
    if not data:
        return {}
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _LOG.warning("nats_event_bus malformed payload: %s", exc)
        return {"_raw": repr(data[:256])}
    if not isinstance(decoded, dict):
        _LOG.warning(
            "nats_event_bus non-dict payload: %s (wrapping under '_payload')",
            type(decoded).__name__,
        )
        return {"_payload": decoded}
    return decoded


def _decode_headers(raw: Any) -> dict[str, str]:
    """Normalize header containers from nats-py into a plain ``dict[str, str]``.

    nats-py returns headers as ``dict[str, str] | None``; the Protocol
    promises plain ``dict[str, str]`` so we never propagate ``None``.
    """
    if not raw:
        return {}
    return {str(k): str(v) for k, v in raw.items()}


class NatsEventBus(EventBus):
    """:class:`EventBus` backed by a NATS server.

    Parameters
    ----------
    url:
        NATS connection URL, e.g. ``nats://netcortex-nats:4222`` in cluster
        or ``nats://localhost:4222`` in CI / local dev.
    name:
        Optional connection name surfaced in ``/connz`` for operator
        visibility. Defaults to ``"netcortex"``.
    max_reconnect_attempts:
        Forwarded to nats-py. ``-1`` means retry forever, which is what we
        want in cluster — NATS is the substrate, losing it means we're
        broken regardless. Tests can override to fail fast.
    connect_timeout:
        Seconds to wait for the initial TCP/handshake before raising.
    """

    def __init__(
        self,
        url: str,
        *,
        name: str = "netcortex",
        max_reconnect_attempts: int = -1,
        connect_timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._name = name
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connect_timeout = connect_timeout
        self._nc: NATSClient | None = None
        self._closed = False
        self._connect_lock = asyncio.Lock()
        self._subscriptions: set[NATSSubscription] = set()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    async def _ensure_connected(self) -> None:
        """Lazily open the NATS connection on first use.

        Guarded by a lock so concurrent publish/subscribe callers don't race
        to open multiple connections. Idempotent — if the connection is
        already up, returns immediately.
        """
        if self._closed:
            raise EventBusValidationError("event bus is closed")
        nc = self._nc
        if nc is not None and nc.is_connected:
            return
        async with self._connect_lock:
            if self._nc is not None and self._nc.is_connected:
                return
            try:
                # Local import keeps nats-py out of the import path for
                # processes that don't actually use NATS (e.g. some scripts
                # that only need InMemoryEventBus).
                import nats
            except ImportError as exc:  # pragma: no cover - covered by req
                raise EventBusValidationError(
                    "nats-py is not installed; add 'nats-py>=2.6' to dependencies"
                ) from exc
            self._nc = await nats.connect(
                servers=[self._url],
                name=self._name,
                max_reconnect_attempts=self._max_reconnect_attempts,
                connect_timeout=self._connect_timeout,
            )
            _LOG.info("nats_event_bus connected url=%s name=%s", self._url, self._name)

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------
    async def publish(
        self,
        subject: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        # Validate BEFORE connecting so the contract test for invalid
        # subjects passes even if the bus has never been used.
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

        await self._ensure_connected()
        body = json.dumps(payload, default=str).encode("utf-8")
        assert self._nc is not None  # for type narrowing — set by _ensure_connected
        await self._nc.publish(subject, body, headers=headers or None)

    def subscribe(self, pattern: str) -> AsyncIterator[EventMessage]:
        # Pattern validation is eager: the contract test for invalid
        # patterns expects the error at subscribe time, not at first iter.
        _validate_pattern(pattern)
        return self._iter_subscription(pattern)

    async def _iter_subscription(self, pattern: str) -> AsyncIterator[EventMessage]:
        """Async generator that drives one NATS subscription.

        The actual ``await nc.subscribe(...)`` happens on first iteration,
        not at ``subscribe()`` call time, because the Protocol returns a
        sync-call ``AsyncIterator``. The 50ms warm-up sleep already present
        in the contract tests covers the round-trip to the server.

        On generator close (``aclose()`` or break) the subscription is
        unsubscribed cleanly so an idle subscriber does not retain server
        resources.
        """
        await self._ensure_connected()
        assert self._nc is not None
        sub = await self._nc.subscribe(pattern)
        self._subscriptions.add(sub)
        try:
            async for msg in sub.messages:
                yield EventMessage(
                    subject=msg.subject,
                    payload=_decode_payload(msg.data),
                    headers=_decode_headers(msg.headers),
                    ts=datetime.now(tz=timezone.utc),
                )
        finally:
            self._subscriptions.discard(sub)
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._subscriptions):
            with contextlib.suppress(Exception):
                await sub.unsubscribe()
        self._subscriptions.clear()
        if self._nc is not None:
            with contextlib.suppress(Exception):
                # drain() flushes pending pubs and unsubscribes cleanly
                # before closing the socket. Safer than close() alone.
                await self._nc.drain()
            self._nc = None
