"""健康检查 API。"""

from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/api/health")
async def health(request: web.Request) -> web.Response:
    """简单的健康检查端点，前端用于检测后端是否在线。"""
    return web.json_response({"status": "ok"})
