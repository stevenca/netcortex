"""Regression tests for the webhook → secret-backend call paths.

The bug fixed in 0.8.0-dev9 was that ``webhooks/meraki.py`` and
``webhooks/catalyst_center.py`` called ``backend.get_secret(...)``,
which is **not** a real method on :class:`AwsSecretsManagerBackend`.
The bare ``except Exception`` swallowed the resulting
``AttributeError``, leaving ``_SECRET_CACHE`` empty and causing
:func:`handle_meraki_webhook` to fall through to the
"no-secret-configured" branch — which accepts the webhook **without
HMAC verification**, just with a warning.

The bug was not caught by existing route tests because those tests
inject the secret directly into ``_SECRET_CACHE`` via fixture,
bypassing the buggy fetch path.

These tests stand up a fake backend that exposes the real backend's
shape (``get(path, required=False) -> dict | None``) and assert that
the webhook modules:

1. Call the correct method (``get``, not ``get_secret``).
2. Pass ``required=False`` so a missing tenant returns ``None``
   without raising.
3. Return the extracted ``shared_secret`` when the backend has one.
4. Return ``None`` (gracefully) when the backend has nothing.
5. Log and return ``None`` on a backend exception (don't 5xx).
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeBackend:
    """Mimics the real ``AwsSecretsManagerBackend.get`` signature.

    The shape is intentionally drawn from
    ``inspect.signature(backend.get)`` on a live backend:
    ``get(path: str, required: bool = True) -> dict[str, Any]``.

    We track calls so tests can assert how the webhook module invoked
    us — in particular, that it used ``get`` (not ``get_secret``) and
    that it passed ``required=False``.
    """

    def __init__(self, data: dict[str, dict[str, Any]] | None = None) -> None:
        self._data = data or {}
        self.calls: list[tuple[str, bool]] = []
        self.raise_on_get: Exception | None = None

    async def get(self, path: str, required: bool = True) -> dict[str, Any] | None:
        self.calls.append((path, required))
        if self.raise_on_get is not None:
            raise self.raise_on_get
        if path in self._data:
            return self._data[path]
        if required:
            # Mimics the real backend's SecretNotFoundError behavior.
            raise KeyError(f"secret not found: {path}")
        return None


@pytest.fixture
def reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the module-level secret caches so each test sees a cold path."""
    from netcortex.webhooks import meraki as meraki_module
    from netcortex.webhooks import catalyst_center as catc_module

    monkeypatch.setattr(meraki_module, "_SECRET_CACHE", {})
    monkeypatch.setattr(catc_module, "_SECRET_CACHE", {})


# ---------------------------------------------------------------------------
# Meraki
# ---------------------------------------------------------------------------


async def test_meraki_secret_fetch_uses_correct_backend_method(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    fake = FakeBackend({
        "netcortex/webhooks/meraki/PROD_TENANT": {"shared_secret": "test-secret-meraki"},
    })
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.meraki import _get_shared_secret

    secret = await _get_shared_secret("PROD_TENANT")
    assert secret == "test-secret-meraki"
    # Confirms the dev9 fix: must call ``get(path, required=False)``,
    # NOT ``get_secret(path)``.
    assert fake.calls == [("netcortex/webhooks/meraki/PROD_TENANT", False)]


async def test_meraki_secret_fetch_missing_tenant_returns_none(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    """A tenant with no stored secret returns None without raising —
    the route then accepts (with a loud warning) so operators can
    bootstrap the integration."""
    fake = FakeBackend({})
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.meraki import _get_shared_secret

    secret = await _get_shared_secret("UNCONFIGURED_TENANT")
    assert secret is None
    assert fake.calls == [("netcortex/webhooks/meraki/UNCONFIGURED_TENANT", False)]


async def test_meraki_secret_fetch_backend_failure_logged_and_none(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    """A transient backend error must not 5xx the webhook route —
    log and return None, route degrades to no-secret-configured path."""
    fake = FakeBackend({})
    fake.raise_on_get = RuntimeError("AWS Secrets Manager unreachable")
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.meraki import _get_shared_secret

    secret = await _get_shared_secret("PROD_TENANT")
    assert secret is None


async def test_meraki_secret_fetch_caches_result(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    """Second call hits the cache, not the backend."""
    fake = FakeBackend({
        "netcortex/webhooks/meraki/PROD_TENANT": {"shared_secret": "test-secret-meraki"},
    })
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.meraki import _get_shared_secret

    s1 = await _get_shared_secret("PROD_TENANT")
    s2 = await _get_shared_secret("PROD_TENANT")
    assert s1 == s2 == "test-secret-meraki"
    assert len(fake.calls) == 1  # second call cached


# ---------------------------------------------------------------------------
# Catalyst Center (same bug, same fix, same test surface)
# ---------------------------------------------------------------------------


async def test_catalyst_center_secret_fetch_uses_correct_backend_method(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    fake = FakeBackend({
        "netcortex/webhooks/catalyst_center/cpn-ful-catc1": {
            "shared_secret": "test-secret-catc",
        },
    })
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.catalyst_center import _get_shared_secret

    secret = await _get_shared_secret("cpn-ful-catc1")
    assert secret == "test-secret-catc"
    assert fake.calls == [
        ("netcortex/webhooks/catalyst_center/cpn-ful-catc1", False)
    ]


async def test_catalyst_center_secret_fetch_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch, reset_caches: None
) -> None:
    fake = FakeBackend({})
    monkeypatch.setattr(
        "netcortex.secrets.get_secret_backend", lambda: fake
    )

    from netcortex.webhooks.catalyst_center import _get_shared_secret

    secret = await _get_shared_secret("UNCONFIGURED_TENANT")
    assert secret is None
