"""Unit tests for ``_compute_netbox_delta``.

This helper encodes the NetCortex design philosophy:

  * NetCortex is authoritative for the CURRENT state of the network.
  * NetBox is INTENT — operator-curated, often incomplete.
  * When the two disagree, NetCortex does NOT silently overwrite
    either side.  It records a small, structured delta on the
    canonical node so a future reconciliation UI can flag the
    mismatch to the operator.

These tests pin down the exact shape of the delta so downstream
consumers (and the future reconciliation tool) have something stable
to read.
"""

from __future__ import annotations

import pytest

from netcortex.sync.netbox_enrich import _compute_netbox_delta


def test_empty_when_everything_agrees() -> None:
    """No delta recorded when NetBox and observed state line up."""
    assert _compute_netbox_delta(
        netbox_name="cpn-ful-n9k1",
        netbox_serial="FCH1234ABCD",
        current_name="cpn-ful-n9k1",
        current_serial="FCH1234ABCD",
    ) == {}


def test_empty_when_fqdn_matches_short_form() -> None:
    """FQDN vs short hostname is normalised away — same intent, different format."""
    assert _compute_netbox_delta(
        netbox_name="cpn-ful-n9k1",
        netbox_serial="",
        current_name="cpn-ful-n9k1.ciscops.net",
        current_serial="",
    ) == {}


def test_name_delta_recorded_when_observed_differs() -> None:
    """The motivating case: an Intersight FI named ``FI-A-FCH2903782Y`` whose
    NetBox device record is ``cpn-ful-aipod-fi-A``.  Both forms are kept
    verbatim so the UI can show "intent → current" without re-normalising.
    """
    delta = _compute_netbox_delta(
        netbox_name="cpn-ful-aipod-fi-A",
        netbox_serial="FCH2903782Y",
        current_name="FI-A-FCH2903782Y",
        current_serial="FCH2903782Y",
    )
    assert delta == {
        "name": {
            "intent": "cpn-ful-aipod-fi-A",
            "current": "FI-A-FCH2903782Y",
        },
    }


def test_serial_delta_when_match_was_by_name_only() -> None:
    """If we matched the device by NAME but the serials differ, surface that
    too — it almost always signals a NetBox record that's been re-used
    for a hardware swap without updating the serial number.
    """
    delta = _compute_netbox_delta(
        netbox_name="cpn-ful-n9k1",
        netbox_serial="OLD-SERIAL-001",
        current_name="cpn-ful-n9k1",
        current_serial="NEW-SERIAL-002",
    )
    assert delta == {
        "serial": {
            "intent": "OLD-SERIAL-001",
            "current": "NEW-SERIAL-002",
        },
    }


def test_both_deltas_recorded_when_both_differ() -> None:
    delta = _compute_netbox_delta(
        netbox_name="intended-name",
        netbox_serial="intended-serial",
        current_name="observed-name",
        current_serial="observed-serial",
    )
    assert set(delta.keys()) == {"name", "serial"}


@pytest.mark.parametrize("netbox_name,current_name", [
    ("", "ntnx-fi-1"),       # NetBox has no name → can't form an intent comparison
    ("ntnx-fi-1", ""),       # Observed has no name → can't form a current comparison
    ("", ""),
])
def test_name_delta_skipped_when_either_side_missing(
    netbox_name: str, current_name: str
) -> None:
    """Missing side → no delta.  We never invent a "differs from nothing"
    record because the absent side may simply not have been collected
    yet (transient state) and we'd flap the delta every cycle.
    """
    assert "name" not in _compute_netbox_delta(
        netbox_name=netbox_name,
        netbox_serial="",
        current_name=current_name,
        current_serial="",
    )


def test_serial_comparison_is_case_insensitive() -> None:
    """Serials in NetBox vs Intersight differ in case — that's NOT a delta."""
    assert _compute_netbox_delta(
        netbox_name="",
        netbox_serial="fch2903782y",
        current_name="",
        current_serial="FCH2903782Y",
    ) == {}
