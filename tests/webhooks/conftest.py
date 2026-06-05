"""Shared fixtures for webhook integration tests.

This is the first test module in the repo that drives FastAPI routes
end-to-end (previous tests stayed below the HTTP layer). The fixtures
here exist to keep that pattern consistent for the dev9/dev10/dev11
vendor receivers that will land in subsequent releases.

Design choices
--------------
* We build a **minimal** FastAPI app per test, mounting only the
  ``webhook_router``. This avoids dragging in the full ``netcortex.main``
  app, which needs Neo4j, Redis, the secret backend, MCP, etc. — none
  of which a webhook route touches.

* The bus is a small in-test :class:`CapturingEventBus` that records
  every publish in a list. We deliberately do not use the production
  :class:`NatsEventBus` (would need a NATS server) or the
  :class:`InMemoryEventBus` from the contract tests (which is built for
  async-iterator subscriptions — overkill here). The capturing bus
  exercises the full validation path via :class:`SensoryPublisher`
  while letting tests assert synchronously on what was published.

* HMAC shared secrets are injected directly into the
  :mod:`netcortex.webhooks.meraki` module-level cache so tests do not
  reach the AWS Secrets Manager backend.

* Adapter instances are patched to an empty dict so the
  ``BackgroundTasks`` adapter-sync callback no-ops cleanly.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from netcortex.thalamus import SensoryPublisher
from netcortex.webhooks.router import router as webhook_router


class CapturingEventBus:
    """Test-only :class:`EventBus` substitute that records every publish.

    Implements the same async ``publish`` / ``subscribe`` / ``close``
    surface so :class:`SensoryPublisher` accepts it. Subscribe is a
    no-op iterator because the webhook tests assert on the publish
    side only — runner/handler behavior is covered separately in
    ``tests/reflex/`` against the real :class:`InMemoryEventBus`.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.publish_failures: list[Exception] = []
        self.closed = False

    async def publish(
        self,
        subject: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self.publish_failures:
            raise self.publish_failures.pop(0)
        self.published.append((subject, dict(payload)))

    async def subscribe(self, pattern: str) -> Any:  # pragma: no cover - unused
        async def _empty() -> Any:
            if False:
                yield None
        return _empty()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def capturing_bus() -> CapturingEventBus:
    return CapturingEventBus()


@pytest.fixture
def webhook_app(capturing_bus: CapturingEventBus) -> FastAPI:
    """Minimal FastAPI app with just the webhook router and a captured bus."""
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.event_bus = capturing_bus
    app.state._sensory_publishers = {
        "meraki_webhook": SensoryPublisher(capturing_bus, source="meraki_webhook"),
    }
    return app


@pytest.fixture
def webhook_client(webhook_app: FastAPI) -> TestClient:
    """Synchronous TestClient against the minimal webhook app."""
    return TestClient(webhook_app)


@pytest.fixture
def webhook_app_no_bus() -> FastAPI:
    """Variant with ``event_bus = None`` — exercises the
    publisher-unavailable degradation path."""
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.event_bus = None
    app.state._sensory_publishers = {}
    return app


@pytest.fixture
def webhook_client_no_bus(webhook_app_no_bus: FastAPI) -> TestClient:
    return TestClient(webhook_app_no_bus)


@pytest.fixture
def meraki_shared_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """Inject a known HMAC shared secret for instance 'TEST_TENANT'.

    Bypasses the AWS Secrets Manager backend entirely by pre-populating
    the module-level ``_SECRET_CACHE`` dict.
    """
    from netcortex.webhooks import meraki as meraki_module

    secret = "test-shared-secret-do-not-use-in-prod"  # noqa: S105 — fixture-scoped test secret
    monkeypatch.setitem(meraki_module._SECRET_CACHE, "TEST_TENANT", secret)
    return secret


@pytest.fixture
def meraki_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``_get_shared_secret`` returns None — exercises the
    'no signing key configured' degradation path (still accepts the
    request but emits a warning)."""
    from netcortex.webhooks import meraki as meraki_module

    async def _no_secret(_instance_name: str) -> str | None:
        return None

    monkeypatch.setattr(meraki_module, "_get_shared_secret", _no_secret)


@pytest.fixture(autouse=True)
def stub_adapter_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``get_instances`` to return an empty mapping for every
    webhook test, so the ``BackgroundTasks`` adapter-sync callback
    finishes cleanly instead of trying to reach a real Meraki API.
    """
    from netcortex.adapters import __init__ as adapters_init  # noqa: F401

    def _empty_instances() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        "netcortex.adapters.get_instances", _empty_instances
    )
