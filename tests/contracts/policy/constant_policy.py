"""Reference deterministic ``Policy`` used by contract tests.

Always returns the same outcome with full confidence and a populated
rationale. Useful as a sanity baseline for the contract harness.
"""

from __future__ import annotations

from netcortex.contracts.policy import Decision, Policy, PolicyContext


class ConstantPolicy(Policy):
    policy_id = "test:constant:v1"

    def decide(self, context: PolicyContext) -> Decision:
        return Decision(
            outcome="constant",
            confidence=1.0,
            rationale="reference deterministic policy used by contract tests",
            evidence={"input_keys": sorted(context.inputs.keys())},
            deterministic=True,
            consulted=("rule:constant",),
        )
