import json

import asyncpg

from app.config import settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb kolonları Python dict/list olarak okunup yazılsın
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(min_size: int = 1, max_size: int = 10, statement_timeout_ms: int = 10_000) -> asyncpg.Pool:
    """statement_timeout: yavaş bir sorgu bağlantıyı sonsuza kadar tutup havuzu tüketemesin."""
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=min_size,
        max_size=max_size,
        init=_init_connection,
        command_timeout=max(5, statement_timeout_ms / 1000 + 5),
        server_settings={
            "statement_timeout": str(statement_timeout_ms),
            "idle_in_transaction_session_timeout": "60000",
            "application_name": "moderation",
        },
    )
