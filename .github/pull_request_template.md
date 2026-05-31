<!--
  Thanks for the PR. Keep this section short — reviewers (human or agent)
  read this first.
-->

## Summary
<!-- 1–3 bullets describing what changed and why. -->

## Type of change
- [ ] Bug fix
- [ ] New feature / capability
- [ ] Refactor (no behavior change)
- [ ] Docs only
- [ ] CI / build / tooling
- [ ] Schema or contract change (requires CODEOWNERS review)
- [ ] Security-relevant (auth, secrets, policy, sanitizer, lint)

## Risk
<!-- What's the blast radius if this is wrong? Which release boundary? -->

## Validation
- [ ] `ruff check` clean
- [ ] `mypy` clean (or explicitly justified)
- [ ] `pytest` green
- [ ] If touching `tests/cassettes/`: sanitizer ran and `lint_no_unsanitized_cassettes.py` is green
- [ ] If touching `netcortex/contracts/`: contract tests updated
- [ ] If touching `docs/architecture/brain.md`: roadmap impact called out below

## Roadmap impact
<!-- Optional. Does this advance, delay, or change a phase from docs/architecture/brain.md? -->

## For agent reviewers
<!--
  Hints for an automated reviewer (when we wire one up). Examples:
    - related-issues: #123, #456
    - smoke-scope: meraki, netbox-writeback
    - non-goals: do NOT extend reflex handlers in this PR
-->
