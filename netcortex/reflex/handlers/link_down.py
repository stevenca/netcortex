"""``link_down`` — reflex handler for interface-down signals.

Subscribes to ``sensory.link_down.>`` so the same handler reacts to
every source of a link-down observation (SNMP trap, SNMP poll diff,
Meraki webhook, gNMI dial-out sample) without needing per-source
duplicate registrations.

Dedup
-----
The same physical link going down typically arrives multiple times
within a short window — the trap, then the Meraki webhook ~50ms later,
then the SNMP poll on its next 30-second pass. We collapse those to a
single fire via the runner-supplied :class:`DedupStore`. Per-handler
defaults documented in ``docs/architecture/subjects.md``.

Dev2 was idle (no publishers). Dev3 wires the dedup contract but is
still idle in terms of upstream publishers — the first real
``sensory.link_down.snmp_poll.<target>`` publish lands in 0.8.0-dev4
when the ingest worker starts dual-writing detected state changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.contracts.subjects import parse_sensory_subject
from netcortex.reflex.protocol import ReflexContext, ReflexOutcome
from netcortex.reflex.registry import register_handler

_PATTERN: Final[str] = "sensory.link_down.>"

_DEFAULT_DEDUP_WINDOW_SECONDS: Final[float] = 60.0


class LinkDownHandler:
    """Reflex for link-down observations from any source."""

    id: Final[str] = "link_down"
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
        event_class, source, _target_from_subject = parse_sensory_subject(
            event.subject
        )
        device = (
            payload.get("device_id")
            or payload.get("device")
            or payload.get("target")
        )
        interface = payload.get("interface") or payload.get("if_name")
        if device and interface:
            target: str | None = f"{device}|{interface}"
        elif device:
            target = str(device)
        else:
            target = None

        now = datetime.now(tz=timezone.utc)

        # Dedup check — only when both a store is provided AND we have
        # a canonical target. An event with no target identifier cannot
        # be meaningfully deduped (every arrival is treated as unique).
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
                    # Severity is intentionally demoted on the skipped
                    # outcome — it is informational corroboration, not a
                    # second incident.
                    severity="info",
                    occurred_at=now,
                    payload={"source": source},
                    outcome="skipped",
                    rationale=(
                        f"duplicate link_down within {self.dedup_window_seconds}s "
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
                "interface": interface,
                # Cap upstream key echo at 16 — same rationale as dev2
                # (a chatty publisher cannot blow up the outcome).
                "upstream_keys": sorted(payload.keys())[:16],
            },
            outcome="logged",
            rationale=(
                f"link_down on {target!r} from {source!r}; "
                "dev3 dedup wired, no remediation yet"
            ),
        )


_HANDLER = register_handler(LinkDownHandler())
