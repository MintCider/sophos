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
    """通用 OneBot API 工具：直接将参数转发给 api.call()。

    自动从 context 补全 group_id（如果 schema 中声明了但调用时未提供）。
    """

    def __init__(
        self,
        *,
        action: str,
        name: str,
        description: str,
        parameters: dict[str, Any],
        category: str = "input",
        scope: str = "all",
    ) -> None:
        self._action = action
        self._name = name
        self._description = description
        self._parameters = parameters
        self._category = category
        self._scope = scope

    @property
    def category(self) -> str:
        return self._category

    @property
    def scope(self) -> str:
        return self._scope

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
        # 自动注入 group_id：schema 中有此字段但调用时未提供，从 context 补全
        if "group_id" not in params and "group_id" in context:
            if "group_id" in self._parameters.get("properties", {}):
                params["group_id"] = context["group_id"]
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
                "text": {
                    "type": "string",
                    "description": "要发送的文本内容",
                },
                "message_type": {
                    "type": "string",
                    "enum": ["group", "private"],
                    "description": "消息类型（可选，默认当前会话类型）",
                },
                "target_id": {
                    "type": "integer",
                    "description": "目标 ID（可选，默认当前会话；跨群/跨私聊时需指定）",
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
            "required": ["text"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        api: OneBotAPI = context["api"]
        store: MessageStore = context["store"]

        # 从 context 补全 message_type 和 target_id
        msg_type = params.get("message_type") or context.get("message_type", "group")
        target_id: int = params.get("target_id") or (
            context.get("group_id") if msg_type == "group" else context.get("user_id")
        ) or 0

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


# ── 输出工具 ─────────────────────────────────────────────

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


class GroupAdminTool(Tool):
    """群管理操作合集：踢人、禁言、设置群名片、改群名、退群、设置头衔。

    合并 7 个 set_group_* API 为一个工具，减少 tool 数量。
    group_id 可从 context 自动注入。
    """

    # action → OneBot API action 名
    _ACTION_MAP: dict[str, str] = {
        "kick": "set_group_kick",
        "ban": "set_group_ban",
        "whole_ban": "set_group_whole_ban",
        "card": "set_group_card",
        "name": "set_group_name",
        "leave": "set_group_leave",
        "special_title": "set_group_special_title",
    }

    @property
    def category(self) -> str:
        return "output"

    @property
    def scope(self) -> str:
        return "group"

    @property
    def name(self) -> str:
        return "group_admin"

    @property
    def description(self) -> str:
        return (
            "群管理操作。action: kick=踢人, ban=禁言(duration秒,0解禁), "
            "whole_ban=全员禁言, card=设群名片, name=改群名, "
            "leave=退群, special_title=设头衔"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTION_MAP.keys()),
                    "description": "操作类型",
                },
                "group_id": {
                    "type": "integer",
                    "description": "群号（可选，默认当前群）",
                },
                "user_id": {
                    "type": "integer",
                    "description": "目标用户 QQ 号（kick/ban/card/special_title 需要）",
                },
                "duration": {
                    "type": "integer",
                    "description": "禁言时长秒，0=解禁，默认1800（ban 用）",
                },
                "enable": {
                    "type": "boolean",
                    "description": "是否开启全员禁言，默认 true（whole_ban 用）",
                },
                "card": {
                    "type": "string",
                    "description": "群名片内容，空串=删除（card 用）",
                },
                "group_name": {
                    "type": "string",
                    "description": "新群名（name 用）",
                },
                "special_title": {
                    "type": "string",
                    "description": "专属头衔，空串=删除（special_title 用）",
                },
                "reject_add_request": {
                    "type": "boolean",
                    "description": "踢人后拒绝加群，默认 false（kick 用）",
                },
                "is_dismiss": {
                    "type": "boolean",
                    "description": "是否解散群，默认 false（leave 用，仅群主）",
                },
            },
            "required": ["action"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        api: OneBotAPI = context["api"]
        action = params.pop("action")
        onebot_action = self._ACTION_MAP.get(action)
        if onebot_action is None:
            return {"error": f"unknown action: {action}"}
        # 自动注入 group_id
        if "group_id" not in params and "group_id" in context:
            params["group_id"] = context["group_id"]
        return await api.call(onebot_action, params)


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
    GroupAdminTool(),
    # 输入工具
    #_get_login_info,
    _get_stranger_info,
    #_get_friend_list,
    _get_group_info,
    #_get_group_list,
    _get_group_member_info,
    _get_group_member_list,
]
