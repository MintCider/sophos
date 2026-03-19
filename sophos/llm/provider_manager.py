"""LLM Provider 管理器。

管理多个 OpenAI 兼容 provider 的生命周期：
- 从 DB 加载 / 从 .env seed 初始化
- 运行时热切换 provider + model
- 模型列表拉取与缓存
"""

import json
import logging
import re
from typing import Any

import aiohttp
import asyncpg

from sophos.config import settings
from sophos.llm.anthropic import AnthropicProvider
from sophos.llm.embedding import EmbeddingProvider
from sophos.llm.gemini import GeminiProvider
from sophos.llm.openai_compat import OpenAICompatProvider
from sophos.llm.provider import LLMProvider
from sophos import runtime_config

logger = logging.getLogger(__name__)


_VALID_API_TYPES = {"openai", "gemini", "anthropic"}


def _normalize_base_urls(raw: Any) -> dict[str, str]:
    """清理 base_urls：JSON 字符串自动解析，去空值/首尾空白，只保留合法 API 类型。"""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if key not in _VALID_API_TYPES or not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            normalized[key] = stripped
    return normalized


def resolve_base_url(base_urls: Any, api_type: str) -> str:
    """从 base_urls 字典解析出指定 api_type 的 base URL。

    优先级：
    1. 有显式 per-type URL → 直接用
    2. 从 openai URL 推导：去掉尾部 /v\\d+(beta\\d*)?
    """
    base_urls = _normalize_base_urls(base_urls)
    explicit_url = base_urls.get(api_type, "").rstrip("/")
    if explicit_url:
        return explicit_url
    openai_url = base_urls.get("openai", "").rstrip("/")
    if api_type == "openai" or not openai_url:
        return openai_url
    return re.sub(r'/v\d+(?:beta\d*)?$', '', openai_url)


class ProviderManager:
    """管理当前活跃的 LLM provider 实例，支持热切换。"""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._provider: LLMProvider | None = None
        self._current_alias: str = ""
        self._current_model: str = ""
        self._current_api_type: str = "openai"
        # Vision slot
        self._vision_provider: LLMProvider | None = None
        self._vision_alias: str = ""
        self._vision_model: str = ""
        self._vision_api_type: str = "openai"
        # Trigger slot (lightweight LLM for trigger evaluation)
        self._trigger_provider: LLMProvider | None = None
        self._trigger_alias: str = ""
        self._trigger_model: str = ""
        self._trigger_api_type: str = "openai"
        # Embedding slot
        self._embedding_provider: EmbeddingProvider | None = None
        self._embedding_alias: str = ""
        self._embedding_model: str = ""

    # ── 启动 / 关闭 ──────────────────────────────────────────

    async def init(self) -> None:
        """启动时加载活跃配置。DB 有记录则用，否则从 .env seed。"""
        row = await self._pool.fetchrow(
            """
            SELECT p.id, p.alias, p.base_urls, p.api_key,
                   a.extra_body, p.stream, a.request_timeout,
                   a.model, a.api_type
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'default'
            """,
        )
        if row:
            self._apply_row(row, api_type=row.get("api_type", "openai") or "openai")
            logger.info(
                "Loaded LLM provider from DB: %s / %s",
                self._current_alias, self._current_model,
            )
            await self._init_vision()
            await self._init_trigger()
            await self._init_embedding()
            return

        # DB 无记录 → 尝试从 .env seed
        if not settings.llm_base_url:
            logger.warning("No LLM provider configured (DB empty, LLM_BASE_URL not set)")
            await self._init_vision()
            await self._init_trigger()
            await self._init_embedding()
            return

        await self._seed_from_env()
        await self._init_vision()
        await self._init_trigger()
        await self._init_embedding()

    async def _init_vision(self) -> None:
        """启动时加载 vision slot。DB 有记录则用，否则从 .env seed。"""
        row = await self._pool.fetchrow(
            """
            SELECT p.id, p.alias, p.base_urls, p.api_key,
                   a.extra_body, p.stream, a.request_timeout,
                   a.model, a.api_type
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'vision'
            """,
        )
        if row:
            self._apply_vision_row(row, api_type=row.get("api_type", "openai") or "openai")
            logger.info(
                "Loaded vision provider from DB: %s / %s",
                self._vision_alias, self._vision_model,
            )
            return

        if not settings.vision_base_url:
            logger.info("No vision provider configured (DB empty, VISION_BASE_URL not set)")
            return

        await self._seed_vision_from_env()

    async def _init_trigger(self) -> None:
        """启动时加载 trigger slot。DB 有记录则用，否则从 .env seed。"""
        row = await self._pool.fetchrow(
            """
            SELECT p.id, p.alias, p.base_urls, p.api_key,
                   a.extra_body, p.stream, a.request_timeout,
                   a.model, a.api_type
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'trigger'
            """,
        )
        if row:
            self._apply_trigger_row(row, api_type=row.get("api_type", "openai") or "openai")
            logger.info(
                "Loaded trigger provider from DB: %s / %s",
                self._trigger_alias, self._trigger_model,
            )
            return

        if not settings.trigger_base_url:
            logger.info("No trigger provider configured (DB empty, TRIGGER_BASE_URL not set)")
            return

        await self._seed_trigger_from_env()

    async def _init_embedding(self) -> None:
        """启动时加载 embedding slot。DB 有记录则用，否则从 .env seed。"""
        row = await self._pool.fetchrow(
            """
            SELECT p.id, p.alias, p.base_urls, p.api_key,
                   a.request_timeout, a.model, a.extra_body
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'embedding'
            """,
        )
        if row:
            cfg = await self._pool.fetchrow(
                "SELECT endpoint FROM embedding_config WHERE id = 1",
            )
            endpoint = (cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings")
            extra_body = row.get("extra_body")
            if isinstance(extra_body, str):
                extra_body = json.loads(extra_body)
            emb_urls = _normalize_base_urls(row.get("base_urls"))
            self._embedding_provider = EmbeddingProvider(
                base_url=emb_urls.get("openai", ""),
                api_key=row["api_key"],
                model=row["model"],
                endpoint=endpoint,
                extra_body=extra_body,
                request_timeout=row.get("request_timeout", 30) or 30,
            )
            self._embedding_alias = row["alias"]
            self._embedding_model = row["model"]
            logger.info(
                "Loaded embedding provider from DB: %s / %s (endpoint=%s)",
                self._embedding_alias, self._embedding_model, endpoint,
            )
            return

        if not settings.embedding_base_url:
            logger.info("No embedding provider configured (DB empty, EMBEDDING_BASE_URL not set)")
            return

        await self._seed_embedding_from_env()

    async def _seed_embedding_from_env(self) -> None:
        """从 .env EMBEDDING_* 配置 seed 一个 'embedding' slot 到 DB。"""
        extra_body = None
        if settings.embedding_extra_body:
            try:
                extra_body = json.loads(settings.embedding_extra_body)
            except json.JSONDecodeError:
                logger.warning("Invalid EMBEDDING_EXTRA_BODY JSON: %s", settings.embedding_extra_body)
        extra_json = json.dumps(extra_body, ensure_ascii=False) if extra_body else None

        # 查找或创建 provider
        existing = await self._pool.fetchval(
            "SELECT id FROM llm_providers WHERE base_urls->>'openai' = $1 AND api_key = $2",
            settings.embedding_base_url, settings.embedding_api_key,
        )
        if existing:
            provider_id = existing
        else:
            emb_base_urls_json = json.dumps({"openai": settings.embedding_base_url}, ensure_ascii=False)
            provider_id = await self._pool.fetchval(
                """
                INSERT INTO llm_providers (alias, base_urls, api_key, stream)
                VALUES ($1, $2::jsonb, $3, false)
                ON CONFLICT (alias) DO UPDATE SET alias = EXCLUDED.alias
                RETURNING id
                """,
                f"embedding-{settings.embedding_model}",
                emb_base_urls_json,
                settings.embedding_api_key,
            )

        endpoint = settings.embedding_endpoint or "/embeddings"
        self._embedding_provider = EmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            endpoint=endpoint,
            extra_body=extra_body,
            request_timeout=settings.embedding_request_timeout,
        )
        self._embedding_alias = f"embedding-{settings.embedding_model}"
        self._embedding_model = settings.embedding_model

        # 检测维度并写入 DB
        try:
            dim = await self._embedding_provider.detect_dimension()
        except Exception:
            logger.exception("Failed to detect embedding dimension during seed")
            await self._embedding_provider.close()
            self._embedding_provider = None
            return

        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, extra_body, request_timeout)
            VALUES ('embedding', $1, $2, $3::jsonb, $4)
            ON CONFLICT (key) DO NOTHING
            """,
            provider_id, settings.embedding_model, extra_json,
            settings.embedding_request_timeout,
        )
        await self._pool.execute(
            "UPDATE embedding_config SET dimension = $1, endpoint = $2, updated_at = now() WHERE id = 1",
            dim, endpoint,
        )

        logger.info(
            "Seeded embedding provider from .env: %s / %s (dim=%d, endpoint=%s)",
            settings.embedding_base_url, settings.embedding_model, dim, endpoint,
        )

    async def close(self) -> None:
        """关闭所有 provider 的 HTTP session。"""
        if self._provider is not None:
            await self._provider.close()
            self._provider = None
        if self._vision_provider is not None:
            await self._vision_provider.close()
            self._vision_provider = None
        if self._trigger_provider is not None:
            await self._trigger_provider.close()
            self._trigger_provider = None
        if self._embedding_provider is not None:
            await self._embedding_provider.close()
            self._embedding_provider = None

    # ── 访问 ─────────────────────────────────────────────────

    def get_provider(self) -> LLMProvider:
        """获取当前活跃 provider。未配置时抛异常。"""
        if self._provider is None:
            raise RuntimeError("No LLM provider configured")
        return self._provider

    def current_info(self) -> dict[str, str]:
        """返回当前活跃配置摘要。"""
        info: dict[str, str] = {
            "alias": self._current_alias,
            "model": self._current_model,
            "api_type": self._current_api_type,
        }
        if self._vision_alias:
            info["vision_alias"] = self._vision_alias
            info["vision_model"] = self._vision_model
            info["vision_api_type"] = self._vision_api_type
        if self._trigger_alias:
            info["trigger_alias"] = self._trigger_alias
            info["trigger_model"] = self._trigger_model
            info["trigger_api_type"] = self._trigger_api_type
        if self._embedding_alias:
            info["embedding_alias"] = self._embedding_alias
            info["embedding_model"] = self._embedding_model
        return info

    def get_vision_provider(self) -> LLMProvider | None:
        """获取 vision provider。未配置时返回 None（优雅降级）。"""
        return self._vision_provider

    def get_trigger_provider(self) -> LLMProvider | None:
        """获取 trigger provider。未配置时返回 None。"""
        return self._trigger_provider

    def get_embedding_provider(self) -> EmbeddingProvider | None:
        """获取 embedding provider。未配置时返回 None。"""
        return self._embedding_provider

    # ── Provider CRUD ────────────────────────────────────────

    async def add_provider(
        self,
        alias: str,
        base_urls: dict[str, str],
        api_key: str,
        *,
        stream: bool = True,
    ) -> str:
        """新增 provider 到 DB。返回确认信息。"""
        base_urls = _normalize_base_urls(base_urls)
        if not base_urls:
            return "至少提供一个 Base URL"
        base_urls_json = json.dumps(base_urls, ensure_ascii=False)
        try:
            await self._pool.execute(
                """
                INSERT INTO llm_providers (alias, base_urls, api_key, stream)
                VALUES ($1, $2::jsonb, $3, $4)
                """,
                alias, base_urls_json, api_key, stream,
            )
        except asyncpg.UniqueViolationError:
            return f"provider '{alias}' 已存在"
        return f"已添加 provider '{alias}'"

    async def set_provider_url(self, alias: str, api_type: str, url: str | None) -> str:
        """设置或清除 provider 的 per-type base URL。url=None 表示清除。"""
        row = await self._pool.fetchrow(
            "SELECT base_urls FROM llm_providers WHERE alias = $1", alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"
        base_urls = _normalize_base_urls(row["base_urls"])
        if url is None:
            base_urls.pop(api_type, None)
        else:
            stripped = url.strip()
            if stripped:
                base_urls[api_type] = stripped
            else:
                base_urls.pop(api_type, None)
        if not base_urls:
            return f"provider '{alias}' 至少保留一个 Base URL"
        base_urls_json = json.dumps(base_urls, ensure_ascii=False)
        await self._pool.execute(
            "UPDATE llm_providers SET base_urls = $1::jsonb WHERE alias = $2",
            base_urls_json, alias,
        )
        if url is None:
            return f"已清除 '{alias}' 的 {api_type} URL（回退到自动推导）"
        return f"已设置 '{alias}' 的 {api_type} URL: {url}"

    async def get_provider_urls(self, alias: str) -> dict[str, str] | str:
        """获取 provider 的所有 base URLs。"""
        row = await self._pool.fetchrow(
            "SELECT base_urls FROM llm_providers WHERE alias = $1", alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"
        return _normalize_base_urls(row["base_urls"])

    async def update_provider(
        self,
        alias: str,
        *,
        base_urls: dict[str, str] | None = None,
    ) -> str:
        """更新 provider 配置。当前仅支持编辑 base_urls。"""
        row = await self._pool.fetchrow(
            "SELECT id FROM llm_providers WHERE alias = $1",
            alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"

        if base_urls is not None:
            normalized = _normalize_base_urls(base_urls)
            if not normalized:
                return "至少提供一个 Base URL"
            await self._pool.execute(
                "UPDATE llm_providers SET base_urls = $1::jsonb WHERE alias = $2",
                json.dumps(normalized, ensure_ascii=False),
                alias,
            )
        return f"已更新 provider '{alias}'"

    async def remove_provider(self, alias: str) -> str:
        """删除 provider。如果是当前活跃的则拒绝。"""
        active_rows = await self._pool.fetch(
            """
            SELECT a.key
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE p.alias = $1
            ORDER BY a.key
            """,
            alias,
        )
        if active_rows:
            slots = ", ".join(row["key"] for row in active_rows)
            return f"无法删除 provider '{alias}'，仍被以下 slot 使用: {slots}"
        result = await self._pool.execute(
            "DELETE FROM llm_providers WHERE alias = $1", alias,
        )
        if result == "DELETE 0":
            return f"provider '{alias}' 不存在"
        return f"已删除 provider '{alias}'"

    @staticmethod
    def _mask_api_key(api_key: str) -> str:
        if len(api_key) <= 8:
            return "*" * len(api_key)
        return f"{api_key[:4]}{'*' * max(len(api_key) - 8, 4)}{api_key[-4:]}"

    async def list_providers(self) -> list[dict[str, Any]]:
        """列出所有 provider（含 active slots、masked key）。"""
        rows = await self._pool.fetch(
            """
            SELECT p.alias, p.base_urls, p.api_key, p.models
            FROM llm_providers p
            ORDER BY p.created_at
            """,
        )
        active_rows = await self._pool.fetch(
            """
            SELECT a.key, p.alias, a.model, a.api_type
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            """,
        )
        active_by_alias: dict[str, list[dict[str, str]]] = {}
        for ar in active_rows:
            active_by_alias.setdefault(ar["alias"], []).append({
                "key": ar["key"],
                "model": ar["model"],
                "api_type": ar.get("api_type", "openai") or "openai",
            })

        result = []
        for r in rows:
            models_raw = r["models"]
            if isinstance(models_raw, str):
                models_raw = json.loads(models_raw)
            base_urls = _normalize_base_urls(r["base_urls"])
            result.append({
                "alias": r["alias"],
                "base_urls": base_urls,
                "api_key_masked": self._mask_api_key(r["api_key"]),
                "models": models_raw or [],
                "model_count": len(models_raw) if models_raw else 0,
                "active_slots": active_by_alias.get(r["alias"], []),
            })
        return result

    async def get_provider_api_key(self, alias: str) -> str | None:
        """获取 provider 的原始 API Key，不存在则返回 None。"""
        return await self._pool.fetchval(
            "SELECT api_key FROM llm_providers WHERE alias = $1", alias,
        )

    # ── 模型列表 ─────────────────────────────────────────────

    async def fetch_models(self, alias: str) -> list[str] | str:
        """从 provider 的 /v1/models 拉取模型列表，更新 DB 缓存。

        返回模型名列表，或错误信息字符串。
        """
        row = await self._pool.fetchrow(
            "SELECT base_urls, api_key FROM llm_providers WHERE alias = $1", alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"

        fm_base_urls = _normalize_base_urls(row.get("base_urls"))
        openai_url = fm_base_urls.get("openai", "").rstrip("/")
        if not openai_url:
            return f"provider '{alias}' 未配置 OpenAI Base URL，无法拉取 /models"
        url = openai_url + "/models"
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

    async def set_provider_extra_body(self, slot: str, extra_body_json: str) -> str:
        """设置活跃 slot 的 extra_body 并热重载。slot: 'default' | 'vision'。"""
        try:
            parsed = json.loads(extra_body_json) if extra_body_json else None
        except json.JSONDecodeError:
            return f"JSON 格式错误: {extra_body_json}"
        extra_json = json.dumps(parsed, ensure_ascii=False) if parsed else None
        result = await self._pool.execute(
            "UPDATE llm_active SET extra_body = $1::jsonb WHERE key = $2",
            extra_json, slot,
        )
        if result == "UPDATE 0":
            return f"slot '{slot}' 不存在"
        # 热重载到内存中的 provider 实例
        if slot == "default" and self._provider is not None:
            self._provider._extra_body = parsed or {}
            logger.debug("Hot-reloaded extra_body for default slot")
        elif slot == "vision" and self._vision_provider is not None:
            self._vision_provider._extra_body = parsed or {}
            logger.debug("Hot-reloaded extra_body for vision slot")
        elif slot == "trigger" and self._trigger_provider is not None:
            self._trigger_provider._extra_body = parsed or {}
            logger.debug("Hot-reloaded extra_body for trigger slot")
        elif slot == "embedding" and self._embedding_provider is not None:
            self._embedding_provider._extra_body = parsed or {}
            logger.debug("Hot-reloaded extra_body for embedding slot")
        return "extra_body 已更新" + (f": {parsed}" if parsed else " (已清除)")

    async def set_timeout(self, slot: str, seconds: int) -> str:
        """设置活跃 slot 的 request_timeout 并热重载。"""
        if seconds < 5 or seconds > 600:
            return "timeout 范围: 5-600 秒"
        result = await self._pool.execute(
            "UPDATE llm_active SET request_timeout = $1 WHERE key = $2",
            seconds, slot,
        )
        if result == "UPDATE 0":
            return f"slot '{slot}' 不存在"
        # 热重载
        provider = {
            "default": self._provider,
            "vision": self._vision_provider,
            "trigger": self._trigger_provider,
            "embedding": self._embedding_provider,
        }.get(slot)
        if provider is not None:
            import aiohttp
            provider._timeout = aiohttp.ClientTimeout(total=seconds)
            logger.debug("Hot-reloaded timeout for %s slot: %ds", slot, seconds)
        return f"timeout 已更新: {seconds}s"

    async def get_active_extra_body(self, slot: str) -> str:
        """获取活跃 slot 的 extra_body。"""
        row = await self._pool.fetchrow(
            """
            SELECT a.extra_body, p.alias
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = $1
            """,
            slot,
        )
        if not row:
            return f"slot '{slot}' 未配置"
        eb = row["extra_body"]
        return f"{row['alias']} extra_body: {eb}" if eb else f"{row['alias']} extra_body: (无)"

    # ── 热切换 ───────────────────────────────────────────────

    async def _fetch_and_validate(
        self, alias: str, api_type: str, slot: str,
    ) -> tuple[asyncpg.Record, None] | tuple[None, str]:
        """拉取 provider 行并校验 base_url，返回 (row, None) 或 (None, err)。"""
        row = await self._pool.fetchrow(
            """
            SELECT id, alias, base_urls, api_key, stream
            FROM llm_providers WHERE alias = $1
            """,
            alias,
        )
        if not row:
            return None, f"provider '{alias}' 不存在"
        if not resolve_base_url(row.get("base_urls"), api_type):
            hint = f"，或使用 --type gemini/anthropic 切换 {slot}" if api_type == "openai" else ""
            kind = "OpenAI " if api_type == "openai" else f"{api_type} "
            return None, f"provider '{alias}' 未配置可用的 {kind}Base URL{hint}"
        return row, None

    async def switch(self, alias: str, model: str, *, api_type: str = "openai") -> str:
        """热切换到指定 provider + model。返回确认信息。"""
        row, err = await self._fetch_and_validate(alias, api_type, "default")
        if err:
            return err

        # 读取当前 slot 的 timeout（切换时保留）
        existing_timeout = await self._pool.fetchval(
            "SELECT request_timeout FROM llm_active WHERE key = 'default'",
        ) or 60

        # 关闭旧 provider
        if self._provider is not None:
            await self._provider.close()

        # 创建新实例（extra_body 清空，timeout 保留）
        self._apply_row(
            {**dict(row), "model": model, "extra_body": None, "request_timeout": existing_timeout},
            api_type=api_type,
        )

        # 更新 DB（extra_body 重置为 NULL，request_timeout 保留）
        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout, updated_at)
            VALUES ('default', $1, $2, $3, NULL, $4, now())
            ON CONFLICT (key) DO UPDATE
            SET provider_id = EXCLUDED.provider_id,
                model = EXCLUDED.model,
                api_type = EXCLUDED.api_type,
                extra_body = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            row["id"], model, api_type, existing_timeout,
        )

        logger.info("Switched LLM to %s / %s (api_type=%s)", alias, model, api_type)
        return f"已切换到 {alias} / {model}" + (f" (api_type={api_type})" if api_type != "openai" else "")

    async def switch_vision(self, alias: str, model: str, *, api_type: str = "openai") -> str:
        """热切换 vision slot 到指定 provider + model。"""
        row, err = await self._fetch_and_validate(alias, api_type, "vision")
        if err:
            return err

        existing_timeout = await self._pool.fetchval(
            "SELECT request_timeout FROM llm_active WHERE key = 'vision'",
        ) or 30

        if self._vision_provider is not None:
            await self._vision_provider.close()

        self._apply_vision_row(
            {**dict(row), "model": model, "extra_body": None, "request_timeout": existing_timeout},
            api_type=api_type,
        )

        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout, updated_at)
            VALUES ('vision', $1, $2, $3, NULL, $4, now())
            ON CONFLICT (key) DO UPDATE
            SET provider_id = EXCLUDED.provider_id,
                model = EXCLUDED.model,
                api_type = EXCLUDED.api_type,
                extra_body = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            row["id"], model, api_type, existing_timeout,
        )

        logger.info("Switched vision to %s / %s (api_type=%s)", alias, model, api_type)
        return f"已切换 vision 到 {alias} / {model}" + (f" (api_type={api_type})" if api_type != "openai" else "")

    async def disable_vision(self) -> str:
        """关闭 vision provider。"""
        if self._vision_provider is not None:
            await self._vision_provider.close()
            self._vision_provider = None
        self._vision_alias = ""
        self._vision_model = ""
        await self._pool.execute("DELETE FROM llm_active WHERE key = 'vision'")
        logger.info("Vision provider disabled")
        return "已关闭 vision"

    async def switch_trigger(self, alias: str, model: str, *, api_type: str = "openai") -> str:
        """热切换 trigger slot 到指定 provider + model。"""
        row, err = await self._fetch_and_validate(alias, api_type, "trigger")
        if err:
            return err

        existing_timeout = await self._pool.fetchval(
            "SELECT request_timeout FROM llm_active WHERE key = 'trigger'",
        ) or 15

        if self._trigger_provider is not None:
            await self._trigger_provider.close()

        self._apply_trigger_row(
            {**dict(row), "model": model, "extra_body": None, "request_timeout": existing_timeout},
            api_type=api_type,
        )

        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout, updated_at)
            VALUES ('trigger', $1, $2, $3, NULL, $4, now())
            ON CONFLICT (key) DO UPDATE
            SET provider_id = EXCLUDED.provider_id,
                model = EXCLUDED.model,
                api_type = EXCLUDED.api_type,
                extra_body = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            row["id"], model, api_type, existing_timeout,
        )

        logger.info("Switched trigger to %s / %s (api_type=%s)", alias, model, api_type)
        return f"已切换 trigger 到 {alias} / {model}" + (f" (api_type={api_type})" if api_type != "openai" else "")

    async def disable_trigger(self) -> str:
        """关闭 trigger provider。"""
        if self._trigger_provider is not None:
            await self._trigger_provider.close()
            self._trigger_provider = None
        self._trigger_alias = ""
        self._trigger_model = ""
        await self._pool.execute("DELETE FROM llm_active WHERE key = 'trigger'")
        logger.info("Trigger provider disabled")
        return "已关闭 trigger"

    # ── Embedding 切换 + 迁移 ──────────────────────────────────

    async def switch_embedding(self, alias: str, model: str) -> str:
        """切换 embedding 模型。首次直接激活，后续暂存待迁移。"""
        row = await self._pool.fetchrow(
            "SELECT id, alias, base_urls, api_key FROM llm_providers WHERE alias = $1",
            alias,
        )
        if not row:
            return f"provider '{alias}' 不存在"

        # 从 embedding_config 读取 endpoint
        cfg = await self._pool.fetchrow("SELECT endpoint FROM embedding_config WHERE id = 1")
        endpoint = (cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings")
        # 从 llm_active 读取 extra_body 和 request_timeout
        active_row = await self._pool.fetchrow(
            "SELECT extra_body, request_timeout FROM llm_active WHERE key = 'embedding'",
        )
        extra_body = None
        timeout = 30
        if active_row:
            eb_raw = active_row.get("extra_body")
            extra_body = json.loads(eb_raw) if isinstance(eb_raw, str) else eb_raw
            timeout = active_row.get("request_timeout") or 30

        # 创建临时 provider 检测维度
        emb_base_url = _normalize_base_urls(row.get("base_urls")).get("openai", "")
        tmp = EmbeddingProvider(
            base_url=emb_base_url, api_key=row["api_key"], model=model,
            endpoint=endpoint, extra_body=extra_body,
            request_timeout=timeout,
        )
        try:
            dim = await tmp.detect_dimension()
        except Exception as e:
            await tmp.close()
            return f"维度检测失败: {e}"

        # 首次设置（DB 无 embedding 行）
        has_active = await self._pool.fetchval(
            "SELECT 1 FROM llm_active WHERE key = 'embedding'",
        )
        if not has_active:
            await self._pool.execute(
                """
                INSERT INTO llm_active (key, provider_id, model, updated_at)
                VALUES ('embedding', $1, $2, now())
                """,
                row["id"], model,
            )
            await self._pool.execute(
                "UPDATE embedding_config SET dimension = $1, updated_at = now() WHERE id = 1",
                dim,
            )
            # 创建向量索引
            try:
                await self._pool.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_embedding "
                    "ON memories USING hnsw (embedding vector_cosine_ops);"
                )
            except Exception:
                logger.warning("Could not create HNSW index (may need data first)")

            if self._embedding_provider is not None:
                await self._embedding_provider.close()
            self._embedding_provider = tmp
            self._embedding_alias = row["alias"]
            self._embedding_model = model
            logger.info("Activated embedding: %s / %s (dim=%d)", alias, model, dim)
            return f"已激活 embedding: {alias} / {model} (维度: {dim})"

        # 后续切换 → 暂存
        await tmp.close()
        await self._pool.execute(
            """
            UPDATE embedding_config
            SET pending_provider_id = $1, pending_model = $2, pending_dimension = $3,
                migration_status = 'pending', updated_at = now()
            WHERE id = 1
            """,
            row["id"], model, dim,
        )
        logger.info("Staged embedding switch: %s / %s (dim=%d)", alias, model, dim)
        return f"已暂存 {alias} / {model} (维度: {dim})，执行 .memory migrate 开始迁移"

    async def run_embedding_migration(self) -> str:
        """执行 embedding 模型迁移（staging column 方案）。"""
        cfg = await self._pool.fetchrow("SELECT * FROM embedding_config WHERE id = 1")
        if not cfg:
            return "embedding 未配置"

        status = cfg["migration_status"]
        if status == "none":
            return "无待执行的迁移"
        if status not in ("pending", "running"):
            return f"迁移状态异常: {status}，请先 rollback"

        # 获取 pending provider 信息
        prov_row = await self._pool.fetchrow(
            "SELECT base_urls, api_key FROM llm_providers WHERE id = $1",
            cfg["pending_provider_id"],
        )
        if not prov_row:
            return "pending provider 不存在"

        endpoint = cfg.get("endpoint") or "/embeddings"
        active_row = await self._pool.fetchrow(
            "SELECT extra_body, request_timeout FROM llm_active WHERE key = 'embedding'",
        )
        extra_body = None
        timeout = 30
        if active_row:
            eb_raw = active_row.get("extra_body")
            extra_body = json.loads(eb_raw) if isinstance(eb_raw, str) else eb_raw
            timeout = active_row.get("request_timeout") or 30

        new_provider = EmbeddingProvider(
            base_url=_normalize_base_urls(prov_row.get("base_urls")).get("openai", ""),
            api_key=prov_row["api_key"],
            model=cfg["pending_model"],
            endpoint=endpoint, extra_body=extra_body,
            request_timeout=timeout,
        )

        try:
            # 标记 running
            await self._pool.execute(
                "UPDATE embedding_config SET migration_status = 'running', updated_at = now() WHERE id = 1",
            )

            # 确保 staging 列存在
            try:
                await self._pool.execute(
                    "ALTER TABLE memories ADD COLUMN embedding_new vector;"
                )
            except asyncpg.DuplicateColumnError:
                pass  # 崩溃恢复：列已存在

            # 分批 re-embed
            batch_size = 50
            total = 0
            while True:
                rows = await self._pool.fetch(
                    "SELECT id, content FROM memories WHERE embedding_new IS NULL ORDER BY id LIMIT $1",
                    batch_size,
                )
                if not rows:
                    break
                texts = [r["content"] for r in rows]
                vectors = await new_provider.embed(texts)
                async with self._pool.acquire() as conn:
                    for row, vec in zip(rows, vectors):
                        await conn.execute(
                            "UPDATE memories SET embedding_new = $2::vector WHERE id = $1",
                            row["id"], str(vec),
                        )
                total += len(rows)
                logger.info("Migration progress: %d rows re-embedded", total)

            # 原子切换
            async with self._pool.acquire() as conn:
                await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding;")
                await conn.execute("ALTER TABLE memories DROP COLUMN embedding;")
                await conn.execute("ALTER TABLE memories RENAME COLUMN embedding_new TO embedding;")
                try:
                    await conn.execute(
                        "CREATE INDEX idx_memories_embedding "
                        "ON memories USING hnsw (embedding vector_cosine_ops);"
                    )
                except Exception:
                    logger.warning("Could not create HNSW index after migration")

                # 更新配置
                await conn.execute(
                    """
                    UPDATE embedding_config
                    SET dimension = pending_dimension,
                        pending_provider_id = NULL, pending_model = NULL,
                        pending_dimension = NULL, migration_status = 'none',
                        updated_at = now()
                    WHERE id = 1
                    """,
                )
                await conn.execute(
                    """
                    UPDATE llm_active SET provider_id = $1, model = $2, updated_at = now()
                    WHERE key = 'embedding'
                    """,
                    cfg["pending_provider_id"], cfg["pending_model"],
                )

            # 热替换
            if self._embedding_provider is not None:
                await self._embedding_provider.close()
            self._embedding_provider = new_provider
            self._embedding_model = cfg["pending_model"]
            # 查 alias
            alias_row = await self._pool.fetchrow(
                "SELECT alias FROM llm_providers WHERE id = $1", cfg["pending_provider_id"],
            )
            self._embedding_alias = alias_row["alias"] if alias_row else ""

            logger.info(
                "Embedding migration complete: %s (dim=%d, %d rows)",
                cfg["pending_model"], cfg["pending_dimension"], total,
            )
            return f"迁移完成: {cfg['pending_model']} (维度: {cfg['pending_dimension']}, {total} 条记忆)"

        except Exception:
            await new_provider.close()
            await self._pool.execute(
                "UPDATE embedding_config SET migration_status = 'failed', updated_at = now() WHERE id = 1",
            )
            logger.exception("Embedding migration failed")
            raise

    async def rollback_embedding_migration(self) -> str:
        """回滚 embedding 迁移：丢弃 staging 列，恢复原状。"""
        cfg = await self._pool.fetchrow("SELECT migration_status FROM embedding_config WHERE id = 1")
        if not cfg or cfg["migration_status"] == "none":
            return "无需回滚"

        try:
            await self._pool.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding_new;")
        except Exception:
            pass
        await self._pool.execute(
            """
            UPDATE embedding_config
            SET pending_provider_id = NULL, pending_model = NULL,
                pending_dimension = NULL, migration_status = 'none',
                updated_at = now()
            WHERE id = 1
            """,
        )
        logger.info("Embedding migration rolled back")
        return "迁移已回滚"

    async def set_embedding_endpoint(self, endpoint: str) -> str:
        """切换 embedding API endpoint 并重建 provider。"""
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        await self._pool.execute(
            "UPDATE embedding_config SET endpoint = $1, updated_at = now() WHERE id = 1",
            endpoint,
        )
        await self._rebuild_embedding_provider(endpoint=endpoint)
        return f"Endpoint 已切换为 {endpoint}"

    async def set_embedding_extra_body(self, extra_body_json: str) -> str:
        """设置 embedding extra_body（统一存 llm_active）。"""
        return await self.set_provider_extra_body("embedding", extra_body_json)

    async def _rebuild_embedding_provider(
        self, *, endpoint: str | None = None,
    ) -> None:
        """重建当前 embedding provider（endpoint 变更后调用）。"""
        if self._embedding_provider is None:
            return
        old = self._embedding_provider
        row = await self._pool.fetchrow(
            """
            SELECT p.base_urls, p.api_key, a.request_timeout, a.model, a.extra_body
            FROM llm_active a
            JOIN llm_providers p ON p.id = a.provider_id
            WHERE a.key = 'embedding'
            """,
        )
        if not row:
            return
        extra_body = row.get("extra_body")
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body)
        reb_base_urls = _normalize_base_urls(row.get("base_urls"))
        if endpoint is None:
            cfg = await self._pool.fetchrow(
                "SELECT endpoint FROM embedding_config WHERE id = 1",
            )
            endpoint = (cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings")
        self._embedding_provider = EmbeddingProvider(
            base_url=reb_base_urls.get("openai", ""), api_key=row["api_key"],
            model=row["model"], endpoint=endpoint, extra_body=extra_body,
            request_timeout=row.get("request_timeout", 30) or 30,
        )
        await old.close()
        logger.info("Rebuilt embedding provider (endpoint=%s)", endpoint)

    # ── 内部方法 ─────────────────────────────────────────────

    def _apply_row(self, row: dict[str, Any] | asyncpg.Record, api_type: str = "openai") -> None:
        """从 DB 行创建 LLM provider 实例，根据 api_type 选择实现。"""
        extra_body = row.get("extra_body")
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body)

        base_url = resolve_base_url(row.get("base_urls"), api_type)

        kwargs = {
            "base_url": base_url,
            "api_key": row["api_key"],
            "model": row["model"],
            "default_temperature": runtime_config.get("llm_temperature"),
            "default_max_tokens": runtime_config.get("llm_max_tokens"),
            "request_timeout": row.get("request_timeout", 60) or 60,
            "stream": row.get("stream", True) if row.get("stream") is not None else True,
            "extra_body": extra_body,
        }
        if api_type == "gemini":
            self._provider = GeminiProvider(**kwargs)
        elif api_type == "anthropic":
            self._provider = AnthropicProvider(**kwargs)
        else:
            self._provider = OpenAICompatProvider(**kwargs)
        self._current_alias = row["alias"]
        self._current_model = row["model"]
        self._current_api_type = api_type

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
        base_urls_json = json.dumps({"openai": settings.llm_base_url}, ensure_ascii=False)
        provider_id = await self._pool.fetchval(
            """
            INSERT INTO llm_providers (alias, base_urls, api_key, stream)
            VALUES ('default', $1::jsonb, $2, $3)
            RETURNING id
            """,
            base_urls_json,
            settings.llm_api_key,
            settings.llm_stream,
        )

        # INSERT active（extra_body + request_timeout 存这里）
        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout)
            VALUES ('default', $1, $2, $3, $4::jsonb, $5)
            """,
            provider_id, settings.llm_model, settings.llm_api_type, extra_json,
            settings.llm_request_timeout,
        )

        # 创建实例
        kwargs = {
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
            "default_temperature": runtime_config.get("llm_temperature"),
            "default_max_tokens": runtime_config.get("llm_max_tokens"),
            "request_timeout": settings.llm_request_timeout,
            "stream": settings.llm_stream,
            "extra_body": extra_body,
        }
        if settings.llm_api_type == "gemini":
            self._provider = GeminiProvider(**kwargs)
        elif settings.llm_api_type == "anthropic":
            self._provider = AnthropicProvider(**kwargs)
        else:
            self._provider = OpenAICompatProvider(**kwargs)
        self._current_alias = "default"
        self._current_model = settings.llm_model
        self._current_api_type = settings.llm_api_type

        logger.info(
            "Seeded default LLM provider from .env: %s / %s",
            settings.llm_base_url, settings.llm_model,
        )

    def _apply_vision_row(self, row: dict[str, Any] | asyncpg.Record, api_type: str = "openai") -> None:
        """从 DB 行创建 vision provider 实例。"""
        extra_body = row.get("extra_body")
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body)

        base_url = resolve_base_url(row.get("base_urls"), api_type)

        kwargs = {
            "base_url": base_url,
            "api_key": row["api_key"],
            "model": row["model"],
            "default_temperature": 0.3,
            "default_max_tokens": 512,
            "request_timeout": row.get("request_timeout", 30) or 30,
            "stream": row.get("stream", False) if row.get("stream") is not None else False,
            "extra_body": extra_body,
        }
        if api_type == "gemini":
            self._vision_provider = GeminiProvider(**kwargs)
        elif api_type == "anthropic":
            self._vision_provider = AnthropicProvider(**kwargs)
        else:
            self._vision_provider = OpenAICompatProvider(**kwargs)
        self._vision_alias = row["alias"]
        self._vision_model = row["model"]
        self._vision_api_type = api_type

    async def _seed_vision_from_env(self) -> None:
        """从 .env VISION_* 配置 seed 一个 'vision' slot 到 DB。"""
        extra_body = None
        if settings.vision_extra_body:
            try:
                extra_body = json.loads(settings.vision_extra_body)
            except json.JSONDecodeError:
                logger.warning("Invalid VISION_EXTRA_BODY JSON: %s", settings.vision_extra_body)

        extra_json = json.dumps(extra_body, ensure_ascii=False) if extra_body else None

        # 查找或创建 provider（vision 可能复用已有 provider）
        existing = await self._pool.fetchval(
            "SELECT id FROM llm_providers WHERE base_urls->>'openai' = $1 AND api_key = $2",
            settings.vision_base_url, settings.vision_api_key,
        )
        if existing:
            provider_id = existing
        else:
            vision_base_urls_json = json.dumps({"openai": settings.vision_base_url}, ensure_ascii=False)
            provider_id = await self._pool.fetchval(
                """
                INSERT INTO llm_providers (alias, base_urls, api_key, stream)
                VALUES ($1, $2::jsonb, $3, $4)
                ON CONFLICT (alias) DO UPDATE SET alias = EXCLUDED.alias
                RETURNING id
                """,
                f"vision-{settings.vision_model}",
                vision_base_urls_json,
                settings.vision_api_key,
                settings.vision_stream,
            )

        # extra_body + request_timeout 存到 llm_active
        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout)
            VALUES ('vision', $1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (key) DO NOTHING
            """,
            provider_id, settings.vision_model, settings.vision_api_type, extra_json,
            settings.vision_request_timeout,
        )

        kwargs = {
            "base_url": settings.vision_base_url,
            "api_key": settings.vision_api_key,
            "model": settings.vision_model,
            "default_temperature": 0.3,
            "default_max_tokens": 512,
            "request_timeout": settings.vision_request_timeout,
            "stream": settings.vision_stream,
            "extra_body": extra_body,
        }
        if settings.vision_api_type == "gemini":
            self._vision_provider = GeminiProvider(**kwargs)
        elif settings.vision_api_type == "anthropic":
            self._vision_provider = AnthropicProvider(**kwargs)
        else:
            self._vision_provider = OpenAICompatProvider(**kwargs)
        self._vision_alias = f"vision-{settings.vision_model}"
        self._vision_model = settings.vision_model
        self._vision_api_type = settings.vision_api_type

        logger.info(
            "Seeded vision provider from .env: %s / %s",
            settings.vision_base_url, settings.vision_model,
        )

    def _apply_trigger_row(self, row: dict[str, Any] | asyncpg.Record, api_type: str = "openai") -> None:
        """从 DB 行创建 trigger provider 实例。"""
        extra_body = row.get("extra_body")
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body)

        base_url = resolve_base_url(row.get("base_urls"), api_type)

        kwargs = {
            "base_url": base_url,
            "api_key": row["api_key"],
            "model": row["model"],
            "default_temperature": 0.0,
            "default_max_tokens": 64,
            "request_timeout": row.get("request_timeout", 15) or 15,
            "stream": row.get("stream", False) if row.get("stream") is not None else False,
            "extra_body": extra_body,
        }
        if api_type == "gemini":
            self._trigger_provider = GeminiProvider(**kwargs)
        elif api_type == "anthropic":
            self._trigger_provider = AnthropicProvider(**kwargs)
        else:
            self._trigger_provider = OpenAICompatProvider(**kwargs)
        self._trigger_alias = row["alias"]
        self._trigger_model = row["model"]
        self._trigger_api_type = api_type

    async def _seed_trigger_from_env(self) -> None:
        """从 .env TRIGGER_* 配置 seed 一个 'trigger' slot 到 DB。"""
        extra_body = None
        if settings.trigger_extra_body:
            try:
                extra_body = json.loads(settings.trigger_extra_body)
            except json.JSONDecodeError:
                logger.warning("Invalid TRIGGER_EXTRA_BODY JSON: %s", settings.trigger_extra_body)

        extra_json = json.dumps(extra_body, ensure_ascii=False) if extra_body else None

        existing = await self._pool.fetchval(
            "SELECT id FROM llm_providers WHERE base_urls->>'openai' = $1 AND api_key = $2",
            settings.trigger_base_url, settings.trigger_api_key,
        )
        if existing:
            provider_id = existing
        else:
            trigger_base_urls_json = json.dumps({"openai": settings.trigger_base_url}, ensure_ascii=False)
            provider_id = await self._pool.fetchval(
                """
                INSERT INTO llm_providers (alias, base_urls, api_key, stream)
                VALUES ($1, $2::jsonb, $3, $4)
                ON CONFLICT (alias) DO UPDATE SET alias = EXCLUDED.alias
                RETURNING id
                """,
                f"trigger-{settings.trigger_model}",
                trigger_base_urls_json,
                settings.trigger_api_key,
                settings.trigger_stream,
            )

        await self._pool.execute(
            """
            INSERT INTO llm_active (key, provider_id, model, api_type, extra_body, request_timeout)
            VALUES ('trigger', $1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (key) DO NOTHING
            """,
            provider_id, settings.trigger_model, settings.trigger_api_type, extra_json,
            settings.trigger_request_timeout,
        )

        kwargs = {
            "base_url": settings.trigger_base_url,
            "api_key": settings.trigger_api_key,
            "model": settings.trigger_model,
            "default_temperature": 0.0,
            "default_max_tokens": 64,
            "request_timeout": settings.trigger_request_timeout,
            "stream": settings.trigger_stream,
            "extra_body": extra_body,
        }
        if settings.trigger_api_type == "gemini":
            self._trigger_provider = GeminiProvider(**kwargs)
        elif settings.trigger_api_type == "anthropic":
            self._trigger_provider = AnthropicProvider(**kwargs)
        else:
            self._trigger_provider = OpenAICompatProvider(**kwargs)
        self._trigger_alias = f"trigger-{settings.trigger_model}"
        self._trigger_model = settings.trigger_model
        self._trigger_api_type = settings.trigger_api_type

        logger.info(
            "Seeded trigger provider from .env: %s / %s",
            settings.trigger_base_url, settings.trigger_model,
        )
