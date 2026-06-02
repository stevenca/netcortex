"""Replaceability contracts for NetCortex.

Every cross-module dependency in NetCortex must flow through a Protocol defined
in this package. The discipline this enforces:

* Concrete implementations live in their own modules (``netcortex/thalamus/``,
  ``netcortex/memory/episodic/``, etc.) and import the Protocol from here.
* Tests in ``tests/contracts/`` are written against the Protocol, not against
  any one implementation. Every concrete class must register itself with the
  test parametrization and pass.
* Swapping NATS for Kafka, Splunk for OpenSearch, Bedrock for a local CLI, etc.
  is therefore a configuration change — not a code change in the call sites.

When that promise is broken, the answer is to add a Protocol here and refactor
the call site to depend on it. Never let one implementation's quirks leak into
the consumer.

Adding a new Protocol
---------------------
1. Create a new module in this package.
2. Add a ``typing.Protocol`` subclass with the smallest viable surface.
3. Add an associated event/value type (``@dataclass(frozen=True)``) if needed.
4. Add a contract test suite at ``tests/contracts/<name>/test_<name>_contract.py``.
5. Register at least one in-memory / stub implementation so the contract test
   can run from day one.
6. Document any expected invariants in the Protocol docstring — those become
   what the contract test asserts.

This package itself imports nothing from the rest of ``netcortex``. It is the
*bottom* of the import graph. Any change that introduces a downstream import
is a bug.
"""

from __future__ import annotations

from netcortex.contracts.dedup_store import DedupStore
from netcortex.contracts.event_bus import EventBus, EventBusValidationError, EventMessage
from netcortex.contracts.policy import Decision, Policy, PolicyContext
from netcortex.contracts.reflex_event_sink import ReflexEventSink
from netcortex.contracts.sensory_adapter import SensoryAdapter, SensoryEvent

__all__ = [
    "Decision",
    "DedupStore",
    "EventBus",
    "EventBusValidationError",
    "EventMessage",
    "Policy",
    "PolicyContext",
    "ReflexEventSink",
    "SensoryAdapter",
    "SensoryEvent",
]
