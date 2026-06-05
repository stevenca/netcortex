"""Meraki webhook payload → sensory event mapper.

Meraki Dashboard sends one webhook POST per alert with a top-level
``alertType`` and a vendor-specific ``alertData`` blob. This module is
the pure-function bridge from that vendor dialect to our closed
:data:`SENSORY_EVENT_CLASSES` vocabulary.

Why pure functions
------------------
The mapper has no I/O — no NATS, no Neo4j, no HTTP. It takes a parsed
payload and returns a list of structured events. That makes it trivial
to unit-test with sample payloads and equally easy to evolve when
Meraki adds a new alert type (just add a clause + a test).

Coverage in 0.8.0-dev8
----------------------
We deliberately ship a small, high-value initial set:

* **Port connectivity** (``port_connectivity``) → ``link_up`` /
  ``link_down``. This is the headline use case: the same port flap that
  the SNMP poller will detect in the next 30-second cycle now arrives
  in 50–200ms via webhook, and the dedup store added in 0.8.0-dev3
  collapses the duplicate into a single ``:ReflexEvent``.
* **Switch port connection changed** — same mapping, treated as a
  synonym (Meraki uses both names interchangeably).
* **IDS alert** (``ids_alerted``) → ``security_alert``.
* **Malware detected** → ``security_alert``.
* **Settings changed** / **Configuration change** → ``config_change``.
* **Uplink status change** → ``link_up`` / ``link_down`` (treating the
  WAN uplink as an interface; matches existing reflex handler pattern).

Everything else returns an empty list and emits a single
``webhook.meraki.unmapped_alert_type`` log line so we can spot new
types in production and add them in subsequent releases. We do
**not** publish a generic ``sensory.unknown.*`` event — that would be
unsubscribable noise.

Target shape
------------
Targets follow the existing convention from the SNMP publisher:

* Port events: ``meraki:<deviceSerial>|<portName>`` (or
  ``Port_<num>`` when portName is absent). The ``meraki:`` prefix
  matches how the Meraki adapter keys ``Device.id`` in Neo4j, so the
  ``:AFFECTS`` edge resolver added in 0.8.0-dev7 matches against
  ``Device.id`` and the edge lands cleanly.
* Device-level events: ``meraki:<deviceSerial>``.
* IDS events: ``meraki:<deviceSerial>|<clientMac>`` when client MAC is
  known, otherwise ``meraki:<deviceSerial>``.
* Network-level events with no specific device: ``meraki:<networkId>``.

Payload echo
------------
Every emitted event carries a normalized payload with:

* ``device`` (human name when present, else serial-prefixed form)
* ``device_id`` (always ``meraki:<serial>``)
* ``interface`` (port name verbatim, original whitespace preserved)
* ``meraki_alert_type`` (the raw alertType for forensics)
* ``meraki_network_id`` (for network/org-level grouping queries)
* ``occurred_at`` (Meraki's ``occurredAt``, ISO-8601)

The :class:`SensoryPublisher` will add ``source`` and ``recorded_at``
automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MerakiSensoryEvent:
    """One sensory event derived from a Meraki webhook payload.

    Instances of this class are pure data — the mapper produces them,
    the route consumes them and hands them to a
    :class:`SensoryPublisher`. Tests assert on the dataclass equality
    rather than on side effects.
    """

    event_class: str
    target_parts: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def map_meraki_payload(payload: dict[str, Any]) -> list[MerakiSensoryEvent]:
    """Map one Meraki webhook payload to zero-or-more sensory events.

    Returns an empty list for:

    * unrecognized ``alertType`` values (logged once at INFO so we can
      grow coverage based on real traffic)
    * payloads where the required identifiers (``deviceSerial`` /
      ``networkId``) are missing — without those we can't construct a
      meaningful target and would only produce subjects that no one
      subscribes to
    """
    alert_type_raw = payload.get("alertType") or payload.get("alertTypeId") or ""
    alert_type = str(alert_type_raw).strip()
    if not alert_type:
        log.info("webhook.meraki.no_alert_type", payload_keys=sorted(payload.keys())[:16])
        return []

    handler = _ROUTE.get(_normalize_alert_type(alert_type))
    if handler is None:
        log.info(
            "webhook.meraki.unmapped_alert_type",
            alert_type=alert_type,
            payload_keys=sorted(payload.keys())[:16],
        )
        return []
    return handler(payload, alert_type)


# ---------------------------------------------------------------------------
# Per-alert-type handlers
# ---------------------------------------------------------------------------


def _handle_port_connectivity(
    payload: dict[str, Any], alert_type: str
) -> list[MerakiSensoryEvent]:
    """Port up/down events.

    Meraki sends ``alertData.currentValue`` and ``previousValue`` as
    one of ``"Connected"`` / ``"Disconnected"``. We map on the new
    state only — the reflex layer (and ``:ReflexEvent`` queries) tell
    the up/down story over time without us baking the transition into
    the event_class.
    """
    alert_data = _alert_data(payload)
    device_serial = payload.get("deviceSerial") or alert_data.get("deviceSerial")
    if not device_serial:
        return []

    port_name = (
        alert_data.get("portName")
        or alert_data.get("port")
        or (f"Port {alert_data['portNum']}" if alert_data.get("portNum") else None)
    )
    if not port_name:
        log.info(
            "webhook.meraki.port_event_missing_port",
            alert_type=alert_type,
            device_serial=device_serial,
            alert_data_keys=sorted(alert_data.keys())[:16],
        )
        return []

    current = str(alert_data.get("currentValue") or "").strip().lower()
    if current == "connected":
        event_class = "link_up"
    elif current == "disconnected":
        event_class = "link_down"
    else:
        log.info(
            "webhook.meraki.port_event_unknown_state",
            alert_type=alert_type,
            device_serial=device_serial,
            current_value=current,
        )
        return []

    device_id = f"meraki:{device_serial}"
    device_name = payload.get("deviceName") or device_id
    target_parts = (device_id, str(port_name))
    return [
        MerakiSensoryEvent(
            event_class=event_class,
            target_parts=target_parts,
            payload={
                "device": device_name,
                "device_id": device_id,
                "interface": port_name,
                "meraki_alert_type": alert_type,
                "meraki_network_id": payload.get("networkId"),
                "occurred_at": payload.get("occurredAt"),
                "previous_state": alert_data.get("previousValue"),
                "current_state": alert_data.get("currentValue"),
            },
        )
    ]


def _handle_uplink_status(
    payload: dict[str, Any], alert_type: str
) -> list[MerakiSensoryEvent]:
    """WAN uplink up/down — same mapping as a switch port, but the
    "interface" is the uplink interface (``wan1`` / ``wan2`` / ``cellular``).
    """
    alert_data = _alert_data(payload)
    device_serial = payload.get("deviceSerial") or alert_data.get("deviceSerial")
    if not device_serial:
        return []

    uplink_name = (
        alert_data.get("uplinkName")
        or alert_data.get("uplink")
        or alert_data.get("interface")
        or "wan"
    )
    current = str(alert_data.get("currentValue") or alert_data.get("status") or "").strip().lower()
    # Meraki uplink states observed in the wild: "active" / "ready" /
    # "failed" / "not connected". We classify the binary down vs not-down.
    if current in {"active", "ready", "connected", "up"}:
        event_class = "link_up"
    elif current in {"failed", "not connected", "disconnected", "down"}:
        event_class = "link_down"
    else:
        log.info(
            "webhook.meraki.uplink_unknown_state",
            alert_type=alert_type,
            device_serial=device_serial,
            current_value=current,
        )
        return []

    device_id = f"meraki:{device_serial}"
    device_name = payload.get("deviceName") or device_id
    return [
        MerakiSensoryEvent(
            event_class=event_class,
            target_parts=(device_id, str(uplink_name)),
            payload={
                "device": device_name,
                "device_id": device_id,
                "interface": uplink_name,
                "meraki_alert_type": alert_type,
                "meraki_network_id": payload.get("networkId"),
                "occurred_at": payload.get("occurredAt"),
                "previous_state": alert_data.get("previousValue"),
                "current_state": alert_data.get("currentValue") or alert_data.get("status"),
            },
        )
    ]


def _handle_ids_alert(
    payload: dict[str, Any], alert_type: str
) -> list[MerakiSensoryEvent]:
    """IDS / IPS alerts from MX appliances."""
    alert_data = _alert_data(payload)
    device_serial = payload.get("deviceSerial") or alert_data.get("deviceSerial")
    network_id = payload.get("networkId") or alert_data.get("networkId")
    if not device_serial and not network_id:
        return []

    client_mac = (
        alert_data.get("clientMac")
        or alert_data.get("srcMac")
        or alert_data.get("src_mac")
    )
    # IDS targets prefer device-scoped form when we have one, falling
    # back to network-scoped. Including client MAC when known so the
    # dedup key naturally distinguishes per-client incidents.
    if device_serial:
        device_id = f"meraki:{device_serial}"
        target_parts: tuple[str, ...] = (
            (device_id, str(client_mac)) if client_mac else (device_id,)
        )
        device_name = payload.get("deviceName") or device_id
    else:
        target_parts = (f"meraki:{network_id}",) if not client_mac else (
            f"meraki:{network_id}", str(client_mac)
        )
        device_id = None
        device_name = payload.get("networkName") or f"meraki:{network_id}"

    return [
        MerakiSensoryEvent(
            event_class="security_alert",
            target_parts=target_parts,
            payload={
                "device": device_name,
                "device_id": device_id,
                "alertType": alert_type,
                "meraki_alert_type": alert_type,
                "meraki_network_id": network_id,
                "clientMac": client_mac,
                "deviceSerial": device_serial,
                "networkId": network_id,
                "occurred_at": payload.get("occurredAt"),
                "signature": alert_data.get("signature") or alert_data.get("signatureId"),
                "message": alert_data.get("message") or alert_data.get("description"),
                "severity_raw": alert_data.get("severity"),
            },
        )
    ]


def _handle_config_change(
    payload: dict[str, Any], alert_type: str
) -> list[MerakiSensoryEvent]:
    """Settings/configuration change events."""
    alert_data = _alert_data(payload)
    network_id = payload.get("networkId") or alert_data.get("networkId")
    device_serial = payload.get("deviceSerial") or alert_data.get("deviceSerial")
    if not network_id and not device_serial:
        return []

    if device_serial:
        device_id = f"meraki:{device_serial}"
        device_name = payload.get("deviceName") or device_id
        target_parts: tuple[str, ...] = (device_id,)
    else:
        device_id = None
        device_name = payload.get("networkName") or f"meraki:{network_id}"
        target_parts = (f"meraki:{network_id}",)

    return [
        MerakiSensoryEvent(
            event_class="config_change",
            target_parts=target_parts,
            payload={
                "device": device_name,
                "device_id": device_id,
                "meraki_alert_type": alert_type,
                "meraki_network_id": network_id,
                "occurred_at": payload.get("occurredAt"),
                "admin_email": alert_data.get("adminEmail") or alert_data.get("admin"),
                "change_summary": alert_data.get("description") or alert_data.get("changes"),
            },
        )
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alert_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload['alertData']`` as a dict, or ``{}`` if absent / malformed.

    Meraki sometimes nests the useful fields under ``alertData`` and
    sometimes lifts them to the top level. The handlers below tolerate
    both by looking up keys in ``alertData`` first and the payload as
    a fallback.
    """
    data = payload.get("alertData")
    return data if isinstance(data, dict) else {}


def _normalize_alert_type(alert_type: str) -> str:
    """Fold Meraki's two equivalent naming conventions into one key.

    Meraki sends both human ("Port connectivity") and machine
    ("port_connectivity") forms depending on the dashboard version and
    the specific alert. We lowercase, strip, and collapse spaces to
    underscores so a single ``_ROUTE`` entry handles both.
    """
    return alert_type.strip().lower().replace(" ", "_")


# Mapping from normalized alert_type to handler. Edit this dict to add
# new coverage — every entry MUST have a corresponding test in
# tests/webhooks/test_meraki_events.py.
_ROUTE: dict[str, Any] = {
    "port_connectivity": _handle_port_connectivity,
    "switch_port_connection_changed": _handle_port_connectivity,
    "uplink_status_change": _handle_uplink_status,
    "uplink_status": _handle_uplink_status,
    "ids_alerted": _handle_ids_alert,
    "ids_alert": _handle_ids_alert,
    "malware_detected": _handle_ids_alert,
    "settings_changed": _handle_config_change,
    "configuration_change": _handle_config_change,
    "configuration_changes": _handle_config_change,
}


__all__ = ["MerakiSensoryEvent", "map_meraki_payload"]
