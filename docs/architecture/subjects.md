# NATS Subject Taxonomy

The bus (Thalamus, see [`brain.md`](./brain.md)) carries every observation,
every derived fact, every reflex outcome, and every motor action through a
single shared subject namespace. The shape of that namespace determines
how easily handlers can subscribe to **all sources of one thing** versus
**all things from one source** — those two needs pull in different
directions, and the taxonomy below is what we landed on.

## Top-level namespaces

| Namespace | Who publishes | Who subscribes | Lifetime |
|---|---|---|---|
| `sensory.>` | Receivers (webhook / trap / telemetry) and pollers (SNMP, Meraki API, etc.) | Resolver (Stage 2), fusion (Stage 3), reflex (Stage 4 fallback), episodic memory | Raw observations, retained briefly in JetStream |
| `fact.>` (0.9.0+) | Fusion stage, after dedup + corroboration | Reflex, working memory, semantic memory | Derived facts, retained longer |
| `reflex.>` | Reflex handlers | UI, episodic memory, downstream agents | Handler outcomes |
| `motor.>` (future) | Conductor / agents | Action executors, audit, semantic memory | Outbound actions |
| `consolidation.>` (future) | Consolidation cycles | Semantic memory, pattern memory | Bulk derived state |

Anything published outside these four namespaces will be rejected by the
receiver-side validators once those are in place; for now (0.8.0) it is
strongly discouraged.

## `sensory.<event_class>.<source>.<target_parts...>`

Event-class first. This is the **only** ordering that lets a single
handler subscribe to "all link-down observations regardless of where
they came from" with a NATS wildcard.

### `<event_class>` — a closed vocabulary

| Class | Meaning | Typical sources |
|---|---|---|
| `link_down` | An interface transitioned to operationally-down | SNMP linkDown trap, SNMP poll diff, Meraki webhook, gNMI dial-out |
| `link_up` | An interface transitioned to operationally-up | same |
| `bgp_drop` | A BGP session entered a non-Established state | BGP4-MIB trap, CISCO-BGP4-MIB trap, gNMI BGP neighbor sample |
| `bgp_up` | A BGP session entered Established | same |
| `device_reboot` | A device restarted | coldStart trap, sysUpTime reset on poll, Meraki status webhook |
| `device_unreachable` | A device stopped responding to our reachability probes | poller probe failure, Meraki status webhook |
| `device_reachable` | The inverse | same |
| `security_alert` | A security-class event observed | Meraki webhook (IDS/malware/blocked-URL), Cisco AMP webhook, future SIEM |
| `config_change` | A device configuration changed | Meraki webhook, NETCONF change notification, RANCID-style diff |
| `topology_change` | A new neighbor appeared or an existing one disappeared | LLDP/CDP poll diff |
| `route_advertisement_change` | Advertised prefix set changed on a session | BGP RIB poll diff |

New classes are added by amending this table **and** the
`SENSORY_EVENT_CLASSES` constant in `netcortex/contracts/event_bus.py`
in the same PR. Reviewers reject PRs that grow one without the other.

### `<source>` — `<modality>_<provenance>` joined as one token

Why one token: NATS wildcards match exactly one token (`*`) or trailing
greedy (`>`). Compound sources as separate tokens would make
"all snmp sources" subscriptions require multiple subscriptions.

| Modality | Source token | Notes |
|---|---|---|
| SNMP | `snmp_trap`, `snmp_poll`, `snmp_walk` | `_walk` reserved for bulk MIB walks distinct from per-OID polls |
| HTTP webhook | `meraki_webhook`, `thousandeyes_webhook`, `cisco_amp_webhook`, `catalyst_center_webhook` | Source names match the upstream platform |
| Streaming telemetry | `gnmi_dialout`, `gnmi_dialin`, `netconf_yangpush`, `cisco_mdt` | |
| API poll | `meraki_api`, `intersight_api`, `vsphere_api`, `fmc_api`, `nexus_dashboard_api` | One per platform adapter |
| Synthetic | `netcortex_inference` | When the system itself derives an observation (rare; prefer `fact.*`) |

### `<target_parts...>` — canonical identifiers, dot-separated tokens

Targets are the entities the event is about. Multi-part keys use `|`
(URL-safe, unambiguous) **within** a token, dots between tokens:

| Event class | Target shape | Example |
|---|---|---|
| `link_down`, `link_up` | `<device>\|<interface>` | `sensory.link_down.snmp_trap.cpn-ful-cat9k1\|Gi1/0/12` |
| `bgp_drop`, `bgp_up` | `<device>\|<peer_ip>` | `sensory.bgp_drop.gnmi_dialout.cpn-ful-cat8k1\|10.0.1.5` |
| `device_*` | `<device>` | `sensory.device_reboot.snmp_trap.cpn-ful-cat9k1` |
| `security_alert` | `<network>\|<client_mac_or_ip>` | `sensory.security_alert.meraki_webhook.N_1\|aa:bb:cc:dd:ee:ff` |
| `config_change` | `<device>` or `<device>\|<section>` | `sensory.config_change.meraki_webhook.Q2XX-YYYY-ZZZZ` |
| `topology_change` | `<device_a>\|<iface_a>\|<device_b>\|<iface_b>` | `sensory.topology_change.snmp_poll.r1\|Gi0/1\|r2\|Gi0/24` |
| `route_advertisement_change` | `<device>\|<peer>` | `sensory.route_advertisement_change.snmp_poll.r1\|10.0.1.5` |

**Canonicalization happens before publish.** Receivers publish with the
identifier shape they observed (raw IP, MAC, serial, ifIndex). The
resolver stage (0.9.0+) canonicalizes to NetBox-blessed names by querying
semantic memory and re-publishes under the same subject with the
canonical target. Until then (0.8.0), best-effort canonicalization
happens inline in the receiver; un-canonicalized observations still pass
through, they just may dedup imperfectly.

## `fact.<event_class>.<target_parts...>` (lands in 0.9.0)

After the fusion stage dedups same-event-different-source observations
within a per-class time window, the surviving fact is republished under
`fact.>`. Reflex handlers eventually move from `sensory.*` subscription
to `fact.*` subscription, gaining:

* one-fire-per-real-event semantics for free (no per-handler dedup code)
* corroboration metadata (which sources observed it)
* identity already canonicalized

## `reflex.<handler_id>.<outcome>` (future)

Reflex handlers may emit their own events back to the bus so other
consumers (UI live tail, downstream automation, episodic memory) can
react. Not used in 0.8.0-dev3 but the namespace is reserved.

Examples:

* `reflex.link_down.applied.cpn-ful-cat9k1|Gi1/0/12`
* `reflex.bgp_drop.skipped.cpn-ful-cat8k1|10.0.1.5` (skipped because deduped)
* `reflex.security_webhook.errored.N_1|aa:bb:cc:dd:ee:ff`

## `motor.<target>.<action>.<outcome>` (future)

Reserved for actuation. Not in scope until the policy library + write-gate
land.

## Dedup model (0.8.0)

Same-event-multiple-sources is handled at the **reflex handler layer**
using a `DedupStore` (in-memory in 0.8.0, Redis in 0.9.0+, see
[`netcortex.contracts.dedup_store.DedupStore`](../../netcortex/contracts/dedup_store.py)).
The fact key is intentionally short and scoped by event class so two
handlers reacting to the same target on different conditions (e.g.
`link_down` and `link_up`) do not collide:

```
fact_key = f"{event_class}|{canonical_target}"
```

The store enforces a TTL window per call. First arrival succeeds and
records the key with TTL = `handler.dedup_window_seconds`. Any later
arrival of the same `fact_key` before TTL expiry is treated as a
duplicate and the handler returns a `skipped` outcome (still recorded,
so operators see corroboration in the UI).

**Known limitation in 0.8.0:** a real flap whose down→up→down
transitions all land within one window collapses to one fire. We accept
this because flap detection is a working-memory concern (0.9.0) and
adding a transition counter in 0.8.0 would require publishers to emit
one, which they cannot uniformly do. The fusion stage in 0.9.0 tracks
state transitions explicitly.

Per-handler defaults:

| Handler | `dedup_window_seconds` | Rationale |
|---|---|---|
| `link_down` | 60 | A real flap surfaces as multiple facts; a duplicate-source surfaces once |
| `bgp_drop` | 60 | Same as link_down — BGP sessions don't flap meaningfully faster |
| `security_webhook` | 300 | Meraki retries delivery; same alert may arrive 2-3 times within minutes |

Handlers can override per-event by computing a different `fact_key` (e.g.,
include a `transition_id` from the payload) — see each handler's docstring.

## Wildcards and worked subscriptions

Common reflex subscriptions and what they catch:

```text
sensory.link_down.>                # ALL link-down regardless of source
sensory.link_down.snmp_trap.>      # link-down ONLY from SNMP traps
sensory.link_down.*.cpn-ful-cat9k1.* # link-down from any source for one device
sensory.bgp_drop.>                 # ALL bgp drops
sensory.security_alert.meraki_webhook.> # Meraki security alerts only
sensory.*.snmp_trap.>              # Everything from any SNMP trap source
sensory.>                          # Firehose (episodic memory only)
fact.link_down.>                   # 0.9.0+: post-fusion link-down facts
```

## Versioning and breaking changes

Subject names are part of the **operator-facing contract**. Renaming an
event class or source token is a breaking change and requires:

1. A `BREAKING CHANGE:` note in the changelog
2. Dual-publish under both old and new subjects for one minor version
3. A deprecation warning in the CHANGELOG entry
4. Removal of the old subject in the version after that

Adding a new event class or source token is **not** a breaking change.
