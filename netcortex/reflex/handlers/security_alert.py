"""``security_alert`` — reflex handler for security webhook events.

Renamed from ``security_webhook`` (dev2) to ``security_alert`` in dev3
to reflect the event-class-first taxonomy: the handler subscribes to
``sensory.security_alert.>`` and reacts to every webhook source
(Meraki today; Cisco AMP, future SIEM webhooks tomorrow) without
needing per-source registrations.

Dedup
-----
Meraki retries webhook delivery on failure (typically 2-3 retries spaced
over a minute or two), so the same ``alertId`` can arrive several times
within a few minutes. Default dedup window is 300 seconds — long enough
to absorb the retry pattern, short enough that distinct alerts on the
same target later in the day still fire fresh.

Note that ``alertId`` is the *upstream* dedup identifier; we still
construct our own ``fact_key`` from (event_class + target) so the same
underlying incident observed via Meraki AND via a future Cisco AMP
webhook would still collapse to one outcome — assuming both publishers
agree on the canonical target. Identity reconciliation across webhook
sources is a 0.9.0 fusion-layer concern; in 0.8.0 we accept some
slippage (e.g., Meraki using clientMac while AMP uses clientIp).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.contracts.subjects import parse_sensory_subject
from netcortex.reflex.protocol import ReflexContext, ReflexOutcome, Severity
from netcortex.reflex.registry import register_handler

_PATTERN: Final[str] = "sensory.security_alert.>"

_DEFAULT_DEDUP_WINDOW_SECONDS: Final[float] = 300.0

# Coarse mapping for dev3. Real severity policy lives in
# policy/security_severity.py once the policy library lands (0.8.x).
_MERAKI_SEVERITY_MAP: Final[dict[str, Severity]] = {
    "informational": "info",
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "high": "high",
    "critical": "critical",
}


class SecurityAlertHandler:
    """Reflex for security-class webhook events."""

    id: Final[str] = "security_alert"
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
        upstream_sev = str(payload.get("severity") or "").lower()
        severity: Severity = _MERAKI_SEVERITY_MAP.get(upstream_sev, "warn")
        target = (
            payload.get("clientMac")
            or payload.get("deviceSerial")
            or payload.get("networkId")
            or payload.get("target")
        )
        target = str(target) if target else None
        event_type = payload.get("alertType") or payload.get("eventType")

        now = datetime.now(tz=timezone.utc)

        if ctx.dedup_store is not None and target is not None and event_class:
            # Include event_type in the fact_key when present so two
            # different alert types on the same client don't dedup with
            # each other.
            fact_key_parts = [event_class, target]
            if event_type:
                fact_key_parts.append(str(event_type))
            fact_key = "|".join(fact_key_parts)
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
                    payload={"source": source, "event_type": event_type},
                    outcome="skipped",
                    rationale=(
                        f"duplicate security_alert within {self.dedup_window_seconds}s "
                        f"dedup window (source={source!r}, event_type={event_type!r})"
                    ),
                )

        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=target,
            severity=severity,
            occurred_at=now,
            payload={
                "source": source,
                "alert_id": payload.get("alertId"),
                "event_type": event_type,
                "network_id": payload.get("networkId"),
            },
            outcome="logged",
            rationale=(
                f"security_alert {event_type!r} on {target!r} from {source!r}; "
                "dev3 dedup wired"
            ),
        )


_HANDLER = register_handler(SecurityAlertHandler())
