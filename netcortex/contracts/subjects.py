"""NATS subject taxonomy — constants and helpers.

The full specification lives in [`docs/architecture/subjects.md`](../../docs/architecture/subjects.md).
This module is the **machine-readable** half of that contract: an
authoritative enumeration of the closed event-class vocabulary, plus a
handful of small helpers for constructing and parsing subjects in a way
that matches what receivers actually emit.

Why a closed vocabulary
-----------------------
Reflex handlers, fusion rules, and operator alerts all switch on event
class. If publishers were free to invent new classes ad-hoc, downstream
consumers would have to defensively handle unknown values everywhere.
Instead we lock the vocabulary here and force a documentation + code
change to introduce a new one — see the doc's "Versioning and breaking
changes" section.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Top-level namespaces
# ---------------------------------------------------------------------------

SENSORY_NS: Final[str] = "sensory"
FACT_NS: Final[str] = "fact"
REFLEX_NS: Final[str] = "reflex"
MOTOR_NS: Final[str] = "motor"
CONSOLIDATION_NS: Final[str] = "consolidation"

NAMESPACES: Final[frozenset[str]] = frozenset({
    SENSORY_NS,
    FACT_NS,
    REFLEX_NS,
    MOTOR_NS,
    CONSOLIDATION_NS,
})

# ---------------------------------------------------------------------------
# Closed vocabulary of event classes used after `sensory.` and `fact.`
# ---------------------------------------------------------------------------

#: Adding to this set MUST be accompanied by a documentation update in
#: docs/architecture/subjects.md in the same PR. Reviewers reject changes
#: that grow one without the other.
SENSORY_EVENT_CLASSES: Final[frozenset[str]] = frozenset({
    "link_down",
    "link_up",
    "bgp_drop",
    "bgp_up",
    "device_reboot",
    "device_unreachable",
    "device_reachable",
    "security_alert",
    "config_change",
    "topology_change",
    "route_advertisement_change",
})

# ---------------------------------------------------------------------------
# Source-token registry (modality_provenance, single NATS token)
# ---------------------------------------------------------------------------

#: Sources we currently know how to receive from. Add to this set when a
#: new receiver lands. Receivers ARE expected to validate their own source
#: token against this set at startup, so a typo fails fast.
SENSORY_SOURCES: Final[frozenset[str]] = frozenset({
    # SNMP
    "snmp_trap",
    "snmp_poll",
    "snmp_walk",
    # Webhooks
    "meraki_webhook",
    "thousandeyes_webhook",
    "cisco_amp_webhook",
    "catalyst_center_webhook",
    # Streaming telemetry
    "gnmi_dialout",
    "gnmi_dialin",
    "netconf_yangpush",
    "cisco_mdt",
    # API pollers (one per platform adapter)
    "meraki_api",
    "intersight_api",
    "vsphere_api",
    "fmc_api",
    "nexus_dashboard_api",
    "catalyst_center_api",
    # Synthetic — when NetCortex itself derives an observation. Rare;
    # most synthetic state should publish to `fact.*` instead.
    "netcortex_inference",
})


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def sensory_subject(event_class: str, source: str, *target_parts: str) -> str:
    """Build a validated `sensory.<class>.<source>.<target...>` subject.

    Receivers should use this rather than f-string concatenation. The
    validation catches drift between the doc, this module, and what
    publishers actually emit — the kind of drift that produces
    "subscriptions silently match nothing" bugs that are miserable to
    debug in production.
    """
    if event_class not in SENSORY_EVENT_CLASSES:
        raise ValueError(
            f"unknown sensory event class {event_class!r}; "
            f"add to SENSORY_EVENT_CLASSES + docs/architecture/subjects.md"
        )
    if source not in SENSORY_SOURCES:
        raise ValueError(
            f"unknown sensory source {source!r}; "
            f"add to SENSORY_SOURCES + docs/architecture/subjects.md"
        )
    if not target_parts:
        raise ValueError(
            f"sensory subject {event_class}/{source} requires at least one target part; "
            f"got 0 — an empty subject tail makes subjects with wildcards ambiguous"
        )
    # NATS tokens cannot contain '.' (token separator) or whitespace.
    # We don't try to validate the full grammar here; we just catch the
    # two cases that produce silent-but-wrong subjects.
    for i, part in enumerate(target_parts):
        if not part:
            raise ValueError(
                f"sensory subject target part {i} is empty — would produce "
                f"a malformed subject like 'sensory.{event_class}.{source}..foo'"
            )
        if "." in part:
            raise ValueError(
                f"sensory subject target part {i}={part!r} contains '.' which is "
                f"the NATS token separator; use '|' to join compound identifiers"
            )
        if any(ch.isspace() for ch in part):
            raise ValueError(
                f"sensory subject target part {i}={part!r} contains whitespace; "
                f"NATS subjects must be whitespace-free"
            )
    return ".".join([SENSORY_NS, event_class, source, *target_parts])


def fact_subject(event_class: str, *target_parts: str) -> str:
    """Build a validated `fact.<class>.<target...>` subject.

    Lands in production with the fusion stage (0.9.0). Provided here so
    code written against the future fact namespace can validate today.
    """
    if event_class not in SENSORY_EVENT_CLASSES:
        raise ValueError(
            f"unknown fact event class {event_class!r}; "
            f"add to SENSORY_EVENT_CLASSES + docs/architecture/subjects.md"
        )
    if not target_parts:
        raise ValueError(
            f"fact subject {event_class} requires at least one target part"
        )
    return ".".join([FACT_NS, event_class, *target_parts])


# ---------------------------------------------------------------------------
# Parser — used by handlers to extract the canonical fact_key
# ---------------------------------------------------------------------------


def parse_sensory_subject(subject: str) -> tuple[str, str, str]:
    """Parse a sensory subject into (event_class, source, target_joined).

    `target_joined` is the remaining tokens after class+source, re-joined
    with the NATS separator (``.``). Handlers typically use this to
    compute their dedup fact_key.

    Returns a 3-tuple even when the subject is malformed — the caller
    decides what to do. Empty fields signal "we don't know."

    Examples
    --------
    >>> parse_sensory_subject("sensory.link_down.snmp_trap.r1|Gi0/1")
    ('link_down', 'snmp_trap', 'r1|Gi0/1')
    >>> parse_sensory_subject("sensory.security_alert.meraki_webhook.N_1.aa:bb:cc:dd:ee:ff")
    ('security_alert', 'meraki_webhook', 'N_1.aa:bb:cc:dd:ee:ff')
    """
    parts = subject.split(".")
    if len(parts) < 4 or parts[0] != SENSORY_NS:
        return ("", "", "")
    return (parts[1], parts[2], ".".join(parts[3:]))


__all__ = [
    "CONSOLIDATION_NS",
    "FACT_NS",
    "MOTOR_NS",
    "NAMESPACES",
    "REFLEX_NS",
    "SENSORY_EVENT_CLASSES",
    "SENSORY_NS",
    "SENSORY_SOURCES",
    "fact_subject",
    "parse_sensory_subject",
    "sensory_subject",
]
