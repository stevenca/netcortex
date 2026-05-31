# Recorded API cassettes

> Status: **skeleton**. First cassette lands with `0.7.x-dev2`.

Cassettes are recorded HTTP request/response pairs (via `pytest-recording` /
VCR) that let integration tests run offline in CI. They are the foundation
that makes the brain-architecture refactor safe — recorded NetBox / Meraki /
ThousandEyes interactions become deterministic regression tests that survive
the move from `graph/correlate.py` to `association/`.

## Hard rules

1. **No cassette enters this directory without going through the sanitizer.**
   Real IPs, MACs, hostnames, serial numbers, and tokens MUST be scrubbed.
2. **CI enforces this** via `tools/lint_no_unsanitized_cassettes.py`.
3. **The sanitizer is `tools/sanitize_cassette.py`.** It scrubs IPv4/IPv6,
   MAC, Meraki serials, JWTs, AWS keys/ARNs, bearer tokens, private keys, and
   customer hostnames (`cpn-*`, `*.corp`, `*.local`, etc).
4. **Cassettes are reviewable artifacts.** The reviewer's job is to confirm
   that what is being replayed in tests is actually representative.

## Recording a new cassette

```bash
# 1. Set up live credentials for the target system (NetBox/Meraki/etc.).
#    These come from .env.local — never committed.
source .env.local

# 2. Run the test in record mode. The cassette is written to .unsanitized.
pytest tests/integration/test_<thing>.py \
    --record-mode=once \
    -v

# By default pytest-recording writes to a path next to the test. Move/rename
# the raw output so it has an .unsanitized.yaml suffix so the gitignore catches
# it if you forget to sanitize.
mv tests/cassettes/test_<thing>.yaml tests/cassettes/test_<thing>.unsanitized.yaml

# 3. Sanitize.
python tools/sanitize_cassette.py \
    tests/cassettes/test_<thing>.unsanitized.yaml \
    --output tests/cassettes/test_<thing>.yaml

# 4. Eyeball the sanitized file. Anything sensitive that slipped through?
#    Add a pattern to tools/sanitize_cassette.py if so.

# 5. Verify the sanitized cassette replays correctly.
pytest tests/integration/test_<thing>.py --record-mode=none -v

# 6. Commit the sanitized cassette only. Delete the .unsanitized file (it's
#    gitignored but be tidy).
rm tests/cassettes/test_<thing>.unsanitized.yaml
git add tests/cassettes/test_<thing>.yaml
```

## Refreshing a cassette

When an API changes shape:

1. Delete the existing cassette: `rm tests/cassettes/test_<thing>.yaml`
2. Re-record from live (steps 2–5 above).
3. In the PR, call out which sanitized placeholders moved (the diff is noisy
   but reviewable — focus on the *structure* changes).

## Adding new redaction rules

If you find a value type the sanitizer missed:

1. Add the pattern to `tools/sanitize_cassette.py`.
2. Add a Hypothesis-based test in `tests/unit/tools/test_sanitize_cassette.py`
   that generates examples of the new pattern and asserts the sanitizer
   scrubs them.
3. Re-run `python tools/sanitize_cassette.py` against every existing cassette
   to catch back-leakage.

## What never goes in a cassette

- Real customer device names that aren't already public.
- Real production NetBox object IDs that map to real hardware (sanitizer
  preserves IDs by default — if that's a problem, scrub manually).
- Anything from a system the customer hasn't signed off on for use in
  CI / open-source contexts.

When in doubt, do not record.
