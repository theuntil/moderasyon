"""Panel girişi: e-posta, env ile kurtarma (ADMIN_RESET_PASSWORD), kilit, boşluk ve teşhis kayıtları."""
import asyncio
import secrets

import httpx

from app.config import settings
from conftest import ADMIN, CSRF, Admin, create_admin, rand_ip


def login(email: str, password: str) -> httpx.Response:
    c = httpx.Client(base_url=ADMIN, headers=CSRF | {"X-Forwarded-For": rand_ip()})
    return c.post("/auth/login", json={"email": email, "password": password})


def run_bootstrap(monkeypatch, **env):
    from app.admin.main import bootstrap_owner
    from app.db import create_pool
    for k, v in env.items():
        monkeypatch.setattr(settings, k, v)

    async def main():
        pool = await create_pool(max_size=2)
        try:
            await bootstrap_owner(pool)
        finally:
            await pool.close()
    asyncio.run(main())


def test_env_password_no_longer_valid_after_first_change_and_env_reset_fixes_it(monkeypatch):
    user = create_admin("owner")                      # geçici şifre → ilk girişte yeni şifre belirlendi
    assert login(user.username, user.password).status_code == 200
    env_pw = "EnvSifresi2026x"
    assert login(user.username, env_pw).status_code == 401   # sorunun kendisi: env şifresi artık geçerli değil

    run_bootstrap(monkeypatch, admin_email=user.username, admin_password=env_pw, admin_reset_password=True)
    assert login(user.username, env_pw).status_code == 200    # env ile sıfırlandı
    assert login(user.username, user.password).status_code == 401

    # Kullanıcı panelde yeni şifre belirler; yeniden başlatma (aynı env) şifreyi EZMEZ
    a = Admin(user.username, env_pw)
    a.login()
    new_pw = "PaneldenYeni2026x"
    assert a.post("/auth/change-password", {"current_password": env_pw, "new_password": new_pw}).status_code == 200
    run_bootstrap(monkeypatch, admin_email=user.username, admin_password=env_pw, admin_reset_password=True)
    assert login(user.username, new_pw).status_code == 200


def test_env_reset_unlocks_locked_account_and_strips_whitespace(monkeypatch):
    user = create_admin("admin")
    for _ in range(5):
        login(user.username, "yanlis")
    locked = login(user.username, user.password)
    assert locked.status_code == 423                            # doğru şifre de kilitte reddedilir
    run_bootstrap(monkeypatch, admin_email=f"  {user.username.upper()}  ", admin_password="  Bosluklu2026Sifre \n",
                  admin_reset_password=True)
    assert login(user.username, "Bosluklu2026Sifre").status_code == 200


def test_env_reset_creates_missing_owner_and_weak_password_forces_change(monkeypatch):
    email = f"kurtarma.{secrets.token_hex(3)}@moderasyon-test.com"
    assert login(email, "Nanda.6936").status_code == 401
    run_bootstrap(monkeypatch, admin_email=email, admin_password="Nanda.6936", admin_reset_password=True)
    r = login(email, "Nanda.6936")
    assert r.status_code == 200 and r.json()["must_change_password"] is True and r.json()["role"] == "owner"


def test_reset_flag_off_changes_nothing(monkeypatch):
    user = create_admin("viewer")
    run_bootstrap(monkeypatch, admin_email=user.username, admin_password="BaskaSifre2026x", admin_reset_password=False)
    assert login(user.username, user.password).status_code == 200
    assert login(user.username, "BaskaSifre2026x").status_code == 401


def test_failed_login_reason_is_audited(owner):
    user = create_admin("viewer")
    login(user.username, "yanlis-sifre")
    items = owner.get("/audit?action=auth.login_failed&limit=20").json()["items"]
    assert any(i["details"].get("reason") == "wrong_password" and i["actor_email"] == user.username for i in items)
