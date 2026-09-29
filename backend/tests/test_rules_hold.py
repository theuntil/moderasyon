"""Küfür seviyesi, yasaklı kelimeler, etiketli yanıt, kritik içerik, yasal saklama, kullanıcı bilgileri."""
import secrets

import pytest

from conftest import api_client, wait_final


@pytest.fixture()
def proj(owner):
    slug = f"kural-{secrets.token_hex(3)}"
    data = owner.post("/projects", {"name": slug, "slug": slug}).json()
    owner.client.put(f"/projects/{data['id']}/policy", json={"ai_mode": "off"})
    yield {"id": data["id"], "api": api_client(data["api_key"]), "slug": slug}


def text(api, t, **extra):
    return api.post("/v1/moderate?wait=true", json={"type": "text", "text": t, **extra}).json()


def test_profanity_levels(owner, proj):
    api = proj["api"]
    r = text(api, "kocam mk senin")
    assert r["decision"] == "block" and r["labels"] == ["küfür"] and r["severity"] == "normal"
    owner.client.put(f"/projects/{proj['id']}/policy", json={"ai_mode": "off", "profanity_level": "moderate"})
    assert text(api, "amk ya")["decision"] == "allow"              # orta: sokak ağzı serbest
    assert text(api, "siktir git")["decision"] == "block"          # ağır küfür yine engel
    owner.client.put(f"/projects/{proj['id']}/policy", json={"ai_mode": "off", "profanity_level": "off"})
    assert text(api, "siktir git")["decision"] == "allow"


def test_custom_terms_with_labels_and_obfuscation(owner, proj):
    tag = secrets.token_hex(2)
    r = owner.post("/rules/terms", {"term": f"yasakli{tag}", "label": "özel", "severity": "block"})
    assert r.status_code == 201
    r2 = owner.post("/rules/terms", {"term": "pkk", "label": "terör:pkk", "severity": "critical", "match_mode": "contains",
                                     "project_id": proj["id"]})
    assert r2.status_code in (201, 409)
    import time
    time.sleep(16)                                                 # kural önbelleği (15 sn)
    body = text(proj["api"], f"bu bir YASAKLI{tag.upper()} kelime")
    assert body["decision"] == "block" and "özel" in body["labels"]
    crit = text(proj["api"], "yaşasın P.K.K'lılar")               # gizleme denemesi
    assert crit["decision"] == "block" and "terör:pkk" in crit["labels"] and crit["severity"] == "critical"
    holds = owner.get("/legal-holds").json()["items"]
    assert any(h["public_id"] == crit["id"] for h in holds)        # kritik → yasal saklama
    other = api_client(owner.post("/projects", {"name": "o" + tag, "slug": "o" + tag}).json()["api_key"])
    assert text(other, "pkk hakkında haber")["decision"] != "block" or "terör:pkk" not in text(other, "pkk")["labels"]


def test_user_info_encrypted_and_visible_to_admin_only(owner, proj):
    from conftest import create_admin
    body = text(proj["api"], "siktir git", user={"id": "u-42", "name": "Ali", "surname": "Veli",
                                                  "email": "ali@ornek.com", "phone": "+905551112233"})
    assert "user" not in body and "Ali" not in str(body)          # yanıtta kişisel veri dönmez
    d = owner.get(f"/decisions/{body['id']}").json()
    assert d["user_info"]["surname"] == "Veli" and d["external_user_id"] == "u-42"
    mod = create_admin("moderator")
    assert mod.get(f"/decisions/{body['id']}").json()["user_info"] is None


def test_critical_media_is_held_with_original_and_never_purged(owner, proj, media):
    import asyncio
    import time

    tag = secrets.token_hex(2)
    owner.post("/rules/terms", {"term": f"örgüt{tag}", "label": f"terör:test{tag}", "severity": "critical"})
    time.sleep(16)
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (900, 400), "white")
    ImageDraw.Draw(im).text((40, 150), f"ÖRGÜT{tag.upper()}", fill="black",
                            font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72))
    path = media["clean"].parent / f"orgut{tag}.png"
    im.save(path)
    r = proj["api"].post("/v1/moderate/upload?wait=true", files={"file": (path.name, path.read_bytes())},
                         data={"user": '{"name": "Test", "phone": "+900000"}'})
    body = wait_final(proj["api"], r.json()["id"], 120)
    assert body["decision"] == "block" and body["severity"] == "critical" and f"terör:test{tag}" in body["labels"]
    hold = next(h for h in owner.get("/legal-holds").json()["items"] if h["public_id"] == body["id"])
    assert hold["has_original"] and hold["user_info"]["name"] == "Test" and hold["client_ip"]
    orig = owner.get(f"/legal-holds/{body['id']}/original")
    assert orig.status_code == 200 and orig.content[:4] == b"\x89PNG"      # orijinal dosya birebir
    # Saklama süresi dolsa bile silinmez
    from test_media_storage import run_purge, sql
    sql("UPDATE moderation_requests SET created_at = now() - interval '900 days', "
        "completed_at = now() - interval '900 days' WHERE public_id = $1", body["id"])
    run_purge()

    async def retention():
        from app.db import create_pool
        from app.platform_settings import SettingsCache
        from app.storage import storage
        from app.worker.main import purge_expired
        pool = await create_pool(max_size=2)
        await storage.start()
        try:
            await purge_expired({"db": pool, "settings_cache": SettingsCache()})
        finally:
            await storage.close()
            await pool.close()
    asyncio.run(retention())                                       # 30 günlük saklama süresi temizliği
    d = owner.get(f"/decisions/{body['id']}").json()
    assert d["legal_hold"] and d["evidence"] and d["client_ip"]
    # Proje silinemez; yasal saklamadan kalıcı silme çalışır
    assert owner.delete(f"/projects/{proj['id']}", {"confirm_slug": proj["slug"]}).status_code == 409
    assert owner.delete(f"/legal-holds/{body['id']}", {"confirm_id": "yanlis"}).status_code == 422
    assert owner.delete(f"/legal-holds/{body['id']}", {"confirm_id": body["id"]}).status_code == 200
    assert owner.get(f"/legal-holds/{body['id']}/original").status_code == 404


def test_visual_rules_crud(owner):
    r = owner.post("/rules/visual", {"label": "test:sembol", "description": "A test symbol drawn as a green triangle"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert owner.patch(f"/rules/visual/{rid}", {"enabled": False}).status_code == 200
    assert any(v["id"] == rid and not v["enabled"] for v in owner.get("/rules/visual").json())
    assert owner.delete(f"/rules/visual/{rid}").status_code == 200
    labels = {v["label"] for v in owner.get("/rules/visual").json()}
    assert "terör:pkk_kck" in labels                               # varsayılan kural
