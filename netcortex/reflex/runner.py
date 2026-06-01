"""Reflex runner — wires every registered handler to the event bus.

One :class:`ReflexRunner` instance owns one :class:`EventBus` connection
and one asyncio task per registered handler. Each task is a long-running
loop that pulls events from its handler's subscription and dispatches
them to the handler.

Failure isolation
-----------------
A bug in one handler MUST NOT take down the others, and a bug in one
event MUST NOT take down its handler's loop. The dispatch wrapper:

1. catches every exception the handler raises;
2. turns the exception into an :func:`ReflexOutcome` with
   ``outcome="errored"`` so the operator UI sees what failed and why;
3. logs the traceback at WARNING (not ERROR — handlers fail often during
   sensory-modality onboarding and a flood of ERRORs would mask real
   incidents);
4. continues with the next event.

Lifecycle
---------
:meth:`start` is idempotent: a second call returns immediately. :meth:`stop`
cancels every per-handler task, drains the bus, and waits for the tasks to
unwind. Callers that need to know the runner is fully up before publishing
test events can ``await runner.ready_event.wait()``.

In dev2 the outcomes are only logged — the Neo4j ``:ReflexEvent`` persistence
path lands with the first real publisher in 0.8.0-dev3. Until then the
runner is fully functional but every outcome surfaces as a structured log
line, which is enough to integration-test the wiring end-to-end against
``InMemoryEventBus``.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone

from netcortex.contracts.event_bus import EventBus, EventMessage
from netcortex.reflex.protocol import ReflexContext, ReflexHandler, ReflexOutcome
from netcortex.reflex.registry import all_handlers

_LOG = logging.getLogger(__name__)


class ReflexRunner:
    """Drives the registered handler set against one :class:`EventBus`.

    Parameters
    ----------
    bus:
        The bus to subscribe against. The runner does NOT own the bus —
        the caller is responsible for ``bus.close()`` after the runner
        has stopped. This split keeps the runner reusable across short
        test buses and the long-lived in-cluster :class:`NatsEventBus`.
    handlers:
        Optional explicit handler list. If omitted, the runner enumerates
        the registry (the common case). Tests use the explicit form to
        isolate runs from the global registry.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        handlers: list[ReflexHandler] | None = None,
        context: ReflexContext | None = None,
    ) -> None:
        self._bus = bus
        self._handlers: list[ReflexHandler] = (
            list(handlers) if handlers is not None else list(all_handlers())
        )
        # Default context has every shared resource set to None — the
        # opt-out path for handlers that don't need any of them. The
        # production wiring (web pod / worker pod) replaces this with a
        # context that has dedup_store + future memory layers attached.
        self._context: ReflexContext = context or ReflexContext()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._stopping = False
        # Set after every handler's subscription task has been spawned;
        # tests use this to avoid the "publish before subscribe" race.
        self.ready_event = asyncio.Event()
        # The runner records every outcome it dispatches. In dev2 this is
        # the only persistence path; dev3+ replaces this with a Neo4j
        # write that still keeps the in-memory copy for the operator
        # status endpoint.
        self.outcomes: list[ReflexOutcome] = []

    @property
    def handlers(self) -> list[ReflexHandler]:
        """Snapshot of handlers this runner is driving (read-only)."""
        return list(self._handlers)

    @property
    def context(self) -> ReflexContext:
        """The :class:`ReflexContext` every handler will receive on dispatch."""
        return self._context

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Spawn one subscription task per handler.

        Idempotent: a second call is a no-op. Returns when every handler
        has its subscription task scheduled; that does not necessarily
        mean every subscription has been acknowledged by the bus.
        Use ``await runner.ready_event.wait()`` plus a small grace period
        if you need cross-handler ordering guarantees.
        """
        if self._started:
            return
        self._started = True
        for handler in self._handlers:
            task = asyncio.create_task(
                self._consume(handler),
                name=f"reflex:{handler.id}",
            )
            self._tasks.append(task)
            _LOG.info(
                "reflex.runner.handler_started id=%s pattern=%s",
                handler.id,
                handler.pattern,
            )
        self.ready_event.set()

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel every handler task and wait for them to unwind.

        ``timeout`` bounds the wait so a buggy handler that swallows
        ``CancelledError`` cannot wedge shutdown. Tasks still running
        after the timeout are logged and abandoned.
        """
        if not self._started or self._stopping:
            return
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                stuck = [t for t in self._tasks if not t.done()]
                _LOG.warning(
                    "reflex.runner.stop_timeout stuck_tasks=%d names=%s",
                    len(stuck),
                    [t.get_name() for t in stuck],
                )
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Per-handler loop
    # ------------------------------------------------------------------
    async def _consume(self, handler: ReflexHandler) -> None:
        """Drive one handler. Runs until cancelled or the bus closes."""
        try:
            async for event in self._bus.subscribe(handler.pattern):
                await self._dispatch_one(handler, event)
        except asyncio.CancelledError:
            # Normal shutdown path — re-raise so the task records as cancelled.
            raise
        except Exception as exc:
            # Subscription itself broke (bus down, etc.). Log loudly; the
            # supervising deployment will restart the pod.
            _LOG.error(
                "reflex.runner.subscription_failed handler=%s pattern=%s error=%s",
                handler.id,
                handler.pattern,
                exc,
            )

    async def _dispatch_one(
        self,
        handler: ReflexHandler,
        event: EventMessage,
    ) -> None:
        """Invoke one handler safely and persist its outcome."""
        try:
            outcome = await handler.handle(event, self._context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = ReflexOutcome(
                handler=handler.id,
                subject=event.subject,
                target=str(event.payload.get("target") or "") or None,
                severity="high",
                occurred_at=datetime.now(tz=timezone.utc),
                payload={},
                outcome="errored",
                rationale=f"handler raised {type(exc).__name__}",
                # Cap the traceback so a runaway recursion can't blow up
                # the operator UI. 4KB is plenty for a useful diag.
                diagnostic={"traceback": traceback.format_exc()[-4096:]},
            )
            _LOG.warning(
                "reflex.runner.handler_raised handler=%s subject=%s error=%s",
                handler.id,
                event.subject,
                exc,
            )
        if outcome is None:
            return
        self.outcomes.append(outcome)
        _LOG.info(
            "reflex.outcome handler=%s subject=%s target=%s severity=%s outcome=%s",
            outcome.handler,
            outcome.subject,
            outcome.target,
            outcome.severity,
            outcome.outcome,
        )


__all__ = ["ReflexRunner"]
