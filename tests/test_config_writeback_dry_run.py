"""Unit tests for the netbox_writeback_dry_run config knob.

This setting controls whether the worker's periodic NetBox writeback loop
performs real PATCH/POST/DELETE calls or only computes the diff. It is
intentionally configurable via both env var and the core secret so that
operators can flip it during a release rollout (env var) or pin it as part
of the deployment baseline (core secret).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from netcortex.config import BootstrapSettings, Settings


def _make_settings(monkeypatch: pytest.MonkeyPatch, *, env_value: str | None) -> Settings:
    if env_value is None:
        monkeypatch.delenv("NETBOX_WRITEBACK_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("NETBOX_WRITEBACK_DRY_RUN", env_value)
    monkeypatch.setenv("SECRET_BACKEND", "aws_sm")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_SECRET_PREFIX", "netcortex")
    bootstrap = BootstrapSettings()  # type: ignore[call-arg]
    return Settings(bootstrap)


def test_default_is_false_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _make_settings(monkeypatch, env_value=None)
    assert s.netbox_writeback_dry_run is False


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "YES", "on", "On"])
def test_env_var_truthy_values_enable_dry_run(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    s = _make_settings(monkeypatch, env_value=truthy)
    assert s.netbox_writeback_dry_run is True


@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off", "", "anything"])
def test_env_var_non_truthy_values_keep_writes_on(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    s = _make_settings(monkeypatch, env_value=falsy)
    assert s.netbox_writeback_dry_run is False


@pytest.mark.asyncio
async def test_hydrate_promotes_core_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with env unset, a `netbox_writeback_dry_run` in the core secret wins."""
    s = _make_settings(monkeypatch, env_value=None)
    assert s.netbox_writeback_dry_run is False

    fake_core = {
        "netbox_url": "https://nb.example.test",
        "netbox_token": "tok",
        "netbox_writeback_dry_run": True,
    }
    fake_backend = AsyncMock()
    fake_backend.get_core = AsyncMock(return_value=fake_core)

    with patch("netcortex.secrets.get_secret_backend", return_value=fake_backend):
        await s.hydrate()

    assert s.netbox_writeback_dry_run is True


@pytest.mark.asyncio
async def test_hydrate_accepts_string_form_in_core_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_settings(monkeypatch, env_value=None)
    fake_core = {
        "netbox_url": "https://nb.example.test",
        "netbox_token": "tok",
        "netbox_writeback_dry_run": "yes",
    }
    fake_backend = AsyncMock()
    fake_backend.get_core = AsyncMock(return_value=fake_core)
    with patch("netcortex.secrets.get_secret_backend", return_value=fake_backend):
        await s.hydrate()
    assert s.netbox_writeback_dry_run is True
