"""Public API güvenlik ve doğruluk testleri."""
import secrets
import time

import httpx

from conftest import API, api_client, rand_ip, wait_final


def test_missing_and_invalid_key():
    c = httpx.Client(base_url=API, headers={"X-Forwarded-For": rand_ip()})
    assert c.post("/v1/moderate", json={"type": "text", "text": "x"}).status_code == 401
    r = c.post("/v1/moderate", json={"type": "text", "text": "x"}, headers={"Authorization": "Bearer mk_live_" + "a" * 40})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "invalid_api_key"


def test_invalid_key_bruteforce_is_throttled_per_ip():
    ip = rand_ip()
    c = httpx.Client(base_url=API, headers={"X-Forwarded-For": ip})
    codes = [c.get("/v1/moderate/x", headers={"Authorization": "Bearer mk_live_" + secrets.token_hex(20)}).status_code
             for _ in range(35)]
    assert codes[0] == 401 and codes[-1] == 429
    # Başka bir IP etkilenmez
    other = httpx.Client(base_url=API, headers={"X-Forwarded-For": rand_ip()})
    assert other.get("/v1/moderate/x", headers={"Authorization": "Bearer mk_live_" + "b" * 40}).status_code == 401


def test_text_decisions(api):
    ok = api.post("/v1/moderate?wait=true", json={"type": "text", "text": "Harika bir ürün, teşekkürler"}).json()
    assert ok["decision"] == "allow"
    bad = api.post("/v1/moderate?wait=true", json={"type": "text", "text": "s1kt1r git amk"}).json()
    assert bad["decision"] == "block"
    mild = api.post("/v1/moderate?wait=true", json={"type": "text", "text": "salak mısın"}).json()
    assert mild["decision"] == "review"
    # İstemciye iç detay (skor, model) dönmez
    assert set(bad) <= {"id", "type", "status", "decision", "reason", "created_at", "labels", "severity"}
    assert "score" not in str(bad) and "model" not in str(bad)          # skor ve model bilgisi sızmaz


def test_validation_error_does_not_echo_content(api):
    secret_text = "GIZLI-" + secrets.token_hex(8)
    r = api.post("/v1/moderate", json={"type": "text", "text": secret_text, "metadata": {"x": "y" * 5000}})
    assert r.status_code == 422
    assert secret_text not in r.text


def test_chunked_body_over_limit_is_rejected(api):
    def gen():
        for _ in range(40):
            yield b'{"type":"text","text":"' + b"a" * 10_000
    r = api.post("/v1/moderate", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_idempotency(api):
    key = "idem-" + secrets.token_hex(6)
    a = api.post("/v1/moderate", json={"type": "text", "text": "merhaba"}, headers={"Idempotency-Key": key}).json()
    b = api.post("/v1/moderate", json={"type": "text", "text": "merhaba"}, headers={"Idempotency-Key": key})
    assert b.status_code == 200 and b.json()["id"] == a["id"]
    c = api.post("/v1/moderate", json={"type": "text", "text": "farklı"}, headers={"Idempotency-Key": key})
    assert c.status_code == 409


def test_tenant_isolation(owner, api):
    slug = f"iso-{secrets.token_hex(3)}"
    other = owner.post("/projects", {"name": slug, "slug": slug}).json()
    try:
        mine = api.post("/v1/moderate", json={"type": "text", "text": "özel içerik"}).json()
        stranger = api_client(other["api_key"])
        assert stranger.get(f"/v1/moderate/{mine['id']}").status_code == 404
    finally:
        owner.delete(f"/projects/{other['id']}", {"confirm_slug": slug})


def test_x_forwarded_for_spoofing_cannot_bypass_ip_ban(owner, project):
    banned = rand_ip()
    rule = owner.post("/ip-rules", {"cidr": banned, "project_id": project["id"], "reason": "test"}).json()
    try:
        body = {"type": "text", "text": "merhaba"}
        # Gerçek istemci IP'si zincirin sağındaki güvenilmeyen adres (proxy'nin eklediği)
        assert api_client(project["key"], banned).post("/v1/moderate", json=body).status_code == 403
        assert api_client(project["key"], f"1.2.3.4, {banned}").post("/v1/moderate", json=body).status_code == 403
        # Saldırgan sola sahte adres eklese de kendi adresi kullanılır; soldaki sahte adres yok sayılır
        assert api_client(project["key"], f"{banned}, {rand_ip()}").post("/v1/moderate", json=body).status_code == 202
    finally:
        owner.delete(f"/ip-rules/{rule['id']}")


def test_service_and_project_toggles(owner, project, api):
    body = {"type": "text", "text": "merhaba"}
    owner.patch("/settings", {"service_enabled": False, "maintenance_message": "Bakım"})
    try:
        time.sleep(6)  # API ayar önbelleği 5 sn
        r = api.post("/v1/moderate", json=body)
        assert r.status_code == 503 and r.json()["detail"]["message"] == "Bakım"
    finally:
        owner.patch("/settings", {"service_enabled": True})
    time.sleep(6)
    owner.patch(f"/projects/{project['id']}", {"status": "disabled"})
    try:
        assert api.post("/v1/moderate", json=body).status_code == 403
    finally:
        owner.patch(f"/projects/{project['id']}", {"status": "active"})
    assert api.post("/v1/moderate", json=body).status_code == 202


def test_unsafe_url_schemes_rejected_by_api(api):
    for url in ["file:///etc/passwd", "ftp://example.com/a.jpg"]:
        assert api.post("/v1/moderate", json={"type": "image", "url": url}).status_code == 422, url


def test_upload_requires_auth_before_reading_body():
    c = httpx.Client(base_url=API, headers={"X-Forwarded-For": rand_ip()})
    r = c.post("/v1/moderate/upload", files={"file": ("x.jpg", b"\xff\xd8\xff" + b"0" * 2_000_000)})
    assert r.status_code == 401


def test_upload_rejects_fake_and_bomb(api, media):
    r = api.post("/v1/moderate/upload", files={"file": ("x.jpg", media["fake"].read_bytes())})
    assert r.status_code == 415
    r = api.post("/v1/moderate/upload?wait=true", files={"file": ("b.png", media["bomb"].read_bytes())})
    body = wait_final(api, r.json()["id"])
    assert body["status"] == "failed" and body["error"] == "image_too_large"
