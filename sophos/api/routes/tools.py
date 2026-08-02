"""工具管理 API。"""

import json

import asyncpg
from aiohttp import web

from sophos.tools.image_gen import _DEFAULT_DESCRIPTION, _DEFAULT_REQUEST_TIMEOUT
from sophos.tools.registry import UNSAFE_DISABLE_TOOLS, ToolRegistry

routes = web.RouteTableDef()

_GROUP_LABELS: dict[str, str] = {
    "messaging": "消息交互",
    "admin": "成员管理",
    "moderation": "成员管理",
    "memory": "记忆系统",
    "vision": "视觉",
    "web": "联网",
    "image_generation": "图像生成",
    "other": "其他",
}

_GROUP_ORDER: list[str] = [
    "messaging",
    "admin",
    "memory",
    "vision",
    "web",
    "image_generation",
    "other",
]


def _json_object(raw: object) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _coerce_request_timeout(value: object) -> int:
    try:
        timeout = int(value) if value is not None else _DEFAULT_REQUEST_TIMEOUT
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason="request_timeout 必须是整数秒") from None
    if not 5 <= timeout <= 3600:
        raise web.HTTPBadRequest(reason="request_timeout 范围必须是 5~3600 秒")
    return timeout


@routes.get("/api/tools")
async def list_tools(request: web.Request) -> web.Response:
    """列出所有工具，按类别分组，含启用状态和描述覆盖信息。

    enabled 以 custom_tools 表为真相来源（无记录即默认启用）。
    """
    registry: ToolRegistry = request.app.get("tool_registry")  # type: ignore[assignment]
    if registry is None:
        return web.json_response({"error": "tool registry not available"}, status=500)

    pool: asyncpg.Pool = request.app["pool"]

    override_rows = await pool.fetch(
        "SELECT tool_name, description FROM tool_description_overrides",
    )
    description_overrides = {r["tool_name"]: r["description"] for r in override_rows}

    custom_rows = await pool.fetch(
        """
        SELECT name, display_name, provider_alias, model_name, api_type, send_as, enabled
        FROM custom_tools
        """
    )
    custom_configs = {r["name"]: dict(r) for r in custom_rows}

    tools_info = registry.get_tools_info(description_overrides=description_overrides)

    for info in tools_info:
        custom = custom_configs.get(info["name"])
        if custom is not None:
            info["enabled"] = custom["enabled"]
            info["custom_config"] = {
                "display_name": custom["display_name"],
                "provider_alias": custom["provider_alias"],
                "model_name": custom["model_name"],
                "api_type": custom["api_type"],
                "send_as": custom["send_as"],
            }
        else:
            info["enabled"] = info["is_builtin"]

    groups: dict[str, list[dict]] = {}
    for info in tools_info:
        groups.setdefault(info["group"], []).append(info)

    ordered_groups = []
    for group_key in _GROUP_ORDER:
        if group_key in groups:
            ordered_groups.append(
                {
                    "key": group_key,
                    "label": _GROUP_LABELS.get(group_key, group_key),
                    "tools": groups[group_key],
                }
            )
    for group_key, tools_list in groups.items():
        if group_key not in _GROUP_ORDER:
            ordered_groups.append(
                {
                    "key": group_key,
                    "label": _GROUP_LABELS.get(group_key, group_key),
                    "tools": tools_list,
                }
            )

    return web.json_response({"groups": ordered_groups})


@routes.put("/api/tools/{name}/description")
async def update_tool_description(request: web.Request) -> web.Response:
    """更新工具描述（覆盖默认值）。"""
    pool: asyncpg.Pool = request.app["pool"]
    tool_name = request.match_info["name"]
    payload = await request.json()
    description = str(payload.get("description", "")).strip()

    if not description:
        raise web.HTTPBadRequest(reason="description 不能为空")

    await pool.execute(
        """
        INSERT INTO tool_description_overrides (tool_name, description, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (tool_name) DO UPDATE SET description = $2, updated_at = now()
        """,
        tool_name,
        description,
    )
    return web.json_response({"message": f"工具 '{tool_name}' 描述已更新"})


@routes.delete("/api/tools/{name}/description")
async def reset_tool_description(request: web.Request) -> web.Response:
    """恢复工具默认描述。"""
    pool: asyncpg.Pool = request.app["pool"]
    tool_name = request.match_info["name"]

    result = await pool.execute(
        "DELETE FROM tool_description_overrides WHERE tool_name = $1",
        tool_name,
    )
    if result == "DELETE 0":
        return web.json_response(
            {"message": f"工具 '{tool_name}' 无自定义描述"},
            status=404,
        )
    return web.json_response({"message": f"工具 '{tool_name}' 描述已恢复默认"})


@routes.put("/api/tools/{name}/enabled")
async def set_tool_enabled(request: web.Request) -> web.Response:
    """设置工具启用/禁用状态。"""
    pool: asyncpg.Pool = request.app["pool"]
    registry: ToolRegistry = request.app.get("tool_registry")  # type: ignore[assignment]
    tool_name = request.match_info["name"]
    payload = await request.json()
    enabled = bool(payload.get("enabled", True))

    if not enabled and tool_name in UNSAFE_DISABLE_TOOLS:
        raise web.HTTPBadRequest(reason=f"工具 '{tool_name}' 不可禁用")

    if registry is None:
        raise web.HTTPInternalServerError(reason="tool registry not available")
    tool = registry.get(tool_name)
    if tool is None:
        raise web.HTTPBadRequest(reason=f"工具 '{tool_name}' 不存在")

    registry.set_disabled(tool_name, not enabled)

    custom_row = await pool.fetchrow(
        "SELECT name FROM custom_tools WHERE name = $1",
        tool_name,
    )
    if custom_row:
        await pool.execute(
            "UPDATE custom_tools SET enabled = $2, updated_at = now() WHERE name = $1",
            tool_name,
            enabled,
        )
    else:
        # 内置工具首次设置 → 写一条记录跟踪启用状态
        await pool.execute(
            """
            INSERT INTO custom_tools (
                name, display_name, description, category, conversation_kinds, tool_type, enabled
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, 'builtin_override', $6)
            ON CONFLICT (name) DO UPDATE SET enabled = $6, updated_at = now()
            """,
            tool_name,
            tool.name,
            tool.description,
            tool.category,
            json.dumps(sorted(tool.conversation_kinds) if tool.conversation_kinds is not None else []),
            enabled,
        )

    from sophos.pipeline import _invalidate_tool_disabled_cache

    _invalidate_tool_disabled_cache()

    return web.json_response(
        {
            "message": f"工具 '{tool_name}' 已{'启用' if enabled else '禁用'}",
        }
    )


@routes.get("/api/tools/image-generation")
async def get_image_generation_config(request: web.Request) -> web.Response:
    """获取图像生成工具配置。"""
    pool: asyncpg.Pool = request.app["pool"]

    row = await pool.fetchrow(
        """
        SELECT name, display_name, description, provider_alias, model_name,
               api_type, send_as, config, enabled
        FROM custom_tools
        WHERE name = 'generate_image'
        """
    )

    if row:
        config = dict(row)
        custom_config = _json_object(config.pop("config", None))
        config["request_timeout"] = _coerce_request_timeout(custom_config.get("request_timeout"))
    else:
        config = {
            "name": "generate_image",
            "display_name": "图像生成",
            "description": _DEFAULT_DESCRIPTION,
            "provider_alias": None,
            "model_name": None,
            "api_type": "openai",
            "send_as": "image_url",
            "request_timeout": _DEFAULT_REQUEST_TIMEOUT,
            "enabled": False,
        }

    providers = await pool.fetch("SELECT alias FROM llm_providers ORDER BY created_at")
    config["available_providers"] = [r["alias"] for r in providers]

    return web.json_response({"config": config})


@routes.put("/api/tools/image-generation")
async def update_image_generation_config(request: web.Request) -> web.Response:
    """更新图像生成工具配置。"""
    pool: asyncpg.Pool = request.app["pool"]
    registry: ToolRegistry = request.app.get("tool_registry")  # type: ignore[assignment]
    payload = await request.json()

    provider_alias = payload.get("provider_alias") or None
    model_name = payload.get("model_name") or None
    api_type = payload.get("api_type", "openai")
    send_as = payload.get("send_as", "image_url")
    description = payload.get("description") or _DEFAULT_DESCRIPTION
    request_timeout = _coerce_request_timeout(payload.get("request_timeout"))
    enabled = bool(payload.get("enabled", False))

    if api_type not in ("openai", "gemini"):
        raise web.HTTPBadRequest(reason="api_type 必须是 openai 或 gemini")

    if provider_alias:
        exists = await pool.fetchval(
            "SELECT 1 FROM llm_providers WHERE alias = $1",
            provider_alias,
        )
        if not exists:
            raise web.HTTPBadRequest(reason=f"供应商 '{provider_alias}' 不存在")

    await pool.execute(
        """
        INSERT INTO custom_tools (
            name, display_name, description, category, conversation_kinds, tool_type,
            provider_alias, model_name, api_type, send_as, config, enabled
        )
        VALUES (
            'generate_image', '图像生成', $1, 'output', '[]'::jsonb, 'image_generation',
            $2, $3, $4, $5, $6::jsonb, $7
        )
        ON CONFLICT (name) DO UPDATE SET
            description = $1, provider_alias = $2, model_name = $3,
            api_type = $4, send_as = $5,
            config = COALESCE(custom_tools.config, '{}'::jsonb) || $6::jsonb,
            enabled = $7, updated_at = now()
        """,
        description,
        provider_alias,
        model_name,
        api_type,
        send_as,
        json.dumps({"request_timeout": request_timeout}, ensure_ascii=False),
        enabled,
    )

    if registry is not None:
        registry.set_disabled("generate_image", not enabled)

    from sophos.pipeline import _invalidate_tool_disabled_cache

    _invalidate_tool_disabled_cache()

    return web.json_response({"message": "图像生成工具配置已更新"})
