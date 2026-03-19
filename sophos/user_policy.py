"""用户触发策略。

按用户控制触发行为：抑制 LLM 触发、调整概率倍率、屏蔽 context refresh 注入。
主要用于防止群内多 bot 互触发及恶意用户管控。

缓存策略：首次访问全量加载，写操作后 invalidate。
查询时 scope-specific 优先，global 兜底。Master 豁免。
"""

import logging
from dataclasses import dataclass

import asyncpg

from sophos.permission import is_master

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TriggerPolicy:
    """单条用户触发策略。"""

    user_id: int
    scope_type: str
    scope_id: int
    suppress_llm_trigger: bool
    rate_multiplier: float
    suppress_refresh: bool
    note: str | None


# ── 缓存 ─────────────────────────────────────────────────

_cache: dict[tuple[int, str, int], TriggerPolicy] | None = None


def invalidate() -> None:
    """清除缓存，下次访问时重新加载。"""
    global _cache  # noqa: PLW0603
    _cache = None


async def _ensure_cache(
    pool: asyncpg.Pool,
) -> dict[tuple[int, str, int], TriggerPolicy]:
    global _cache  # noqa: PLW0603
    if _cache is not None:
        return _cache
    rows = await pool.fetch(
        "SELECT user_id, scope_type, scope_id, "
        "suppress_llm_trigger, rate_multiplier, suppress_refresh, note "
        "FROM user_trigger_policy"
    )
    _cache = {}
    for r in rows:
        key = (r["user_id"], r["scope_type"], r["scope_id"])
        _cache[key] = TriggerPolicy(
            user_id=r["user_id"],
            scope_type=r["scope_type"],
            scope_id=r["scope_id"],
            suppress_llm_trigger=r["suppress_llm_trigger"],
            rate_multiplier=r["rate_multiplier"],
            suppress_refresh=r["suppress_refresh"],
            note=r["note"],
        )
    return _cache


# ── 查询 ─────────────────────────────────────────────────


async def get_policy(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
) -> TriggerPolicy | None:
    """获取用户触发策略。Master 豁免（返回 None）。

    查找顺序：scope-specific → global fallback。
    """
    if is_master(user_id):
        return None
    cache = await _ensure_cache(pool)
    policy = cache.get((user_id, scope_type, scope_id))
    if policy is not None:
        return policy
    return cache.get((user_id, "global", 0))


async def get_all_policies(pool: asyncpg.Pool) -> list[TriggerPolicy]:
    """列出所有策略。"""
    cache = await _ensure_cache(pool)
    return list(cache.values())


def get_refresh_suppressed_users(
    cache: dict[tuple[int, str, int], TriggerPolicy],
    scope_type: str,
    scope_id: int,
) -> frozenset[int]:
    """从已加载的缓存中提取 suppress_refresh 的 user_id 集合。

    同时考虑 scope-specific 和 global 策略。
    """
    suppressed: set[int] = set()
    for key, policy in cache.items():
        if not policy.suppress_refresh:
            continue
        uid, st, sid = key
        if is_master(uid):
            continue
        if (st == scope_type and sid == scope_id) or (st == "global" and sid == 0):
            suppressed.add(uid)
    return frozenset(suppressed)


# ── 写操作 ────────────────────────────────────────────────


async def set_policy(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str = "global",
    scope_id: int = 0,
    *,
    suppress_llm_trigger: bool = True,
    rate_multiplier: float = 0.0,
    suppress_refresh: bool = True,
    note: str | None = None,
) -> None:
    """设置用户触发策略（UPSERT）。"""
    await pool.execute(
        "INSERT INTO user_trigger_policy "
        "(user_id, scope_type, scope_id, suppress_llm_trigger, rate_multiplier, "
        "suppress_refresh, note, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, now()) "
        "ON CONFLICT (user_id, scope_type, scope_id) DO UPDATE SET "
        "suppress_llm_trigger = $4, rate_multiplier = $5, "
        "suppress_refresh = $6, note = $7, updated_at = now()",
        user_id, scope_type, scope_id,
        suppress_llm_trigger, rate_multiplier, suppress_refresh, note,
    )
    invalidate()


async def update_field(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
    field: str,
    value: bool | float | str | None,
) -> bool:
    """更新策略的单个字段。返回是否找到并更新了行。"""
    allowed = {"suppress_llm_trigger", "rate_multiplier", "suppress_refresh", "note"}
    if field not in allowed:
        raise ValueError(f"Unknown field: {field}")
    result = await pool.execute(
        f"UPDATE user_trigger_policy SET {field} = $1, updated_at = now() "  # noqa: S608
        "WHERE user_id = $2 AND scope_type = $3 AND scope_id = $4",
        value, user_id, scope_type, scope_id,
    )
    invalidate()
    return result == "UPDATE 1"


async def remove_policy(
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str = "global",
    scope_id: int = 0,
) -> bool:
    """移除用户触发策略。返回是否存在并已删除。"""
    result = await pool.execute(
        "DELETE FROM user_trigger_policy "
        "WHERE user_id = $1 AND scope_type = $2 AND scope_id = $3",
        user_id, scope_type, scope_id,
    )
    invalidate()
    return result == "DELETE 1"
