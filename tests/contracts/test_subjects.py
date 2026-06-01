"""Tests for the subject taxonomy module.

These check the **validation** the builders perform — empty parts,
unknown event classes, unknown sources, embedded dots, whitespace —
because those are exactly the silent-but-wrong subject failures that
plague NATS-based systems when there is no centralized builder. The
closed-vocabulary checks are the contract that production publishers
will rely on starting in 0.8.0-dev4.
"""

from __future__ import annotations

import pytest

from netcortex.contracts.subjects import (
    SENSORY_EVENT_CLASSES,
    SENSORY_SOURCES,
    fact_subject,
    parse_sensory_subject,
    sensory_subject,
)


# ---------------------------------------------------------------------------
# sensory_subject builder
# ---------------------------------------------------------------------------


def test_builds_well_formed_subject() -> None:
    s = sensory_subject("link_down", "snmp_trap", "r1|Gi0/1")
    assert s == "sensory.link_down.snmp_trap.r1|Gi0/1"


def test_supports_multi_token_targets() -> None:
    s = sensory_subject(
        "security_alert", "meraki_webhook", "N_1", "aa:bb:cc:dd:ee:ff"
    )
    # Multi-token targets give downstream subscribers more wildcard
    # options (e.g. sensory.security_alert.*.N_1.>).
    assert s == "sensory.security_alert.meraki_webhook.N_1.aa:bb:cc:dd:ee:ff"


def test_rejects_unknown_event_class() -> None:
    with pytest.raises(ValueError, match="unknown sensory event class"):
        sensory_subject("not_an_event", "snmp_trap", "r1")


def test_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown sensory source"):
        sensory_subject("link_down", "carrier_pigeon", "r1")


def test_rejects_no_target_parts() -> None:
    with pytest.raises(ValueError, match="at least one target part"):
        sensory_subject("link_down", "snmp_trap")


def test_rejects_empty_target_part() -> None:
    with pytest.raises(ValueError, match="empty"):
        sensory_subject("link_down", "snmp_trap", "")


def test_rejects_dot_in_target_part() -> None:
    """`.` is the NATS token separator — sneaking one in produces a
    malformed subject with the wrong token count."""
    with pytest.raises(ValueError, match="separator"):
        sensory_subject("link_down", "snmp_trap", "r1.Gi0/1")


def test_rejects_whitespace_in_target_part() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        sensory_subject("link_down", "snmp_trap", "r1 problem")


# ---------------------------------------------------------------------------
# fact_subject builder
# ---------------------------------------------------------------------------


def test_fact_subject_basic() -> None:
    s = fact_subject("link_down", "r1|Gi0/1")
    assert s == "fact.link_down.r1|Gi0/1"


def test_fact_subject_rejects_unknown_class() -> None:
    with pytest.raises(ValueError):
        fact_subject("not_an_event", "r1")


def test_fact_subject_requires_target() -> None:
    with pytest.raises(ValueError):
        fact_subject("link_down")


# ---------------------------------------------------------------------------
# parse_sensory_subject
# ---------------------------------------------------------------------------


def test_parse_extracts_class_source_and_target() -> None:
    assert parse_sensory_subject("sensory.link_down.snmp_trap.r1|Gi0/1") == (
        "link_down",
        "snmp_trap",
        "r1|Gi0/1",
    )


def test_parse_joins_multi_token_target() -> None:
    assert parse_sensory_subject(
        "sensory.security_alert.meraki_webhook.N_1.aabbccddeeff"
    ) == ("security_alert", "meraki_webhook", "N_1.aabbccddeeff")


def test_parse_returns_empty_tuple_for_non_sensory() -> None:
    assert parse_sensory_subject("fact.link_down.r1") == ("", "", "")


def test_parse_returns_empty_tuple_for_too_few_tokens() -> None:
    assert parse_sensory_subject("sensory.link_down.snmp_trap") == ("", "", "")


# ---------------------------------------------------------------------------
# Vocabulary integrity
# ---------------------------------------------------------------------------


def test_all_event_classes_are_lower_snake_case() -> None:
    """A taxonomy with mixed case becomes a footgun on case-sensitive
    matching. Lock it down here."""
    for cls in SENSORY_EVENT_CLASSES:
        assert cls == cls.lower(), f"event class {cls!r} must be lowercase"
        assert " " not in cls, f"event class {cls!r} must not contain whitespace"
        assert "." not in cls, f"event class {cls!r} must not contain '.'"


def test_all_sources_are_lower_snake_case() -> None:
    for src in SENSORY_SOURCES:
        assert src == src.lower(), f"source {src!r} must be lowercase"
        assert " " not in src, f"source {src!r} must not contain whitespace"
        assert "." not in src, f"source {src!r} must not contain '.'"
