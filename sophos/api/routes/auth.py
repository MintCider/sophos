"""鉴权路由：登录 / 登出 / 状态检查。"""

import json
import logging

from aiohttp import web

from sophos.api.auth import (
    SESSION_MAX_AGE,
    create_session,
    revoke_session,
    validate_session,
)
from sophos.config import settings

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()


@routes.post("/api/auth/login")
async def login(request: web.Request) -> web.Response:
    """验证密码，成功则设置 httpOnly session cookie。"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        raise web.HTTPBadRequest(reason="Invalid JSON")

    password = body.get("password", "")
    if not settings.webui_password or password != settings.webui_password:
        raise web.HTTPUnauthorized(reason="Wrong password")

    token = create_session()
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        "sophos_session",
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@routes.post("/api/auth/logout")
async def logout(request: web.Request) -> web.Response:
    """清除 session cookie 并撤销服务端 session。"""
    token = request.cookies.get("sophos_session")
    if token:
        revoke_session(token)
    resp = web.json_response({"ok": True})
    resp.del_cookie("sophos_session", path="/")
    return resp


@routes.get("/api/auth/check")
async def check(request: web.Request) -> web.Response:
    """返回当前鉴权状态。required=false 表示未设置密码，无需登录。"""
    required = bool(settings.webui_password)
    if not required:
        return web.json_response({"authenticated": True, "required": False})

    token = request.cookies.get("sophos_session")
    authenticated = bool(token and validate_session(token))
    return web.json_response({
        "authenticated": authenticated,
        "required": True,
    })
