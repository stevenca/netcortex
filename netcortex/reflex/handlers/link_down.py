"""``link_down`` — reflex handler for interface-down signals.

Subscribes to the SNMP linkDown trap subject. In the brain-mapped
architecture this is the fast deterministic response to an interface
going hard-down: log it now, let the deliberative loop (prefrontal,
0.11.0) decide whether to open a ticket, page someone, or just wait
for the symmetric linkUp.

This module is dev2 scaffolding — the handler is registered and the
runner will subscribe it to the bus, but no publisher exists yet. The
first real linkDown publish lands in 0.8.0-dev3 when the SNMP-trap
sensory adapter (``sensory/trap/snmp.py``) is wired in.

When publishers exist, the handler will also:

* fetch the affected (device, interface) from semantic memory and verify
  it is not in a maintenance window;
* deduplicate against a Redis "recently seen" window so a flapping link
  produces one outcome per minute, not one per trap;
* attach a NetBox journal entry on the Interface object so an operator
  sees the trap immediately in the tool they live in;
* emit a follow-up ``reflex.link_down.applied`` event so consolidation
  knows a synthetic interface-state transition has been recorded.

None of that is in dev2. The current implementation captures the trap,
extracts the target, and returns a ``logged`` outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.reflex.protocol import ReflexOutcome
from netcortex.reflex.registry import register_handler

# Subject pattern.
#
# Real publishers in 0.8.0-dev3+ will use
# ``sensory.snmp.trap.link_down.<device_id>`` and emit one event per
# (device, interface) transition. The trailing ``>`` matches any number
# of further tokens so the handler can be subscribed today and the
# publisher's exact subject layout can evolve without redeploying the
# handler.
_PATTERN: Final[str] = "sensory.snmp.trap.link_down.>"


class LinkDownHandler:
    """Reflex for IF-MIB linkDown traps."""

    id: Final[str] = "link_down"
    pattern: Final[str] = _PATTERN

    async def handle(self, event: EventMessage) -> ReflexOutcome | None:
        payload = event.payload
        target = (
            payload.get("device_id")
            or payload.get("device")
            or payload.get("target")
        )
        interface = payload.get("interface") or payload.get("if_name")
        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=str(target) if target else None,
            severity="high",
            occurred_at=datetime.now(tz=timezone.utc),
            payload={
                "interface": interface,
                # Cap the upstream payload echo so a chatty publisher
                # cannot blow up the outcome record.
                "upstream_keys": sorted(payload.keys())[:16],
            },
            outcome="logged",
            rationale=(
                f"linkDown observed on {target!r} interface {interface!r}; "
                "dev2 idle handler — no remediation yet"
            ),
        )


_HANDLER = register_handler(LinkDownHandler())
