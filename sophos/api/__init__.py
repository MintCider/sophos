"""WebUI API 应用工厂。"""

import aiohttp_cors
import asyncpg
from aiohttp import web

from sophos.api.middleware import auth_middleware, error_middleware
from sophos.api.routes import setup_routes
from sophos.llm.provider_manager import ProviderManager
from sophos.memory.store import MemoryStore


async def create_app(
    *,
    pool: asyncpg.Pool,
    provider_mgr: ProviderManager,
    memory_store: MemoryStore | None,
) -> web.Application:
    """创建并配置 aiohttp 应用。"""
    app = web.Application(middlewares=[error_middleware, auth_middleware])

    # 依赖注入到 app context，handler 通过 request.app["xxx"] 访问
    app["pool"] = pool
    app["provider_mgr"] = provider_mgr
    app["memory_store"] = memory_store

    # CORS — 开发阶段 Vite(5173) → API(8080) 跨域
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*",
        )
    })

    setup_routes(app, cors)
    return app
