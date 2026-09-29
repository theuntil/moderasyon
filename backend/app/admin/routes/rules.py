"""Kurallar ve yasal saklama: yasaklı kelimeler, yasaklı görsel/semboller, kritik içerik arşivi."""
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.admin.auth import AdminUser, audit, current_user, require_role
from app.moderation.rules import normalize_term
from app.purge import purge_requests
from app.storage import storage

router = APIRouter(tags=["rules"])

LABEL_PATTERN = r"^[\wçğıöşüÇĞİÖŞÜ:_.\- ]{1,48}$"


# ---------------------------------------------------------------- yasaklı kelimeler

class TermBody(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    project_id: UUID | None = None
    match_mode: Literal["word", "contains"] = "word"
    label: str = Field(pattern=LABEL_PATTERN)
    severity: Literal["block", "critical"] = "block"


@router.get("/rules/terms")
async def list_terms(request: Request, project_id: UUID | None = None, _: AdminUser = Depends(current_user)):
    rows = await request.app.state.db.fetch(
        """
        SELECT t.id, t.project_id, p.name AS project_name, t.term, t.match_mode, t.label, t.severity, t.created_at,
               u.username AS created_by
        FROM custom_terms t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN admin_users u ON u.id = t.created_by
        WHERE ($1::uuid IS NULL OR t.project_id IS NULL OR t.project_id = $1)
        ORDER BY t.label, t.term
        """,
        project_id,
    )
    return [dict(r) | {"id": str(r["id"]), "project_id": str(r["project_id"]) if r["project_id"] else None} for r in rows]


@router.post("/rules/terms", status_code=status.HTTP_201_CREATED)
async def create_term(body: TermBody, request: Request, user: AdminUser = Depends(require_role("admin"))):
    normalized = normalize_term(body.term)
    if not normalized:
        raise HTTPException(status_code=422, detail={"error": "invalid_term", "message": "Kelime harf veya rakam içermeli."})
    async with request.app.state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO custom_terms (project_id, term, normalized, match_mode, label, severity, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING RETURNING id
            """,
            body.project_id, body.term.strip(), normalized, body.match_mode, body.label.strip().lower(), body.severity, user.id,
        )
        if row is None:
            raise HTTPException(status_code=409, detail={"error": "term_exists", "message": "Bu kelime zaten listede."})
        await audit(conn, request, user, "rules.term_added", project_id=body.project_id, target_type="custom_term",
                    target_id=str(row["id"]), details={"term": body.term, "label": body.label, "severity": body.severity})
    return {"id": str(row["id"])}


@router.delete("/rules/terms/{term_id}")
async def delete_term(term_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("DELETE FROM custom_terms WHERE id = $1 RETURNING term, project_id", term_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        await audit(conn, request, user, "rules.term_deleted", project_id=row["project_id"], target_type="custom_term",
                    target_id=str(term_id), details={"term": row["term"]})
    return {"ok": True}


# ---------------------------------------------------------------- yasaklı görsel / semboller

class VisualBody(BaseModel):
    label: str = Field(pattern=LABEL_PATTERN)
    description: str = Field(min_length=10, max_length=600)
    severity: Literal["block", "critical"] = "critical"


class VisualPatch(BaseModel):
    label: str | None = Field(default=None, pattern=LABEL_PATTERN)
    description: str | None = Field(default=None, min_length=10, max_length=600)
    severity: Literal["block", "critical"] | None = None
    enabled: bool | None = None


@router.get("/rules/visual")
async def list_visual(request: Request, _: AdminUser = Depends(current_user)):
    rows = await request.app.state.db.fetch(
        """
        SELECT v.id, v.label, v.description, v.severity, v.enabled, v.created_at, u.username AS created_by
        FROM visual_rules v LEFT JOIN admin_users u ON u.id = v.created_by ORDER BY v.created_at
        """
    )
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.post("/rules/visual", status_code=status.HTTP_201_CREATED)
async def create_visual(body: VisualBody, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        rid = await conn.fetchval(
            "INSERT INTO visual_rules (label, description, severity, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
            body.label.strip().lower(), body.description.strip(), body.severity, user.id,
        )
        await audit(conn, request, user, "rules.visual_added", target_type="visual_rule", target_id=str(rid),
                    details=body.model_dump())
    return {"id": str(rid)}


@router.patch("/rules/visual/{rule_id}")
async def update_visual(rule_id: UUID, body: VisualPatch, request: Request, user: AdminUser = Depends(require_role("admin"))):
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        return {"ok": True}
    cols = list(changes)
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
    async with request.app.state.db.acquire() as conn, conn.transaction():
        done = await conn.fetchval(f"UPDATE visual_rules SET {sets} WHERE id = $1 RETURNING id", rule_id,
                                   *[changes[c] for c in cols])
        if done is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        await audit(conn, request, user, "rules.visual_updated", target_type="visual_rule", target_id=str(rule_id),
                    details=changes)
    return {"ok": True}


@router.delete("/rules/visual/{rule_id}")
async def delete_visual(rule_id: UUID, request: Request, user: AdminUser = Depends(require_role("admin"))):
    async with request.app.state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("DELETE FROM visual_rules WHERE id = $1 RETURNING label", rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        await audit(conn, request, user, "rules.visual_deleted", target_type="visual_rule", target_id=str(rule_id),
                    details={"label": row["label"]})
    return {"ok": True}


# ---------------------------------------------------------------- yasal saklama

@router.get("/legal-holds")
async def list_holds(request: Request, label: str | None = Query(None, max_length=64),
                     limit: int = Query(100, ge=1, le=500), user: AdminUser = Depends(require_role("moderator"))):
    from app.admin.routes.moderation import _user_info
    rows = await request.app.state.db.fetch(
        """
        SELECT r.public_id, r.content_type, r.source, left(r.content_text, 200) AS preview, r.held_at, r.created_at,
               host(r.client_ip) AS client_ip, r.external_user_id, r.external_content_id, r.user_info_enc,
               r.hold_media_key IS NOT NULL AS has_original, r.media_mime, r.media_bytes,
               p.id AS project_id, p.name AS project_name, res.labels, res.severity, res.decision
        FROM moderation_requests r JOIN projects p ON p.id = r.project_id
        LEFT JOIN moderation_results res ON res.request_id = r.id
        WHERE r.legal_hold AND ($1::text IS NULL OR $1 = ANY(res.labels))
        ORDER BY r.held_at DESC LIMIT $2
        """,
        label, limit,
    )
    stats = await request.app.state.db.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE held_at > now() - interval '7 days') AS last_7d "
        "FROM moderation_requests WHERE legal_hold"
    )
    items = [{k: v for k, v in dict(r).items() if k != "user_info_enc"} | {
        "project_id": str(r["project_id"]), "user_info": _user_info(r["user_info_enc"], user)} for r in rows]
    return {"items": items, "stats": dict(stats)}


@router.get("/legal-holds/{public_id}/original")
async def download_original(public_id: str, request: Request, user: AdminUser = Depends(require_role("admin"))):
    from app.admin.routes.moderation import RESTRICTED_LABELS
    row = await request.app.state.db.fetchrow(
        """
        SELECT r.hold_media_key, r.media_mime, coalesce(res.labels, '{}') AS labels FROM moderation_requests r
        LEFT JOIN moderation_results res ON res.request_id = r.id WHERE r.public_id = $1 AND r.legal_hold
        """,
        public_id,
    )
    if row is None or not row["hold_media_key"] or storage.client is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if set(row["labels"]) & RESTRICTED_LABELS:
        raise HTTPException(status_code=403, detail={"error": "restricted_content",
                                                     "message": "Bu içerik panelden indirilemez; yetkililere bildirin."})
    data = await storage.get_bytes(row["hold_media_key"], 250 * 1024 * 1024)
    async with request.app.state.db.acquire() as conn:
        await audit(conn, request, user, "legal_hold.original_downloaded", target_type="request", target_id=public_id)
    name = f"{public_id}.{row['hold_media_key'].rsplit('.', 1)[-1]}"
    return Response(content=data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})


class HoldDelete(BaseModel):
    confirm_id: str = Field(description="Silmeyi onaylamak için içeriğin kimliği (mod_...)")


@router.delete("/legal-holds/{public_id}")
async def delete_hold(public_id: str, body: HoldDelete, request: Request, user: AdminUser = Depends(require_role("admin"))):
    """Kalıcı silme: orijinal medya, kanıt kareleri, metin, IP ve kullanıcı bilgileri silinir. Geri alınamaz."""
    if body.confirm_id != public_id:
        raise HTTPException(status_code=422, detail={"error": "confirmation_mismatch"})
    async with request.app.state.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, project_id, hold_media_key FROM moderation_requests WHERE public_id = $1 AND legal_hold FOR UPDATE",
            public_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        keys = await purge_requests(conn, [row["id"]], forget_user=True)
        if row["hold_media_key"]:
            keys.append(row["hold_media_key"])
        await conn.execute(
            "UPDATE moderation_requests SET legal_hold = false, hold_media_key = NULL WHERE id = $1", row["id"])
        await audit(conn, request, user, "legal_hold.deleted", project_id=row["project_id"], target_type="request",
                    target_id=public_id, details={"objects": len(keys)})
    if keys and storage.client is not None:
        await storage.delete_keys(keys)
    return {"ok": True, "deleted_objects": len(keys)}


__all__ = ["router", "json"]
