"""Status transition history & flap detection (pure functions).

This module owns the canonical schema for "operational state history"
that NetCortex stamps onto every Device / link / routing peer that
has a status field worth tracking.  It is intentionally **pure** —
no I/O, no Neo4j, no globals — so unit tests cover every edge case
and the correlator that calls into it stays a thin orchestrator.

Storage shape (per tracked field on each element)
-------------------------------------------------
::

    <field>                      current value (existing)
    <field>_changed_at           epoch ms of last change (existing)
    <field>_history              JSON string '[[at, to], [at, to], ...]'
                                 sorted ascending, capped to a 7-day
                                 window and to MAX_EVENTS entries
                                 (defense against runaway flapping)
    <field>_flap_count_1h        int — transitions in the last hour
    <field>_flap_count_24h       int — transitions in the last 24 hours
    <field>_flap_score_1h        float in [0,1] — flap_count_1h / 6,
                                 saturating.  0 = stable, 1 = ≥6 flaps
                                 in the last hour
    <field>_flap_state           "stable" | "unstable" | "flapping"

Why JSON-string instead of list-of-maps in Neo4j
------------------------------------------------
Neo4j stores list-of-maps as packed bytes that can't be filtered or
projected in Cypher without first being unpacked.  Because we only
ever **read** the history as a whole (UI strip, MCP ``history.get``)
and **derived scalars** (``flap_state``, ``flap_count_24h``) are
what queries actually filter on, the simpler JSON-string form is a
strict upgrade:

* Trivial to read/write from any language (UI uses ``JSON.parse``).
* No Neo4j version coupling for nested-map property support.
* The flap scalars stay native ints/floats so Cypher filters like
  ``WHERE link.oper_status_flap_state = 'flapping'`` still work.

Tuple form ``[at, to]`` (not ``{at, to}``) saves ~30 % bytes for the
typical 50-event history without losing information — the "from"
value at index ``i`` is implicit (it's the ``to`` of index ``i-1``).
"""

from __future__ import annotations

import json
from typing import Any

# ── Tunables ─────────────────────────────────────────────────────────────────
#
# Window the UI's connectivity strip needs.  Operators asked for 7d
# so weekly maintenance patterns and weekend flaps show up.  Older
# transitions are trimmed on every correlator pass.
HISTORY_WINDOW_MS: int = 7 * 24 * 60 * 60 * 1000

# Absolute cap to defend against runaway flapping that hasn't been
# trimmed yet (e.g. a port flapping 60×/min for hours).  Without
# this, a single property could grow unbounded between trim cycles.
# 200 entries = ~28 transitions per day at the 7-day window, which
# is well above the "definitely flapping" threshold.
MAX_EVENTS: int = 200

# Flap-state classification (per-field, per-element).
#
# ``flapping`` = clearly bouncing right now → alerting-grade.
# ``unstable`` = has bounced recently but not in the last hour →
#                operator attention recommended.
# ``stable``   = no recent transitions.
#
# These thresholds match the operational definitions used in most
# NOCs (RFC 4271 style: ≥5 flaps/60min = damping candidate for BGP)
# and pair cleanly with the connectivity-strip UI: ``flapping`` gets
# a lightning-bolt badge, ``unstable`` gets an amber outline.
FLAPPING_THRESHOLD_1H: int = 5
UNSTABLE_THRESHOLD_24H: int = 5

# Saturation point for the [0,1] flap_score so the UI's color ramp
# tops out at "very flappy" without requiring exotic input values.
FLAP_SCORE_DENOM: int = 6

ONE_HOUR_MS: int = 60 * 60 * 1000
ONE_DAY_MS: int = 24 * ONE_HOUR_MS
ONE_WEEK_MS: int = 7 * ONE_DAY_MS
HOUR_BUCKET_MS: int = ONE_HOUR_MS
MAX_HOURLY_BUCKETS: int = 7 * 24


# ── Pure helpers ─────────────────────────────────────────────────────────────


def parse_history(history_json: str | None) -> list[tuple[int, str]]:
    """Parse a stored ``<field>_history`` JSON string.

    Returns an empty list for any malformed / missing input — callers
    treat that as "no history yet" rather than an error, so a
    corrupted property never blocks the correlator.

    The returned list is sorted ascending by timestamp; entries
    older than ``HISTORY_WINDOW_MS`` are NOT pruned here (call
    :func:`trim_history` for that).
    """
    if not history_json:
        return []
    try:
        raw = json.loads(history_json)
    except (TypeError, ValueError):
        return []
    out: list[tuple[int, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        at, to = item
        try:
            at_int = int(at)
        except (TypeError, ValueError):
            continue
        to_str = str(to) if to is not None else ""
        out.append((at_int, to_str))
    out.sort(key=lambda e: e[0])
    return out


def serialize_history(history: list[tuple[int, str]]) -> str:
    """Inverse of :func:`parse_history`.  Always emits compact JSON."""
    return json.dumps([[int(at), str(to)] for at, to in history],
                      separators=(",", ":"))


def trim_history(
    history: list[tuple[int, str]],
    now_ms: int,
    window_ms: int = HISTORY_WINDOW_MS,
    max_events: int = MAX_EVENTS,
) -> list[tuple[int, str]]:
    """Drop entries older than the window AND cap to ``max_events``.

    The cap is applied AFTER the window trim, keeping the most
    recent ``max_events`` so a port that has been flapping
    continuously surfaces its newest transitions to the operator.
    """
    cutoff = now_ms - window_ms
    trimmed = [e for e in history if e[0] >= cutoff]
    if len(trimmed) > max_events:
        trimmed = trimmed[-max_events:]
    return trimmed


def compute_flap_stats(
    history: list[tuple[int, str]],
    now_ms: int,
) -> dict[str, Any]:
    """Derive scalar flap metrics from a (trimmed) transition list.

    Returns a stable dict with five keys regardless of history size:
    ``flap_count_1h``, ``flap_count_24h``, ``flap_score_1h``,
    ``flap_state``, ``last_change_at`` (None when history is empty).

    Convention: each entry in ``history`` represents one transition,
    so the count IS the transition count.  We do not count the
    initial "from null" seed transition any differently — if the
    object only just appeared with no prior state, it has 1 flap by
    construction, which correctly reads as "stable" (well below the
    threshold) until something else happens.
    """
    cutoff_1h = now_ms - ONE_HOUR_MS
    cutoff_24h = now_ms - ONE_DAY_MS
    flap_1h = sum(1 for at, _ in history if at >= cutoff_1h)
    flap_24h = sum(1 for at, _ in history if at >= cutoff_24h)
    score_1h = min(1.0, flap_1h / FLAP_SCORE_DENOM)
    if flap_1h >= FLAPPING_THRESHOLD_1H:
        state = "flapping"
    elif flap_24h >= UNSTABLE_THRESHOLD_24H:
        state = "unstable"
    else:
        state = "stable"
    last_change = history[-1][0] if history else None
    return {
        "flap_count_1h":   flap_1h,
        "flap_count_24h":  flap_24h,
        "flap_score_1h":   round(score_1h, 3),
        "flap_state":      state,
        "last_change_at":  last_change,
    }


def apply_transition(
    field: str,
    current_value: str | None,
    new_value: str | None,
    history_json: str | None,
    now_ms: int,
    window_ms: int = HISTORY_WINDOW_MS,
    max_events: int = MAX_EVENTS,
) -> dict[str, Any] | None:
    """Compute the property writes needed to record one observation.

    Returns ``None`` when nothing has to be written — when there's
    no transition AND the existing flap stats are still accurate.
    Returns a dict of property → value writes otherwise, namespaced
    with ``field`` so the caller can ``SET n += $updates`` and trust
    that the right element is touched.

    Four cases:

    1. **No prior history, no current state, no new state** → return
       blanked flap scalars (no history write, no ``_changed_at``).
    2. **No prior history but ``new_value`` is set** → SEED.  We
       append one entry at ``now_ms`` so the connectivity strip has
       something to draw, but we DO NOT stamp ``<field>_changed_at``
       — seeding is "first time we ever observed this element", not
       an actual transition we witnessed.  ``_stamp_freshness``
       backfills ``<field>_changed_at`` from ``first_seen`` so the
       UI's "down since X" clock starts at the earliest moment we
       can honestly attribute the current state to.

       (Why this matters: on the first correlator pass after this
       feature rolled out, EVERY existing link hit this branch with
       ``history_json IS NULL``.  If the seed wrote ``_changed_at =
       now_ms``, every long-standing-down link's "down since" timer
       would reset to the rollout instant, producing a misleading
       cluster of "20 links just went down" alerts.)
    3. **Real transition** → append, trim, recompute stats, AND
       stamp ``<field>_changed_at = now_ms`` (this IS an observed
       state change).
    4. **No transition but stats need refresh** → recompute and
       return only the changed scalars (so the UI's "flap_state"
       reflects the passage of time, not just new transitions).

    The ``field`` argument is the base name (e.g. ``"oper_status"``);
    we synthesise ``<field>_history`` etc. internally to keep the
    storage convention consistent across every call site.
    """
    new_norm = _normalise(new_value)
    cur_norm = _normalise(current_value)

    history = trim_history(parse_history(history_json), now_ms,
                           window_ms=window_ms, max_events=max_events)
    seeded = False
    transitioned = False

    # Seed-on-first-observation: only when we have an actual value
    # to record.  Avoids stamping an "unknown" baseline that the
    # UI would render as a flapping strip on cold start.
    #
    # Critically distinct from a real transition: we DO NOT set
    # ``transitioned = True`` here, because we have no honest basis
    # for claiming the value just changed at ``now_ms``.  See the
    # docstring above for why this distinction matters.
    if not history and new_norm:
        history.append((now_ms, new_norm))
        seeded = True
    elif new_norm and new_norm != _last_value(history):
        # Real transition (the last recorded state differs from
        # what we just observed).  We compare against the LAST
        # history entry rather than ``current_value`` because the
        # latter may lag behind a concurrent write that already
        # appended to history but hasn't refreshed the scalar.
        history.append((now_ms, new_norm))
        transitioned = True

    new_history = trim_history(history, now_ms,
                               window_ms=window_ms, max_events=max_events)
    stats = compute_flap_stats(new_history, now_ms)

    # If nothing transitioned AND the current value already matches,
    # we still want flap stats to refresh as time passes (a flap
    # cluster from 6 hours ago should age out of "unstable" once it
    # falls outside the 24h window).  We return only the scalar
    # updates in that case to keep the write small.
    if not seeded and not transitioned and cur_norm == new_norm:
        return {
            f"{field}_flap_count_1h":  stats["flap_count_1h"],
            f"{field}_flap_count_24h": stats["flap_count_24h"],
            f"{field}_flap_score_1h":  stats["flap_score_1h"],
            f"{field}_flap_state":     stats["flap_state"],
        }

    updates: dict[str, Any] = {
        field:                       new_norm,
        f"{field}_history":          serialize_history(new_history),
        f"{field}_flap_count_1h":    stats["flap_count_1h"],
        f"{field}_flap_count_24h":   stats["flap_count_24h"],
        f"{field}_flap_score_1h":    stats["flap_score_1h"],
        f"{field}_flap_state":       stats["flap_state"],
    }
    # Only a REAL transition stamps ``_changed_at``.  Seeds do not —
    # ``_stamp_freshness`` backfills from ``first_seen`` instead.
    if transitioned:
        updates[f"{field}_changed_at"] = now_ms
    return updates


# ── internals ────────────────────────────────────────────────────────────────


def _normalise(value: Any) -> str | None:
    """Coerce any observed status value to a canonical lowercase str.

    Empty strings, ``None``, and whitespace collapse to ``None`` so
    the seed-on-first-observation rule doesn't fire on "no data".
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None


def _last_value(history: list[tuple[int, str]]) -> str | None:
    return history[-1][1] if history else None


def parse_hourly_metric_history(
    history_json: str | None,
) -> list[tuple[int, float, int]]:
    """Parse an hourly metric history JSON string.

    Storage shape:
      ``[[bucket_start_ms, avg_value, sample_count], ...]``

    Older shape compatibility (from ad-hoc tests):
      ``[[bucket_start_ms, avg_value], ...]`` -> sample_count defaults to 1.
    """
    if not history_json:
        return []
    try:
        raw = json.loads(history_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, float, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            bucket = int(item[0])
            avg = float(item[1])
            cnt = int(item[2]) if len(item) >= 3 else 1
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            cnt = 1
        out.append((bucket, avg, cnt))
    out.sort(key=lambda e: e[0])
    return out


def serialize_hourly_metric_history(
    history: list[tuple[int, float, int]],
) -> str:
    """Serialize ``[(bucket_ms, avg, count), ...]`` to compact JSON."""
    return json.dumps(
        [[int(at), round(float(avg), 4), int(cnt)] for at, avg, cnt in history],
        separators=(",", ":"),
    )


def trim_hourly_metric_history(
    history: list[tuple[int, float, int]],
    now_ms: int,
    window_ms: int = ONE_WEEK_MS,
    max_buckets: int = MAX_HOURLY_BUCKETS,
) -> list[tuple[int, float, int]]:
    """Trim hourly metric buckets to the rolling window and bucket cap."""
    cutoff = now_ms - window_ms
    trimmed = [e for e in history if e[0] >= cutoff]
    if len(trimmed) > max_buckets:
        trimmed = trimmed[-max_buckets:]
    return trimmed


def apply_hourly_metric_sample(
    history_json: str | None,
    sample_value: float | int | None,
    now_ms: int,
    window_ms: int = ONE_WEEK_MS,
    bucket_ms: int = HOUR_BUCKET_MS,
    max_buckets: int = MAX_HOURLY_BUCKETS,
) -> dict[str, Any]:
    """Upsert one sample into an hourly rolling average series.

    Returns:
      ``{"history_json", "avg_1h", "avg_24h", "count_1h", "count_24h"}``
    """
    history = trim_hourly_metric_history(
        parse_hourly_metric_history(history_json),
        now_ms,
        window_ms=window_ms,
        max_buckets=max_buckets,
    )
    val: float | None
    try:
        val = float(sample_value) if sample_value is not None else None
    except (TypeError, ValueError):
        val = None
    if val is not None:
        bucket = (int(now_ms) // bucket_ms) * bucket_ms
        if history and history[-1][0] == bucket:
            at, avg, cnt = history[-1]
            new_cnt = cnt + 1
            new_avg = ((avg * cnt) + val) / new_cnt
            history[-1] = (at, new_avg, new_cnt)
        else:
            history.append((bucket, val, 1))
    history = trim_hourly_metric_history(
        history,
        now_ms,
        window_ms=window_ms,
        max_buckets=max_buckets,
    )

    def _window_avg(cutoff_ms: int) -> tuple[float | None, int]:
        total = 0.0
        cnt_sum = 0
        for at, avg, cnt in history:
            if at >= cutoff_ms:
                total += avg * cnt
                cnt_sum += cnt
        if cnt_sum == 0:
            return None, 0
        return round(total / cnt_sum, 3), cnt_sum

    avg_1h, cnt_1h = _window_avg(now_ms - ONE_HOUR_MS)
    avg_24h, cnt_24h = _window_avg(now_ms - ONE_DAY_MS)
    return {
        "history_json": serialize_hourly_metric_history(history),
        "avg_1h": avg_1h,
        "avg_24h": avg_24h,
        "count_1h": cnt_1h,
        "count_24h": cnt_24h,
    }
