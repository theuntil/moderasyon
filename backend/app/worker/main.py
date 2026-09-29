"""Arka plan işçileri.

İki ayrı kuyruk ve iki ayrı worker süreci vardır:
- arq:queue  (WorkerSettings)      → metin moderasyonu, webhook teslimatı, bakım cron'ları. Hızlı işler.
- arq:media  (MediaWorkerSettings) → görsel/video. Ağır ve yavaş; ayrı süreçte, sınırlı eşzamanlılıkla
  çalışır ki uzun bir video metin moderasyonunu bekletmesin.
"""
import logging
import time
from pathlib import Path
from uuid import UUID

from arq import Retry
from arq.connections import RedisSettings
from arq.cron import cron

from app import webhooks
from app.config import settings
from app.logsetup import setup as _setup_logging

_setup_logging()
from app.db import create_pool
from app.media import detectors
from app.media.files import InvalidMedia, encode_evidence
from app.media.pipeline import MediaAnalysis, analyze_file
from app.media.pipeline import _distinct_frames as pipeline_distinct
from app.media.sniff import sniff
from app.moderation import ai, ai_gateway
from app.moderation import rules as rules_mod
from app.moderation.policy import EMPTY_POLICY, ProjectPolicy, evaluate, resolve_mode
from app.moderation.text_pipeline import DetectionResult, run_text_pipeline
from app.netsafe import FetchError, UnsafeURL, download
from app.platform_settings import SettingsCache
from app.storage import (
    HINTS, StorageError, StorageNotConfigured, error_code, evidence_key, incoming_key, is_permanent, storage,
)

log = logging.getLogger("worker")

MAX_TRIES = 5
TEXT_QUEUE = "arq:queue"
MEDIA_QUEUE = "arq:media"
MAX_EVIDENCE_FRAMES = 6
ORPHAN_AGE_S = 3600          # imzalı adresle yüklenip moderasyona gönderilmeyen nesneler bu süre sonra silinir

_CLAIM = """
    UPDATE moderation_requests
    SET status = 'processing', started_at = now(), attempts = attempts + 1
    WHERE id = $1 AND status = 'queued'
    RETURNING id, public_id, project_id, content_type, content_text, content_url, source, media_key,
              external_user_id, external_content_id, created_at, attempts
"""


# ---------------------------------------------------------------- ortak

async def _fail(ctx: dict, req, error: str) -> None:
    pool = ctx["db"]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE moderation_requests SET status = 'failed', error = $2, completed_at = now() WHERE id = $1",
            req["id"], error[:100],
        )
        delivery = await webhooks.record_event(
            conn, req["project_id"], "moderation.failed",
            webhooks.result_payload(req["public_id"], "failed", None, error, req["content_type"],
                                    req["external_user_id"], req["external_content_id"]),
        )
    await webhooks.enqueue(ctx["redis"], delivery)


async def _project_policy(pool, project_id) -> ProjectPolicy:
    prow = await pool.fetchrow("SELECT policy, policy_revision FROM projects WHERE id = $1", project_id)
    return ProjectPolicy.from_row(prow["policy"], prow["policy_revision"]) if prow else EMPTY_POLICY


def _hold_key(public_id: str, created_at, name: str) -> str:
    return f"hold/{created_at.strftime('%Y/%m')}/{public_id}/{name}"


async def _finalize(ctx: dict, req, detection: DetectionResult, elapsed_ms: int,
                    media: MediaAnalysis | None = None, mime: str | None = None, size: int | None = None) -> bool:
    """Kararı verir ve kaydeder. Yasal saklamaya alındıysa True döner (medya işi orijinali saklar)."""
    pool = ctx["db"]
    platform = await ctx["settings_cache"].get(pool)
    policy = await _project_policy(pool, req["project_id"])
    rules = await ctx.setdefault("rules", rules_mod.RulesCache()).get(pool)

    # Yasaklı kelimeler: metinde ve görsel/videodaki yazıda (OCR)
    rules_mod.apply_terms(detection, (media.text if media else req["content_text"]), rules, req["project_id"])

    # Yasaklı görsel/semboller: AI görsel kontrolü (panelde tanımlı kurallar)
    if media is not None and platform.visual_rules_enabled and rules.visual:
        scores = {c.name: c.score for c in detection.categories}
        if ai.may_send_images(scores, allow_sensitive=platform.ai_sensitive_media, block_threshold=platform.threshold_block):
            frames = [a.frame.image for a in pipeline_distinct(media.frames, 4)]
            vr = await ai_gateway.classify(
                pool=pool, redis=ctx["redis"], s=platform, project_id=req["project_id"], request_id=req["id"],
                text=None, images=frames, task="visual", visual_rules=rules.visual,
            )
            for rule in rules.visual:
                if (vr.scores or {}).get(rule["id"], 0.0) >= 0.7:
                    detection.add("forbidden_visual", 1.0)
                    detection.labels.add(rule["label"])
                    if rule["severity"] == "critical":
                        detection.critical = True

    layer1 = evaluate(detection, platform, policy)

    # Layer 2: "smart" modda risk skoru ai_trigger'ı geçince (varsayılan 0.50), "always" modda her içerik.
    # Yerel modelin kesin engellediği içerik gönderilmez. Bütçe/limit/devre kesici ai_gateway'de;
    # AI yanıt veremezse Layer 1 kararı ve projenin belirsiz içerik kuralı geçerli.
    ai_used, ai_ms = False, None
    if policy.wants_ai(layer1):
        images = [a.frame.image for a in sorted(media.frames, key=lambda a: a.top, reverse=True)[:4]] if media else []
        if images and not ai.may_send_images({c.name: c.score for c in detection.categories},
                                             allow_sensitive=platform.ai_sensitive_media,
                                             block_threshold=platform.threshold_block):
            images = []   # kesin çıplaklık dış sağlayıcıya gönderilmez (zaten engellenir)
        if not media or images or (media and media.text):
            result = await ai_gateway.classify(
                pool=pool, redis=ctx["redis"], s=platform, project_id=req["project_id"], request_id=req["id"],
                # Görsel/videoda OCR ile okunan metin de gönderilir (yazıdaki hakareti AI metinden değerlendirir)
                text=(media.text or None) if media else req["content_text"], images=images,
            )
            if result.scores is not None:
                ai_used, ai_ms = True, result.latency_ms
                # AI belirsiz bölgede hakemdir; "always" modda yerel sonucun üstüne sadece ekler
                judge = policy.ai_mode == "smart" or layer1.decision == "review"
                detection = ai.merge(detection, result.scores, judge=judge, model=result.model,
                                     evaluated=result.evaluated)

    decision = resolve_mode(evaluate(detection, platform, policy) if ai_used else layer1, policy)
    categories = [{"name": c.name, "score": round(c.score, 4)} for c in detection.categories]
    labels = rules_mod.final_labels(detection, decision.decision, list(decision.flagged))
    critical = rules_mod.is_critical(detection, decision.decision, list(decision.flagged))
    severity = "critical" if critical else "normal"
    hold = critical and platform.legal_hold_enabled   # yönetici silene kadar saklanır

    # Kanıt: sadece izin verilmeyen medya için küçültülmüş kopya (izin verilen içerik saklanmaz)
    evidence_rows: list[tuple] = []
    saved: list[str] = []   # R2 anahtarları
    # İnceleme bekleyen içerik için her zaman (moderatör görmeli); otomatik engellenen için sadece
    # kanıt saklama süresi > 0 ise. İzin verilen içerikten hiçbir şey saklanmaz.
    keep_evidence = decision.decision == "review" or hold or (decision.decision == "block" and platform.evidence_retention_hours > 0)
    if media is not None and keep_evidence:
        ranked = sorted(media.frames, key=lambda a: a.top, reverse=True)[:MAX_EVIDENCE_FRAMES]
        ranked.sort(key=lambda a: a.frame.timestamp_ms or 0)
        kind = "frame" if req["content_type"] == "video" else "image"
        for i, a in enumerate(ranked):
            key = (_hold_key(req["public_id"], req["created_at"], f"{i:02d}.jpg") if hold
                   else evidence_key(req["public_id"], req["created_at"], i))
            data, w, h = encode_evidence(a.frame.image)
            await storage.put_bytes(key, data, "image/jpeg")
            saved.append(key)
            cats = [{"name": k, "score": v} for k, v in sorted(a.categories.items(), key=lambda kv: -kv[1])]
            evidence_rows.append((kind, key, a.frame.timestamp_ms, w, h, a.frame.phash, cats))

    try:
        async with pool.acquire() as conn, conn.transaction():
            if media is not None:
                await conn.execute(
                    """
                    UPDATE moderation_requests SET media_mime = $2, media_bytes = $3, media_width = $4,
                           media_height = $5, media_duration_ms = $6, media_frames = $7, media_sha256 = $8
                    WHERE id = $1
                    """,
                    req["id"], mime, size, media.width, media.height, media.duration_ms,
                    len(media.frames), media.sha256,
                )
            await conn.execute(
                """
                INSERT INTO moderation_results
                    (request_id, project_id, decision, categories, max_score, reason,
                     providers, policy_version, processing_time_ms, layer1_decision, ai_used, ai_latency_ms,
                     labels, severity)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                req["id"], req["project_id"], decision.decision, categories, decision.max_score,
                decision.reason, detection.providers, decision.policy_version, elapsed_ms,
                layer1.decision, ai_used, ai_ms, labels, severity,
            )
            if hold:
                await conn.execute(
                    "UPDATE moderation_requests SET legal_hold = true, held_at = now() WHERE id = $1", req["id"])
            for kind, rel, ts, w, h, ph, cats in evidence_rows:
                await conn.execute(
                    """
                    INSERT INTO media_evidence (request_id, kind, path, timestamp_ms, width, height, phash, categories)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    req["id"], kind, rel, ts, w, h, ph, cats,
                )
            if decision.decision == "review":
                await conn.execute(
                    "INSERT INTO review_queue (project_id, request_id, ai_decision) VALUES ($1, $2, $3)",
                    req["project_id"], req["id"], decision.decision,
                )
            await conn.execute(
                "UPDATE moderation_requests SET status = 'completed', completed_at = now() WHERE id = $1", req["id"]
            )
            delivery = await webhooks.record_event(
                conn, req["project_id"], "moderation.completed",
                webhooks.result_payload(req["public_id"], "completed", decision.decision, decision.reason,
                                        req["content_type"], req["external_user_id"], req["external_content_id"],
                                        labels, severity),
            )
    except Exception:
        await storage.delete_keys(saved)  # veritabanına yazılamadıysa R2'de yetim nesne bırakma
        raise
    await webhooks.enqueue(ctx["redis"], delivery)
    return hold


async def _retry_or_fail(ctx: dict, req, exc: Exception, error: str) -> None:
    if ctx["job_try"] < MAX_TRIES:
        await ctx["db"].execute("UPDATE moderation_requests SET status = 'queued' WHERE id = $1", req["id"])
        raise Retry(defer=ctx["job_try"] * 10) from exc
    await _fail(ctx, req, error)


# ---------------------------------------------------------------- metin

async def moderate(ctx: dict, request_id: str) -> None:
    req = await ctx["db"].fetchrow(_CLAIM, UUID(request_id))
    if req is None:
        return  # başka bir işçi aldı ya da zaten tamamlandı
    if req["content_type"] != "text":
        # Yanlış kuyruğa düşmüş medya işi: medya kuyruğuna aktar
        await ctx["db"].execute("UPDATE moderation_requests SET status = 'queued' WHERE id = $1", req["id"])
        await ctx["redis"].enqueue_job("moderate_media", request_id, _queue_name=MEDIA_QUEUE)
        return

    started = time.perf_counter()
    try:
        policy = await _project_policy(ctx["db"], req["project_id"])
        detection = run_text_pipeline(req["content_text"] or "", policy.profanity_level)
        await _finalize(ctx, req, detection, int((time.perf_counter() - started) * 1000))
    except Retry:
        raise
    except Exception as exc:
        log.exception("text moderation failed for %s", request_id)
        await _retry_or_fail(ctx, req, exc, type(exc).__name__)  # mesaj içerik sızdırabilir, sadece tip


# ---------------------------------------------------------------- görsel / video

async def moderate_media(ctx: dict, request_id: str) -> None:
    """Medya R2'de: URL kaynağıysa önce R2'ye akıtılır (diske yazılmaz), sonra R2'den analiz edilir.
    Orijinal nesne analizden hemen sonra R2'den silinir."""
    req = await ctx["db"].fetchrow(_CLAIM, UUID(request_id))
    if req is None:
        return
    if not storage.configured():
        await _fail(ctx, req, "media_storage_not_configured")
        return
    started = time.perf_counter()
    limit = settings.max_image_bytes if req["content_type"] == "image" else settings.max_video_bytes
    key = req["media_key"]
    keep_object = False
    try:
        if req["source"] == "url" and (key is None or await storage.head(key) is None):
            key = incoming_key(req["project_id"])
            await ctx["db"].execute("UPDATE moderation_requests SET media_key = $2 WHERE id = $1", req["id"], key)
            writer = storage.writer(key, "application/octet-stream")
            try:
                await download(req["content_url"], writer.write, max_bytes=limit,
                               timeout_s=settings.fetch_timeout_s, max_redirects=settings.fetch_max_redirects)
                await writer.close()
            except UnsafeURL as exc:
                await writer.abort()
                await _fail(ctx, req, f"url_not_allowed:{exc}")
                return
            except FetchError as exc:
                await writer.abort()
                if str(exc) in ("too_large", "http_404", "http_403", "http_410", "empty_body"):
                    await _fail(ctx, req, f"fetch_failed:{exc}")
                    return
                await _retry_or_fail(ctx, req, exc, f"fetch_failed:{exc}")
                return

        meta = await storage.head(key) if key else None
        if meta is None:
            await _fail(ctx, req, "media_missing")
            return
        if meta["size"] > limit:
            await _fail(ctx, req, "too_large")
            return
        sniffed = sniff(await storage.read_range(key, 0, 31))
        if sniffed is None:
            await _fail(ctx, req, "unsupported_media_format")
            return
        if sniffed.kind != req["content_type"]:
            await _fail(ctx, req, f"media_type_mismatch:{sniffed.kind}")
            return

        policy = await _project_policy(ctx["db"], req["project_id"])
        analysis = await analyze_file(key, sniffed, pool=ctx["db"], redis=ctx["redis"], project_id=req["project_id"],
                                      profanity_level=policy.profanity_level)
        if analysis.text:
            analysis.detection.labels |= run_text_pipeline(analysis.text, policy.profanity_level).labels
        held = await _finalize(ctx, req, analysis.detection, int((time.perf_counter() - started) * 1000),
                               media=analysis, mime=sniffed.mime, size=meta["size"])
        if held:
            # Yasal saklama: orijinal dosya hold/ altına kopyalanır (otomatik silme kuralları dokunmaz)
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "video/mp4": "mp4",
                   "video/quicktime": "mov", "video/webm": "webm", "video/x-msvideo": "avi"}.get(sniffed.mime, "bin")
            hold_key = _hold_key(req["public_id"], req["created_at"], f"original.{ext}")
            await storage.copy(key, hold_key)
            await ctx["db"].execute("UPDATE moderation_requests SET hold_media_key = $2 WHERE id = $1", req["id"], hold_key)
    except InvalidMedia as exc:
        await _fail(ctx, req, str(exc))
    except StorageNotConfigured:
        await _fail(ctx, req, "media_storage_not_configured")
    except StorageError as exc:
        await _fail(ctx, req, str(exc))
    except Retry:
        keep_object = True  # tekrar denemede nesne lazım
        raise
    except Exception as exc:
        code = error_code(exc)
        if is_permanent(exc):
            # Ayar/yetki hatası (ör. NoSuchBucket, AccessDenied): 5 kez boşuna denemek yerine hemen,
            # gerçek R2 koduyla bitir. Kod panelde ve loglarda görünür.
            log.error("R2 hatası (%s): %s. %s", request_id, code, HINTS.get(code, ""))
            await _fail(ctx, req, f"storage_error:{code}")
            return
        log.exception("media moderation failed for %s (%s)", request_id, code)
        keep_object = ctx["job_try"] < MAX_TRIES
        await _retry_or_fail(ctx, req, exc, code)
    finally:
        if key and not keep_object:
            # Orijinal medya hiçbir zaman saklanmaz
            try:
                await storage.delete_keys([key])
            except Exception:  # noqa: BLE001 — lifecycle kuralı yine siler
                pass
            await ctx["db"].execute("UPDATE moderation_requests SET media_key = NULL WHERE id = $1", req["id"])


# ---------------------------------------------------------------- webhook

async def deliver_webhook(ctx: dict, delivery_id: str) -> None:
    await webhooks.deliver(ctx["db"], delivery_id)


async def dispatch_webhooks(ctx: dict) -> None:
    """Zamanı gelmiş (veya kuyruğa hiç girememiş) teslimatları kuyruğa alır."""
    rows = await ctx["db"].fetch(
        """
        SELECT id, attempts FROM webhook_deliveries
        WHERE status = 'pending' AND next_attempt_at <= now()
          AND (attempts > 0 OR created_at < now() - interval '1 minute')
        ORDER BY next_attempt_at LIMIT 500
        """
    )
    for r in rows:
        await ctx["redis"].enqueue_job("deliver_webhook", str(r["id"]), _job_id=f"wh:{r['id']}:{r['attempts']}",
                                       _queue_name=TEXT_QUEUE)


# ---------------------------------------------------------------- kurtarma

async def requeue_stuck(ctx: dict) -> None:
    """Kuyruğa hiç girememiş veya işçi çökünce yarım kalmış işleri kurtarır.

    last_enqueued_at sayesinde kuyrukta bekleyen bir iş her dakika tekrar eklenmez (kopya birikmez).
    Deneme hakkı biten işler 'failed' olur; sonsuz döngü olmaz.
    """
    pool = ctx["db"]
    stale = """
        (status = 'queued' AND coalesce(last_enqueued_at, created_at) < now() - interval '5 minutes')
        OR (status = 'processing' AND started_at < now() - CASE WHEN content_type = 'text'
                                                              THEN interval '5 minutes' ELSE interval '20 minutes' END)
    """
    failed = await pool.fetch(
        f"""
        UPDATE moderation_requests SET status = 'failed', error = 'max_attempts_exceeded', completed_at = now()
        WHERE id IN (SELECT id FROM moderation_requests WHERE ({stale}) AND attempts >= $1
                     LIMIT 500 FOR UPDATE SKIP LOCKED)
        RETURNING id
        """,
        MAX_TRIES,
    )
    rows = await pool.fetch(
        f"""
        UPDATE moderation_requests SET status = 'queued', last_enqueued_at = now()
        WHERE id IN (SELECT id FROM moderation_requests WHERE ({stale}) AND attempts < $1
                     LIMIT 500 FOR UPDATE SKIP LOCKED)
        RETURNING id, content_type
        """,
        MAX_TRIES,
    )
    for r in rows:
        if r["content_type"] == "text":
            await ctx["redis"].enqueue_job("moderate", str(r["id"]), _queue_name=TEXT_QUEUE)
        else:
            await ctx["redis"].enqueue_job("moderate_media", str(r["id"]), _queue_name=MEDIA_QUEUE)
    if rows or failed:
        log.warning("requeued %d stuck requests, failed %d", len(rows), len(failed))


# ---------------------------------------------------------------- saklama süresi (KVKK)

async def purge_expired(ctx: dict) -> None:
    """Saklama süresi dolan içerikleri siler: metin, URL, metadata, istemci IP'si ve kanıt görselleri.
    Karar, kategori ve istatistik kayıtları kalır (kişisel veri içermez). İncelemesi bekleyenlere dokunulmaz."""
    pool = ctx["db"]
    platform = await ctx["settings_cache"].get(pool)
    deadline = time.monotonic() + 50
    total = 0
    while time.monotonic() < deadline:
        async with pool.acquire() as conn, conn.transaction():
            ids = [r["id"] for r in await conn.fetch(
                """
                UPDATE moderation_requests r
                SET content_text = NULL, content_url = NULL, metadata = '{}'::jsonb, client_ip = NULL, user_info_enc = NULL,
                    content_purged_at = now()
                WHERE r.id IN (
                    SELECT id FROM moderation_requests m
                    WHERE m.content_purged_at IS NULL AND m.status IN ('completed', 'failed')
                      AND NOT m.legal_hold                     -- yasal saklamadakiler yönetici silene kadar kalır
                      AND m.created_at < now() - make_interval(days => $1)
                      AND NOT EXISTS (SELECT 1 FROM review_queue q WHERE q.request_id = m.id AND q.status = 'pending')
                    LIMIT 2000 FOR UPDATE SKIP LOCKED
                )
                RETURNING r.id
                """,
                platform.retention_days,
            )]
            if not ids:
                break
            paths = [r["path"] for r in await conn.fetch(
                "DELETE FROM media_evidence WHERE request_id = ANY($1::uuid[]) RETURNING path", ids
            )]
        if paths and storage.configured():
            await storage.delete_keys(paths)
        total += len(ids)

    await pool.execute(
        "DELETE FROM webhook_deliveries WHERE status <> 'pending' AND created_at < now() - interval '30 days'"
    )
    await pool.execute(
        "DELETE FROM admin_sessions WHERE expires_at < now() - interval '7 days' OR revoked_at < now() - interval '7 days'"
    )
    await pool.execute("DELETE FROM admin_mfa_challenges WHERE expires_at < now()")
    await pool.execute("DELETE FROM ai_calls WHERE created_at < now() - interval '30 days'")
    # Çöken bir işçinin kapatamadığı eski bütçe rezervasyonları (bugünkü bütçeyi etkilemez, aylığı şişirmesin)
    await pool.execute("UPDATE ai_usage_daily SET reserved_usd = 0 WHERE reserved_usd > 0 AND day < current_date - 1")

    if total:
        log.warning("purged content of %d requests (retention %d days)", total, platform.retention_days)


# ---------------------------------------------------------------- medya depolama temizliği

async def purge_media(ctx: dict) -> dict:
    """15 dakikada bir: R2'deki kanıt kareleri ve yetim yüklemeler.

    1. Kanıt süresi dolanlar silinir: karar verilmiş içerikte (otomatik veya moderatör) karar anından
       itibaren evidence_retention_hours sonra. İnceleme bekleyenlere dokunulmaz.
    2. Yetim yüklemeler: imzalı adresle yüklenip moderasyona hiç gönderilmemiş veya işi bitmiş ama
       silinememiş incoming/ nesneleri (1 saatten eski, işlenmeyi beklemeyen).
    Güvenlik ağı: bucket lifecycle kuralı incoming/ nesnelerini 1 günde, evidence/ nesnelerini 35 günde siler.
    """
    pool = ctx["db"]
    platform = await ctx["settings_cache"].get(pool)
    removed = {"evidence": 0, "incoming": 0}
    if not storage.configured():
        return removed

    while True:
        rows = await pool.fetch(
            """
            DELETE FROM media_evidence WHERE id IN (
                SELECT e.id FROM media_evidence e
                JOIN moderation_requests r ON r.id = e.request_id
                LEFT JOIN review_queue q ON q.request_id = r.id
                WHERE (q.id IS NULL OR q.status = 'resolved') AND NOT r.legal_hold
                  AND coalesce(q.reviewed_at, r.completed_at, r.created_at) < now() - make_interval(hours => $1)
                LIMIT 1000
            ) RETURNING path
            """,
            platform.evidence_retention_hours,
        )
        if not rows:
            break
        await storage.delete_keys([r["path"] for r in rows])
        removed["evidence"] += len(rows)

    try:
        old = await storage.list_older_than("incoming/", ORPHAN_AGE_S)
    except Exception:  # noqa: BLE001
        log.exception("R2 list failed")
        old = []
    if old:
        active = {r["media_key"] for r in await pool.fetch(
            "SELECT media_key FROM moderation_requests WHERE media_key = ANY($1::text[]) AND status IN ('queued', 'processing')",
            old,
        )}
        stale = [k for k in old if k not in active]
        await storage.delete_keys(stale)
        removed["incoming"] = len(stale)
    if any(removed.values()):
        log.info("media cleanup: %s", removed)
    return removed


async def clean_temp_dirs(ctx: dict | None = None) -> int:
    """Medya işçisinin çökme/yeniden başlatmada yarım kalan geçici klasörleri (video kareleri, OCR)."""
    import shutil
    import tempfile
    base = Path(tempfile.gettempdir())
    cutoff = time.time() - 3600
    count = 0
    for d in base.glob("*"):
        if d.name.startswith(("media_", "ocr_")) and d.is_dir():
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    count += 1
            except OSError:
                pass
    return count


# ---------------------------------------------------------------- yaşam döngüsü

async def startup(ctx: dict) -> None:
    ctx["db"] = await create_pool(max_size=20, statement_timeout_ms=60_000)
    ctx["settings_cache"] = SettingsCache(ttl=5)
    await storage.start()
    if storage.configured():
        try:
            await storage.ensure_ready(create_bucket=settings.r2_create_bucket)
        except Exception:  # noqa: BLE001 — metin moderasyonu R2 olmadan da çalışır
            log.exception("R2 hazır değil; görsel/video işlenemez")
    else:
        log.warning("R2 yapılandırılmamış (R2_* env). Metin çalışır; görsel/video istekleri reddedilir.")


async def media_startup(ctx: dict) -> None:
    await startup(ctx)
    detectors.warmup()  # modeli ilk işte değil açılışta yükle
    await clean_temp_dirs()


async def shutdown_all(ctx: dict) -> None:
    await storage.close()
    await ctx["db"].close()


async def shutdown(ctx: dict) -> None:
    await ctx["db"].close()


class WorkerSettings:
    """Metin + webhook + bakım."""
    queue_name = TEXT_QUEUE
    functions = [moderate, deliver_webhook]
    cron_jobs = [
        cron(requeue_stuck, second=0),
        cron(dispatch_webhooks, second=30),
        cron(purge_expired, minute=17, second=10),     # saatte bir
        cron(purge_media, minute={0, 15, 30, 45}, second=40),
    ]
    on_startup = startup
    on_shutdown = shutdown_all
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = MAX_TRIES
    max_jobs = settings.worker_max_jobs
    job_timeout = 60
    poll_delay = 0.1
    health_check_interval = 30
    health_check_key = "arq:queue:health-check"


class MediaWorkerSettings:
    """Görsel / video. Eşzamanlılık düşük tutulur: her iş CPU yoğun."""
    queue_name = MEDIA_QUEUE
    functions = [moderate_media]
    cron_jobs = [cron(clean_temp_dirs, minute={5, 35})]
    on_startup = media_startup
    on_shutdown = shutdown_all
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = MAX_TRIES
    max_jobs = settings.media_worker_max_jobs
    job_timeout = 600
    poll_delay = 0.2
    health_check_interval = 30
    health_check_key = "arq:media:health-check"
