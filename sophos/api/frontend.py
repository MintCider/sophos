"""Serve the production WebUI from the same aiohttp process as the API."""

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def setup_frontend_routes(app: web.Application, web_dist: Path = DEFAULT_WEB_DIST) -> bool:
    """Register static assets and Vue history fallback when a build is present."""
    web_dist = web_dist.resolve()
    index_file = web_dist / "index.html"
    if not index_file.is_file():
        logger.info("WebUI build not found at %s; API-only mode enabled", web_dist)
        return False

    assets_dir = web_dist / "assets"
    if assets_dir.is_dir():
        app.router.add_static("/assets/", assets_dir, name="web-assets")

    async def serve_spa(request: web.Request) -> web.FileResponse:
        relative_path = request.match_info.get("path", "")
        if relative_path == "api" or relative_path.startswith("api/"):
            raise web.HTTPNotFound()

        requested_file = (web_dist / relative_path).resolve()
        try:
            requested_file.relative_to(web_dist)
        except ValueError as exc:
            raise web.HTTPNotFound() from exc

        if relative_path and requested_file.is_file():
            return web.FileResponse(requested_file)
        return web.FileResponse(index_file, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/", serve_spa, name="web-index")
    app.router.add_get("/{path:.*}", serve_spa, name="web-spa")
    logger.info("Serving WebUI from %s", web_dist)
    return True
