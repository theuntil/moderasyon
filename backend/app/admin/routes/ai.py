"""AI ve maliyet yönetimi: sağlayıcı/model/limit/bütçe ayarları, fiyat tablosu, kullanım ve test."""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.admin.auth import AdminUser, audit, current_user, require_role
from app.config import settings
from app.moderation import ai, ai_gateway
from app.platform_settings import SettingsCache

router = APIRouter(prefix="/ai", tags=["ai"])

MODEL_PATTERN = r"^[A-Za-z0-9._:/-]{2,100}$"


class AiSettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    ai_provider: Literal["openai_moderation", "chat"] | None = None
    ai_model: str | None = Field(default=None, pattern=MODEL_PATTERN)
    ai_vision_model: str | None = Field(default=None, pattern=MODEL_PATTERN)
    ai_max_calls_per_minute: int | None = Field(default=None, ge=0, le=100_000)
    ai_daily_budget_usd: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=4)
    ai_monthly_budget_usd: Decimal | None = Field(default=None, ge=0, le=10_000_000, decimal_places=4)
    ai_alert_percent: int | None = Field(default=None, ge=1, le=100)


class PriceBody(BaseModel):
    input_per_1m: Decimal = Field(ge=0, le=10_000, decimal_places=6)
    output_per_1m: Decimal = Field(ge=0, le=10_000, decimal_places=6)


class TestBody(BaseModel):
    text: str = Field(default="Bu bir bağlantı testidir, merhaba!", min_length=1, max_length=500)


@router.get("/overview")
async def overview(request: Request, _: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    tz = settings.panel_timezone
    s = await pool.fetchrow(
        """
        SELECT ai_enabled, ai_provider, ai_model, ai_vision_model, ai_max_calls_per_minute,
               ai_daily_budget_usd, ai_monthly_budget_usd, ai_alert_percent
        FROM platform_settings WHERE id = 1
        """
    )
    totals = await pool.fetchrow(
        """
        WITH d AS (SELECT (now() AT TIME ZONE $1)::date AS today)
        SELECT d.today,
               coalesce(sum(u.cost_usd) FILTER (WHERE u.day = d.today), 0) AS today_cost,
               coalesce(sum(u.reserved_usd) FILTER (WHERE u.day = d.today), 0) AS today_reserved,
               coalesce(sum(u.cost_usd), 0) AS month_cost,
               coalesce(sum(u.calls) FILTER (WHERE u.day = d.today), 0) AS today_calls,
               coalesce(sum(u.errors) FILTER (WHERE u.day = d.today), 0) AS today_errors,
               coalesce(sum(u.skipped) FILTER (WHERE u.day = d.today), 0) AS today_skipped,
               coalesce(sum(u.calls), 0) AS month_calls,
               coalesce(sum(u.input_tokens + u.output_tokens), 0) AS month_tokens,
               round(coalesce(sum(u.latency_ms_total) FILTER (WHERE u.day = d.today), 0)::numeric
                     / nullif(sum(u.calls) FILTER (WHERE u.day = d.today), 0))::bigint AS today_avg_latency
        FROM d LEFT JOIN ai_usage_daily u ON u.day >= date_trunc('month', d.today)::date
        GROUP BY d.today
        """,
        tz,
    )
    series = await pool.fetch(
        """
        WITH days AS (
            SELECT generate_series((now() AT TIME ZONE $1)::date - 29, (now() AT TIME ZONE $1)::date, '1 day')::date AS day
        )
        SELECT days.day, coalesce(sum(u.cost_usd), 0) AS cost, coalesce(sum(u.calls), 0) AS calls,
               coalesce(sum(u.errors), 0) AS errors, coalesce(sum(u.skipped), 0) AS skipped
        FROM days LEFT JOIN ai_usage_daily u ON u.day = days.day
        GROUP BY days.day ORDER BY days.day
        """,
        tz,
    )
    by_project = await pool.fetch(
        """
        SELECT u.project_id, coalesce(p.name, CASE WHEN u.project_id = $2 THEN 'Panel testi' ELSE 'Silinmiş proje' END) AS name,
               p.ai_monthly_budget_usd AS budget, sum(u.calls) AS calls, sum(u.cost_usd) AS cost, sum(u.skipped) AS skipped
        FROM ai_usage_daily u LEFT JOIN projects p ON p.id = u.project_id
        WHERE u.day >= date_trunc('month', (now() AT TIME ZONE $1)::date)::date
        GROUP BY u.project_id, p.name, p.ai_monthly_budget_usd ORDER BY cost DESC, calls DESC
        """,
        tz, ai_gateway.PANEL_PROJECT,
    )
    by_model = await pool.fetch(
        """
        SELECT model, sum(calls) AS calls, sum(errors) AS errors, sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens, sum(cost_usd) AS cost
        FROM ai_usage_daily WHERE day >= date_trunc('month', (now() AT TIME ZONE $1)::date)::date
        GROUP BY model ORDER BY cost DESC, calls DESC
        """,
        tz,
    )
    recent = await pool.fetch(
        """
        SELECT c.id, c.project_id, p.name AS project_name, c.model, c.kind, c.status, c.input_tokens, c.output_tokens,
               c.cost_usd, c.latency_ms, c.error, c.created_at
        FROM ai_calls c LEFT JOIN projects p ON p.id = c.project_id
        ORDER BY c.id DESC LIMIT 30
        """
    )
    alerts = await pool.fetch(
        """
        SELECT id, kind, period, message, created_at FROM ai_alerts
        WHERE created_at >= now() - interval '35 days' ORDER BY id DESC LIMIT 20
        """
    )
    return {
        "settings": dict(s),
        "configured": ai.configured(),
        "base_url": ai.base_url() or None,
        "api_key": ai.masked_key(),
        "moderation_model": settings.ai_moderation_model,
        "circuit": await ai_gateway.circuit_state(request.app.state.redis),
        "totals": dict(totals),
        "series": [dict(r) | {"day": r["day"].isoformat()} for r in series],
        "by_project": [dict(r) | {"project_id": str(r["project_id"])} for r in by_project],
        "by_model": [dict(r) for r in by_model],
        "recent": [dict(r) | {"project_id": str(r["project_id"])} for r in recent],
        "alerts": [dict(r) for r in alerts],
    }


@router.get("/alerts/active")
async def active_alerts(request: Request, _: AdminUser = Depends(current_user)):
    """Panel üst uyarı şeridi için: bu dönemde geçerli kritik uyarılar."""
    rows = await request.app.state.db.fetch(
        """
        WITH d AS (SELECT (now() AT TIME ZONE $1)::date AS today)
        SELECT a.kind, a.message FROM ai_alerts a, d
        WHERE (a.kind = 'daily_exhausted' AND a.period = d.today::text)
           OR (a.kind = 'monthly_exhausted' AND a.period = to_char(d.today, 'YYYY-MM'))
           OR (a.kind IN ('auth_error', 'no_price') AND a.created_at >= now() - interval '1 hour')
        ORDER BY a.id DESC LIMIT 5
        """,
        settings.panel_timezone,
    )
    circuit = await ai_gateway.circuit_state(request.app.state.redis)
    return {"alerts": [dict(r) for r in rows], "circuit": circuit}


@router.patch("/settings")
async def update_ai_settings(body: AiSettingsUpdate, request: Request, user: AdminUser = Depends(require_role("admin"))):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        return {"ok": True}
    async with request.app.state.db.acquire() as conn, conn.transaction():
        before = await conn.fetchrow("SELECT * FROM platform_settings WHERE id = 1 FOR UPDATE")
        provider = changes.get("ai_provider", before["ai_provider"])
        if provider == "chat":
            # Fiyatı bilinmeyen ücretli model seçilemez (maliyeti ölçülemeyen harcama olmasın)
            for key in ("ai_model", "ai_vision_model"):
                model = changes.get(key, before[key])
                if not await conn.fetchval("SELECT 1 FROM ai_model_prices WHERE model = $1", model):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail={"error": "price_required", "field": key,
                                "message": f"'{model}' için önce fiyat tablosuna fiyat ekleyin."},
                    )
        cols = list(changes)
        sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await conn.execute(f"UPDATE platform_settings SET {sets}, updated_by = ${len(cols) + 1}, updated_at = now() WHERE id = 1",
                           *[changes[c] for c in cols], user.id)
        diff = {c: {"old": before[c], "new": changes[c]} for c in cols if before[c] != changes[c]}
        action = "ai.settings_updated"
        if "ai_enabled" in diff:
            action = "ai.enabled" if changes["ai_enabled"] else "ai.disabled"
        await audit(conn, request, user, action, target_type="ai", target_id="platform", details=diff)
    return {"ok": True}


@router.get("/prices")
async def list_prices(request: Request, _: AdminUser = Depends(current_user)):
    rows = await request.app.state.db.fetch(
        """
        SELECT p.model, p.input_per_1m, p.output_per_1m, p.updated_at, u.username AS updated_by
        FROM ai_model_prices p LEFT JOIN admin_users u ON u.id = p.updated_by ORDER BY p.input_per_1m, p.model
        """
    )
    return [dict(r) for r in rows]


@router.put("/prices/{model}")
async def put_price(model: str, body: PriceBody, request: Request, user: AdminUser = Depends(require_role("admin"))):
    import re

    if not re.match(MODEL_PATTERN, model):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"error": "invalid_model"})
    async with request.app.state.db.acquire() as conn, conn.transaction():
        old = await conn.fetchrow("SELECT input_per_1m, output_per_1m FROM ai_model_prices WHERE model = $1", model)
        await conn.execute(
            """
            INSERT INTO ai_model_prices (model, input_per_1m, output_per_1m, updated_by) VALUES ($1, $2, $3, $4)
            ON CONFLICT (model) DO UPDATE SET input_per_1m = EXCLUDED.input_per_1m, output_per_1m = EXCLUDED.output_per_1m,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            model, body.input_per_1m, body.output_per_1m, user.id,
        )
        await audit(conn, request, user, "ai.price_updated", target_type="ai_model", target_id=model,
                    details={"old": dict(old) if old else None, "new": body.model_dump()})
    return {"ok": True}


@router.delete("/prices/{model}")
async def delete_price(model: str, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        s = await conn.fetchrow("SELECT ai_provider, ai_model, ai_vision_model FROM platform_settings WHERE id = 1")
        if s["ai_provider"] == "chat" and model in (s["ai_model"], s["ai_vision_model"]):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail={"error": "model_in_use", "message": "Kullanımdaki modelin fiyatı silinemez."})
        if model == settings.ai_moderation_model:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "model_in_use"})
        deleted = await conn.fetchval("DELETE FROM ai_model_prices WHERE model = $1 RETURNING model", model)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "price_not_found"})
        await audit(conn, request, user, "ai.price_deleted", target_type="ai_model", target_id=model)
    return {"ok": True}


@router.post("/test")
async def test_call(body: TestBody, request: Request, user: AdminUser = Depends(require_role("admin"))):
    """Anahtar, model ve bağlantıyı gerçek bir çağrıyla dener. Maliyeti 'Panel testi' olarak kaydedilir."""
    pool = request.app.state.db
    s = await SettingsCache(ttl=0).get(pool)
    result = await ai_gateway.classify(pool=pool, redis=request.app.state.redis, s=s, project_id=ai_gateway.PANEL_PROJECT,
                                       request_id=None, text=body.text, images=[])
    async with pool.acquire() as conn:
        await audit(conn, request, user, "ai.test", target_type="ai", target_id="platform",
                    details={"status": result.status, "model": result.model})
    top = sorted((result.scores or {}).items(), key=lambda kv: -kv[1])[:5]
    return {"status": result.status, "model": result.model, "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "top_scores": [{"name": k, "score": v} for k, v in top]}


@router.post("/circuit/reset")
async def reset_circuit(request: Request, user: AdminUser = Depends(require_role("admin"))):
    await ai_gateway.reset_circuit(request.app.state.redis)
    async with request.app.state.db.acquire() as conn:
        await audit(conn, request, user, "ai.circuit_reset", target_type="ai", target_id="platform")
    return {"ok": True}
