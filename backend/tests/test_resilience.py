"""Karar modları ve dayanıklılık: iki/üç adım, belirsiz içerik, yanıt sözleşmesi ve arıza senaryoları.

Arıza testleri API'yi süreç içinde (ASGI) çalıştırır ve Redis/işçi/veritabanı arızalarını taklit eder.
Her durumda istemci JSON yanıt alır; hiçbir istek asılı kalmaz.
"""
import asyncio
import secrets

import httpx
import pytest

from app.config import settings
from app.db import create_pool
from app.platform_settings import SettingsCache
from conftest import api_client


@pytest.fixture()
def proj(owner):
    slug = f"res-{secrets.token_hex(4)}"
    data = owner.post("/projects", {"name": slug, "slug": slug}).json()
    yield {"id": data["id"], "key": data["api_key"], "api": api_client(data["api_key"])}
    owner.delete(f"/projects/{data['id']}", {"confirm_slug": slug})


def text(api, t):
    return api.post("/v1/moderate?wait=true", json={"type": "text", "text": t}).json()


# ---------------------------------------------------------------- karar modları

def test_two_step_is_default_and_never_returns_review(owner, proj):
    assert owner.get(f"/projects/{proj['id']}/policy").json()["policy"]["decision_mode"] == "two_step"
    body = text(proj["api"], "salak mısın")                   # risk 0.60: belirsiz bölge, AI kapalı
    assert body["decision"] == "block" and body["reason"] == "harassment_detected"
    assert text(proj["api"], "Harika bir gün")["decision"] == "allow"
    assert owner.get(f"/review?project_id={proj['id']}").json() == []    # iki adımda insan kuyruğu yok
    for key in ("id", "type", "status", "decision", "reason", "created_at"):
        assert key in body


def test_uncertain_action_and_three_step(owner, proj):
    owner.client.put(f"/projects/{proj['id']}/policy", json={"uncertain_action": "allow"})
    assert text(proj["api"], "salak mısın")["decision"] == "allow"
    assert text(proj["api"], "amk siktir")["decision"] == "block"          # kesin engel etkilenmez
    owner.client.put(f"/projects/{proj['id']}/policy", json={"decision_mode": "three_step"})
    assert text(proj["api"], "salak mısın")["decision"] == "review"
    bad = owner.client.put(f"/projects/{proj['id']}/policy", json={"decision_mode": "two_step", "fallback_decision": "review"})
    assert bad.status_code == 422


# ---------------------------------------------------------------- arıza senaryoları (süreç içi API)

class FakeJob:
    def __init__(self, delay: float):
        self.delay = delay

    async def result(self, timeout=None, poll_delay=None):
        await asyncio.sleep(min(self.delay, timeout or self.delay))
        raise TimeoutError


class FakeArq:
    """Redis/işçi arızası taklidi: enqueue hata verir veya iş hiç tamamlanmaz."""
    def __init__(self, mode: str, delay: float = 0.2):
        self.mode, self.delay = mode, delay

    async def enqueue_job(self, *args, **kwargs):
        if self.mode == "redis_down":
            raise ConnectionError("redis down")
        return FakeJob(self.delay)

    def __getattr__(self, name):          # rate limit, auth sayacı vb. → Redis yok gibi davran
        async def broken(*a, **k):
            raise ConnectionError("redis down")
        if name == "pipeline":
            return lambda *a, **k: (_ for _ in ()).throw(ConnectionError("redis down"))
        return broken


class FlakyPool:
    """Gerçek havuz; down=True iken veritabanı erişilemez."""
    def __init__(self, pool):
        self.pool, self.down = pool, False

    def _check(self):
        if self.down:
            raise OSError("database down")

    async def fetchrow(self, *a, **k):
        self._check()
        return await self.pool.fetchrow(*a, **k)

    async def fetchval(self, *a, **k):
        self._check()
        return await self.pool.fetchval(*a, **k)

    async def fetch(self, *a, **k):
        self._check()
        return await self.pool.fetch(*a, **k)

    async def execute(self, *a, **k):
        self._check()
        return await self.pool.execute(*a, **k)

    def acquire(self):
        self._check()
        return self.pool.acquire()


def run_app(fn, arq_mode: str, delay: float = 0.2):
    from app.api import deps
    from app.api.main import app

    async def main():
        pool = await create_pool(max_size=5)
        flaky = FlakyPool(pool)
        app.state.db, app.state.arq, app.state.settings_cache = flaky, FakeArq(arq_mode, delay), SettingsCache()
        deps._auth_cache.clear()
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
                return await fn(client, flaky)
        finally:
            await pool.close()
    return asyncio.run(main())


def test_redis_down_text_is_processed_inline(proj):
    async def t(client, db):
        r = await client.post("/v1/moderate", json={"type": "text", "text": "amk ya"},
                              headers={"Authorization": f"Bearer {proj['key']}"})
        return r.status_code, r.json()
    code, body = run_app(t, "redis_down")
    assert code == 200 and body["status"] == "completed" and body["decision"] == "block"
    assert proj["api"].get(f"/v1/moderate/{body['id']}").json()["decision"] == "block"   # kaydedildi


def test_worker_down_wait_true_processed_by_api_once(proj):
    async def t(client, db):
        r = await client.post("/v1/moderate?wait=true", json={"type": "text", "text": "merhaba dünya"},
                              headers={"Authorization": f"Bearer {proj['key']}"})
        body = r.json()
        n = await db.pool.fetchval(
            "SELECT count(*) FROM moderation_results res JOIN moderation_requests r ON r.id = res.request_id WHERE r.public_id = $1",
            body["id"])
        return r.status_code, body, n
    code, body, results = run_app(t, "worker_down")
    assert code == 200 and body["decision"] == "allow" and results == 1


def test_database_down_degraded_and_unprocessed(owner, proj):
    owner.client.put(f"/projects/{proj['id']}/policy", json={"fallback_decision": "block"})

    async def t(client, db):
        h = {"Authorization": f"Bearer {proj['key']}"}
        warm = await client.post("/v1/moderate", json={"type": "text", "text": "ısınma"}, headers=h)
        db.down = True
        degraded = await client.post("/v1/moderate", json={"type": "text", "text": "siktir git"}, headers=h)
        media = await client.post("/v1/moderate", json={"type": "image", "url": "https://example.com/a.jpg"}, headers=h)
        unknown = await client.post("/v1/moderate", json={"type": "text", "text": "x"},
                                    headers={"Authorization": "Bearer mk_live_" + "z" * 40})
        return warm, degraded, media, unknown
    from app.api import deps
    deps._auth_cache.clear()
    warm, degraded, media, unknown = run_app(t, "redis_down")
    assert warm.status_code == 200
    d = degraded.json()
    assert degraded.status_code == 200 and d["degraded"] is True and d["persisted"] is False
    assert d["decision"] == "block" and d["id"] is None                    # veritabanı yokken de karar verildi
    m = media.json()
    assert media.status_code == 503 and m["status"] == "unprocessed" and m["retryable"] is True
    assert m["fallback_decision"] == "block" and m["decision"] is None
    u = unknown.json()
    assert unknown.status_code == 503 and u["status"] == "unprocessed" and u["retryable"] is True


def test_request_never_hangs(proj, monkeypatch):
    monkeypatch.setattr(settings, "request_timeout_s", 1.5)
    monkeypatch.setattr(settings, "sync_wait_timeout_s", 10)

    async def t(client, db):
        r = await client.post("/v1/moderate?wait=true", json={"type": "text", "text": "yavaş işçi"},
                              headers={"Authorization": f"Bearer {proj['key']}"})
        return r.status_code, r.json()
    code, body = run_app(t, "worker_down", delay=10)
    assert code == 503 and body["status"] == "unprocessed" and body["error"] == "timeout" and body["retryable"] is True
