"""消息存储层。

职责：
  1. save_event_message()  — 从 WS 事件存入（ON CONFLICT DO NOTHING）
  2. save_self_message()   — Sophos 发消息后主动存入（ON CONFLICT DO UPDATE）
  3. get_context()         — 按会话查询最近 N 条
  4. get_by_message_id()   — 按 message_id 查找（CQ:reply 展开用）

去重策略：
  - 群聊以 (group_id, message_id) 唯一，私聊以 (user_id, message_id) 唯一
  - 事件入口用 DO NOTHING：如果 Sophos 已通过 save_self_message 存过，则跳过
  - Sophos 发消息用 DO UPDATE：无论事件是否先到，最终都标记为 source='sophos'
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from sophos import runtime_config

logger = logging.getLogger(__name__)


class MessageStore:
    """消息存储，封装对 messages 表的读写操作。"""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        """公开数据库连接池，供外部模块（如 vision）使用。"""
        return self._pool

    # ── 从 WS 事件存入 ───────────────────────────────────

    async def save_event_message(self, event: dict[str, Any], self_id: int | None = None) -> int | None:
        """从 OneBot 消息事件中提取字段并存入数据库。

        如果该 message_id 已存在（Sophos 先通过 save_self_message 存过），则跳过。
        如果是同账号发出的消息（user_id == self_id）但不是 Sophos 存的，标记为 co_account。

        Args:
            event: OneBot v11 消息事件（post_type 为 message 或 message_sent）
            self_id: Bot 自身的 QQ 号，从事件的 self_id 字段获取

        Returns:
            数据库自增 id，已存在时返回 None
        """
        try:
            message_id, message_type, group_id, user_id, nickname, card, raw_message, plain_text, timestamp = (
                self._extract_event_fields(event)
            )

            # 判断来源
            is_from_self_account = (user_id == self_id) if self_id is not None else False
            source = "co_account" if is_from_self_account else "user"

            # ON CONFLICT DO NOTHING：如果已存在（Sophos 先存过）就跳过
            if group_id is not None:
                conflict_clause = "ON CONFLICT (group_id, message_id) WHERE group_id IS NOT NULL DO NOTHING"
            else:
                conflict_clause = "ON CONFLICT (user_id, message_id) WHERE group_id IS NULL DO NOTHING"

            row_id = await self._pool.fetchval(
                f"""
                INSERT INTO messages
                    (message_id, message_type, group_id, user_id,
                     nickname, card, source, raw_message, plain_text, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                {conflict_clause}
                RETURNING id
                """,
                message_id, message_type, group_id, user_id,
                nickname, card, source,
                json.dumps(raw_message, ensure_ascii=False),
                plain_text, timestamp,
            )

            if row_id is not None:
                logger.debug("Saved event message id=%s (message_id=%s, source=%s)", row_id, message_id, source)
            else:
                logger.debug("Skipped event message (message_id=%s, already exists)", message_id)
            return row_id

        except Exception:
            logger.exception("Failed to save event message")
            return None

    # ── Sophos 发消息后主动存入 ───────────────────────────

    async def save_self_message(
        self,
        *,
        message_id: int,
        message_type: str,
        group_id: int | None,
        user_id: int,
        raw_message: list[dict[str, Any]],
        timestamp: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        """Sophos 发送消息后主动存储，标记 source='sophos'。

        nickname 和 card 留空——LLM 可通过调用查询接口获取。
        使用 ON CONFLICT DO UPDATE：如果事件先到（标记为 co_account），覆盖为 sophos。
        """
        try:
            plain_text = "".join(
                seg["data"]["text"]
                for seg in raw_message
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
            ts = timestamp or datetime.now(tz=UTC)
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

            if group_id is not None:
                conflict_clause = """
                    ON CONFLICT (group_id, message_id) WHERE group_id IS NOT NULL
                    DO UPDATE SET source = 'sophos', extra = EXCLUDED.extra
                """
            else:
                conflict_clause = """
                    ON CONFLICT (user_id, message_id) WHERE group_id IS NULL
                    DO UPDATE SET source = 'sophos', extra = EXCLUDED.extra
                """

            row_id = await self._pool.fetchval(
                f"""
                INSERT INTO messages
                    (message_id, message_type, group_id, user_id,
                     nickname, card, source, raw_message, plain_text, timestamp, extra)
                VALUES ($1, $2, $3, $4, '', '', 'sophos', $5::jsonb, $6, $7, $8::jsonb)
                {conflict_clause}
                RETURNING id
                """,
                message_id, message_type, group_id, user_id,
                json.dumps(raw_message, ensure_ascii=False),
                plain_text, ts, extra_json,
            )
            logger.debug("Saved self message id=%s (message_id=%s)", row_id, message_id)
            return row_id

        except Exception:
            logger.exception("Failed to save self message")
            return None

    # ── 读取上下文 ────────────────────────────────────────

    async def get_context(
        self,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
        limit: int | None = None,
        include_co_account: bool = True,
    ) -> list[dict[str, Any]]:
        """获取指定会话的最近 N 条消息。

        群聊：传 group_id
        私聊：传 user_id（group_id 为 None）
        include_co_account: 是否包含同账号其他来源的消息

        返回按时间正序排列的消息列表（最旧在前），方便直接拼接给 LLM。
        """
        max_messages = limit or runtime_config.get("max_context_messages")

        # 根据配置决定是否过滤 co_account
        source_filter = "" if include_co_account else "AND source != 'co_account'"

        if group_id is not None:
            rows = await self._pool.fetch(
                f"""
                SELECT * FROM messages
                WHERE group_id = $1 {source_filter}
                ORDER BY timestamp DESC
                LIMIT $2
                """,
                group_id, max_messages,
            )
        elif user_id is not None:
            rows = await self._pool.fetch(
                f"""
                SELECT * FROM messages
                WHERE user_id = $1 AND group_id IS NULL {source_filter}
                ORDER BY timestamp DESC
                LIMIT $2
                """,
                user_id, max_messages,
            )
        else:
            raise ValueError("Must provide either group_id or user_id")

        # DB 返回的是 DESC 顺序（最新在前），反转为正序
        return [dict(row) for row in reversed(rows)]

    # ── 按 message_id 查找（CQ:reply 展开用）──────────────

    async def get_by_message_id(self, message_id: int) -> dict[str, Any] | None:
        """按 OneBot message_id 查找一条消息。"""
        row = await self._pool.fetchrow(
            "SELECT * FROM messages WHERE message_id = $1",
            message_id,
        )
        return dict(row) if row else None

    # ── plain_text 更新（段展开后）────────────────────────────

    async def update_plain_text(self, message_id: int, plain_text: str) -> None:
        """更新消息的 plain_text（段展开后的富文本）。"""
        await self._pool.execute(
            "UPDATE messages SET plain_text = $2 WHERE message_id = $1",
            message_id, plain_text,
        )

    # ── 图片描述更新 ────────────────────────────────────────

    async def update_image_extra(
        self, message_id: int, image_infos: list[dict[str, str]],
    ) -> None:
        """将图片描述写入消息的 extra.images 字段。

        与现有 extra 字段（如 cross_context）合并，互不干扰。
        """
        images_json = json.dumps(image_infos, ensure_ascii=False)
        await self._pool.execute(
            """
            UPDATE messages
            SET extra = COALESCE(extra, '{}'::jsonb) || jsonb_build_object('images', $2::jsonb)
            WHERE message_id = $1
            """,
            message_id, images_json,
        )

    # ── 按时间范围查询 ────────────────────────────────────

    async def query_by_time_range(
        self,
        *,
        message_type: str,
        target_id: int,
        start: datetime,
        end: datetime,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """按时间范围查询指定会话的消息。

        Args:
            message_type: "group" 或 "private"
            target_id:    群号（group）或用户 QQ 号（private）
            start:        时间窗口起点（UTC）
            end:          时间窗口终点（UTC）
            limit:        最大返回条数

        Returns:
            按时间正序排列的消息列表
        """
        if message_type == "group":
            where = "group_id = $1"
        else:
            where = "user_id = $1 AND group_id IS NULL"

        rows = await self._pool.fetch(
            f"""
            SELECT * FROM messages
            WHERE {where} AND timestamp BETWEEN $2 AND $3
            ORDER BY timestamp ASC
            LIMIT $4
            """,
            target_id, start, end, limit,
        )
        return [dict(r) for r in rows]

    # ── 跨 context 背景查询 ───────────────────────────────

    async def get_cross_context_background(
        self,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        """查找当前会话中最近一条带 cross_context 背景的 Sophos 消息。

        用于 system 模式的背景注入：找到最近一次跨 context 发来的消息，
        将其背景摘要注入到 system prompt 中。

        Returns:
            整行 dict（含 timestamp、extra 等），供格式化用。无则 None。
        """
        if group_id is not None:
            row = await self._pool.fetchrow(
                """
                SELECT * FROM messages
                WHERE group_id = $1
                  AND source = 'sophos'
                  AND extra->'cross_context' IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                group_id,
            )
        elif user_id is not None:
            row = await self._pool.fetchrow(
                """
                SELECT * FROM messages
                WHERE user_id = $1 AND group_id IS NULL
                  AND source = 'sophos'
                  AND extra->'cross_context' IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                user_id,
            )
        else:
            return None

        return dict(row) if row else None

    # ── 最近全局消息（跨上下文）──────────────────────────

    async def get_recent_global(
        self,
        *,
        exclude_group_id: int | None = None,
        exclude_private_user_id: int | None = None,
        limit: int = 50,
        min_self_messages: int = 5,
    ) -> list[dict[str, Any]]:
        """获取其他上下文的最近消息，确保至少包含 min_self_messages 条 sophos 消息。

        Args:
            exclude_group_id:        当前群聊 ID（排除）
            exclude_private_user_id: 当前私聊用户 ID（排除）
            limit:                   最大消息条数
            min_self_messages:       sophos 消息最少条数

        Returns:
            按时间正序排列的消息列表
        """
        # 构建排除条件
        exclude_parts: list[str] = []
        params: list[Any] = []
        idx = 1

        if exclude_group_id is not None:
            exclude_parts.append(f"NOT (group_id = ${idx})")
            params.append(exclude_group_id)
            idx += 1
        if exclude_private_user_id is not None:
            exclude_parts.append(f"NOT (group_id IS NULL AND user_id = ${idx})")
            params.append(exclude_private_user_id)
            idx += 1

        where = " AND ".join(exclude_parts) if exclude_parts else "TRUE"

        # Query 1: 最近 limit 条非当前上下文消息
        params.append(limit)
        rows = await self._pool.fetch(
            f"""
            SELECT * FROM messages
            WHERE {where}
            ORDER BY timestamp DESC
            LIMIT ${idx}
            """,
            *params,
        )
        rows = [dict(r) for r in rows]

        # 统计 sophos 消息数量
        sophos_count = sum(1 for r in rows if r.get("source") == "sophos")

        if sophos_count < min_self_messages:
            # Query 2: 补充 sophos 消息
            seen_ids = {r["id"] for r in rows}
            need = min_self_messages - sophos_count
            extra_params: list[Any] = []
            extra_idx = 1
            extra_parts: list[str] = []

            if exclude_group_id is not None:
                extra_parts.append(f"NOT (group_id = ${extra_idx})")
                extra_params.append(exclude_group_id)
                extra_idx += 1
            if exclude_private_user_id is not None:
                extra_parts.append(f"NOT (group_id IS NULL AND user_id = ${extra_idx})")
                extra_params.append(exclude_private_user_id)
                extra_idx += 1

            extra_where = " AND ".join(extra_parts) if extra_parts else "TRUE"
            extra_params.append(need + len(rows))  # 多取一些以跳过已有的

            extra_rows = await self._pool.fetch(
                f"""
                SELECT * FROM messages
                WHERE {extra_where} AND source = 'sophos'
                ORDER BY timestamp DESC
                LIMIT ${extra_idx}
                """,
                *extra_params,
            )
            for r in extra_rows:
                rd = dict(r)
                if rd["id"] not in seen_ids:
                    rows.append(rd)
                    seen_ids.add(rd["id"])
                    need -= 1
                    if need <= 0:
                        break

        # 按时间正序
        rows.sort(key=lambda r: r["timestamp"])
        # 截断到 limit
        return rows[-limit:] if len(rows) > limit else rows

    # ── 内部工具 ──────────────────────────────────────────

    @staticmethod
    def _extract_event_fields(
        event: dict[str, Any],
    ) -> tuple[Any, str, int | None, Any, str, str, list[Any], str, datetime]:
        """从 OneBot 事件提取存储所需的字段。"""
        message_id = event.get("message_id")
        message_type = event.get("message_type", "private")
        group_id = event.get("group_id")
        user_id = event.get("user_id")

        sender = event.get("sender", {})
        nickname = sender.get("nickname", "")
        card = sender.get("card", "")

        raw_message = event.get("message", [])
        plain_text = "".join(
            seg["data"]["text"]
            for seg in raw_message
            if isinstance(seg, dict) and seg.get("type") == "text"
        )

        ts = event.get("time")
        timestamp = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(tz=UTC)

        return message_id, message_type, group_id, user_id, nickname, card, raw_message, plain_text, timestamp
