"""运行时配置管理。

DB-backed KV 配置，支持热修改。启动时从 DB 加载到内存缓存，
写入时 UPSERT DB + 刷新缓存。.env 值作为默认回退。
"""

import json
import logging
from typing import Any

import asyncpg

from sophos.config import settings

logger = logging.getLogger(__name__)

# 可调配置项及其 .env 默认值映射
_DEFAULTS: dict[str, Any] = {
    "llm_temperature": settings.llm_temperature,
    "llm_max_tokens": settings.llm_max_tokens,
    "llm_max_tool_rounds": settings.llm_max_tool_rounds,
    "llm_flatten_context": settings.llm_flatten_context,
    "llm_user_schema": settings.llm_user_schema,
    "llm_bot_schema": settings.llm_bot_schema,
    "max_context_messages": settings.max_context_messages,
    "include_co_account_in_context": settings.include_co_account_in_context,
    "cross_context_mode": settings.cross_context_mode,
    "recent_global_limit": 50,
    "recent_global_min_self": 5,
    "vision_system_prompt": settings.vision_system_prompt,
    "vision_refine_prompt": settings.vision_refine_prompt,
    "forward_head_count": 3,
    "forward_tail_count": 2,
    "forward_max_depth": 2,
    "reply_max_length": 100,
    # 触发器引擎
    "trigger_delay": 2.0,
    "trigger_qps": 0.5,
    "trigger_wait_timeout": 30.0,
    "trigger_eval_context_limit": 20,
    "trigger_eval_max_tokens": 64,
    "trigger_eval_temperature": 0.0,
    "trigger_eval_persona": "",
    "trigger_bucket_capacity": 10,
    "trigger_bucket_refill": 6.0,
    # Tavily 联网搜索
    "tavily_api_key": "",
    "tavily_max_results": 5,
    "tavily_include_answer": True,
    # 数据清理
    "cleanup_messages_max_bytes": 2 * 1024 * 1024 * 1024,  # 2GB
    "cleanup_memories_max_bytes": 2 * 1024 * 1024 * 1024,  # 2GB
    "cleanup_interval_seconds": 3600,  # 1 小时
}

_cache: dict[str, Any] = {}
_pool: asyncpg.Pool | None = None


async def init(pool: asyncpg.Pool) -> None:
    """启动时从 DB 加载全部配置到缓存。"""
    global _pool
    _pool = pool
    rows = await pool.fetch("SELECT key, value FROM bot_config")
    for row in rows:
        _cache[row["key"]] = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
    logger.info("RuntimeConfig loaded %d keys from DB", len(_cache))


def get(key: str, default: Any = None) -> Any:
    """读取配置值。优先缓存 → _DEFAULTS → default。"""
    if key in _cache:
        return _cache[key]
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    return default


async def set_value(key: str, value: Any) -> None:
    """写入配置值。UPSERT DB + 刷新缓存。"""
    if _pool is None:
        raise RuntimeError("RuntimeConfig not initialized")
    value_json = json.dumps(value, ensure_ascii=False)
    await _pool.execute(
        """
        INSERT INTO bot_config (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        key,
        value_json,
    )
    _cache[key] = value
    logger.info("RuntimeConfig set %s = %r", key, value)


async def delete(key: str) -> bool:
    """删除配置值，恢复为 .env 默认。返回是否存在。"""
    if _pool is None:
        raise RuntimeError("RuntimeConfig not initialized")
    result = await _pool.execute("DELETE FROM bot_config WHERE key = $1", key)
    existed = result == "DELETE 1"
    _cache.pop(key, None)
    if existed:
        logger.info("RuntimeConfig reset %s to default", key)
    return existed


def get_all() -> dict[str, Any]:
    """返回所有可调配置的当前值（合并 defaults + cache）。"""
    merged = dict(_DEFAULTS)
    merged.update(_cache)
    return merged


def get_defaults() -> dict[str, Any]:
    """返回所有默认值。"""
    return dict(_DEFAULTS)
