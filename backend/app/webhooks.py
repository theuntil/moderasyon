"""Webhook'lar: karar hazır olduğunda (ve insan incelemesi bittiğinde) uygulamaya bildirim.

- Olay önce veritabanına yazılır (outbox), sonra gönderilir. Kuyruk kaybolsa bile dakikalık cron
  bekleyen teslimatları tekrar dener; hiçbir olay sessizce kaybolmaz.
- İmza: X-Moderation-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<gövde>")>
  Alıcı zaman damgasını kontrol ederek tekrar oynatma saldırılarını reddetmelidir (ör. ±5 dakika).
- 2xx dışındaki yanıtlarda artan aralıklarla 8 deneme (~1 gün). Güvensiz adres (iç ağ) hemen başarısız.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.config import settings
from app.netsafe import FetchError, UnsafeURL, post_json

log = logging.getLogger("webhooks")

BACKOFF_S = [30, 120, 600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600]
EVENTS = ("moderation.completed", "moderation.reviewed", "moderation.failed")


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    """Alıcı tarafta kullanılabilecek referans doğrulama."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = sign(secret, ts, body).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


async def record_event(conn, project_id: UUID, event: str, data: dict) -> UUID | None:
    """Proje webhook'u etkinse teslimat kaydı oluşturur (aynı transaction içinde çağrılmalı)."""
    enabled = await conn.fetchval(
        "SELECT webhook_enabled AND webhook_url IS NOT NULL FROM projects WHERE id = $1", project_id
    )
    if not enabled:
        return None
    payload = {
        "id": "evt_" + secrets.token_hex(12),
        "event": event,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return await conn.fetchval(
        """
        INSERT INTO webhook_deliveries (project_id, event, request_public_id, payload)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        project_id, event, data.get("id"), payload,
    )


async def enqueue(arq, delivery_id: UUID | None) -> None:
    if delivery_id is None or arq is None:
        return
    try:
        await arq.enqueue_job("deliver_webhook", str(delivery_id), _job_id=f"wh:{delivery_id}:0", _queue_name="arq:queue")
    except Exception:  # noqa: BLE001 — cron tekrar dener
        log.warning("webhook enqueue failed for %s", delivery_id)


async def deliver(pool, delivery_id: str) -> None:
    row = await pool.fetchrow(
        """
        SELECT d.id, d.payload, d.event, d.attempts, d.status, p.webhook_url, p.webhook_secret, p.webhook_enabled
        FROM webhook_deliveries d JOIN projects p ON p.id = d.project_id
        WHERE d.id = $1
        """,
        UUID(delivery_id),
    )
    if row is None or row["status"] != "pending":
        return
    if not row["webhook_enabled"] or not row["webhook_url"]:
        await pool.execute(
            "UPDATE webhook_deliveries SET status = 'failed', last_error = 'webhook_disabled' WHERE id = $1", row["id"]
        )
        return

    body = json.dumps(row["payload"], separators=(",", ":"), ensure_ascii=False).encode()
    ts = int(time.time())
    headers = {
        "X-Moderation-Event": row["event"],
        "X-Moderation-Delivery": str(row["id"]),
        "X-Moderation-Signature": sign(row["webhook_secret"], ts, body),
    }
    status_code, error, permanent = None, None, False
    try:
        status_code = await post_json(row["webhook_url"], body, headers, timeout_s=settings.webhook_timeout_s)
    except UnsafeURL as exc:
        error, permanent = f"unsafe_url:{exc}", True
    except FetchError as exc:
        error = str(exc)

    attempts = row["attempts"] + 1
    if status_code is not None and 200 <= status_code < 300:
        await pool.execute(
            """
            UPDATE webhook_deliveries SET status = 'delivered', attempts = $2, last_status_code = $3,
                   last_error = NULL, delivered_at = now() WHERE id = $1
            """,
            row["id"], attempts, status_code,
        )
        return

    if status_code is not None:
        error = f"http_{status_code}"
    if permanent or attempts > len(BACKOFF_S):
        await pool.execute(
            """
            UPDATE webhook_deliveries SET status = 'failed', attempts = $2, last_status_code = $3, last_error = $4
            WHERE id = $1
            """,
            row["id"], attempts, status_code, error,
        )
        return
    next_at = datetime.now(timezone.utc) + timedelta(seconds=BACKOFF_S[attempts - 1])
    await pool.execute(
        """
        UPDATE webhook_deliveries SET attempts = $2, last_status_code = $3, last_error = $4, next_attempt_at = $5
        WHERE id = $1
        """,
        row["id"], attempts, status_code, error, next_at,
    )


def result_payload(public_id: str, status: str, decision: str | None, reason: str | None,
                   content_type: str, user_id: str | None, content_id: str | None,
                   labels: list[str] | None = None, severity: str | None = None) -> dict:
    return {
        "id": public_id, "status": status, "type": content_type, "decision": decision, "reason": reason,
        "user_id": user_id, "content_id": content_id, "labels": labels or [], "severity": severity or "normal",
    }
