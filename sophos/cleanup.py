"""后台数据清理：按有效行逻辑大小执行聊天记录 / 记忆 LRU 淘汰。"""

import asyncio
import logging
from dataclasses import dataclass

import asyncpg

from sophos import runtime_config

logger = logging.getLogger(__name__)

_MEBIBYTE = 1024 * 1024
_LOW_WATERMARK_NUMERATOR = 9
_LOW_WATERMARK_DENOMINATOR = 10
_BATCH_DIVISOR = 20
_DEFAULT_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class _CleanupSpec:
    table: str
    lru_expression: str


@dataclass(frozen=True)
class _TableStats:
    logical_bytes: int
    row_count: int


_CLEANUP_SPECS = {
    "messages": _CleanupSpec(
        table="messages",
        lru_expression="COALESCE(last_accessed_at, occurred_at)",
    ),
    "memories": _CleanupSpec(
        table="memories",
        lru_expression="COALESCE(last_hit, created_at)",
    ),
}


def _require_positive_int(value: object, name: str) -> int:
    """返回正整数配置值，拒绝 bool、浮点数和非正值。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _spec(table: str) -> _CleanupSpec:
    try:
        return _CLEANUP_SPECS[table]
    except KeyError:
        raise ValueError(f"unsupported cleanup table: {table}") from None


async def _logical_table_stats(pool: asyncpg.Pool, table: str) -> _TableStats:
    """一次扫描返回有效行逻辑大小和行数。"""
    spec = _spec(table)
    row = await pool.fetchrow(
        f"""
        SELECT
            COALESCE(SUM(pg_column_size(row_data)::bigint), 0) AS logical_bytes,
            COUNT(*) AS row_count
        FROM {spec.table} AS row_data
        """
    )
    if not row:
        return _TableStats(logical_bytes=0, row_count=0)
    return _TableStats(
        logical_bytes=int(row["logical_bytes"]),
        row_count=int(row["row_count"]),
    )


async def _protected_message_stats(pool: asyncpg.Pool, keep_per_conversation: int) -> _TableStats:
    """统计每个会话最近 K 条受保护消息，仅在容量无法下降时执行。"""
    row = await pool.fetchrow(
        """
        WITH protected AS MATERIALIZED (
            SELECT recent.id
            FROM conversations c
            CROSS JOIN LATERAL (
                SELECT m.id
                FROM messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.id DESC
                LIMIT $1
            ) recent
        )
        SELECT
            COALESCE(SUM(pg_column_size(m)::bigint), 0) AS logical_bytes,
            COUNT(*) AS row_count
        FROM messages m
        JOIN protected p ON p.id = m.id
        """,
        keep_per_conversation,
    )
    return _TableStats(
        logical_bytes=int(row["logical_bytes"]) if row else 0,
        row_count=int(row["row_count"]) if row else 0,
    )


async def _cleanup_table(
    pool: asyncpg.Pool,
    table: str,
    max_bytes: int,
    *,
    keep_per_conversation: int | None = None,
) -> None:
    """超过高水位后，按 LRU 分批删除到 90% 低水位。"""
    max_bytes = _require_positive_int(max_bytes, f"cleanup_{table}_max_bytes")
    spec = _spec(table)
    if table == "messages":
        keep_per_conversation = _require_positive_int(
            keep_per_conversation,
            "max_context_messages",
        )
    elif keep_per_conversation is not None:
        raise ValueError("keep_per_conversation is only supported for messages")
    stats = await _logical_table_stats(pool, table)
    size = stats.logical_bytes
    if size <= max_bytes:
        return

    target_bytes = max_bytes * _LOW_WATERMARK_NUMERATOR // _LOW_WATERMARK_DENOMINATOR
    batch = max(stats.row_count // _BATCH_DIVISOR, 1)

    while size > target_bytes:
        logger.info(
            "%s logical cleanup: %d MiB / %d MiB, deleting up to %d rows",
            spec.table,
            size // _MEBIBYTE,
            max_bytes // _MEBIBYTE,
            batch,
        )
        if table == "messages":
            # 每个会话只做一次 K+1 定位；避免对整张消息表执行窗口排序。
            deleted = await pool.fetch(
                f"""
                WITH conversation_cutoffs AS MATERIALIZED (
                    SELECT c.id AS conversation_id,
                           (
                               SELECT m.id
                               FROM messages m
                               WHERE m.conversation_id = c.id
                               ORDER BY m.id DESC
                               OFFSET $2
                               LIMIT 1
                           ) AS max_deletable_id
                    FROM conversations c
                ), victims AS MATERIALIZED (
                    SELECT m.id
                    FROM messages m
                    JOIN conversation_cutoffs cutoff
                      ON cutoff.conversation_id = m.conversation_id
                     AND cutoff.max_deletable_id IS NOT NULL
                     AND m.id <= cutoff.max_deletable_id
                    WHERE m.enrichment_status NOT IN ('pending', 'streaming')
                    ORDER BY {spec.lru_expression} ASC, m.id ASC
                    LIMIT $1
                )
                DELETE FROM messages m
                USING victims v
                WHERE m.id = v.id
                RETURNING pg_column_size(m)::bigint AS logical_bytes
                """,
                batch,
                keep_per_conversation,
            )
        else:
            deleted = await pool.fetch(
                f"""
                DELETE FROM {spec.table}
                WHERE id IN (
                    SELECT id FROM {spec.table}
                    ORDER BY {spec.lru_expression} ASC, id ASC
                    LIMIT $1
                )
                RETURNING pg_column_size({spec.table})::bigint AS logical_bytes
                """,
                batch,
            )
        if not deleted:
            if table == "messages":
                protected = await _protected_message_stats(pool, keep_per_conversation)
                logger.warning(
                    "messages cleanup retained %d protected rows (%d MiB); "
                    "per-conversation context floor takes precedence over the %d MiB limit",
                    protected.row_count,
                    protected.logical_bytes // _MEBIBYTE,
                    max_bytes // _MEBIBYTE,
                )
            logger.warning(
                "%s logical cleanup stopped before reaching target: %d MiB / %d MiB",
                spec.table,
                size // _MEBIBYTE,
                target_bytes // _MEBIBYTE,
            )
            return
        size -= sum(int(row["logical_bytes"]) for row in deleted)

    logger.info(
        "%s logical cleanup complete: approximately %d MiB / %d MiB",
        spec.table,
        max(size, 0) // _MEBIBYTE,
        max_bytes // _MEBIBYTE,
    )


async def cleanup_messages(pool: asyncpg.Pool, max_bytes: int) -> None:
    """按逻辑大小和 LRU 清理 messages。"""
    keep = _require_positive_int(runtime_config.get("max_context_messages"), "max_context_messages")
    await _cleanup_table(pool, "messages", max_bytes, keep_per_conversation=keep)


async def cleanup_memories(pool: asyncpg.Pool, max_bytes: int) -> None:
    """按逻辑大小和 LRU 清理 memories。"""
    await _cleanup_table(pool, "memories", max_bytes)


async def run_cleanup_loop(pool: asyncpg.Pool) -> None:
    """后台循环：启动时跑一次，之后按间隔定期检查。"""
    try:
        while True:
            try:
                interval = _require_positive_int(
                    runtime_config.get("cleanup_interval_seconds"),
                    "cleanup_interval_seconds",
                )
            except ValueError:
                logger.exception(
                    "invalid cleanup interval; using default %d seconds",
                    _DEFAULT_INTERVAL_SECONDS,
                )
                interval = _DEFAULT_INTERVAL_SECONDS

            try:
                msg_max = _require_positive_int(
                    runtime_config.get("cleanup_messages_max_bytes"),
                    "cleanup_messages_max_bytes",
                )
                mem_max = _require_positive_int(
                    runtime_config.get("cleanup_memories_max_bytes"),
                    "cleanup_memories_max_bytes",
                )
                await cleanup_messages(pool, msg_max)
                await cleanup_memories(pool, mem_max)
            except Exception:
                logger.exception("cleanup error")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.debug("cleanup loop cancelled")


# TODO: 增加显式的物理空间整理操作。VACUUM FULL 会独占锁表且需要额外临时磁盘空间，
#       必须由管理员确认并展示运行状态，不能放进这个自动清理循环。
