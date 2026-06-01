"""``security_webhook`` — reflex handler for Meraki security webhooks.

Subscribes to security-class Meraki Dashboard webhooks (IDS alerts,
malware, anomalous traffic, blocked URL hit, etc.) once the webhook
receiver in ``sensory/webhook/meraki.py`` lands (0.8.x patch). Dev2
ships the handler skeleton so the pattern is reserved and the runner's
subscription map is complete from day one.

When publishers exist, this handler will:

* dedupe via Meraki's ``alertId`` (a Redis "seen recently" set with a
  short TTL — Meraki retries the same alert on delivery failure);
* check whether the affected client is known to semantic memory; if not,
  promote it to the working-memory "unknown clients" set so the
  operator UI can surface unrecognized endpoints;
* compute a severity from the ``occurredAt`` + ``eventType`` cross
  product using a policy in ``policy/security_severity.py`` (so the
  threshold is operator-tunable, not hardcoded);
* attach a NetBox journal entry on the affected IPAddress/Interface
  when it can be resolved.

None of that is in dev2. The current implementation logs the inbound
event and returns an outcome whose severity comes verbatim from the
Meraki payload's ``severity`` field when present, defaulting to
``warn`` (Meraki's own scale runs informational/warning/critical; we
collapse to our four-bucket scale in the policy module later).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.reflex.protocol import ReflexOutcome, Severity
from netcortex.reflex.registry import register_handler

_PATTERN: Final[str] = "sensory.meraki.webhook.security.>"

# Coarse mapping for dev2. Real severity policy lives in
# policy/security_severity.py once the policy library lands (0.8.x).
_MERAKI_SEVERITY_MAP: Final[dict[str, Severity]] = {
    "informational": "info",
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "high": "high",
    "critical": "critical",
}


class SecurityWebhookHandler:
    """Reflex for Meraki security-class webhook events."""

    id: Final[str] = "security_webhook"
    pattern: Final[str] = _PATTERN

    async def handle(self, event: EventMessage) -> ReflexOutcome | None:
        payload = event.payload
        upstream_sev = str(payload.get("severity") or "").lower()
        severity: Severity = _MERAKI_SEVERITY_MAP.get(upstream_sev, "warn")
        target = (
            payload.get("clientMac")
            or payload.get("deviceSerial")
            or payload.get("networkId")
            or payload.get("target")
        )
        event_type = payload.get("alertType") or payload.get("eventType")
        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=str(target) if target else None,
            severity=severity,
            occurred_at=datetime.now(tz=timezone.utc),
            payload={
                "alert_id": payload.get("alertId"),
                "event_type": event_type,
                "network_id": payload.get("networkId"),
            },
            outcome="logged",
            rationale=(
                f"meraki security webhook {event_type!r} on {target!r}; "
                "dev2 idle handler — dedupe + classification pending"
            ),
        )


_HANDLER = register_handler(SecurityWebhookHandler())
