from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app import webhooks
from app.config import settings
from app.admin.auth import AdminUser, audit, current_user, require_role
from app.storage import storage
from app.netsafe import UnsafeURL, validate_url
from app.security import api_key_prefix, generate_api_key, hash_api_key

router = APIRouter(tags=["projects"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _not_found(what: str = "project") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": f"{what}_not_found"})


# ---------------------------------------------------------------- projects

class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    slug: str = Field(pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=500)
    key_name: str = Field(default="Default", min_length=1, max_length=100)
    key_environment: Literal["live", "test"] = "test"


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    status: Literal["active", "disabled"] | None = None
    rate_limit_per_second: int | None = Field(default=None, ge=1, le=100_000)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10_000_000)
    use_default_rate_limits: bool = False


class ProjectDelete(BaseModel):
    confirm_slug: str


_PROJECT_COLUMNS = """
    p.id, p.name, p.slug, p.description, p.status, p.created_at, p.updated_at,
    p.rate_limit_per_second, p.rate_limit_per_minute, p.webhook_url, p.webhook_enabled,
    (SELECT count(*) FROM project_api_keys k WHERE k.project_id = p.id AND k.status = 'active') AS active_keys,
    (SELECT count(*) FROM review_queue q WHERE q.project_id = p.id AND q.status = 'pending') AS pending_reviews,
    s.requests_30d, s.block_30d, s.review_30d, s.latency_30d, s.last_request_at
"""
_PROJECT_STATS_JOIN = """
    LEFT JOIN LATERAL (
        SELECT count(*) AS requests_30d,
               count(*) FILTER (WHERE res.decision = 'block')  AS block_30d,
               count(*) FILTER (WHERE res.decision = 'review') AS review_30d,
               round(avg(extract(epoch FROM r.completed_at - r.created_at) * 1000))::bigint AS latency_30d,
               max(r.created_at) AS last_request_at
        FROM moderation_requests r
        LEFT JOIN moderation_results res ON res.request_id = r.id
        WHERE r.project_id = p.id AND r.created_at >= now() - interval '30 days'
    ) s ON true
"""


def _project(row) -> dict:
    return dict(row) | {"id": str(row["id"])}


@router.get("/projects")
async def list_projects(request: Request, _: AdminUser = Depends(current_user)):
    rows = await request.app.state.db.fetch(
        f"SELECT {_PROJECT_COLUMNS} FROM projects p {_PROJECT_STATS_JOIN} ORDER BY p.created_at DESC"
    )
    return [_project(r) for r in rows]


@router.get("/projects/{project_id}")
async def get_project(project_id: UUID, request: Request, _: AdminUser = Depends(current_user)):
    row = await request.app.state.db.fetchrow(
        f"SELECT {_PROJECT_COLUMNS} FROM projects p {_PROJECT_STATS_JOIN} WHERE p.id = $1", project_id
    )
    if row is None:
        raise _not_found()
    return _project(row)


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, request: Request, user: AdminUser = Depends(require_role("admin"))):
    key = generate_api_key(body.key_environment)
    async with request.app.state.db.acquire() as conn, conn.transaction():
        try:
            project_id = await conn.fetchval(
                "INSERT INTO projects (name, slug, description) VALUES ($1, $2, $3) RETURNING id",
                body.name.strip(), body.slug, body.description,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "slug_taken", "message": "Bu slug başka bir project tarafından kullanılıyor."},
            )
        key_id = await conn.fetchval(
            """
            INSERT INTO project_api_keys (project_id, name, key_prefix, key_hash, environment, created_by)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """,
            project_id, body.key_name, api_key_prefix(key), hash_api_key(key), body.key_environment, user.id,
        )
        await audit(conn, request, user, "project.created", project_id=project_id, target_type="project",
                    target_id=str(project_id), details={"name": body.name, "slug": body.slug})
        await audit(conn, request, user, "api_key.created", project_id=project_id, target_type="api_key",
                    target_id=str(key_id), details={"name": body.key_name, "environment": body.key_environment})
    return {"id": str(project_id), "api_key": key, "api_key_id": str(key_id)}


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: UUID, body: ProjectUpdate, request: Request, user: AdminUser = Depends(require_role("admin"))
):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        before = await conn.fetchrow("SELECT * FROM projects WHERE id = $1 FOR UPDATE", project_id)
        if before is None:
            raise _not_found()

        changes = body.model_dump(exclude_unset=True, exclude={"use_default_rate_limits"})
        if body.use_default_rate_limits:
            changes["rate_limit_per_second"] = None
            changes["rate_limit_per_minute"] = None
        if not changes:
            return {"ok": True}

        allowed = {"name", "description", "status", "rate_limit_per_second", "rate_limit_per_minute"}
        cols = [c for c in changes if c in allowed]
        sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
        await conn.execute(
            f"UPDATE projects SET {sets}, updated_at = now() WHERE id = $1",
            project_id, *[changes[c] for c in cols],
        )

        diff = {c: {"old": before[c], "new": changes[c]} for c in cols if before[c] != changes[c]}
        action = "project.updated"
        if "status" in diff:
            action = "project.enabled" if changes["status"] == "active" else "project.disabled"
        await audit(conn, request, user, action, project_id=project_id, target_type="project",
                    target_id=str(project_id), details=diff)
    return {"ok": True}


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: UUID, body: ProjectDelete, request: Request, user: AdminUser = Depends(require_role("admin"))
):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        project = await conn.fetchrow("SELECT id, name, slug FROM projects WHERE id = $1 FOR UPDATE", project_id)
        if project is None:
            raise _not_found()
        if body.confirm_slug != project["slug"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "confirmation_mismatch", "message": "Onay için project slug'ını doğru yazın."},
            )
        held = await conn.fetchval("SELECT count(*) FROM moderation_requests WHERE project_id = $1 AND legal_hold", project_id)
        if held:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "error": "legal_hold_exists",
                "message": f"Bu projede yasal saklamada {held} içerik var. Önce Yasal saklama sayfasından silin."})
        # Diskteki kanıt görselleri: kayıtlar silinmeden önce yolları topla, commit'ten sonra sil
        evidence_paths = [r["path"] for r in await conn.fetch(
            """
            SELECT e.path FROM media_evidence e JOIN moderation_requests r ON r.id = e.request_id
            WHERE r.project_id = $1
            """,
            project_id,
        )]
        evidence_paths += [r["preview_path"] for r in await conn.fetch(
            "SELECT preview_path FROM hash_blocklist WHERE project_id = $1 AND preview_path IS NOT NULL", project_id
        )]
        # Sonuçlar ve review kayıtları isteklere bağlı olduğu için onlarla birlikte silinir
        deleted = await conn.execute("DELETE FROM moderation_requests WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)  # key'ler ve IP kuralları cascade
        await audit(conn, request, user, "project.deleted", project_id=project_id, target_type="project",
                    target_id=str(project_id),
                    details={"name": project["name"], "slug": project["slug"],
                             "deleted_requests": int(deleted.split()[-1])})
    if evidence_paths and storage.client is not None:
        await storage.delete_keys(evidence_paths)
    return {"ok": True}


# ---------------------------------------------------------------- API keys

class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    environment: Literal["live", "test"]
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyRotate(BaseModel):
    # Eski key'in çalışmaya devam edeceği süre. 0 = hemen iptal.
    grace_hours: int = Field(default=0, ge=0, le=168)


def _key(row) -> dict:
    return dict(row) | {"id": str(row["id"]), "project_id": str(row["project_id"])}


@router.get("/projects/{project_id}/keys")
async def list_keys(project_id: UUID, request: Request, _: AdminUser = Depends(require_role("admin"))):
    pool = request.app.state.db
    keys = await pool.fetch(
        """
        SELECT k.id, k.project_id, k.name, k.key_prefix, k.environment, k.status, k.created_at,
               k.last_used_at, k.expires_at, k.revoked_at, u.username AS created_by_email
        FROM project_api_keys k
        LEFT JOIN admin_users u ON u.id = k.created_by
        WHERE k.project_id = $1
        ORDER BY (k.status = 'revoked'), k.created_at DESC
        """,
        project_id,
    )
    # Son 30 günde her key'in hangi IP'lerden kullanıldığı
    ips = await pool.fetch(
        """
        SELECT api_key_id, host(client_ip) AS ip, count(*) AS requests, max(created_at) AS last_seen
        FROM moderation_requests
        WHERE project_id = $1 AND client_ip IS NOT NULL AND created_at >= now() - interval '30 days'
        GROUP BY 1, 2 ORDER BY last_seen DESC
        """,
        project_id,
    )
    by_key: dict[UUID, list] = {}
    for r in ips:
        by_key.setdefault(r["api_key_id"], []).append(
            {"ip": r["ip"], "requests": r["requests"], "last_seen": r["last_seen"]}
        )
    return [_key(k) | {"recent_ips": by_key.get(k["id"], [])[:10]} for k in keys]


async def _insert_key(conn, project_id: UUID, name: str, env: str, expires_at, user: AdminUser):
    key = generate_api_key(env)
    key_id = await conn.fetchval(
        """
        INSERT INTO project_api_keys (project_id, name, key_prefix, key_hash, environment, expires_at, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """,
        project_id, name, api_key_prefix(key), hash_api_key(key), env, expires_at, user.id,
    )
    return key, key_id


@router.post("/projects/{project_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_key(
    project_id: UUID, body: KeyCreate, request: Request, user: AdminUser = Depends(require_role("admin"))
):
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    async with request.app.state.db.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", project_id):
            raise _not_found()
        key, key_id = await _insert_key(conn, project_id, body.name, body.environment, expires_at, user)
        await audit(conn, request, user, "api_key.created", project_id=project_id, target_type="api_key",
                    target_id=str(key_id), details={"name": body.name, "environment": body.environment,
                                                    "expires_at": expires_at})
    return {"id": str(key_id), "api_key": key}


async def _get_key(conn, key_id: UUID):
    row = await conn.fetchrow("SELECT * FROM project_api_keys WHERE id = $1 FOR UPDATE", key_id)
    if row is None:
        raise _not_found("api_key")
    return row


@router.post("/keys/{key_id}/rotate")
async def rotate_key(key_id: UUID, body: KeyRotate, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        old = await _get_key(conn, key_id)
        if old["status"] == "revoked":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "key_revoked"})
        new_key, new_id = await _insert_key(conn, old["project_id"], old["name"], old["environment"],
                                            old["expires_at"], user)
        if body.grace_hours == 0:
            await conn.execute(
                "UPDATE project_api_keys SET status = 'revoked', revoked_at = now() WHERE id = $1", key_id
            )
        else:
            await conn.execute(
                "UPDATE project_api_keys SET expires_at = now() + make_interval(hours => $2) WHERE id = $1",
                key_id, body.grace_hours,
            )
        await audit(conn, request, user, "api_key.rotated", project_id=old["project_id"], target_type="api_key",
                    target_id=str(key_id),
                    details={"name": old["name"], "new_key_id": str(new_id), "grace_hours": body.grace_hours})
    return {"id": str(new_id), "api_key": new_key}


@router.post("/keys/{key_id}/{action}")
async def key_action(
    key_id: UUID,
    action: Literal["disable", "enable", "revoke"],
    request: Request,
    user: AdminUser = Depends(require_role("admin")),
):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        key = await _get_key(conn, key_id)
        if key["status"] == "revoked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "key_revoked", "message": "İptal edilmiş bir key tekrar kullanılamaz."},
            )
        new_status = {"disable": "disabled", "enable": "active", "revoke": "revoked"}[action]
        await conn.execute(
            """
            UPDATE project_api_keys SET status = $2,
                   revoked_at = CASE WHEN $2 = 'revoked' THEN now() ELSE revoked_at END
            WHERE id = $1
            """,
            key_id, new_status,
        )
        await audit(conn, request, user, f"api_key.{action}d" if action != "revoke" else "api_key.revoked",
                    project_id=key["project_id"], target_type="api_key", target_id=str(key_id),
                    details={"name": key["name"], "key_prefix": key["key_prefix"]})
    return {"ok": True}


# ---------------------------------------------------------------- webhook

class WebhookUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


@router.get("/projects/{project_id}/webhook")
async def get_webhook(project_id: UUID, request: Request, _: AdminUser = Depends(require_role("admin"))):
    pool = request.app.state.db
    p = await pool.fetchrow(
        "SELECT webhook_url, webhook_enabled, webhook_secret IS NOT NULL AS has_secret FROM projects WHERE id = $1",
        project_id,
    )
    if p is None:
        raise _not_found()
    deliveries = await pool.fetch(
        """
        SELECT id, event, request_public_id, status, attempts, last_status_code, last_error, next_attempt_at,
               delivered_at, created_at
        FROM webhook_deliveries WHERE project_id = $1 ORDER BY created_at DESC LIMIT 50
        """,
        project_id,
    )
    stats = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'delivered') AS delivered,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'pending') AS pending
        FROM webhook_deliveries WHERE project_id = $1 AND created_at >= now() - interval '7 days'
        """,
        project_id,
    )
    return dict(p) | {"stats_7d": dict(stats), "deliveries": [dict(d) | {"id": str(d["id"])} for d in deliveries]}


@router.patch("/projects/{project_id}/webhook")
async def update_webhook(
    project_id: UUID, body: WebhookUpdate, request: Request, user: AdminUser = Depends(require_role("admin"))
):
    if body.url:
        try:
            validate_url(body.url)
        except UnsafeURL as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "url_not_allowed", "field": "url",
                        "message": f"Bu adrese gönderim yapılamaz ({exc}). Herkese açık bir https adresi girin."},
            )
        if not body.url.startswith("https://") and not settings.allow_private_network:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error": "https_required", "field": "url", "message": "Webhook adresi https:// ile başlamalı."},
            )
    new_secret = None
    async with request.app.state.db.acquire() as conn, conn.transaction():
        p = await conn.fetchrow("SELECT webhook_url, webhook_enabled, webhook_secret FROM projects WHERE id = $1 FOR UPDATE",
                                project_id)
        if p is None:
            raise _not_found()
        url = body.url if body.url is not None else p["webhook_url"]
        if body.url == "":
            url = None
        enabled = body.enabled if body.enabled is not None else p["webhook_enabled"]
        if enabled and not url:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail={"error": "url_required", "message": "Önce webhook adresini girin."})
        secret = p["webhook_secret"]
        if secret is None and url:
            secret = new_secret = webhooks.new_secret()
        await conn.execute(
            "UPDATE projects SET webhook_url = $2, webhook_enabled = $3, webhook_secret = $4, updated_at = now() WHERE id = $1",
            project_id, url, enabled, secret,
        )
        await audit(conn, request, user, "webhook.updated", project_id=project_id, target_type="project",
                    target_id=str(project_id), details={"url": url, "enabled": enabled})
    return {"ok": True, "secret": new_secret}


@router.post("/projects/{project_id}/webhook/rotate-secret")
async def rotate_webhook_secret(project_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    secret = webhooks.new_secret()
    async with request.app.state.db.acquire() as conn, conn.transaction():
        done = await conn.fetchval("UPDATE projects SET webhook_secret = $2 WHERE id = $1 RETURNING id", project_id, secret)
        if not done:
            raise _not_found()
        await audit(conn, request, user, "webhook.secret_rotated", project_id=project_id, target_type="project",
                    target_id=str(project_id))
    return {"secret": secret}


@router.post("/projects/{project_id}/webhook/test")
async def test_webhook(project_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        delivery = await webhooks.record_event(conn, project_id, "moderation.completed", {
            "id": "mod_test_" + "0" * 16, "status": "completed", "type": "text", "decision": "allow",
            "reason": "no_violation_detected", "user_id": None, "content_id": None, "test": True,
        })
        if delivery is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail={"error": "webhook_disabled", "message": "Webhook kapalı veya adres girilmemiş."})
    await webhooks.enqueue(request.app.state.arq, delivery)
    return {"delivery_id": str(delivery)}


# ---------------------------------------------------------------- proje policy'si

KNOWN_CATEGORIES = ("harassment", "hate", "sexual", "nudity", "suggestive", "violence", "weapons", "drugs",
                    "self_harm", "spam", "scam", "extremism", "illicit")


class CategoryRule(BaseModel):
    action: Literal["default", "allow", "review", "block"] = "default"
    review: float | None = Field(default=None, gt=0, lt=1)
    block: float | None = Field(default=None, gt=0, le=1)


class PolicyBody(BaseModel):
    categories: dict[str, CategoryRule] = Field(default_factory=dict)
    decision_mode: Literal["two_step", "three_step"] = "two_step"
    uncertain_action: Literal["block", "allow"] = "block"
    ai_mode: Literal["off", "smart", "always"] = "smart"
    ai_trigger: float = Field(default=0.5, ge=0.05, le=0.99)
    fallback_decision: Literal["allow", "review", "block"] | None = None
    profanity_level: Literal["strict", "moderate", "off"] = "strict"


@router.get("/projects/{project_id}/policy")
async def get_policy(project_id: UUID, request: Request, _: AdminUser = Depends(current_user)):
    from app.moderation import ai

    row = await request.app.state.db.fetchrow("SELECT policy, policy_revision FROM projects WHERE id = $1", project_id)
    if row is None:
        raise _not_found()
    platform = await request.app.state.db.fetchrow("SELECT ai_enabled, ai_provider, ai_model FROM platform_settings WHERE id = 1")
    label = "OpenAI Moderation (ücretsiz)" if platform["ai_provider"] == "openai_moderation" else platform["ai_model"]
    budget = await request.app.state.db.fetchval("SELECT ai_monthly_budget_usd FROM projects WHERE id = $1", project_id)
    from app.moderation.policy import ProjectPolicy

    eff = ProjectPolicy.from_row(row["policy"], row["policy_revision"])
    effective = {"categories": eff.categories, "decision_mode": eff.decision_mode, "uncertain_action": eff.uncertain_action,
                 "ai_mode": eff.ai_mode, "ai_trigger": eff.ai_trigger, "fallback_decision": eff.fallback_decision,
                 "profanity_level": eff.profanity_level}
    return {"policy": effective, "revision": row["policy_revision"], "categories": KNOWN_CATEGORIES,
            "ai_available": ai.configured() and platform["ai_enabled"], "ai_provider": label,
            "ai_monthly_budget_usd": budget}


@router.put("/projects/{project_id}/policy")
async def put_policy(project_id: UUID, body: PolicyBody, request: Request, user: AdminUser = Depends(require_role("admin"))):
    unknown = [c for c in body.categories if c not in KNOWN_CATEGORIES]
    if unknown:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={"error": "unknown_category", "message": f"Bilinmeyen kategori: {', '.join(unknown)}"})
    categories = {}
    for name, rule in body.categories.items():
        if rule.review is not None and rule.block is not None and rule.review >= rule.block:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail={"error": "invalid_thresholds", "message": f"{name}: inceleme eşiği engelleme eşiğinden küçük olmalı."})
        clean = {k: v for k, v in rule.model_dump().items() if v is not None and not (k == "action" and v == "default")}
        if clean:
            categories[name] = clean
    if body.decision_mode == "two_step" and body.fallback_decision == "review":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={"error": "invalid_fallback", "message": "İki adımlı modda yedek karar izin ver veya engelle olmalı."})
    policy = {"categories": categories, "decision_mode": body.decision_mode, "uncertain_action": body.uncertain_action,
              "ai_mode": body.ai_mode, "ai_trigger": round(body.ai_trigger, 3), "fallback_decision": body.fallback_decision,
              "profanity_level": body.profanity_level}
    async with request.app.state.db.acquire() as conn, conn.transaction():
        before = await conn.fetchrow("SELECT policy, policy_revision FROM projects WHERE id = $1 FOR UPDATE", project_id)
        if before is None:
            raise _not_found()
        if (before["policy"] or {}) == policy:
            return {"ok": True, "revision": before["policy_revision"]}
        revision = before["policy_revision"] + 1
        await conn.execute("UPDATE projects SET policy = $2, policy_revision = $3, updated_at = now() WHERE id = $1",
                           project_id, policy, revision)
        await audit(conn, request, user, "policy.project_updated", project_id=project_id, target_type="project",
                    target_id=str(project_id), details={"old": before["policy"], "new": policy, "revision": revision})
    return {"ok": True, "revision": revision}


class ProjectAiBudget(BaseModel):
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=1_000_000)


@router.put("/projects/{project_id}/ai-budget")
async def put_project_ai_budget(project_id: UUID, body: ProjectAiBudget, request: Request,
                                user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        old = await conn.fetchrow("SELECT ai_monthly_budget_usd FROM projects WHERE id = $1 FOR UPDATE", project_id)
        if old is None:
            raise _not_found()
        await conn.execute("UPDATE projects SET ai_monthly_budget_usd = $2 WHERE id = $1", project_id,
                           None if body.monthly_budget_usd is None else round(body.monthly_budget_usd, 4))
        await audit(conn, request, user, "ai.project_budget_updated", project_id=project_id, target_type="project",
                    target_id=str(project_id), details={"old": old["ai_monthly_budget_usd"], "new": body.monthly_budget_usd})
    return {"ok": True}
