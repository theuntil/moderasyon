"""Admin paneli API'si.

Bu uygulama dışarıya açılmaz: sadece compose ağı içinde, panel (nginx) üzerinden erişilir.
Public moderasyon API'si (app.api.main) ile aynı veritabanını kullanır ama ayrı bir process'tir.
"""
import logging
import re
from contextlib import asynccontextmanager

from arq import create_pool as create_arq_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from redis.asyncio import Redis

from app.admin.auth import USERNAME_PATTERN, hash_password, validate_password
from app.admin.routes import ai as ai_routes, auth, moderation, platform, projects, rules, stats
from app.config import settings
from app.logsetup import setup as _setup_logging

_setup_logging()
from app.db import create_pool
from app.autoban import AutoBanMiddleware
from app.realip import CloudflareRealIPMiddleware
from app.http_common import BodyLimitMiddleware, install_common
from app.platform_settings import SettingsCache
from app.storage import storage


log = logging.getLogger("admin")


def _env_value(name: str, value: str | None) -> str:
    raw = value or ""
    clean = raw.strip()
    if clean != raw:
        log.warning("%s başında/sonunda boşluk vardı; temizlendi.", name)
    return clean


async def _audit_system(conn, action: str, target_id: str | None, details: dict) -> None:
    import json as _json
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_email, action, target_type, target_id, details)
        VALUES ('sistem', $1, 'admin_user', $2, $3)
        """,
        action, target_id, _json.loads(_json.dumps(details)),
    )


async def bootstrap_owner(pool) -> None:
    """Panel yöneticisi kurulumu (admin-api her açıldığında çalışır, idempotent).

    - Hiç yönetici yoksa ADMIN_EMAIL / ADMIN_PASSWORD ile owner oluşturur.
    - Eski kurulumda kullanıcı adıyla açılmış hesabın e-postası boşsa ADMIN_EMAIL'e bağlar.
    - ADMIN_RESET_PASSWORD=true ise ADMIN_EMAIL hesabının şifresini ADMIN_PASSWORD'e sıfırlar,
      kilidini açar ve hesabı etkinleştirir (aynı env şifresiyle yalnızca bir kez).
    - Sonunda yöneticilerin durumunu loga yazar (Dokploy loglarından teşhis için).
    """
    import hashlib

    email = _env_value("ADMIN_EMAIL", settings.admin_email).lower()
    legacy_username = _env_value("ADMIN_USERNAME", settings.admin_username)
    password = _env_value("ADMIN_PASSWORD", settings.admin_password)
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        log.error("ADMIN_EMAIL geçersiz: %s", email)
        email = ""
    login = email or legacy_username
    fp = hashlib.sha256((settings.api_key_pepper + ":" + login + ":" + password).encode()).hexdigest() if password else None

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(815002)")
        try:
            existing = await conn.fetchval("SELECT count(*) FROM admin_users")

            # 1) Hiç yönetici yok → owner oluştur
            if not existing and login and password:
                if len(password) < 8:
                    log.error("ADMIN_PASSWORD çok kısa (en az 8 karakter). İlk yönetici oluşturulmadı.")
                elif not email and not re.match(USERNAME_PATTERN, legacy_username):
                    log.error("ADMIN_EMAIL tanımlayın (ör. sen@alanadi.com).")
                else:
                    try:
                        validate_password(password, login)
                        must_change = False
                    except Exception:
                        must_change = True
                    await conn.execute(
                        """
                        INSERT INTO admin_users (username, email, name, role, password_hash, must_change_password, env_reset_fp)
                        VALUES ($1, $2, $3, 'owner', $4, $5, $6)
                        """,
                        email or legacy_username, email or None, (email.split("@")[0] if email else legacy_username),
                        hash_password(password), must_change, fp,
                    )
                    await _audit_system(conn, "admin.bootstrapped", None, {"login": login})
                    log.warning("İlk yönetici oluşturuldu: %s%s", login,
                                " (şifre kurallara uymadığı için ilk girişte yenisi istenecek)" if must_change else "")

            # 2) Eski hesaba e-posta bağla
            elif existing and email and not await conn.fetchval("SELECT 1 FROM admin_users WHERE lower(email) = $1", email):
                target = await conn.fetchrow(
                    """
                    SELECT id, username FROM admin_users
                    WHERE email IS NULL AND (lower(username) = lower($1)
                          OR (role = 'owner' AND status = 'active'
                              AND (SELECT count(*) FROM admin_users WHERE role = 'owner' AND status = 'active') = 1))
                    ORDER BY (lower(username) = lower($1)) DESC LIMIT 1
                    """,
                    legacy_username,
                )
                if target:
                    await conn.execute("UPDATE admin_users SET email = $2 WHERE id = $1", target["id"], email)
                    await _audit_system(conn, "admin.email_linked", str(target["id"]), {"email": email})
                    log.warning("Yönetici %s hesabına e-posta bağlandı: %s", target["username"], email)
                elif not settings.admin_reset_password:
                    log.error(
                        "ADMIN_EMAIL (%s) hiçbir yönetici hesabına bağlanamadı; bu e-postayla giriş yapılamaz. "
                        "Çözüm: env'e ADMIN_RESET_PASSWORD=true ekleyip yeniden başlatın (bu e-postayla owner "
                        "oluşturulur/şifresi sıfırlanır) veya api terminalinde: python -m app.cli admin-status", email)

            # 3) Env ile şifre sıfırlama / kurtarma
            if settings.admin_reset_password and login and password:
                if len(password) < 8:
                    log.error("ADMIN_RESET_PASSWORD: ADMIN_PASSWORD en az 8 karakter olmalı; sıfırlama yapılmadı.")
                else:
                    row = await conn.fetchrow(
                        "SELECT id, env_reset_fp FROM admin_users WHERE lower(email) = $1 OR lower(username) = $1 LIMIT 1",
                        login.lower(),
                    )
                    try:
                        validate_password(password, login)
                        must_change = False
                    except Exception:
                        must_change = True
                    if row is None:
                        admin_id = await conn.fetchval(
                            """
                            INSERT INTO admin_users (username, email, name, role, password_hash, must_change_password, env_reset_fp)
                            VALUES ($1, $2, $3, 'owner', $4, $5, $6) RETURNING id
                            """,
                            login, email or None, login.split("@")[0], hash_password(password), must_change, fp,
                        )
                        await _audit_system(conn, "admin.env_reset", str(admin_id), {"login": login, "created": True})
                        log.warning("ADMIN_RESET_PASSWORD: %s için owner hesabı oluşturuldu.", login)
                    elif row["env_reset_fp"] != fp:
                        await conn.execute(
                            """
                            UPDATE admin_users SET password_hash = $2, must_change_password = $3, failed_logins = 0,
                                   locked_until = NULL, status = 'active', password_changed_at = now(), env_reset_fp = $4
                            WHERE id = $1
                            """,
                            row["id"], hash_password(password), must_change, fp,
                        )
                        await conn.execute("UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", row["id"])
                        await _audit_system(conn, "admin.env_reset", str(row["id"]), {"login": login})
                        log.warning("ADMIN_RESET_PASSWORD: %s şifresi env'deki değere sıfırlandı, kilidi açıldı. "
                                    "Giriş yaptıktan sonra ADMIN_RESET_PASSWORD satırını env'den kaldırın.", login)
                    else:
                        log.info("ADMIN_RESET_PASSWORD: bu env şifresi daha önce uygulandı; tekrar sıfırlanmadı.")

            # 4) Teşhis için durum raporu
            rows = await conn.fetch(
                """
                SELECT coalesce(email, username) AS login, role, status, must_change_password,
                       locked_until > now() AS locked, failed_logins, totp_enabled
                FROM admin_users ORDER BY created_at LIMIT 20
                """
            )
            if not rows:
                log.error("Hiç panel yöneticisi yok. Env'e ADMIN_EMAIL ve ADMIN_PASSWORD ekleyip yeniden başlatın.")
            for r in rows:
                flags = [r["role"], r["status"]]
                if r["locked"]:
                    flags.append("KİLİTLİ (15 dk)")
                if r["failed_logins"]:
                    flags.append(f"{r['failed_logins']} hatalı deneme")
                if r["must_change_password"]:
                    flags.append("ilk girişte şifre değişecek")
                if r["totp_enabled"]:
                    flags.append("2FA açık")
                log.warning("Panel yöneticisi: %s [%s]", r["login"], ", ".join(flags))
        finally:
            await conn.execute("SELECT pg_advisory_unlock(815002)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await create_pool(max_size=10, statement_timeout_ms=20_000)
    await bootstrap_owner(app.state.db)
    app.state.redis = Redis.from_url(settings.redis_url)
    app.state.arq = await create_arq_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.settings_cache = SettingsCache()
    await storage.start()
    yield
    await storage.close()
    await app.state.arq.aclose()
    await app.state.redis.aclose()
    await app.state.db.close()


app = FastAPI(
    title="Moderation Platform Admin API",
    version="0.2.0",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,  # admin API'nin şeması dışarıya gösterilmez
)

for module in (auth, stats, projects, moderation, platform, ai_routes, rules):
    app.include_router(module.router)


install_common(app)
app.add_middleware(BodyLimitMiddleware, default_limit=64 * 1024)
app.add_middleware(AutoBanMiddleware, get_state=lambda: (app.state.redis, app.state.db, app.state.settings_cache))


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# En dış katman (en son eklenen): Cloudflare arkasında gerçek istemci IP'si. IP banı, hız sınırı ve
# otomatik koruma bu adresi kullanır.
app.add_middleware(CloudflareRealIPMiddleware, enabled=settings.trust_cloudflare)
