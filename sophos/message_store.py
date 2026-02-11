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

from sophos.config import settings

logger = logging.getLogger(__name__)


class MessageStore:
    """消息存储，封装对 messages 表的读写操作。"""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

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

            if group_id is not None:
                conflict_clause = """
                    ON CONFLICT (group_id, message_id) WHERE group_id IS NOT NULL
                    DO UPDATE SET source = 'sophos'
                """
            else:
                conflict_clause = """
                    ON CONFLICT (user_id, message_id) WHERE group_id IS NULL
                    DO UPDATE SET source = 'sophos'
                """

            row_id = await self._pool.fetchval(
                f"""
                INSERT INTO messages
                    (message_id, message_type, group_id, user_id,
                     nickname, card, source, raw_message, plain_text, timestamp)
                VALUES ($1, $2, $3, $4, '', '', 'sophos', $5::jsonb, $6, $7)
                {conflict_clause}
                RETURNING id
                """,
                message_id, message_type, group_id, user_id,
                json.dumps(raw_message, ensure_ascii=False),
                plain_text, ts,
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
        max_messages = limit or settings.max_context_messages

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
