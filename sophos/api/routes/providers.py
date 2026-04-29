"""Provider 管理 API。"""

from aiohttp import web

from sophos.llm.provider_manager import ProviderManager, _normalize_base_urls, resolve_base_url

routes = web.RouteTableDef()


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
    """列出 provider，供 WebUI 展示（不含明文 API Key）。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    providers = await provider_mgr.list_providers()

    detailed = []
    for item in providers:
        base_urls = item["base_urls"]
        preferred_type = _preferred_api_type(base_urls)
        detailed.append(
            {
                **item,
                "preferred_api_type": preferred_type,
                "preferred_base_url": resolve_base_url(base_urls, preferred_type),
            }
        )

    return web.json_response({"providers": detailed})


@routes.get("/api/providers/{alias}/api-key")
async def reveal_api_key(request: web.Request) -> web.Response:
    """获取 provider 的明文 API Key。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    alias = request.match_info["alias"]
    api_key = await provider_mgr.get_provider_api_key(alias)
    if api_key is None:
        return web.json_response(
            {"message": f"provider '{alias}' 不存在"},
            status=404,
        )
    return web.json_response({"api_key": api_key})


@routes.post("/api/providers/{alias}/models/refresh")
async def refresh_provider_models(request: web.Request) -> web.Response:
    """刷新 provider 的可用模型列表。"""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    alias = request.match_info["alias"]
    result = await provider_mgr.fetch_models(alias)
    if isinstance(result, str):
        return web.json_response({"message": result}, status=400)
    return web.json_response(
        {
            "message": f"已刷新 {alias} 的可用模型列表（{len(result)} 个）",
            "models": result,
        }
    )


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
