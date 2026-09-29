"""Akıllı IP koruması: tarama, anahtar denemesi, panel girişi; izleme modu, izin listesi, kademeli süre."""
import random
import time

import httpx
import pytest

from conftest import ADMIN, API, CSRF, api_client


def public_ip() -> str:
    return f"45.{random.randint(1, 250)}.{random.randint(1, 250)}.{random.randint(1, 250)}"


def set_settings(owner, **values):
    r = owner.patch("/settings", values)
    assert r.status_code == 200, r.text
    time.sleep(6)   # API/işçi ayar önbelleği


@pytest.fixture(scope="module")
def strict(owner):
    set_settings(owner, autoban_mode="enforce", autoban_scan_limit=5, autoban_auth_fail_limit=5,
                 autoban_panel_login_limit=5, autoban_allowlist=[])
    yield owner
    set_settings(owner, autoban_mode="enforce", autoban_scan_limit=30, autoban_auth_fail_limit=50,
                 autoban_panel_login_limit=20, autoban_allowlist=[])


def rule_for(owner, ip):
    return next((r for r in owner.get("/ip-rules?scope=global").json() if r["cidr"] in (ip, f"{ip}/32")), None)


def test_scanner_is_banned_even_with_valid_key_and_unban_works(strict, project):
    ip = public_ip()
    bot = httpx.Client(base_url=API, headers={"X-Forwarded-For": ip})
    for path in ["/wp-admin", "/.env", "/phpmyadmin", "/admin.php", "/.git/config"]:
        assert bot.get(path).status_code == 404
    blocked = api_client(project["key"], ip).post("/v1/moderate", json={"type": "text", "text": "merhaba"})
    assert blocked.status_code == 403 and blocked.json()["detail"]["error"] == "ip_blocked"
    rule = rule_for(strict, ip)
    assert rule and rule["source"] == "auto" and "tarama" in rule["reason"] and rule["expires_at"]
    events = strict.get("/security/autoban-events").json()["events"]
    assert any(e["ip"] == ip and e["action"] == "banned" and e["minutes"] == 15 for e in events)
    # Panelden kaldırınca hemen açılır
    assert strict.delete(f"/ip-rules/{rule['id']}").status_code == 200
    assert api_client(project["key"], ip).post("/v1/moderate", json={"type": "text", "text": "merhaba"}).status_code == 202
    # İkinci suç: süre uzar (15 dk → 60 dk)
    for path in ["/a", "/b", "/c", "/d", "/e"]:
        bot.get(path)
    events = strict.get("/security/autoban-events").json()["events"]
    assert any(e["ip"] == ip and e["action"] == "banned" and e["minutes"] == 60 for e in events)
    strict.delete(f"/ip-rules/{rule_for(strict, ip)['id']}")


def test_key_guessing_is_banned(strict, project):
    ip = public_ip()
    guesser = httpx.Client(base_url=API, headers={"X-Forwarded-For": ip})
    for _ in range(5):
        guesser.post("/v1/moderate", json={"type": "text", "text": "x"}, headers={"Authorization": "Bearer mk_live_" + "q" * 40})
    assert api_client(project["key"], ip).get("/v1/moderate/x").status_code == 403
    strict.delete(f"/ip-rules/{rule_for(strict, ip)['id']}")


def test_panel_login_bruteforce_is_banned(strict):
    ip = public_ip()
    attacker = httpx.Client(base_url=ADMIN, headers=CSRF | {"X-Forwarded-For": ip})
    for _ in range(5):
        attacker.post("/auth/login", json={"email": "yok@moderasyon-test.com", "password": "yanlis"})
    r = attacker.post("/auth/login", json={"email": "yok@moderasyon-test.com", "password": "yanlis"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "ip_blocked"
    strict.delete(f"/ip-rules/{rule_for(strict, ip)['id']}")


def test_monitor_mode_and_allowlist_do_not_ban(strict, project):
    set_settings(strict, autoban_mode="monitor")
    ip = public_ip()
    bot = httpx.Client(base_url=API, headers={"X-Forwarded-For": ip})
    for path in ["/x1", "/x2", "/x3", "/x4", "/x5"]:
        bot.get(path)
    assert api_client(project["key"], ip).post("/v1/moderate", json={"type": "text", "text": "a"}).status_code == 202
    events = strict.get("/security/autoban-events").json()["events"]
    assert any(e["ip"] == ip and e["action"] == "monitored" for e in events) and rule_for(strict, ip) is None

    safe = public_ip()
    set_settings(strict, autoban_mode="enforce", autoban_allowlist=[f"{safe.rsplit('.', 1)[0]}.0/24"])
    bot = httpx.Client(base_url=API, headers={"X-Forwarded-For": safe})
    for path in ["/y1", "/y2", "/y3", "/y4", "/y5"]:
        bot.get(path)
    assert api_client(project["key"], safe).post("/v1/moderate", json={"type": "text", "text": "a"}).status_code == 202
    events = strict.get("/security/autoban-events").json()["events"]
    assert any(e["ip"] == safe and e["action"] == "skipped_allowlist" for e in events)

    bad = strict.patch("/settings", {"autoban_allowlist": ["bu-bir-ip-degil"]})
    assert bad.status_code == 422


def test_admin_email_login_and_bootstrap_link(owner):
    me = owner.get("/auth/me").json()
    assert "@" in me["email"]
    tag = random.randint(1000, 999999)
    r = owner.post("/admins", {"email": f"yeni.moderator{tag}@moderasyon-test.com", "role": "moderator"})
    assert r.status_code == 201, r.text
    c = httpx.Client(base_url=ADMIN, headers=CSRF | {"X-Forwarded-For": public_ip()})
    ok = c.post("/auth/login", json={"email": f"Yeni.Moderator{tag}@moderasyon-test.com", "password": r.json()["temporary_password"]})
    assert ok.status_code == 200 and ok.json()["email"] == f"yeni.moderator{tag}@moderasyon-test.com"
    assert owner.post("/admins", {"email": f"yeni.moderator{tag}@moderasyon-test.com", "role": "viewer"}).status_code == 409
    assert owner.post("/admins", {"email": "gecersiz", "role": "viewer"}).status_code == 422
