"""In-app authentication for the NetCortex web surface (0.8.0-dev11).

Provides a SAML 2.0 Service Provider (Okta-tested) that gates human
browser access to the UI/API behind an IdP login, plus a server-side
(Redis-backed) session layer. Machine callers (MCP, ``api_secret``
bearer, webhook HMAC/token) are unaffected — they never touch SAML.

Submodules:
  * ``session`` — opaque CSPRNG session ids in a Secure/HttpOnly cookie,
    backed by Redis with idle + absolute timeouts.
  * ``saml``    — python3-saml SP settings + request adapters.
  * ``router``  — /saml/login, /saml/acs, /saml/metadata, /saml/logout.
"""
