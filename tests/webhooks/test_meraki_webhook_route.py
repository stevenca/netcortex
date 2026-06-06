"""End-to-end HTTP tests for the Meraki webhook route.

These tests are the first in the repo that drive FastAPI routes via
:class:`TestClient`. The pattern (capturing bus + minimal app + secret
fixture) is intentionally generic so dev9/dev10/dev11 webhook
receivers can reuse it.

Coverage
--------
* HMAC verification: valid, invalid, missing signature header
* No-secret-configured degradation (logs warning, still publishes)
* Body parsing: valid JSON, malformed JSON
* End-to-end publish: a port-flap payload produces the expected
  ``sensory.link_down.meraki_webhook.<target>`` subject
* Mapper integration: unknown alertType returns 200 with zero
  published events (sync trigger still queues)
* Publisher unavailable: app with ``event_bus = None`` still 200s
  and routes the sync trigger correctly
* Bus failure: a transient publish error doesn't 5xx the webhook
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.webhooks.conftest import CapturingEventBus


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _now_iso() -> str:
    """Current UTC time as an ISO8601 'Z' string (fresh for the replay
    guard added in 0.8.0-dev10)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _port_disconnected_payload(sent_at: str | None = None) -> dict[str, Any]:
    """Realistic Meraki port-disconnect payload (sanitized identifiers)."""
    return {
        "alertType": "Port connectivity",
        "alertTypeId": "port_connectivity",
        "version": "0.1",
        "sentAt": sent_at or _now_iso(),
        "organizationId": "EXAMPLE_ORG",
        "organizationName": "Example Org",
        "networkId": "L_EXAMPLE001",
        "networkName": "main-office",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "deviceName": "cpn-arlington-ms1",
        "alertId": "test-alert-001",
        "occurredAt": "2026-06-05T20:00:00Z",
        "alertData": {
            "portNum": "12",
            "portName": "Port 12",
            "previousValue": "Connected",
            "currentValue": "Disconnected",
        },
    }


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def test_valid_hmac_returns_200_and_publishes(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    body = json.dumps(_port_disconnected_payload()).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    j = resp.json()
    assert j["status"] == "queued"
    assert j["adapter"] == "meraki/TEST_TENANT"
    assert j["sensory_events_published"] == "1"

    assert len(capturing_bus.published) == 1
    subject, payload = capturing_bus.published[0]
    # The publisher sanitizes whitespace in target parts (dev6); the
    # original ``Port 12`` is preserved in the payload (dev6 contract).
    assert subject == "sensory.link_down.meraki_webhook.meraki:Q4CD-EXAM-PLE1.Port_12"
    assert payload["device"] == "cpn-arlington-ms1"
    assert payload["device_id"] == "meraki:Q4CD-EXAM-PLE1"
    assert payload["interface"] == "Port 12"
    assert payload["meraki_alert_type"] == "Port connectivity"
    assert payload["source"] == "meraki_webhook"  # injected by publisher


def test_invalid_hmac_returns_401_no_publish(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    body = json.dumps(_port_disconnected_payload()).encode()

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": "deadbeef" * 8,
        },
    )

    assert resp.status_code == 401
    assert "Invalid Meraki webhook signature" in resp.text
    assert capturing_bus.published == []


def test_missing_signature_returns_401_no_publish(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    body = json.dumps(_port_disconnected_payload()).encode()

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 401
    assert capturing_bus.published == []


def test_signature_prefix_sha256_accepted(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    """Newer Meraki firmware prefixes the signature with 'sha256='."""
    body = json.dumps(_port_disconnected_payload()).encode()
    sig = "sha256=" + _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert len(capturing_bus.published) == 1


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


def test_malformed_json_returns_400(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    # A correctly-signed but non-JSON body: signature passes, parsing fails.
    body = b"this is not json at all"
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 400
    assert capturing_bus.published == []


# ---------------------------------------------------------------------------
# Fail-closed: no-secret-configured (0.8.0-dev10, F1)
# ---------------------------------------------------------------------------


def test_no_secret_configured_rejected_503(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_no_secret: None,
) -> None:
    """When no signing secret is configured for the tenant, the receiver
    now FAILS CLOSED (503) instead of silently accepting the unsigned
    request. Pre-dev10 this returned 200."""
    body = json.dumps(_port_disconnected_payload()).encode()

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 503
    assert capturing_bus.published == []


def test_no_secret_bootstrap_allows_when_flag_set(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_no_secret: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit ``webhook_allow_unsigned`` bootstrap switch lets an
    unsigned request through (with a loud warning) so operators can
    validate the receive path before provisioning the secret."""
    from netcortex.webhooks import auth as auth_module

    monkeypatch.setattr(auth_module, "_allow_unsigned", lambda: True)
    body = json.dumps(_port_disconnected_payload()).encode()

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert len(capturing_bus.published) == 1


# ---------------------------------------------------------------------------
# Replay resistance (0.8.0-dev10, F9)
# ---------------------------------------------------------------------------


def test_stale_timestamp_rejected_403(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    """A correctly-signed payload whose ``sentAt`` is far outside the
    freshness window is rejected as a replay."""
    stale = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    body = json.dumps(_port_disconnected_payload(sent_at=stale)).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 403
    assert capturing_bus.published == []


# ---------------------------------------------------------------------------
# Body-size cap (0.8.0-dev10, F3)
# ---------------------------------------------------------------------------


def test_oversized_body_rejected_413(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body larger than the configured cap is rejected (413) before
    parsing — even with a valid Content-Length header."""
    from netcortex.webhooks import auth as auth_module

    monkeypatch.setattr(auth_module, "_max_body_bytes", lambda: 1024)
    big = b"x" * 4096

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=big,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert capturing_bus.published == []


# ---------------------------------------------------------------------------
# Mapper integration
# ---------------------------------------------------------------------------


def test_unknown_alert_type_returns_200_with_zero_published(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    """Unmapped alertTypes still 200 (the adapter sync is still
    valuable for ground-truth reconciliation) but produce no
    sensory events."""
    payload: dict[str, Any] = {
        "alertType": "Camera detected motion",
        "deviceSerial": "Q4CD-EXAM-PLE1",
        "networkId": "L_EXAMPLE001",
        "alertData": {"foo": "bar"},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["sensory_events_published"] == "0"
    assert capturing_bus.published == []


def test_ids_alerted_publishes_security_alert(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    payload: dict[str, Any] = {
        "alertType": "IDS alerted",
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
    body = json.dumps(payload).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert len(capturing_bus.published) == 1
    subject, pub_payload = capturing_bus.published[0]
    assert subject.startswith("sensory.security_alert.meraki_webhook.")
    assert pub_payload["clientMac"] == "aa:bb:cc:dd:ee:ff"


# ---------------------------------------------------------------------------
# Publisher unavailable / failure degradation
# ---------------------------------------------------------------------------


def test_route_works_without_publisher_configured(
    webhook_client_no_bus: TestClient,
    meraki_shared_secret: str,
) -> None:
    """When NATS_URL is unset (``app.state.event_bus = None``), the
    route still accepts the webhook and queues the sync trigger.
    Pre-dev8 behavior is preserved end-to-end."""
    body = json.dumps(_port_disconnected_payload()).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client_no_bus.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    # No publisher → no events counted, but request succeeded.
    assert resp.json()["sensory_events_published"] == "0"


def test_bus_publish_failure_does_not_fail_request(
    webhook_client: TestClient,
    capturing_bus: CapturingEventBus,
    meraki_shared_secret: str,
) -> None:
    """A transient NATS failure mid-publish must not 5xx the webhook;
    the sync trigger is still queued. The SensoryPublisher swallows
    the underlying bus error."""
    capturing_bus.publish_failures.append(RuntimeError("nats connection lost"))
    body = json.dumps(_port_disconnected_payload()).encode()
    sig = _sign(body, meraki_shared_secret)

    resp = webhook_client.post(
        "/webhooks/meraki/TEST_TENANT",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cisco-Meraki-Signature": sig,
        },
    )

    assert resp.status_code == 200
    # The publish failed and was swallowed inside SensoryPublisher;
    # the handler still reports one event was attempted (the mapper
    # produced one) so the count reflects mapper output, not bus
    # success — operationally we have ``bus.published`` and
    # ``sensory_publisher.publish_failed`` log lines to disambiguate.
    assert resp.json()["sensory_events_published"] == "1"
    assert capturing_bus.published == []
