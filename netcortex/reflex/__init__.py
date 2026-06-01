"""Reflex — fast deterministic responders on the event bus.

Reflex handlers subscribe to narrow NATS subject patterns, run
deterministic logic in milliseconds, and produce :class:`ReflexOutcome`
records the deliberative loop later consolidates.

Public surface:

* :class:`ReflexHandler` — Protocol every handler obeys.
* :class:`ReflexOutcome` — frozen record of what a handler decided.
* :func:`register_handler` — registration entry point for handler modules.
* :class:`ReflexRunner` — wires the registered handler set to a bus.
* :mod:`netcortex.reflex.handlers` — importing this submodule registers
  the first-party handlers (``link_down``, ``security_webhook``,
  ``bgp_drop``).

See ``docs/architecture/brain.md`` for the role of reflex in the
brain-mapped architecture.
"""

from netcortex.reflex.protocol import ReflexContext, ReflexHandler, ReflexOutcome
from netcortex.reflex.registry import (
    DuplicateHandlerError,
    all_handlers,
    get_handler,
    register_handler,
)
from netcortex.reflex.runner import ReflexRunner

__all__ = [
    "DuplicateHandlerError",
    "ReflexContext",
    "ReflexHandler",
    "ReflexOutcome",
    "ReflexRunner",
    "all_handlers",
    "get_handler",
    "register_handler",
]
