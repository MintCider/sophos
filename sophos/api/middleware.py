"""API 中间件。"""

import logging

from aiohttp import web

from sophos.api.auth import validate_session
from sophos.config import settings

logger = logging.getLogger(__name__)

# 无需鉴权的 API 路径
_AUTH_WHITELIST = frozenset({
    "/api/auth/login",
    "/api/auth/check",
    "/api/health",
})


@web.middleware
async def error_middleware(request: web.Request, handler):
    """捕获 handler 异常，统一返回 JSON 错误响应。"""
    try:
        return await handler(request)
    except web.HTTPException as e:
        return web.json_response({"error": e.reason, "status": e.status}, status=e.status)
    except Exception:
        logger.exception("Unhandled error in %s %s", request.method, request.path)
        return web.json_response({"error": "Internal Server Error", "status": 500}, status=500)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """API 鉴权：仅拦截 /api/* 路径，白名单放行。"""
    # 未设置密码 → 全部放行
    if not settings.webui_password:
        return await handler(request)

    # 非 API 路径放行（静态文件 / SPA fallback）
    if not request.path.startswith("/api/"):
        return await handler(request)

    # 白名单放行
    if request.path in _AUTH_WHITELIST:
        return await handler(request)

    # 检查 session cookie
    token = request.cookies.get("sophos_session")
    if not token or not validate_session(token):
        raise web.HTTPUnauthorized(reason="Not authenticated")

    return await handler(request)
