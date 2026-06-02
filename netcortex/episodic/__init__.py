"""Episodic memory — durable record of *what happened, when, and to whom*.

In the brain metaphor (see ``docs/architecture/brain.md``), episodic memory
is the hippocampal layer: a chronological log of discrete events with
enough context to reconstruct the experience. NetCortex's analogue is the
``:ReflexEvent`` graph layer — one node per reflex firing, plus edges to
the entities affected, so an operator can ask Neo4j questions like:

* "What reflexes fired on cpn-ful-cat9k1 in the last hour?"
* "Which interfaces saw three or more link_down reflexes in the last week?"
* "What did the BGP-drop reflex do the last time peer 10.0.0.1 went down?"

Episodic memory is intentionally separate from the live state graph (the
Device/Interface/IP nodes that adapters ingest). Reflex events are facts
about *observations*, not facts about *state* — they accumulate, they
never UPSERT, and they're meant to be queried with time windows.

Public surface
--------------
The only thing external callers should reach for is the constructor of a
concrete :class:`~netcortex.contracts.reflex_event_sink.ReflexEventSink`:

* :class:`~netcortex.episodic.reflex_event_sink.Neo4jReflexEventSink` —
  production default, writes to the cluster's Neo4j.
* :class:`~netcortex.episodic.reflex_event_sink.InMemoryReflexEventSink` —
  used by reflex unit/integration tests.

Both satisfy the same Protocol so the runner can be swapped freely.
"""

from __future__ import annotations

from netcortex.episodic.reflex_event_sink import (
    InMemoryReflexEventSink,
    Neo4jReflexEventSink,
)

__all__ = [
    "InMemoryReflexEventSink",
    "Neo4jReflexEventSink",
]
