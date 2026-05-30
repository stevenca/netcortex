"""MCP tools for sync engine visibility and control."""

from netcortex.mcp.server import mcp


@mcp.tool()
async def reconcile_netbox_inventory(dry_run: bool = True) -> dict:
    """
    Reconcile observed NetCortex graph state back to NetBox inventory.

    Runs four additive passes:
      1. serial_fill   — fills blank NetBox serials from observed graph values.
      2. interfaces    — creates interfaces missing from NetBox.
      3. ip_addresses  — creates IPs in IPAM and assigns them to interfaces.
      4. cables        — creates cables from high-confidence LLDP/CDP links
                         between matched NetBox devices.

    Also returns a read-only analysis section:
      - site_mismatches: devices whose observed state differs from NetBox intent.
      - absent_devices:  devices in the graph but not in NetBox (candidates for
                         manual creation or cleanup).

    dry_run: when True (default), computes full diff but makes NO changes to
             NetBox. Set dry_run=False to apply changes.

    Returns a structured report with per-pass summaries and change lists.
    """
    try:
        from netcortex.config import get_settings
        cfg = get_settings()
    except Exception as exc:
        return {"error": f"config not ready: {exc}"}

    if not (cfg.netbox_url and cfg.netbox_token):
        return {"error": "NetBox not configured (netbox_url / netbox_token missing)"}

    from netcortex.sync.netbox_writeback import reconcile_to_netbox
    return await reconcile_to_netbox(
        cfg.netbox_url,
        cfg.netbox_token,
        verify_ssl=cfg.netbox_verify_ssl,
        dry_run=dry_run,
    )


@mcp.tool()
async def get_netbox_discrepancies() -> dict:
    """
    Return a read-only analysis of discrepancies between NetCortex and NetBox.

    Reports:
      - site_mismatches: devices where the observed site or field values differ
        from what NetBox says (netbox_delta is populated on the Device node).
      - absent_devices: devices present in the graph but not in NetBox — these
        may need to be added to NetBox manually or investigated as rogue devices.

    No writes are made; safe to call any time.
    """
    from netcortex.sync.netbox_writeback import (
        analyse_site_mismatches,
        analyse_absent_devices,
    )
    site_mismatches = await analyse_site_mismatches()
    absent_devices  = await analyse_absent_devices()
    return {
        "site_mismatches": site_mismatches,
        "absent_devices":  absent_devices,
        "counts": {
            "site_mismatches": len(site_mismatches),
            "absent_devices":  len(absent_devices),
        },
    }


@mcp.tool()
async def get_sync_status(adapter: str | None = None) -> dict:
    """
    Return sync status for one or all adapter instances.

    adapter: optional instance ID filter, e.g. "meraki/corp", or type prefix "meraki".
             Omit to return status for all instances.
    """
    # TODO: read from Redis sync state cache, keyed by instance_id
    return {"instances": {}}


@mcp.tool()
async def get_pending_diffs(
    adapter: str | None = None,
    diff_type: str | None = None,
) -> dict:
    """
    Return changes detected by the sync engine not yet reconciled with NetBox.

    adapter: optional instance ID filter, e.g. "meraki/corp".
    diff_type: optional filter — "added", "removed", or "changed".
    """
    # TODO: read pending diffs from Redis, keyed by instance_id
    return {"diffs": [], "count": 0}


@mcp.tool()
async def trigger_sync(adapter: str, scope: str = "all") -> dict:
    """
    Manually trigger a sync for one adapter instance or all instances of a type.

    adapter: instance ID ("meraki/corp") or type prefix ("meraki" for all Meraki instances).
    scope:   "devices" | "interfaces" | "vlans" | "topology" | "all" (default)
    """
    # TODO: enqueue sync job(s) via APScheduler or Celery, keyed by instance_id
    return {"adapter": adapter, "scope": scope, "jobs_queued": [], "error": "not implemented"}
