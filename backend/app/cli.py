"""Panel gelene kadar project ve API key yönetimi için komut satırı aracı.

Kullanım (api container terminalinden, /srv klasöründe):
  python -m app.cli create-admin --email sen@alanadi.com --role owner
  python -m app.cli reset-admin-password --email sen@alanadi.com
  docker compose run --rm api python -m app.cli create-project --name KAYS --slug kays
  docker compose run --rm api python -m app.cli create-key --project kays --name "KAYS Development" --env test
  docker compose run --rm api python -m app.cli list-keys --project kays
  docker compose run --rm api python -m app.cli revoke-key --id <key-uuid>
"""
import argparse
import asyncio
from uuid import UUID

from app.db import create_pool
from app.security import api_key_prefix, generate_api_key, hash_api_key


async def _create_key(conn, project_id, name: str, env: str) -> str:
    key = generate_api_key(env)
    await conn.execute(
        """
        INSERT INTO project_api_keys (project_id, name, key_prefix, key_hash, environment)
        VALUES ($1, $2, $3, $4, $5)
        """,
        project_id, name, api_key_prefix(key), hash_api_key(key), env,
    )
    return key


def _print_key_once(key: str) -> None:
    print("\nAPI Key (sadece bir kez gösterilir, şimdi kaydet):\n")
    print(f"  {key}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-project")
    p.add_argument("--name", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--description")
    p.add_argument("--env", choices=["live", "test"], default="test")

    k = sub.add_parser("create-key")
    k.add_argument("--project", required=True, help="project slug")
    k.add_argument("--name", required=True)
    k.add_argument("--env", choices=["live", "test"], required=True)

    lk = sub.add_parser("list-keys")
    lk.add_argument("--project", required=True)

    r = sub.add_parser("revoke-key")
    r.add_argument("--id", required=True)

    a = sub.add_parser("create-admin", help="Panel yöneticisi oluştur")
    a.add_argument("--email", help="Panel girişi e-postayla yapılır")
    a.add_argument("--username", help="Boşsa e-posta kullanılır")
    a.add_argument("--name")
    a.add_argument("--role", choices=["owner", "admin", "moderator", "viewer"], default="owner")

    sub.add_parser("admin-status", help="Panel yöneticilerinin durumu (giriş sorunlarını teşhis için)")
    sub.add_parser("r2-check", help="Cloudflare R2 bağlantısını adım adım test et (medya sorunlarını teşhis için)")
    ub = sub.add_parser("unban-ip", help="Bir IP'nin engelini kaldır (otomatik veya elle)")
    ub.add_argument("--ip", required=True)

    rp = sub.add_parser("reset-admin-password", help="Yöneticiye geçici şifre ver, kilidi aç (panel erişimi kaybolursa)")
    rp.add_argument("--email", help="veya --username")
    rp.add_argument("--username")

    args = parser.parse_args()
    pool = await create_pool(max_size=2)
    try:
        async with pool.acquire() as conn:
            if args.cmd == "create-project":
                async with conn.transaction():
                    project_id = await conn.fetchval(
                        "INSERT INTO projects (name, slug, description) VALUES ($1, $2, $3) RETURNING id",
                        args.name, args.slug, args.description,
                    )
                    key = await _create_key(conn, project_id, f"{args.name} Default", args.env)
                print(f"Project oluşturuldu: {args.name} ({args.slug}) id={project_id}")
                _print_key_once(key)

            elif args.cmd == "create-key":
                project_id = await conn.fetchval("SELECT id FROM projects WHERE slug = $1", args.project)
                if project_id is None:
                    raise SystemExit(f"Project bulunamadı: {args.project}")
                _print_key_once(await _create_key(conn, project_id, args.name, args.env))

            elif args.cmd == "list-keys":
                rows = await conn.fetch(
                    """
                    SELECT k.id, k.name, k.key_prefix, k.environment, k.status, k.created_at, k.last_used_at
                    FROM project_api_keys k JOIN projects p ON p.id = k.project_id
                    WHERE p.slug = $1 ORDER BY k.created_at
                    """,
                    args.project,
                )
                for row in rows:
                    print(f"{row['id']}  {row['key_prefix']}…  {row['environment']:4}  {row['status']:8}  "
                          f"{row['name']}  last_used={row['last_used_at']}")

            elif args.cmd == "create-admin":
                from app.admin.auth import generate_password, hash_password

                password = generate_password()
                try:
                    await conn.execute(
                        """
                        INSERT INTO admin_users (username, email, name, role, password_hash, must_change_password)
                        VALUES ($1, $2, $3, $4, $5, true)
                        """,
                        (args.username or args.email).strip().lower() if (args.username or args.email) else None,
                        args.email.strip().lower() if args.email else None,
                        args.name, args.role, hash_password(password),
                    )
                except Exception as exc:
                    raise SystemExit(f"Yönetici oluşturulamadı: {exc}")
                print(f"Yönetici oluşturuldu: {args.email or args.username} ({args.role})")
                print("\nGeçici şifre (ilk girişte değiştirmen istenecek):\n")
                print(f"  {password}\n")

            elif args.cmd == "r2-check":
                await _r2_check()

            elif args.cmd == "admin-status":
                rows = await conn.fetch(
                    """
                    SELECT coalesce(email, '-') AS email, username, role, status, must_change_password,
                           locked_until > now() AS locked, failed_logins, totp_enabled, last_login_at
                    FROM admin_users ORDER BY created_at
                    """
                )
                if not rows:
                    print("Hiç panel yöneticisi yok. Env'e ADMIN_EMAIL ve ADMIN_PASSWORD ekleyip admin-api'yi yeniden başlatın.")
                for r in rows:
                    print(f"- {r['email']}  (kullanıcı adı: {r['username']})")
                    print(f"    rol={r['role']} durum={r['status']} kilitli={'EVET' if r['locked'] else 'hayır'} "
                          f"hatalı_deneme={r['failed_logins']} 2fa={'açık' if r['totp_enabled'] else 'kapalı'} "
                          f"şifre_değişecek={'evet' if r['must_change_password'] else 'hayır'} son_giriş={r['last_login_at'] or '-'}")
                bans = await conn.fetch(
                    "SELECT cidr::text AS cidr, reason, expires_at FROM ip_rules WHERE project_id IS NULL "
                    "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT 10"
                )
                if bans:
                    print("\nEtkin platform IP engelleri (panele erişimi de engeller):")
                    for b in bans:
                        print(f"  {b['cidr']}  {b['reason'] or ''}  bitiş: {b['expires_at'] or 'süresiz'}")

            elif args.cmd == "unban-ip":
                import ipaddress

                from redis.asyncio import Redis

                from app.config import settings
                ip = str(ipaddress.ip_address(args.ip.strip()))
                deleted = await conn.fetch("DELETE FROM ip_rules WHERE $1::inet <<= cidr RETURNING cidr::text AS cidr", ip)
                r = Redis.from_url(settings.redis_url)
                await r.delete(f"ban:{ip}")
                await r.aclose()
                print(f"{ip}: {len(deleted)} kural kaldırıldı" + (f" ({', '.join(d['cidr'] for d in deleted)})" if deleted else ""))

            elif args.cmd == "reset-admin-password":
                from app.admin.auth import generate_password, hash_password

                password = generate_password()
                found = await conn.fetchval(
                    """
                    UPDATE admin_users SET password_hash = $2, must_change_password = true, failed_logins = 0,
                           locked_until = NULL, status = 'active', password_changed_at = now()
                    WHERE lower(username) = lower($1) OR lower(email) = lower($1) RETURNING id
                    """,
                    (args.email or args.username or "").strip(), hash_password(password),
                )
                if not found:
                    raise SystemExit(f"Yönetici bulunamadı: {args.email or args.username}")
                await conn.execute("UPDATE admin_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", found)
                print(f"{args.email or args.username} için geçici şifre (girişte değiştirmen istenecek):\n\n  {password}\n")

            elif args.cmd == "revoke-key":
                done = await conn.execute(
                    "UPDATE project_api_keys SET status = 'revoked', revoked_at = now() "
                    "WHERE id = $1 AND status <> 'revoked'",
                    UUID(args.id),
                )
                print("Revoke edildi." if done.endswith("1") else "Key bulunamadı veya zaten revoke edilmiş.")
    finally:
        await pool.close()


async def _r2_check() -> None:
    """R2 ayarlarını ve erişimini adım adım dener; her sorun için Türkçe çözüm önerir."""
    import time as _time

    from app.config import settings
    from app.storage import HINTS, configured, endpoint_url, error_code, storage

    def mask(v: str) -> str:
        return (v[:4] + "…" + v[-4:]) if len(v) > 10 else ("(boş)" if not v else "(çok kısa)")

    print("Cloudflare R2 kontrolü\n")
    print(f"  R2_ACCOUNT_ID        : {settings.r2_account_id or '(boş)'}")
    print(f"  R2_BUCKET            : {settings.r2_bucket or '(boş)'}")
    print(f"  R2_JURISDICTION      : {settings.r2_jurisdiction or '(boş: standart bucket)'}")
    print(f"  R2_ACCESS_KEY_ID     : {mask(settings.r2_access_key_id)} ({len(settings.r2_access_key_id)} karakter; genelde 32)")
    print(f"  R2_SECRET_ACCESS_KEY : {mask(settings.r2_secret_access_key)} ({len(settings.r2_secret_access_key)} karakter; genelde 64)")
    print(f"  Bağlantı adresi      : {endpoint_url() or '(üretilemedi)'}\n")
    for name, value in (("R2_ACCESS_KEY_ID", settings.r2_access_key_id), ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
                        ("R2_BUCKET", settings.r2_bucket), ("R2_ACCOUNT_ID", settings.r2_account_id)):
        if value != value.strip():
            print(f"  ! {name} başında/sonunda boşluk var; env'de düzeltin.")
    if not configured():
        print("✗ R2 yapılandırılmamış: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY ve R2_BUCKET dolu olmalı.")
        return

    await storage.start()
    key = f"incoming/_r2check/{int(_time.time())}.txt"
    steps = [
        ("Bucket'a erişim", lambda: storage._c().head_bucket(Bucket=storage.bucket)),
        ("Dosya yazma", lambda: storage.put_bytes(key, b"r2-check", "text/plain")),
        ("Dosya okuma", lambda: storage.get_bytes(key, 1024)),
        ("Dosya silme", lambda: storage.delete_keys([key])),
    ]
    ok = True
    try:
        for label, fn in steps:
            try:
                await fn()
                print(f"✓ {label}")
            except Exception as exc:  # noqa: BLE001
                code = error_code(exc)
                print(f"✗ {label}: {code}")
                print(f"    Çözüm: {HINTS.get(code, 'Env değerlerini ve token izinlerini kontrol edin.')}")
                ok = False
                break
        if ok:
            try:
                rules = await storage._c().get_bucket_lifecycle_configuration(Bucket=storage.bucket)
                ids = [r.get("ID") or r.get("Filter", {}).get("Prefix") for r in rules.get("Rules", [])]
                print(f"✓ Otomatik silme kuralları: {', '.join(str(i) for i in ids) or 'yok'}")
            except Exception as exc:  # noqa: BLE001
                print(f"• Otomatik silme kuralları okunamadı ({error_code(exc)}). Object Read & Write token'ında bu normal; "
                      "Cloudflare panelinden elle ekleyin: incoming/ 1 gün, evidence/ 35 gün.")
            print("\nSONUÇ: R2 hazır. Görsel ve video moderasyonu çalışır.")
        else:
            print("\nSONUÇ: R2 kullanılamıyor. Yukarıdaki çözümü uygulayıp env'i kaydedin, yeniden deploy edin ve tekrar deneyin.")
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
