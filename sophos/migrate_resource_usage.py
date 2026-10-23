"""Offline schema v4 -> v5: stop Sophos, run this module, restart."""

import asyncio

import asyncpg

from sophos.config import settings
from sophos.db import _CREATE_TOOL_USAGE_TABLE, _TOOL_USAGE_INDEXES


async def migrate() -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction():
            version = await conn.fetchval("SELECT version FROM schema_meta WHERE singleton FOR UPDATE")
            if version != 4:
                raise RuntimeError(f"Expected schema v4, found v{version}")
            await conn.execute(_CREATE_TOOL_USAGE_TABLE)
            for sql in _TOOL_USAGE_INDEXES:
                await conn.execute(sql)
            await conn.execute("UPDATE schema_meta SET version = 5 WHERE singleton")
        print("Schema v5 ready.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
