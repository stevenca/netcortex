"""Tests for ``netcortex.util.timestamps``.

We exercise ``iso_to_epoch_ms`` against the input shapes the Meraki
Dashboard API actually returns in ``lastReportedAt`` so the adapter
can rely on a single, well-tested parser instead of inlining
``datetime.fromisoformat`` calls at every adapter call site.
"""

from __future__ import annotations

import pytest

from netcortex.util.timestamps import iso_to_epoch_ms


@pytest.mark.parametrize(
    "value, expected_ms",
    [
        # Meraki returns the Z-suffixed shape — must be supported.
        ("1970-01-01T00:00:00Z", 0),
        ("2025-06-14T19:22:06Z", 1_749_928_926_000),
        # Explicit offsets work too.
        ("2025-06-14T19:22:06+00:00", 1_749_928_926_000),
        # Sub-second precision is preserved.
        ("2025-06-14T19:22:06.250Z", 1_749_928_926_250),
        # Naive timestamps (no tz info) are documented as UTC.
        ("2025-06-14T19:22:06", 1_749_928_926_000),
    ],
)
def test_iso_to_epoch_ms_parses_supported_shapes(
    value: str, expected_ms: int,
) -> None:
    assert iso_to_epoch_ms(value) == expected_ms


@pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", "2025-13-99"])
def test_iso_to_epoch_ms_returns_none_for_invalid(bad: object) -> None:
    assert iso_to_epoch_ms(bad) is None  # type: ignore[arg-type]


def test_iso_to_epoch_ms_handles_non_string() -> None:
    # The Meraki adapter passes through whatever JSON gave us, so the
    # parser must defend against non-string inputs (e.g. None, ints).
    assert iso_to_epoch_ms(123)  is None  # type: ignore[arg-type]
    assert iso_to_epoch_ms(True) is None  # type: ignore[arg-type]
