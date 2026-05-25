"""SNMP interface counter polling + Redis-backed delta computation.

We poll IF-MIB and IF-MIB extensions to get per-interface counters:
    ifHCInOctets   (64-bit byte counter — required for >2Gbps links)
    ifHCOutOctets
    ifInErrors
    ifOutErrors
    ifInDiscards
    ifOutDiscards

To compute rates / utilization we need a *previous* sample.  Per the user's
requirement ("I do not want to keep history yet"), we keep only the most
recent sample in Redis (TTL = 2 cycles).  Each cycle:

    1. Read previous sample from Redis (if any) keyed by (device, ifIndex).
    2. Walk current counters.
    3. Compute deltas: rate_bps = (cur - prev) * 8 / elapsed_s
    4. Compute utilization_pct = rate_bps / (ifSpeed * 1e6) * 100
    5. Persist new sample to Redis with TTL.
    6. Return per-interface health dict — caller stores it on the Interface
       node (so PHYSICAL_LINK edges can be enriched too).

If Redis is unavailable, we still poll counters (so absolute values flow
into the graph) but utilization/error-rate stays None.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# IF-MIB OIDs
OID_IF_HC_IN_OCTETS   = "1.3.6.1.2.1.31.1.1.1.6"   # ifHCInOctets (64-bit)
OID_IF_HC_OUT_OCTETS  = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets
OID_IF_IN_ERRORS      = "1.3.6.1.2.1.2.2.1.14"     # ifInErrors
OID_IF_OUT_ERRORS     = "1.3.6.1.2.1.2.2.1.20"     # ifOutErrors
OID_IF_IN_DISCARDS    = "1.3.6.1.2.1.2.2.1.13"     # ifInDiscards
OID_IF_OUT_DISCARDS   = "1.3.6.1.2.1.2.2.1.19"     # ifOutDiscards

# Counter32 wraps at 2^32, Counter64 at 2^64
_C32_MAX = 1 << 32
_C64_MAX = 1 << 64

# Redis key TTL — long enough to survive 2 normal cycles but short enough
# that a stale device doesn't pollute the cache forever.
_SAMPLE_TTL_S = 600


def _redis_key(dev_node_id: str, ifindex: str) -> str:
    return f"netcortex:ifctr:{dev_node_id}:{ifindex}"


async def _get_prev_sample(cli, dev_node_id: str, ifindex: str) -> dict | None:
    """Fetch the previous counter sample for one interface from Redis."""
    try:
        raw = await cli.hgetall(_redis_key(dev_node_id, ifindex))
        if not raw:
            return None
        # decode bytes → str → typed
        out = {}
        for k, v in raw.items():
            ks = k.decode() if isinstance(k, bytes) else k
            vs = v.decode() if isinstance(v, bytes) else v
            out[ks] = vs
        return out
    except Exception:
        return None


async def _store_sample(cli, dev_node_id: str, ifindex: str, sample: dict) -> None:
    """Persist current counter sample to Redis for next-cycle delta."""
    try:
        key = _redis_key(dev_node_id, ifindex)
        # All values stored as strings (HSET only takes str/bytes/int/float)
        flat = {k: str(v) for k, v in sample.items()}
        await cli.hset(key, mapping=flat)
        await cli.expire(key, _SAMPLE_TTL_S)
    except Exception as exc:
        log.debug("snmp_counters.store_failed", error=str(exc))


def _counter_delta(prev: int, cur: int, width: int = 64) -> int | None:
    """Compute counter delta, handling wrap-around.  Returns None if invalid."""
    if cur is None or prev is None:
        return None
    if cur >= prev:
        return cur - prev
    # Wrap detected.  Use the appropriate max.
    cap = _C64_MAX if width == 64 else _C32_MAX
    # If the delta is larger than half the counter space, treat as no-data
    # rather than a real wrap (counter reset on device reboot).
    delta = (cap - prev) + cur
    if delta > cap / 2:
        return None
    return delta


async def poll_interface_counters(
    sess,
    dev_node_id: str,
    if_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Poll per-interface counters and return per-ifindex health metrics.

    Returns:
      { ifindex: {
          in_octets, out_octets, in_errors, out_errors,
          in_discards, out_discards,
          rate_in_bps, rate_out_bps, util_in_pct, util_out_pct,
          error_rate_in_per_s, error_rate_out_per_s,
          sample_age_s, has_baseline (bool),
      } }

    Health metrics that require a baseline (rate_*, util_*, error_rate_*)
    are None on the very first cycle.
    """
    import asyncio

    # Concurrent walks
    in_oct, out_oct, in_err, out_err, in_dis, out_dis = await asyncio.gather(
        sess.walk(OID_IF_HC_IN_OCTETS),
        sess.walk(OID_IF_HC_OUT_OCTETS),
        sess.walk(OID_IF_IN_ERRORS),
        sess.walk(OID_IF_OUT_ERRORS),
        sess.walk(OID_IF_IN_DISCARDS),
        sess.walk(OID_IF_OUT_DISCARDS),
    )

    def _index(rows):
        out: dict[str, int] = {}
        for oid, val in rows or []:
            idx = oid.rsplit(".", 1)[-1]
            try:
                out[idx] = int(str(val))
            except (TypeError, ValueError):
                continue
        return out

    cur_in_oct  = _index(in_oct)
    cur_out_oct = _index(out_oct)
    cur_in_err  = _index(in_err)
    cur_out_err = _index(out_err)
    cur_in_dis  = _index(in_dis)
    cur_out_dis = _index(out_dis)

    now = time.time()
    results: dict[str, dict[str, Any]] = {}

    # Open redis (per-loop cached helper from ingest.queue)
    try:
        from netcortex.ingest.queue import _client
        cli = await _client()
    except Exception:
        cli = None

    for ifindex, iface in if_map.items():
        # Cap useful absolute counters (so they flow into the graph even
        # without a baseline)
        in_o   = cur_in_oct.get(ifindex)
        out_o  = cur_out_oct.get(ifindex)
        in_e   = cur_in_err.get(ifindex)
        out_e  = cur_out_err.get(ifindex)
        in_d   = cur_in_dis.get(ifindex)
        out_d  = cur_out_dis.get(ifindex)

        if in_o is None and out_o is None and in_e is None and out_e is None:
            continue  # device didn't return counters for this interface

        entry: dict[str, Any] = {
            "in_octets":   in_o,
            "out_octets":  out_o,
            "in_errors":   in_e,
            "out_errors":  out_e,
            "in_discards": in_d,
            "out_discards":out_d,
            "rate_in_bps":  None,
            "rate_out_bps": None,
            "util_in_pct":  None,
            "util_out_pct": None,
            "error_rate_in_per_s":  None,
            "error_rate_out_per_s": None,
            "has_baseline": False,
            "sample_age_s": None,
        }

        prev = None
        if cli is not None:
            prev = await _get_prev_sample(cli, dev_node_id, ifindex)

        if prev:
            try:
                prev_ts = float(prev.get("ts", 0))
                elapsed = now - prev_ts
                if elapsed > 0:
                    entry["sample_age_s"] = round(elapsed, 1)
                    d_in  = _counter_delta(int(prev.get("in_octets", 0)),  in_o or 0) \
                                if in_o  is not None else None
                    d_out = _counter_delta(int(prev.get("out_octets", 0)), out_o or 0) \
                                if out_o is not None else None
                    d_ie  = _counter_delta(int(prev.get("in_errors", 0)),  in_e or 0, width=32) \
                                if in_e  is not None else None
                    d_oe  = _counter_delta(int(prev.get("out_errors", 0)), out_e or 0, width=32) \
                                if out_e is not None else None

                    if d_in is not None:
                        entry["rate_in_bps"] = round(d_in * 8 / elapsed, 1)
                    if d_out is not None:
                        entry["rate_out_bps"] = round(d_out * 8 / elapsed, 1)
                    if d_ie is not None:
                        entry["error_rate_in_per_s"] = round(d_ie / elapsed, 4)
                    if d_oe is not None:
                        entry["error_rate_out_per_s"] = round(d_oe / elapsed, 4)

                    # Utilization needs interface speed
                    speed_mbps = iface.get("speed_mbps") or 0
                    if speed_mbps > 0 and entry["rate_in_bps"] is not None:
                        cap_bps = speed_mbps * 1_000_000
                        entry["util_in_pct"]  = round(
                            entry["rate_in_bps"]  * 100 / cap_bps, 2
                        )
                        entry["util_out_pct"] = round(
                            entry["rate_out_bps"] * 100 / cap_bps, 2
                        )
                    entry["has_baseline"] = True
            except Exception as exc:
                log.debug("snmp_counters.delta_failed",
                          ifindex=ifindex, error=str(exc))

        # Persist this cycle's sample for next time
        if cli is not None:
            sample = {
                "ts": now,
                "in_octets":   in_o or 0,
                "out_octets":  out_o or 0,
                "in_errors":   in_e or 0,
                "out_errors":  out_e or 0,
                "in_discards": in_d or 0,
                "out_discards":out_d or 0,
            }
            await _store_sample(cli, dev_node_id, ifindex, sample)

        results[ifindex] = entry

    return results
