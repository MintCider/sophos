"""OneBot 平台相关的工具集。"""

from typing import Any

from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.tools.base import Tool


class SendMessageTool(Tool):
    """发送消息（群聊或私聊）。"""

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return "发送一条文本消息到指定的群聊或私聊"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "enum": ["group", "private"],
                    "description": "消息类型：group=群聊, private=私聊",
                },
                "target_id": {
                    "type": "integer",
                    "description": "目标 ID（群号或用户 QQ 号）",
                },
                "text": {
                    "type": "string",
                    "description": "要发送的文本内容",
                },
            },
            "required": ["message_type", "target_id", "text"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """通过 OneBot WS 发送消息，返回 message_id。

        发送成功后主动存入数据库，标记 source='sophos'。

        context 中需要:
            api:   OneBotAPI    — OneBot API 调用器
            store: MessageStore — 消息存储
        """
        api: OneBotAPI = context["api"]
        store: MessageStore = context["store"]

        msg_type = params["message_type"]
        raw_message: list[dict[str, Any]] = [{"type": "text", "data": {"text": params["text"]}}]
        api_params: dict[str, Any] = {
            "message_type": msg_type,
            "message": raw_message,
        }

        target_id = params["target_id"]
        if msg_type == "group":
            api_params["group_id"] = target_id
        else:
            api_params["user_id"] = target_id

        result = await api.call("send_msg", api_params)
        message_id = result.get("message_id")

        # 主动存储 Sophos 发出的消息
        if message_id is not None:
            await store.save_self_message(
                message_id=message_id,
                message_type=msg_type,
                group_id=target_id if msg_type == "group" else None,
                user_id=target_id if msg_type == "private" else context.get("self_id", 0),
                raw_message=raw_message,
            )

        return {"status": "ok", "message_id": message_id}


class GetGroupMemberInfoTool(Tool):
    """查询群成员信息。"""

    @property
    def name(self) -> str:
        return "get_group_member_info"

    @property
    def description(self) -> str:
        return "查询指定群聊中某个成员的详细信息（昵称、群名片、角色等）"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "group_id": {
                    "type": "integer",
                    "description": "群号",
                },
                "user_id": {
                    "type": "integer",
                    "description": "要查询的用户 QQ 号",
                },
            },
            "required": ["group_id", "user_id"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """通过 OneBot WS 查询群成员信息，同步等待结果返回。"""
        api: OneBotAPI = context["api"]

        result = await api.call("get_group_member_info", {
            "group_id": params["group_id"],
            "user_id": params["user_id"],
        })
        return result
