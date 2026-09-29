from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.admin.auth import (
    CSRF_HEADER, CSRF_VALUE, AdminUser, audit, check_totp, clear_session_cookie, create_session, current_user,
    decrypt_secret, encrypt_secret, hash_password, hash_recovery_code, new_recovery_codes, new_totp_secret,
    request_ip, revoke_session, totp_uri, validate_password, verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("admin.auth")

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
IP_ATTEMPTS_PER_15_MIN = 20


class LoginBody(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    username: str | None = Field(default=None, max_length=254)   # eski istemciler için
    password: str = Field(max_length=256)


class MfaBody(BaseModel):
    mfa_token: str = Field(max_length=128)
    code: str = Field(max_length=32)


class CodeBody(BaseModel):
    code: str = Field(max_length=32)


class DisableMfaBody(BaseModel):
    password: str = Field(max_length=256)
    code: str = Field(max_length=32)


MFA_TTL_MINUTES = 5
MFA_MAX_ATTEMPTS = 5


class ChangePasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def _user_payload(u: AdminUser) -> dict:
    return {
        "id": str(u.id), "username": u.username, "email": u.email, "name": u.name, "role": u.role,
        "must_change_password": u.must_change_password,
    }


_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "invalid_credentials", "message": "E-posta veya şifre hatalı."},
)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "csrf_check_failed"})

    pool = request.app.state.db
    redis = request.app.state.redis
    ip = request_ip(request) or "unknown"

    # IP başına deneme sınırı (kaba kuvvet saldırılarına karşı)
    ip_key = f"admin:login:ip:{ip}"
    attempts = await redis.incr(ip_key)
    if attempts == 1:
        await redis.expire(ip_key, 15 * 60)
    if attempts > IP_ATTEMPTS_PER_15_MIN:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "too_many_attempts", "message": "Çok fazla giriş denemesi. 15 dakika sonra tekrar deneyin."},
        )

    login_name = (body.email or body.username or "").strip().lower()
    if not login_name:
        raise _INVALID
    # Kullanıcı adıyla ya da (tanımlıysa) e-postayla giriş yapılabilir
    user = await pool.fetchrow(
        """
        SELECT id, username, email, name, role, status, password_hash, must_change_password, locked_until,
               locked_until > now() AS is_locked, totp_enabled
        FROM admin_users WHERE lower(username) = $1 OR lower(email) = $1
        ORDER BY (lower(username) = $1) DESC
        LIMIT 1
        """,
        login_name,
    )

    password_ok = verify_password(user["password_hash"] if user else None, body.password)

    if user is None or user["status"] != "active":
        reason = "user_not_found" if user is None else "account_disabled"
        log.warning("panel girişi reddedildi: %s giriş=%s ip=%s", reason, login_name, request_ip(request))
        if user is not None:
            async with pool.acquire() as conn:
                await audit(conn, request, None, "auth.login_failed", target_type="admin_user",
                            target_id=str(user["id"]), actor_email=user["username"], details={"reason": reason})
        from app import autoban
        await autoban.record(redis, pool, await request.app.state.settings_cache.get(pool), request_ip(request), "panel_login")
        raise _INVALID

    if user["is_locked"]:
        log.warning("panel girişi reddedildi: account_locked giriş=%s ip=%s", login_name, request_ip(request))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={"error": "account_locked", "message": "Hesap çok fazla hatalı deneme nedeniyle geçici olarak kilitlendi."},
        )

    if not password_ok:
        log.warning("panel girişi reddedildi: wrong_password giriş=%s ip=%s", login_name, request_ip(request))
        from app import autoban
        await autoban.record(redis, pool, await request.app.state.settings_cache.get(pool), request_ip(request), "panel_login")
        async with pool.acquire() as conn:
            failed = await conn.fetchval(
                """
                UPDATE admin_users
                SET failed_logins = failed_logins + 1,
                    locked_until = CASE WHEN failed_logins + 1 >= $2
                                        THEN now() + make_interval(mins => $3) ELSE locked_until END
                WHERE id = $1 RETURNING failed_logins
                """,
                user["id"], MAX_FAILED_LOGINS, LOCK_MINUTES,
            )
            await audit(conn, request, None, "auth.login_failed", target_type="admin_user",
                        target_id=str(user["id"]), actor_email=user["username"],
                        details={"reason": "wrong_password", "failed_logins": failed})
        raise _INVALID

    await pool.execute("UPDATE admin_users SET failed_logins = 0, locked_until = NULL WHERE id = $1", user["id"])

    if user["totp_enabled"]:
        # Şifre doğru ama oturum henüz açılmaz: 5 dakika geçerli, tek kullanımlık ara anahtar
        token = secrets.token_urlsafe(32)
        await pool.execute(
            "INSERT INTO admin_mfa_challenges (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
            hashlib.sha256(token.encode()).hexdigest(), user["id"],
            datetime.now(timezone.utc) + timedelta(minutes=MFA_TTL_MINUTES),
        )
        return {"mfa_required": True, "mfa_token": token}

    await _complete_login(pool, redis, request, response, user, ip_key, method="password")
    return _row_payload(user)


def _row_payload(user) -> dict:
    return {
        "id": str(user["id"]), "username": user["username"], "email": user["email"], "name": user["name"],
        "role": user["role"], "must_change_password": user["must_change_password"],
        "totp_enabled": user["totp_enabled"],
    }


async def _complete_login(pool, redis, request: Request, response: Response, user, ip_key: str, method: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE admin_users SET last_login_at = now(), last_login_ip = $2::inet WHERE id = $1",
            user["id"], request_ip(request),
        )
        await audit(conn, request, None, "auth.login", target_type="admin_user",
                    target_id=str(user["id"]), actor_email=user["username"], details={"method": method})
    await create_session(pool, response, user["id"], request)
    await redis.delete(ip_key)


@router.post("/mfa")
async def login_mfa(body: MfaBody, request: Request, response: Response):
    """İki adımlı girişin ikinci adımı: doğrulama uygulamasındaki kod veya kurtarma kodu."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "csrf_check_failed"})
    pool = request.app.state.db
    redis = request.app.state.redis
    ip_key = f"admin:login:ip:{request_ip(request) or 'unknown'}"

    token_hash = hashlib.sha256(body.mfa_token.encode()).hexdigest()
    expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "mfa_expired", "message": "Doğrulama süresi doldu. Tekrar giriş yapın."},
    )
    async with pool.acquire() as conn, conn.transaction():
        ch = await conn.fetchrow(
            "SELECT user_id, attempts FROM admin_mfa_challenges WHERE token_hash = $1 AND expires_at > now() FOR UPDATE",
            token_hash,
        )
        if ch is None:
            raise expired
        user = await conn.fetchrow(
            """
            SELECT id, username, email, name, role, status, must_change_password, totp_enabled, totp_secret,
                   totp_last_step, recovery_codes
            FROM admin_users WHERE id = $1 FOR UPDATE
            """,
            ch["user_id"],
        )
        if user is None or user["status"] != "active" or not user["totp_enabled"]:
            await conn.execute("DELETE FROM admin_mfa_challenges WHERE token_hash = $1", token_hash)
            raise expired

        method = None
        step = check_totp(decrypt_secret(user["totp_secret"]), body.code, user["totp_last_step"])
        if step is not None:
            await conn.execute("UPDATE admin_users SET totp_last_step = $2 WHERE id = $1", user["id"], step)
            method = "totp"
        else:
            h = hash_recovery_code(body.code)
            if h in (user["recovery_codes"] or []):
                await conn.execute(
                    "UPDATE admin_users SET recovery_codes = array_remove(recovery_codes, $2) WHERE id = $1", user["id"], h
                )
                method = "recovery_code"

        if method is None:
            if ch["attempts"] + 1 >= MFA_MAX_ATTEMPTS:
                await conn.execute("DELETE FROM admin_mfa_challenges WHERE token_hash = $1", token_hash)
            else:
                await conn.execute(
                    "UPDATE admin_mfa_challenges SET attempts = attempts + 1 WHERE token_hash = $1", token_hash
                )
            await audit(conn, request, None, "auth.mfa_failed", target_type="admin_user",
                        target_id=str(user["id"]), actor_email=user["username"])
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_code", "message": "Kod hatalı veya süresi geçmiş."},
            )
        await conn.execute("DELETE FROM admin_mfa_challenges WHERE token_hash = $1", token_hash)

    await _complete_login(pool, redis, request, response, user, ip_key, method=method)
    payload = _row_payload(user)
    if method == "recovery_code":
        payload["recovery_codes_left"] = len(user["recovery_codes"]) - 1
    return payload


@router.post("/logout")
async def logout(request: Request, response: Response, user: AdminUser = Depends(current_user)):
    await revoke_session(request.app.state.db, user.session_id)
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request, user: AdminUser = Depends(current_user)):
    row = await request.app.state.db.fetchrow(
        "SELECT totp_enabled, cardinality(recovery_codes) AS recovery_left FROM admin_users WHERE id = $1", user.id
    )
    return _user_payload(user) | {"totp_enabled": row["totp_enabled"], "recovery_codes_left": row["recovery_left"]}


@router.post("/change-password")
async def change_password(body: ChangePasswordBody, request: Request, user: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    current_hash = await pool.fetchval("SELECT password_hash FROM admin_users WHERE id = $1", user.id)
    if not verify_password(current_hash, body.current_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "wrong_password", "message": "Mevcut şifre hatalı."},
        )
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "same_password", "message": "Yeni şifre eskisiyle aynı olamaz."},
        )
    validate_password(body.new_password, user.username)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE admin_users SET password_hash = $2, must_change_password = false, password_changed_at = now()
            WHERE id = $1
            """,
            user.id, hash_password(body.new_password),
        )
        # Diğer cihazlardaki oturumları kapat, mevcut oturum açık kalsın
        await conn.execute(
            "UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND id <> $2 AND revoked_at IS NULL",
            user.id, user.session_id,
        )
        await audit(conn, request, user, "auth.password_changed", target_type="admin_user", target_id=str(user.id))
    return {"ok": True}


@router.get("/sessions")
async def list_sessions(request: Request, user: AdminUser = Depends(current_user)):
    rows = await request.app.state.db.fetch(
        """
        SELECT id, host(ip) AS ip, user_agent, created_at, last_seen_at
        FROM admin_sessions
        WHERE user_id = $1 AND revoked_at IS NULL AND expires_at > now()
        ORDER BY last_seen_at DESC
        """,
        user.id,
    )
    return [dict(r) | {"current": r["id"] == user.session_id} for r in rows]


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request, user: AdminUser = Depends(current_user)):
    async with request.app.state.db.acquire() as conn:
        result = await conn.execute(
            "UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND id <> $2 AND revoked_at IS NULL",
            user.id, user.session_id,
        )
        await audit(conn, request, user, "auth.sessions_revoked", target_type="admin_user", target_id=str(user.id))
    return {"revoked": int(result.split()[-1])}


# ---------------------------------------------------------------- 2FA kurulumu

@router.post("/2fa/setup")
async def setup_2fa(request: Request, user: AdminUser = Depends(current_user)):
    """Yeni bir sır üretir (henüz etkin değil). Doğrulama uygulamasına eklenip /2fa/enable ile onaylanır."""
    pool = request.app.state.db
    if await pool.fetchval("SELECT totp_enabled FROM admin_users WHERE id = $1", user.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "already_enabled", "message": "İki adımlı doğrulama zaten açık."})
    secret = new_totp_secret()
    await pool.execute("UPDATE admin_users SET totp_secret = $2 WHERE id = $1", user.id, encrypt_secret(secret))
    return {"secret": secret, "otpauth_uri": totp_uri(secret, user.username)}


@router.post("/2fa/enable")
async def enable_2fa(body: CodeBody, request: Request, user: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT totp_secret, totp_enabled FROM admin_users WHERE id = $1 FOR UPDATE", user.id
        )
        if row["totp_enabled"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "already_enabled"})
        if not row["totp_secret"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "setup_required"})
        step = check_totp(decrypt_secret(row["totp_secret"]), body.code, None)
        if step is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail={"error": "invalid_code", "message": "Kod hatalı. Uygulamadaki güncel kodu girin."})
        codes = new_recovery_codes()
        await conn.execute(
            """
            UPDATE admin_users SET totp_enabled = true, totp_last_step = $2, recovery_codes = $3 WHERE id = $1
            """,
            user.id, step, [hash_recovery_code(c) for c in codes],
        )
        await conn.execute(
            "UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND id <> $2 AND revoked_at IS NULL",
            user.id, user.session_id,
        )
        await audit(conn, request, user, "auth.2fa_enabled", target_type="admin_user", target_id=str(user.id))
    return {"recovery_codes": codes}


@router.post("/2fa/disable")
async def disable_2fa(body: DisableMfaBody, request: Request, user: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT password_hash, totp_secret, totp_enabled, totp_last_step FROM admin_users WHERE id = $1 FOR UPDATE",
            user.id,
        )
        if not row["totp_enabled"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "not_enabled"})
        if not verify_password(row["password_hash"], body.password):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail={"error": "wrong_password", "message": "Şifre hatalı."})
        if check_totp(decrypt_secret(row["totp_secret"]), body.code, row["totp_last_step"]) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail={"error": "invalid_code", "message": "Kod hatalı."})
        await conn.execute(
            """
            UPDATE admin_users SET totp_enabled = false, totp_secret = NULL, totp_last_step = NULL,
                   recovery_codes = '{}' WHERE id = $1
            """,
            user.id,
        )
        await audit(conn, request, user, "auth.2fa_disabled", target_type="admin_user", target_id=str(user.id))
    return {"ok": True}


@router.post("/2fa/recovery-codes")
async def regenerate_recovery_codes(body: CodeBody, request: Request, user: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT totp_secret, totp_enabled, totp_last_step FROM admin_users WHERE id = $1 FOR UPDATE", user.id
        )
        if not row["totp_enabled"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "not_enabled"})
        step = check_totp(decrypt_secret(row["totp_secret"]), body.code, row["totp_last_step"])
        if step is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail={"error": "invalid_code", "message": "Kod hatalı."})
        codes = new_recovery_codes()
        await conn.execute(
            "UPDATE admin_users SET recovery_codes = $2, totp_last_step = $3 WHERE id = $1",
            user.id, [hash_recovery_code(c) for c in codes], step,
        )
        await audit(conn, request, user, "auth.recovery_codes_regenerated", target_type="admin_user",
                    target_id=str(user.id))
    return {"recovery_codes": codes}
