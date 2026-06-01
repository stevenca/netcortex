"""Snapshot loader for golden tests.

A test calls ``assert_snapshot(name, actual)``. The fixture:

* Reads the pinned snapshot at ``tests/golden/snapshots/<name>.json``.
* Compares it to ``actual`` (serialized via JSON for deterministic diffs).
* If ``--update-snapshots`` was passed, writes ``actual`` to disk and passes.
* Otherwise, on mismatch, raises an assertion error with a unified diff so
  the reviewer can see exactly what moved.

Snapshot files are JSON to keep diffs reviewable. Non-JSON types (sets,
tuples, dataclasses) are coerced to JSON-equivalents with a stable ordering
before comparison.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Write current outputs to disk as the new golden snapshots. "
        "Use sparingly; every snapshot change must be reviewed in the PR.",
    )


def _normalize(value: Any) -> Any:
    """Convert ``value`` into a JSON-friendly, stably-ordered representation."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normalize(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize(value[k]) for k in sorted(value.keys(), key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, set):
        return [_normalize(v) for v in sorted(value, key=lambda x: json.dumps(_normalize(x), sort_keys=True))]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Last resort: stringify so the snapshot stays readable, but flag in the
    # diff if a non-serializable value sneaks in.
    return f"<non-json:{type(value).__name__}:{value!r}>"


def _serialize(value: Any) -> str:
    return json.dumps(_normalize(value), indent=2, sort_keys=False, ensure_ascii=False) + "\n"


@pytest.fixture
def assert_snapshot(request: pytest.FixtureRequest) -> Callable[[str, Any], None]:
    update_mode: bool = request.config.getoption("--update-snapshots")

    def _assert(name: str, actual: Any) -> None:
        # Permit nested names so callers can group by function:
        # assert_snapshot("correlate/_port_tail/exhaustive", actual)
        path = _SNAPSHOTS_DIR / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        actual_text = _serialize(actual)

        if update_mode or not path.exists():
            path.write_text(actual_text, encoding="utf-8")
            if not update_mode:
                pytest.fail(
                    f"created new snapshot at {path.relative_to(Path.cwd())} — "
                    "review the contents and re-run without --update-snapshots."
                )
            return

        expected_text = path.read_text(encoding="utf-8")
        if expected_text == actual_text:
            return

        diff = "".join(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                actual_text.splitlines(keepends=True),
                fromfile=f"a/{path.relative_to(Path.cwd())}",
                tofile=f"b/{path.relative_to(Path.cwd())}",
                n=3,
            )
        )
        pytest.fail(
            f"snapshot drift for {name}:\n{diff}\n"
            "If this change is intentional, run with --update-snapshots and "
            "explain the diff in the PR description (CODEOWNERS review required)."
        )

    return _assert
