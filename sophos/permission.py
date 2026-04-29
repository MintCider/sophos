"""权限系统。

Flat ACL 模型：
- perm_scope: 会话开关（行不存在 = 禁用）
- perm_grant: 用户权限授予（行存在 = 已授权）
- perm_tool:  工具白名单（无行 = 全部可用）

Master 隐式拥有所有权限。缓存策略：首次访问全量加载，写操作后 invalidate。
"""

import logging

import asyncpg

from sophos.config import settings

logger = logging.getLogger(__name__)

# ── Master ────────────────────────────────────────────────

_masters: frozenset[int] | None = None


def _get_masters() -> frozenset[int]:
    global _masters  # noqa: PLW0603
    if _masters is None:
        raw = settings.master_qq
        ids: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        _masters = frozenset(ids)
    return _masters


def is_master(user_id: int) -> bool:
    """检查是否为 master。"""
    return user_id in _get_masters()


# ── 缓存 ─────────────────────────────────────────────────

_scope_cache: dict[tuple[str, int], bool] | None = None
_grant_cache: dict[tuple[int, str, int], set[str]] | None = None
_pool_ref: asyncpg.Pool | None = None


def invalidate() -> None:
    """清除缓存，下次访问时重新加载。"""
    global _scope_cache, _grant_cache  # noqa: PLW0603
    _scope_cache = None
    _grant_cache = None


async def _ensure_scope_cache(pool: asyncpg.Pool) -> dict[tuple[str, int], bool]:
    global _scope_cache  # noqa: PLW0603
    if _scope_cache is not None:
        return _scope_cache
    rows = await pool.fetch("SELECT scope_type, scope_id, enabled FROM perm_scope")
    _scope_cache = {(r["scope_type"], r["scope_id"]): r["enabled"] for r in rows}
    return _scope_cache


async def _ensure_grant_cache(
    pool: asyncpg.Pool,
) -> dict[tuple[int, str, int], set[str]]:
    global _grant_cache  # noqa: PLW0603
    if _grant_cache is not None:
        return _grant_cache
    rows = await pool.fetch("SELECT user_id, scope_type, scope_id, permission FROM perm_grant")
    cache: dict[tuple[int, str, int], set[str]] = {}
    for r in rows:
        key = (r["user_id"], r["scope_type"], r["scope_id"])
        cache.setdefault(key, set()).add(r["permission"])
    _grant_cache = cache
    return _grant_cache


# ── 查询 ─────────────────────────────────────────────────


async def is_scope_enabled(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
) -> bool:
    """检查会话是否启用。行不存在 = 禁用。"""
    cache = await _ensure_scope_cache(pool)
    return cache.get((scope_type, scope_id), False)


async def has_permission(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
    permission: str,
) -> bool:
    """检查用户是否拥有指定权限。Master 隐式通过。"""
    if is_master(user_id):
        return True
    cache = await _ensure_grant_cache(pool)
    # scope-specific
    scope_perms = cache.get((user_id, scope_type, scope_id), set())
    if permission in scope_perms:
        return True
    # global fallback
    global_perms = cache.get((user_id, "global", 0), set())
    return permission in global_perms


# ── 写操作 ────────────────────────────────────────────────


async def set_scope_enabled(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
    enabled: bool,
) -> None:
    """启用/禁用会话。"""
    await pool.execute(
        "INSERT INTO perm_scope (scope_type, scope_id, enabled) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (scope_type, scope_id) "
        "DO UPDATE SET enabled = $3, updated_at = now()",
        scope_type,
        scope_id,
        enabled,
    )
    invalidate()


async def grant(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
    permission: str,
    granted_by: int,
) -> None:
    """授予权限。"""
    await pool.execute(
        "INSERT INTO perm_grant (user_id, scope_type, scope_id, permission, granted_by) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
        user_id,
        scope_type,
        scope_id,
        permission,
        granted_by,
    )
    invalidate()


async def revoke(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
    permission: str,
) -> bool:
    """撤销权限。返回是否存在并已删除。"""
    result = await pool.execute(
        "DELETE FROM perm_grant WHERE user_id=$1 AND scope_type=$2 AND scope_id=$3 AND permission=$4",
        user_id,
        scope_type,
        scope_id,
        permission,
    )
    invalidate()
    return result == "DELETE 1"


async def batch_grant(
    pool: asyncpg.Pool,
    user_ids: list[int],
    scope_type: str,
    scope_id: int,
    permission: str,
    granted_by: int,
) -> int:
    """批量授予权限。返回新增数量。"""
    if not user_ids:
        return 0
    count = 0
    for uid in user_ids:
        result = await pool.execute(
            "INSERT INTO perm_grant (user_id, scope_type, scope_id, permission, granted_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            uid,
            scope_type,
            scope_id,
            permission,
            granted_by,
        )
        if result == "INSERT 0 1":
            count += 1
    invalidate()
    return count


async def batch_revoke(
    pool: asyncpg.Pool,
    user_ids: list[int],
    scope_type: str,
    scope_id: int,
    permission: str,
) -> int:
    """批量撤销权限。返回删除数量。"""
    if not user_ids:
        return 0
    count = 0
    for uid in user_ids:
        result = await pool.execute(
            "DELETE FROM perm_grant WHERE user_id=$1 AND scope_type=$2 AND scope_id=$3 AND permission=$4",
            uid,
            scope_type,
            scope_id,
            permission,
        )
        if result == "DELETE 1":
            count += 1
    invalidate()
    return count


async def list_grants(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
) -> list[tuple[int, str]]:
    """列出某个 scope 下所有授权。返回 [(user_id, permission), ...]。"""
    rows = await pool.fetch(
        "SELECT user_id, permission FROM perm_grant WHERE scope_type=$1 AND scope_id=$2 ORDER BY user_id, permission",
        scope_type,
        scope_id,
    )
    return [(r["user_id"], r["permission"]) for r in rows]


async def list_user_grants(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
) -> list[str]:
    """列出用户在某个 scope 下的所有权限。"""
    cache = await _ensure_grant_cache(pool)
    perms = set()
    perms.update(cache.get((user_id, scope_type, scope_id), set()))
    perms.update(cache.get((user_id, "global", 0), set()))
    return sorted(perms)


# ── 工具白名单 ────────────────────────────────────────────


async def get_tool_whitelist(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
) -> list[str] | None:
    """获取工具白名单。None = 全部可用（无行）。"""
    rows = await pool.fetch(
        "SELECT tool_name FROM perm_tool WHERE scope_type=$1 AND scope_id=$2",
        scope_type,
        scope_id,
    )
    if not rows:
        return None
    return [r["tool_name"] for r in rows]


async def add_tools(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
    tool_names: list[str],
) -> int:
    """添加工具到白名单。返回新增数量。"""
    count = 0
    for name in tool_names:
        result = await pool.execute(
            "INSERT INTO perm_tool (scope_type, scope_id, tool_name) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            scope_type,
            scope_id,
            name,
        )
        if result == "INSERT 0 1":
            count += 1
    return count


async def remove_tools(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
    tool_names: list[str],
) -> int:
    """从白名单移除工具。返回删除数量。"""
    count = 0
    for name in tool_names:
        result = await pool.execute(
            "DELETE FROM perm_tool WHERE scope_type=$1 AND scope_id=$2 AND tool_name=$3",
            scope_type,
            scope_id,
            name,
        )
        if result == "DELETE 1":
            count += 1
    return count


async def reset_tools(
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
) -> None:
    """重置工具白名单（删除所有行 = 全部可用）。"""
    await pool.execute(
        "DELETE FROM perm_tool WHERE scope_type=$1 AND scope_id=$2",
        scope_type,
        scope_id,
    )
