"""Contract tests every ``Policy`` implementation must pass."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netcortex.contracts import Decision, Policy, PolicyContext


def test_policy_has_stable_id(policy_factory: Callable[[], Policy]) -> None:
    p = policy_factory()
    assert p.policy_id
    assert ":" in p.policy_id, (
        "policy_id should follow <area>:<name>:<version> form for traceability"
    )


def test_decision_shape(policy_factory: Callable[[], Policy]) -> None:
    p = policy_factory()
    ctx = PolicyContext(inputs={"key": "value"})
    decision = p.decide(ctx)
    assert isinstance(decision, Decision)
    assert 0.0 <= decision.confidence <= 1.0
    # Either deterministic, OR consulted list is populated.
    if not decision.deterministic:
        assert decision.consulted, (
            "non-deterministic decisions must declare what was consulted"
        )


def test_deterministic_policies_are_actually_deterministic(
    policy_factory: Callable[[], Policy],
) -> None:
    p = policy_factory()
    ctx = PolicyContext(inputs={"a": 1, "b": [1, 2, 3], "c": {"nested": True}})
    d1 = p.decide(ctx)
    if not d1.deterministic:
        pytest.skip("policy declares itself non-deterministic")
    d2 = p.decide(ctx)
    assert d1.outcome == d2.outcome
    assert d1.confidence == d2.confidence


def test_does_not_raise_for_empty_context(
    policy_factory: Callable[[], Policy],
) -> None:
    p = policy_factory()
    # The Protocol forbids raising for missing data; return low-confidence
    # Decision with outcome=None instead.
    decision = p.decide(PolicyContext(inputs={}))
    assert isinstance(decision, Decision)
