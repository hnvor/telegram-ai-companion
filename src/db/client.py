import json

import asyncpg
from pgvector.asyncpg import register_vector

from src.config import settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # JSONB / JSON → dict/list автоматически (по умолчанию asyncpg возвращает str)
    for typename in ("jsonb", "json"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    # Supabase ставит pgvector в schema 'extensions', а pgvector.asyncpg по умолчанию
    # ищет в 'public'. Авто-детектим где живёт тип vector.
    schema = await conn.fetchval(
        "SELECT n.nspname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE t.typname = 'vector' LIMIT 1"
    )
    try:
        await register_vector(conn, schema=schema or "public")
    except TypeError:
        await register_vector(conn)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
            statement_cache_size=0,  # Supabase transaction pooler не дружит с prepared statements
            max_inactive_connection_lifetime=60,  # меньше, чем idle timeout Supabase Pooler
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
