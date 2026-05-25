"""Unit tests for ``netcortex.graph.history``.

These tests are pure-Python — no Neo4j, no I/O.  They lock in the
contracts the correlator and UI rely on:

* Status transitions are detected even when the scalar value lags.
* Window trimming drops events older than 7 days and caps to
  MAX_EVENTS to defend against unbounded growth.
* Flap-state classification matches the operational thresholds
  (≥5 transitions/hour = flapping; ≥5/day = unstable).
* ``apply_transition`` is idempotent on a no-op observation but
  still returns refreshed scalars so flap stats age out cleanly.
"""

from __future__ import annotations

from netcortex.graph import history as H


# ── parse / serialize round-trip ─────────────────────────────────────────────


def test_parse_history_handles_missing_input() -> None:
    """Empty / None / malformed JSON must yield [] without raising."""
    assert H.parse_history(None) == []
    assert H.parse_history("") == []
    assert H.parse_history("not-json") == []
    assert H.parse_history('{"oops": true}') == []
    # Wrong inner shape: entries that aren't 2-tuples are skipped.
    assert H.parse_history('[1, "down"]') == []
    assert H.parse_history('[[1]]') == []
    assert H.parse_history('[[1, "down", "extra"]]') == []


def test_parse_history_sorts_ascending() -> None:
    parsed = H.parse_history('[[3000, "up"], [1000, "down"], [2000, "up"]]')
    assert parsed == [(1000, "down"), (2000, "up"), (3000, "up")]


def test_serialize_history_is_compact() -> None:
    s = H.serialize_history([(1000, "up"), (2000, "down")])
    # No spaces — saves bytes when stored as a Neo4j property.
    assert " " not in s
    assert s == '[[1000,"up"],[2000,"down"]]'


# ── trim_history ─────────────────────────────────────────────────────────────


def test_trim_window_drops_old_events() -> None:
    now = 10_000_000
    win = 1_000_000
    history = [
        (now - 2_000_000, "down"),   # outside window
        (now - 500_000,   "up"),     # inside
        (now - 100,       "down"),   # inside
    ]
    trimmed = H.trim_history(history, now, window_ms=win)
    assert trimmed == [(now - 500_000, "up"), (now - 100, "down")]


def test_trim_caps_to_max_events() -> None:
    now = 10_000_000
    history = [(now - i, "up" if i % 2 else "down") for i in range(50, 0, -1)]
    trimmed = H.trim_history(history, now, window_ms=H.HISTORY_WINDOW_MS,
                              max_events=10)
    # Keeps the newest 10.
    assert len(trimmed) == 10
    assert trimmed[-1] == history[-1]
    assert trimmed[0] == history[-10]


# ── compute_flap_stats ───────────────────────────────────────────────────────


def test_flap_state_classification() -> None:
    now = 10_000_000

    # All within the last hour, ≥5 transitions → flapping.
    flapping = [(now - i * 60_000, "up" if i % 2 else "down") for i in range(6)]
    stats = H.compute_flap_stats(flapping, now)
    assert stats["flap_state"] == "flapping"
    assert stats["flap_count_1h"] == 6
    assert stats["flap_score_1h"] == 1.0
    assert stats["last_change_at"] == flapping[-1][0]

    # 5 transitions in the last 24h but none in the last hour →
    # unstable.  Spread them across 6–12 hours back so they're
    # safely outside the 1-hour window.
    unstable = [(now - (2 + i) * H.ONE_HOUR_MS, "up" if i % 2 else "down")
                for i in range(5)]
    stats = H.compute_flap_stats(unstable, now)
    assert stats["flap_state"] == "unstable"
    assert stats["flap_count_1h"] == 0
    assert stats["flap_count_24h"] == 5
    assert stats["flap_score_1h"] == 0.0

    # 1 transition ever → stable.
    stable = [(now - 3 * H.ONE_DAY_MS, "up")]
    stats = H.compute_flap_stats(stable, now)
    assert stats["flap_state"] == "stable"
    assert stats["flap_count_1h"] == 0
    assert stats["flap_count_24h"] == 0

    # Empty history → stable, last_change_at is None.
    stats = H.compute_flap_stats([], now)
    assert stats["flap_state"] == "stable"
    assert stats["last_change_at"] is None


def test_flap_score_saturates_at_one() -> None:
    now = 10_000_000
    spam = [(now - i, "up" if i % 2 else "down") for i in range(20)]
    stats = H.compute_flap_stats(spam, now)
    assert stats["flap_score_1h"] == 1.0


# ── apply_transition ─────────────────────────────────────────────────────────


def test_apply_transition_seeds_first_observation() -> None:
    """The very first time we see a state, we record it so the UI
    has something to draw on the connectivity strip.  Without this,
    a freshly-discovered link wouldn't show ANY status history until
    its first real transition (potentially hours later).

    Critically, the seed must NOT stamp ``oper_status_changed_at``.
    Seeding only proves "first time we observed this element", not
    an actual transition we witnessed — ``_stamp_freshness``
    backfills ``_changed_at`` from ``first_seen`` so the UI's
    "down since X" clock reflects the earliest moment we can
    honestly attribute the current state to.
    """
    now = 10_000_000
    updates = H.apply_transition(
        field="oper_status",
        current_value=None,
        new_value="up",
        history_json=None,
        now_ms=now,
    )
    assert updates is not None
    assert updates["oper_status"] == "up"
    history = H.parse_history(updates["oper_status_history"])
    assert history == [(now, "up")]
    # Seed must NOT write _changed_at — that's _stamp_freshness's job.
    assert "oper_status_changed_at" not in updates
    assert updates["oper_status_flap_state"] == "stable"


def test_apply_transition_seed_on_rollout_does_not_stamp_changed_at() -> None:
    """Regression: when a feature rollout adds the history machinery
    to a graph full of pre-existing links, every link has its current
    ``oper_status`` already populated by adapter writes but no
    ``oper_status_history`` yet.  The seed branch MUST NOT use
    ``now_ms`` as the transition timestamp — doing so would reset
    every long-standing-down link's "down since" clock to the
    rollout instant and produce a cluster of misleading
    "20 uplinks just went down" alerts in ``top_problems``.

    This regression appeared in 0.6.0-dev16 and is fixed in dev17.
    """
    now = 10_000_000
    # Simulates the rollout state: current_value is set (adapter has
    # been writing oper_status for weeks), history is empty (the new
    # field has never been written before).
    updates = H.apply_transition(
        field="oper_status",
        current_value="down",
        new_value="down",
        history_json=None,
        now_ms=now,
    )
    assert updates is not None
    # History gets seeded so the UI strip has something to draw.
    assert updates["oper_status_history"] is not None
    # But the smoking gun — _changed_at — must NOT be stamped.
    assert "oper_status_changed_at" not in updates


def test_apply_transition_seed_then_real_transition_stamps_changed_at() -> None:
    """End-to-end sequence: seed pass leaves _changed_at unset; the
    NEXT pass where a real transition is observed stamps it
    correctly with ``now_ms``."""
    seed_at = 10_000_000
    seed_updates = H.apply_transition(
        field="oper_status",
        current_value=None,
        new_value="up",
        history_json=None,
        now_ms=seed_at,
    )
    assert seed_updates is not None
    assert "oper_status_changed_at" not in seed_updates
    seeded_history_json = seed_updates["oper_status_history"]

    # Time passes; the link transitions up → down.
    transition_at = seed_at + H.ONE_HOUR_MS
    transition_updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="down",
        history_json=seeded_history_json,
        now_ms=transition_at,
    )
    assert transition_updates is not None
    assert transition_updates["oper_status"] == "down"
    # Now _changed_at IS stamped — this was a real transition.
    assert transition_updates["oper_status_changed_at"] == transition_at
    history = H.parse_history(transition_updates["oper_status_history"])
    assert history == [(seed_at, "up"), (transition_at, "down")]


def test_apply_transition_records_real_change() -> None:
    now = 10_000_000
    prior_hist = H.serialize_history([(now - H.ONE_HOUR_MS, "up")])
    updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="down",
        history_json=prior_hist,
        now_ms=now,
    )
    assert updates is not None
    assert updates["oper_status"] == "down"
    assert updates["oper_status_changed_at"] == now
    history = H.parse_history(updates["oper_status_history"])
    assert history == [(now - H.ONE_HOUR_MS, "up"), (now, "down")]


def test_apply_transition_no_change_returns_refreshed_stats_only() -> None:
    """When the observation doesn't change state, we still update
    flap scalars (so a cluster from 25 hours ago ages out of
    "unstable") but we don't bump ``changed_at`` or rewrite the
    history string.

    This is the hot path — most polling cycles observe "still up"
    on a stable link, and we want those to be cheap writes.
    """
    now = 10_000_000
    # Last transition was 25h ago — outside the 24h flap window.
    prior_hist = H.serialize_history([(now - 25 * H.ONE_HOUR_MS, "up")])
    updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="up",
        history_json=prior_hist,
        now_ms=now,
    )
    assert updates is not None
    # Scalars present
    assert updates["oper_status_flap_state"] == "stable"
    assert updates["oper_status_flap_count_1h"] == 0
    assert updates["oper_status_flap_count_24h"] == 0
    # But NOT the history rewrite or changed_at bump
    assert "oper_status_history" not in updates
    assert "oper_status_changed_at" not in updates
    assert "oper_status" not in updates


def test_apply_transition_ignores_empty_new_value() -> None:
    """An unobserved cycle (new_value is None / empty) must not
    seed an "unknown" baseline.  Many adapters report a partial
    observation that omits oper_status; the correlator must not
    confuse that with a real "went unknown" transition."""
    now = 10_000_000
    # No prior history, no value to record → nothing to do.
    updates = H.apply_transition(
        field="oper_status",
        current_value=None,
        new_value=None,
        history_json=None,
        now_ms=now,
    )
    # We return the (empty) flap scalars so the caller can blank
    # them on a node that has no history at all, but we MUST NOT
    # invent a transition.
    assert updates is not None
    assert "oper_status_history" not in updates
    assert "oper_status_changed_at" not in updates


def test_apply_transition_trims_to_window_on_real_change() -> None:
    now = 10_000_000
    too_old = now - 8 * H.ONE_DAY_MS   # outside 7-day window
    fresh   = now - H.ONE_HOUR_MS
    prior_hist = H.serialize_history([(too_old, "up"), (fresh, "up")])
    updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="down",
        history_json=prior_hist,
        now_ms=now,
    )
    assert updates is not None
    history = H.parse_history(updates["oper_status_history"])
    # too_old must have been trimmed; fresh + new transition remain
    assert history == [(fresh, "up"), (now, "down")]


def test_apply_transition_normalises_case_and_whitespace() -> None:
    now = 10_000_000
    prior_hist = H.serialize_history([(now - H.ONE_HOUR_MS, "up")])
    # "  UP  " is the same state as "up" — must not record a flap.
    updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="  UP  ",
        history_json=prior_hist,
        now_ms=now,
    )
    assert updates is not None
    assert "oper_status_changed_at" not in updates
    # But "Down" IS a real change.
    updates = H.apply_transition(
        field="oper_status",
        current_value="up",
        new_value="Down",
        history_json=prior_hist,
        now_ms=now,
    )
    assert updates is not None
    assert updates["oper_status"] == "down"
    assert updates["oper_status_changed_at"] == now


def test_hourly_metric_history_upserts_and_averages() -> None:
    now = 10_000_000
    # First sample seeds the current hour bucket.
    out = H.apply_hourly_metric_sample(None, 50.0, now)
    hist = H.parse_hourly_metric_history(out["history_json"])
    assert len(hist) == 1
    bucket, avg, cnt = hist[0]
    assert cnt == 1
    assert avg == 50.0
    assert out["avg_1h"] == 50.0
    assert out["avg_24h"] == 50.0

    # Second sample in the same hour updates running average.
    out2 = H.apply_hourly_metric_sample(out["history_json"], 70.0, now + 1000)
    hist2 = H.parse_hourly_metric_history(out2["history_json"])
    assert len(hist2) == 1
    bucket2, avg2, cnt2 = hist2[0]
    assert bucket2 == bucket
    assert cnt2 == 2
    assert avg2 == 60.0
    assert out2["avg_1h"] == 60.0


def test_hourly_metric_history_trims_to_7_days() -> None:
    now = 30 * H.ONE_DAY_MS
    # 10-day-old bucket must trim out.
    old_bucket = ((now - 10 * H.ONE_DAY_MS) // H.ONE_HOUR_MS) * H.ONE_HOUR_MS
    fresh_bucket = ((now - H.ONE_HOUR_MS) // H.ONE_HOUR_MS) * H.ONE_HOUR_MS
    seed = H.serialize_hourly_metric_history([
        (old_bucket, 10.0, 1),
        (fresh_bucket, 20.0, 1),
    ])
    out = H.apply_hourly_metric_sample(seed, 30.0, now)
    hist = H.parse_hourly_metric_history(out["history_json"])
    assert all(at >= now - H.ONE_WEEK_MS for at, _, _ in hist)
    # Old bucket removed; fresh + current remain.
    assert len(hist) == 2


def test_hourly_metric_history_handles_missing_sample() -> None:
    now = 14_400_000  # 4h, aligned to whole-hour boundaries for deterministic cutoff
    seed = H.serialize_hourly_metric_history([
        (((now - H.ONE_HOUR_MS) // H.ONE_HOUR_MS) * H.ONE_HOUR_MS, 42.0, 3),
    ])
    out = H.apply_hourly_metric_sample(seed, None, now)
    hist = H.parse_hourly_metric_history(out["history_json"])
    assert len(hist) == 1
    assert out["avg_1h"] == 42.0
