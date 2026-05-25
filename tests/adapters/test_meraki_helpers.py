"""Unit tests for the Meraki adapter's pure normalisation helpers.

These helpers exist because v0.6.0-dev20 plumbs three new data-quality
signals end-to-end:

  * SDWAN_TUNNEL.oper_status derived from Meraki AutoVPN reachability,
    so the history correlator and top_problems link_down check both
    cover SD-WAN tunnels (not just physical / WAN-uplink edges).
  * Prefix.kind discriminator derived from the adapter-internal scope,
    so downstream tools can switch on a small operator-facing taxonomy
    without re-deriving the distinction.
  * Device name canonicalisation (whitespace trim + internal collapse)
    so dashboard typos like "Home MX " don't silently break cross-system
    joins (NetBox, top_problems grouping, history keys).

Keeping these as pure helpers makes them trivially testable without
spinning up httpx or the live Meraki API.
"""

from __future__ import annotations

import pytest

from netcortex.adapters.meraki import (
    _norm_device_name,
    _reachability_to_oper_status,
    _scope_to_prefix_kind,
)


# ── _reachability_to_oper_status ─────────────────────────────────────

@pytest.mark.parametrize("reachability,expected", [
    ("reachable",    "up"),
    ("Reachable",    "up"),   # case-insensitive (dashboard mixes case)
    ("REACHABLE",    "up"),
    ("  reachable ", "up"),   # whitespace tolerant
    ("unreachable",  "down"),
    ("UNREACHABLE",  "down"),
    (" unreachable ","down"),
])
def test_reachability_maps_to_oper_status(reachability: str,
                                          expected: str) -> None:
    """Documented dashboard vocabulary must collapse to canonical up/down."""
    assert _reachability_to_oper_status(reachability) == expected


@pytest.mark.parametrize("reachability", [
    None, "", "unknown", "Unknown", "indeterminate", "n/a", "weird",
])
def test_reachability_unknown_returns_none(reachability: str | None) -> None:
    """Unknown / missing reachability MUST NOT seed a bogus oper_status.

    The history correlator filters rows whose oper_status is NULL; if
    we returned ``"unknown"`` (or any other label) here, the tunnel
    would land in the transition log with a fake state change every
    time the dashboard's opinion flipped between "unknown" and a real
    value, polluting flap stats.
    """
    assert _reachability_to_oper_status(reachability) is None


# ── _scope_to_prefix_kind ────────────────────────────────────────────

@pytest.mark.parametrize("scope,expected", [
    ("vlan",   "vlan_subnet"),
    ("vlan6",  "vlan_subnet"),
    ("svi",    "vlan_subnet"),
    ("svi6",   "vlan_subnet"),
    ("static", "static_route"),
    ("STATIC", "static_route"),   # case-insensitive
    ("  vlan", "vlan_subnet"),
])
def test_scope_maps_to_kind(scope: str, expected: str) -> None:
    """All five documented ingest scopes collapse onto the operator-facing kind."""
    assert _scope_to_prefix_kind(scope) == expected


@pytest.mark.parametrize("scope", [
    None, "", "bgp", "transit", "wan", "unrecognised",
])
def test_unknown_scope_returns_none(scope: str | None) -> None:
    """Unknown scopes are NOT labeled — silently dropping is correct here.

    The Prefix node still exists (with its raw ``scope`` preserved); we
    just don't invent a discriminator that downstream code would treat
    as authoritative.
    """
    assert _scope_to_prefix_kind(scope) is None


# ── _norm_device_name ────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Home MX ",                 "Home MX"),         # trailing space
    ("  Home MX",                "Home MX"),         # leading space
    ("  Home MX   ",             "Home MX"),         # both ends
    ("Home  MX",                 "Home MX"),         # internal double space
    ("Home\tMX",                 "Home MX"),         # tab → space
    ("Home\nMX",                 "Home MX"),         # newline → space
    ("cpn-ash-cat8k1.example",   "cpn-ash-cat8k1.example"),  # already clean
])
def test_device_name_canonicalises(raw: str, expected: str) -> None:
    """Whitespace artefacts the dashboard accepts must NOT leak into the graph."""
    assert _norm_device_name(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n", "  \t  "])
def test_empty_or_whitespace_returns_empty(raw: str | None) -> None:
    """Empty / whitespace-only input returns "" so callers fall back to serial."""
    assert _norm_device_name(raw) == ""
