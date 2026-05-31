# Contract tests

> Status: **skeleton**. First Protocol stubs land in `netcortex/contracts/` in
> a follow-up commit; first contract tests land alongside `0.8.0`.

A *contract test* verifies that a concrete implementation of a Protocol
behaves the way the contract promises. It is implementation-agnostic: the same
test suite must pass for every concrete class that claims to implement the
contract.

This directory exists so that when `EventBus` has both a `NatsEventBus` and a
`RedisEventBus`, swapping them in production is a configuration change — not
a leap of faith. The contract test is the bridge.

## Layout (planned)

```
tests/contracts/
├── README.md
├── conftest.py                       # parametrizes test suites over implementations
├── event_bus/
│   ├── __init__.py
│   └── test_event_bus_contract.py    # one suite, run against every EventBus impl
├── sensory_adapter/
│   └── test_sensory_adapter_contract.py
└── policy/
    └── test_policy_contract.py
```

## Registering an implementation

When you add a new `EventBus` implementation, register it in
`tests/contracts/conftest.py`:

```python
EVENT_BUS_IMPLEMENTATIONS = [
    ("nats", lambda: NatsEventBus.in_memory()),
    ("redis", lambda: RedisEventBus.fake()),
]
```

The conftest parametrizes every contract test over the list. CI will run the
full suite against every implementation on every PR.

## What a contract test asserts

For `EventBus` (as the canonical example):

- `publish(subject, event)` followed by a matching `subscribe(pattern)` yields
  the event.
- Subscribers receive events in publish order within a single subject.
- A subscriber that joins after publish does *not* see past events
  (consistent with at-least-once semantics, not replay).
- Schema-invalid events are rejected with `EventBusValidationError`.
- Backpressure: a slow subscriber does not block other subscribers.
- `close()` is idempotent.

For `SensoryAdapter`:

- `discover()` returns an `AsyncIterator[SensoryEvent]`.
- Each event has a non-empty `modality` and a `target`.
- Cancellation propagates cleanly (no leaked tasks).

For `Policy`:

- `decide(context)` returns a `Decision` with a known `outcome` and
  `confidence ∈ [0, 1]`.
- Identical input produces identical output (deterministic by default; if a
  policy consults the LLM, it must declare itself non-deterministic in
  metadata and the contract test asserts metadata is consistent).

## Why this matters

The brain-architecture replaceability rule says: every cross-module dependency
goes through a Protocol. Contract tests are how we *know* that promise is
real, not aspirational. Without them, "pluggable" becomes "pluggable in
principle, broken in practice."
