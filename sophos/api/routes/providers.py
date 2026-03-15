"""Provider 管理 API。"""

import json
from typing import Any

from aiohttp import web

from sophos.llm.provider_manager import ProviderManager, resolve_base_url

routes = web.RouteTableDef()

_VALID_API_TYPES = {"openai", "gemini", "anthropic"}


def _mask_api_key(api_key: str) -> str:
    """将 API Key 打码展示。"""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}{'*' * max(len(api_key) - 8, 4)}{api_key[-4:]}"


def _normalize_base_urls(base_urls: Any) -> dict[str, str]:
    """归一化 base_urls。"""
    if isinstance(base_urls, str):
        base_urls = json.loads(base_urls)
    if not isinstance(base_urls, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in base_urls.items():
        if key not in _VALID_API_TYPES or not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            result[key] = stripped
    return result


def _preferred_api_type(base_urls: dict[str, str]) -> str:
    """选一个默认展示的 API 类型。"""
    if base_urls.get("openai"):
        return "openai"
    for api_type in ("gemini", "anthropic"):
        if base_urls.get(api_type):
            return api_type
    return "openai"


@routes.get("/api/providers")
async def list_providers(request: web.Request) -> web.Response:
    """列出 provider，供 WebUI 展示。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    providers = await provider_mgr.list_providers()
    active_rows = await provider_mgr._pool.fetch(  # noqa: SLF001
        """
        SELECT a.key, p.alias, a.model, a.api_type
        FROM llm_active a
        JOIN llm_providers p ON p.id = a.provider_id
        """
    )
    active_by_alias: dict[str, list[dict[str, str]]] = {}
    for row in active_rows:
        active_by_alias.setdefault(row["alias"], []).append({
            "key": row["key"],
            "model": row["model"],
            "api_type": row.get("api_type", "openai") or "openai",
        })

    detailed = []
    for item in providers:
        row = await provider_mgr._pool.fetchrow(  # noqa: SLF001
            "SELECT api_key FROM llm_providers WHERE alias = $1",
            item["alias"],
        )
        api_key = row["api_key"] if row else ""
        base_urls = _normalize_base_urls(item["base_urls"])
        preferred_type = _preferred_api_type(base_urls)
        detailed.append({
            **item,
            "base_urls": base_urls,
            "preferred_api_type": preferred_type,
            "preferred_base_url": resolve_base_url(base_urls, preferred_type),
            "api_key_masked": _mask_api_key(api_key),
            "api_key": api_key,
            "active_slots": active_by_alias.get(item["alias"], []),
        })

    return web.json_response({"providers": detailed})


@routes.post("/api/providers/{alias}/models/refresh")
async def refresh_provider_models(request: web.Request) -> web.Response:
    """刷新 provider 的可用模型列表。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    alias = request.match_info["alias"]
    result = await provider_mgr.fetch_models(alias)
    if isinstance(result, str):
        return web.json_response({"message": result}, status=400)
    return web.json_response({
        "message": f"已刷新 {alias} 的可用模型列表（{len(result)} 个）",
        "models": result,
    })


@routes.post("/api/providers")
async def create_provider(request: web.Request) -> web.Response:
    """新增 provider。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    payload = await request.json()

    alias = str(payload.get("alias", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    base_urls = _normalize_base_urls(payload.get("base_urls"))

    if not alias:
        raise web.HTTPBadRequest(reason="alias 不能为空")
    if not api_key:
        raise web.HTTPBadRequest(reason="api_key 不能为空")
    if not base_urls:
        raise web.HTTPBadRequest(reason="至少提供一个 Base URL")

    result = await provider_mgr.add_provider(alias, base_urls, api_key)
    status = 400 if "已存在" in result or "至少提供" in result else 200
    return web.json_response({"message": result}, status=status)


@routes.put("/api/providers/{alias}")
async def update_provider(request: web.Request) -> web.Response:
    """更新 provider 的 base URLs。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    alias = request.match_info["alias"]
    payload = await request.json()
    base_urls = _normalize_base_urls(payload.get("base_urls"))
    if not base_urls:
        raise web.HTTPBadRequest(reason="至少提供一个 Base URL")
    result = await provider_mgr.update_provider(alias, base_urls=base_urls)
    status = 404 if "不存在" in result else 200
    return web.json_response({"message": result}, status=status)


@routes.delete("/api/providers/{alias}")
async def delete_provider(request: web.Request) -> web.Response:
    """删除 provider。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    alias = request.match_info["alias"]
    result = await provider_mgr.remove_provider(alias)
    status = 400 if ("无法删除" in result or "不存在" in result) else 200
    return web.json_response({"message": result}, status=status)
