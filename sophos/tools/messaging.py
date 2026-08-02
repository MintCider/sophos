"""Platform-neutral tools exposed to the model."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from sophos import runtime_config
from sophos.llm.context import apply_schema, format_timestamp, get_display_name
from sophos.message_store import MessageStore
from sophos.messaging import MessageService
from sophos.platform import Capability, SendMessageRequest
from sophos.tools.base import Tool


class SendMessageTool(Tool):
    category = "output"
    group = "messaging"
    name = "send_message"
    description = "向当前或指定内部会话发送消息；消息、用户和会话参数均使用 Sophos 内部 ID"
    required_capabilities = frozenset({Capability.MESSAGE_SEND.value})
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "文本内容"},
            "conversation_id": {"type": "integer", "description": "目标内部会话 ID；默认当前会话"},
            "reply_to_message_id": {"type": "integer", "description": "要回复的内部消息 ID"},
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要提及的内部用户 ID",
            },
            "attachment_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要发送的内部附件 ID",
            },
            "background": {"type": "string", "description": "跨会话发送时必填的背景摘要"},
        },
        "required": ["text"],
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        service: MessageService = context["message_service"]
        current_conversation_id = int(context["conversation_id"])
        conversation_id = int(params.get("conversation_id") or current_conversation_id)
        background = str(params.get("background") or "").strip()
        if conversation_id != current_conversation_id and not background:
            return {"error": "跨会话发送必须提供 background，说明来源、原因和关键信息"}
        text = re.sub(r"^\[回复[^\]]*\]\s*", "", str(params["text"]))
        metadata: dict[str, Any] = {}
        if background:
            metadata["cross_context"] = {
                "summary": background,
                "source_conversation_id": current_conversation_id,
            }
        sent = await service.send_message(
            SendMessageRequest(
                conversation_id=conversation_id,
                text=text,
                reply_to_message_id=params.get("reply_to_message_id"),
                mention_user_ids=tuple(params.get("mention_user_ids") or ()),
                attachment_ids=tuple(params.get("attachment_ids") or ()),
                metadata=metadata,
            )
        )
        return {
            "status": "ok",
            "message_id": sent.message_id,
            "conversation_id": sent.conversation_id,
            "message_text": text,
        }


class RecallMessageTool(Tool):
    category = "output"
    group = "messaging"
    name = "recall_message"
    description = "撤回一条由内部消息 ID 指定的消息"
    required_capabilities = frozenset({Capability.MESSAGE_RECALL.value})
    parameters = {
        "type": "object",
        "properties": {"message_id": {"type": "integer", "description": "内部消息 ID"}},
        "required": ["message_id"],
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        service: MessageService = context["message_service"]
        await service.recall_message(int(params["message_id"]))
        return {"status": "ok", "message_id": int(params["message_id"])}


class QueryMessagesTool(Tool):
    group = "messaging"
    name = "query_messages"
    description = "按时间查询当前或指定内部会话的历史消息"
    parameters = {
        "type": "object",
        "properties": {
            "conversation_id": {"type": "integer", "description": "内部会话 ID；默认当前会话"},
            "anchor_time": {"type": "string", "description": "锚点时间 YYYY-MM-DD HH:MM；默认当前时间"},
            "before_minutes": {"type": "integer", "description": "锚点前分钟数，默认 5"},
            "after_minutes": {"type": "integer", "description": "锚点后分钟数，默认 0"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "返回数量，默认 30"},
        },
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        store: MessageStore = context["store"]
        conversation_id = int(params.get("conversation_id") or context["conversation_id"])
        timezone = ZoneInfo(context.get("timezone") or "Asia/Shanghai")
        anchor_value = params.get("anchor_time")
        try:
            anchor = (
                datetime.strptime(str(anchor_value), "%Y-%m-%d %H:%M").replace(tzinfo=timezone)
                if anchor_value
                else datetime.now(tz=timezone)
            )
        except ValueError:
            return {"error": f"时间格式错误，应为 YYYY-MM-DD HH:MM，收到: {anchor_value}"}
        start = (anchor - timedelta(minutes=int(params.get("before_minutes", 5)))).astimezone(UTC)
        end = (anchor + timedelta(minutes=int(params.get("after_minutes", 0)))).astimezone(UTC)
        limit = min(max(int(params.get("limit", 30)), 1), 100)
        rows = await store.query_by_time_range(
            conversation_id=conversation_id,
            start=start,
            end=end,
            limit=limit,
        )
        await store.touch_accessed([int(row["id"]) for row in rows])
        lines = [self._format_row(row) for row in rows]
        return {
            "conversation_id": conversation_id,
            "messages": "\n".join(lines) if lines else "(无记录)",
            "count": len(lines),
        }

    @staticmethod
    def _format_row(row: dict[str, Any]) -> str:
        schema = (
            runtime_config.get("llm_bot_schema")
            if row.get("source") == "sophos"
            else runtime_config.get("llm_user_schema")
        )
        return apply_schema(
            schema,
            time=format_timestamp(row),
            mid=str(row["id"]),
            name="Sophos" if row.get("source") == "sophos" else get_display_name(row),
            uid="" if row.get("source") == "sophos" else str(row.get("user_id", "")),
            message=str(row.get("plain_text") or ""),
        )


class GetUserTool(Tool):
    group = "directory"
    name = "get_user"
    description = "查询内部用户及其已认证或已观察的平台身份"
    parameters = {
        "type": "object",
        "properties": {"user_id": {"type": "integer", "description": "内部用户 ID"}},
        "required": ["user_id"],
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        pool: asyncpg.Pool = context["pool"]
        user_id = int(params["user_id"])
        user = await pool.fetchrow("SELECT id, display_name, metadata FROM users WHERE id = $1", user_id)
        if not user:
            return {"error": f"用户不存在: {user_id}"}
        identities = await pool.fetch(
            """
            SELECT id, platform, identity_namespace, display_name, verification_method, verified_at
            FROM user_identities WHERE user_id = $1 ORDER BY id
            """,
            user_id,
        )
        roles = await pool.fetch("SELECT role FROM user_roles WHERE user_id = $1 ORDER BY role", user_id)
        return {
            "user_id": user_id,
            "display_name": user["display_name"],
            "roles": [row["role"] for row in roles],
            "identities": [dict(row) for row in identities],
        }


class GetConversationTool(Tool):
    group = "directory"
    name = "get_conversation"
    description = "查询内部会话的类型、名称、父会话和当前适配器能力"
    parameters = {
        "type": "object",
        "properties": {"conversation_id": {"type": "integer", "description": "内部会话 ID"}},
        "required": ["conversation_id"],
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        conversation_id = int(params["conversation_id"])
        conversation = await context["platform_store"].get_conversation(conversation_id)
        if conversation is None:
            return {"error": f"会话不存在: {conversation_id}"}
        result = {
            "conversation_id": conversation.conversation_id,
            "kind": conversation.kind.value,
            "display_name": conversation.display_name,
            "parent_conversation_id": conversation.parent_conversation_id,
        }
        if conversation.account_id == context.get("account_id"):
            result["capabilities"] = sorted(context.get("adapter_capabilities", ()))
        return result


class ListConversationMembersTool(Tool):
    group = "directory"
    name = "list_conversation_members"
    description = "列出当前或指定内部会话的成员，返回内部用户 ID"
    required_capabilities = frozenset({Capability.MEMBER_LIST.value})
    parameters = {
        "type": "object",
        "properties": {
            "conversation_id": {"type": "integer", "description": "内部会话 ID；默认当前会话"}
        },
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        service: MessageService = context["message_service"]
        conversation_id = int(params.get("conversation_id") or context["conversation_id"])
        conversation = await service.platform_store.get_conversation(conversation_id)
        if conversation is None:
            return {"error": f"会话不存在: {conversation_id}"}
        adapter = service.router.for_account(conversation.account_id, Capability.MEMBER_LIST)
        method = getattr(adapter, "list_conversation_members", None)
        if method is None:
            return {"error": "当前适配器未实现成员目录能力"}
        return {"conversation_id": conversation_id, "members": await method(conversation)}


class ModerateMemberTool(Tool):
    category = "output"
    group = "moderation"
    name = "moderate_member"
    description = "在会话中移除、禁言或解除禁言内部用户"
    required_capabilities = frozenset({Capability.MEMBER_MODERATE.value})
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["kick", "ban", "unban"], "description": "管理动作"},
            "user_id": {"type": "integer", "description": "目标内部用户 ID"},
            "conversation_id": {"type": "integer", "description": "内部会话 ID；默认当前会话"},
            "duration_seconds": {"type": "integer", "minimum": 0, "description": "禁言秒数，默认 1800"},
            "reason": {"type": "string", "description": "管理原因；平台不支持时仅用于审计"},
        },
        "required": ["action", "user_id"],
    }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        service: MessageService = context["message_service"]
        conversation_id = int(params.get("conversation_id") or context["conversation_id"])
        conversation = await service.platform_store.get_conversation(conversation_id)
        if conversation is None:
            return {"error": f"会话不存在: {conversation_id}"}
        adapter = service.router.for_account(conversation.account_id, Capability.MEMBER_MODERATE)
        method = getattr(adapter, "moderate_member", None)
        if method is None:
            return {"error": "当前适配器未实现成员管理能力"}
        await method(
            conversation,
            user_id=int(params["user_id"]),
            action=str(params["action"]),
            duration_seconds=params.get("duration_seconds"),
            reason=params.get("reason"),
        )
        return {
            "status": "ok",
            "conversation_id": conversation_id,
            "user_id": int(params["user_id"]),
            "action": str(params["action"]),
        }


ALL_MESSAGING_TOOLS: list[Tool] = [
    SendMessageTool(),
    RecallMessageTool(),
    QueryMessagesTool(),
    GetUserTool(),
    GetConversationTool(),
    ListConversationMembersTool(),
    ModerateMemberTool(),
]
