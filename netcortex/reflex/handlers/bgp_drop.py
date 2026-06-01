"""``bgp_drop`` — reflex handler for BGP session state-down signals.

Subscribes to the BGP4-MIB ``bgpBackwardTransition`` trap (and, once the
streaming telemetry adapter lands, also ``sensory.cisco.mdt.bgp.>``
neighbor-down samples). The fast deterministic response is to record a
session-down outcome so the deliberative loop (route convergence
analysis, prefix advertisement drift) has the wall-clock anchor.

This module is dev2 scaffolding. When publishers land in 0.8.0-dev3+ the
handler will additionally:

* resolve the peer IP against semantic memory's ``:BgpSession`` nodes so
  the outcome carries the canonical session identifier, not just the
  peer address;
* check whether the device is in a maintenance window OR the peer is a
  known-flapping route-server (operator policy);
* attach a NetBox journal entry to the BGP session object (once the
  reconciliation engine starts surfacing those — they are not first-
  class in NetBox today, so the journal will live on the device);
* trigger a deliberative follow-up to assess prefix-advertisement
  impact, comparing the last-known advertised prefix set on this
  session against the post-drop topology snapshot.

None of that is in dev2. The current handler logs and returns a
``high``-severity outcome; downstream consumers can already key off it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from netcortex.contracts.event_bus import EventMessage
from netcortex.reflex.protocol import ReflexOutcome
from netcortex.reflex.registry import register_handler

# Subject pattern.
#
# Real publishers will use
# ``sensory.snmp.trap.bgp_backward_transition.<device_id>`` or
# ``sensory.cisco.mdt.bgp_neighbor_state.<device_id>``. For dev2 the
# handler subscribes to the SNMP trap subject only; the second
# subscription (or a glob) lands once the telemetry adapter exists.
_PATTERN: Final[str] = "sensory.snmp.trap.bgp_backward_transition.>"


class BgpDropHandler:
    """Reflex for BGP session backward-transition (down) events."""

    id: Final[str] = "bgp_drop"
    pattern: Final[str] = _PATTERN

    async def handle(self, event: EventMessage) -> ReflexOutcome | None:
        payload = event.payload
        device = (
            payload.get("device_id")
            or payload.get("device")
            or payload.get("target")
        )
        peer = payload.get("peer") or payload.get("peer_ip")
        peer_asn = payload.get("peer_asn") or payload.get("remote_as")
        last_state = payload.get("last_state") or payload.get("previous_state")
        # Compose a target identifier that survives whether or not the peer
        # IP is known — preferring the canonical session "device|peer" key
        # when both are available, falling back to whichever is present.
        if device and peer:
            target = f"{device}|{peer}"
        elif peer:
            target = str(peer)
        elif device:
            target = str(device)
        else:
            target = None
        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=target,
            # BGP session loss is high-severity by default. Operators can
            # tune later via the policy library once it exists; we do not
            # second-guess severity inside the handler.
            severity="high",
            occurred_at=datetime.now(tz=timezone.utc),
            payload={
                "device": device,
                "peer": peer,
                "peer_asn": peer_asn,
                "last_state": last_state,
            },
            outcome="logged",
            rationale=(
                f"BGP session {target!r} backward-transition observed; "
                f"last_state={last_state!r} — dev2 idle handler"
            ),
        )


_HANDLER = register_handler(BgpDropHandler())
