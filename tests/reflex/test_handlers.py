"""Tests for the first-party reflex handlers (dev3 taxonomy).

Covers:

* the operator-facing identity surface (handler id + subject pattern);
* per-handler outcome shape on the happy path;
* target-extraction fallbacks for partial / missing payload fields;
* dedup behavior when a :class:`DedupStore` is wired through the
  :class:`ReflexContext`.

The runner-level dispatch is covered separately in ``test_runner.py``.

Importing ``netcortex.reflex.handlers`` side-registers all three first-
party handlers. We then read them back out of the registry by id; this
exercises the same import-for-side-effect path the runner uses in
production, rather than constructing fresh instances ourselves.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from netcortex.contracts.event_bus import EventMessage
from netcortex.reflex import handlers as _handlers  # noqa: F401 — side-effect import
from netcortex.reflex.protocol import (
    ReflexContext,
    ReflexHandler,
    ReflexOutcome,
)
from netcortex.reflex.registry import get_handler
from netcortex.working.dedup import InMemoryDedupStore

pytestmark = pytest.mark.asyncio


def _event(subject: str, payload: dict[str, object]) -> EventMessage:
    return EventMessage(
        subject=subject,
        payload=payload,
        headers={},
        ts=datetime.now(tz=timezone.utc),
    )


def _empty_ctx() -> ReflexContext:
    return ReflexContext()


# ---------------------------------------------------------------------------
# Identity / wiring smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_id,expected_pattern",
    [
        ("link_down", "sensory.link_down.>"),
        ("security_alert", "sensory.security_alert.>"),
        ("bgp_drop", "sensory.bgp_drop.>"),
    ],
)
async def test_handler_registered_with_expected_pattern(
    handler_id: str, expected_pattern: str
) -> None:
    """Handler ids and patterns are part of the operator-facing surface.

    A rename here is fine, but it MUST be intentional — production
    operators read these ids in alerts and on the reconciliation UI.

    Declared async to match the module-level ``pytestmark =
    pytest.mark.asyncio`` (no await needed here).
    """
    h = get_handler(handler_id)
    assert isinstance(h, ReflexHandler)
    assert h.id == handler_id
    assert h.pattern == expected_pattern


# ---------------------------------------------------------------------------
# link_down
# ---------------------------------------------------------------------------


async def test_link_down_extracts_device_and_interface() -> None:
    h = get_handler("link_down")
    outcome = await h.handle(
        _event(
            "sensory.link_down.snmp_trap.r1",
            {"device_id": "r1", "interface": "Gi0/1"},
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.handler == "link_down"
    assert outcome.target == "r1|Gi0/1"
    assert outcome.severity == "high"
    assert outcome.payload["interface"] == "Gi0/1"
    assert outcome.payload["source"] == "snmp_trap"
    assert outcome.outcome == "logged"


async def test_link_down_handles_missing_target_field() -> None:
    """No device field at all — outcome.target is None."""
    h = get_handler("link_down")
    outcome = await h.handle(
        _event("sensory.link_down.snmp_trap.unknown", {"interface": "Gi0/1"}),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target is None


async def test_link_down_caps_upstream_keys() -> None:
    """A pathologically wide payload doesn't blow up the outcome record."""
    h = get_handler("link_down")
    wide = {f"k{i}": i for i in range(100)}
    outcome = await h.handle(
        _event("sensory.link_down.snmp_trap.r1", wide), _empty_ctx()
    )
    assert outcome is not None
    assert len(outcome.payload["upstream_keys"]) <= 16


async def test_link_down_dedups_across_sources() -> None:
    """The whole point of dev3: trap + webhook + poll on same target dedup."""
    h = get_handler("link_down")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        # First arrival: SNMP trap → fires
        out1 = await h.handle(
            _event(
                "sensory.link_down.snmp_trap.r1",
                {"device_id": "r1", "interface": "Gi0/1"},
            ),
            ctx,
        )
        assert out1 is not None
        assert out1.outcome == "logged"

        # Same physical event arrives via Meraki webhook ~50ms later
        out2 = await h.handle(
            _event(
                "sensory.link_down.meraki_webhook.r1",
                {"device_id": "r1", "interface": "Gi0/1"},
            ),
            ctx,
        )
        assert out2 is not None
        assert out2.outcome == "skipped"
        assert "duplicate" in out2.rationale.lower()
        # Severity demoted on the skipped outcome.
        assert out2.severity == "info"

        # And via the SNMP poll diff
        out3 = await h.handle(
            _event(
                "sensory.link_down.snmp_poll.r1",
                {"device_id": "r1", "interface": "Gi0/1"},
            ),
            ctx,
        )
        assert out3 is not None
        assert out3.outcome == "skipped"
    finally:
        await store.close()


async def test_link_down_does_not_dedup_different_targets() -> None:
    """Different (device, interface) tuples are independent facts."""
    h = get_handler("link_down")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        out1 = await h.handle(
            _event(
                "sensory.link_down.snmp_trap.r1",
                {"device_id": "r1", "interface": "Gi0/1"},
            ),
            ctx,
        )
        out2 = await h.handle(
            _event(
                "sensory.link_down.snmp_trap.r1",
                {"device_id": "r1", "interface": "Gi0/2"},
            ),
            ctx,
        )
        out3 = await h.handle(
            _event(
                "sensory.link_down.snmp_trap.r2",
                {"device_id": "r2", "interface": "Gi0/1"},
            ),
            ctx,
        )
        assert out1 is not None and out1.outcome == "logged"
        assert out2 is not None and out2.outcome == "logged"
        assert out3 is not None and out3.outcome == "logged"
    finally:
        await store.close()


async def test_link_down_skips_dedup_when_target_unknown() -> None:
    """Without a canonical target we can't meaningfully dedup."""
    h = get_handler("link_down")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        # No device_id / interface — handler returns logged outcome twice,
        # because there's no fact_key to dedup against.
        out1 = await h.handle(
            _event("sensory.link_down.snmp_trap.unknown", {}), ctx
        )
        out2 = await h.handle(
            _event("sensory.link_down.snmp_trap.unknown", {}), ctx
        )
        assert out1 is not None and out1.outcome == "logged"
        assert out2 is not None and out2.outcome == "logged"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# security_alert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "upstream,expected",
    [
        ("informational", "info"),
        ("warning", "warn"),
        ("high", "high"),
        ("critical", "critical"),
        # Unknown values fall back to "warn" — defensive default so an
        # unrecognized Meraki severity never gets dropped below warn.
        ("", "warn"),
        ("totally-bogus", "warn"),
        # Case-insensitive.
        ("CRITICAL", "critical"),
    ],
)
async def test_security_alert_severity_mapping(
    upstream: str, expected: str
) -> None:
    h = get_handler("security_alert")
    outcome = await h.handle(
        _event(
            "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
            {
                "severity": upstream,
                "alertId": "abc-123",
                "networkId": "N_1",
                "clientMac": "aa:bb:cc:dd:ee:ff",
                "alertType": "ids_alerted",
            },
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.severity == expected


async def test_security_alert_prefers_client_mac_as_target() -> None:
    h = get_handler("security_alert")
    outcome = await h.handle(
        _event(
            "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
            {
                "clientMac": "aa:bb:cc:dd:ee:ff",
                "deviceSerial": "Q2XX-YYYY-ZZZZ",
                "networkId": "N_1",
                "severity": "warning",
            },
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target == "aa:bb:cc:dd:ee:ff"


async def test_security_alert_falls_back_to_device_serial() -> None:
    h = get_handler("security_alert")
    outcome = await h.handle(
        _event(
            "sensory.security_alert.meraki_webhook.N_1|Q2XX-YYYY-ZZZZ",
            {"deviceSerial": "Q2XX-YYYY-ZZZZ", "networkId": "N_1"},
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target == "Q2XX-YYYY-ZZZZ"


async def test_security_alert_dedups_repeated_meraki_retries() -> None:
    """Meraki retries the same alert; we collapse to one outcome."""
    h = get_handler("security_alert")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        payload = {
            "clientMac": "aa:bb:cc:dd:ee:ff",
            "networkId": "N_1",
            "severity": "warning",
            "alertId": "retry-test",
            "alertType": "ids_alerted",
        }
        # First delivery
        out1 = await h.handle(
            _event(
                "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
                payload,
            ),
            ctx,
        )
        # Second delivery (Meraki retry)
        out2 = await h.handle(
            _event(
                "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
                payload,
            ),
            ctx,
        )
        assert out1 is not None and out1.outcome == "logged"
        assert out2 is not None and out2.outcome == "skipped"
    finally:
        await store.close()


async def test_security_alert_different_event_types_dont_dedup() -> None:
    """Two distinct alert types on the same client are different facts."""
    h = get_handler("security_alert")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        base_payload = {
            "clientMac": "aa:bb:cc:dd:ee:ff",
            "networkId": "N_1",
            "severity": "warning",
        }
        out1 = await h.handle(
            _event(
                "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
                {**base_payload, "alertType": "ids_alerted"},
            ),
            ctx,
        )
        out2 = await h.handle(
            _event(
                "sensory.security_alert.meraki_webhook.N_1|aabbccddeeff",
                {**base_payload, "alertType": "malware_detected"},
            ),
            ctx,
        )
        assert out1 is not None and out1.outcome == "logged"
        assert out2 is not None and out2.outcome == "logged"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# bgp_drop
# ---------------------------------------------------------------------------


async def test_bgp_drop_composes_session_target() -> None:
    """device+peer -> ``device|peer`` canonical session id."""
    h = get_handler("bgp_drop")
    outcome = await h.handle(
        _event(
            "sensory.bgp_drop.snmp_trap.r1|10.0.1.5",
            {
                "device_id": "r1",
                "peer": "10.0.1.5",
                "peer_asn": 65001,
                "last_state": "Established",
            },
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target == "r1|10.0.1.5"
    assert outcome.severity == "high"
    assert outcome.payload["peer_asn"] == 65001
    assert outcome.payload["last_state"] == "Established"
    assert outcome.payload["source"] == "snmp_trap"


async def test_bgp_drop_target_falls_back_to_peer_only() -> None:
    h = get_handler("bgp_drop")
    outcome = await h.handle(
        _event(
            "sensory.bgp_drop.snmp_trap.unknown|10.0.1.5",
            {"peer": "10.0.1.5"},
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target == "10.0.1.5"


async def test_bgp_drop_target_none_when_no_identifiers() -> None:
    h = get_handler("bgp_drop")
    outcome = await h.handle(
        _event(
            "sensory.bgp_drop.snmp_trap.unknown",
            {"last_state": "Established"},
        ),
        _empty_ctx(),
    )
    assert outcome is not None
    assert outcome.target is None


async def test_bgp_drop_dedups_trap_and_gnmi_for_same_session() -> None:
    h = get_handler("bgp_drop")
    store = InMemoryDedupStore()
    ctx = ReflexContext(dedup_store=store)
    try:
        out1 = await h.handle(
            _event(
                "sensory.bgp_drop.snmp_trap.r1|10.0.1.5",
                {"device_id": "r1", "peer": "10.0.1.5"},
            ),
            ctx,
        )
        # Same session, observed via gNMI a moment later
        out2 = await h.handle(
            _event(
                "sensory.bgp_drop.gnmi_dialout.r1|10.0.1.5",
                {"device_id": "r1", "peer": "10.0.1.5"},
            ),
            ctx,
        )
        assert out1 is not None and out1.outcome == "logged"
        assert out2 is not None and out2.outcome == "skipped"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# All-handler invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_id,sample_subject",
    [
        ("link_down", "sensory.link_down.snmp_trap.test"),
        ("security_alert", "sensory.security_alert.meraki_webhook.test"),
        ("bgp_drop", "sensory.bgp_drop.snmp_trap.test"),
    ],
)
async def test_handler_returns_frozen_outcome_with_required_fields(
    handler_id: str, sample_subject: str,
) -> None:
    """Every handler must produce a well-formed :class:`ReflexOutcome`."""
    h = get_handler(handler_id)
    outcome = await h.handle(
        _event(sample_subject, {"target": "test-target"}), _empty_ctx()
    )
    assert outcome is not None
    assert isinstance(outcome, ReflexOutcome)
    assert outcome.handler == handler_id
    assert outcome.severity in {"info", "warn", "high", "critical"}
    assert outcome.outcome in {"logged", "applied", "skipped", "errored"}
