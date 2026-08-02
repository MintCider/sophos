"""路由汇总。"""

from sophos.api.routes.auth import routes as auth_routes
from sophos.api.routes.health import routes as health_routes
from sophos.api.routes.logs import routes as log_routes
from sophos.api.routes.permissions import routes as permission_routes
from sophos.api.routes.providers import routes as provider_routes
from sophos.api.routes.tools import routes as tools_routes


def setup_routes(app, cors):
    app.router.add_routes(auth_routes)
    app.router.add_routes(health_routes)
    app.router.add_routes(log_routes)
    app.router.add_routes(permission_routes)
    app.router.add_routes(provider_routes)
    app.router.add_routes(tools_routes)
    for route in list(app.router.routes()):
        cors.add(route)
