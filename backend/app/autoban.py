"""Akıllı IP koruması (otomatik ban).

Kurallar (eşikler panelden ayarlanır, pencereler sabittir):
- auth_fail:   5 dk içinde N geçersiz/eksik API anahtarı          → anahtar tahmini, kimlik bilgisi denemesi
- scan:        5 dk içinde N adet 404/405 (olmayan adres, yanlış metot) → açık arayan tarayıcı bot
- flood:       1 dk içinde N istek (anahtar geçerli olsa bile)      → olağandışı trafik seli
- panel_login: 15 dk içinde N hatalı panel girişi                   → yönetici hesabına kaba kuvvet

Akıllı davranışlar:
- Kademeli süre: aynı IP son 30 günde tekrar banlandıysa süre uzar (15 dk → 1 sa → 1 gün → 7 gün).
- Asla banlanmayanlar: loopback/iç ağ adresleri ve panelden tanımlanan izin listesi (ör. kendi sunucularınız).
- İzleme modu: kurallar çalışır ve olaylar kaydedilir ama kimse banlanmaz (eşikleri güvenle denemek için).
- Hızlı ret: banlı IP'ler Redis'te tutulur; istek veritabanına hiç gitmeden 403 alır.
- Ban, IP engelleri listesine "Otomatik" olarak eklenir; panelden tek tıkla kaldırılabilir.
"""
import ipaddress
import json
import logging
import time

log = logging.getLogger("autoban")

RULE_WINDOWS = {"auth_fail": 300, "scan": 300, "flood": 60, "panel_login": 900}
RULE_LABELS = {
    "auth_fail": "geçersiz API anahtarı denemesi",
    "scan": "olmayan adreslere tarama",
    "flood": "olağandışı istek hacmi",
    "panel_login": "hatalı panel girişi",
}
DURATIONS_MIN = [15, 60, 24 * 60, 7 * 24 * 60]
SCAN_STATUSES = {404, 405}


def ban_key(ip: str) -> str:
    return f"ban:{ip}"


def _limit(s, rule: str) -> int:
    return {"auth_fail": s.autoban_auth_fail_limit, "scan": s.autoban_scan_limit,
            "flood": s.autoban_flood_limit, "panel_login": s.autoban_panel_login_limit}[rule]


def exempt(ip: str, s) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    for cidr in s.autoban_allowlist:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


async def is_banned(redis, ip: str | None) -> bool:
    if not ip:
        return False
    try:
        return bool(await redis.exists(ban_key(ip)))
    except Exception:  # noqa: BLE001 — Redis yoksa veritabanındaki IP kuralı kontrolü yine çalışır
        return False


async def record(redis, pool, s, ip: str | None, rule: str) -> None:
    """Bir olayı say; eşik tam aşıldığında (bir kez) ban uygula."""
    if not ip or s.autoban_mode == "off":
        return
    window = RULE_WINDOWS[rule]
    key = f"ab:{rule}:{ip}:{int(time.time() // window)}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window + 5)
    except Exception:  # noqa: BLE001
        return
    if count < _limit(s, rule):
        return
    # Eşik geçildiği sürece tetiklenir: ban süresi dolan veya elle kaldırılan IP saldırıya devam ederse
    # aynı pencerede yeniden (daha uzun süreyle) banlanır. Banlı IP'ler dış katmanda reddedildiği için sayılmaz.
    # İzleme/izin listesi olayları ise pencere başına bir kez kaydedilir.
    noted = f"ab:noted:{rule}:{ip}:{int(time.time() // window)}"
    if exempt(ip, s) or s.autoban_mode == "monitor":
        try:
            if not await redis.set(noted, 1, ex=window + 5, nx=True):
                return
        except Exception:  # noqa: BLE001
            return
    await _trigger(redis, pool, s, ip, rule, count)


async def _trigger(redis, pool, s, ip: str, rule: str, count: int) -> None:
    if exempt(ip, s):
        await _event(pool, ip, rule, count, "skipped_allowlist", None)
        return
    if s.autoban_mode == "monitor":
        await _event(pool, ip, rule, count, "monitored", None)
        return
    try:
        async with pool.acquire() as conn, conn.transaction():
            # Aynı IP için etkin bir kural varsa tekrar ekleme
            if await conn.fetchval(
                "SELECT 1 FROM ip_rules WHERE cidr >>= $1::inet AND project_id IS NULL AND (expires_at IS NULL OR expires_at > now())",
                ip,
            ):
                return
            previous = await conn.fetchval(
                "SELECT count(*) FROM autoban_events WHERE ip = $1::inet AND action = 'banned' AND created_at > now() - interval '30 days'",
                ip,
            )
            minutes = DURATIONS_MIN[min(previous, len(DURATIONS_MIN) - 1)]
            window_min = RULE_WINDOWS[rule] // 60
            reason = f"Otomatik: {window_min} dk içinde {count} {RULE_LABELS[rule]}"
            if previous:
                reason += f" ({previous + 1}. kez)"
            net = ipaddress.ip_network(ip)
            await conn.execute(
                """
                INSERT INTO ip_rules (project_id, cidr, reason, source, expires_at)
                VALUES (NULL, $1::cidr, $2, 'auto', now() + make_interval(mins => $3))
                """,
                str(net), reason, minutes,
            )
            await conn.execute(
                "INSERT INTO autoban_events (ip, rule, count, action, minutes) VALUES ($1::inet, $2, $3, 'banned', $4)",
                ip, rule, count, minutes,
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (actor_email, action, target_type, target_id, details, ip)
                VALUES ('sistem', 'ip.auto_banned', 'ip_rule', $1, $2, $3::inet)
                """,
                ip, json.loads(json.dumps({"rule": rule, "count": count, "minutes": minutes})), ip,
            )
        await redis.set(ban_key(ip), rule, ex=minutes * 60)
        log.warning("auto-banned %s for %d min (%s, %d)", ip, minutes, rule, count)
    except Exception:  # noqa: BLE001 — koruma hatası isteği etkilemesin
        log.exception("auto-ban failed for %s", ip)


async def _event(pool, ip: str, rule: str, count: int, action: str, minutes: int | None) -> None:
    try:
        await pool.execute(
            "INSERT INTO autoban_events (ip, rule, count, action, minutes) VALUES ($1::inet, $2, $3, $4, $5)",
            ip, rule, count, action, minutes,
        )
    except Exception:  # noqa: BLE001
        pass


class AutoBanMiddleware:
    """Her istekte: banlıysa hemen 403; değilse yanıt durumuna göre sayaçları artır.

    Kimlik doğrulama hataları deps.py'de (auth_fail) sayılır; burada hacim (flood) ve tarama (scan).
    """

    def __init__(self, app, get_state):
        self.app = app
        self.get_state = get_state      # () -> (redis, pool, settings_cache)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in ("/health", "/ready"):
            return await self.app(scope, receive, send)
        client = scope.get("client")
        ip = client[0] if client else None
        redis, pool, cache = self.get_state()
        if await is_banned(redis, ip):
            from app.http_common import _send_json
            detail = {"error": "ip_blocked", "message": "Requests from this IP address are blocked."}
            return await _send_json(send, 403, {"detail": detail, **detail})

        status_code = 0

        async def tracking_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, tracking_send)
        try:
            s = await cache.get(pool)
        except Exception:  # noqa: BLE001
            return
        if s.autoban_mode == "off" or not ip:
            return
        await record(redis, pool, s, ip, "flood")
        if status_code in SCAN_STATUSES:
            await record(redis, pool, s, ip, "scan")
