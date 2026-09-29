import ipaddress
import time
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.admin.auth import (
    ROLES, USERNAME_PATTERN, AdminUser, audit, current_user, generate_password, hash_password, require_role,
)

router = APIRouter(tags=["platform"])


# ---------------------------------------------------------------- settings

class SettingsUpdate(BaseModel):
    service_enabled: bool | None = None
    maintenance_message: str | None = Field(default=None, max_length=300)
    threshold_review: float | None = Field(default=None, gt=0, lt=1)
    threshold_block: float | None = Field(default=None, gt=0, le=1)
    default_rate_limit_per_second: int | None = Field(default=None, ge=1, le=100_000)
    default_rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10_000_000)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    evidence_retention_hours: int | None = Field(default=None, ge=0, le=8760)
    legal_hold_enabled: bool | None = None
    ai_sensitive_media: bool | None = None
    visual_rules_enabled: bool | None = None
    autoban_mode: Literal["off", "monitor", "enforce"] | None = None
    autoban_auth_fail_limit: int | None = Field(default=None, ge=5, le=100_000)
    autoban_scan_limit: int | None = Field(default=None, ge=5, le=100_000)
    autoban_flood_limit: int | None = Field(default=None, ge=100, le=10_000_000)
    autoban_panel_login_limit: int | None = Field(default=None, ge=5, le=10_000)
    autoban_allowlist: list[str] | None = Field(default=None, max_length=200)

    @field_validator("autoban_allowlist")
    @classmethod
    def _networks(cls, v):
        if v is None:
            return v
        out = []
        for item in v:
            item = item.strip()
            if not item:
                continue
            try:
                out.append(str(ipaddress.ip_network(item, strict=False)))
            except ValueError:
                raise ValueError(f"Geçersiz IP veya CIDR: {item}")
        return out


_SETTINGS_SELECT = """
    SELECT s.service_enabled, s.maintenance_message, s.threshold_review, s.threshold_block,
           s.policy_version, s.default_rate_limit_per_second, s.default_rate_limit_per_minute,
           s.retention_days, s.autoban_mode, s.autoban_auth_fail_limit, s.autoban_scan_limit, s.autoban_flood_limit,
           s.autoban_panel_login_limit, s.autoban_allowlist, s.evidence_retention_hours,
           s.legal_hold_enabled, s.ai_sensitive_media, s.visual_rules_enabled, s.updated_at, u.username AS updated_by_email
    FROM platform_settings s LEFT JOIN admin_users u ON u.id = s.updated_by
    WHERE s.id = 1
"""


@router.get("/settings")
async def get_settings(request: Request, _: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    current = await pool.fetchrow(_SETTINGS_SELECT)
    versions = await pool.fetch(
        """
        SELECT v.version, v.threshold_review, v.threshold_block, v.created_at, u.username AS created_by_email
        FROM policy_versions v LEFT JOIN admin_users u ON u.id = v.created_by
        ORDER BY v.version DESC LIMIT 20
        """
    )
    return dict(current) | {"policy_versions": [dict(v) for v in versions]}


@router.patch("/settings")
async def update_settings(body: SettingsUpdate, request: Request, user: AdminUser = Depends(require_role("admin"))):
    changes = body.model_dump(exclude_unset=True)
    for k in ("threshold_review", "threshold_block"):
        if changes.get(k) is not None:
            changes[k] = round(changes[k], 3)
    if not changes:
        return {"ok": True}

    async with request.app.state.db.acquire() as conn, conn.transaction():
        before = await conn.fetchrow("SELECT * FROM platform_settings WHERE id = 1 FOR UPDATE")
        review = changes.get("threshold_review", before["threshold_review"])
        block = changes.get("threshold_block", before["threshold_block"])
        if review >= block:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "invalid_thresholds", "message": "İnceleme eşiği, engelleme eşiğinden küçük olmalı."},
            )

        cols = list(changes)
        thresholds_changed = (
            abs(review - before["threshold_review"]) > 1e-6 or abs(block - before["threshold_block"]) > 1e-6
        )
        new_version = before["policy_version"] + 1 if thresholds_changed else before["policy_version"]

        sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await conn.execute(
            f"""
            UPDATE platform_settings
            SET {sets}, policy_version = ${len(cols) + 1}, updated_by = ${len(cols) + 2}, updated_at = now()
            WHERE id = 1
            """,
            *[changes[c] for c in cols], new_version, user.id,
        )
        if thresholds_changed:
            await conn.execute(
                "INSERT INTO policy_versions (version, threshold_review, threshold_block, created_by) VALUES ($1, $2, $3, $4)",
                new_version, review, block, user.id,
            )

        diff = {c: {"old": before[c], "new": changes[c]} for c in cols if before[c] != changes[c]}
        action = "settings.updated"
        if "service_enabled" in diff:
            action = "service.enabled" if changes["service_enabled"] else "service.disabled"
        elif thresholds_changed:
            action = "policy.updated"
        if thresholds_changed:
            diff["policy_version"] = {"old": before["policy_version"], "new": new_version}
        await audit(conn, request, user, action, target_type="settings", target_id="platform", details=diff)
    return {"ok": True, "policy_version": new_version}


# ---------------------------------------------------------------- admin users

class AdminCreate(BaseModel):
    email: EmailStr
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)   # boşsa e-posta kullanılır
    name: str | None = Field(default=None, max_length=100)
    role: Literal["owner", "admin", "moderator", "viewer"]


class AdminUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    role: Literal["owner", "admin", "moderator", "viewer"] | None = None
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def _something(self):
        if self.name is None and self.role is None and self.status is None:
            raise ValueError("Değiştirilecek bir alan yok.")
        return self


@router.get("/admins")
async def list_admins(request: Request, _: AdminUser = Depends(require_role("admin"))):
    rows = await request.app.state.db.fetch(
        """
        SELECT id, username, email, name, role, status, must_change_password, last_login_at, totp_enabled,
               host(last_login_ip) AS last_login_ip,
               created_at, locked_until > now() AS locked,
               (SELECT count(*) FROM admin_sessions s
                 WHERE s.user_id = a.id AND s.revoked_at IS NULL AND s.expires_at > now()) AS active_sessions
        FROM admin_users a
        ORDER BY array_position($1::text[], role), created_at
        """,
        list(reversed(ROLES)),
    )
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.post("/admins", status_code=status.HTTP_201_CREATED)
async def create_admin(body: AdminCreate, request: Request, user: AdminUser = Depends(require_role("owner"))):
    temp_password = generate_password()
    async with request.app.state.db.acquire() as conn, conn.transaction():
        try:
            admin_id = await conn.fetchval(
                """
                INSERT INTO admin_users (username, email, name, role, password_hash, must_change_password, created_by)
                VALUES ($1, $2, $3, $4, $5, true, $6) RETURNING id
                """,
                (body.username or body.email).strip().lower(), body.email.lower(), body.name, body.role,
                hash_password(temp_password), user.id,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "username_taken", "message": "Bu kullanıcı adı veya e-posta başka bir yöneticide kayıtlı."},
            )
        await audit(conn, request, user, "admin.created", target_type="admin_user", target_id=str(admin_id),
                    details={"email": body.email, "role": body.role})
    return {"id": str(admin_id), "temporary_password": temp_password}


async def _ensure_owner_remains(conn, excluding: UUID) -> None:
    others = await conn.fetchval(
        "SELECT count(*) FROM admin_users WHERE role = 'owner' AND status = 'active' AND id <> $1", excluding
    )
    if others == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "last_owner", "message": "En az bir aktif owner kalmalı."},
        )


@router.patch("/admins/{admin_id}")
async def update_admin(
    admin_id: UUID, body: AdminUpdate, request: Request, user: AdminUser = Depends(require_role("owner"))
):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        target = await conn.fetchrow("SELECT * FROM admin_users WHERE id = $1 FOR UPDATE", admin_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "admin_not_found"})
        if admin_id == user.id and (body.status == "disabled" or (body.role and body.role != "owner")):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "cannot_modify_self", "message": "Kendi rolünüzü düşüremez veya hesabınızı kapatamazsınız."},
            )
        losing_owner = target["role"] == "owner" and (
            (body.role and body.role != "owner") or body.status == "disabled"
        )
        if losing_owner:
            await _ensure_owner_remains(conn, admin_id)

        changes = body.model_dump(exclude_none=True)
        cols = list(changes)
        sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        await conn.execute(f"UPDATE admin_users SET {sets} WHERE id = $1", admin_id, *[changes[c] for c in cols])

        # Rol değişince veya hesap kapatılınca açık oturumlar sonlandırılır
        if body.status == "disabled" or (body.role and body.role != target["role"]):
            await conn.execute(
                "UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", admin_id
            )
        diff = {c: {"old": target[c], "new": changes[c]} for c in cols if target[c] != changes[c]}
        await audit(conn, request, user, "admin.updated", target_type="admin_user", target_id=str(admin_id),
                    details={"username": target["username"], **diff})
    return {"ok": True}


@router.post("/admins/{admin_id}/reset-password")
async def reset_admin_password(admin_id: UUID, request: Request, user: AdminUser = Depends(require_role("owner"))):
    temp_password = generate_password()
    async with request.app.state.db.acquire() as conn, conn.transaction():
        username = await conn.fetchval(
            """
            UPDATE admin_users SET password_hash = $2, must_change_password = true, failed_logins = 0,
                   locked_until = NULL, password_changed_at = now()
            WHERE id = $1 RETURNING username
            """,
            admin_id, hash_password(temp_password),
        )
        if username is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "admin_not_found"})
        await conn.execute(
            "UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", admin_id
        )
        await audit(conn, request, user, "admin.password_reset", target_type="admin_user",
                    target_id=str(admin_id), details={"username": username})
    return {"temporary_password": temp_password}


@router.post("/admins/{admin_id}/reset-2fa")
async def reset_admin_2fa(admin_id: UUID, request: Request, user: AdminUser = Depends(require_role("owner"))):
    """Telefonunu kaybeden yöneticinin 2FA'sını kapatır; bir sonraki girişte yeniden kurabilir."""
    if admin_id == user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "cannot_modify_self", "message": "Kendi 2FA'nızı Hesabım sayfasından yönetin."})
    async with request.app.state.db.acquire() as conn, conn.transaction():
        username = await conn.fetchval(
            """
            UPDATE admin_users SET totp_enabled = false, totp_secret = NULL, totp_last_step = NULL, recovery_codes = '{}'
            WHERE id = $1 RETURNING username
            """,
            admin_id,
        )
        if username is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "admin_not_found"})
        await conn.execute("UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", admin_id)
        await audit(conn, request, user, "admin.2fa_reset", target_type="admin_user", target_id=str(admin_id),
                    details={"username": username})
    return {"ok": True}


@router.delete("/admins/{admin_id}")
async def delete_admin(admin_id: UUID, request: Request, user: AdminUser = Depends(require_role("owner"))):
    if admin_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "cannot_modify_self", "message": "Kendi hesabınızı silemezsiniz."},
        )
    async with request.app.state.db.acquire() as conn, conn.transaction():
        target = await conn.fetchrow("SELECT username, role FROM admin_users WHERE id = $1 FOR UPDATE", admin_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "admin_not_found"})
        if target["role"] == "owner":
            await _ensure_owner_remains(conn, admin_id)
        await conn.execute("DELETE FROM admin_users WHERE id = $1", admin_id)
        await audit(conn, request, user, "admin.deleted", target_type="admin_user", target_id=str(admin_id),
                    details={"username": target["username"], "role": target["role"]})
    return {"ok": True}


# ---------------------------------------------------------------- audit log

@router.get("/audit")
async def list_audit(
    request: Request,
    project_id: UUID | None = None,
    action: str | None = Query(None, max_length=64),
    actor: str | None = Query(None, max_length=254),
    before: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    _: AdminUser = Depends(require_role("admin")),
):
    rows = await request.app.state.db.fetch(
        """
        SELECT a.id, a.actor_email, a.action, a.project_id, p.name AS project_name, a.target_type, a.target_id,
               a.details, host(a.ip) AS ip, a.created_at
        FROM audit_logs a
        LEFT JOIN projects p ON p.id = a.project_id
        WHERE ($1::uuid IS NULL OR a.project_id = $1)
          AND ($2::text IS NULL OR a.action LIKE $2 || '%')
          AND ($3::text IS NULL OR lower(a.actor_email) = lower(trim($3)))
          AND ($4::bigint IS NULL OR a.id < $4)
        ORDER BY a.id DESC
        LIMIT $5
        """,
        project_id, action, actor, before, limit,
    )
    items = [dict(r) | {"project_id": str(r["project_id"]) if r["project_id"] else None} for r in rows]
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}


# ---------------------------------------------------------------- system health

@router.get("/system")
async def system_health(request: Request, _: AdminUser = Depends(current_user)):
    pool = request.app.state.db
    redis = request.app.state.redis
    result: dict = {"checked_at": datetime.now().astimezone().isoformat()}

    t = time.perf_counter()
    try:
        db_version = await pool.fetchval("SHOW server_version")
        db_size = await pool.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        result["database"] = {"ok": True, "latency_ms": round((time.perf_counter() - t) * 1000, 1),
                              "version": db_version, "size": db_size}
    except Exception as exc:  # noqa: BLE001
        result["database"] = {"ok": False, "error": type(exc).__name__}

    t = time.perf_counter()
    try:
        await redis.ping()
        info = await redis.info("memory")
        queue_depth = await redis.zcard("arq:queue")
        media_depth = await redis.zcard("arq:media")
        # arq worker'ları çalışırken bu anahtarları periyodik olarak günceller
        heartbeat = await redis.get("arq:queue:health-check")
        media_heartbeat = await redis.get("arq:media:health-check")
        result["redis"] = {"ok": True, "latency_ms": round((time.perf_counter() - t) * 1000, 1),
                           "memory": info.get("used_memory_human")}
        result["queue"] = {"depth": queue_depth, "media_depth": media_depth}
        result["worker"] = {"ok": heartbeat is not None,
                            "heartbeat": heartbeat.decode() if isinstance(heartbeat, bytes) else heartbeat}
        result["media_worker"] = {"ok": media_heartbeat is not None,
                                  "heartbeat": media_heartbeat.decode() if isinstance(media_heartbeat, bytes) else media_heartbeat}
    except Exception as exc:  # noqa: BLE001
        result["redis"] = {"ok": False, "error": type(exc).__name__}

    if result["database"].get("ok"):
        row = await pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status = 'queued' AND created_at < now() - interval '5 minutes') AS stuck,
                   count(*) FILTER (WHERE status IN ('queued', 'processing')) AS in_progress,
                   count(*) FILTER (WHERE status = 'failed' AND created_at >= now() - interval '24 hours') AS failed_24h
            FROM moderation_requests
            WHERE created_at >= now() - interval '2 days'
            """
        )
        result.setdefault("queue", {}).update(dict(row))
        settings_row = await pool.fetchrow(
            "SELECT service_enabled, policy_version, retention_days FROM platform_settings WHERE id = 1"
        )
        result["service"] = dict(settings_row)
        result["webhooks"] = dict(await pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'failed' AND created_at >= now() - interval '24 hours') AS failed_24h
            FROM webhook_deliveries
            """
        ))
        from app.storage import storage
        ev = await pool.fetchrow(
            """
            SELECT count(*) AS files, count(*) FILTER (WHERE q.status = 'pending') AS pending_review
            FROM media_evidence e LEFT JOIN review_queue q ON q.request_id = e.request_id
            """
        )
        waiting = await pool.fetchval(
            "SELECT count(*) FROM moderation_requests WHERE media_key IS NOT NULL AND status IN ('queued', 'processing')"
        )
        result["storage"] = (await storage.health()) | {
            "evidence_files": ev["files"], "evidence_pending_review": ev["pending_review"], "incoming_files": waiting,
        }
    return result
