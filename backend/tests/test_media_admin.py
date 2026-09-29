"""Görsel/video, engel listesi, webhook, 2FA, roller ve saklama süresi testleri."""
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pyotp
import pytest

from conftest import ADMIN, CSRF, Admin, create_admin, rand_ip, wait_final


def upload(api, path, **fields):
    r = api.post("/v1/moderate/upload?wait=true", files={"file": (path.name, path.read_bytes())}, data=fields)
    assert r.status_code in (200, 202), r.text
    return wait_final(api, r.json()["id"], timeout=120)


# ---------------------------------------------------------------- görsel / video kararları

def test_clean_image_allowed_and_not_stored(api, owner, media):
    body = upload(api, media["clean"])
    assert body["decision"] == "allow"
    detail = owner.get(f"/decisions/{body['id']}").json()
    assert detail["evidence"] == []                  # izin verilen içerik saklanmaz
    assert detail["media_width"] == 800 and detail["media_mime"] == "image/jpeg"


def test_text_in_image_is_blocked_with_evidence(api, owner, media):
    body = upload(api, media["meme"], user_id="u1", content_id="c1")
    assert body["decision"] == "block"
    detail = owner.get(f"/decisions/{body['id']}").json()
    assert any(c["name"] == "harassment" for c in detail["categories"])
    assert len(detail["evidence"]) == 1
    ev = owner.get(f"/media/evidence/{detail['evidence'][0]['id']}")
    assert ev.status_code == 200 and ev.headers["content-type"] == "image/jpeg"
    assert "sandbox" in ev.headers["content-security-policy"] and ev.headers["x-content-type-options"] == "nosniff"
    assert ev.content[:3] == b"\xff\xd8\xff"


def test_hidden_frame_in_animated_gif_is_caught(api, media):
    assert upload(api, media["gif"])["decision"] == "block"


def test_video_frame_is_caught_with_timestamp(api, owner, media):
    body = upload(api, media["video"])
    assert body["type"] == "video" and body["decision"] == "block"
    detail = owner.get(f"/decisions/{body['id']}").json()
    assert 11_000 <= detail["media_duration_ms"] <= 13_000
    stamps = [e["timestamp_ms"] for e in detail["evidence"]]
    assert any(6_500 <= s <= 8_500 for s in stamps), stamps   # küfür 7. saniyedeki karede


def test_evidence_requires_moderator(api, owner, media):
    body = upload(api, media["meme"], content_id="viewer-check")
    ev_id = owner.get(f"/decisions/{body['id']}").json()["evidence"][0]["id"]
    viewer = create_admin("viewer")
    assert viewer.get(f"/media/evidence/{ev_id}").status_code == 403
    anon = httpx.Client(base_url=ADMIN)
    assert anon.get(f"/media/evidence/{ev_id}").status_code == 401


# ---------------------------------------------------------------- engel listesi

def test_blocklist_catches_resized_copy(api, owner, media):
    first = upload(api, media["mild"])
    assert first["decision"] == "review"
    item = next(i for i in owner.get("/review").json() if i["public_id"] == first["id"])
    assert item["evidence"], "incelemedeki görselin kanıtı olmalı"
    r = owner.post(f"/review/{item['id']}", {"decision": "block", "add_to_blocklist": True, "note": "test"})
    assert r.json()["blocklisted"] is True
    # İnsan kararı istemciye yansır
    assert api.get(f"/v1/moderate/{first['id']}").json()["reason"] == "human_reviewed"

    copy = upload(api, media["mild_copy"])      # küçültülmüş + yeniden sıkıştırılmış
    assert copy["decision"] == "block" and copy["reason"] == "blocklist_match_detected"

    entry = next(b for b in owner.get("/blocklist").json() if b["source_public_id"] == first["id"])
    assert owner.get(f"/media/blocklist/{entry['id']}").status_code == 200
    assert owner.delete(f"/blocklist/{entry['id']}").status_code == 200


# ---------------------------------------------------------------- webhook

class _Receiver(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Receiver.received.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.skipif(os.environ.get("ALLOW_PRIVATE_NETWORK") != "true", reason="lokal alıcı için ALLOW_PRIVATE_NETWORK=true gerekli")
def test_webhook_signed_delivery(owner, project, api):
    from app.webhooks import verify

    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/hook"
    r = owner.patch(f"/projects/{project['id']}/webhook", {"url": url, "enabled": True})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"] or owner.post(f"/projects/{project['id']}/webhook/rotate-secret").json()["secret"]
    try:
        body = api.post("/v1/moderate?wait=true", json={"type": "text", "text": "amk", "content_id": "wh-1"}).json()
        deadline = time.time() + 20
        while time.time() < deadline and not any(json.loads(b)["data"].get("id") == body["id"] for _, b in _Receiver.received):
            time.sleep(0.3)
        headers, raw = next((h, b) for h, b in _Receiver.received if json.loads(b)["data"].get("id") == body["id"])
        payload = json.loads(raw)
        assert payload["event"] == "moderation.completed" and payload["data"]["decision"] == "block"
        assert verify(secret, headers["X-Moderation-Signature"], raw)
        assert not verify("whsec_yanlis", headers["X-Moderation-Signature"], raw)
        deliveries = owner.get(f"/projects/{project['id']}/webhook").json()["deliveries"]
        assert any(d["status"] == "delivered" for d in deliveries)
    finally:
        owner.patch(f"/projects/{project['id']}/webhook", {"enabled": False})
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- panel güvenliği

def test_admin_csrf_and_auth():
    anon = httpx.Client(base_url=ADMIN)
    assert anon.get("/projects").status_code == 401
    r = anon.post("/auth/login", json={"username": "x", "password": "y"})    # CSRF başlığı yok
    assert r.status_code == 403


def test_roles_and_session_revocation(owner, project):
    mod = create_admin("moderator")
    assert mod.get(f"/projects/{project['id']}/keys").status_code == 403
    assert mod.patch("/settings", {"service_enabled": False}).status_code == 403
    assert mod.get("/review").status_code == 200
    target = next(a for a in owner.get("/admins").json() if a["username"] == mod.username)
    owner.patch(f"/admins/{target['id']}", {"role": "viewer"})
    assert mod.get("/review").status_code == 401        # rol değişince oturum düşer


def test_two_factor_login_flow(owner):
    user = create_admin("admin")
    setup = user.post("/auth/2fa/setup").json()
    totp = pyotp.TOTP(setup["secret"])
    enabled = user.post("/auth/2fa/enable", {"code": totp.now()})
    assert enabled.status_code == 200
    recovery = enabled.json()["recovery_codes"]
    assert len(recovery) == 10

    fresh = Admin(user.username, user.password)
    step1 = fresh.client.post("/auth/login", json={"email": user.username, "password": user.password}).json()
    assert step1.get("mfa_required") is True and "id" not in step1         # şifre tek başına yetmez
    assert fresh.get("/auth/me").status_code == 401
    bad = fresh.client.post("/auth/mfa", json={"mfa_token": step1["mfa_token"], "code": "000000"})
    assert bad.status_code == 401
    # Aynı kod ikinci kez kullanılamaz (enable sırasında kullanıldı) → kurtarma kodu ile giriş
    ok = fresh.client.post("/auth/mfa", json={"mfa_token": step1["mfa_token"], "code": recovery[0]})
    assert ok.status_code == 200 and ok.json()["recovery_codes_left"] == 9
    assert fresh.get("/auth/me").status_code == 200

    again = Admin(user.username, user.password)
    t = again.client.post("/auth/login", json={"email": user.username, "password": user.password}).json()
    reused = again.client.post("/auth/mfa", json={"mfa_token": t["mfa_token"], "code": recovery[0]})
    assert reused.status_code == 401                                        # kurtarma kodu tek kullanımlık
    next_code = totp.at(time.time() + 30)                                   # uygulamadaki bir sonraki kod
    assert again.client.post("/auth/mfa", json={"mfa_token": t["mfa_token"], "code": next_code}).status_code == 200

    third = Admin(user.username, user.password)
    t3 = third.client.post("/auth/login", json={"email": user.username, "password": user.password}).json()
    replay = third.client.post("/auth/mfa", json={"mfa_token": t3["mfa_token"], "code": next_code})
    assert replay.status_code == 401                                        # aynı TOTP kodu ikinci kez geçmez


def test_login_lockout():
    user = create_admin("viewer")
    c = httpx.Client(base_url=ADMIN, headers=CSRF | {"X-Forwarded-For": rand_ip()})
    codes = [c.post("/auth/login", json={"email": user.username, "password": "yanlis"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 423


# ---------------------------------------------------------------- saklama süresi (KVKK)

def test_retention_purges_content_and_evidence(api, owner, media):
    import asyncpg

    from app.worker.main import purge_expired
    from app.db import create_pool
    from app.platform_settings import SettingsCache

    body = upload(api, media["meme"], content_id="retention")
    text = api.post("/v1/moderate?wait=true", json={"type": "text", "text": "amk retention"}).json()
    detail = owner.get(f"/decisions/{body['id']}").json()
    assert detail["evidence"]

    async def run():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await conn.execute(
            "UPDATE moderation_requests SET created_at = now() - interval '400 days' WHERE public_id = ANY($1::text[])",
            [body["id"], text["id"]],
        )
        await conn.close()
        from app.storage import storage
        pool = await create_pool(max_size=2)
        await storage.start()
        await purge_expired({"db": pool, "settings_cache": SettingsCache()})
        await storage.close()
        await pool.close()

    asyncio.run(run())
    after = owner.get(f"/decisions/{body['id']}").json()
    assert after["evidence"] == [] and after["content_purged_at"] and after["client_ip"] is None
    assert owner.get(f"/media/evidence/{detail['evidence'][0]['id']}").status_code == 404
    assert owner.get(f"/decisions/{text['id']}").json()["content_text"] is None
    # Karar kaydı kalır (istatistik), içerik gider
    assert after["ai_decision"] == "block"


def test_small_text_in_high_resolution_screen_recording_is_caught(api, tmp_path):
    """Retina ekran kaydı (2880x1800) içindeki normal boyutlu yazı (24 px) da okunmalı."""
    import subprocess

    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (2880, 1800), (30, 30, 30))
    ImageDraw.Draw(im).text((460, 420), "SİKTİR GİT AMK", fill=(235, 235, 235),
                            font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24))
    im.save(tmp_path / "kare.png")
    video = tmp_path / "kayit.mov"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-i", str(tmp_path / "kare.png"), "-t", "3",
                    "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=120)
    assert upload(api, video)["decision"] == "block"


def test_screen_recording_with_vulgar_abbreviation_is_blocked(api, tmp_path):
    """Kullanıcının gerçek testi: Retina ekran kaydında 'kocam mk senin' yazısı."""
    import subprocess

    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (2880, 1800), (255, 255, 255))
    ImageDraw.Draw(im).text((500, 500), "kocam mk senin", fill=(20, 20, 20),
                            font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28))
    im.save(tmp_path / "k.png")
    video = tmp_path / "kayit2.mov"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-i", str(tmp_path / "k.png"), "-t", "3",
                    "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=120)
    assert upload(api, video)["decision"] == "block"
