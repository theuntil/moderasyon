"""Cloudflare R2 (S3 uyumlu API) medya deposu.

Kural: medya hiçbir zaman sunucunun diskine yazılmaz.
- Yüklemeler R2'ye gider: ya istemci imzalı adresle doğrudan yükler (baytlar sunucuya hiç uğramaz),
  ya da /v1/moderate/upload gövdesi diske yazılmadan 8 MB'lık parçalarla R2'ye akıtılır.
- Analiz: görseller R2'den belleğe okunur; videolar ffmpeg ile doğrudan R2'den (imzalı adres) okunur.
- Orijinal dosya analizden hemen sonra R2'den silinir. Sadece izin verilmeyen içeriğin küçültülmüş kanıt
  kareleri R2'de, kısa süreli tutulur.
- Güvenlik ağı: bucket'a otomatik silme kuralları (lifecycle) kurulur; uygulama temizliği aksasa bile
  incoming/ 1 günde, evidence/ 35 günde R2 tarafından silinir.

Anahtar yapısı:
    incoming/<proje>/<rastgele>       işlenmeyi bekleyen yükleme (kısa ömürlü)
    evidence/<yyyy>/<mm>/<istek>/<n>.jpg   kanıt karesi
    blocklist/<id>.jpg                görsel engel listesi önizlemesi
"""
import hashlib
import logging
import secrets
import time
from contextlib import AsyncExitStack
from datetime import datetime

from app.config import settings

log = logging.getLogger("storage")

PART_SIZE = 8 * 1024 * 1024          # R2 çok parçalı yüklemede en az 5 MB; 8 MB bellek/verim dengesi
INCOMING_TTL_DAYS = 1
EVIDENCE_TTL_DAYS = 35


class StorageError(Exception):
    pass


class StorageNotConfigured(StorageError):
    pass


# Ayar/yetki hataları: tekrar denemek anlamsız, hemen net bir hatayla bitir
PERMANENT_CODES = {"NoSuchBucket", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                   "InvalidBucketName", "AuthorizationHeaderMalformed", "Unauthorized", "403", "401", "404"}

HINTS = {
    "NoSuchBucket": "Bucket bulunamadı: R2_BUCKET adını kontrol edin. Bucket EU seçilmeden oluşturulduysa "
                    "R2_JURISDICTION boş olmalı; EU ise 'eu' olmalı.",
    "AccessDenied": "Erişim reddedildi: token'ın bu bucket için 'Object Read & Write' izni olmalı. Token'da IP "
                    "filtresi varsa kaldırın veya sunucunun çıkış IP'sini (IPv6 dahil) ekleyin.",
    "InvalidAccessKeyId": "R2_ACCESS_KEY_ID yanlış: token ekranındaki 'Access Key ID' değerini kullanın "
                          "('Token value' değil).",
    "SignatureDoesNotMatch": "R2_SECRET_ACCESS_KEY yanlış (veya başında/sonunda boşluk var).",
    "Unauthorized": "Kimlik bilgileri reddedildi: Access Key ID / Secret Access Key değerlerini kontrol edin.",
    "401": "Kimlik bilgileri reddedildi: Access Key ID / Secret Access Key değerlerini kontrol edin.",
    "403": "Erişim reddedildi: token izinleri, bucket kapsamı veya IP filtresi.",
    "404": "Bucket bulunamadı: R2_BUCKET adını kontrol edin. Bucket EU seçilmeden oluşturulduysa "
           "R2_JURISDICTION boş olmalı; EU ise 'eu' olmalı.",
    "EndpointConnectionError": "R2'ye bağlanılamadı: R2_ACCOUNT_ID ve R2_JURISDICTION değerlerini kontrol edin.",
}


def error_code(exc: BaseException) -> str:
    """botocore hatasından gerçek R2 hata kodunu çıkarır (ör. NoSuchBucket, AccessDenied)."""
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return str(err.get("Code") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or "ClientError")
    if isinstance(exc, EndpointConnectionError):
        return "EndpointConnectionError"
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    if isinstance(exc, StorageError):
        return str(exc) or type(exc).__name__
    return type(exc).__name__


def is_permanent(exc: BaseException) -> bool:
    return isinstance(exc, StorageNotConfigured) or error_code(exc) in PERMANENT_CODES


def endpoint_url() -> str:
    if settings.r2_endpoint:
        return settings.r2_endpoint.rstrip("/")
    if settings.r2_account_id:
        region = f".{settings.r2_jurisdiction}" if settings.r2_jurisdiction else ""
        return f"https://{settings.r2_account_id}{region}.r2.cloudflarestorage.com"
    return ""


def configured() -> bool:
    return bool(endpoint_url() and settings.r2_bucket and settings.r2_access_key_id and settings.r2_secret_access_key)


def incoming_key(project_id) -> str:
    return f"incoming/{project_id}/{secrets.token_urlsafe(18)}"


def evidence_key(public_id: str, created_at: datetime, index: int) -> str:
    return f"evidence/{created_at.strftime('%Y/%m')}/{public_id}/{index:02d}.jpg"


def blocklist_key(item_id) -> str:
    return f"blocklist/{item_id}.jpg"


class Storage:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.client = None

    @property
    def bucket(self) -> str:
        return settings.r2_bucket

    def configured(self) -> bool:
        return configured()

    async def start(self) -> None:
        if not configured() or self.client is not None:
            return
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        self._stack = AsyncExitStack()
        self.client = await self._stack.enter_async_context(get_session().create_client(
            "s3",
            endpoint_url=endpoint_url(),
            region_name="auto",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=AioConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 4, "mode": "standard"},
                connect_timeout=5, read_timeout=60, max_pool_connections=64,
            ),
        ))

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self.client = None, None

    def _c(self):
        if self.client is None:
            raise StorageNotConfigured("R2 is not configured")
        return self.client

    # ---------------------------------------------------------------- temel işlemler

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        await self._c().put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def head(self, key: str) -> dict | None:
        from botocore.exceptions import ClientError
        try:
            r = await self._c().head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": r["ContentLength"], "content_type": r.get("ContentType"), "last_modified": r.get("LastModified")}

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        r = await self._c().get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")
        async with r["Body"] as body:
            return await body.read()

    async def get_bytes(self, key: str, max_bytes: int) -> bytes:
        r = await self._c().get_object(Bucket=self.bucket, Key=key)
        if r["ContentLength"] > max_bytes:
            r["Body"].close()
            raise StorageError("too_large")
        async with r["Body"] as body:
            data = await body.read()
        if len(data) > max_bytes:
            raise StorageError("too_large")
        return data

    async def sha256(self, key: str) -> str:
        """Nesneyi diske yazmadan akış halinde okuyup hash'ler (R2'de veri çıkışı ücretsiz)."""
        h = hashlib.sha256()
        r = await self._c().get_object(Bucket=self.bucket, Key=key)
        async with r["Body"] as body:
            async for chunk in body.iter_chunks(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()

    async def presign_get(self, key: str, expires: int = 900) -> str:
        return await self._c().generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)

    async def presign_put(self, key: str, content_type: str, expires: int = 900) -> str:
        return await self._c().generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=expires)

    async def copy(self, src: str, dst: str) -> None:
        await self._c().copy_object(Bucket=self.bucket, Key=dst, CopySource={"Bucket": self.bucket, "Key": src})

    async def delete_keys(self, keys: list[str]) -> int:
        """Toplu silme (R2'de silme ücretsiz). Hata olsa bile yaşam döngüsü kuralı sonunda siler."""
        keys = [k for k in dict.fromkeys(keys) if k]
        deleted = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            try:
                await self._c().delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
                deleted += len(batch)
            except StorageNotConfigured:
                raise
            except Exception:  # noqa: BLE001
                log.exception("R2 delete failed for %d keys", len(batch))
        return deleted

    async def list_older_than(self, prefix: str, age_s: int, limit: int = 5000) -> list[str]:
        cutoff = time.time() - age_s
        out: list[str] = []
        paginator = self._c().get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"].timestamp() < cutoff:
                    out.append(obj["Key"])
                    if len(out) >= limit:
                        return out
        return out

    # ---------------------------------------------------------------- çok parçalı akış yüklemesi

    def writer(self, key: str, content_type: str) -> "StreamWriter":
        return StreamWriter(self, key, content_type)

    # ---------------------------------------------------------------- kurulum ve sağlık

    async def ensure_ready(self, *, create_bucket: bool = False) -> None:
        """Bucket'a erişimi doğrular ve otomatik silme kurallarını kurar (idempotent, hata olursa loglar)."""
        from botocore.exceptions import ClientError
        c = self._c()
        try:
            await c.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchBucket", "NotFound") and create_bucket:
                await c.create_bucket(Bucket=self.bucket)
            else:
                log.error("R2 bucket '%s' erişilemiyor (%s). %s", self.bucket, code,
                          HINTS.get(str(code), "Bucket adını ve API token izinlerini kontrol edin."))
                raise
        try:
            await c.put_bucket_lifecycle_configuration(Bucket=self.bucket, LifecycleConfiguration={"Rules": [
                {"ID": "moderation-incoming", "Status": "Enabled", "Filter": {"Prefix": "incoming/"},
                 "Expiration": {"Days": INCOMING_TTL_DAYS},
                 "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
                {"ID": "moderation-evidence", "Status": "Enabled", "Filter": {"Prefix": "evidence/"},
                 "Expiration": {"Days": EVIDENCE_TTL_DAYS}},
            ]})
        except Exception as exc:  # noqa: BLE001 — token'da yetki yoksa uygulama temizliği yine çalışır
            log.warning("R2 lifecycle kuralları kurulamadı (%s). Cloudflare panelinden elle ekleyin: "
                        "incoming/ 1 gün, evidence/ %d gün.", type(exc).__name__, EVIDENCE_TTL_DAYS)

    async def health(self) -> dict:
        if not configured():
            return {"ok": False, "configured": False, "error": "not_configured"}
        started = time.perf_counter()
        try:
            await self._c().head_bucket(Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001
            code = error_code(exc)
            return {"ok": False, "configured": True, "bucket": self.bucket, "error": code, "hint": HINTS.get(code)}
        return {"ok": True, "configured": True, "bucket": self.bucket,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


class StreamWriter:
    """Parça parça R2'ye yazar; bellekte en fazla bir parça (8 MB) tutar. Küçük dosyalar tek PUT."""

    def __init__(self, storage: Storage, key: str, content_type: str):
        self.s, self.key, self.content_type = storage, key, content_type
        self.buffer = bytearray()
        self.upload_id: str | None = None
        self.parts: list[dict] = []
        self.size = 0
        self.sha = hashlib.sha256()

    async def write(self, data: bytes) -> None:
        self.buffer += data
        self.size += len(data)
        self.sha.update(data)
        while len(self.buffer) >= PART_SIZE:
            await self._flush(bytes(self.buffer[:PART_SIZE]))
            del self.buffer[:PART_SIZE]

    async def _flush(self, chunk: bytes) -> None:
        c = self.s._c()
        if self.upload_id is None:
            r = await c.create_multipart_upload(Bucket=self.s.bucket, Key=self.key, ContentType=self.content_type)
            self.upload_id = r["UploadId"]
        n = len(self.parts) + 1
        r = await c.upload_part(Bucket=self.s.bucket, Key=self.key, UploadId=self.upload_id, PartNumber=n, Body=chunk)
        self.parts.append({"PartNumber": n, "ETag": r["ETag"]})

    async def close(self) -> None:
        if self.upload_id is None:
            await self.s.put_bytes(self.key, bytes(self.buffer), self.content_type)
        else:
            if self.buffer:
                await self._flush(bytes(self.buffer))
            await self.s._c().complete_multipart_upload(
                Bucket=self.s.bucket, Key=self.key, UploadId=self.upload_id, MultipartUpload={"Parts": self.parts})
        self.buffer = bytearray()

    async def abort(self) -> None:
        try:
            if self.upload_id is not None:
                await self.s._c().abort_multipart_upload(Bucket=self.s.bucket, Key=self.key, UploadId=self.upload_id)
        except Exception:  # noqa: BLE001 — lifecycle kuralı yarım yüklemeleri 1 günde temizler
            log.warning("R2 multipart abort failed for %s", self.key)
        self.buffer = bytearray()

    @property
    def sha256(self) -> str:
        return self.sha.hexdigest()


storage = Storage()
