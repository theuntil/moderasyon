"""AI kapısı: her Layer 2 çağrısı buradan geçer.

Sıra:
1. Genel anahtar (panel) ve yapılandırma (API anahtarı) kontrolü
2. Devre kesici: sağlayıcı art arda hata veriyorsa bir süre hiç denenmez (istekler boşuna beklemez)
3. Dakikalık çağrı sınırı (sağlayıcının rate limit'ine ve maliyete karşı sigorta)
4. Fiyat: ücretli bir modelin fiyatı tanımlı değilse çağrı yapılmaz (maliyeti bilinmeyen harcama olmaz)
5. Bütçe rezervasyonu: tahmini maliyet günlük/aylık/proje bütçesinden veritabanı kilidi altında ÖNCEDEN
   ayrılır. Eşzamanlı çağrılar bütçeyi aşamaz (kesin sınır).
6. Çağrı → gerçek token sayısıyla maliyet → rezervasyon kapatılır, kullanım ve çağrı kaydı yazılır
7. Eşik uyarıları (ör. %80) ve bütçe bitti uyarıları; her dönem için bir kez, denetim kaydına da yazılır

Herhangi bir adımda çağrı yapılmazsa sonuç None döner ve Layer 1 kararı geçerli olur.
"""
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from PIL import Image

from app.config import settings
from app.moderation import ai
from app.platform_settings import PlatformSettings

log = logging.getLogger("ai_gateway")

PANEL_PROJECT = UUID(int=0)          # panelden yapılan test çağrıları
FAILS_TO_OPEN = 5                    # bu kadar ardışık hata → devre açılır
OPEN_SECONDS = 60
AUTH_OPEN_SECONDS = 300
FAILURE_STATUSES = {"error", "timeout", "rate_limited", "bad_response"}
_LOCK_KEY = 815003
_MICRO = Decimal("0.000001")


@dataclass
class GatewayResult:
    scores: dict[str, float] | None
    status: str
    model: str | None = None
    cost_usd: Decimal = Decimal(0)
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    evaluated: frozenset = frozenset()


def cost_of(input_tokens: int, output_tokens: int, price: tuple[Decimal, Decimal]) -> Decimal:
    value = (Decimal(input_tokens) * price[0] + Decimal(output_tokens) * price[1]) / Decimal(1_000_000)
    return value.quantize(_MICRO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- Redis tabanlı yumuşak kontroller

async def circuit_state(redis) -> str | None:
    try:
        value = await redis.get("ai:cb:open")
    except Exception:  # noqa: BLE001 — Redis yoksa devre kesici devre dışı
        return None
    return value.decode() if isinstance(value, bytes) else value


async def reset_circuit(redis) -> None:
    await redis.delete("ai:cb:open", "ai:cb:fails")


async def _update_circuit(redis, status: str) -> None:
    try:
        if status == "ok":
            await redis.delete("ai:cb:fails")
        elif status == "auth_error":
            await redis.set("ai:cb:open", "auth_error", ex=AUTH_OPEN_SECONDS)
        elif status in FAILURE_STATUSES:
            fails = await redis.incr("ai:cb:fails")
            await redis.expire("ai:cb:fails", OPEN_SECONDS)
            if fails >= FAILS_TO_OPEN:
                await redis.set("ai:cb:open", "failures", ex=OPEN_SECONDS)
                log.warning("AI circuit opened after %d consecutive failures", fails)
    except Exception:  # noqa: BLE001
        pass


async def _within_minute_cap(redis, cap: int, calls: int) -> bool:
    if cap <= 0:
        return True
    try:
        key = f"ai:calls:{int(time.time() // 60)}"
        n = await redis.incrby(key, calls)
        if n == calls:
            await redis.expire(key, 70)
        return n <= cap
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- uyarılar

async def raise_alert(conn, kind: str, period: str, message: str) -> None:
    new_id = await conn.fetchval(
        "INSERT INTO ai_alerts (kind, period, message) VALUES ($1, $2, $3) ON CONFLICT (kind, period) DO NOTHING RETURNING id",
        kind, period, message,
    )
    if new_id:
        await conn.execute(
            """
            INSERT INTO audit_logs (actor_email, action, target_type, details)
            VALUES ('sistem', 'ai.alert', 'ai', jsonb_build_object('kind', $1::text, 'message', $2::text))
            """,
            kind, message,
        )
        log.warning("AI alert: %s", message)


async def _check_thresholds(conn, s: PlatformSettings) -> None:
    row = await conn.fetchrow(
        """
        WITH d AS (SELECT (now() AT TIME ZONE $1)::date AS today)
        SELECT d.today,
               coalesce(sum(u.cost_usd) FILTER (WHERE u.day = d.today), 0) AS today_cost,
               coalesce(sum(u.cost_usd), 0) AS month_cost
        FROM d LEFT JOIN ai_usage_daily u ON u.day >= date_trunc('month', d.today)::date
        GROUP BY d.today
        """,
        settings.panel_timezone,
    )
    today, month = str(row["today"]), str(row["today"])[:7]
    for label, spent, budget, period in (("daily", row["today_cost"], s.ai_daily_budget_usd, today),
                                         ("monthly", row["month_cost"], s.ai_monthly_budget_usd, month)):
        if budget <= 0:
            continue
        pct = spent / budget * 100
        name = "Günlük" if label == "daily" else "Aylık"
        if pct >= 100:
            await raise_alert(conn, f"{label}_exhausted", period,
                              f"{name} AI bütçesi doldu (${spent:.4f} / ${budget}). Dönem sonuna kadar yerel modellerle devam ediliyor.")
        elif pct >= s.ai_alert_percent:
            await raise_alert(conn, f"{label}_threshold", period,
                              f"{name} AI bütçesinin %{int(pct)}'i kullanıldı (${spent:.4f} / ${budget}).")


# ---------------------------------------------------------------- kayıt

async def _record_skip(pool, project_id: UUID, model: str) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO ai_usage_daily (day, project_id, model, skipped)
            VALUES ((now() AT TIME ZONE $1)::date, $2, $3, 1)
            ON CONFLICT (day, project_id, model) DO UPDATE SET skipped = ai_usage_daily.skipped + 1
            """,
            settings.panel_timezone, project_id, model,
        )
    except Exception:  # noqa: BLE001 — kayıt hatası kararı engellemesin
        log.exception("failed to record AI skip")


async def _reserve(pool, s: PlatformSettings, project_id: UUID, model: str, amount: Decimal) -> tuple[str | None, object]:
    """Bütçe uygunsa tutarı ayırır. (engel nedeni veya None, rezervasyon günü) döner."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY)
        row = await conn.fetchrow(
            """
            WITH d AS (SELECT (now() AT TIME ZONE $1)::date AS today)
            SELECT d.today,
                   coalesce(sum(u.cost_usd + u.reserved_usd) FILTER (WHERE u.day = d.today), 0) AS today_total,
                   coalesce(sum(u.cost_usd + u.reserved_usd), 0) AS month_total,
                   coalesce(sum(u.cost_usd + u.reserved_usd) FILTER (WHERE u.project_id = $2), 0) AS project_total,
                   (SELECT ai_monthly_budget_usd FROM projects WHERE id = $2) AS project_budget
            FROM d LEFT JOIN ai_usage_daily u ON u.day >= date_trunc('month', d.today)::date
            GROUP BY d.today
            """,
            settings.panel_timezone, project_id,
        )
        today = row["today"]
        reason = None
        if row["today_total"] + amount > s.ai_daily_budget_usd:
            reason = "budget_daily"
        elif row["month_total"] + amount > s.ai_monthly_budget_usd:
            reason = "budget_monthly"
        elif row["project_budget"] is not None and row["project_total"] + amount > row["project_budget"]:
            reason = "budget_project"
        if reason:
            if reason != "budget_project":
                label = "daily" if reason == "budget_daily" else "monthly"
                name = "Günlük" if label == "daily" else "Aylık"
                period = str(today) if label == "daily" else str(today)[:7]
                await raise_alert(conn, f"{label}_exhausted", period,
                                  f"{name} AI bütçesi doldu. Dönem sonuna kadar yerel modellerle devam ediliyor.")
            return reason, today
        await conn.execute(
            """
            INSERT INTO ai_usage_daily (day, project_id, model, reserved_usd) VALUES ($1, $2, $3, $4)
            ON CONFLICT (day, project_id, model) DO UPDATE SET reserved_usd = ai_usage_daily.reserved_usd + EXCLUDED.reserved_usd
            """,
            today, project_id, model, amount,
        )
        return None, today


async def _settle(pool, s: PlatformSettings, *, day, project_id: UUID, request_id: UUID | None, model: str, kind: str,
                  reserved: Decimal, result: ai.CallResult, cost: Decimal) -> None:
    ok = result.status == "ok"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO ai_usage_daily (day, project_id, model, calls, errors, input_tokens, output_tokens,
                                        cost_usd, latency_ms_total)
            VALUES (coalesce($1, (now() AT TIME ZONE $11)::date), $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (day, project_id, model) DO UPDATE SET
                calls = ai_usage_daily.calls + EXCLUDED.calls,
                errors = ai_usage_daily.errors + EXCLUDED.errors,
                input_tokens = ai_usage_daily.input_tokens + EXCLUDED.input_tokens,
                output_tokens = ai_usage_daily.output_tokens + EXCLUDED.output_tokens,
                cost_usd = ai_usage_daily.cost_usd + EXCLUDED.cost_usd,
                latency_ms_total = ai_usage_daily.latency_ms_total + EXCLUDED.latency_ms_total,
                reserved_usd = greatest(ai_usage_daily.reserved_usd - $10, 0)
            """,
            day, project_id, model, result.http_calls, 0 if ok else 1, result.input_tokens, result.output_tokens,
            cost, result.latency_ms, reserved, settings.panel_timezone,
        )
        await conn.execute(
            """
            INSERT INTO ai_calls (project_id, request_id, model, kind, status, input_tokens, output_tokens,
                                  cost_usd, latency_ms, error)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            project_id, request_id, model, kind, result.status, result.input_tokens, result.output_tokens,
            cost, result.latency_ms, (result.error or "")[:300] or None,
        )
        if result.status == "auth_error":
            await raise_alert(conn, "auth_error", time.strftime("%Y-%m-%d-%H"),
                              "OpenAI API anahtarı reddedildi (401/403). Anahtarı kontrol edin; AI 5 dakika duraklatıldı.")
        if cost > 0:
            await _check_thresholds(conn, s)


# ---------------------------------------------------------------- giriş noktası

async def classify(*, pool, redis, s: PlatformSettings, project_id: UUID, request_id: UUID | None,
                   text: str | None, images: list[Image.Image], task: str = "moderate",
                   visual_rules: list[dict] | None = None) -> GatewayResult:
    """task="moderate": genel sınıflandırma. task="visual": yasaklı sembol/görsel kontrolü (her zaman sohbet
    modeliyle; OpenAI Moderation özel kurallara bakamaz). İkisi de aynı bütçe/limit/devre kesiciden geçer."""
    if not s.ai_enabled:
        return GatewayResult(None, "disabled")
    if not ai.configured():
        return GatewayResult(None, "not_configured")
    if not (text or images):
        return GatewayResult(None, "no_input")

    visual = task == "visual"
    if visual and not (images and visual_rules):
        return GatewayResult(None, "no_input")
    free = s.ai_provider == "openai_moderation" and not visual
    model = s.ai_vision_model if visual else ai.model_for(s.ai_provider, s.ai_model, s.ai_vision_model, bool(images))
    kind = "image" if images else "text"

    if await circuit_state(redis):
        await _record_skip(pool, project_id, model)
        return GatewayResult(None, "circuit_open", model)

    planned_calls = (int(bool(text)) + min(len(images), 4)) if free else 1
    if not await _within_minute_cap(redis, s.ai_max_calls_per_minute, planned_calls):
        await _record_skip(pool, project_id, model)
        return GatewayResult(None, "cap_minute", model)

    price: tuple[Decimal, Decimal] | None = (Decimal(0), Decimal(0))
    if not free:
        row = await pool.fetchrow("SELECT input_per_1m, output_per_1m FROM ai_model_prices WHERE model = $1", model)
        if row is None:
            async with pool.acquire() as conn:
                await raise_alert(conn, "no_price", model,
                                  f"'{model}' modelinin fiyatı tanımlı değil; bu modelle AI çağrısı yapılmıyor. AI ve maliyet sayfasından fiyat ekleyin.")
            await _record_skip(pool, project_id, model)
            return GatewayResult(None, "no_price", model)
        price = (row["input_per_1m"], row["output_per_1m"])

    est_in, est_out = ai.estimate_tokens(text, min(len(images), 4))
    reserved = Decimal(0) if free else cost_of(est_in, est_out, price)
    day = None
    if reserved > 0:
        reason, day = await _reserve(pool, s, project_id, model, reserved)
        if reason:
            await _record_skip(pool, project_id, model)
            return GatewayResult(None, reason, model)

    try:
        if visual:
            result = await ai.call_chat(model, None, images, system_prompt=ai.visual_prompt(visual_rules),
                                        parse=ai.parse_matches)
        elif free:
            result = await ai.call_moderation(text, images)
        else:
            result = await ai.call_chat(model, text, images)
    except Exception as exc:  # noqa: BLE001 — sağlayıcı katmanında beklenmeyen hata: rezervasyonu mutlaka kapat
        result = ai.CallResult(None, "error", http_calls=1, error=type(exc).__name__)

    # Maliyet: sağlayıcı token bildirdiyse gerçek değer; zaman aşımında istek işlenmiş olabilir → tahmin;
    # HTTP hata yanıtları ücretlendirilmez.
    if free:
        cost = Decimal(0)
    elif result.usage_reported:
        cost = cost_of(result.input_tokens, result.output_tokens, price)
    elif result.status in ("ok", "timeout", "bad_response"):
        cost = reserved
    else:
        cost = Decimal(0)

    try:
        await _settle(pool, s, day=day, project_id=project_id, request_id=request_id, model=model, kind=kind,
                      reserved=reserved, result=result, cost=cost)
    except Exception:  # noqa: BLE001 — muhasebe hatası kararı engellemesin ama loglansın
        log.exception("failed to settle AI usage")
    await _update_circuit(redis, result.status)
    return GatewayResult(result.scores if result.status == "ok" else None, result.status, model, cost,
                         result.latency_ms, result.input_tokens, result.output_tokens, result.evaluated)
