"""Working memory — short-lived, fast, replaceable state stores.

Working memory in the brain-mapped architecture (see
[`docs/architecture/brain.md`](../../docs/architecture/brain.md)) is the
layer that holds **right now** state: active alerts, in-flight
reconciliation jobs, dedup windows, sliding metric aggregates. It is
deliberately separate from semantic memory (Neo4j, long-lived structural
knowledge) and episodic memory (Splunk, raw event history).

Production target storage is Redis. The interfaces are defined in
``netcortex/contracts/`` and concrete backends live in the per-concern
subpackages here:

* ``netcortex.working.dedup`` — TTL-windowed dedup (in-memory for 0.8.0,
  Redis from 0.9.0)
* ``netcortex.working.activity`` (future) — sliding-window counters
* ``netcortex.working.queues`` (future) — per-flow rate budgets

In 0.8.0 we ship only the in-memory implementations. They are functional
and used in CI; production deployments will swap them for Redis-backed
implementations in 0.9.0 with no call-site changes (the swap is wired
through the Protocols in ``netcortex/contracts/``).
"""
