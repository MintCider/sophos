"""路由汇总。"""

import aiohttp_cors
from aiohttp import web

from sophos.api.routes.auth import routes as auth_routes
from sophos.api.routes.health import routes as health_routes
from sophos.api.routes.logs import routes as log_routes


def setup_routes(app: web.Application, cors: aiohttp_cors.CorsConfig) -> None:
    """注册所有 API 路由并应用 CORS。"""
    app.router.add_routes(auth_routes)
    app.router.add_routes(health_routes)
    app.router.add_routes(log_routes)

    # 对所有已注册路由应用 CORS
    for route in list(app.router.routes()):
        cors.add(route)
