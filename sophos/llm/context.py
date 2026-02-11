"""上下文构建：将 MessageStore 的消息数据转换为 LLM 可用的 messages 列表。

两种模式：
- 多轮模式（默认）：每条消息独立为一个 Message，bot 消息用 assistant role
- 拍平模式（降级）：所有消息合并为单条 user message，兼容不支持连续 user message 的模型

通过 settings.llm_flatten_context 开关切换。
"""

from datetime import timedelta, timezone
from typing import Any

from sophos.config import settings
from sophos.llm.provider import Message
from sophos.message_store import MessageStore


async def build_chat_context(
    store: MessageStore,
    *,
    group_id: int | None = None,
    user_id: int | None = None,
    system_prompt: str,
) -> list[Message]:
    """从 MessageStore 构建 LLM 对话上下文。

    Args:
        store:         消息存储实例
        group_id:      群聊 ID（群聊时传入）
        user_id:       用户 ID（私聊时传入）
        system_prompt: 系统提示词

    Returns:
        OpenAI 格式的 messages 列表，以 system message 开头
    """
    rows = await store.get_context(
        group_id=group_id,
        user_id=user_id,
        include_co_account=settings.include_co_account_in_context,
    )

    if settings.llm_flatten_context:
        return _build_flat(rows, system_prompt)
    return _build_multi_turn(rows, system_prompt)


def _build_multi_turn(
    rows: list[dict[str, Any]], system_prompt: str
) -> list[Message]:
    """多轮模式：每条消息独立，bot 消息用 assistant role。"""
    messages: list[Message] = [{"role": "system", "content": system_prompt}]

    for row in rows:
        if row.get("source") == "sophos":
            # bot 自己的消息 → assistant role，不加前缀（role 本身就是身份信号）
            messages.append({"role": "assistant", "content": row.get("plain_text", "")})
        else:
            # 他人消息 → user role，用 user_schema 格式化
            content = _apply_schema(
                settings.llm_user_schema,
                time=_format_timestamp(row),
                name=_get_display_name(row),
                uid=str(row.get("user_id", "")),
                message=row.get("plain_text", ""),
            )
            messages.append({"role": "user", "content": content})

    return messages


def _build_flat(
    rows: list[dict[str, Any]], system_prompt: str
) -> list[Message]:
    """拍平模式：所有消息合并为单条 user message。"""
    lines: list[str] = []
    bot_name = settings.bot_nickname or "Sophos"

    for row in rows:
        if row.get("source") == "sophos":
            line = _apply_schema(
                settings.llm_bot_schema,
                time=_format_timestamp(row),
                name=bot_name,
                uid="",
                message=row.get("plain_text", ""),
            )
        else:
            line = _apply_schema(
                settings.llm_user_schema,
                time=_format_timestamp(row),
                name=_get_display_name(row),
                uid=str(row.get("user_id", "")),
                message=row.get("plain_text", ""),
            )
        lines.append(line)

    chat_log = "\n".join(lines)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"以下是最近的聊天记录：\n\n{chat_log}"},
    ]


# ── 格式化工具 ───────────────────────────────────────────


def _apply_schema(
    schema: str,
    *,
    time: str = "",
    name: str = "",
    uid: str = "",
    message: str = "",
) -> str:
    """应用 schema 模板，替换 {{placeholder}} 占位符。"""
    return (
        schema
        .replace("{{time}}", time)
        .replace("{{name}}", name)
        .replace("{{uid}}", uid)
        .replace("{{message}}", message)
    )


def _format_timestamp(row: dict[str, Any]) -> str:
    """将 UTC 时间戳转为本地时间字符串 MM-DD HH:MM。"""
    ts = row.get("timestamp")
    if ts is None:
        return "??-?? ??:??"
    local_tz = timezone(timedelta(hours=settings.timezone_offset))
    local_time = ts.astimezone(local_tz)
    return local_time.strftime("%m-%d %H:%M")


def _get_display_name(row: dict[str, Any]) -> str:
    """获取显示名：优先群名片 card，其次昵称 nickname。"""
    card = row.get("card", "")
    if card:
        return card
    return row.get("nickname", "") or str(row.get("user_id", "未知"))
