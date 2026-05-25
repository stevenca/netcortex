"""OUI → vendor lookup for MAC addresses.

Uses the bundled IEEE OUI snapshot shipped with ``mac-vendor-lookup``.

The library's public ``MacLookup.lookup()`` wraps an async coroutine in
``loop.run_until_complete()``, which raises (or worse, emits a stranded
coroutine warning) when called from inside an already-running asyncio
event loop — which is exactly the case for the worker's correlation
pass. We bypass that and consult the underlying in-memory prefix dict
directly, giving us a true synchronous, allocation-free hot path.

The lookup table is loaded once at module import (~30k entries) and
results are memoized with ``lru_cache`` so a repeat MAC costs nothing.

If for some reason the import or data load fails we fail-open by
returning ``""`` rather than crashing the correlation pipeline.
"""

from __future__ import annotations

import asyncio
import threading
from functools import lru_cache

import structlog

log = structlog.get_logger(__name__)

# {bytes_OUI_uppercase: bytes_vendor_name} — populated lazily on first use.
_prefixes: dict[bytes, bytes] = {}
_load_lock = threading.Lock()
_loaded = False


def _load_prefixes() -> None:
    """Populate ``_prefixes`` exactly once, safely from any context.

    ``mac_vendor_lookup`` only exposes an async loader. We don't want to
    block on it at module import time (which would race with the worker's
    asyncio loop), so we lazily call it on first need, on a dedicated
    helper thread that owns a fresh event loop. This works whether the
    caller is sync or already inside an async loop.
    """
    global _prefixes, _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        try:
            from mac_vendor_lookup import AsyncMacLookup  # type: ignore[import-untyped]

            async_obj = AsyncMacLookup()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(async_obj.load_vendors())
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True, name="oui-loader")
            t.start()
            t.join(timeout=30)
            if t.is_alive():
                raise TimeoutError("OUI loader thread did not finish in 30s")
            _prefixes = dict(async_obj.prefixes)
            log.info("oui.loaded", entries=len(_prefixes))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("oui.load_failed", error=str(exc))
        finally:
            _loaded = True


def _normalize_mac(mac: str) -> str:
    """Return the MAC in canonical ``AA:BB:CC:DD:EE:FF`` form, or ``""`` if unparseable."""
    if not mac:
        return ""
    hex_chars = [c for c in mac.upper() if c in "0123456789ABCDEF"]
    if len(hex_chars) < 12:
        return ""
    head = "".join(hex_chars[:12])
    return ":".join(head[i : i + 2] for i in range(0, 12, 2))


@lru_cache(maxsize=8192)
def lookup_vendor(mac: str) -> str:
    """Return the IEEE-registered vendor name for ``mac`` or an empty string.

    Accepts any of the common MAC string formats (``aa:bb:cc:dd:ee:ff``,
    ``aabb.ccdd.eeff``, ``AABBCCDDEEFF``). Locally-administered MACs
    (U/L bit set on the first octet), unparseable inputs, and OUIs that
    aren't registered with the IEEE return ``""``.
    """
    norm = _normalize_mac(mac)
    if not norm:
        return ""
    # Locally-administered (bit 1 of the first octet) — no OUI assignment.
    first = int(norm.split(":", 1)[0], 16)
    if first & 0b0000_0010:
        return ""
    if not _loaded:
        _load_prefixes()
    if not _prefixes:
        return ""
    # Library stores the first 3 octets as uppercase hex with no separators.
    oui = norm.replace(":", "")[:6].encode("ascii")
    raw = _prefixes.get(oui)
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
