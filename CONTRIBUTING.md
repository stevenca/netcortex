# Contributing to NetCortex

Thanks for taking the time to file an issue or open a PR. NetCortex is the
intelligence layer for the network — small changes can have outsized blast
radius, so we ask contributors (humans and agents alike) to follow these
conventions.

## Before you start

1. Read **[`docs/architecture/brain.md`](docs/architecture/brain.md)** — the
   target architecture. Most non-trivial changes map to a "brain region"
   (sensory, motor, prefrontal, etc.). Knowing which region your change lives
   in makes the PR review fast.
2. Skim **[`docs/implementation-journal.md`](docs/implementation-journal.md)**
   for the state of what is actually built today.
3. Check **open issues** to see if your idea is already in flight.

## Filing issues

We use three issue templates:

- **Bug** — something broke or behaves unexpectedly.
- **Feature / capability** — propose new capability. Include the brain region
  it targets.
- **Security** — vulnerability or hardening request. Prefer private contact
  for exploitable issues.

## Opening a PR

1. **Fork or branch.** Branches in the main repo are fine for maintainers;
   forks for everyone else.
2. **Use the PR template.** It's short and points at the few things reviewers
   actually need to know.
3. **CI must be green** before review. The pipeline runs `ruff`, `mypy`,
   unit tests, golden tests, contract tests, recorded integration replays,
   security lints, and an SBOM/dependency audit. See
   [`.github/workflows/ci.yaml`](.github/workflows/ci.yaml).
4. **CODEOWNERS** — changes under `docs/architecture/`, `netcortex/contracts/`,
   `netcortex/policy/`, `.github/`, or the lint tools require explicit owner
   approval. See [`.github/CODEOWNERS`](.github/CODEOWNERS).
5. **Snapshot changes get extra scrutiny.** A change to a file under
   `tests/golden/snapshots/` is a behavior change. The PR description must
   explain *why* the snapshot moved.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# The full CI pipeline, locally:
ruff check netcortex tests tools
ruff format --check netcortex tests tools
mypy netcortex
pytest -q
python tools/lint_no_self_rewrite.py
python tools/lint_no_unsanitized_cassettes.py
```

## What never goes in a PR

- **Hardcoded credentials.** Not in code, not in tests, not in docs, not in
  cassettes. The sanitizer scrubs many things but defense in depth beats
  cleanup. See [`tests/cassettes/README.md`](tests/cassettes/README.md).
- **Unsanitized cassettes.** CI will block. See above link for the workflow.
- **Self-rewrite primitives** (`exec`/`eval`/`compile`/dynamic
  `import_module`) in agent paths. The lint
  [`tools/lint_no_self_rewrite.py`](tools/lint_no_self_rewrite.py) enforces
  this. The rule is documented in
  [`docs/architecture/brain.md`](docs/architecture/brain.md) under
  "Replaceability discipline."
- **Customer-identifying data.** When in doubt, do not include.

## For agent contributors

NetCortex is built with agentic contributors as a first-class audience. If
you are an LLM-driven agent opening a PR:

- Cite the brain region(s) you are modifying in the PR description.
- Include the contract / Protocol your change implements or affects.
- For schema or policy changes, explicitly call out which approval flow you
  followed (`plasticity/` proposal or direct PR).
- Be honest about uncertainty. A PR description that says "I am uncertain
  whether X" gets reviewed faster than one that hides it.

## Style and conventions

- **Python 3.12+.** Type annotations everywhere new code lands. `mypy` is
  strict (we are migrating the existing codebase to strict — new code is
  expected to be clean from the start).
- **Async-first** for IO. We use `httpx.AsyncClient`, `asyncio`, and
  structured concurrency (no orphan tasks).
- **`structlog`** for logging. No `print(...)` outside `tools/`.
- **Imperative reconciliation** over declarative magic for any write path —
  every NetBox/Meraki/etc. mutation should be a small, named function with a
  golden test under it.

## Releases

Release notes live in [`CHANGELOG.md`](CHANGELOG.md). Version policy is in
[`docs/implementation-journal.md`](docs/implementation-journal.md) under
"Versioning Policy."
