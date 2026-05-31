# Golden snapshot tests

> Status: **skeleton**. First snapshots land with `0.7.x-dev2`.

## What is a golden test?

A *golden test* pins the output of a pure (or pure-by-injection) function for a
specific input. The pinned output lives next to the test as a `.json` or
`.yaml` snapshot. The test passes if the function's current output equals the
snapshot, byte-for-byte.

Goldens protect *behavior preservation*. They are the safety net under the
brain-architecture refactor described in
[`docs/architecture/brain.md`](../../docs/architecture/brain.md): when we move
`graph/correlate.py` to `association/` or split the writeback engine, golden
tests prove the decisions did not change.

## What belongs as a golden vs. a unit test?

| Test type | Use when |
|---|---|
| Unit | A function has a small, obvious input/output and the assertion is easy to inline. |
| Golden | A function produces a complex, structured output (e.g. a reconciliation plan with dozens of operations) where inline assertions would be unreadable. |
| Contract | The thing being tested is the *interface*, not a specific implementation — every concrete class must pass. |

## Layout

```
tests/golden/
├── README.md
├── conftest.py          # snapshot loader + diff renderer (added with first golden)
├── snapshots/           # the pinned outputs themselves
│   └── <function>/
│       └── <case>.json
└── test_<module>.py     # the assertion sites
```

## Workflow

1. **Write the test.** Call the function with a fixed input. Compare against
   the snapshot via the loader.
2. **Generate the snapshot.** First run with `--update-snapshots` writes it.
3. **Review the snapshot in the PR.** This is critical — the snapshot is the
   contract. Reviewers must read it.
4. **Approve once green.** From then on, any change to the function's output
   fails CI unless the snapshot is intentionally updated.

## When a snapshot legitimately changes

Goldens *will* legitimately change. When that happens:

1. Run locally with `--update-snapshots`.
2. Diff the result in your PR description — explain what changed and why.
3. Get a CODEOWNERS review on `tests/golden/`.
4. Merge.

A snapshot change without explanation in the PR description is a blocker.

## What NOT to golden-test

- Anything that depends on wall clock, random seeds, or process ID without
  injection. Inject the clock and randomness first.
- Anything that hits the network. Use [`tests/cassettes/`](../cassettes/README.md)
  for that.
- Anything that depends on the Neo4j or Splunk wire state. Goldens are pure;
  if you need a graph, build a fixture graph in-memory.

## First five goldens (planned for 0.7.x-dev2)

1. `netcortex.sync.netbox_writeback._diff_interfaces`
2. `netcortex.sync.netbox_writeback._reconcile_site_vlans` (plan only, no IO)
3. `netcortex.graph.correlate.derive_routes_to`
4. `netcortex.mcp.top_problems.compute` (with fixed input graph)
5. `netcortex.adapters.meraki._normalize_switch_port_config`
