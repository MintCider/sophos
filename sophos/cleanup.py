"""后台数据清理：聊天记录 / 记忆 LRU 淘汰。"""

import asyncio
import logging

import asyncpg

from sophos import runtime_config

logger = logging.getLogger(__name__)


async def _table_size(pool: asyncpg.Pool, table: str) -> int:
    """返回表的总大小（含索引和 TOAST），单位字节。"""
    row = await pool.fetchrow(
        "SELECT pg_total_relation_size($1) AS size_bytes",
        table,
    )
    return int(row["size_bytes"]) if row else 0


async def cleanup_messages(pool: asyncpg.Pool, max_bytes: int) -> None:
    """按 LRU 清理 messages 表，每轮删 5% 直到低于阈值。"""
    while True:
        size = await _table_size(pool, "messages")
        if size <= max_bytes:
            return
        count = await pool.fetchval("SELECT count(*) FROM messages")
        if not count:
            return
        batch = max(count // 20, 1)
        logger.info(
            "messages cleanup: %d MB / %d MB, deleting %d rows",
            size // (1024 * 1024),
            max_bytes // (1024 * 1024),
            batch,
        )
        await pool.execute(
            """
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages
                ORDER BY COALESCE(last_accessed, timestamp) ASC
                LIMIT $1
            )
            """,
            batch,
        )


async def cleanup_memories(pool: asyncpg.Pool, max_bytes: int) -> None:
    """按 LRU 清理 memories 表，每轮删 5% 直到低于阈值。"""
    while True:
        size = await _table_size(pool, "memories")
        if size <= max_bytes:
            return
        count = await pool.fetchval("SELECT count(*) FROM memories")
        if not count:
            return
        batch = max(count // 20, 1)
        logger.info(
            "memories cleanup: %d MB / %d MB, deleting %d rows",
            size // (1024 * 1024),
            max_bytes // (1024 * 1024),
            batch,
        )
        await pool.execute(
            """
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories
                ORDER BY COALESCE(last_hit, created_at) ASC
                LIMIT $1
            )
            """,
            batch,
        )


async def run_cleanup_loop(pool: asyncpg.Pool) -> None:
    """后台循环：启动时跑一次，之后按间隔定期检查。"""
    try:
        while True:
            try:
                msg_max = runtime_config.get("cleanup_messages_max_bytes")
                mem_max = runtime_config.get("cleanup_memories_max_bytes")
                await cleanup_messages(pool, msg_max)
                await cleanup_memories(pool, mem_max)
            except Exception:
                logger.exception("cleanup error")
            interval = runtime_config.get("cleanup_interval_seconds")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.debug("cleanup loop cancelled")
