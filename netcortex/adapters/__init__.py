"""Platform adapter registry.

Two registries are maintained:

  _type_registry  — maps adapter type name → adapter class
                    populated at import time via entry points
                    e.g. {"meraki": MerakiAdapter, "catalyst_center": ...}

  _instances      — maps instance_id → live adapter instance
                    populated at startup after the secret backend is available
                    e.g. {"meraki/corp": <MerakiAdapter>, "meraki/branch": <MerakiAdapter>}

Instance IDs are always "{type}/{name}", e.g. "meraki/corp", "catalyst_center/dc1".
The name portion is arbitrary and set in the netcortex/adapters/_index secret.
"""

from __future__ import annotations

from importlib.metadata import entry_points
import structlog

from netcortex.adapters.base import PlatformAdapter

log = structlog.get_logger(__name__)

# Maps adapter type slug → adapter class (populated from entry points)
_type_registry: dict[str, type[PlatformAdapter]] = {}

# Maps instance_id ("{type}/{name}") → live adapter instance
_instances: dict[str, PlatformAdapter] = {}

# Maps instance_id → error string for instances that failed to load
_failed_instances: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Type registry (adapter classes, not instances)
# ---------------------------------------------------------------------------

def load_adapter_types() -> dict[str, type[PlatformAdapter]]:
    """Discover all registered adapter classes via entry points."""
    global _type_registry
    if _type_registry:
        return _type_registry
    eps = entry_points(group="netcortex.adapters")
    for ep in eps:
        try:
            adapter_cls = ep.load()
            _type_registry[ep.name] = adapter_cls
            log.debug("adapter.type.registered", type=ep.name)
        except Exception as exc:
            log.error("adapter.type.load_failed", type=ep.name, error=str(exc))
    return _type_registry


def get_adapter_class(adapter_type: str) -> type[PlatformAdapter]:
    """Return the class for a given adapter type slug."""
    if not _type_registry:
        load_adapter_types()
    if adapter_type not in _type_registry:
        raise KeyError(
            f"No adapter class registered for type {adapter_type!r}. "
            f"Available types: {list(_type_registry)}"
        )
    return _type_registry[adapter_type]


# ---------------------------------------------------------------------------
# Instance registry (live configured adapter instances)
# ---------------------------------------------------------------------------

async def load_instances() -> dict[str, PlatformAdapter]:
    """
    Read the adapter index from the secret backend and instantiate all
    enabled adapter instances. Stores them in _instances keyed by
    "{type}/{name}".

    Call this once at application startup, after the secret backend is ready.
    """
    global _instances, _failed_instances
    from netcortex.secrets import get_secret_backend

    load_adapter_types()
    backend = get_secret_backend()
    index = await backend.get_adapter_index()

    if not index:
        log.warning(
            "adapter.index.empty",
            hint="Add instances to netcortex/adapters/_index in your secret backend",
        )

    new_instances: dict[str, PlatformAdapter] = {}
    new_failed: dict[str, str] = {}

    # Cache type-level configs to avoid redundant secret fetches
    _type_configs: dict[str, dict] = {}

    # Fetch global default from core secret (already done by config, but we
    # need it here for interval resolution before cfg is injected into worker)
    core_cfg = await backend.get_core()
    global_interval_default: int | None = None
    raw_iv = core_cfg.get("sync_interval")
    if raw_iv is not None:
        try:
            global_interval_default = int(raw_iv)
        except (ValueError, TypeError):
            pass

    for entry in index:
        adapter_type = entry.get("type", "")
        instance_name = entry.get("name", "")
        enabled = entry.get("enabled", True)
        instance_id = f"{adapter_type}/{instance_name}"

        if not enabled:
            log.info("adapter.instance.skipped", instance_id=instance_id, reason="disabled")
            continue

        if not adapter_type or not instance_name:
            log.error(
                "adapter.index.invalid_entry",
                entry=entry,
                hint="Each entry needs 'type' and 'name' keys",
            )
            continue

        try:
            cls = get_adapter_class(adapter_type)
        except KeyError as exc:
            err = str(exc)
            log.error("adapter.instance.unknown_type", instance_id=instance_id, error=err)
            new_failed[instance_id] = f"Unknown adapter type '{adapter_type}': {err}"
            continue

        try:
            # Fetch type-level config once per adapter type
            if adapter_type not in _type_configs:
                _type_configs[adapter_type] = await backend.get_adapter_type_config(adapter_type)
            type_cfg = _type_configs[adapter_type]

            config = await backend.get_adapter_config(adapter_type, instance_name)
            instance = cls(config=config, instance_name=instance_name)

            # Resolve interval: instance > type > global-core > None (worker uses cfg default)
            raw = (
                config.get("interval")
                or type_cfg.get("interval")
                or global_interval_default
            )
            if raw is not None:
                try:
                    instance._interval = int(raw)
                except (ValueError, TypeError):
                    pass

            new_instances[instance_id] = instance
            log.info(
                "adapter.instance.loaded",
                instance_id=instance_id,
                interval=instance._interval,
            )
        except Exception as exc:
            err = str(exc)
            log.error("adapter.instance.init_failed", instance_id=instance_id, error=err)
            new_failed[instance_id] = err

    _instances = new_instances
    _failed_instances = new_failed
    return _instances


def get_failed_instances() -> dict[str, str]:
    """Return instance_id → error string for adapters that failed to load."""
    return dict(_failed_instances)


def get_instance(instance_id: str) -> PlatformAdapter:
    """
    Return a live adapter instance by its ID ("{type}/{name}").
    Raises KeyError if not found.
    """
    if instance_id not in _instances:
        raise KeyError(
            f"No adapter instance {instance_id!r}. "
            f"Running instances: {list(_instances)}"
        )
    return _instances[instance_id]


def get_instances(adapter_type: str | None = None) -> dict[str, PlatformAdapter]:
    """
    Return all running adapter instances, optionally filtered by type.

    Examples:
        get_instances()              → all instances
        get_instances("meraki")      → {"meraki/corp": ..., "meraki/branch": ...}
        get_instances("catalyst_center") → {"catalyst_center/dc1": ..., "catalyst_center/dc2": ...}
    """
    if adapter_type is None:
        return dict(_instances)
    return {
        iid: inst
        for iid, inst in _instances.items()
        if iid.startswith(f"{adapter_type}/")
    }


def list_instance_ids(adapter_type: str | None = None) -> list[str]:
    """Return a sorted list of instance IDs, optionally filtered by type."""
    return sorted(get_instances(adapter_type).keys())
