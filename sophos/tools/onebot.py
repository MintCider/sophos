"""OneBot 11 API 工具集。

包含两类工具：
- 输入工具（input）：查询信息，无副作用
- 输出工具（output）：执行操作，改变状态

大部分工具是简单的 API 转发，用 OneBotTool 通用类声明式定义。
有特殊逻辑的工具（如 send_msg 需要存储消息）用独立类。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sophos.config import settings
from sophos.llm.context import apply_schema, format_timestamp, get_display_name
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
                "background": {
                    "type": "string",
                    "description": "跨 context 发消息时的背景摘要（几句话概括来龙去脉，跨群/跨私聊时必填）",
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

        # 跨 context 检测：目标与当前会话不同时，必须提供 background
        is_cross = False
        if msg_type != context.get("message_type"):
            is_cross = True
        elif msg_type == "group" and target_id != context.get("group_id"):
            is_cross = True
        elif msg_type == "private" and target_id != context.get("user_id"):
            is_cross = True

        if is_cross and not params.get("background"):
            return {
                "error": "跨 context 发消息必须提供 background 参数，"
                         "简要说明对话背景（来源、原因、关键信息）"
            }

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

        # 构建 extra（跨 context 背景）
        extra = None
        if bg := params.get("background"):
            extra = {
                "cross_context": {
                    "summary": bg,
                    "source_type": context.get("message_type", "group"),
                    "source_id": context.get("group_id") or context.get("user_id"),
                }
            }

        if message_id is not None:
            await store.save_self_message(
                message_id=message_id,
                message_type=msg_type,
                group_id=target_id if msg_type == "group" else None,
                user_id=target_id if msg_type == "private" else context.get("self_id", 0),
                raw_message=segments,
                extra=extra,
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


class QueryMessagesTool(Tool):
    """查询指定会话在某个时间点附近的聊天记录。

    用于跨 context 场景：LLM 看到背景摘要后，可以用此工具回溯原始对话。
    也可用于一般性的历史消息查询。
    """

    @property
    def name(self) -> str:
        return "query_messages"

    @property
    def description(self) -> str:
        return "查询指定会话在某个时间点附近的聊天记录"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "enum": ["group", "private"],
                    "description": "会话类型",
                },
                "target_id": {
                    "type": "integer",
                    "description": "目标 ID（群号或用户 QQ 号）",
                },
                "anchor_time": {
                    "type": "string",
                    "description": "锚点时间，格式 YYYY-MM-DD HH:MM",
                },
                "before_minutes": {
                    "type": "integer",
                    "description": "锚点前多少分钟（默认 5）",
                },
                "after_minutes": {
                    "type": "integer",
                    "description": "锚点后多少分钟（默认 0）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大返回条数（默认 30）",
                },
            },
            "required": ["message_type", "target_id", "anchor_time"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        store: MessageStore = context["store"]

        # 校验必填参数
        for key in ("message_type", "target_id", "anchor_time"):
            if key not in params:
                return {"error": f"缺少必填参数: {key}"}

        # 解析锚点时间（本地时间 → UTC）
        anchor_str = params["anchor_time"]
        try:
            local_tz = timezone(timedelta(hours=settings.timezone_offset))
            local_dt = datetime.strptime(anchor_str, "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
            anchor_utc = local_dt.astimezone(timezone.utc)
        except ValueError:
            return {"error": f"时间格式错误，应为 YYYY-MM-DD HH:MM，收到: {anchor_str}"}

        before = params.get("before_minutes", 5)
        after = params.get("after_minutes", 0)
        limit = params.get("limit", 30)

        start = anchor_utc - timedelta(minutes=before)
        end = anchor_utc + timedelta(minutes=after)

        rows = await store.query_by_time_range(
            message_type=params["message_type"],
            target_id=params["target_id"],
            start=start,
            end=end,
            limit=limit,
        )

        if not rows:
            return {"messages": "(无记录)", "count": 0}

        # 用与上下文相同的 schema 格式化
        bot_name = settings.bot_nickname or "Sophos"
        lines: list[str] = []
        for row in rows:
            if row.get("source") == "sophos":
                line = apply_schema(
                    settings.llm_bot_schema,
                    time=format_timestamp(row),
                    mid=str(row.get("message_id", "")),
                    name=bot_name,
                    uid="",
                    message=row.get("plain_text", ""),
                )
            else:
                line = apply_schema(
                    settings.llm_user_schema,
                    time=format_timestamp(row),
                    mid=str(row.get("message_id", "")),
                    name=get_display_name(row),
                    uid=str(row.get("user_id", "")),
                    message=row.get("plain_text", ""),
                )
            lines.append(line)

        return {"messages": "\n".join(lines), "count": len(lines)}


class CorrectImageDescriptionTool(Tool):
    """纠正图片描述（懒加载）。

    将修正信息写入 image_cache，下次该图片再出现时强制 VLM 重新识别。
    """

    @property
    def name(self) -> str:
        return "correct_image_description"

    @property
    def description(self) -> str:
        return (
            "纠正图片描述。从聊天记录中的 [图片(hash): ...] 获取 hash，"
            "提供正确描述后，下次该图片出现时会重新识别"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_hash": {
                    "type": "string",
                    "description": "图片哈希（从上下文中 [图片(hash): ...] 获取）",
                },
                "correct_description": {
                    "type": "string",
                    "description": "用户指出的正确描述或修正提示",
                },
            },
            "required": ["image_hash", "correct_description"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        store: MessageStore = context["store"]
        image_hash = params["image_hash"]
        correction = params["correct_description"]

        result = await store.pool.execute(
            """
            UPDATE image_cache
            SET correction_hint = $2, pending_correction = true
            WHERE hash = $1
            """,
            image_hash, correction,
        )
        if result == "UPDATE 0":
            return {"error": f"未找到哈希为 {image_hash} 的图片缓存"}
        return {"status": "ok", "message": f"已记录纠正，下次该图片出现时将重新识别"}


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
    CorrectImageDescriptionTool(),
    # 输入工具
    QueryMessagesTool(),
    #_get_login_info,
    _get_stranger_info,
    #_get_friend_list,
    _get_group_info,
    #_get_group_list,
    _get_group_member_info,
    _get_group_member_list,
]
