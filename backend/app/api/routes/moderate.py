import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator

from app.api.deps import DB_ERRORS, AuthContext, require_api_key, unprocessed
from app.moderation.policy import ProjectPolicy, evaluate, resolve_mode
from app.moderation.text_pipeline import run_text_pipeline
from app.config import settings
from app.media.sniff import sniff
from app.netsafe import UnsafeURL, validate_url
from app.security import new_public_id
from botocore.exceptions import BotoCoreError, ClientError

from app.storage import StorageError, incoming_key, storage

router = APIRouter(prefix="/v1", tags=["moderation"])
log = logging.getLogger("api")

# Kalıcı hatalar: aynı içerik tekrar gönderilse de sonuç değişmez
PERMANENT_ERRORS = {"image_too_large", "invalid_image", "invalid_video", "unsupported_media_format",
                    "media_type_mismatch", "too_large", "video_too_long", "no_video_stream", "no_frames",
                    "url_not_allowed", "invalid_metadata"}

TEXT_QUEUE = "arq:queue"
MEDIA_QUEUE = "arq:media"


class UserInfo(BaseModel):
    """İçeriği gönderen kullanıcı (isteğe bağlı). Veritabanında şifreli tutulur, sadece panelde yetkili
    yöneticiye görünür; yasal saklamada içerikle birlikte saklanır, diğerlerinde saklama süresiyle silinir."""
    id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=100)
    surname: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    extra: dict[str, Any] = Field(default_factory=dict, description="Ek bilgiler (ör. doğum tarihi, ülke)")

    @model_validator(mode="after")
    def _size(self) -> "UserInfo":
        if len(json.dumps(self.extra, default=str)) > 2048:
            raise ValueError("'user.extra' must be at most 2 KB")
        return self


def _encrypt_user(user: "UserInfo | None") -> str | None:
    if user is None:
        return None
    data = {k: v for k, v in user.model_dump().items() if v not in (None, "", {})}
    if not data:
        return None
    from app.admin.auth import encrypt_secret
    return encrypt_secret(json.dumps(data, ensure_ascii=False))


class ModerateRequest(BaseModel):
    type: Literal["text", "image", "video"]
    text: str | None = Field(default=None, max_length=settings.max_text_length)
    url: HttpUrl | None = Field(default=None, description="image/video için herkese açık http(s) adresi")
    upload_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{16,64}$",
                                  description="POST /v1/uploads ile alınan ve dosyanın yüklendiği kimlik")
    user_id: str | None = Field(default=None, max_length=128)
    content_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    user: UserInfo | None = Field(default=None, description="Gönderen kullanıcı bilgileri (isteğe bağlı, şifreli saklanır)")

    @model_validator(mode="after")
    def _check(self) -> "ModerateRequest":
        if self.type == "text":
            if not (self.text and self.text.strip()):
                raise ValueError("'text' is required when type is 'text'")
            if self.url is not None or self.upload_id is not None:
                raise ValueError("'url' and 'upload_id' are only allowed for image or video")
        else:
            if (self.url is None) == (self.upload_id is None):
                raise ValueError("send exactly one of 'url' or 'upload_id' for image/video (or use /v1/moderate/upload)")
            if self.text is not None:
                raise ValueError("'text' is only allowed for type 'text'")
        if len(json.dumps(self.metadata)) > settings.max_metadata_bytes:
            raise ValueError(f"'metadata' must be at most {settings.max_metadata_bytes} bytes")
        return self


# Bir içerik insan tarafından incelendiyse client'a insanın kararı döner.
_FINAL_DECISION_COLUMNS = """
    r.public_id, r.status, r.content_type, r.created_at, r.request_hash, r.error,
    res.labels, res.severity,
    COALESCE(q.human_decision, res.decision) AS decision,
    CASE WHEN q.human_decision IS NOT NULL THEN 'human_reviewed' ELSE res.reason END AS reason
"""
_FINAL_DECISION_JOINS = """
    FROM moderation_requests r
    LEFT JOIN moderation_results res ON res.request_id = r.id
    LEFT JOIN review_queue q ON q.request_id = r.id
"""
_SELECT_BY_PUBLIC_ID = f"SELECT {_FINAL_DECISION_COLUMNS} {_FINAL_DECISION_JOINS} WHERE r.public_id = $1 AND r.project_id = $2"


def _require_storage(auth: AuthContext) -> None:
    """Medya sadece Cloudflare R2'de tutulur; R2 yapılandırılmamışsa medya kabul edilmez (metin etkilenmez)."""
    if storage.client is None:
        raise unprocessed("media_storage_not_configured", "Media storage is not available. Retry later.",
                          _policy(auth).fallback_decision, retry_after=300)


MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp",
               "video/mp4", "video/quicktime", "video/webm", "video/x-msvideo"}


def _policy(auth: AuthContext) -> ProjectPolicy:
    return ProjectPolicy.from_row(auth.policy, auth.policy_revision)


def _to_response(row, auth: AuthContext | None = None) -> dict:
    """Yanıt sözleşmesi: her durumda aynı alanlar. İstemciye iç detay (model, skor) dönmez; bunlar panelde.
    status: completed | queued | processing | failed | unprocessed"""
    body = {"id": row["public_id"], "type": row["content_type"], "status": row["status"],
            "decision": row["decision"], "reason": row["reason"]}
    if row["status"] == "completed":
        # labels: uygulamanın kullanıcıya işlem yapması için (ör. "terör:pkk" → hesabı kapat)
        # severity: "critical" → pornografi, çocuk istismarı, terör veya kritik işaretli kural
        body["labels"] = list(row["labels"] or [])
        body["severity"] = row["severity"] or "normal"
    if row["status"] == "failed":
        code = (row["error"] or "failed").split(":")[0]
        body["error"] = code
        body["retryable"] = code not in PERMANENT_ERRORS
    if row["status"] != "completed" and auth is not None:
        fb = _policy(auth).fallback_decision
        if fb:
            body["fallback_decision"] = fb
    body["created_at"] = row["created_at"].isoformat()
    return body


def _degraded_text(auth: AuthContext, text: str, platform) -> JSONResponse:
    """Veritabanına yazılamıyor: metin yine de yerel modelle anında değerlendirilir (AI ve kayıt olmadan)."""
    policy = _policy(auth)
    decision = resolve_mode(evaluate(run_text_pipeline(text), platform, policy), policy)
    return JSONResponse(status_code=status.HTTP_200_OK, content={
        "id": None, "type": "text", "status": "completed", "decision": decision.decision, "reason": decision.reason,
        "degraded": True, "persisted": False, "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def _process_inline(request: Request, request_id, budget_s: float) -> None:
    """Kuyruk/işçi erişilemiyor veya yavaş: metni API işler. Atomik sahiplenme (queued → processing)
    sayesinde aynı içerik işçiyle birlikte iki kez işlenmez."""
    from arq import Retry

    from app.worker import main as worker

    ctx = {"db": request.app.state.db, "redis": request.app.state.arq,
           "settings_cache": request.app.state.settings_cache, "job_try": worker.MAX_TRIES - 1}
    try:
        await asyncio.wait_for(worker.moderate(ctx, str(request_id)), timeout=max(1.0, budget_s))
    except Retry:
        pass   # geçici hata: kayıt 'queued' kaldı, işçi/cron tekrar dener
    except Exception:  # noqa: BLE001
        log.exception("inline processing failed for %s", request_id)


def _media_error(code: str, message: str, http: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": code, "message": message})


async def _create(
    request: Request, auth: AuthContext, *, content_type: str, text: str | None, url: str | None, source: str,
    user_id: str | None, content_id: str | None, metadata: dict, idempotency_key: str | None, request_hash: str,
    media_key: str | None = None, user_info: "UserInfo | None" = None,
) -> tuple[Any, bool]:
    """İsteği kaydeder. (satır, yeni_mi) döner; idempotency çakışmasında mevcut satır döner."""
    pool = request.app.state.db
    row = await pool.fetchrow(
        """
        INSERT INTO moderation_requests
            (public_id, project_id, api_key_id, content_type, content_text, content_url, source,
             external_user_id, external_content_id, metadata, idempotency_key, request_hash, client_ip,
             last_enqueued_at, media_key, user_info_enc)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::inet, now(), $14, $15)
        ON CONFLICT (project_id, idempotency_key) DO NOTHING
        RETURNING id, public_id
        """,
        new_public_id(), auth.project_id, auth.api_key_id, content_type, text, url, source,
        user_id or (user_info.id if user_info else None), content_id, metadata, idempotency_key, request_hash,
        auth.client_ip, media_key, _encrypt_user(user_info),
    )
    if row is not None:
        return row, True

    existing = await pool.fetchrow(
        f"SELECT {_FINAL_DECISION_COLUMNS} {_FINAL_DECISION_JOINS} WHERE r.project_id = $1 AND r.idempotency_key = $2",
        auth.project_id, idempotency_key,
    )
    if existing is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "idempotency_conflict"})
    if existing["request_hash"] != request_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "idempotency_key_reused",
                    "message": "This Idempotency-Key was used with a different request body."},
        )
    return existing, False


async def _enqueue_and_respond(request: Request, auth: AuthContext, row, content_type: str, wait: bool):
    started = time.monotonic()
    arq = request.app.state.arq
    job = None
    try:
        if content_type == "text":
            job = await arq.enqueue_job("moderate", str(row["id"]), _job_id=str(row["id"]), _queue_name=TEXT_QUEUE)
        else:
            job = await arq.enqueue_job("moderate_media", str(row["id"]), _job_id=str(row["id"]), _queue_name=MEDIA_QUEUE)
    except Exception:  # noqa: BLE001 — Redis yok: kayıt 'queued' kalır, cron tekrar kuyruğa alır
        job = None
        log.warning("enqueue failed for %s", row["id"])

    if content_type == "text" and job is None:
        # Kuyruk yoksa beklemeden API kendisi işler (sonuç kaydedilir)
        await _process_inline(request, row["id"], settings.sync_wait_timeout_s)
    elif wait and job is not None:
        timeout = settings.sync_wait_timeout_s if content_type == "text" else settings.media_sync_wait_timeout_s
        try:
            await job.result(timeout=timeout, poll_delay=0.05 if content_type == "text" else 0.2)
        except Exception:  # noqa: BLE001 — süre doldu: işçi yoksa/yavaşsa metni API işler
            if content_type == "text":
                await _process_inline(request, row["id"], settings.request_timeout_s - (time.monotonic() - started) - 2)

    try:
        current = await request.app.state.db.fetchrow(_SELECT_BY_PUBLIC_ID, row["public_id"], auth.project_id)
    except DB_ERRORS:
        current = None
    if current is None:
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={
            "id": row["public_id"], "type": content_type, "status": "queued", "decision": None, "reason": None,
            **({"fallback_decision": _policy(auth).fallback_decision} if _policy(auth).fallback_decision else {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    code = status.HTTP_200_OK if current["status"] in ("completed", "failed") else status.HTTP_202_ACCEPTED
    return JSONResponse(status_code=code, content=_to_response(current, auth))


@router.post("/moderate", status_code=status.HTTP_202_ACCEPTED)
async def create_moderation(
    body: ModerateRequest,
    request: Request,
    auth: AuthContext = Depends(require_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    wait: bool = Query(default=False, description="Sonucu kısa bir süre bekle (sync kullanım)"),
):
    url = str(body.url) if body.url is not None else None
    if url is not None:
        # Açıkça güvensiz adresleri hemen reddet (asıl kontrol indirme anında DNS seviyesinde yapılır)
        try:
            validate_url(url)
        except UnsafeURL as exc:
            raise _media_error("url_not_allowed", f"This URL cannot be fetched ({exc}).")
    request_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    platform = await request.app.state.settings_cache.get(request.app.state.db)
    if body.type != "text":
        _require_storage(auth)
    if auth.degraded:
        if body.type == "text":
            return _degraded_text(auth, body.text, platform)
        raise unprocessed("service_degraded", "Media cannot be processed right now. Retry later.", _policy(auth).fallback_decision)
    media_key = None
    if body.upload_id is not None:
        # Yükleme projeye özel anahtar altında: başka projenin yüklemesi kullanılamaz
        media_key = f"incoming/{auth.project_id}/{body.upload_id}"
        meta = await storage.head(media_key)
        if meta is None:
            raise _media_error("upload_not_found", "No uploaded file for this upload_id (expired or not uploaded yet).")
        limit = settings.max_image_bytes if body.type == "image" else settings.max_video_bytes
        if meta["size"] > limit:
            await storage.delete_keys([media_key])
            raise _media_error("file_too_large", f"Maximum size is {limit // (1024 * 1024)} MB.",
                               status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        row, created = await _create(
            request, auth, content_type=body.type, text=body.text, url=url,
            source="json" if body.type == "text" else ("upload" if media_key else "url"),
            user_id=body.user_id, content_id=body.content_id, metadata=body.metadata,
            idempotency_key=idempotency_key, request_hash=request_hash, media_key=media_key, user_info=body.user,
        )
    except DB_ERRORS:
        if body.type == "text":
            return _degraded_text(auth, body.text, platform)
        raise unprocessed("service_degraded", "Media cannot be processed right now. Retry later.", _policy(auth).fallback_decision)
    if not created:
        return JSONResponse(status_code=status.HTTP_200_OK, content=_to_response(row, auth))
    return await _enqueue_and_respond(request, auth, row, body.type, wait)


class UploadRequest(BaseModel):
    content_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp",
                          "video/mp4", "video/quicktime", "video/webm", "video/x-msvideo"]
    size: int = Field(gt=0, description="Dosya boyutu (bayt)")


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def create_upload(body: UploadRequest, request: Request, auth: AuthContext = Depends(require_api_key)):
    """Doğrudan yükleme (önerilen): dosya imzalı adresle doğrudan Cloudflare R2'ye yüklenir, baytlar bu
    sunucuya hiç uğramaz. Sonra POST /v1/moderate {"type": "image"|"video", "upload_id": ...} gönderin.
    Adres 15 dakika geçerlidir; kullanılmayan yüklemeler otomatik silinir."""
    _require_storage(auth)
    limit = settings.max_image_bytes if body.content_type.startswith("image/") else settings.max_video_bytes
    if body.size > limit:
        raise _media_error("file_too_large", f"Maximum size is {limit // (1024 * 1024)} MB.",
                           status.HTTP_413_CONTENT_TOO_LARGE)
    key = incoming_key(auth.project_id)
    url = await storage.presign_put(key, body.content_type, expires=900)
    return {"upload_id": key.rsplit("/", 1)[1], "upload_url": url, "method": "PUT",
            "headers": {"Content-Type": body.content_type}, "expires_in": 900, "max_bytes": limit}


class _MultipartEvents:
    """python-multipart geri çağrılarını olay listesine çevirir (akış halinde, bellekte küçük parçalar)."""

    def __init__(self):
        self.events: list = []
        self._name, self._value, self._headers = b"", b"", {}

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._begin, "on_header_field": self._hf, "on_header_value": self._hv,
            "on_header_end": self._he, "on_headers_finished": self._hd, "on_part_data": self._data,
            "on_part_end": lambda: self.events.append(("end", None)),
        }

    def _begin(self):
        self._headers = {}

    def _hf(self, data, start, end):
        self._name += data[start:end]

    def _hv(self, data, start, end):
        self._value += data[start:end]

    def _he(self):
        self._headers[self._name.lower()] = self._value
        self._name, self._value = b"", b""

    def _hd(self):
        from python_multipart.multipart import parse_options_header
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        self.events.append(("begin", params.get(b"name", b"").decode("utf-8", "replace")))

    def _data(self, data, start, end):
        self.events.append(("data", bytes(data[start:end])))

    def drain(self) -> list:
        out, self.events = self.events, []
        return out


@router.post("/moderate/upload", status_code=status.HTTP_202_ACCEPTED, openapi_extra={
    "requestBody": {"content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}, "user_id": {"type": "string"},
                       "content_id": {"type": "string"}, "metadata": {"type": "string", "description": "JSON nesnesi"}},
    }}}}})
async def upload_moderation(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    wait: bool = Query(default=False),
):
    """Görsel veya video dosyasını yükle (multipart/form-data, alan adı: file). Dosya diske yazılmadan
    parça parça Cloudflare R2'ye akıtılır. Tür dosyanın içeriğinden tespit edilir."""
    from python_multipart.multipart import MultipartParser, parse_options_header

    # ÖNCE kimlik doğrulama ve rate limit, SONRA gövde
    auth = await require_api_key(request)
    if auth.degraded:
        raise unprocessed("service_degraded", "Media cannot be processed right now. Retry later.", _policy(auth).fallback_decision)
    _require_storage(auth)
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise _media_error("invalid_multipart", "Send multipart/form-data with a 'file' field.")

    events = _MultipartEvents()
    parser = MultipartParser(boundary, events.callbacks())
    fields: dict[str, bytearray] = {}
    current: str | None = None
    writer = None
    head = bytearray()
    sniffed = None
    limit = settings.max_video_bytes
    try:
        async def handle(kind: str, data):
            nonlocal current
            if kind == "begin":
                current = data
                if current != "file" and len(fields) >= 8:
                    raise _media_error("validation_error", "Too many form fields.")
                if current == "file" and (writer is not None or head):
                    raise _media_error("validation_error", "Send only one file.")
                return
            if kind == "end":
                if current == "file" and writer is None and head:
                    await start_file()
                current = None
                return
            if current == "file":
                if writer is None:
                    head.extend(data)
                    if len(head) >= 32:
                        await start_file()
                else:
                    if writer.size + len(data) > limit:
                        raise _media_error("file_too_large", f"Maximum size is {limit // (1024 * 1024)} MB.",
                                           status.HTTP_413_CONTENT_TOO_LARGE)
                    await writer.write(data)
            elif current in ("user_id", "content_id", "metadata", "user"):
                buf = fields.setdefault(current, bytearray())
                buf.extend(data)
                if len(buf) > settings.max_metadata_bytes:
                    raise _media_error("validation_error", f"'{current}' is too long.")

        async def start_file():
            nonlocal writer, sniffed, limit
            sniffed = sniff(bytes(head[:32]))
            if sniffed is None:
                raise _media_error("unsupported_media_format", "Supported: JPEG, PNG, GIF, WebP, MP4, MOV, WebM, AVI.",
                                   status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
            limit = settings.max_image_bytes if sniffed.kind == "image" else settings.max_video_bytes
            if len(head) > limit:
                raise _media_error("file_too_large", f"Maximum size is {limit // (1024 * 1024)} MB.",
                                   status.HTTP_413_CONTENT_TOO_LARGE)
            writer = storage.writer(incoming_key(auth.project_id), sniffed.mime)
            await writer.write(bytes(head))

        async for chunk in request.stream():
            parser.write(chunk)
            for kind, data in events.drain():
                await handle(kind, data)
        parser.finalize()
        for kind, data in events.drain():
            await handle(kind, data)
        if writer is None:
            raise _media_error("file_required", "Send the media in a 'file' field.")
        await writer.close()
    except HTTPException:
        if writer is not None:
            await writer.abort()
            await storage.delete_keys([writer.key])
        raise
    except (StorageError, ClientError, BotoCoreError):
        if writer is not None:
            await writer.abort()
        raise unprocessed("media_storage_error", "Media storage is temporarily unavailable. Retry later.",
                          _policy(auth).fallback_decision)
    except Exception:
        if writer is not None:
            await writer.abort()
            await storage.delete_keys([writer.key])
        raise _media_error("invalid_multipart", "Could not read the multipart body.")

    def field(name: str) -> str | None:
        value = fields.get(name)
        if not value:
            return None
        text = value.decode("utf-8", "replace")
        if name not in ("metadata", "user") and len(text) > 128:
            raise _media_error("validation_error", f"'{name}' must be at most 128 characters.")
        return text

    try:
        user_id, content_id, metadata = field("user_id"), field("content_id"), field("metadata")
        user_raw = field("user")
        try:
            user_info = UserInfo.model_validate_json(user_raw) if user_raw else None
        except Exception:  # noqa: BLE001
            raise _media_error("invalid_user", "'user' must be a JSON object with name, surname, email, phone...")
        try:
            meta = json.loads(metadata) if metadata else {}
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            raise _media_error("invalid_metadata", "'metadata' must be a JSON object.")
        request_hash = hashlib.sha256(
            json.dumps({"sha": writer.sha256, "user_id": user_id, "content_id": content_id, "metadata": meta},
                       sort_keys=True).encode()
        ).hexdigest()
        try:
            row, created = await _create(
                request, auth, content_type=sniffed.kind, text=None, url=None, source="upload",
                user_id=user_id, content_id=content_id, metadata=meta,
                idempotency_key=idempotency_key, request_hash=request_hash, media_key=writer.key, user_info=user_info,
            )
        except DB_ERRORS:
            raise unprocessed("service_degraded", "Media cannot be processed right now. Retry later.", _policy(auth).fallback_decision)
    except BaseException:
        await storage.delete_keys([writer.key])
        raise
    if not created:
        await storage.delete_keys([writer.key])     # aynı istek daha önce işlendi; yeni kopyaya gerek yok
        return JSONResponse(status_code=status.HTTP_200_OK, content=_to_response(row, auth))
    return await _enqueue_and_respond(request, auth, row, sniffed.kind, wait)


@router.get("/moderate/{public_id}")
async def get_moderation(public_id: str, request: Request, auth: AuthContext = Depends(require_api_key)):
    if len(public_id) > 64:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    # project_id filtresi zorunlu: başka project'in kaydı "yok" gibi görünür.
    try:
        row = await request.app.state.db.fetchrow(_SELECT_BY_PUBLIC_ID, public_id, auth.project_id)
    except DB_ERRORS:
        raise unprocessed("service_unavailable", "Status is temporarily unavailable. Retry later.")
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    return _to_response(row, auth)


# ---------------------------------------------------------------- KVKK: silme

async def _audit_api(conn, auth: AuthContext, action: str, details: dict) -> None:
    await conn.execute(
        """
        INSERT INTO audit_logs (actor_email, action, project_id, target_type, details, ip)
        VALUES ('api', $1, $2, 'data', $3, $4::inet)
        """,
        action, auth.project_id, details, auth.client_ip,
    )


@router.delete("/moderate/{public_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_moderation_content(public_id: str, request: Request, auth: AuthContext = Depends(require_api_key)):
    """Bir içeriğin verisini siler (metin, URL, metadata, IP, kanıt görselleri). Karar kaydı anonim kalır."""
    from app.purge import purge_requests, remove_files

    async with request.app.state.db.acquire() as conn, conn.transaction():
        rid = await conn.fetchval(
            "SELECT id FROM moderation_requests WHERE public_id = $1 AND project_id = $2 AND NOT legal_hold",
            public_id, auth.project_id
        )
        if rid is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
        paths = await purge_requests(conn, [rid])
        await _audit_api(conn, auth, "data.content_deleted", {"id": public_id})
    await remove_files(paths)


@router.delete("/users/{user_id}/data")
async def delete_user_data(user_id: str, request: Request, auth: AuthContext = Depends(require_api_key)):
    """Unutulma hakkı: bu projede user_id'ye ait tüm içerikleri siler ve kullanıcı kimliğini kayıtlardan kaldırır."""
    from app.purge import purge_requests, remove_files

    if not user_id or len(user_id) > 128:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"error": "invalid_user_id"})
    total, paths = 0, []
    async with request.app.state.db.acquire() as conn, conn.transaction():
        while True:
            ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM moderation_requests WHERE project_id = $1 AND external_user_id = $2 AND NOT legal_hold LIMIT 5000",
                auth.project_id, user_id,
            )]
            if not ids:
                break
            paths += await purge_requests(conn, ids, forget_user=True)
            total += len(ids)
        held = await conn.fetchval(
            "SELECT count(*) FROM moderation_requests WHERE project_id = $1 AND external_user_id = $2 AND legal_hold",
            auth.project_id, user_id)
        await _audit_api(conn, auth, "data.user_deleted", {"requests": total, "held": held})   # user_id loglanmaz
    await remove_files(paths)
    # held: yasal saklamadaki kayıtlar (kritik içerik) silinmez; yönetici panelden siler
    return {"deleted": total, "held": held}
