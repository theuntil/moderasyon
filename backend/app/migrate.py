"""Basit, sıralı SQL migration çalıştırıcı.

- migrations/*.sql dosyalarını isim sırasıyla uygular
- Uygulananları schema_migrations tablosunda tutar
- Advisory lock sayesinde birden fazla API instance aynı anda başlasa da
  migration yalnızca bir kez çalışır
"""
import asyncio
import logging
from pathlib import Path

from app.db import create_pool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_ID = 727_001  # rastgele sabit bir sayı

log = logging.getLogger("migrate")


async def run() -> None:
    pool = await create_pool(max_size=1)
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    text PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    if path.name in applied:
                        continue
                    async with conn.transaction():
                        await conn.execute(path.read_text(encoding="utf-8"))
                        await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.name)
                    print(f"migration applied: {path.name}", flush=True)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
