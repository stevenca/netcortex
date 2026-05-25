"""Timestamp helpers for graph writes.

NetCortex stamps two universal properties on every node and edge it
writes into Neo4j:

* ``first_seen`` — set ONCE, the first time the object is created.
* ``last_seen``  — refreshed every time we observe / re-MERGE the object.

For objects with operational state (links with ``oper_status``, devices
with ``status``, etc.) we ALSO stamp ``<field>_changed_at`` whenever
the value transitions, so the UI can answer "how long has this link
been down?" / "when did this device last go offline?".

Units: epoch **milliseconds** (matches Neo4j's native ``timestamp()``
return type, so existing correlator code that uses ``timestamp()`` in
Cypher and our Python-side ``epoch_ms()`` produce comparable values).

Why ms and not seconds:
    - Neo4j's ``timestamp()`` returns ms; mixing units would create
      subtle bugs when the UI or a correlator compares "Python" and
      "Cypher" timestamps.  Standardizing on ms across the codebase
      keeps everything additive.
    - JS ``new Date(ms)`` accepts ms directly, so the UI does
      ``new Date(node.last_seen)`` with no conversion needed.

NOTE: Existing code in ``adapters/snmp.py`` writes ``snmp_polled_at`` /
``snmp_direct_at`` / ``health_updated_at`` etc. in epoch **seconds**
(via ``time.time()``).  Those legacy fields are kept untouched for
back-compat; the new ``first_seen`` / ``last_seen`` are the canonical
timestamps going forward and are always in ms.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time

#: Property name for "object was first observed at this timestamp (ms)".
FIRST_SEEN = "first_seen"

#: Property name for "object was last observed at this timestamp (ms)".
LAST_SEEN = "last_seen"


def epoch_ms() -> int:
    """Current time as epoch **milliseconds** (matches Neo4j timestamp())."""
    return int(time.time() * 1000)


def changed_at_field(field: str) -> str:
    """Return the canonical "<field>_changed_at" property name.

    Used by enrichment correlators that want to stamp when an operational
    state field transitioned (e.g. ``oper_status_changed_at``,
    ``health_score_changed_at``).
    """
    return f"{field}_changed_at"


def iso_to_epoch_ms(value: str | None) -> int | None:
    """Parse an ISO-8601 timestamp (e.g. Meraki's ``lastReportedAt``) to ms.

    Returns ``None`` for empty/unparseable input.  Accepts both the
    Z-suffix form (``2025-06-14T19:22:06Z``) and the explicit offset
    form (``2025-06-14T19:22:06.123+00:00``) — Python's
    ``datetime.fromisoformat`` handles offsets natively in 3.11+, but
    only began parsing the trailing ``Z`` in 3.11, so we normalize it
    here for safety.

    The output is in the same epoch-ms unit as ``epoch_ms()``, so it
    composes directly with ``last_seen`` / ``first_seen`` comparisons
    without any unit-juggling at the call site.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Normalize Zulu suffix to an explicit +00:00 offset for portability
    # across Python versions.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive timestamps from upstream APIs are documented as UTC; do
        # not silently re-interpret them as local time.
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
