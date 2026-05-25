"""Webhook receivers for inbound platform events.

Each platform sub-module:
  1. Validates the incoming request (HMAC, token header, etc.)
  2. Parses the payload
  3. Enqueues a targeted sync event onto Redis so an ingest worker can
     refresh just the affected resource without waiting for the next
     scheduled poll cycle.

Secret paths (in the configured secret backend):
  netcortex/webhooks/meraki           → {"shared_secret": "..."}
  netcortex/webhooks/catalyst_center  → {"shared_secret": "..."}
"""

from netcortex.webhooks.router import router

__all__ = ["router"]
