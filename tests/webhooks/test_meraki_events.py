"""Unit tests for the Meraki webhook → sensory event mapper.

The mapper is a pure function so tests are blazingly fast and don't
need any fixtures beyond raw sample payloads.

Sample payload shapes are intentionally varied — the mapper has to
handle Meraki's habit of sending the same field under different keys
depending on the alert type and dashboard version (``portName`` vs
``port``, ``adminEmail`` vs ``admin``, ``alertType`` vs ``alertTypeId``).

All identifiers (deviceSerial, networkId) and email addresses are
fabricated and contain ``EXAMPLE`` or use RFC 5737 / RFC 2606
documentation values where applicable.
"""

from __future__ import annotations

from typing import Any

import pytest

from netcortex.webhooks.meraki_events import (
    MerakiSensoryEvent,
    map_meraki_payload,
)


# ---------------------------------------------------------------------------
# Port connectivity (the headline dev8 use case — dedup overlap with SNMP)
# ---------------------------------------------------------------------------


def test_port_connectivity_disconnected_emits_link_down() -> None:
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "alertTypeId": "port_connectivity",
        "networkId": "L_EXAMPLE001",
        "networkName": "main-office",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "deviceName": "cpn-arlington-ms1",
        "occurredAt": "2026-06-05T20:00:00Z",
        "alertData": {
            "portNum": "12",
            "portName": "Port 12",
            "previousValue": "Connected",
            "currentValue": "Disconnected",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_class == "link_down"
    assert ev.target_parts == ("meraki:Q4CD-EXAM-PLE1", "Port 12")
    assert ev.payload["device"] == "cpn-arlington-ms1"
    assert ev.payload["device_id"] == "meraki:Q4CD-EXAM-PLE1"
    assert ev.payload["interface"] == "Port 12"
    assert ev.payload["meraki_alert_type"] == "Port connectivity"
    assert ev.payload["meraki_network_id"] == "L_EXAMPLE001"
    assert ev.payload["previous_state"] == "Connected"
    assert ev.payload["current_state"] == "Disconnected"


def test_port_connectivity_connected_emits_link_up() -> None:
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "deviceName": "cpn-arlington-ms1",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portName": "Port 12",
            "currentValue": "Connected",
            "previousValue": "Disconnected",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "link_up"
    assert events[0].target_parts == ("meraki:Q4CD-EXAM-PLE1", "Port 12")


def test_port_connectivity_synonym_switch_port_connection_changed() -> None:
    """Meraki uses two interchangeable names for the same alert."""
    payload: dict[str, Any] = {
        "alertType": "Switch port connection changed",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portName": "Port 3",
            "currentValue": "Disconnected",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "link_down"


def test_port_connectivity_builds_port_name_from_portnum_when_missing() -> None:
    """Older payloads omit portName and only send numeric portNum."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portNum": "5",
            "currentValue": "Disconnected",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].target_parts == ("meraki:Q4CD-EXAM-PLE1", "Port 5")
    assert events[0].payload["interface"] == "Port 5"


def test_port_event_missing_device_serial_returns_empty() -> None:
    """No way to construct a meaningful target without the device serial."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portName": "Port 1",
            "currentValue": "Disconnected",
        },
    }
    assert map_meraki_payload(payload) == []


def test_port_event_missing_port_returns_empty() -> None:
    """No port identifier at all — skip rather than publish a malformed target."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {"currentValue": "Disconnected"},
    }
    assert map_meraki_payload(payload) == []


def test_port_event_unknown_state_returns_empty() -> None:
    """``currentValue`` of something we don't recognize is not silently mapped."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portName": "Port 1",
            "currentValue": "PartiallyOnline",
        },
    }
    assert map_meraki_payload(payload) == []


def test_port_event_device_name_falls_back_to_serial_form() -> None:
    """When deviceName is missing, payload['device'] falls back to the
    serial-prefixed form so the dev7 :AFFECTS resolver still matches
    on ``Device.id`` (which uses the same ``meraki:<serial>`` form)."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "portName": "Port 1",
            "currentValue": "Disconnected",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].payload["device"] == "meraki:Q4CD-EXAM-PLE1"
    assert events[0].payload["device_id"] == "meraki:Q4CD-EXAM-PLE1"


# ---------------------------------------------------------------------------
# Uplink status
# ---------------------------------------------------------------------------


def test_uplink_status_change_failed_emits_link_down() -> None:
    payload: dict[str, Any] = {
        "alertType": "Uplink status change",
        "deviceSerial": "Q4XX-EXAM-PLE2",
        "deviceName": "cpn-mx-arlington",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "uplinkName": "wan1",
            "currentValue": "failed",
            "previousValue": "active",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "link_down"
    assert events[0].target_parts == ("meraki:Q4XX-EXAM-PLE2", "wan1")
    assert events[0].payload["interface"] == "wan1"


def test_uplink_status_change_active_emits_link_up() -> None:
    payload: dict[str, Any] = {
        "alertType": "Uplink status change",
        "deviceSerial": "Q4XX-EXAM-PLE2",
        "networkId": "L_EXAMPLE001",
        "alertData": {
            "uplinkName": "wan2",
            "currentValue": "active",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "link_up"


# ---------------------------------------------------------------------------
# IDS / security
# ---------------------------------------------------------------------------


def test_ids_alerted_with_client_mac_emits_security_alert() -> None:
    payload: dict[str, Any] = {
        "alertType": "IDS alerted",
        "alertTypeId": "ids_alerted",
        "deviceSerial": "Q4XX-EXAM-PLE2",
        "deviceName": "cpn-mx-arlington",
        "networkId": "L_EXAMPLE001",
        "occurredAt": "2026-06-05T20:01:00Z",
        "alertData": {
            "clientMac": "aa:bb:cc:dd:ee:ff",
            "signature": "1:2008725:1",
            "message": "ET POLICY example",
            "severity": "high",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_class == "security_alert"
    assert ev.target_parts == ("meraki:Q4XX-EXAM-PLE2", "aa:bb:cc:dd:ee:ff")
    assert ev.payload["clientMac"] == "aa:bb:cc:dd:ee:ff"
    assert ev.payload["signature"] == "1:2008725:1"
    assert ev.payload["severity_raw"] == "high"


def test_ids_alerted_without_client_mac_uses_device_only_target() -> None:
    payload: dict[str, Any] = {
        "alertType": "IDS alerted",
        "deviceSerial": "Q4XX-EXAM-PLE2",
        "networkId": "L_EXAMPLE001",
        "alertData": {"signature": "1:2008725:1"},
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].target_parts == ("meraki:Q4XX-EXAM-PLE2",)


def test_malware_detected_maps_to_security_alert() -> None:
    payload: dict[str, Any] = {
        "alertType": "Malware detected",
        "deviceSerial": "Q4XX-EXAM-PLE2",
        "networkId": "L_EXAMPLE001",
        "alertData": {"clientMac": "aa:bb:cc:dd:ee:ff"},
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "security_alert"


# ---------------------------------------------------------------------------
# Config changes
# ---------------------------------------------------------------------------


def test_settings_changed_at_network_level_emits_config_change() -> None:
    payload: dict[str, Any] = {
        "alertType": "Settings changed",
        "networkId": "L_EXAMPLE001",
        "networkName": "main-office",
        "alertData": {
            "adminEmail": "ops@example.com",
            "description": "Updated SSID encryption",
        },
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_class == "config_change"
    assert ev.target_parts == ("meraki:L_EXAMPLE001",)
    assert ev.payload["admin_email"] == "ops@example.com"
    assert ev.payload["change_summary"] == "Updated SSID encryption"


def test_settings_changed_at_device_level_uses_device_target() -> None:
    payload: dict[str, Any] = {
        "alertType": "Configuration change",
        "networkId": "L_EXAMPLE001",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "deviceName": "cpn-arlington-ms1",
        "alertData": {"adminEmail": "ops@example.com"},
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].target_parts == ("meraki:Q4CD-EXAM-PLE1",)
    assert events[0].payload["device"] == "cpn-arlington-ms1"


# ---------------------------------------------------------------------------
# Unmapped / malformed
# ---------------------------------------------------------------------------


def test_unknown_alert_type_returns_empty_list() -> None:
    """Unknown alertTypes do not crash and do not silently publish."""
    payload: dict[str, Any] = {
        "alertType": "Camera detected motion",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {"foo": "bar"},
    }
    assert map_meraki_payload(payload) == []


def test_missing_alert_type_returns_empty_list() -> None:
    """A payload with no alertType at all — never publish."""
    payload: dict[str, Any] = {"deviceSerial": "Q4CD-EXAM-PLE1"}
    assert map_meraki_payload(payload) == []


def test_alert_type_id_fallback_when_alert_type_absent() -> None:
    """Some dashboard variants send only the snake_case ID."""
    payload: dict[str, Any] = {
        "alertTypeId": "port_connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {"portName": "Port 1", "currentValue": "Disconnected"},
    }
    events = map_meraki_payload(payload)
    assert len(events) == 1
    assert events[0].event_class == "link_down"


def test_alert_data_missing_returns_empty_for_port_event() -> None:
    """Mapper must not raise on a payload that has no alertData."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
    }
    assert map_meraki_payload(payload) == []


def test_alert_data_wrong_type_does_not_raise() -> None:
    """alertData is a string by accident — handled gracefully."""
    payload: dict[str, Any] = {
        "alertType": "Port connectivity",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": "not a dict",
    }
    assert map_meraki_payload(payload) == []


# ---------------------------------------------------------------------------
# Subject builder compatibility
# ---------------------------------------------------------------------------


def test_emitted_events_compose_into_valid_sensory_subjects() -> None:
    """Smoke-test: every emitted target_parts tuple must be acceptable to
    :func:`sensory_subject` after the publisher's whitespace sanitization.

    The publisher (added in 0.8.0-dev6) sanitizes whitespace in target
    parts but rejects dots; this guards against introducing an alert
    type whose target parts contain dots."""
    from netcortex.contracts.subjects import sensory_subject

    payloads = [
        {
            "alertType": "Port connectivity",
            "deviceSerial": "Q4CD-EXAM-PLE1",
            "networkId": "L_EXAMPLE001",
            "alertData": {"portName": "Port 12", "currentValue": "Disconnected"},
        },
        {
            "alertType": "IDS alerted",
            "deviceSerial": "Q4XX-EXAM-PLE2",
            "networkId": "L_EXAMPLE001",
            "alertData": {"clientMac": "aa:bb:cc:dd:ee:ff"},
        },
        {
            "alertType": "Settings changed",
            "networkId": "L_EXAMPLE001",
            "alertData": {"adminEmail": "ops@example.com"},
        },
    ]
    for p in payloads:
        events = map_meraki_payload(p)
        assert events, f"expected at least one event for {p['alertType']}"
        for ev in events:
            # Sanitize whitespace as the publisher would, then construct
            # the subject. This must not raise.
            sanitized = tuple(part.replace(" ", "_") for part in ev.target_parts)
            subject = sensory_subject(ev.event_class, "meraki_webhook", *sanitized)
            assert subject.startswith(f"sensory.{ev.event_class}.meraki_webhook.")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
