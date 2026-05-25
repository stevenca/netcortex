"""Unit tests for the top_problems staleness policy.

The policy is a pure function (``_apply_staleness_policy``) — it owns
the decision of "should this problem be reported as-is, demoted, or
dropped entirely because its source-of-truth has not refreshed in a
long time".  Keeping it pure makes it trivially testable without
spinning up the full MCP/Neo4j stack.

These tests pin "now" so the policy is deterministic across CI runs
and document the four behaviours the operator-facing config knobs
need to provide:

  1. Passthrough when no staleness signal is available.
  2. Passthrough when the device reported within the threshold.
  3. Demote to the configured severity when stale.
  4. Filter (drop) entirely when configured severity is ``"filter"``.
"""

from __future__ import annotations

import pytest

from netcortex.mcp.tools.agentic_ops import _apply_staleness_policy

# Pinned wall-clock so tests are independent of real time.
# 2025-06-15 12:00:00 UTC in epoch ms.
NOW_MS = 1_750_000_000_000
THRESHOLD_S = 86_400  # 24 h, matching the production default


def test_passthrough_when_last_reported_unknown() -> None:
    """No source-of-truth signal -> no policy applies; original severity wins."""
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=None,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="info",
    )
    assert out == "critical"


def test_passthrough_when_threshold_disabled() -> None:
    """A non-positive threshold is the documented "policy off" sentinel."""
    very_old = NOW_MS - 10 * 365 * 86_400 * 1000  # 10 years ago
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=very_old,
        now_ms=NOW_MS,
        threshold_seconds=0,
        stale_severity="filter",
    )
    assert out == "critical"


def test_passthrough_when_recent() -> None:
    """Device that reported well within the threshold keeps its severity."""
    recent = NOW_MS - 60 * 1000  # 60 s ago, well inside any reasonable threshold
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=recent,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="info",
    )
    assert out == "critical"


def test_passthrough_at_threshold_boundary() -> None:
    """Exactly-on-threshold is documented as NOT stale (strictly greater-than)."""
    on_boundary = NOW_MS - THRESHOLD_S * 1000
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=on_boundary,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="info",
    )
    assert out == "critical"


def test_demotes_when_stale() -> None:
    """Device past the threshold gets the configured stale severity."""
    stale = NOW_MS - (THRESHOLD_S + 60) * 1000  # 24h + 1m ago
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=stale,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="info",
    )
    assert out == "info"


def test_filters_when_configured() -> None:
    """``stale_severity = "filter"`` is the documented way to drop entirely."""
    stale = NOW_MS - 90 * 86_400 * 1000  # 90 days ago — abandoned inventory
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=stale,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="filter",
    )
    assert out is None


def test_invalid_stale_severity_falls_back_to_passthrough() -> None:
    """A typo in the secret must not silently break the policy.

    Production startup also validates this in ``Settings.hydrate``, but
    we defend in depth here so a bad value injected at runtime (or by a
    test stub) cannot drop or mis-rank a real outage.
    """
    stale = NOW_MS - 7 * 86_400 * 1000
    out = _apply_staleness_policy(
        severity="critical",
        last_reported_at_ms=stale,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="totally-bogus",
    )
    assert out == "critical"


@pytest.mark.parametrize("original", ["critical", "warning", "info"])
def test_demote_preserves_caller_severity_passthrough(original: str) -> None:
    """The policy never escalates — only demotes or passes through."""
    recent = NOW_MS - 1000
    out = _apply_staleness_policy(
        severity=original,
        last_reported_at_ms=recent,
        now_ms=NOW_MS,
        threshold_seconds=THRESHOLD_S,
        stale_severity="info",
    )
    assert out == original
