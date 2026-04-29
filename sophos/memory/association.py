"""联想 — 自动检索相关记忆。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sophos.llm.embedding import EmbeddingProvider
    from sophos.memory.store import MemoryStore

logger = logging.getLogger(__name__)


async def auto_retrieve(
    store: MemoryStore,
    embedding_provider: EmbeddingProvider,
    recent_messages: list[dict[str, Any]],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """根据最近消息自动检索相关记忆。"""
    # 拼接最近消息文本
    recent_text = "\n".join(msg.get("plain_text", "") for msg in recent_messages[-10:] if msg.get("plain_text"))
    if not recent_text.strip():
        return []

    # 嵌入拼接文本作为查询向量
    try:
        query_vec = await embedding_provider.embed_single(recent_text[:2000])
    except Exception:
        logger.warning("Failed to embed for association retrieval", exc_info=True)
        return []

    return await store.search_hybrid(recent_text, query_vec, limit=limit)


def format_association_block(memories: list[dict[str, Any]]) -> str:
    """格式化联想记忆为 prompt 块。"""
    if not memories:
        return ""
    lines: list[str] = []
    for m in memories:
        ts = m["created_at"].strftime("%Y-%m-%d") if m.get("created_at") else "?"
        lines.append(f"- [{ts}] #{m['id']}: {m['content']}")
    return "[联想]\n" + "\n".join(lines)
