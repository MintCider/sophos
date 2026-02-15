"""上下文构建：将 MessageStore 的消息数据转换为 LLM 可用的 messages 列表。

两种模式：
- 多轮模式（默认）：每条消息独立为一个 Message，bot 消息用 assistant role
- 拍平模式（降级）：所有消息合并为单条 user message，兼容不支持连续 user message 的模型

通过 settings.llm_flatten_context 开关切换。

跨 context 背景注入（settings.cross_context_mode）：
- "system"：最近一条 cross_context 背景追加到 system prompt
- "inline"：每条带 background 的 Sophos 消息附带背景信息
- "off"：不注入
"""

import json
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

    # system 模式：查询最近一条 cross_context 背景，追加到 system prompt
    mode = settings.cross_context_mode
    if mode == "system":
        bg_row = await store.get_cross_context_background(
            group_id=group_id, user_id=user_id,
        )
        if bg_row is not None:
            system_prompt = system_prompt + _format_bg_for_system(bg_row)

    if settings.llm_flatten_context:
        return _build_flat(rows, system_prompt, inline_bg=(mode == "inline"))
    return _build_multi_turn(rows, system_prompt, inline_bg=(mode == "inline"))


def _build_multi_turn(
    rows: list[dict[str, Any]], system_prompt: str, *, inline_bg: bool = False,
) -> list[Message]:
    """多轮模式：每条消息独立，bot 消息用 assistant role。"""
    messages: list[Message] = [{"role": "system", "content": system_prompt}]

    for row in rows:
        if row.get("source") == "sophos":
            content = row.get("plain_text", "")
            if inline_bg:
                content = _maybe_append_inline_bg(content, row)
            messages.append({"role": "assistant", "content": content})
        else:
            content = apply_schema(
                settings.llm_user_schema,
                time=format_timestamp(row),
                mid=str(row.get("message_id", "")),
                name=get_display_name(row),
                uid=str(row.get("user_id", "")),
                message=row.get("plain_text", ""),
            )
            messages.append({"role": "user", "content": content})

    return messages


def _build_flat(
    rows: list[dict[str, Any]], system_prompt: str, *, inline_bg: bool = False,
) -> list[Message]:
    """拍平模式：所有消息合并为单条 user message。"""
    lines: list[str] = []
    bot_name = settings.bot_nickname or "Sophos"

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
            if inline_bg:
                line = _maybe_append_inline_bg(line, row)
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

    chat_log = "\n".join(lines)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"以下是最近的聊天记录：\n\n{chat_log}"},
    ]


# ── 格式化工具（公开 API，供 QueryMessagesTool 等复用）────


def apply_schema(
    schema: str,
    *,
    time: str = "",
    mid: str = "",
    name: str = "",
    uid: str = "",
    message: str = "",
) -> str:
    """应用 schema 模板，替换 {{placeholder}} 占位符。"""
    return (
        schema
        .replace("{{time}}", time)
        .replace("{{mid}}", mid)
        .replace("{{name}}", name)
        .replace("{{uid}}", uid)
        .replace("{{message}}", message)
    )


def describe_schema(schema: str) -> str:
    """将 schema 模板转为人类可读的格式说明（给 LLM 看）。"""
    return apply_schema(
        schema, time="时间", mid="消息ID", name="昵称", uid="QQ号", message="内容",
    )


def format_timestamp(row: dict[str, Any]) -> str:
    """将 UTC 时间戳转为本地时间字符串 YYYY-MM-DD HH:MM。"""
    ts = row.get("timestamp")
    if ts is None:
        return "????-??-?? ??:??"
    local_tz = timezone(timedelta(hours=settings.timezone_offset))
    local_time = ts.astimezone(local_tz)
    return local_time.strftime("%Y-%m-%d %H:%M")


def get_display_name(row: dict[str, Any]) -> str:
    """获取显示名：优先群名片 card，其次昵称 nickname。"""
    card = row.get("card", "")
    if card:
        return card
    return row.get("nickname", "") or str(row.get("user_id", "未知"))


# ── 跨 context 背景格式化 ─────────────────────────────────


def _format_bg_for_system(bg_row: dict[str, Any]) -> str:
    """将 cross_context 背景格式化为 system prompt 追加段落。"""
    extra = bg_row.get("extra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra)
    cc = extra.get("cross_context", {})
    summary = cc.get("summary", "")
    source_type = cc.get("source_type", "unknown")
    source_id = cc.get("source_id", "")
    ts = format_timestamp(bg_row)

    type_label = "群聊" if source_type == "group" else "私聊"
    return (
        f"\n---\n"
        f"[跨对话背景] 最近一次跨 context 对话来自{type_label} {source_id}"
        f"（{ts}）：{summary}\n"
        f"如需查看原始对话，可使用 query_messages 工具。"
    )


def _maybe_append_inline_bg(content: str, row: dict[str, Any]) -> str:
    """inline 模式：如果消息带 cross_context 背景，附加到内容末尾。"""
    extra = row.get("extra")
    if not extra:
        return content
    if isinstance(extra, str):
        extra = json.loads(extra)
    cc = extra.get("cross_context")
    if not cc:
        return content

    summary = cc.get("summary", "")
    source_type = cc.get("source_type", "unknown")
    source_id = cc.get("source_id", "")
    ts = format_timestamp(row)

    type_label = "群聊" if source_type == "group" else "私聊"
    return (
        f"{content}\n---\n"
        f"[背景] 来自{type_label} {source_id}（{ts}）：{summary}"
    )

