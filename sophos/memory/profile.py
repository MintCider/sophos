"""档案注入 — 构建本能记忆 prompt 块。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sophos.memory.store import MemoryStore


async def build_profile_block(
    store: MemoryStore,
    *,
    scope_type: str,
    scope_id: int,
    context_user_ids: list[int],
    context_text: str,
) -> str:
    """构建档案 prompt 块。无内容时返回空字符串。"""
    parts: list[str] = []

    # 自我档案（全局）
    self_content = await store.get_profile_self()
    if self_content:
        parts.append(f"自我: {self_content}")

    # 会话档案
    ctx_content = await store.get_profile_context(scope_type, scope_id)
    if ctx_content:
        parts.append(f"当前会话: {ctx_content}")

    # 用户档案：按 ID 匹配 + 按关键词匹配
    by_id = await store.get_profile_users_by_ids(context_user_ids)
    by_kw = await store.get_profile_users_by_keyword(context_text)

    # 合并去重
    seen: set[int] = set()
    user_lines: list[str] = []
    for m in by_id + by_kw:
        uid = m["user_id"]
        if uid in seen:
            continue
        seen.add(uid)
        user_lines.append(f"- user_id={uid}: {m['content']}")

    if user_lines:
        parts.append("用户:\n" + "\n".join(user_lines))

    if not parts:
        return ""
    return "[档案]\n" + "\n".join(parts)
