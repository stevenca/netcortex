"""SNMP link-state change publisher (0.8.0-dev5 — first sensory publisher).

The SNMP poller already walks ``ifOperStatus`` every poll cycle. This
module wires that data to the NATS event bus: when an interface
transitions between ``up`` and ``down`` between two consecutive polls,
we emit a ``sensory.link_down.snmp_poll.<device>|<ifname>`` (or
``link_up``) event so reflex handlers can react.

Why not compare against the prior in-memory poll?
-------------------------------------------------
The poller is sharded — a given device may be polled by a different
worker pod on consecutive cycles when we scale out. Reading prior state
from Neo4j gives a single source of truth for "what we believed last
time" regardless of which process did the polling.

Why not piggyback on ingest's status_changed_at stamping?
---------------------------------------------------------
Ingest currently stamps ``status_changed_at`` only on the :Device label,
not on interfaces (see the exploration in the dev5 PR description). Going
through ingest also adds latency — the poller has the new data in hand
*before* ingest writes it; emitting at poll time keeps the reflex chain
under one second from observation to ``:ReflexEvent`` write.

Deduplication
-------------
This module does not dedup. Dedup is the reflex handler's job (via
:class:`DedupStore`), because the same logical link-down may legitimately
arrive from multiple sources (poller + trap + Meraki webhook) and the
handler is the only place with enough context to collapse them. Each
publisher is responsible only for "what I observed", not "what's already
been observed by someone else".

Failure modes
-------------
* Neo4j read fails → log + skip the diff; no events published this cycle.
  The next poll cycle will pick up any transitions that span the outage.
* Bus publish fails → :class:`SensoryPublisher` swallows + logs; the
  graph still gets the new state via the normal ingest path so we don't
  lose the observation, only the reflex hook for this one event.
* No prior state in Neo4j (first-ever poll of a device) → every
  currently-down interface emits one ``link_down``. The dedup window in
  the handler suppresses re-emission on the next cycle when the prior
  state is now known.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from netcortex.thalamus.sensory_publisher import SensoryPublisher

_LOG = logging.getLogger(__name__)


async def emit_link_state_transitions(
    *,
    publisher: SensoryPublisher | None,
    driver: Any | None,
    device_node_id: str,
    device_name: str,
    if_map: dict[str, dict[str, Any]],
) -> int:
    """Compare freshly-polled ``if_map`` against prior Neo4j state, publish deltas.

    Returns the number of events published. Returns 0 (silently) if any
    of the prerequisites are missing — publisher not configured, driver
    not initialized, empty if_map. That degradation path is what lets
    the SNMP adapter call this unconditionally without branching on
    "is dev5 wiring up yet".

    Parameters
    ----------
    publisher:
        Wired ``SensoryPublisher`` with ``source='snmp_poll'``. ``None``
        disables publishing for this poll (the adapter still ingests the
        observation as usual).
    driver:
        The shared Neo4j async driver, or ``None`` if the graph layer
        isn't initialized yet. ``None`` means we can't compute a diff,
        so we silently emit nothing.
    device_node_id:
        The ``Device.id`` whose interfaces we're diffing. Comes from
        ``SnmpAdapter._poll_device``'s ``dev_node_id``.
    device_name:
        Human-readable device name used in the published subject and
        payload. Comes from ``SnmpAdapter._poll_device``'s ``dev_name``.
    if_map:
        Fresh poll result keyed by ifIndex. Each value is the dict
        ``_poll_interfaces`` populated with ``name``, ``oper_status``,
        and the other IF-MIB fields.
    """
    if publisher is None or driver is None or not if_map or not device_name:
        return 0

    # Build the set of (interface_name, current_oper_status) we just observed.
    # We use the canonical id shape that `_build_interface_health_nodes` uses
    # so the Cypher MATCH below finds the same nodes ingest writes.
    fresh: dict[str, str] = {}
    for iface in if_map.values():
        name = iface.get("name")
        oper = iface.get("oper_status")
        if not name or oper not in ("up", "down"):
            continue
        fresh[str(name)] = str(oper)

    if not fresh:
        return 0

    # Read prior oper_status for these interfaces in one query. We match
    # by canonical id (same shape `_build_interface_health_nodes` writes)
    # AND by name, because some interfaces are written by both SNMP and
    # platform adapters and may have non-snmp ids — we want the most
    # recent observation regardless of provenance.
    iface_ids = [f"snmp-if:{device_node_id}:{name}" for name in fresh]
    try:
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (i:Interface)
                WHERE i.id IN $ids
                RETURN i.id AS id, i.name AS name, i.oper_status AS oper
                """,
                ids=iface_ids,
            )
            prior: dict[str, str | None] = {}
            async for record in result:
                prior[str(record["name"])] = (
                    str(record["oper"]) if record["oper"] is not None else None
                )
    except Exception as exc:
        _LOG.warning(
            "snmp.link_state.prior_read_failed device=%s error=%s",
            device_name, exc,
        )
        return 0

    transitions: list[tuple[str, str, str | None, str]] = []
    for ifname, new_oper in fresh.items():
        prev = prior.get(ifname)
        if prev is None:
            # First-ever observation for this interface. We only emit
            # for currently-down so a noisy first-time poll of a 96-port
            # switch doesn't publish 96 link_up events. Currently-up
            # interfaces are the steady state we expect.
            if new_oper == "down":
                transitions.append((ifname, "link_down", None, new_oper))
            continue
        if prev == new_oper:
            continue
        if new_oper == "down":
            transitions.append((ifname, "link_down", prev, new_oper))
        elif new_oper == "up":
            transitions.append((ifname, "link_up", prev, new_oper))

    if not transitions:
        return 0

    published = 0
    for ifname, event_class, prev, new in transitions:
        target = f"{device_name}|{ifname}"
        payload: dict[str, Any] = {
            "device": device_name,
            "device_id": device_node_id,
            "interface": ifname,
            "oper_status": new,
            "prior_oper_status": prev,
        }
        try:
            await publisher.publish(event_class, target, payload=payload)
            published += 1
        except Exception as exc:
            # SensoryPublisher already swallows bus errors; this catches
            # programmer errors (bad subject, unknown event_class) so
            # one bad transition doesn't kill the rest of the batch.
            _LOG.warning(
                "snmp.link_state.publish_failed device=%s interface=%s class=%s error=%s",
                device_name, ifname, event_class, exc,
            )

    if published:
        _LOG.info(
            "snmp.link_state.transitions_published device=%s count=%d",
            device_name, published,
        )
    return published


__all__ = ["emit_link_state_transitions"]
