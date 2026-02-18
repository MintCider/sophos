"""MemoryStore — 记忆 CRUD + 混合搜索。"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from sophos.llm.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)


class MemoryStore:
    """统一管理三层记忆的存储操作。"""

    def __init__(self, pool: asyncpg.Pool, embedding: EmbeddingProvider | None) -> None:
        self._pool = pool
        self._embed = embedding

    def update_embedding_provider(self, provider: EmbeddingProvider | None) -> None:
        """热替换 embedding provider（迁移完成后调用）。"""
        self._embed = provider

    # ── 档案：会话 ────────────────────────────────────────────

    async def set_profile_context(
        self, scope_type: str, scope_id: int, content: str,
    ) -> dict[str, Any]:
        """整体替换会话档案。"""
        await self._pool.execute(
            """
            INSERT INTO memory_profile_context (scope_type, scope_id, content, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (scope_type, scope_id)
            DO UPDATE SET content = EXCLUDED.content, updated_at = now()
            """,
            scope_type, scope_id, content,
        )
        return {"status": "ok", "scope_type": scope_type, "scope_id": scope_id}

    async def get_profile_context(
        self, scope_type: str, scope_id: int,
    ) -> str | None:
        return await self._pool.fetchval(
            "SELECT content FROM memory_profile_context WHERE scope_type = $1 AND scope_id = $2",
            scope_type, scope_id,
        )

    # ── 档案：用户 ────────────────────────────────────────────

    async def set_profile_user(
        self, user_id: int, content: str, keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """整体替换用户档案。"""
        kw_json = json.dumps(keywords or [], ensure_ascii=False)
        await self._pool.execute(
            """
            INSERT INTO memory_profile_user (user_id, content, keywords, updated_at)
            VALUES ($1, $2, $3::jsonb, now())
            ON CONFLICT (user_id)
            DO UPDATE SET content = EXCLUDED.content, keywords = EXCLUDED.keywords, updated_at = now()
            """,
            user_id, content, kw_json,
        )
        return {"status": "ok", "user_id": user_id}

    async def get_profile_user(self, user_id: int) -> dict[str, Any] | None:
        row = await self._pool.fetchrow(
            "SELECT user_id, content, keywords FROM memory_profile_user WHERE user_id = $1",
            user_id,
        )
        if not row:
            return None
        kw = row["keywords"]
        if isinstance(kw, str):
            kw = json.loads(kw)
        return {"user_id": row["user_id"], "content": row["content"], "keywords": kw}

    async def get_profile_users_by_ids(self, user_ids: list[int]) -> list[dict[str, Any]]:
        """批量获取用户档案。"""
        if not user_ids:
            return []
        rows = await self._pool.fetch(
            "SELECT user_id, content, keywords FROM memory_profile_user WHERE user_id = ANY($1)",
            user_ids,
        )
        result = []
        for r in rows:
            kw = r["keywords"]
            if isinstance(kw, str):
                kw = json.loads(kw)
            result.append({"user_id": r["user_id"], "content": r["content"], "keywords": kw})
        return result

    async def get_profile_users_by_keyword(self, text: str) -> list[dict[str, Any]]:
        """查找 keywords 出现在 text 中的用户档案。"""
        if not text:
            return []
        rows = await self._pool.fetch(
            """
            SELECT user_id, content, keywords FROM memory_profile_user
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(keywords) kw
                WHERE $1 ILIKE '%%' || kw || '%%'
            )
            """,
            text,
        )
        result = []
        for r in rows:
            kw = r["keywords"]
            if isinstance(kw, str):
                kw = json.loads(kw)
            result.append({"user_id": r["user_id"], "content": r["content"], "keywords": kw})
        return result

    # ── 记忆 CRUD ─────────────────────────────────────────────

    async def write_memory(
        self, content: str, source_scope: str | None = None, source_id: int | None = None,
    ) -> dict[str, Any]:
        """写入记忆（自动嵌入 + 去重合并）。"""
        if self._embed is None:
            return {"error": "embedding provider 未配置"}

        vec = await self._embed.embed_single(content)
        vec_str = "[" + ",".join(str(v) for v in vec) + "]"

        # 去重：cosine similarity > 0.9
        existing = await self._pool.fetchrow(
            """
            SELECT id, content FROM memories
            WHERE embedding IS NOT NULL
              AND 1 - (embedding <=> $1::vector) > 0.9
            ORDER BY embedding <=> $1::vector
            LIMIT 1
            """,
            vec_str,
        )
        if existing:
            await self._pool.execute(
                "UPDATE memories SET content = $2, embedding = $3::vector, last_hit = now() WHERE id = $1",
                existing["id"], content, vec_str,
            )
            logger.debug("Memory merged into id=%d", existing["id"])
            return {"status": "merged", "id": existing["id"]}

        row_id = await self._pool.fetchval(
            """
            INSERT INTO memories (content, embedding, source_scope, source_id)
            VALUES ($1, $2::vector, $3, $4)
            RETURNING id
            """,
            content, vec_str, source_scope, source_id,
        )
        logger.debug("Memory created id=%d", row_id)
        return {"status": "created", "id": row_id}

    async def delete_memory(self, memory_id: int) -> dict[str, Any]:
        """按 ID 删除记忆。"""
        result = await self._pool.execute("DELETE FROM memories WHERE id = $1", memory_id)
        if result == "DELETE 0":
            return {"error": f"记忆 #{memory_id} 不存在"}
        return {"status": "deleted", "id": memory_id}

    async def search_hybrid(
        self, query_text: str, query_vec: list[float] | None = None, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """混合搜索：关键词 + 向量，RRF 融合。"""
        fetch_n = limit * 2

        # 关键词路
        kw_rows = await self._pool.fetch(
            """
            SELECT id, content, created_at,
                   ts_rank(tsv, plainto_tsquery('simple', $1)) AS score
            FROM memories
            WHERE tsv @@ plainto_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $2
            """,
            query_text, fetch_n,
        )

        # 向量路
        vec_rows: list[asyncpg.Record] = []
        if query_vec is not None:
            vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"
            vec_rows = await self._pool.fetch(
                """
                SELECT id, content, created_at,
                       1 - (embedding <=> $1::vector) AS score
                FROM memories
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $2
                """,
                vec_str, fetch_n,
            )

        # RRF 融合
        fused = self._rrf_merge(kw_rows, vec_rows, limit)

        # 更新命中统计
        if fused:
            ids = [m["id"] for m in fused]
            await self._pool.execute(
                "UPDATE memories SET last_hit = now(), hit_count = hit_count + 1 WHERE id = ANY($1)",
                ids,
            )

        return fused

    @staticmethod
    def _rrf_merge(
        kw_rows: list[asyncpg.Record],
        vec_rows: list[asyncpg.Record],
        limit: int,
        k: int = 60,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion。"""
        scores: dict[int, float] = {}
        data: dict[int, dict[str, Any]] = {}

        for rank, row in enumerate(kw_rows):
            rid = row["id"]
            scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
            if rid not in data:
                data[rid] = {"id": rid, "content": row["content"], "created_at": row["created_at"]}

        for rank, row in enumerate(vec_rows):
            rid = row["id"]
            scores[rid] = scores.get(rid, 0) + 1 / (k + rank + 1)
            if rid not in data:
                data[rid] = {"id": rid, "content": row["content"], "created_at": row["created_at"]}

        sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]
        return [data[rid] for rid in sorted_ids]

    # ── 统计 ──────────────────────────────────────────────────

    async def get_memory_stats(self) -> dict[str, int]:
        """各表行数统计。"""
        ctx_count = await self._pool.fetchval("SELECT count(*) FROM memory_profile_context") or 0
        user_count = await self._pool.fetchval("SELECT count(*) FROM memory_profile_user") or 0
        mem_count = await self._pool.fetchval("SELECT count(*) FROM memories") or 0
        return {"profile_context": ctx_count, "profile_user": user_count, "memories": mem_count}
