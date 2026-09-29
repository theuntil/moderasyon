from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.admin.auth import AdminUser, current_user
from app.config import settings

router = APIRouter(prefix="/stats", tags=["stats"])

Range = Literal["24h", "7d", "30d", "90d"]
_RANGES: dict[str, tuple[timedelta, str]] = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "day"),
    "30d": (timedelta(days=30), "day"),
    "90d": (timedelta(days=90), "day"),
}


@router.get("/overview")
async def overview(
    request: Request,
    range: Range = Query("7d"),
    project_id: UUID | None = None,
    _: AdminUser = Depends(current_user),
):
    """Dashboard ve project sayfası için tüm istatistikler tek istekte."""
    pool = request.app.state.db
    span, bucket = _RANGES[range]
    tz = settings.panel_timezone

    # $1 = zaman aralığı, $2 = project filtresi (NULL ise tümü)
    where = "r.created_at >= now() - $1::interval AND ($2::uuid IS NULL OR r.project_id = $2)"

    totals = await pool.fetchrow(
        f"""
        SELECT count(*)                                              AS requests,
               count(*) FILTER (WHERE res.decision = 'allow')        AS allow,
               count(*) FILTER (WHERE res.decision = 'review')       AS review,
               count(*) FILTER (WHERE res.decision = 'block')        AS block,
               count(*) FILTER (WHERE r.status = 'failed')           AS failed,
               count(*) FILTER (WHERE r.status IN ('queued', 'processing')) AS in_progress,
               count(*) FILTER (WHERE r.content_type = 'text')       AS text,
               count(*) FILTER (WHERE r.content_type = 'image')      AS image,
               count(*) FILTER (WHERE r.content_type = 'video')      AS video
        FROM moderation_requests r
        LEFT JOIN moderation_results res ON res.request_id = r.id
        WHERE {where}
        """,
        span, project_id,
    )

    latency = await pool.fetchrow(
        f"""
        SELECT round(avg(ms))::bigint AS avg,
               round(percentile_cont(0.50) WITHIN GROUP (ORDER BY ms))::bigint AS p50,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY ms))::bigint AS p95,
               round(percentile_cont(0.99) WITHIN GROUP (ORDER BY ms))::bigint AS p99
        FROM (
            SELECT extract(epoch FROM r.completed_at - r.created_at) * 1000 AS ms
            FROM moderation_requests r
            WHERE {where} AND r.status = 'completed'
        ) t
        """,
        span, project_id,
    )

    # Boş saat/günler de 0 olarak görünsün diye generate_series ile doldurulur
    series = await pool.fetch(
        f"""
        WITH buckets AS (
            SELECT generate_series(
                date_trunc($3, now() - $1::interval, $4),
                date_trunc($3, now(), $4),
                ('1 ' || $3)::interval,
                $4
            ) AS t
        ),
        agg AS (
            SELECT date_trunc($3, r.created_at, $4) AS t,
                   count(*) AS total,
                   count(*) FILTER (WHERE res.decision = 'allow')  AS allow,
                   count(*) FILTER (WHERE res.decision = 'review') AS review,
                   count(*) FILTER (WHERE res.decision = 'block')  AS block,
                   count(*) FILTER (WHERE r.status = 'failed')     AS failed,
                   round(avg(extract(epoch FROM r.completed_at - r.created_at) * 1000))::bigint AS latency
            FROM moderation_requests r
            LEFT JOIN moderation_results res ON res.request_id = r.id
            WHERE {where}
            GROUP BY 1
        )
        SELECT b.t, coalesce(a.total, 0) AS total, coalesce(a.allow, 0) AS allow,
               coalesce(a.review, 0) AS review, coalesce(a.block, 0) AS block,
               coalesce(a.failed, 0) AS failed, a.latency
        FROM buckets b LEFT JOIN agg a ON a.t = b.t
        ORDER BY b.t
        """,
        span, project_id, bucket, tz,
    )

    categories = await pool.fetch(
        f"""
        SELECT c->>'name' AS name, count(*) AS count
        FROM moderation_requests r
        JOIN moderation_results res ON res.request_id = r.id,
             jsonb_array_elements(res.categories) c
        WHERE {where}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """,
        span, project_id,
    )

    projects = await pool.fetch(
        f"""
        SELECT p.id, p.name, p.slug,
               count(r.id) AS requests,
               count(*) FILTER (WHERE res.decision = 'block')  AS block,
               count(*) FILTER (WHERE res.decision = 'review') AS review
        FROM moderation_requests r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN moderation_results res ON res.request_id = r.id
        WHERE {where}
        GROUP BY p.id ORDER BY requests DESC LIMIT 10
        """,
        span, project_id,
    )

    live = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE created_at >= now() - interval '1 minute') AS last_minute,
               count(*) FILTER (WHERE created_at >= date_trunc('day', now(), $2))  AS today
        FROM moderation_requests
        WHERE created_at >= now() - interval '1 day' AND ($1::uuid IS NULL OR project_id = $1)
        """,
        project_id, tz,
    )

    pending_reviews = await pool.fetchval(
        "SELECT count(*) FROM review_queue WHERE status = 'pending' AND ($1::uuid IS NULL OR project_id = $1)",
        project_id,
    )
    human_overrides = await pool.fetchval(
        """
        SELECT count(*) FROM review_queue q
        WHERE q.status = 'resolved' AND q.reviewed_at >= now() - $1::interval
          AND ($2::uuid IS NULL OR q.project_id = $2)
        """,
        span, project_id,
    )

    return {
        "range": range,
        "bucket": bucket,
        "timezone": tz,
        "totals": dict(totals),
        "latency": dict(latency),
        "series": [dict(r) | {"t": r["t"].isoformat()} for r in series],
        "categories": [dict(r) for r in categories],
        "projects": [dict(r) | {"id": str(r["id"])} for r in projects],
        "live": dict(live),
        "pending_reviews": pending_reviews,
        "reviews_resolved": human_overrides,
    }


@router.get("/quality")
async def quality(
    request: Request,
    range: Range = Query("30d"),
    project_id: UUID | None = None,
    _: AdminUser = Depends(current_user),
):
    """İnsan kararlarıyla otomatik sistemin karşılaştırması: eşik ayarı için gerçek veri (madde 46)."""
    pool = request.app.state.db
    span, _bucket = _RANGES[range]
    by_category = await pool.fetch(
        """
        WITH reviewed AS (
            SELECT q.human_decision, res.categories
            FROM review_queue q JOIN moderation_results res ON res.request_id = q.request_id
            WHERE q.status = 'resolved' AND q.human_decision IS NOT NULL
              AND q.reviewed_at >= now() - $1::interval AND ($2::uuid IS NULL OR q.project_id = $2)
        ),
        top AS (
            SELECT human_decision,
                   (SELECT c->>'name' FROM jsonb_array_elements(categories) c
                    ORDER BY (c->>'score')::float DESC LIMIT 1) AS category
            FROM reviewed
        )
        SELECT coalesce(category, 'none') AS category, count(*) AS reviewed,
               count(*) FILTER (WHERE human_decision = 'block') AS human_block,
               count(*) FILTER (WHERE human_decision = 'allow') AS human_allow
        FROM top GROUP BY 1 ORDER BY 2 DESC
        """,
        span, project_id,
    )
    ai_row = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE ai_used) AS ai_calls,
               round(avg(ai_latency_ms) FILTER (WHERE ai_used))::bigint AS ai_avg_ms,
               count(*) FILTER (WHERE ai_used AND layer1_decision = 'review' AND decision <> 'review') AS ai_resolved,
               count(*) FILTER (WHERE layer1_decision = 'review') AS layer1_review
        FROM moderation_results
        WHERE created_at >= now() - $1::interval AND ($2::uuid IS NULL OR project_id = $2)
        """,
        span, project_id,
    )
    return {"range": range, "categories": [dict(r) for r in by_category], "ai": dict(ai_row)}
