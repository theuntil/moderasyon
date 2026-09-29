import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from app import webhooks
from app.admin.auth import AdminUser, audit, current_user, require_role
from fastapi.responses import Response

from app.storage import blocklist_key, storage

router = APIRouter(tags=["moderation"])


# ---------------------------------------------------------------- IP rules

class IpRuleCreate(BaseModel):
    cidr: str = Field(max_length=64, description="Tek IP (1.2.3.4) veya blok (1.2.3.0/24)")
    project_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=300)
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 365)

    @field_validator("cidr")
    @classmethod
    def _valid_network(cls, v: str) -> str:
        try:
            net = ipaddress.ip_network(v.strip(), strict=False)
        except ValueError:
            raise ValueError("Geçerli bir IP adresi veya CIDR bloğu girin (ör. 203.0.113.7 veya 203.0.113.0/24).")
        # Çok geniş blokları engelle: yanlışlıkla tüm interneti banlamak mümkün olmasın
        if (net.version == 4 and net.prefixlen < 8) or (net.version == 6 and net.prefixlen < 32):
            raise ValueError("Bu IP bloğu çok geniş.")
        return str(net)


@router.get("/security/autoban-events")
async def autoban_events(request: Request, _: AdminUser = Depends(require_role("admin"))):
    rows = await request.app.state.db.fetch(
        """
        SELECT id, host(ip) AS ip, rule, count, action, minutes, created_at
        FROM autoban_events ORDER BY id DESC LIMIT 100
        """
    )
    stats = await request.app.state.db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE action = 'banned' AND created_at > now() - interval '24 hours') AS banned_24h,
               count(*) FILTER (WHERE action = 'monitored' AND created_at > now() - interval '24 hours') AS monitored_24h,
               (SELECT count(*) FROM ip_rules WHERE source = 'auto' AND (expires_at IS NULL OR expires_at > now())) AS active_auto
        FROM autoban_events
        """
    )
    return {"events": [dict(r) for r in rows], "stats": dict(stats)}


@router.get("/ip-rules")
async def list_ip_rules(
    request: Request,
    project_id: UUID | None = None,
    scope: Literal["all", "global", "project"] = "all",
    _: AdminUser = Depends(require_role("admin")),
):
    rows = await request.app.state.db.fetch(
        """
        SELECT r.id, r.project_id, p.name AS project_name, r.cidr::text AS cidr, r.reason, r.source,
               r.created_at, r.expires_at, u.username AS created_by_email,
               (r.expires_at IS NOT NULL AND r.expires_at <= now()) AS expired,
               (SELECT count(*) FROM moderation_requests m
                 WHERE m.client_ip <<= r.cidr AND m.created_at >= now() - interval '7 days'
                   AND (r.project_id IS NULL OR m.project_id = r.project_id)) AS requests_7d
        FROM ip_rules r
        LEFT JOIN projects p ON p.id = r.project_id
        LEFT JOIN admin_users u ON u.id = r.created_by
        WHERE ($1::uuid IS NULL OR r.project_id = $1)
          AND ($2 = 'all' OR ($2 = 'global' AND r.project_id IS NULL) OR ($2 = 'project' AND r.project_id IS NOT NULL))
        ORDER BY r.created_at DESC
        """,
        project_id, scope,
    )
    return [dict(r) | {"id": str(r["id"]), "project_id": str(r["project_id"]) if r["project_id"] else None}
            for r in rows]


@router.post("/ip-rules", status_code=status.HTTP_201_CREATED)
async def create_ip_rule(body: IpRuleCreate, request: Request, user: AdminUser = Depends(require_role("admin"))):
    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours) if body.expires_in_hours else None
    )
    async with request.app.state.db.acquire() as conn, conn.transaction():
        if body.project_id and not await conn.fetchval("SELECT 1 FROM projects WHERE id = $1", body.project_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "project_not_found"})
        rule_id = await conn.fetchval(
            """
            INSERT INTO ip_rules (project_id, cidr, reason, created_by, expires_at)
            VALUES ($1, $2::cidr, $3, $4, $5) RETURNING id
            """,
            body.project_id, body.cidr, body.reason, user.id, expires_at,
        )
        net = ipaddress.ip_network(body.cidr)
        if body.project_id is None and net.num_addresses == 1:
            # Tek IP'lik platform banı: hızlı yol (veritabanına gitmeden ret)
            ttl = body.expires_in_hours * 3600 if body.expires_in_hours else None
            await request.app.state.redis.set(f"ban:{net.network_address}", "manual", ex=ttl)
        await audit(conn, request, user, "ip_rule.created", project_id=body.project_id, target_type="ip_rule",
                    target_id=str(rule_id),
                    details={"cidr": body.cidr, "reason": body.reason, "expires_at": expires_at,
                             "scope": "project" if body.project_id else "global"})
    return {"id": str(rule_id)}


@router.delete("/ip-rules/{rule_id}")
async def delete_ip_rule(rule_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        rule = await conn.fetchrow("DELETE FROM ip_rules WHERE id = $1 RETURNING project_id, cidr::text AS cidr", rule_id)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "ip_rule_not_found"})
        net = ipaddress.ip_network(rule["cidr"])
        if net.num_addresses == 1:
            await request.app.state.redis.delete(f"ban:{net.network_address}")
        await audit(conn, request, user, "ip_rule.deleted", project_id=rule["project_id"], target_type="ip_rule",
                    target_id=str(rule_id), details={"cidr": rule["cidr"]})
    return {"ok": True}


# ---------------------------------------------------------------- review queue

class ReviewDecision(BaseModel):
    decision: Literal["allow", "block"]
    note: str | None = Field(default=None, max_length=500)
    add_to_blocklist: bool = False     # görsel/video: bu içeriğin kopyaları da otomatik engellensin


async def _evidence_for(pool, request_ids: list) -> dict:
    if not request_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT id, request_id, kind, timestamp_ms, width, height, categories
        FROM media_evidence WHERE request_id = ANY($1::uuid[]) ORDER BY request_id, timestamp_ms NULLS FIRST, path
        """,
        request_ids,
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["request_id"], []).append({
            "id": str(r["id"]), "kind": r["kind"], "timestamp_ms": r["timestamp_ms"],
            "width": r["width"], "height": r["height"], "categories": r["categories"],
        })
    return out


@router.get("/review")
async def list_review(
    request: Request,
    project_id: UUID | None = None,
    status_filter: Literal["pending", "resolved"] = Query("pending", alias="status"),
    limit: int = Query(50, ge=1, le=200),
    _: AdminUser = Depends(require_role("moderator")),
):
    rows = await request.app.state.db.fetch(
        """
        SELECT q.id, q.status, q.ai_decision, q.human_decision, q.created_at, q.reviewed_at,
               u.username AS reviewed_by_email, r.id AS request_id,
               r.public_id, r.content_type, r.content_text, r.external_user_id, r.external_content_id,
               r.media_duration_ms, r.media_width, r.media_height, r.media_mime,
               p.id AS project_id, p.name AS project_name,
               res.categories, res.max_score, res.reason, res.policy_version
        FROM review_queue q
        JOIN moderation_requests r ON r.id = q.request_id
        JOIN projects p ON p.id = q.project_id
        LEFT JOIN moderation_results res ON res.request_id = r.id
        LEFT JOIN admin_users u ON u.id = q.reviewed_by
        WHERE q.status = $1 AND ($2::uuid IS NULL OR q.project_id = $2)
        ORDER BY CASE WHEN $1 = 'pending' THEN q.created_at END ASC,
                 CASE WHEN $1 = 'resolved' THEN q.reviewed_at END DESC
        LIMIT $3
        """,
        status_filter, project_id, limit,
    )
    evidence = await _evidence_for(request.app.state.db, [r["request_id"] for r in rows])
    return [
        {k: v for k, v in dict(r).items() if k != "request_id"}
        | {"id": str(r["id"]), "project_id": str(r["project_id"]), "evidence": evidence.get(r["request_id"], [])}
        for r in rows
    ]


@router.get("/review/summary")
async def review_summary(request: Request, _: AdminUser = Depends(current_user)):
    row = await request.app.state.db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
               min(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_at
        FROM review_queue
        """
    )
    return dict(row)


@router.post("/review/{review_id}")
async def resolve_review(
    review_id: UUID, body: ReviewDecision, request: Request, user: AdminUser = Depends(require_role("moderator"))
):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        item = await conn.fetchrow(
            """
            SELECT q.id, q.project_id, q.status, q.ai_decision, r.id AS request_id, r.public_id, r.content_type,
                   r.external_user_id, r.external_content_id, r.media_sha256
            FROM review_queue q JOIN moderation_requests r ON r.id = q.request_id
            WHERE q.id = $1 FOR UPDATE OF q
            """,
            review_id,
        )
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "review_not_found"})
        if item["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "already_resolved", "message": "Bu içerik başka bir moderatör tarafından incelendi."},
            )
        await conn.execute(
            """
            UPDATE review_queue SET status = 'resolved', human_decision = $2, reviewed_by = $3, reviewed_at = now()
            WHERE id = $1
            """,
            review_id, body.decision, user.id,
        )

        blocklist_id = None
        if body.add_to_blocklist and body.decision == "block" and item["content_type"] in ("image", "video"):
            ev = await conn.fetch(
                "SELECT path, phash FROM media_evidence WHERE request_id = $1 ORDER BY timestamp_ms NULLS FIRST",
                item["request_id"],
            )
            phashes = [e["phash"] for e in ev if e["phash"] is not None]
            blocklist_id = await conn.fetchval(
                """
                INSERT INTO hash_blocklist (project_id, sha256, phashes, reason, source_public_id, created_by)
                VALUES (NULL, $1, $2, $3, $4, $5) RETURNING id
                """,
                item["media_sha256"], phashes, body.note, item["public_id"], user.id,
            )
            if ev and storage.client is not None:
                preview = blocklist_key(blocklist_id)
                try:
                    await storage.copy(ev[0]["path"], preview)
                    await conn.execute("UPDATE hash_blocklist SET preview_path = $2 WHERE id = $1", blocklist_id, preview)
                except Exception:  # noqa: BLE001 — önizleme olmadan da engel listesi çalışır
                    pass

        await audit(conn, request, user, f"review.{body.decision}", project_id=item["project_id"],
                    target_type="moderation_request", target_id=item["public_id"],
                    details={"ai_decision": item["ai_decision"], "human_decision": body.decision, "note": body.note,
                             "blocklisted": blocklist_id is not None})
        delivery = await webhooks.record_event(
            conn, item["project_id"], "moderation.reviewed",
            webhooks.result_payload(item["public_id"], "completed", body.decision, "human_reviewed",
                                    item["content_type"], item["external_user_id"], item["external_content_id"]),
        )
    await webhooks.enqueue(request.app.state.arq, delivery)
    return {"ok": True, "blocklisted": blocklist_id is not None}


# ---------------------------------------------------------------- medya (kanıt görselleri)

@router.get("/media/evidence/{evidence_id}")
async def get_evidence(evidence_id: UUID, request: Request, _: AdminUser = Depends(require_role("moderator"))):
    row = await request.app.state.db.fetchrow(
        """
        SELECT e.path, coalesce(res.labels, '{}') AS labels FROM media_evidence e
        LEFT JOIN moderation_results res ON res.request_id = e.request_id WHERE e.id = $1
        """,
        evidence_id,
    )
    if row and set(row["labels"]) & RESTRICTED_LABELS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "restricted_content"})
    return await _serve(row["path"] if row else None)


@router.get("/media/blocklist/{item_id}")
async def get_blocklist_preview(item_id: UUID, request: Request, _: AdminUser = Depends(require_role("moderator"))):
    key = await request.app.state.db.fetchval("SELECT preview_path FROM hash_blocklist WHERE id = $1", item_id)
    return await _serve(key)


async def _serve(key: str | None) -> Response:
    """Kanıt kareleri R2'de (bucket herkese kapalı); yetkili moderatöre panel üzerinden aktarılır."""
    if not key or storage.client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "media_not_found"})
    try:
        data = await storage.get_bytes(key, 5 * 1024 * 1024)
    except Exception:  # noqa: BLE001 — silinmiş veya erişilemeyen nesne
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "media_not_found"})
    # Sadece bizim ürettiğimiz JPEG'ler sunulur; tarayıcı içeriği başka türde yorumlamasın
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
    })


# ---------------------------------------------------------------- görsel engel listesi

@router.get("/blocklist")
async def list_blocklist(request: Request, _: AdminUser = Depends(require_role("moderator"))):
    rows = await request.app.state.db.fetch(
        """
        SELECT b.id, b.project_id, p.name AS project_name, b.sha256 IS NOT NULL AS has_sha,
               cardinality(b.phashes) AS hash_count, b.reason, b.source_public_id,
               b.preview_path IS NOT NULL AS has_preview, u.username AS created_by, b.created_at
        FROM hash_blocklist b
        LEFT JOIN projects p ON p.id = b.project_id
        LEFT JOIN admin_users u ON u.id = b.created_by
        ORDER BY b.created_at DESC LIMIT 500
        """
    )
    return [dict(r) | {"id": str(r["id"]), "project_id": str(r["project_id"]) if r["project_id"] else None} for r in rows]


@router.delete("/blocklist/{item_id}")
async def delete_blocklist(item_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "DELETE FROM hash_blocklist WHERE id = $1 RETURNING project_id, preview_path, source_public_id", item_id
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "blocklist_item_not_found"})
        await audit(conn, request, user, "blocklist.deleted", project_id=row["project_id"], target_type="blocklist",
                    target_id=str(item_id), details={"source": row["source_public_id"]})
    if row["preview_path"] and storage.client is not None:
        await storage.delete_keys([row["preview_path"]])
    return {"ok": True}


# ---------------------------------------------------------------- decisions

@router.get("/decisions")
async def list_decisions(
    request: Request,
    project_id: UUID | None = None,
    decision: Literal["allow", "review", "block"] | None = None,
    request_status: Literal["queued", "processing", "completed", "failed"] | None = Query(None, alias="status"),
    content_type: Literal["text", "image", "video"] | None = Query(None, alias="type"),
    q: str | None = Query(None, max_length=128, description="İstek ID, içerik ID veya kullanıcı ID"),
    label: str | None = Query(None, max_length=64, description="Etiket (ör. terör:pkk, küfür)"),
    critical: bool = False,
    before: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    _: AdminUser = Depends(current_user),
):
    rows = await request.app.state.db.fetch(
        """
        SELECT r.public_id, r.status, r.content_type, r.created_at, r.completed_at,
               r.external_user_id, r.external_content_id, host(r.client_ip) AS client_ip,
               left(r.content_text, 160) AS preview, r.media_duration_ms,
               p.id AS project_id, p.name AS project_name,
               res.decision AS ai_decision, q.human_decision,
               COALESCE(q.human_decision, res.decision) AS final_decision,
               res.max_score, res.categories, res.labels, res.severity, r.legal_hold,
               round(extract(epoch FROM r.completed_at - r.created_at) * 1000)::bigint AS latency_ms
        FROM moderation_requests r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN moderation_results res ON res.request_id = r.id
        LEFT JOIN review_queue q ON q.request_id = r.id
        WHERE ($1::uuid IS NULL OR r.project_id = $1)
          AND ($2::text IS NULL OR res.decision = $2)
          AND ($3::text IS NULL OR r.status = $3)
          AND ($4::text IS NULL OR r.public_id = $4 OR r.external_content_id = $4 OR r.external_user_id = $4)
          AND ($5::timestamptz IS NULL OR r.created_at < $5)
          AND ($7::text IS NULL OR r.content_type = $7)
          AND ($8::text IS NULL OR $8 = ANY(res.labels))
          AND (NOT $9 OR res.severity = 'critical')
        ORDER BY r.created_at DESC
        LIMIT $6
        """,
        project_id, decision, request_status, q.strip() if q else None, before, limit, content_type,
        label.strip() if label else None, critical,
    )
    items = [dict(r) | {"project_id": str(r["project_id"])} for r in rows]
    return {"items": items, "next_before": items[-1]["created_at"] if len(items) == limit else None}


def _user_info(enc: str | None, user: AdminUser) -> dict | None:
    """Kullanıcı kişisel bilgileri şifreli; sadece admin ve owner rolüne açılır."""
    if not enc or user.role not in ("owner", "admin"):
        return None
    import json

    from app.admin.auth import decrypt_secret
    try:
        return json.loads(decrypt_secret(enc))
    except Exception:  # noqa: BLE001
        return None


RESTRICTED_LABELS = {"çocuk_istismarı"}   # kanıtı panelde gösterilmez; yetkililere bildirilmeli


@router.get("/decisions/{public_id}")
async def get_decision(public_id: str, request: Request, user: AdminUser = Depends(current_user)):
    row = await request.app.state.db.fetchrow(
        """
        SELECT r.public_id, r.status, r.content_type, r.content_text, r.content_url, r.metadata,
               r.external_user_id, r.external_content_id, host(r.client_ip) AS client_ip,
               r.idempotency_key, r.attempts, r.error, r.created_at, r.started_at, r.completed_at,
               r.id AS request_id, r.source, r.media_mime, r.media_bytes, r.media_width, r.media_height,
               r.media_duration_ms, r.media_frames, r.content_purged_at,
               p.id AS project_id, p.name AS project_name, k.name AS api_key_name, k.key_prefix,
               res.decision AS ai_decision, res.categories, res.max_score, res.reason, res.providers,
               res.policy_version, res.processing_time_ms, res.layer1_decision, res.ai_used, res.ai_latency_ms,
               res.labels, res.severity, r.legal_hold, r.held_at, r.hold_media_key IS NOT NULL AS has_original,
               r.user_info_enc,
               q.id AS review_id, q.status AS review_status, q.human_decision, q.reviewed_at,
               u.username AS reviewed_by_email,
               round(extract(epoch FROM r.completed_at - r.created_at) * 1000)::bigint AS latency_ms
        FROM moderation_requests r
        JOIN projects p ON p.id = r.project_id
        JOIN project_api_keys k ON k.id = r.api_key_id
        LEFT JOIN moderation_results res ON res.request_id = r.id
        LEFT JOIN review_queue q ON q.request_id = r.id
        LEFT JOIN admin_users u ON u.id = q.reviewed_by
        WHERE r.public_id = $1
        """,
        public_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "decision_not_found"})
    evidence = await _evidence_for(request.app.state.db, [row["request_id"]])
    restricted = bool(set(row["labels"] or []) & RESTRICTED_LABELS)
    return {k: v for k, v in dict(row).items() if k not in ("request_id", "user_info_enc")} | {
        "project_id": str(row["project_id"]),
        "user_info": _user_info(row["user_info_enc"], user),
        "evidence_restricted": restricted,
        "review_id": str(row["review_id"]) if row["review_id"] else None,
        "evidence": [] if restricted else evidence.get(row["request_id"], []),
    }
