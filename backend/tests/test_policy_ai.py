"""Proje policy'si, Layer 2 AI (smart/always, hata güvenliği), KVKK silme API'si ve kalite ölçümü.

Layer 2 testleri için stack AI_BASE_URL=http://127.0.0.1:18999/v1 ile çalışmalı; bu dosya o portta
OpenAI uyumlu sahte bir model sunucusu başlatır.
"""
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import api_client, wait_final

AI_PORT = int(os.environ.get("TEST_AI_PORT", "18999"))
AI_ON = os.environ.get("AI_BASE_URL", "").startswith(f"http://127.0.0.1:{AI_PORT}")


class _FakeModel(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "forbidden visual content" in system:
            import re
            ids = re.findall(r'- "([0-9a-f-]{36})"', system)
            _FakeModel.calls.append({"text": "visual", "image": True, "model": body["model"]})
            reply = {"choices": [{"message": {"content": json.dumps({"matches": {i: 0.95 for i in ids}})}}],
                     "usage": {"prompt_tokens": 900, "completion_tokens": 40}}
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        text = " ".join(p.get("text", "") for p in user if p["type"] == "text")
        has_image = any(p["type"] == "image_url" for p in user)
        _FakeModel.calls.append({"text": text, "image": has_image, "model": body["model"]})
        if "AI_HATA" in text:
            self.send_response(500)
            self.end_headers()
            return
        scores = {"harassment": 0.02}
        if "BELIRSIZ_KOTU" in text:
            scores = {"harassment": 0.96}
        if has_image:
            scores = {"weapons": 0.95}
        reply = {"choices": [{"message": {"content": json.dumps({"scores": scores, "reason": "test"})}}]}
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def fake_model(owner):
    import time
    server = ThreadingHTTPServer(("127.0.0.1", AI_PORT), _FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    before = owner.get("/ai/overview").json()["settings"]["ai_provider"]
    owner.patch("/ai/settings", {"ai_provider": "chat", "ai_enabled": True})
    owner.patch("/settings", {"visual_rules_enabled": False})   # sadece kendi testinde açılır
    owner.post("/ai/circuit/reset")
    time.sleep(6)          # işçilerin ayar önbelleği (5 sn)
    yield _FakeModel
    owner.patch("/ai/settings", {"ai_provider": before, "ai_enabled": False})
    time.sleep(6)
    server.shutdown()
    server.server_close()


@pytest.fixture()
def fresh(owner):
    slug = f"pol-{secrets.token_hex(4)}"
    data = owner.post("/projects", {"name": slug, "slug": slug}).json()
    owner.client.put(f"/projects/{data['id']}/policy", json={"decision_mode": "three_step", "ai_mode": "off"})
    yield {"id": data["id"], "api": api_client(data["api_key"])}
    owner.delete(f"/projects/{data['id']}", {"confirm_slug": slug})


def moderate(api, text):
    return api.post("/v1/moderate?wait=true", json={"type": "text", "text": text}).json()


# ---------------------------------------------------------------- proje policy'si

def test_category_allow_and_block_overrides(owner, fresh):
    api = fresh["api"]
    assert moderate(api, "salak aptal herif")["decision"] == "review"           # varsayılan: 0.75 < 0.85
    r = owner.client.put(f"/projects/{fresh['id']}/policy", json={"categories": {"harassment": {"action": "allow"}}})
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert moderate(api, "amk siktir")["decision"] == "allow"                    # bu projede hakaret serbest

    owner.client.put(f"/projects/{fresh['id']}/policy", json={"categories": {"harassment": {"action": "block"}}})
    body = moderate(api, "salak mısın")                                           # 0.60: normalde incele
    assert body["decision"] == "block"
    assert owner.get(f"/decisions/{body['id']}").json()["policy_version"].endswith(".p3")


def test_category_thresholds(owner, fresh):
    owner.client.put(f"/projects/{fresh['id']}/policy",
                     json={"decision_mode": "three_step", "categories": {"harassment": {"review": 0.7, "block": 0.95}}})
    assert moderate(fresh["api"], "salak mısın")["decision"] == "allow"          # 0.60 < 0.70
    assert moderate(fresh["api"], "amk")["decision"] == "review"                 # 0.90 < 0.95
    bad = owner.client.put(f"/projects/{fresh['id']}/policy", json={"categories": {"harassment": {"review": 0.9, "block": 0.5}}})
    assert bad.status_code == 422
    unknown = owner.client.put(f"/projects/{fresh['id']}/policy", json={"categories": {"yok_boyle": {"action": "block"}}})
    assert unknown.status_code == 422


def test_policy_requires_admin(owner, fresh):
    from conftest import create_admin
    mod = create_admin("moderator")
    assert mod.client.put(f"/projects/{fresh['id']}/policy", json={"categories": {}}).status_code == 403


# ---------------------------------------------------------------- Layer 2

@pytest.mark.skipif(not AI_ON, reason="AI_BASE_URL sahte model sunucusunu göstermeli")
def test_ai_smart_mode_resolves_uncertain_only(owner, fresh, fake_model):
    owner.client.put(f"/projects/{fresh['id']}/policy", json={"ai_mode": "smart", "decision_mode": "two_step"})
    api = fresh["api"]
    before = len(fake_model.calls)
    assert moderate(api, "Harika bir gün")["decision"] == "allow"
    assert len(fake_model.calls) == before                                      # emin olunan içerik AI'ya gitmez

    clean = moderate(api, "salak BELIRSIZ_TEMIZ")                               # Layer 1: incele → AI: temiz
    assert clean["decision"] == "allow"
    detail = owner.get(f"/decisions/{clean['id']}").json()
    assert detail["ai_used"] is True and detail["layer1_decision"] == "review"

    assert moderate(api, "salak BELIRSIZ_KOTU")["decision"] == "block"          # AI: kötü
    failed = moderate(api, "salak AI_HATA")                                      # AI hata verdi
    assert failed["decision"] == "block"                                         # iki adım: belirsiz + AI yok → engel
    assert owner.get(f"/decisions/{failed['id']}").json()["ai_used"] is False

    # Tetikleme eşiği: risk 0.60 < 0.70 → AI'ya hiç sorulmaz
    owner.client.put(f"/projects/{fresh['id']}/policy", json={"ai_mode": "smart", "ai_trigger": 0.7})
    before = len(fake_model.calls)
    assert moderate(api, "salak BELIRSIZ_TEMIZ")["decision"] == "block"
    assert len(fake_model.calls) == before


@pytest.mark.skipif(not AI_ON, reason="AI_BASE_URL sahte model sunucusunu göstermeli")
def test_ai_always_mode_adds_image_detection(owner, fresh, fake_model, media):
    owner.client.put(f"/projects/{fresh['id']}/policy", json={"ai_mode": "always"})
    r = fresh["api"].post("/v1/moderate/upload?wait=true", files={"file": ("c.jpg", media["clean"].read_bytes())})
    body = wait_final(fresh["api"], r.json()["id"], 90)
    assert body["decision"] == "block" and body["reason"] == "weapons_detected"   # Layer 1 görmedi, AI ekledi
    assert fake_model.calls[-1]["image"] is True


def test_quality_endpoint(owner, fresh):
    body = moderate(fresh["api"], "salak mısın")
    item = next(i for i in owner.get("/review").json() if i["public_id"] == body["id"])
    owner.post(f"/review/{item['id']}", {"decision": "allow"})
    q = owner.get(f"/stats/quality?range=24h&project_id={fresh['id']}").json()
    row = next(c for c in q["categories"] if c["category"] == "harassment")
    assert row["reviewed"] >= 1 and row["human_allow"] >= 1
    assert "ai_calls" in q["ai"]


# ---------------------------------------------------------------- KVKK silme API'si

def test_delete_single_content(owner, fresh):
    body = moderate(fresh["api"], "silinecek içerik amk")
    assert fresh["api"].delete(f"/v1/moderate/{body['id']}").status_code == 204
    detail = owner.get(f"/decisions/{body['id']}").json()
    assert detail["content_text"] is None and detail["content_purged_at"]
    assert fresh["api"].get(f"/v1/moderate/{body['id']}").json()["decision"] == "block"   # karar kalır
    assert fresh["api"].delete("/v1/moderate/mod_yok").status_code == 404


def test_delete_user_data(owner, fresh):
    uid = "user-" + secrets.token_hex(3)
    for t in ("birinci", "ikinci salak"):
        fresh["api"].post("/v1/moderate?wait=true", json={"type": "text", "text": t, "user_id": uid})
    other = api_client(owner.post("/projects", {"name": "x" + uid, "slug": "x" + uid}).json()["api_key"])
    other.post("/v1/moderate?wait=true", json={"type": "text", "text": "başka proje", "user_id": uid})

    r = fresh["api"].delete(f"/v1/users/{uid}/data")
    assert r.status_code == 200 and r.json()["deleted"] == 2
    found = owner.get(f"/decisions?q={uid}").json()["items"]
    assert len(found) == 1                                                        # sadece diğer projenin kaydı kalır
    assert owner.get(f"/review?project_id={fresh['id']}").json() == []            # incelemeden de çıktı


@pytest.mark.skipif(not AI_ON, reason="AI_BASE_URL sahte model sunucusunu göstermeli")
def test_visual_rule_blocks_media_with_label_and_legal_hold(owner, fresh, fake_model, media):
    """Yasaklı sembol kuralı: AI görsel kontrolü eşleşme bildirince içerik kritik etiketle engellenir."""
    import time
    owner.client.put(f"/projects/{fresh['id']}/policy", json={"ai_mode": "off"})
    owner.patch("/settings", {"visual_rules_enabled": True})
    time.sleep(16)
    before = len(fake_model.calls)
    r = fresh["api"].post("/v1/moderate/upload?wait=true", files={"file": ("c.jpg", media["clean"].read_bytes())})
    body = wait_final(fresh["api"], r.json()["id"], 90)
    assert body["decision"] == "block" and body["severity"] == "critical"
    assert "terör:pkk_kck" in body["labels"]
    assert len(fake_model.calls) > before                        # AI görsel kontrolü çağrıldı
    assert any(h["public_id"] == body["id"] for h in owner.get("/legal-holds").json()["items"])
    owner.delete(f"/legal-holds/{body['id']}", {"confirm_id": body["id"]})
    owner.patch("/settings", {"visual_rules_enabled": False})
    time.sleep(6)
