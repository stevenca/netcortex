"""``Policy`` Protocol — pluggable decision points.

A Policy turns a structured ``context`` into a structured ``decision``.
Examples of things that *should* be policies (not hard-coded if/else
branches in business code):

* Whether an unknown device-name pattern is a stub or a real device.
* Whether two name candidates refer to the same entity.
* Which VLAN group an operator-authored VLAN should be migrated into.
* What the right NetBox interface type is for a given platform-reported
  port description.

A policy can be implemented purely (a Python function with a registry of
rules), via a small LLM consult (when the deterministic rule set has gaps),
or via a more elaborate ML model. The contract is the same.

Determinism
-----------
Policies are deterministic by default. ``decide(ctx) == decide(ctx)`` for any
fixed ``ctx``. Policies that consult a language model and are therefore
non-deterministic MUST set ``deterministic=False`` in the ``Decision``
metadata and the contract test relaxes the determinism assertion for them.

The Decision still carries provenance (which rule fired, what evidence) so
non-deterministic outputs remain auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class PolicyContext:
    """Inputs to a policy decision.

    Free-form ``inputs`` keyed by whatever the policy needs. The dataclass
    wrapper exists so policy implementations can use single-dispatch on
    type-tagged contexts in the future without breaking call sites today.
    """

    inputs: dict[str, Any]


@dataclass(frozen=True)
class Decision:
    """Output of a policy decision.

    Fields
    ------
    outcome:
        The decision itself. Type is policy-specific; typically a string
        label (``"stub"`` / ``"real"``) or a structured result. Use a stable
        small vocabulary so downstream code can switch on it.
    confidence:
        ``[0.0, 1.0]``. Operators may choose to escalate low-confidence
        decisions to human review.
    rationale:
        Human-readable explanation. Optional but strongly encouraged —
        appears in the reconciliation UI as the "why" column.
    evidence:
        Structured pointers to the inputs that drove the decision. Keyed by
        signal name (``{"name_match": 0.92, "mac_match": True}``). Used
        by the audit trail and by the policy regression harness.
    deterministic:
        ``True`` if identical input yields identical output. ``False`` for
        policies that consult an LLM or other stochastic model.
    consulted:
        Free-form list of subsystems consulted to make the decision
        (``["rule:stub_pattern_v2", "llm:claude-haiku"]``). Required for
        non-deterministic decisions.
    """

    outcome: Any
    confidence: float
    rationale: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    deterministic: bool = True
    consulted: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class Policy(Protocol):
    """Minimum surface every policy implementation must support."""

    #: Stable identifier — appears in logs, audit trail, and the
    #: reconciliation UI. Use ``<area>:<name>:<version>`` form, e.g.
    #: ``"name:stub_classifier:v2"``.
    policy_id: str

    def decide(self, context: PolicyContext) -> Decision:
        """Make a decision given ``context``.

        Implementations MUST return a :class:`Decision` (never raise for an
        "I don't know" — return a low-confidence Decision with
        ``outcome=None`` instead, so the caller can decide whether to
        escalate, fall through, or abort).

        Implementations SHOULD NOT perform IO. Policies that need data
        beyond ``context`` should be supplied that data via ``context``.
        This constraint is what makes the contract test runnable in CI
        without external dependencies.
        """
        ...
