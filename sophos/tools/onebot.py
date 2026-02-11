"""OneBot 11 API 工具集。

包含两类工具：
- 输入工具（input）：查询信息，无副作用
- 输出工具（output）：执行操作，改变状态

大部分工具是简单的 API 转发，用 OneBotTool 通用类声明式定义。
有特殊逻辑的工具（如 send_msg 需要存储消息）用独立类。
"""

from typing import Any

from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.tools.base import Tool


# ── 通用 OneBot 工具类 ──────────────────────────────────────


class OneBotTool(Tool):
    """通用 OneBot API 工具：直接将参数转发给 api.call()。"""

    def __init__(
        self,
        *,
        action: str,
        name: str,
        description: str,
        parameters: dict[str, Any],
        category: str = "input",
    ) -> None:
        self._action = action
        self._name = name
        self._description = description
        self._parameters = parameters
        self._category = category

    @property
    def category(self) -> str:
        return self._category

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        api: OneBotAPI = context["api"]
        return await api.call(self._action, params)


# ── 特殊工具（有额外逻辑）──────────────────────────────────


class SendMessageTool(Tool):
    """发送消息（群聊或私聊），发送后主动存入数据库。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def name(self) -> str:
        return "send_msg"

    @property
    def description(self) -> str:
        return "发送消息到指定的群聊或私聊，支持回复和@"

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
                "reply_to": {
                    "type": "integer",
                    "description": "要回复的消息 ID（可选，会自动添加引用）",
                },
                "at": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "要 @的用户 QQ 号列表（可选）",
                },
            },
            "required": ["message_type", "target_id", "text"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        api: OneBotAPI = context["api"]
        store: MessageStore = context["store"]

        msg_type = params["message_type"]

        # 构建消息段：reply → at → text
        segments: list[dict[str, Any]] = []
        if reply_to := params.get("reply_to"):
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        if at_list := params.get("at"):
            for uid in at_list:
                segments.append({"type": "at", "data": {"qq": str(uid)}})
        segments.append({"type": "text", "data": {"text": params["text"]}})

        api_params: dict[str, Any] = {
            "message_type": msg_type,
            "message": segments,
        }

        target_id = params["target_id"]
        if msg_type == "group":
            api_params["group_id"] = target_id
        else:
            api_params["user_id"] = target_id

        result = await api.call("send_msg", api_params)
        message_id = result.get("message_id")

        if message_id is not None:
            await store.save_self_message(
                message_id=message_id,
                message_type=msg_type,
                group_id=target_id if msg_type == "group" else None,
                user_id=target_id if msg_type == "private" else context.get("self_id", 0),
                raw_message=segments,
            )

        return {"status": "ok", "message_id": message_id}


# ── 输出工具（声明式）─────────────────────────────────────

_delete_msg = OneBotTool(
    action="delete_msg",
    name="delete_msg",
    description="撤回一条消息",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "message_id": {"type": "integer", "description": "要撤回的消息 ID"},
        },
        "required": ["message_id"],
    },
)

_set_group_kick = OneBotTool(
    action="set_group_kick",
    name="set_group_kick",
    description="将指定用户踢出群聊",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "user_id": {"type": "integer", "description": "要踢的用户 QQ 号"},
            "reject_add_request": {"type": "boolean", "description": "是否拒绝此人的加群请求，默认 false"},
        },
        "required": ["group_id", "user_id"],
    },
)

_set_group_ban = OneBotTool(
    action="set_group_ban",
    name="set_group_ban",
    description="禁言群聊中的指定用户",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "user_id": {"type": "integer", "description": "要禁言的用户 QQ 号"},
            "duration": {"type": "integer", "description": "禁言时长（秒），0 表示取消禁言，默认 1800"},
        },
        "required": ["group_id", "user_id"],
    },
)

_set_group_whole_ban = OneBotTool(
    action="set_group_whole_ban",
    name="set_group_whole_ban",
    description="开启或关闭群聊全员禁言",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "enable": {"type": "boolean", "description": "是否开启全员禁言，默认 true"},
        },
        "required": ["group_id"],
    },
)

_set_group_card = OneBotTool(
    action="set_group_card",
    name="set_group_card",
    description="设置指定用户的群名片（群备注）",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "user_id": {"type": "integer", "description": "要设置的用户 QQ 号"},
            "card": {"type": "string", "description": "群名片内容，空字符串表示删除群名片"},
        },
        "required": ["group_id", "user_id"],
    },
)

_set_group_name = OneBotTool(
    action="set_group_name",
    name="set_group_name",
    description="修改群聊名称",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "group_name": {"type": "string", "description": "新群名"},
        },
        "required": ["group_id", "group_name"],
    },
)

_set_group_leave = OneBotTool(
    action="set_group_leave",
    name="set_group_leave",
    description="退出指定群聊（群主可选择解散）",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "is_dismiss": {"type": "boolean", "description": "是否解散群（仅群主有效），默认 false"},
        },
        "required": ["group_id"],
    },
)

_set_group_special_title = OneBotTool(
    action="set_group_special_title",
    name="set_group_special_title",
    description="设置群成员的专属头衔",
    category="output",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "user_id": {"type": "integer", "description": "要设置的用户 QQ 号"},
            "special_title": {"type": "string", "description": "专属头衔，空字符串表示删除"},
        },
        "required": ["group_id", "user_id"],
    },
)


# ── 输入工具（声明式）─────────────────────────────────────

_get_login_info = OneBotTool(
    action="get_login_info",
    name="get_login_info",
    description="获取当前登录号的 QQ 号和昵称",
    parameters={"type": "object", "properties": {}},
)

_get_stranger_info = OneBotTool(
    action="get_stranger_info",
    name="get_stranger_info",
    description="获取指定 QQ 号的昵称、性别、年龄等信息",
    parameters={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "要查询的 QQ 号"},
        },
        "required": ["user_id"],
    },
)

_get_friend_list = OneBotTool(
    action="get_friend_list",
    name="get_friend_list",
    description="获取好友列表（QQ 号、昵称、备注）",
    parameters={"type": "object", "properties": {}},
)

_get_group_info = OneBotTool(
    action="get_group_info",
    name="get_group_info",
    description="获取指定群的名称、成员数等信息",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
        },
        "required": ["group_id"],
    },
)

_get_group_list = OneBotTool(
    action="get_group_list",
    name="get_group_list",
    description="获取已加入的群聊列表",
    parameters={"type": "object", "properties": {}},
)

_get_group_member_info = OneBotTool(
    action="get_group_member_info",
    name="get_group_member_info",
    description="获取指定群成员的详细信息（昵称、群名片、角色、入群时间等）",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
            "user_id": {"type": "integer", "description": "要查询的用户 QQ 号"},
        },
        "required": ["group_id", "user_id"],
    },
)

_get_group_member_list = OneBotTool(
    action="get_group_member_list",
    name="get_group_member_list",
    description="获取指定群的全部成员列表",
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "integer", "description": "群号"},
        },
        "required": ["group_id"],
    },
)


# ── 导出 ──────────────────────────────────────────────────

ALL_TOOLS: list[Tool] = [
    # 输出工具
    SendMessageTool(),
    _delete_msg,
    _set_group_kick,
    _set_group_ban,
    _set_group_whole_ban,
    _set_group_card,
    _set_group_name,
    _set_group_leave,
    _set_group_special_title,
    # 输入工具
    _get_login_info,
    _get_stranger_info,
    _get_friend_list,
    _get_group_info,
    _get_group_list,
    _get_group_member_info,
    _get_group_member_list,
]
