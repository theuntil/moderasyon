"""AI kapısı: maliyet muhasebesi, kesin bütçe, devre kesici, limitler ve sağlayıcı formatları.

Gerçek veritabanı ve Redis kullanır, OpenAI'ı taklit eden sahte sunucuya karşı çalışır. Mevcut harcamayı
okuyup bütçeleri onun üzerine kurar; ortamdaki verileri silmez veya değiştirmez.
"""
import asyncio
import json
import threading
import time
import uuid
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from app.config import settings
from app.db import create_pool
from app.moderation import ai, ai_gateway
from app.platform_settings import PlatformSettings

BASE = PlatformSettings(True, None, 0.5, 0.85, 1, 20, 600, 30, True, "chat", "gpt-5-nano", "gpt-5-nano", 0,
                        Decimal("1000"), Decimal("100000"), 80)


class _OpenAI(BaseHTTPRequestHandler):
    seen: list = []
    status = 200
    delay = 0.0

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _OpenAI.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        time.sleep(_OpenAI.delay)
        if _OpenAI.status != 200:
            self.send_response(_OpenAI.status)
            self.end_headers()
            return
        if self.path.endswith("/moderations"):
            item = body["input"][0]
            scores = {"harassment": 0.91, "harassment/threatening": 0.2, "sexual/minors": 0.0,
                      "violence": 0.93 if item["type"] == "image_url" else 0.0, "illicit/violent": 0.4}
            reply = {"results": [{"flagged": True, "category_scores": scores}]}
        else:
            reply = {"choices": [{"message": {"content": '{"scores": {"weapons": 0.88}}'}}],
                     "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "ai_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "ai_api_key", "sk-test-1234567890")
    _OpenAI.seen, _OpenAI.status, _OpenAI.delay = [], 200, 0.0
    yield _OpenAI
    server.shutdown()
    server.server_close()


def run(fn):
    async def main():
        from redis.asyncio import Redis
        pool = await create_pool(max_size=12)
        redis = Redis.from_url(settings.redis_url)
        await ai_gateway.reset_circuit(redis)
        try:
            return await fn(pool, redis)
        finally:
            await ai_gateway.reset_circuit(redis)
            await redis.aclose()
            await pool.close()
    return asyncio.run(main())


async def spent(pool):
    return await pool.fetchrow(
        """
        WITH d AS (SELECT (now() AT TIME ZONE $1)::date AS today)
        SELECT coalesce(sum(cost_usd + reserved_usd) FILTER (WHERE day = d.today), 0) AS today,
               coalesce(sum(cost_usd + reserved_usd), 0) AS month
        FROM d LEFT JOIN ai_usage_daily u ON u.day >= date_trunc('month', d.today)::date GROUP BY d.today
        """,
        settings.panel_timezone,
    )


def call(pool, redis, s, text="bir test metni", images=None, project=None, request_id=None):
    return ai_gateway.classify(pool=pool, redis=redis, s=s, project_id=project or uuid.uuid4(),
                               request_id=request_id, text=text, images=images or [])


def test_chat_cost_is_computed_from_real_usage(fake):
    async def t(pool, redis):
        rid = uuid.uuid4()
        before = await spent(pool)
        r = await call(pool, redis, BASE, request_id=rid)
        assert r.status == "ok" and r.scores["weapons"] == 0.88
        assert r.cost_usd == Decimal("0.000130")                       # 1000*0.05/1M + 200*0.40/1M
        row = await pool.fetchrow("SELECT status, cost_usd, input_tokens, output_tokens FROM ai_calls WHERE request_id = $1", rid)
        assert row["cost_usd"] == Decimal("0.000130") and row["input_tokens"] == 1000
        after = await spent(pool)
        assert after["today"] - before["today"] == Decimal("0.000130")  # rezervasyon tamamen kapandı
        body = fake.seen[0]["body"]
        assert "temperature" not in body and body["reasoning_effort"] == "minimal"
        assert fake.seen[0]["auth"] == "Bearer sk-test-1234567890"
    run(t)


def test_budget_exhausted_blocks_without_calling(fake):
    async def t(pool, redis):
        now = await spent(pool)
        s = replace(BASE, ai_daily_budget_usd=now["today"])            # kalan bütçe: 0
        r = await call(pool, redis, s)
        assert r.status == "budget_daily" and r.scores is None
        assert fake.seen == []                                          # OpenAI'a hiç gidilmedi
        s = replace(BASE, ai_monthly_budget_usd=now["month"])
        assert (await call(pool, redis, s)).status == "budget_monthly"
    run(t)


def test_concurrent_calls_never_exceed_budget(fake):
    fake.delay = 0.4
    async def t(pool, redis):
        est_in, est_out = ai.estimate_tokens("eşzamanlı", 0)
        estimate = ai_gateway.cost_of(est_in, est_out, (Decimal("0.05"), Decimal("0.40")))
        now = await spent(pool)
        allowance = estimate * 3 + Decimal("0.000001")
        s = replace(BASE, ai_daily_budget_usd=now["today"] + allowance)
        results = await asyncio.gather(*[call(pool, redis, s, text="eşzamanlı") for _ in range(10)])
        ok = [r for r in results if r.status == "ok"]
        assert 1 <= len(ok) <= 3 and len(fake.seen) == len(ok)
        assert all(r.status in ("ok", "budget_daily") for r in results)
        after = await spent(pool)
        assert after["today"] - now["today"] <= allowance               # bütçe asla aşılmadı
    run(t)


def test_project_budget(fake, owner):
    p = owner.post("/projects", {"name": "bütçe", "slug": f"butce-{uuid.uuid4().hex[:6]}"}).json()
    assert owner.client.put(f"/projects/{p['id']}/ai-budget", json={"monthly_budget_usd": 0.00001}).status_code == 200
    async def t(pool, redis):
        assert (await call(pool, redis, BASE, project=uuid.UUID(p["id"]))).status == "budget_project"
        assert fake.seen == []
    run(t)


def test_circuit_breaker_and_auth_error(fake):
    async def t(pool, redis):
        fake.status = 500
        statuses = [(await call(pool, redis, BASE)).status for _ in range(5)]
        assert statuses == ["error"] * 5
        sent = len(fake.seen)
        assert (await call(pool, redis, BASE)).status == "circuit_open" and len(fake.seen) == sent
        await ai_gateway.reset_circuit(redis)
        fake.status = 401
        assert (await call(pool, redis, BASE)).status == "auth_error"
        assert await ai_gateway.circuit_state(redis) == "auth_error"
        assert await pool.fetchval("SELECT 1 FROM ai_alerts WHERE kind = 'auth_error' AND created_at > now() - interval '1 hour'")
    run(t)


def test_unknown_price_kill_switch_and_minute_cap(fake):
    async def t(pool, redis):
        assert (await call(pool, redis, replace(BASE, ai_model="gpt-bilinmeyen"))).status == "no_price"
        assert (await call(pool, redis, replace(BASE, ai_enabled=False))).status == "disabled"
        current = int(await redis.get(f"ai:calls:{int(time.time() // 60)}") or 0)
        capped = replace(BASE, ai_max_calls_per_minute=current + 1)
        first, second = await call(pool, redis, capped), await call(pool, redis, capped)
        assert first.status == "ok" and second.status == "cap_minute"
        assert len(fake.seen) == 1
    run(t)


def test_free_moderation_provider_ignores_budget(fake):
    async def t(pool, redis):
        s = replace(BASE, ai_provider="openai_moderation", ai_daily_budget_usd=Decimal(0), ai_monthly_budget_usd=Decimal(0))
        r = await call(pool, redis, s, text="hakaret", images=[Image.new("RGB", (32, 32))])
        assert r.status == "ok" and r.cost_usd == 0
        assert r.scores["harassment"] == 0.91 and r.scores["violence"] == 0.93 and r.scores["illicit"] == 0.4
        assert len(fake.seen) == 2 and all(c["path"] == "/v1/moderations" for c in fake.seen)
    run(t)


def test_images_with_nudity_are_never_sent():
    assert not ai.may_send_images({"nudity": 0.4}) and not ai.may_send_images({"sexual": 0.9})
    assert ai.may_send_images({"harassment": 0.9, "suggestive": 0.4})


def test_admin_ai_api(owner):
    ov = owner.get("/ai/overview").json()
    assert {"settings", "totals", "series", "by_project", "by_model", "recent", "alerts"} <= set(ov)
    assert len(ov["series"]) == 30
    bad = owner.patch("/ai/settings", {"ai_provider": "chat", "ai_model": "fiyatsiz-model"})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "price_required"
    assert owner.client.put("/ai/prices/test-model-x", json={"input_per_1m": 0.1, "output_per_1m": 0.2}).status_code == 200
    assert any(p["model"] == "test-model-x" for p in owner.get("/ai/prices").json())
    assert owner.delete("/ai/prices/test-model-x").status_code == 200
    assert owner.patch("/ai/settings", {"ai_daily_budget_usd": -1}).status_code == 422
    from conftest import create_admin
    viewer = create_admin("viewer")
    assert viewer.patch("/ai/settings", {"ai_enabled": False}).status_code == 403
    assert viewer.get("/ai/overview").status_code == 200
