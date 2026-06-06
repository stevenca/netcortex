"""Tests for the Redis-backed SAML session store (0.8.0-dev11)."""

from __future__ import annotations

import json
import time

import pytest

from netcortex.auth import session as session_mod


class FakeRedis:
    """Minimal async Redis stand-in for the session store."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self.store.pop(k, None)
            self.ttl.pop(k, None)

    async def expire(self, key: str, ttl: int) -> None:
        if key in self.store:
            self.ttl[key] = ttl

    async def ping(self) -> bool:
        return True


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()

    async def _fake_redis() -> FakeRedis:
        return fake

    monkeypatch.setattr(session_mod, "_redis", _fake_redis)
    return fake


async def test_create_and_load_roundtrip(fake_redis: FakeRedis) -> None:
    sid = await session_mod.create_session(
        subject="user@example.com",
        email="user@example.com",
        groups=["netops"],
        name_id="user@example.com",
    )
    assert sid
    record = await session_mod.load_session(sid)
    assert record is not None
    assert record["email"] == "user@example.com"
    assert record["groups"] == ["netops"]


async def test_load_missing_returns_none(fake_redis: FakeRedis) -> None:
    assert await session_mod.load_session("does-not-exist") is None
    assert await session_mod.load_session(None) is None


async def test_destroy_session(fake_redis: FakeRedis) -> None:
    sid = await session_mod.create_session(subject="u")
    await session_mod.destroy_session(sid)
    assert await session_mod.load_session(sid) is None


async def test_absolute_timeout_expires(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_absolute_ttl", lambda: 100)
    sid = await session_mod.create_session(subject="u")
    # Backdate created_at beyond the absolute window.
    key = session_mod._SESSION_PREFIX + sid
    record = json.loads(fake_redis.store[key])
    record["created_at"] = int(time.time()) - 500
    fake_redis.store[key] = json.dumps(record)

    assert await session_mod.load_session(sid) is None
    assert key not in fake_redis.store  # evicted


async def test_idle_ttl_slides_on_access(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "_idle_ttl", lambda: 1800)
    sid = await session_mod.create_session(subject="u")
    key = session_mod._SESSION_PREFIX + sid
    fake_redis.ttl[key] = 5  # pretend it almost expired
    await session_mod.load_session(sid)
    assert fake_redis.ttl[key] == 1800  # slid forward


def test_cookie_helpers_use_secure_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.responses import Response

    resp = Response()
    session_mod.set_session_cookie(resp, "abc123")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    # secure defaults True when settings uninitialized
    assert "secure" in set_cookie.lower()
