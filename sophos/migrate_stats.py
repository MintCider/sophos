"""Offline schema v3 -> v4: stop Sophos, run `python -m sophos.migrate_stats`, restart."""

import asyncio

import asyncpg

from sophos.config import settings
from sophos.db import _CREATE_STATS_TABLES, _STATS_INDEXES


async def migrate() -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction():
            version = await conn.fetchval("SELECT version FROM schema_meta WHERE singleton FOR UPDATE")
            if version != 3:
                raise RuntimeError(f"Expected schema v3, found v{version}")
            await conn.execute(_CREATE_STATS_TABLES)
            for sql in _STATS_INDEXES:
                await conn.execute(sql)
            await conn.execute("UPDATE schema_meta SET version = 4 WHERE singleton")
        print("Schema v4 ready.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
