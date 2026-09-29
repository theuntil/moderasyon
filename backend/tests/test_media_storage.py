"""Medya depolama: her şey Cloudflare R2'de (testte yerel S3 sunucusu). Sunucu diskine medya yazılmaz.

Orijinal nesne analizden hemen sonra silinir, kanıt kareleri kısa süreli tutulur, imzalı doğrudan
yükleme projeye özeldir, büyük dosyalar parça parça akıtılır, yetim yüklemeler temizlenir.
"""
import asyncio
import os
import subprocess
import time

import httpx

from app.config import settings
from app.db import create_pool
from app.platform_settings import SettingsCache
from conftest import wait_final


def r2(fn):
    """Test sürecinde R2 istemcisiyle bir işlem çalıştır."""
    from app.storage import storage

    async def main():
        await storage.start()
        try:
            return await fn(storage)
        finally:
            await storage.close()
    return asyncio.run(main())


def keys(prefix):
    async def f(s):
        out = []
        async for page in s.client.get_paginator("list_objects_v2").paginate(Bucket=s.bucket, Prefix=prefix):
            out += [o["Key"] for o in page.get("Contents", [])]
        return out
    return r2(f)


def sql(query, *args):
    import asyncpg

    async def main():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()
    return asyncio.run(main())


def run_purge(orphan_age=None):
    from app.storage import storage
    from app.worker import main as worker
    old = worker.ORPHAN_AGE_S
    if orphan_age is not None:
        worker.ORPHAN_AGE_S = orphan_age

    async def main():
        pool = await create_pool(max_size=2)
        await storage.start()
        try:
            return await worker.purge_media({"db": pool, "settings_cache": SettingsCache()})
        finally:
            await storage.close()
            await pool.close()
    try:
        return asyncio.run(main())
    finally:
        worker.ORPHAN_AGE_S = old


def upload(api, path):
    r = api.post("/v1/moderate/upload?wait=true", files={"file": (path.name, path.read_bytes())})
    assert r.status_code in (200, 202), r.text
    return wait_final(api, r.json()["id"], 120)


def test_original_deleted_from_r2_after_processing_evidence_small(api, owner, media, project):
    clean = upload(api, media["clean"])
    blocked = upload(api, media["meme"])
    assert keys(f"incoming/{project['id']}/") == []                     # orijinaller R2'den silindi
    assert owner.get(f"/decisions/{clean['id']}").json()["evidence"] == []
    ev = owner.get(f"/decisions/{blocked['id']}").json()["evidence"]
    key = sql("SELECT path FROM media_evidence WHERE id = $1::uuid", ev[0]["id"])[0]["path"]
    assert key.startswith("evidence/")
    meta = r2(lambda s: s.head(key))
    assert meta and meta["size"] < 200_000 and meta["content_type"] == "image/jpeg"
    img = owner.get(f"/media/evidence/{ev[0]['id']}")                  # panel R2'den aktarır
    assert img.status_code == 200 and img.content[:3] == b"\xff\xd8\xff"


def test_presigned_direct_upload_flow(api, project, media):
    data = media["meme"].read_bytes()
    r = api.post("/v1/uploads", json={"content_type": "image/jpeg", "size": len(data)})
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["method"] == "PUT" and up["expires_in"] == 900
    put = httpx.put(up["upload_url"], content=data, headers=up["headers"])   # doğrudan R2'ye (sunucuya uğramaz)
    assert put.status_code == 200
    body = api.post("/v1/moderate?wait=true", json={"type": "image", "upload_id": up["upload_id"]}).json()
    body = wait_final(api, body["id"], 120)
    assert body["decision"] == "block"
    assert keys(f"incoming/{project['id']}/") == []                     # işlendikten sonra silindi
    assert api.post("/v1/moderate", json={"type": "image", "upload_id": up["upload_id"]}).status_code == 422
    big = api.post("/v1/uploads", json={"content_type": "video/mp4", "size": settings.max_video_bytes + 1})
    assert big.status_code == 413
    bad = api.post("/v1/uploads", json={"content_type": "application/x-sh", "size": 10})
    assert bad.status_code == 422


def test_upload_id_is_scoped_to_project(owner, api, project, media):
    import secrets

    from conftest import api_client
    slug = f"diger-{secrets.token_hex(3)}"
    other = owner.post("/projects", {"name": slug, "slug": slug}).json()
    try:
        data = media["clean"].read_bytes()
        up = api.post("/v1/uploads", json={"content_type": "image/jpeg", "size": len(data)}).json()
        httpx.put(up["upload_url"], content=data, headers=up["headers"])
        stranger = api_client(other["api_key"])
        r = stranger.post("/v1/moderate", json={"type": "image", "upload_id": up["upload_id"]})
        assert r.status_code == 422 and r.json()["detail"]["error"] == "upload_not_found"
        mine = api.post("/v1/moderate?wait=true", json={"type": "image", "upload_id": up["upload_id"]}).json()
        assert wait_final(api, mine["id"], 120)["decision"] == "allow"      # sahibi kullanabilir
    finally:
        owner.delete(f"/projects/{other['id']}", {"confirm_slug": slug})


def test_large_video_streams_to_r2_in_parts(api, project, tmp_path):
    big = tmp_path / "buyuk.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "anoisesrc=d=8", "-t", "8", "-c:v", "libx264", "-b:v", "14M", "-maxrate", "14M",
                    "-bufsize", "14M", "-pix_fmt", "yuv420p", "-c:a", "aac", str(big)], check=True, timeout=180)
    assert big.stat().st_size > 9 * 1024 * 1024                           # 8 MB'lık parçalardan fazlası
    body = upload(api, big)
    assert body["status"] == "completed" and body["type"] == "video"
    assert keys(f"incoming/{project['id']}/") == []


def test_expired_evidence_and_unused_uploads_are_removed(api, owner, media, project):
    blocked = upload(api, media["meme"])
    ev_id = owner.get(f"/decisions/{blocked['id']}").json()["evidence"][0]["id"]
    key = sql("SELECT path FROM media_evidence WHERE id = $1::uuid", ev_id)[0]["path"]
    sql("UPDATE moderation_requests SET completed_at = now() - interval '100 hours' WHERE public_id = $1", blocked["id"])
    data = media["clean"].read_bytes()
    up = api.post("/v1/uploads", json={"content_type": "image/jpeg", "size": len(data)}).json()
    httpx.put(up["upload_url"], content=data, headers=up["headers"])      # yüklendi ama moderasyona gönderilmedi
    time.sleep(1.2)
    removed = run_purge(orphan_age=1)
    assert removed["evidence"] >= 1 and removed["incoming"] >= 1
    assert r2(lambda s: s.head(key)) is None
    assert keys(f"incoming/{project['id']}/") == []
    assert owner.get(f"/decisions/{blocked['id']}").json()["ai_decision"] == "block"   # karar kaydı kalır


def test_zero_retention_keeps_evidence_only_for_pending_review(api, owner, media):
    owner.patch("/settings", {"evidence_retention_hours": 0})
    time.sleep(6)
    try:
        blocked = upload(api, media["meme"])
        assert blocked["decision"] == "block"
        assert owner.get(f"/decisions/{blocked['id']}").json()["evidence"] == []
        review = upload(api, media["mild"])                             # üç adımlı test projesi: incele
        assert len(owner.get(f"/decisions/{review['id']}").json()["evidence"]) == 1
    finally:
        owner.patch("/settings", {"evidence_retention_hours": 72})
        time.sleep(6)


def test_media_rejected_when_r2_unavailable_text_still_works(project):
    from test_resilience import run_app

    async def t(client, db):
        h = {"Authorization": f"Bearer {project['key']}"}
        media_r = await client.post("/v1/moderate", json={"type": "image", "url": "https://example.com/a.jpg"}, headers=h)
        text_r = await client.post("/v1/moderate", json={"type": "text", "text": "merhaba"}, headers=h)
        return media_r, text_r
    media_r, text_r = run_app(t, "redis_down")           # bu süreçte R2 istemcisi başlatılmadı
    body = media_r.json()
    assert media_r.status_code == 503 and body["error"] == "media_storage_not_configured" and body["retryable"] is True
    assert text_r.status_code == 200 and text_r.json()["status"] == "completed"
