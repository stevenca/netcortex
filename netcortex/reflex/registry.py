"""Process-wide registry of loaded :class:`ReflexHandler` instances.

Handlers register themselves at import time via
:func:`register_handler`. The :class:`netcortex.reflex.runner.ReflexRunner`
then enumerates the registry and wires each handler to the bus.

Why a global registry and not dependency-injection
--------------------------------------------------
The set of reflex handlers is **closed and small**: every handler is a
first-party module in this repository, signed by the same release. There
is no plugin loading, no late binding, no external code path. A
module-level dict is the simplest correct answer.

When agent-proposed reflex handlers become a thing (post-1.0.0 plasticity
work), they will land here too — but only after the proposal-approval
cycle outlined in ``docs/architecture/brain.md``, which writes them to
the same first-party path with operator sign-off.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from netcortex.reflex.protocol import ReflexHandler

_LOG = logging.getLogger(__name__)

_REGISTRY: dict[str, ReflexHandler] = {}


class DuplicateHandlerError(ValueError):
    """Raised when two handlers attempt to register the same id."""


def register_handler(handler: ReflexHandler) -> ReflexHandler:
    """Register a handler so the runner picks it up.

    Intended to be used at module scope::

        _HANDLER = register_handler(LinkDownHandler())

    Returning the handler lets the caller bind it to a module-level name
    without repeating the construction. Re-registering the same id raises
    :class:`DuplicateHandlerError` — handler ids are part of the public
    operator-facing surface (they appear on every persisted outcome) and
    silently shadowing them would be a footgun.

    The handler must structurally satisfy the :class:`ReflexHandler`
    Protocol — checked at registration time so import-time failures point
    at the offending file, not at the runner.
    """
    if not isinstance(handler, ReflexHandler):
        # ``runtime_checkable`` Protocol — verifies attribute presence,
        # not call signatures. That is good enough to catch the common
        # mistake of forgetting ``handle()`` on a new class.
        raise TypeError(
            f"object {handler!r} does not satisfy the ReflexHandler Protocol "
            "(must expose 'id', 'pattern', and 'handle')"
        )
    hid = handler.id
    if hid in _REGISTRY:
        existing = _REGISTRY[hid]
        if existing is handler:
            # Idempotent re-registration is harmless — common when handler
            # modules are imported twice in the test suite.
            return handler
        raise DuplicateHandlerError(
            f"reflex handler id {hid!r} already registered "
            f"by {type(existing).__name__}; would be shadowed by "
            f"{type(handler).__name__}"
        )
    _REGISTRY[hid] = handler
    _LOG.debug("reflex.registry.registered id=%s pattern=%s", hid, handler.pattern)
    return handler


def get_handler(handler_id: str) -> ReflexHandler:
    """Look up a registered handler by id. Raises :class:`KeyError` if absent."""
    return _REGISTRY[handler_id]


def all_handlers() -> Iterator[ReflexHandler]:
    """Iterate the registered handlers in registration order.

    Order is the dict insertion order, which is registration order under
    CPython 3.7+. Stable enough for the runner to assign deterministic
    subscription indices in logs.
    """
    return iter(_REGISTRY.values())


def clear_registry() -> None:
    """Drop every registered handler.

    Test-only helper. Production code never calls this — handlers are
    expected to live for the lifetime of the process. The runner calls
    :func:`all_handlers` at startup; mutating the registry after the
    runner starts is undefined.
    """
    _REGISTRY.clear()


__all__ = [
    "DuplicateHandlerError",
    "all_handlers",
    "clear_registry",
    "get_handler",
    "register_handler",
]
