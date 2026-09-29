"""Yük testi: çok projeli eşzamanlı trafik.

Kullanım (backend klasöründe, stack çalışırken; DATABASE_URL vb. ortam değişkenleri tanımlı):
    python scripts/loadtest.py --api http://localhost:8000 --projects 20 --concurrency 100 --requests 3000

Sunucuda (api container terminalinde, /srv içinde) çalıştırırken --api http://localhost:8000 kullanın.
Test projeleri "loadtest-" önekiyle oluşturulur; bitince --cleanup ile silinir.

Ölçülenler: saniyedeki istek, gecikme yüzdelikleri, durum kodları, kararlar. Sonunda veritabanından
kaybolan veya takılı kalan istek olmadığı doğrulanır.
"""
import argparse
import asyncio
import random
import re
import secrets
import statistics
import subprocess
import sys
import time
from collections import Counter

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

TEXTS = {
    "clean": ["Harika bir ürün, çok memnun kaldım", "Kargo hızlı geldi teşekkürler", "Bu konuda sana katılıyorum",
              "Maç çok güzeldi dün akşam", "Yarın toplantı saat kaçta?", "Güzel paylaşım emeğine sağlık"],
    "bad": ["amk ya ne biçim iş bu", "siktirin gidin", "s1kt1r git", "orospu çocukları"],
    "unsure": ["salak mısın", "aptal herif", "gerizekalı"],
}


def create_projects(n: int) -> list[str]:
    keys = []
    tag = secrets.token_hex(3)
    for i in range(n):
        out = subprocess.run([sys.executable, "-m", "app.cli", "create-project", "--name", f"Loadtest {tag}-{i}",
                              "--slug", f"loadtest-{tag}-{i}", "--env", "test"], capture_output=True, text=True, check=True)
        keys.append(re.search(r"(mk_test_\w+)", out.stdout).group(1))
    return keys


async def worker(client, keys, queue, results):
    while True:
        try:
            i = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        kind = random.choices(["clean", "bad", "unsure"], weights=[70, 15, 15])[0]
        wait = i % 10 < 7                      # %70 senkron (sonucu bekler), %30 asenkron
        key = keys[i % len(keys)]
        started = time.perf_counter()
        try:
            r = await client.post(f"/v1/moderate{'?wait=true' if wait else ''}",
                                  json={"type": "text", "text": random.choice(TEXTS[kind]), "user_id": f"u{i % 500}"},
                                  headers={"Authorization": f"Bearer {key}"})
            body = r.json()   # her yanıt JSON olmalı
            results.append((r.status_code, time.perf_counter() - started, body.get("status"), body.get("decision"), wait))
        except Exception as exc:  # noqa: BLE001
            results.append(("exception:" + type(exc).__name__, time.perf_counter() - started, None, None, wait))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--projects", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=100)
    ap.add_argument("--requests", type=int, default=3000)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    print(f"{args.projects} proje oluşturuluyor…")
    keys = create_projects(args.projects)
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(args.requests):
        queue.put_nowait(i)
    results: list = []
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.api, timeout=60, limits=limits) as client:
        print(f"{args.requests} istek, {args.concurrency} eşzamanlı bağlantı…")
        started = time.perf_counter()
        await asyncio.gather(*[worker(client, keys, queue, results) for _ in range(args.concurrency)])
        elapsed = time.perf_counter() - started

    codes = Counter(str(r[0]) for r in results)
    sync = sorted(r[1] for r in results if r[4] and r[0] == 200)
    statuses = Counter(r[2] for r in results)
    decisions = Counter(r[3] for r in results if r[3])
    pct = lambda xs, p: xs[min(len(xs) - 1, int(len(xs) * p))] * 1000 if xs else 0  # noqa: E731
    print(f"\nSüre: {elapsed:.1f} sn · {len(results) / elapsed:.0f} istek/sn")
    print(f"HTTP: {dict(codes)}")
    print(f"Durum: {dict(statuses)}")
    print(f"Karar: {dict(decisions)}")
    if sync:
        print(f"Senkron gecikme (ms): p50={pct(sync, .5):.0f} p95={pct(sync, .95):.0f} p99={pct(sync, .99):.0f} "
              f"ort={statistics.mean(sync) * 1000:.0f}")

    # Kuyruğun boşalmasını bekle ve veritabanından kayıp/takılı kontrolü yap
    import asyncpg

    from app.config import settings
    conn = await asyncpg.connect(settings.database_url)
    pending = None
    for _ in range(60):
        pending = await conn.fetchval(
            """SELECT count(*) FROM moderation_requests r JOIN projects p ON p.id = r.project_id
               WHERE p.slug LIKE 'loadtest-%' AND r.status NOT IN ('completed', 'failed')""")
        if pending == 0:
            break
        await asyncio.sleep(1)
    total = await conn.fetchval(
        "SELECT count(*) FROM moderation_requests r JOIN projects p ON p.id = r.project_id WHERE p.slug LIKE 'loadtest-%'")
    failed = await conn.fetchval(
        """SELECT count(*) FROM moderation_requests r JOIN projects p ON p.id = r.project_id
           WHERE p.slug LIKE 'loadtest-%' AND r.status = 'failed'""")
    print(f"Veritabanı: {total} istek kaydı, işlenmeyi bekleyen {pending}, başarısız {failed}")
    if args.cleanup:
        await conn.execute("DELETE FROM moderation_requests WHERE project_id IN (SELECT id FROM projects WHERE slug LIKE 'loadtest-%')")
        await conn.execute("DELETE FROM projects WHERE slug LIKE 'loadtest-%'")
        print("Test projeleri silindi.")
    await conn.close()

    ok = codes.get("200", 0) + codes.get("202", 0)
    success = ok == len(results) and pending == 0 and failed == 0 and total == len(results)
    print("SONUÇ:", "BAŞARILI" if success else "SORUN VAR")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
