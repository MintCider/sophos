"""Dynamic, platform-neutral identity metadata for the agent system prompt."""

from __future__ import annotations

import asyncpg

from sophos import permission


async def build_identity_variables(
    pool: asyncpg.Pool,
    *,
    current_user_id: int,
    self_user_id: int,
    conversation_id: int,
    conversation_kind: str,
) -> dict[str, str]:
    roles = await pool.fetch(
        "SELECT role FROM user_roles WHERE user_id = $1 ORDER BY role",
        current_user_id,
    )
    masters = await permission.list_masters(pool)
    master_lines = [
        f"- user_id={master['user_id']}, display_name={master['display_name'] or '未命名'}"
        for master in masters
    ]
    if not master_lines:
        master_lines = ["- 当前未配置 Master"]
    current_roles = ", ".join(str(row["role"]) for row in roles) or "无"
    master_users = "\n".join(master_lines)
    identity_context = (
        "[Sophos 内部身份]\n"
        f"你的统一用户ID：{self_user_id}\n"
        f"当前用户：user_id={current_user_id}，roles=[{current_roles}]\n"
        f"当前会话：conversation_id={conversation_id}，kind={conversation_kind}\n"
        "Master（主人）用户：\n"
        + master_users
        + "\n工具参数中的 user_id、conversation_id、message_id 均为 Sophos 内部 ID；"
        "不要使用 QQ 号或其他平台原始 ID 调用工具。"
    )
    return {
        "self_user_id": str(self_user_id),
        "current_user_id": str(current_user_id),
        "current_user_roles": current_roles,
        "conversation_id": str(conversation_id),
        "conversation_kind": conversation_kind,
        "master_users": master_users,
        "identity_context": identity_context,
    }


async def build_identity_block(
    pool: asyncpg.Pool,
    *,
    current_user_id: int,
    self_user_id: int,
    conversation_id: int,
    conversation_kind: str,
) -> str:
    """Compatibility wrapper for callers needing the complete identity block."""
    values = await build_identity_variables(
        pool,
        current_user_id=current_user_id,
        self_user_id=self_user_id,
        conversation_id=conversation_id,
        conversation_kind=conversation_kind,
    )
    return values["identity_context"]
