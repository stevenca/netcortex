"""Thalamus — the typed event bus that routes every signal in NetCortex.

The thalamus is the single nervous system: every sensory adapter publishes to
it; every downstream consumer (reflex, association, conductor, stream bridge,
episodic memory) reads from it. This package contains concrete implementations
of the :class:`netcortex.contracts.event_bus.EventBus` Protocol.

Currently shipped:

* :class:`NatsEventBus` — production default backed by NATS server.

Test-only implementations live in ``tests/contracts/event_bus/`` so they do
not become a runtime dependency of the package.
"""

from netcortex.thalamus.nats_bus import NatsEventBus

__all__ = ["NatsEventBus"]
