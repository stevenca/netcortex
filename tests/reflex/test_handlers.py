"""Tests for the first-party reflex handlers.

These cover the per-handler outcome shape and the target-extraction
fallbacks. The runner-level dispatch is covered in ``test_runner.py``.

Importing the handlers module side-registers them with the global
registry. We pin those identities here so a rename to a handler id —
which is an operator-facing field on every outcome — fails CI loudly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from netcortex.contracts.event_bus import EventMessage

# Import the package so the handlers register themselves. We then read
# them back out of the registry by id rather than constructing fresh
# instances — that way a future change to the registration mechanics is
# automatically exercised by these tests too.
from netcortex.reflex import handlers as _handlers  # noqa: F401
from netcortex.reflex.protocol import ReflexHandler, ReflexOutcome
from netcortex.reflex.registry import get_handler

pytestmark = pytest.mark.asyncio


def _event(subject: str, payload: dict[str, object]) -> EventMessage:
    return EventMessage(
        subject=subject,
        payload=payload,
        headers={},
        ts=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Identity / wiring smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_id,expected_pattern",
    [
        ("link_down", "sensory.snmp.trap.link_down.>"),
        ("security_webhook", "sensory.meraki.webhook.security.>"),
        ("bgp_drop", "sensory.snmp.trap.bgp_backward_transition.>"),
    ],
)
def test_handler_registered_with_expected_pattern(
    handler_id: str, expected_pattern: str
) -> None:
    """Handler ids and patterns are part of the operator-facing surface.

    A rename here is fine, but it MUST be intentional — production
    operators read these ids in alerts and on the reconciliation UI.
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
    outcome = await h.handle(_event(
        "sensory.snmp.trap.link_down.r1",
        {"device_id": "r1", "interface": "Gi0/1"},
    ))
    assert outcome is not None
    assert outcome.handler == "link_down"
    assert outcome.target == "r1"
    assert outcome.severity == "high"
    assert outcome.payload["interface"] == "Gi0/1"
    assert outcome.outcome == "logged"


async def test_link_down_handles_missing_target_field() -> None:
    """No device field at all — outcome.target is None, not a stringified ``None``."""
    h = get_handler("link_down")
    outcome = await h.handle(_event(
        "sensory.snmp.trap.link_down.unknown",
        {"interface": "Gi0/1"},
    ))
    assert outcome is not None
    assert outcome.target is None


async def test_link_down_caps_upstream_keys() -> None:
    """A pathologically wide payload doesn't blow up the outcome record."""
    h = get_handler("link_down")
    wide = {f"k{i}": i for i in range(100)}
    outcome = await h.handle(_event("sensory.snmp.trap.link_down.r1", wide))
    assert outcome is not None
    assert len(outcome.payload["upstream_keys"]) <= 16


# ---------------------------------------------------------------------------
# security_webhook
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
async def test_security_webhook_severity_mapping(
    upstream: str, expected: str
) -> None:
    h = get_handler("security_webhook")
    outcome = await h.handle(_event(
        "sensory.meraki.webhook.security.ids_alerted",
        {"severity": upstream, "alertId": "abc-123",
         "networkId": "N_1", "clientMac": "aa:bb:cc:dd:ee:ff",
         "alertType": "ids_alerted"},
    ))
    assert outcome is not None
    assert outcome.severity == expected


async def test_security_webhook_prefers_client_mac_as_target() -> None:
    h = get_handler("security_webhook")
    outcome = await h.handle(_event(
        "sensory.meraki.webhook.security.ids_alerted",
        {"clientMac": "aa:bb:cc:dd:ee:ff",
         "deviceSerial": "Q2XX-YYYY-ZZZZ",
         "networkId": "N_1",
         "severity": "warning"},
    ))
    assert outcome is not None
    assert outcome.target == "aa:bb:cc:dd:ee:ff"


async def test_security_webhook_falls_back_to_device_serial() -> None:
    h = get_handler("security_webhook")
    outcome = await h.handle(_event(
        "sensory.meraki.webhook.security.malware_detected",
        {"deviceSerial": "Q2XX-YYYY-ZZZZ", "networkId": "N_1"},
    ))
    assert outcome is not None
    assert outcome.target == "Q2XX-YYYY-ZZZZ"


# ---------------------------------------------------------------------------
# bgp_drop
# ---------------------------------------------------------------------------


async def test_bgp_drop_composes_session_target() -> None:
    """device+peer -> ``device|peer`` canonical session id."""
    h = get_handler("bgp_drop")
    outcome = await h.handle(_event(
        "sensory.snmp.trap.bgp_backward_transition.r1",
        {"device_id": "r1", "peer": "10.0.1.5",
         "peer_asn": 65001, "last_state": "Established"},
    ))
    assert outcome is not None
    assert outcome.target == "r1|10.0.1.5"
    assert outcome.severity == "high"
    assert outcome.payload["peer_asn"] == 65001
    assert outcome.payload["last_state"] == "Established"


async def test_bgp_drop_target_falls_back_to_peer_only() -> None:
    h = get_handler("bgp_drop")
    outcome = await h.handle(_event(
        "sensory.snmp.trap.bgp_backward_transition.unknown",
        {"peer": "10.0.1.5"},
    ))
    assert outcome is not None
    assert outcome.target == "10.0.1.5"


async def test_bgp_drop_target_none_when_no_identifiers() -> None:
    h = get_handler("bgp_drop")
    outcome = await h.handle(_event(
        "sensory.snmp.trap.bgp_backward_transition.unknown",
        {"last_state": "Established"},
    ))
    assert outcome is not None
    assert outcome.target is None


# ---------------------------------------------------------------------------
# All-handler invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_id",
    ["link_down", "security_webhook", "bgp_drop"],
)
async def test_handler_returns_frozen_outcome_with_required_fields(
    handler_id: str,
) -> None:
    """Every handler must produce a well-formed :class:`ReflexOutcome`.

    Catches the common bug of forgetting to set ``severity`` or returning
    a dict instead of a dataclass.
    """
    h = get_handler(handler_id)
    outcome = await h.handle(_event(
        # Use a subject that matches each handler's pattern hierarchically.
        # We don't strictly need this — handle() doesn't re-validate the
        # subject — but it makes the test inputs realistic.
        f"sensory.test.invocation.{handler_id}",
        {"target": "test-target"},
    ))
    assert outcome is not None
    assert isinstance(outcome, ReflexOutcome)
    assert outcome.handler == handler_id
    assert outcome.severity in {"info", "warn", "high", "critical"}
    assert outcome.outcome in {"logged", "applied", "skipped", "errored"}
