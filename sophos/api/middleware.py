"""JSON 错误处理中间件。"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


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
