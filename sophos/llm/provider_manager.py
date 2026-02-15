"""LLM Provider 管理器。

管理多个 OpenAI 兼容 provider 的生命周期：
- 从 DB 加载 / 从 .env seed 初始化
- 运行时热切换 provider + model
- 模型列表拉取与缓存
"""

import json
import logging
from typing import Any

import aiohttp
import asyncpg

from sophos.config import settings
from sophos.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


class ProviderManager:
    """管理当前活跃的 LLM provider 实例，支持热切换。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._provider: OpenAICompatProvider | None = None
        self._current_alias: str = ""
        self._current_model: str = ""

    # ── 启动 / 关闭 ──────────────────────────────────────────

    async def init(self) -> None:
        """启动时加载活跃配置。DB 有记录则用，否则从 .env seed。"""
        row = await self._pool.fetchrow(
            """
            SELECT p.id, p.alias, p.base_url, p.api_key,
                   p.extra_body, p.stream, p.request_timeout,
                   a.model
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'default'
            """,
        )
        if row:
            self._apply_row(row)
            logger.info(
                "Loaded LLM provider from DB: %s / %s",
                self._current_alias, self._current_model,
            )
            return

        # DB 无记录 → 尝试从 .env seed
        if not settings.llm_base_url:
            logger.warning("No LLM provider configured (DB empty, LLM_BASE_URL not set)")
            return

        await self._seed_from_env()

    async def close(self) -> None:
        """关闭当前 provider 的 HTTP session。"""
        if self._provider is not None:
            await self._provider.close()
            self._provider = None

    # ── 访问 ─────────────────────────────────────────────────

    def get_provider(self) -> OpenAICompatProvider:
        """获取当前活跃 provider。未配置时抛异常。"""
        if self._provider is None:
            raise RuntimeError("No LLM provider configured")
        return self._provider

    def current_info(self) -> dict[str, str]:
        """返回当前活跃配置摘要。"""
        return {
            "alias": self._current_alias,
            "model": self._current_model,
        }

    # ── Provider CRUD ────────────────────────────────────────

    async def add_provider(
        self,
        alias: str,
        base_url: str,
        api_key: str,
        *,
        stream: bool = True,
        extra_body: dict[str, Any] | None = None,
        request_timeout: int = 60,
    ) -> str:
        """新增 provider 到 DB。返回确认信息。"""
        extra_json = json.dumps(extra_body, ensure_ascii=False) if extra_body else None
        try:
            await self._pool.execute(
                """
                INSERT INTO llm_providers (alias, base_url, api_key, stream, extra_body, request_timeout)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                alias, base_url, api_key, stream, extra_json, request_timeout,
            )
        except asyncpg.UniqueViolationError:
            return f"provider '{alias}' 已存在"
        return f"已添加 provider '{alias}'"

    async def remove_provider(self, alias: str) -> str:
        """删除 provider。如果是当前活跃的则拒绝。"""
        if alias == self._current_alias:
            return f"无法删除当前活跃的 provider '{alias}'，请先切换到其他 provider"
        result = await self._pool.execute(
            "DELETE FROM llm_providers WHERE alias = $1", alias,
        )
        if result == "DELETE 0":
            return f"provider '{alias}' 不存在"
        return f"已删除 provider '{alias}'"

    async def list_providers(self) -> list[dict[str, Any]]:
        """列出所有 provider。"""
        rows = await self._pool.fetch(
            """
            SELECT p.alias, p.base_url, p.models,
                   (p.id = a.provider_id) AS is_active,
                   a.model AS active_model
            FROM llm_providers p
            LEFT JOIN llm_active a ON a.key = 'default' AND a.provider_id = p.id
            ORDER BY p.created_at
            """,
        )
        result = []
        for r in rows:
            models_raw = r["models"]
            if isinstance(models_raw, str):
                models_raw = json.loads(models_raw)
            result.append({
                "alias": r["alias"],
                "base_url": r["base_url"],
                "model_count": len(models_raw) if models_raw else 0,
                "is_active": bool(r["is_active"]),
                "active_model": r["active_model"] or "",
            })
        return result

    # ── 模型列表 ─────────────────────────────────────────────

    async def fetch_models(self, alias: str) -> list[str] | str:
        """从 provider 的 /v1/models 拉取模型列表，更新 DB 缓存。

        返回模型名列表，或错误信息字符串。
        """
        row = await self._pool.fetchrow(
            "SELECT base_url, api_key FROM llm_providers WHERE alias = $1", alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"

        url = row["base_url"].rstrip("/") + "/models"
        headers = {
            "Authorization": f"Bearer {row['api_key']}",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return f"拉取失败 ({resp.status}): {body[:200]}"
                    data = await resp.json()
        except Exception as e:
            return f"拉取失败: {e}"

        model_ids = sorted(item["id"] for item in data.get("data", []))
        models_json = json.dumps(model_ids, ensure_ascii=False)
        await self._pool.execute(
            "UPDATE llm_providers SET models = $1::jsonb WHERE alias = $2",
            models_json, alias,
        )
        return model_ids

    async def set_models(self, alias: str, models: list[str]) -> str:
        """手动设置 provider 的模型列表。"""
        models_json = json.dumps(models, ensure_ascii=False)
        result = await self._pool.execute(
            "UPDATE llm_providers SET models = $1::jsonb WHERE alias = $2",
            models_json, alias,
        )
        if result == "UPDATE 0":
            return f"provider '{alias}' 不存在"
        return f"已更新 '{alias}' 的模型列表（{len(models)} 个）"

    # ── 热切换 ───────────────────────────────────────────────

    async def switch(self, alias: str, model: str) -> str:
        """热切换到指定 provider + model。返回确认信息。"""
        row = await self._pool.fetchrow(
            """
            SELECT id, alias, base_url, api_key,
                   extra_body, stream, request_timeout
            FROM llm_providers WHERE alias = $1
            """,
            alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"

        # 关闭旧 provider
        if self._provider is not None:
            await self._provider.close()

        # 创建新实例
        self._apply_row({**dict(row), "model": model})

        # 更新 DB
        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, updated_at)
            VALUES ('default', $1, $2, now())
            ON CONFLICT (key) DO UPDATE
            SET provider_id = EXCLUDED.provider_id,
                model = EXCLUDED.model,
                updated_at = EXCLUDED.updated_at
            """,
            row["id"], model,
        )

        logger.info("Switched LLM to %s / %s", alias, model)
        return f"已切换到 {alias} / {model}"

    # ── 内部方法 ─────────────────────────────────────────────

    def _apply_row(self, row: dict[str, Any] | asyncpg.Record) -> None:
        """从 DB 行创建 OpenAICompatProvider 实例。"""
        extra_body = row.get("extra_body")
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body)

        self._provider = OpenAICompatProvider(
            base_url=row["base_url"],
            api_key=row["api_key"],
            model=row["model"],
            default_temperature=settings.llm_temperature,
            default_max_tokens=settings.llm_max_tokens,
            request_timeout=row.get("request_timeout", 60) or 60,
            stream=row.get("stream", True) if row.get("stream") is not None else True,
            extra_body=extra_body,
        )
        self._current_alias = row["alias"]
        self._current_model = row["model"]

    async def _seed_from_env(self) -> None:
        """从 .env 配置 seed 一个 'default' provider 到 DB。"""
        extra_body = None
        if settings.llm_extra_body:
            try:
                extra_body = json.loads(settings.llm_extra_body)
            except json.JSONDecodeError:
                logger.warning("Invalid LLM_EXTRA_BODY JSON: %s", settings.llm_extra_body)

        extra_json = json.dumps(extra_body, ensure_ascii=False) if extra_body else None

        # INSERT provider
        provider_id = await self._pool.fetchval(
            """
            INSERT INTO llm_providers (alias, base_url, api_key, stream, extra_body, request_timeout)
            VALUES ('default', $1, $2, $3, $4::jsonb, $5)
            RETURNING id
            """,
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_stream,
            extra_json,
            settings.llm_request_timeout,
        )

        # INSERT active
        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model)
            VALUES ('default', $1, $2)
            """,
            provider_id, settings.llm_model,
        )

        # 创建实例
        self._provider = OpenAICompatProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            default_temperature=settings.llm_temperature,
            default_max_tokens=settings.llm_max_tokens,
            request_timeout=settings.llm_request_timeout,
            stream=settings.llm_stream,
            extra_body=extra_body,
        )
        self._current_alias = "default"
        self._current_model = settings.llm_model

        logger.info(
            "Seeded default LLM provider from .env: %s / %s",
            settings.llm_base_url, settings.llm_model,
        )
