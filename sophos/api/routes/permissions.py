"""权限管理 API。"""

import math

import asyncpg
from aiohttp import web

from sophos import permission, user_policy

routes = web.RouteTableDef()

_SESSION_SCOPE_TYPES = frozenset({"group", "private"})
_ALL_SCOPE_TYPES = _SESSION_SCOPE_TYPES | {"global"}
_PERMISSIONS = frozenset(
    {
        "bot",
        "bot.group",
        "bot.private",
        "cmd.tools",
        "cmd.config",
        "cmd.llm",
        "cmd.trigger",
        "cmd.memory",
        "cmd.prompt",
        "delegate",
    }
)


def _validate_session_scope_type(scope_type: str) -> None:
    if scope_type not in _SESSION_SCOPE_TYPES:
        raise web.HTTPBadRequest(reason="会话类型必须是 group 或 private")


def _validate_scope(scope_type: str, scope_id: int) -> None:
    if scope_type not in _ALL_SCOPE_TYPES:
        raise web.HTTPBadRequest(reason="作用域类型必须是 global、group 或 private")
    if scope_type == "global" and scope_id != 0:
        raise web.HTTPBadRequest(reason="global 作用域的 scope_id 必须为 0")
    if scope_type != "global" and scope_id <= 0:
        raise web.HTTPBadRequest(reason="会话 scope_id 必须是正整数")


def _parse_int(value: object, field: str) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason=f"{field} 必须是整数") from exc
    return parsed


def _parse_float(value: object, field: str) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason=f"{field} 必须是数字") from exc
    return parsed


# ── 会话权限 (perm_scope + perm_tool) ─────────────────────


@routes.get("/api/permissions/scopes")
async def list_scopes(request: web.Request) -> web.Response:
    """列出所有会话权限，附带工具白名单。"""
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        "SELECT scope_type, scope_id, enabled, updated_at "
        "FROM perm_scope WHERE scope_type = ANY($1::text[]) "
        "ORDER BY scope_type, scope_id",
        sorted(_SESSION_SCOPE_TYPES),
    )
    scopes = []
    for r in rows:
        tools = await permission.get_tool_whitelist(
            pool,
            r["scope_type"],
            r["scope_id"],
        )
        scopes.append(
            {
                "scope_type": r["scope_type"],
                "scope_id": r["scope_id"],
                "enabled": r["enabled"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "tools": tools,
            }
        )
    return web.json_response({"scopes": scopes})


@routes.post("/api/permissions/scopes")
async def create_scope(request: web.Request) -> web.Response:
    """新建会话条目。"""
    pool: asyncpg.Pool = request.app["pool"]
    payload = await request.json()
    scope_type = str(payload.get("scope_type", "")).strip()
    scope_id = payload.get("scope_id")
    enabled = payload.get("enabled", True)

    if not scope_type:
        raise web.HTTPBadRequest(reason="scope_type 不能为空")
    _validate_session_scope_type(scope_type)
    if scope_id is None:
        raise web.HTTPBadRequest(reason="scope_id 不能为空")
    parsed_scope_id = _parse_int(scope_id, "scope_id")
    _validate_scope(scope_type, parsed_scope_id)
    if not isinstance(enabled, bool):
        raise web.HTTPBadRequest(reason="enabled 必须是布尔值")

    await permission.set_scope_enabled(pool, scope_type, parsed_scope_id, enabled)
    return web.json_response(
        {
            "message": f"已创建会话 {scope_type}:{scope_id}",
        }
    )


@routes.put("/api/permissions/scopes/{scope_type}/{scope_id}")
async def update_scope(request: web.Request) -> web.Response:
    """启用/禁用会话。"""
    pool: asyncpg.Pool = request.app["pool"]
    scope_type = request.match_info["scope_type"]
    _validate_session_scope_type(scope_type)
    scope_id = _parse_int(request.match_info["scope_id"], "scope_id")
    _validate_scope(scope_type, scope_id)
    payload = await request.json()
    enabled = payload.get("enabled")

    if not isinstance(enabled, bool):
        raise web.HTTPBadRequest(reason="enabled 必须是布尔值")

    await permission.set_scope_enabled(pool, scope_type, scope_id, enabled)
    state = "启用" if enabled else "禁用"
    return web.json_response({"message": f"已{state}会话 {scope_type}:{scope_id}"})


@routes.delete("/api/permissions/scopes/{scope_type}/{scope_id}")
async def delete_scope(request: web.Request) -> web.Response:
    """删除会话条目及其工具白名单。"""
    pool: asyncpg.Pool = request.app["pool"]
    scope_type = request.match_info["scope_type"]
    _validate_session_scope_type(scope_type)
    scope_id = _parse_int(request.match_info["scope_id"], "scope_id")
    _validate_scope(scope_type, scope_id)

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "DELETE FROM perm_scope WHERE scope_type=$1 AND scope_id=$2",
            scope_type,
            scope_id,
        )
        await connection.execute(
            "DELETE FROM perm_tool WHERE scope_type=$1 AND scope_id=$2",
            scope_type,
            scope_id,
        )
    permission.invalidate()
    return web.json_response({"message": f"已删除会话 {scope_type}:{scope_id}"})


@routes.put("/api/permissions/scopes/{scope_type}/{scope_id}/tools")
async def set_scope_tools(request: web.Request) -> web.Response:
    """设置工具白名单（覆盖写入）。"""
    pool: asyncpg.Pool = request.app["pool"]
    scope_type = request.match_info["scope_type"]
    _validate_session_scope_type(scope_type)
    scope_id = _parse_int(request.match_info["scope_id"], "scope_id")
    _validate_scope(scope_type, scope_id)
    payload = await request.json()
    tools = payload.get("tools", [])

    if not isinstance(tools, list):
        raise web.HTTPBadRequest(reason="tools 必须是数组")
    if not tools:
        raise web.HTTPBadRequest(reason="白名单不能为空；如需全部可用，请执行重置")
    if not all(isinstance(name, str) and 0 < len(name.strip()) <= 64 for name in tools):
        raise web.HTTPBadRequest(reason="工具名称必须是 1–64 个字符的字符串")
    normalized_tools = list(dict.fromkeys(name.strip() for name in tools))

    registry = request.app.get("tool_registry")
    if registry is not None:
        known_tools = set(registry.list_tools())
        invalid_tools = sorted(set(normalized_tools) - known_tools)
        if invalid_tools:
            raise web.HTTPBadRequest(reason=f"未知工具: {', '.join(invalid_tools)}")

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "DELETE FROM perm_tool WHERE scope_type=$1 AND scope_id=$2",
            scope_type,
            scope_id,
        )
        await connection.executemany(
            "INSERT INTO perm_tool (scope_type, scope_id, tool_name) VALUES ($1, $2, $3)",
            [(scope_type, scope_id, name) for name in normalized_tools],
        )
    return web.json_response({"message": f"已更新工具白名单（{len(normalized_tools)} 个）"})


@routes.delete("/api/permissions/scopes/{scope_type}/{scope_id}/tools")
async def reset_scope_tools(request: web.Request) -> web.Response:
    """重置工具白名单（全部可用）。"""
    pool: asyncpg.Pool = request.app["pool"]
    scope_type = request.match_info["scope_type"]
    _validate_session_scope_type(scope_type)
    scope_id = _parse_int(request.match_info["scope_id"], "scope_id")
    _validate_scope(scope_type, scope_id)

    await permission.reset_tools(pool, scope_type, scope_id)
    return web.json_response({"message": "已重置工具白名单（全部可用）"})


# ── 用户授权 (perm_grant) ─────────────────────────────────


@routes.get("/api/permissions/grants")
async def list_grants(request: web.Request) -> web.Response:
    """列出所有用户授权。支持 ?scope_type=&scope_id= 过滤。"""
    pool: asyncpg.Pool = request.app["pool"]

    scope_type = request.query.get("scope_type")
    scope_id_raw = request.query.get("scope_id")

    if scope_type and scope_id_raw is not None:
        parsed_scope_id = _parse_int(scope_id_raw, "scope_id")
        _validate_scope(scope_type, parsed_scope_id)
        rows = await pool.fetch(
            "SELECT user_id, scope_type, scope_id, permission, granted_by, created_at "
            "FROM perm_grant WHERE scope_type=$1 AND scope_id=$2 "
            "ORDER BY user_id, permission",
            scope_type,
            parsed_scope_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT user_id, scope_type, scope_id, permission, granted_by, created_at "
            "FROM perm_grant ORDER BY user_id, scope_type, scope_id, permission"
        )

    grants = [
        {
            "user_id": r["user_id"],
            "scope_type": r["scope_type"],
            "scope_id": r["scope_id"],
            "permission": r["permission"],
            "granted_by": r["granted_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return web.json_response({"grants": grants})


@routes.post("/api/permissions/grants")
async def create_grant(request: web.Request) -> web.Response:
    """授予权限。"""
    pool: asyncpg.Pool = request.app["pool"]
    payload = await request.json()

    user_id = payload.get("user_id")
    scope_type = str(payload.get("scope_type", "")).strip()
    scope_id = payload.get("scope_id")
    perm_name = str(payload.get("permission", "")).strip()

    if not user_id:
        raise web.HTTPBadRequest(reason="user_id 不能为空")
    if not scope_type:
        raise web.HTTPBadRequest(reason="scope_type 不能为空")
    if scope_id is None:
        raise web.HTTPBadRequest(reason="scope_id 不能为空")
    if not perm_name:
        raise web.HTTPBadRequest(reason="permission 不能为空")

    parsed_user_id = _parse_int(user_id, "user_id")
    parsed_scope_id = _parse_int(scope_id, "scope_id")
    if parsed_user_id <= 0:
        raise web.HTTPBadRequest(reason="user_id 必须是正整数")
    _validate_scope(scope_type, parsed_scope_id)
    if perm_name not in _PERMISSIONS:
        raise web.HTTPBadRequest(reason=f"未知权限: {perm_name}")

    await permission.grant(pool, parsed_user_id, scope_type, parsed_scope_id, perm_name, 0)
    return web.json_response({"message": f"已授予用户 {user_id} 权限 {perm_name}"})


@routes.delete("/api/permissions/grants")
async def revoke_grant(request: web.Request) -> web.Response:
    """撤销权限。"""
    pool: asyncpg.Pool = request.app["pool"]
    payload = await request.json()

    user_id = payload.get("user_id")
    scope_type = str(payload.get("scope_type", "")).strip()
    scope_id = payload.get("scope_id")
    perm_name = str(payload.get("permission", "")).strip()

    if not user_id or not scope_type or scope_id is None or not perm_name:
        raise web.HTTPBadRequest(reason="缺少必要参数")

    parsed_user_id = _parse_int(user_id, "user_id")
    if parsed_user_id <= 0:
        raise web.HTTPBadRequest(reason="user_id 必须是正整数")
    parsed_scope_id = _parse_int(scope_id, "scope_id")
    _validate_scope(scope_type, parsed_scope_id)
    if perm_name not in _PERMISSIONS:
        raise web.HTTPBadRequest(reason=f"未知权限: {perm_name}")

    removed = await permission.revoke(pool, parsed_user_id, scope_type, parsed_scope_id, perm_name)
    if not removed:
        return web.json_response({"message": "未找到该授权"}, status=404)
    return web.json_response({"message": f"已撤销用户 {user_id} 的权限 {perm_name}"})


# ── 用户触发策略 (user_trigger_policy) ────────────────────


@routes.get("/api/permissions/policies")
async def list_policies(request: web.Request) -> web.Response:
    """列出所有用户触发策略。"""
    pool: asyncpg.Pool = request.app["pool"]
    policies = await user_policy.get_all_policies(pool)
    return web.json_response(
        {
            "policies": [
                {
                    "user_id": p.user_id,
                    "scope_type": p.scope_type,
                    "scope_id": p.scope_id,
                    "suppress_llm_trigger": p.suppress_llm_trigger,
                    "rate_multiplier": p.rate_multiplier,
                    "suppress_refresh": p.suppress_refresh,
                    "note": p.note,
                }
                for p in policies
            ],
        }
    )


@routes.post("/api/permissions/policies")
async def upsert_policy(request: web.Request) -> web.Response:
    """新增/更新用户触发策略（UPSERT）。"""
    pool: asyncpg.Pool = request.app["pool"]
    payload = await request.json()

    user_id = payload.get("user_id")
    if not user_id:
        raise web.HTTPBadRequest(reason="user_id 不能为空")

    parsed_user_id = _parse_int(user_id, "user_id")
    if parsed_user_id <= 0:
        raise web.HTTPBadRequest(reason="user_id 必须是正整数")
    scope_type = str(payload.get("scope_type", "global"))
    scope_id = _parse_int(payload.get("scope_id", 0), "scope_id")
    _validate_scope(scope_type, scope_id)
    rate_multiplier = _parse_float(payload.get("rate_multiplier", 0.0), "rate_multiplier")
    if not math.isfinite(rate_multiplier) or not 0 <= rate_multiplier <= 10:
        raise web.HTTPBadRequest(reason="rate_multiplier 必须在 0–10 之间")
    suppress_llm_trigger = payload.get("suppress_llm_trigger", True)
    suppress_refresh = payload.get("suppress_refresh", True)
    if not isinstance(suppress_llm_trigger, bool) or not isinstance(suppress_refresh, bool):
        raise web.HTTPBadRequest(reason="抑制策略字段必须是布尔值")

    await user_policy.set_policy(
        pool,
        parsed_user_id,
        scope_type=scope_type,
        scope_id=scope_id,
        suppress_llm_trigger=suppress_llm_trigger,
        rate_multiplier=rate_multiplier,
        suppress_refresh=suppress_refresh,
        note=payload.get("note") or None,
    )
    return web.json_response({"message": f"已设置用户 {user_id} 的触发策略"})


@routes.delete("/api/permissions/policies/{user_id}/{scope_type}/{scope_id}")
async def delete_policy(request: web.Request) -> web.Response:
    """删除用户触发策略。"""
    pool: asyncpg.Pool = request.app["pool"]
    user_id = _parse_int(request.match_info["user_id"], "user_id")
    scope_type = request.match_info["scope_type"]
    scope_id = _parse_int(request.match_info["scope_id"], "scope_id")
    _validate_scope(scope_type, scope_id)

    removed = await user_policy.remove_policy(pool, user_id, scope_type, scope_id)
    if not removed:
        return web.json_response({"message": "未找到该策略"}, status=404)
    return web.json_response({"message": f"已删除用户 {user_id} 的触发策略"})
