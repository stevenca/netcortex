"""Unit tests for the reflex handler registry.

The registry is the single source of truth the runner enumerates. Its
contract is small but load-bearing — duplicate ids are an operator-
facing footgun (handler ids appear on every persisted outcome), so the
registry refuses them loudly rather than silently shadowing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import pytest

from netcortex.contracts.event_bus import EventMessage
from netcortex.reflex.protocol import ReflexContext, ReflexOutcome
from netcortex.reflex.registry import (
    DuplicateHandlerError,
    all_handlers,
    clear_registry,
    get_handler,
    register_handler,
)


class _StubHandler:
    """Minimal structural ReflexHandler used by these tests."""

    def __init__(self, hid: str, pattern: str = "sensory.test.test_src.test") -> None:
        self.id: Final[str] = hid
        self.pattern: Final[str] = pattern

    async def handle(
        self, event: EventMessage, ctx: ReflexContext
    ) -> ReflexOutcome | None:
        return ReflexOutcome(
            handler=self.id,
            subject=event.subject,
            target=None,
            severity="info",
            occurred_at=datetime.now(tz=timezone.utc),
            payload={},
        )


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Save/restore the global registry around every test in this module.

    The first-party handlers register themselves at import time. These
    tests need an empty registry to assert on, but they must NOT leak
    the cleared state to sibling test files (e.g., ``test_handlers.py``
    relies on the production handlers being registered). Snapshot before,
    restore after — using only the public registry surface.
    """
    snapshot = list(all_handlers())
    clear_registry()
    yield
    clear_registry()
    for h in snapshot:
        register_handler(h)


def test_register_and_lookup() -> None:
    h = _StubHandler("alpha")
    returned = register_handler(h)
    assert returned is h
    assert get_handler("alpha") is h
    assert list(all_handlers()) == [h]


def test_register_preserves_insertion_order() -> None:
    a, b, c = _StubHandler("a"), _StubHandler("b"), _StubHandler("c")
    register_handler(a)
    register_handler(b)
    register_handler(c)
    assert [h.id for h in all_handlers()] == ["a", "b", "c"]


def test_register_duplicate_id_raises() -> None:
    register_handler(_StubHandler("dup"))
    with pytest.raises(DuplicateHandlerError) as ei:
        register_handler(_StubHandler("dup"))
    assert "dup" in str(ei.value)


def test_register_same_instance_is_idempotent() -> None:
    """Re-registering the exact same instance is a no-op."""
    h = _StubHandler("same")
    register_handler(h)
    register_handler(h)
    assert list(all_handlers()) == [h]


def test_register_rejects_non_handler() -> None:
    """Registration is type-checked at registration time."""

    class NotAHandler:
        pass

    with pytest.raises(TypeError):
        register_handler(NotAHandler())  # type: ignore[arg-type]


def test_get_handler_missing_raises_key_error() -> None:
    with pytest.raises(KeyError):
        get_handler("never-registered")


def test_clear_registry_empties() -> None:
    register_handler(_StubHandler("x"))
    register_handler(_StubHandler("y"))
    clear_registry()
    assert list(all_handlers()) == []
