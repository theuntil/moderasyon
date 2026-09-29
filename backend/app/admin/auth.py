"""Admin paneli kimlik doğrulama.

- Şifreler Argon2id ile hash'lenir.
- Oturumlar sunucu tarafında tutulur; tarayıcıda sadece rastgele bir token (HttpOnly, Secure,
  SameSite=Strict cookie) bulunur. Veritabanında token'ın kendisi değil SHA-256'sı saklanır.
- CSRF: cookie SameSite=Strict + değiştiren her istekte özel başlık zorunlu. Tarayıcılar bu başlığı
  başka bir siteden CORS izni olmadan gönderemez; admin API'de CORS kapalıdır.
"""
import base64
import hashlib
import ipaddress
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pyotp
from argon2 import PasswordHasher
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status

from app.config import settings

# __Host- öneki: çerez sadece HTTPS'te, sadece bu alan adında ve path=/ ile set edilebilir
# (alt alan adlarından çerez enjeksiyonu engellenir). Lokal HTTP geliştirmede önek kullanılamaz.
SESSION_COOKIE = "__Host-mp_session" if settings.panel_cookie_secure else "mp_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "moderation-panel"

ROLE_RANK = {"viewer": 0, "moderator": 1, "admin": 2, "owner": 3}
ROLES = tuple(ROLE_RANK)

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()
# Kullanıcı bulunamadığında da aynı süre harcansın diye (e-posta tahminini zorlaştırır)
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


USERNAME_PATTERN = r"^[A-Za-z0-9._@-]{3,64}$"


def validate_password(password: str, username: str | None = None) -> None:
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"en az {MIN_PASSWORD_LENGTH} karakter olmalı")
    if password.lower() == password or password.upper() == password:
        problems.append("büyük ve küçük harf içermeli")
    if not any(c.isdigit() for c in password):
        problems.append("en az bir rakam içermeli")
    if username and username.split("@")[0].lower() in password.lower():
        problems.append("kullanıcı adını içermemeli")
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "weak_password", "message": "Şifre " + ", ".join(problems) + "."},
        )


def generate_password() -> str:
    # Okunabilir, güçlü geçici şifre: büyük/küçük harf + rakam garantili
    while True:
        pw = secrets.token_urlsafe(14)
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AdminUser:
    id: UUID
    username: str
    email: str | None
    name: str | None
    role: str
    must_change_password: bool
    session_id: UUID

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]


async def create_session(pool, response: Response, user_id: UUID, request: Request) -> None:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.panel_session_hours)
    await pool.execute(
        """
        INSERT INTO admin_sessions (user_id, token_hash, ip, user_agent, expires_at)
        VALUES ($1, $2, $3::inet, $4, $5)
        """,
        user_id, _token_hash(token), request_ip(request),
        (request.headers.get("user-agent") or "")[:300], expires,
    )
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=settings.panel_session_hours * 3600,
        httponly=True, secure=settings.panel_cookie_secure, samesite="strict", path="/",
    )


async def revoke_session(pool, session_id: UUID) -> None:
    await pool.execute("UPDATE admin_sessions SET revoked_at = now() WHERE id = $1", session_id)


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.panel_cookie_secure, samesite="strict")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "not_authenticated"})


async def current_user(request: Request) -> AdminUser:
    # CSRF: GET dışındaki her istek panelin özel başlığını taşımalı
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "csrf_check_failed"})

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise _unauthorized()

    pool = request.app.state.db
    row = await pool.fetchrow(
        """
        SELECT s.id AS session_id, s.last_seen_at, u.id, u.username, u.email, u.name, u.role, u.must_change_password
        FROM admin_sessions s
        JOIN admin_users u ON u.id = s.user_id
        WHERE s.token_hash = $1
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND s.last_seen_at > now() - make_interval(mins => $2)
          AND u.status = 'active'
        """,
        _token_hash(token), settings.panel_idle_minutes,
    )
    if row is None:
        raise _unauthorized()

    await pool.execute(
        "UPDATE admin_sessions SET last_seen_at = now() WHERE id = $1 AND last_seen_at < now() - interval '1 minute'",
        row["session_id"],
    )
    user = AdminUser(row["id"], row["username"], row["email"], row["name"], row["role"], row["must_change_password"], row["session_id"])

    # Geçici şifreyle giren kullanıcı, şifresini değiştirene kadar başka bir şey yapamaz
    allowed_while_forced = ("/auth/me", "/auth/logout", "/auth/change-password")
    if user.must_change_password and request.url.path not in allowed_while_forced:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "password_change_required"})
    return user


def require_role(minimum: str):
    async def dependency(user: AdminUser = Depends(current_user)) -> AdminUser:
        if not user.has_role(minimum):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "insufficient_role"})
        return user

    return dependency


async def audit(
    conn,
    request: Request,
    user: AdminUser | None,
    action: str,
    *,
    project_id: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
    actor_email: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_id, actor_email, action, project_id, target_type, target_id, details, ip)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::inet)
        """,
        user.id if user else None,
        user.username if user else actor_email,
        action, project_id, target_type, target_id,
        json.loads(json.dumps(details or {}, default=str)),
        request_ip(request),
    )


# ---------------------------------------------------------------- 2FA (TOTP)

_TOTP_KEY = hashlib.sha256(b"totp-secret-encryption:" + settings.api_key_pepper.encode()).digest()


def encrypt_secret(secret: str) -> str:
    nonce = secrets.token_bytes(12)
    return "v1:" + base64.b64encode(nonce + AESGCM(_TOTP_KEY).encrypt(nonce, secret.encode(), None)).decode()


def decrypt_secret(stored: str) -> str:
    raw = base64.b64decode(stored.removeprefix("v1:"))
    return AESGCM(_TOTP_KEY).decrypt(raw[:12], raw[12:], None).decode()


def new_totp_secret() -> str:
    return pyotp.random_base32(32)


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Moderasyon Paneli")


def check_totp(secret: str, code: str, last_step: int | None) -> int | None:
    """Kod geçerliyse kullanılan zaman adımını döner. Aynı adım ikinci kez kabul edilmez (tekrar oynatma)."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(datetime.now(timezone.utc).timestamp()) // totp.interval
    for step in (now_step - 1, now_step, now_step + 1):
        if last_step is not None and step <= last_step:
            continue
        if secrets.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def new_recovery_codes(n: int = 10) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
            for _ in range(n)]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
