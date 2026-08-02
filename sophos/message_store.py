"""Platform-neutral message persistence.

All internal mutation and agent-facing references use ``messages.id``. Protocol
IDs are accepted only together with an adapter binding and conversation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg

from sophos import runtime_config
from sophos.platform import MessageEvent

_MESSAGE_COLUMNS = """
    m.*,
    m.id AS message_id,
    m.content AS raw_message,
    m.occurred_at AS timestamp,
    m.last_accessed_at AS last_accessed,
    m.metadata AS extra,
    u.id AS user_id,
    COALESCE(NULLIF(i.display_name, ''), u.display_name, '') AS nickname,
    ''::text AS card,
    c.kind AS conversation_kind
"""


class MessageStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    async def save_event_message(self, event: MessageEvent) -> int:
        """Idempotently persist an adapter-normalized event and return its internal ID."""
        reply_to_message_id = await self._resolve_reply_target(
            adapter_binding_id=event.adapter_binding_id,
            conversation_id=event.conversation.conversation_id,
            segments=event.segments,
        )
        row_id = await self._pool.fetchval(
            """
            INSERT INTO messages
                (adapter_binding_id, conversation_id, sender_identity_id,
                 external_message_id, reply_to_message_id, source, content, plain_text, raw_payload,
                 enrichment_status, occurred_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11, $12::jsonb)
            ON CONFLICT (adapter_binding_id, conversation_id, external_message_id)
                WHERE external_message_id IS NOT NULL
            DO UPDATE SET
                sender_identity_id = COALESCE(messages.sender_identity_id, EXCLUDED.sender_identity_id),
                reply_to_message_id = COALESCE(messages.reply_to_message_id, EXCLUDED.reply_to_message_id),
                source = CASE WHEN messages.source = 'sophos' THEN messages.source ELSE EXCLUDED.source END,
                raw_payload = COALESCE(messages.raw_payload, EXCLUDED.raw_payload),
                metadata = messages.metadata || EXCLUDED.metadata
            RETURNING id
            """,
            event.adapter_binding_id,
            event.conversation.conversation_id,
            event.sender.identity_id,
            event.external_message_id,
            reply_to_message_id,
            event.source,
            json.dumps(event.segments, ensure_ascii=False),
            event.plain_text,
            json.dumps(event.raw_payload, ensure_ascii=False),
            self._initial_enrichment_status(event.segments),
            event.occurred_at,
            json.dumps(event.metadata, ensure_ascii=False),
        )
        return int(row_id)

    async def save_outbound_message(
        self,
        *,
        adapter_binding_id: int,
        conversation_id: int,
        sender_identity_id: int,
        external_message_id: str,
        segments: list[dict[str, Any]],
        reply_to_message_id: int | None = None,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> int:
        """Persist a Sophos delivery; an earlier self echo is upgraded in place."""
        plain_text = "".join(
            segment.get("data", {}).get("text", "")
            for segment in segments
            if isinstance(segment, dict) and segment.get("type") == "text"
        )
        row_id = await self._pool.fetchval(
            """
            INSERT INTO messages
                (adapter_binding_id, conversation_id, sender_identity_id,
                 external_message_id, reply_to_message_id, source, content, plain_text, raw_payload,
                 enrichment_status, occurred_at, metadata)
            VALUES ($1, $2, $3, $4, $5, 'sophos', $6::jsonb, $7, $8::jsonb,
                    'completed', $9, $10::jsonb)
            ON CONFLICT (adapter_binding_id, conversation_id, external_message_id)
                WHERE external_message_id IS NOT NULL
            DO UPDATE SET source = 'sophos',
                          sender_identity_id = EXCLUDED.sender_identity_id,
                          reply_to_message_id = COALESCE(EXCLUDED.reply_to_message_id, messages.reply_to_message_id),
                          content = EXCLUDED.content,
                          plain_text = EXCLUDED.plain_text,
                          raw_payload = COALESCE(EXCLUDED.raw_payload, messages.raw_payload),
                          metadata = messages.metadata || EXCLUDED.metadata
            RETURNING id
            """,
            adapter_binding_id,
            conversation_id,
            sender_identity_id,
            str(external_message_id),
            reply_to_message_id,
            json.dumps(segments, ensure_ascii=False),
            plain_text,
            json.dumps(raw_payload or {}, ensure_ascii=False),
            occurred_at or datetime.now(tz=UTC),
            json.dumps(metadata or {}, ensure_ascii=False),
        )
        return int(row_id)

    async def get_by_id(self, message_id: int) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.id = $1
            """,
            message_id,
        )
        return dict(row) if row else None

    async def get_by_external_locator(
        self,
        *,
        adapter_binding_id: int,
        conversation_id: int,
        external_message_id: str,
    ) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.adapter_binding_id = $1 AND m.conversation_id = $2
              AND m.external_message_id = $3
            """,
            adapter_binding_id,
            conversation_id,
            str(external_message_id),
        )
        return dict(row) if row else None

    async def update_plain_text(self, message_id: int, plain_text: str) -> None:
        await self._pool.execute("UPDATE messages SET plain_text = $2 WHERE id = $1", message_id, plain_text)

    async def update_image_metadata(self, message_id: int, image_infos: list[dict[str, str]]) -> None:
        await self._pool.execute(
            """
            UPDATE messages
            SET metadata = metadata || jsonb_build_object('images', $2::jsonb)
            WHERE id = $1
            """,
            message_id,
            json.dumps(image_infos, ensure_ascii=False),
        )

    async def set_enrichment_streaming(self, message_id: int) -> None:
        await self._pool.execute(
            """
            UPDATE messages SET enrichment_status = 'streaming', enrichment_error = NULL
            WHERE id = $1 AND enrichment_status = 'pending'
            """,
            message_id,
        )

    async def complete_enrichment(self, message_id: int) -> None:
        await self._pool.execute(
            """
            UPDATE messages SET enrichment_status = 'completed', enrichment_error = NULL
            WHERE id = $1 AND enrichment_status IN ('pending', 'streaming')
            """,
            message_id,
        )

    async def finish_image_enrichment(self, message_id: int, image_infos: list[dict[str, str]]) -> None:
        failed = any(info.get("status") == "failed" for info in image_infos)
        status = "failed" if failed else "completed"
        errors = [info.get("error", "") for info in image_infos if info.get("status") == "failed"]
        await self._pool.execute(
            """
            UPDATE messages
            SET metadata = metadata || jsonb_build_object('images', $2::jsonb),
                enrichment_status = $3,
                enrichment_error = $4
            WHERE id = $1
            """,
            message_id,
            json.dumps(image_infos, ensure_ascii=False),
            status,
            "; ".join(filter(None, errors)) or None,
        )

    async def fail_interrupted_enrichments(self) -> int:
        result = await self._pool.execute(
            """
            UPDATE messages SET enrichment_status = 'failed',
                                enrichment_error = '消息富化因服务重启而中断'
            WHERE enrichment_status IN ('pending', 'streaming')
            """
        )
        return int(result.rsplit(" ", 1)[-1])

    async def get_context(
        self,
        *,
        conversation_id: int,
        limit: int | None = None,
        include_co_account: bool = True,
    ) -> list[dict[str, Any]]:
        max_messages = limit or runtime_config.get("max_context_messages")
        source_filter = "" if include_co_account else "AND m.source != 'co_account'"
        pending_source_filter = "" if include_co_account else "AND p.source != 'co_account'"
        rows = await self._pool.fetch(
            f"""
            WITH boundary AS (
                SELECT MIN(p.id) AS pending_id FROM messages p
                WHERE p.conversation_id = $1
                  AND p.enrichment_status IN ('pending', 'streaming')
                  {pending_source_filter}
            )
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            CROSS JOIN boundary b
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = $1 {source_filter}
              AND (b.pending_id IS NULL OR m.id < b.pending_id)
            ORDER BY m.id DESC LIMIT $2
            """,
            conversation_id,
            max_messages,
        )
        return [dict(row) for row in reversed(rows)]

    async def query_by_time_range(
        self,
        *,
        conversation_id: int,
        start: datetime,
        end: datetime,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""
            WITH boundary AS (
                SELECT MIN(p.id) AS pending_id FROM messages p
                WHERE p.conversation_id = $1
                  AND p.enrichment_status IN ('pending', 'streaming')
            )
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            CROSS JOIN boundary b
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = $1 AND m.occurred_at BETWEEN $2 AND $3
              AND m.enrichment_status NOT IN ('pending', 'streaming')
              AND (b.pending_id IS NULL OR m.id < b.pending_id)
            ORDER BY m.occurred_at ASC, m.id ASC LIMIT $4
            """,
            conversation_id,
            start,
            end,
            limit,
        )
        return [dict(row) for row in rows]

    async def touch_accessed(self, ids: list[int]) -> None:
        if ids:
            await self._pool.execute("UPDATE messages SET last_accessed_at = now() WHERE id = ANY($1)", ids)

    async def get_cross_context_background(self, *, conversation_id: int) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = $1 AND m.source = 'sophos'
              AND m.enrichment_status IN ('completed', 'failed')
              AND m.metadata ? 'cross_context'
            ORDER BY m.occurred_at DESC, m.id DESC LIMIT 1
            """,
            conversation_id,
        )
        return dict(row) if row else None

    async def get_recent_global(
        self,
        *,
        exclude_conversation_id: int | None = None,
        limit: int = 50,
        min_self_messages: int = 5,
    ) -> list[dict[str, Any]]:
        rows = await self._recent_global_query(
            exclude_conversation_id=exclude_conversation_id,
            limit=limit,
            source=None,
        )
        sophos_count = sum(row.get("source") == "sophos" for row in rows)
        if sophos_count < min_self_messages:
            extras = await self._recent_global_query(
                exclude_conversation_id=exclude_conversation_id,
                limit=min_self_messages - sophos_count + len(rows),
                source="sophos",
            )
            seen = {row["id"] for row in rows}
            for row in extras:
                if row["id"] not in seen:
                    rows.append(row)
                    seen.add(row["id"])
        rows.sort(key=lambda row: (row["timestamp"], row["id"]))
        return rows[-limit:]

    async def _recent_global_query(
        self,
        *,
        exclude_conversation_id: int | None,
        limit: int,
        source: str | None,
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE ($1::bigint IS NULL OR m.conversation_id != $1)
              AND ($2::text IS NULL OR m.source = $2)
              AND m.enrichment_status IN ('completed', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM messages p
                  WHERE p.conversation_id = m.conversation_id
                    AND p.enrichment_status IN ('pending', 'streaming')
                    AND p.id < m.id
              )
            ORDER BY m.occurred_at DESC, m.id DESC LIMIT $3
            """,
            exclude_conversation_id,
            source,
            limit,
        )
        return [dict(row) for row in rows]

    async def get_max_id(self, *, conversation_id: int) -> int:
        value = await self._pool.fetchval(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE conversation_id = $1",
            conversation_id,
        )
        return int(value)

    async def get_messages_after(
        self,
        *,
        conversation_id: int,
        after_id: int,
        include_co_account: bool = True,
    ) -> list[dict[str, Any]]:
        source_filter = "" if include_co_account else "AND m.source != 'co_account'"
        rows = await self._pool.fetch(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = $1 AND m.id > $2 {source_filter}
            ORDER BY m.id ASC
            """,
            conversation_id,
            after_id,
        )
        return [dict(row) for row in rows]

    async def get_ready_messages_after(
        self,
        *,
        conversation_id: int,
        after_id: int,
        include_co_account: bool = True,
    ) -> list[dict[str, Any]]:
        source_filter = "" if include_co_account else "AND m.source != 'co_account'"
        pending_source_filter = "" if include_co_account else "AND p.source != 'co_account'"
        rows = await self._pool.fetch(
            f"""
            WITH boundary AS (
                SELECT MIN(p.id) AS pending_id FROM messages p
                WHERE p.conversation_id = $1 AND p.id > $2
                  AND p.enrichment_status IN ('pending', 'streaming')
                  {pending_source_filter}
            )
            SELECT {_MESSAGE_COLUMNS}
            FROM messages m
            CROSS JOIN boundary b
            LEFT JOIN user_identities i ON i.id = m.sender_identity_id
            LEFT JOIN users u ON u.id = i.user_id
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = $1 AND m.id > $2 {source_filter}
              AND m.enrichment_status IN ('completed', 'failed')
              AND (b.pending_id IS NULL OR m.id < b.pending_id)
            ORDER BY m.id ASC
            """,
            conversation_id,
            after_id,
        )
        return [dict(row) for row in rows]

    async def get_delivery_locator(self, message_id: int) -> tuple[int, int, str] | None:
        row = await self._pool.fetchrow(
            """
            SELECT adapter_binding_id, conversation_id, external_message_id
            FROM messages WHERE id = $1 AND external_message_id IS NOT NULL
            """,
            message_id,
        )
        if not row:
            return None
        return row["adapter_binding_id"], row["conversation_id"], row["external_message_id"]

    async def _resolve_reply_target(
        self,
        *,
        adapter_binding_id: int,
        conversation_id: int,
        segments: list[dict[str, Any]],
    ) -> int | None:
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "reply":
                continue
            data = segment.get("data", {})
            external_id = data.get("id") or data.get("external_message_id")
            if external_id is None:
                return None
            value = await self._pool.fetchval(
                """
                SELECT id FROM messages
                WHERE adapter_binding_id = $1 AND conversation_id = $2
                  AND external_message_id = $3
                """,
                adapter_binding_id,
                conversation_id,
                str(external_id),
            )
            return int(value) if value is not None else None
        return None

    @staticmethod
    def _initial_enrichment_status(segments: list[Any]) -> str:
        return "pending" if any(
            isinstance(segment, dict) and segment.get("type") != "text" for segment in segments
        ) else "completed"
