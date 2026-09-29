"""Public API kimlik doğrulama ve giriş kontrolleri.

Dayanıklılık: veritabanı geçici olarak erişilemezse son 10 dakika içinde doğrulanmış API anahtarları
bellek içi önbellekten tanınır ve istek "degraded" (kısıtlı) modda işlenir. Tanınamayan anahtarla
gelen istek 503 + {"status": "unprocessed", "retryable": true} alır; istemci her durumda JSON yanıt görür.
"""
import ipaddress
import time
from dataclasses import dataclass, field
from uuid import UUID

import asyncpg
from fastapi import HTTPException, Request, status

from app.config import settings
from app.ratelimit import check_rate_limit
from app.security import hash_api_key, looks_like_api_key

DB_ERRORS = (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, asyncpg.CannotConnectNowError,
             asyncpg.QueryCanceledError, asyncpg.TooManyConnectionsError, OSError, TimeoutError, ConnectionError)

# Normalde her istekte veritabanına bakılır (iptal edilen anahtar/durdurulan proje ANINDA etkili olur).
# Önbellek sadece veritabanı erişilemezken yedek olarak kullanılır.
AUTH_STALE_S = 600         # veritabanı yokken en fazla bu kadar eski bilgiyle devam edilir
_auth_cache: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True)
class AuthContext:
    project_id: UUID
    api_key_id: UUID
    environment: str
    client_ip: str | None
    policy: dict = field(default_factory=dict)
    policy_revision: int = 0
    degraded: bool = False       # veritabanı erişilemiyor; sonuç kalıcı olarak kaydedilemeyebilir


def _error(code: int, error: str, message: str | None = None, headers: dict | None = None, **extra) -> HTTPException:
    detail = {"error": error}
    if message:
        detail["message"] = message
    detail |= extra
    return HTTPException(status_code=code, detail=detail, headers=headers)


def unprocessed(error: str, message: str, fallback_decision: str | None = None, retry_after: int = 30) -> HTTPException:
    """İçerik işlenemedi: istemci bunu 'işlenmedi' olarak kaydedip sonra tekrar deneyebilir."""
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, error, message, headers={"Retry-After": str(retry_after)},
                  status="unprocessed", decision=None, fallback_decision=fallback_decision, retryable=True)


def _unauthorized(error: str) -> HTTPException:
    return _error(status.HTTP_401_UNAUTHORIZED, error, headers={"WWW-Authenticate": "Bearer"})


def client_ip(request: Request) -> str | None:
    # Uvicorn --proxy-headers ile Traefik'in X-Forwarded-For başlığından gerçek IP'yi çözer
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


async def _lookup_key(pool, key_hash: str) -> tuple[dict | None, bool]:
    """(anahtar bilgisi, degraded) döner. Veritabanı yoksa önbellekten."""
    cached = _auth_cache.get(key_hash)
    now = time.monotonic()
    try:
        row = await pool.fetchrow(
            """
            SELECT k.id, k.project_id, k.environment, p.status AS project_status,
                   p.rate_limit_per_second, p.rate_limit_per_minute, p.policy, p.policy_revision
            FROM project_api_keys k
            JOIN projects p ON p.id = k.project_id
            WHERE k.key_hash = $1
              AND k.status = 'active'
              AND (k.expires_at IS NULL OR k.expires_at > now())
            """,
            key_hash,
        )
    except DB_ERRORS:
        if cached and now - cached[0] < AUTH_STALE_S:
            return cached[1], True
        raise unprocessed("service_unavailable", "The moderation service is temporarily unavailable. Retry later.")
    if row is None:
        _auth_cache.pop(key_hash, None)   # iptal edilen anahtar en geç 30 sn içinde düşer
        return None, False
    data = dict(row)
    if len(_auth_cache) > 10_000:
        _auth_cache.clear()
    _auth_cache[key_hash] = (now, data)
    return data, False


async def require_api_key(request: Request) -> AuthContext:
    pool = request.app.state.db
    try:
        platform = await request.app.state.settings_cache.get(pool)
    except Exception:  # noqa: BLE001 — ayar da yoksa hiçbir şey doğrulanamaz
        raise unprocessed("service_unavailable", "The moderation service is temporarily unavailable. Retry later.")

    # 1) Hizmet panelden kapatıldıysa hiçbir istek işlenmez
    if not platform.service_enabled:
        raise unprocessed("service_unavailable",
                          platform.maintenance_message or "The moderation service is temporarily unavailable.", retry_after=60)

    # 2) Bu IP çok fazla geçersiz anahtar denediyse veritabanına hiç gitme
    ip = client_ip(request)
    redis = request.app.state.arq
    fail_key = f"authfail:{ip or 'unknown'}:{int(time.time() // 60)}"
    try:
        fails = int(await redis.get(fail_key) or 0)
    except Exception:  # noqa: BLE001 — Redis yoksa bu korumayı atla
        fails = 0
    if fails >= settings.auth_fail_limit_per_minute:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_auth_failures",
                     "Too many invalid API key attempts from this IP.", headers={"Retry-After": "60"})

    async def auth_failed(code: str) -> HTTPException:
        from app import autoban
        await autoban.record(redis, pool, platform, ip, "auth_fail")
        try:
            pipe = redis.pipeline(transaction=False)
            pipe.incr(fail_key)
            pipe.expire(fail_key, 70)
            await pipe.execute()
        except Exception:  # noqa: BLE001
            pass
        return _unauthorized(code)

    # 3) API key
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise await auth_failed("missing_api_key")
    token = token.strip()
    if not looks_like_api_key(token):
        raise await auth_failed("invalid_api_key")

    row, degraded = await _lookup_key(pool, hash_api_key(token))
    if row is None:
        raise await auth_failed("invalid_api_key")

    # 4) Project panelden durdurulduysa
    if row["project_status"] != "active":
        raise _error(status.HTTP_403_FORBIDDEN, "project_disabled", "This project is disabled.")

    # 5) IP kuralları (platform geneli + bu project'e özel). Veritabanı yoksa atlanır (degraded).
    if ip and not degraded:
        try:
            blocked = await pool.fetchval(
                """
                SELECT 1 FROM ip_rules
                WHERE $1::inet <<= cidr
                  AND (project_id IS NULL OR project_id = $2)
                  AND (expires_at IS NULL OR expires_at > now())
                LIMIT 1
                """,
                ip, row["project_id"],
            )
        except DB_ERRORS:
            blocked, degraded = None, True
        if blocked:
            raise _error(status.HTTP_403_FORBIDDEN, "ip_blocked", "Requests from this IP address are blocked.")

    # 6) Rate limit (Redis yoksa açık kalır)
    rl = await check_rate_limit(
        request.app.state.arq,
        row["project_id"],
        row["rate_limit_per_second"] or platform.default_rate_limit_per_second,
        row["rate_limit_per_minute"] or platform.default_rate_limit_per_minute,
    )
    request.state.rate_limit = rl
    if not rl.allowed:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limit_exceeded",
            headers={"Retry-After": str(rl.retry_after_s), "X-RateLimit-Limit": str(rl.limit)},
            retryable=True,
        )

    if not degraded:
        # last_used_at'i her istekte değil, dakikada en fazla bir kez yaz
        try:
            await pool.execute(
                """
                UPDATE project_api_keys SET last_used_at = now()
                WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')
                """,
                row["id"],
            )
        except DB_ERRORS:
            degraded = True
    return AuthContext(row["project_id"], row["id"], row["environment"], ip,
                       row["policy"] or {}, row["policy_revision"] or 0, degraded)
