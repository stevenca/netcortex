"""``SensoryAdapter`` Protocol — the contract every input modality implements.

A sensory adapter is anything that produces :class:`SensoryEvent` instances
from the outside world. Today's poll adapters (Meraki, SNMP, NetBox,
ThousandEyes, FMC, Intersight), tomorrow's webhook receivers, SNMP trap
listeners, and Cisco streaming-telemetry consumers all implement the same
contract. Downstream code (thalamus normalization, association correlator,
reflex handlers) does not know or care which modality produced a given event.

This is what makes "add a new input modality" mechanically equivalent across
the three new sensory types described in ``docs/architecture/brain.md``:

    sensory/poll/<vendor>.py        # periodic discovery
    sensory/webhook/<vendor>.py     # HTTP push receiver
    sensory/trap/<vendor>.py        # SNMP trap listener
    sensory/telemetry/<vendor>.py   # gRPC subscription consumer

All four file types export a class that satisfies ``SensoryAdapter``.

Provenance
----------
Every :class:`SensoryEvent` carries a ``provenance`` chain. The first entry
is the adapter that observed the event; subsequent entries are added as the
event passes through transformation stages. Provenance is what makes
"how do we know X?" answerable after the fact and is required for the audit
trail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SensoryEvent:
    """A single perception emitted by a sensory adapter.

    Fields
    ------
    modality:
        Dotted vendor/transport identifier. Examples:
        ``"meraki.api"``, ``"meraki.webhook"``, ``"snmp.poll"``,
        ``"snmp.trap"``, ``"cisco.mdt"``, ``"netbox.api"``.
    received_at:
        When the adapter first observed the event.
    occurred_at:
        When the underlying network event actually happened. Equal to
        ``received_at`` for pull-mode adapters where no remote timestamp
        is available. For webhooks and traps this is the source-reported
        time and may lag ``received_at``.
    target:
        What the event is about. Free-form dict keyed by the natural
        identifier(s): ``{"device_id": "...", "interface": "..."}`` for an
        interface event, ``{"site_id": "..."}`` for a site event, etc.
    payload:
        The observation itself. Schema is per-modality.
    confidence:
        ``[0.0, 1.0]``. ``1.0`` for deterministic facts (an API response
        saying "port is up"); lower for inferred or correlated facts
        (e.g. derived from ARP). Consumers may filter on confidence.
    provenance:
        Append-only list of stage names that produced or transformed this
        event. First entry MUST be the adapter id.
    """

    modality: str
    received_at: datetime
    occurred_at: datetime
    target: dict[str, Any]
    payload: dict[str, Any]
    confidence: float = 1.0
    provenance: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class SensoryAdapter(Protocol):
    """Minimum surface every input modality must implement.

    Implementations are expected to:

    * Be re-entrant — multiple ``discover()`` calls from different tasks must
      not interfere.
    * Handle their own retry / backoff for transient transport failures.
    * Emit ``SensoryEvent`` instances with stable ``modality`` strings so
      subscribers can filter reliably.
    * Cancel cleanly when the consumer stops iterating.
    """

    #: Stable identifier for this adapter, e.g. ``"meraki_poll"`` or
    #: ``"snmp_trap_listener"``. Appears as the first entry in every emitted
    #: event's ``provenance``.
    adapter_id: str

    def discover(self) -> AsyncIterator[SensoryEvent]:
        """Yield events for as long as the adapter has more to produce.

        Pull-mode adapters typically terminate after one discovery sweep.
        Push-mode adapters (webhook, trap, telemetry) yield indefinitely
        until the consumer cancels.

        Implementations MUST NOT raise from ``discover()`` itself; they must
        either yield a degraded event with ``confidence=0.0`` (with a payload
        describing the failure) or stop the iteration. Raising bubbles up to
        the thalamus and stops the whole sensory pipeline — almost never
        what you want.
        """
        ...
