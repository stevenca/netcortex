"""``bgp_drop`` — reflex handler for BGP session state-down signals.

Subscribes to ``sensory.bgp_drop.>`` so the same handler reacts to
every source of a BGP session backward-transition (SNMP trap, gNMI
neighbor-state sample, future RIB poll diff).

The event class was ``bgp_backward_transition`` in dev2's draft
taxonomy — renamed to the cleaner ``bgp_drop`` in dev3's event-class-
first taxonomy. See ``docs/architecture/subjects.md``.

Dedup
-----
A real BGP session drop typically produces one trap immediately and a
gNMI sample 1-2 seconds later. Both arrivals collapse to one fire via
the runner-supplied :class:`DedupStore`. Per-handler window default is
60 seconds — short enough that a real flap (drop, restore, drop within
a minute) collapses to one fact but a same-session re-drop after the
window fires fresh. Operators tune via constructor arg.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.contracts.subjects import parse_sensory_subject
from netcortex.reflex.protocol import ReflexContext, ReflexOutcome
from netcortex.reflex.registry import register_handler

_PATTERN: Final[str] = "sensory.bgp_drop.>"

_DEFAULT_DEDUP_WINDOW_SECONDS: Final[float] = 60.0


class BgpDropHandler:
    """Reflex for BGP session backward-transition (down) events."""

    id: Final[str] = "bgp_drop"
    pattern: Final[str] = _PATTERN

    def __init__(
        self,
        *,
        dedup_window_seconds: float = _DEFAULT_DEDUP_WINDOW_SECONDS,
    ) -> None:
        self.dedup_window_seconds = dedup_window_seconds

    async def handle(
        self, event: EventMessage, ctx: ReflexContext
    ) -> ReflexOutcome | None:
        payload = event.payload
        event_class, source, _ = parse_sensory_subject(event.subject)
        device = (
            payload.get("device_id")
            or payload.get("device")
            or payload.get("target")
        )
        peer = payload.get("peer") or payload.get("peer_ip")
        peer_asn = payload.get("peer_asn") or payload.get("remote_as")
        last_state = payload.get("last_state") or payload.get("previous_state")
        if device and peer:
            target: str | None = f"{device}|{peer}"
        elif peer:
            target = str(peer)
        elif device:
            target = str(device)
        else:
            target = None

        now = datetime.now(tz=timezone.utc)

        if ctx.dedup_store is not None and target is not None and event_class:
            fact_key = f"{event_class}|{target}"
            is_new = await ctx.dedup_store.record_unless_duplicate(
                fact_key, ttl_seconds=self.dedup_window_seconds
            )
            if not is_new:
                return ReflexOutcome(
                    handler=self.id,
                    subject=event.subject,
                    target=target,
                    severity="info",
                    occurred_at=now,
                    payload={"source": source},
                    outcome="skipped",
                    rationale=(
                        f"duplicate bgp_drop within {self.dedup_window_seconds}s "
                        f"dedup window (source={source!r})"
                    ),
                )

        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=target,
            severity="high",
            occurred_at=now,
            payload={
                "source": source,
                "device": device,
                "peer": peer,
                "peer_asn": peer_asn,
                "last_state": last_state,
            },
            outcome="logged",
            rationale=(
                f"bgp_drop on {target!r} from {source!r}; "
                f"last_state={last_state!r} — dev3 dedup wired"
            ),
        )


_HANDLER = register_handler(BgpDropHandler())
