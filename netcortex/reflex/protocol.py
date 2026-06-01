"""``ReflexHandler`` Protocol — the typed contract every reflex handler obeys.

Reflex handlers are the **fast path** through the brain. They subscribe to a
narrow NATS subject pattern, run deterministic logic in milliseconds, and
produce a :class:`ReflexOutcome` that downstream consumers (semantic memory
in 0.8.0-dev3+, episodic memory in 0.9.0, NetBox journal mirror) persist.

A handler must:

* declare a stable ``id`` (used as the registry key and as the
  ``handler`` field on the resulting ``ReflexOutcome``);
* declare exactly one NATS subject pattern it subscribes to (multiple
  patterns are intentionally not supported in 0.8.0-dev2 — register two
  handlers if you need two patterns; we'll revisit when a real use case
  demands it);
* implement ``handle(event)`` synchronously OR as an async coroutine that
  returns a :class:`ReflexOutcome` or ``None``.

Handlers MUST NOT block on slow I/O. Anything that needs to wait on a
remote system (LLM call, NetBox writeback, large Cypher query) belongs in
the prefrontal/conductor path (0.11.0), not in reflex.

See ``docs/architecture/brain.md`` for the role of reflex in the
brain-mapped architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from netcortex.contracts.dedup_store import DedupStore
from netcortex.contracts.event_bus import EventMessage

Severity = Literal["info", "warn", "high", "critical"]
"""Severity values understood by the persistence layer.

Kept deliberately small (four buckets) so downstream alerting/escalation
rules can pattern-match without parsing free-form strings. Add new values
here, not in handler source.
"""

OutcomeKind = Literal["logged", "applied", "skipped", "errored"]
"""What the handler actually did with the event.

* ``logged`` — handler observed the event and recorded a :class:`ReflexOutcome`
  but took no corrective action. The default for the dev2 idle handlers.
* ``applied`` — handler took a corrective action (e.g., suppressed a noisy
  alert, opened a NetBox journal entry, kicked a remediation task).
* ``skipped`` — handler decided this event was not actionable (e.g., target
  not in semantic memory, or in a maintenance window).
* ``errored`` — handler raised. The runner converts the exception to this
  outcome rather than crashing the dispatcher.
"""


@dataclass(frozen=True)
class ReflexOutcome:
    """Result of one reflex firing.

    In 0.8.0-dev2 these are only logged. In 0.8.0-dev3+ the runner
    persists them as ``:ReflexEvent`` nodes in semantic memory (Neo4j),
    with an ``:AFFECTS`` edge to the target entity if it is known to the
    graph. When the affected entity is a Device/Interface/IPAddress that
    NetBox knows about, a NetBox journal entry mirrors the outcome so
    operators see it without leaving their tool of choice.

    The dataclass is frozen so an outcome cannot mutate between the handler
    returning it and the runner persisting it — that prevents an entire
    class of "the value I saw at log time differs from what I wrote to
    the graph" bugs that have historically bitten correlator code.
    """

    handler: str
    """The handler id that produced this outcome. Stable across releases."""

    subject: str
    """The NATS subject the event was published on."""

    target: str | None
    """Identifier of the affected entity (``platform_id``, NetBox device
    name, BGP peer IP, etc.). ``None`` if the event has no clear target.
    Used to attach an ``:AFFECTS`` edge in semantic memory."""

    severity: Severity
    occurred_at: datetime
    payload: dict[str, Any]
    """Verbatim subset of the originating event payload that the handler
    deemed relevant. Kept small (handlers should not echo entire payloads)
    so downstream storage doesn't bloat."""

    outcome: OutcomeKind = "logged"
    rationale: str = ""
    """Free-form short explanation, intended for the operator UI. Keep
    under 200 characters; long explanations belong in the rationale-text
    field of the eventual :class:`netcortex.contracts.policy.Decision`."""

    diagnostic: dict[str, Any] = field(default_factory=dict)
    """Optional bag of fields the handler wants attached to the outcome
    for debugging (e.g., the matched threshold value, the policy version
    used). Not surfaced in the operator UI by default."""


@dataclass(frozen=True)
class ReflexContext:
    """Runtime dependencies a handler may use during ``handle()``.

    Threaded through by the :class:`netcortex.reflex.runner.ReflexRunner`
    so handlers can consult shared resources (the dedup store today;
    semantic memory, working memory, and the policy engine in later
    releases) without each one carrying its own constructor wiring.

    Frozen so a handler cannot replace another handler's view of the
    world mid-dispatch. New shared resources are added by appending
    fields here; handlers that don't consume them are unaffected. That
    forward-compatibility is the whole point of using a context dataclass
    rather than positional arguments.

    All fields default to ``None`` so the runner can be constructed
    without ever wiring a context (the default-context path) and tests
    can pass partial contexts that exercise only the resources they
    care about.
    """

    dedup_store: DedupStore | None = None
    """If set, handlers should consult it via
    :meth:`DedupStore.record_unless_duplicate` to suppress duplicate
    arrivals of the same logical event (e.g. trap + webhook + poll all
    observing one interface going down). Handlers that opt out of dedup
    — because their event class is inherently de-duplicated upstream —
    may ignore this field."""


@runtime_checkable
class ReflexHandler(Protocol):
    """Minimum surface every reflex handler must expose.

    Concrete handlers register via
    :func:`netcortex.reflex.registry.register_handler`. The registry is the
    only place these are enumerated; the runner discovers them from there.
    """

    @property
    def id(self) -> str:
        """Stable handler identifier (``"link_down"``, ``"bgp_drop"``, …).

        Used as the registry key AND as the ``handler`` field on every
        resulting :class:`ReflexOutcome`. MUST be unique across the
        loaded handler set; the registry rejects duplicates.
        """
        ...

    @property
    def pattern(self) -> str:
        """NATS subject pattern this handler subscribes to.

        Validated by the runner against the same grammar
        :class:`netcortex.thalamus.NatsEventBus` enforces. One pattern per
        handler in 0.8.0-dev3.

        Patterns follow the event-class-first taxonomy documented in
        ``docs/architecture/subjects.md`` —
        ``sensory.<event_class>.<source>.<target...>`` —  so a single
        wildcard like ``sensory.link_down.>`` catches every source of a
        link-down observation.
        """
        ...

    async def handle(
        self,
        event: EventMessage,
        ctx: ReflexContext,
    ) -> ReflexOutcome | None:
        """Process one event.

        Returning :class:`ReflexOutcome` instructs the runner to log /
        persist it. Returning ``None`` means the handler observed the
        event but consciously chose not to surface an outcome (e.g., the
        event didn't actually represent the condition the handler cares
        about despite matching the pattern). Returning ``None`` is NOT a
        way to silently skip an error — raise instead, and the runner
        will convert it to an ``errored`` outcome with the traceback in
        ``diagnostic``.

        ``ctx`` carries shared runtime resources (dedup store and, in
        later releases, semantic memory / working memory / policy). A
        handler that consults the dedup store and finds the event is a
        duplicate SHOULD return a ``ReflexOutcome`` with
        ``outcome="skipped"`` and a ``rationale`` naming the dedup
        window — the skipped outcome is itself useful telemetry
        (corroboration count, visibility gaps) so the runner persists
        it the same as any other outcome.
        """
        ...
