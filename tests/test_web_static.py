import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sophos.api.frontend import setup_frontend_routes


class FrontendRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        web_dist = Path(self.temp_dir.name)
        (web_dist / "assets").mkdir()
        (web_dist / "index.html").write_text("<main>Sophos WebUI</main>", encoding="utf-8")
        (web_dist / "assets" / "app.js").write_text("console.log('sophos')", encoding="utf-8")

        app = web.Application()

        async def health(_: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        app.router.add_get("/api/health", health)
        self.assertTrue(setup_frontend_routes(app, web_dist))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_serves_index_and_spa_history_fallback(self) -> None:
        for path in ("/", "/permissions", "/providers/example"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn("Sophos WebUI", await response.text())
                self.assertEqual(response.headers["Cache-Control"], "no-cache")

    async def test_serves_built_assets(self) -> None:
        response = await self.client.get("/assets/app.js")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "console.log('sophos')")

    async def test_preserves_api_routes_and_api_404(self) -> None:
        response = await self.client.get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})

        response = await self.client.get("/api/missing")
        self.assertEqual(response.status, 404)


class MissingFrontendBuildTests(unittest.TestCase):
    def test_api_only_mode_when_dist_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = web.Application()
            self.assertFalse(setup_frontend_routes(app, Path(temp_dir)))
            self.assertEqual(len(list(app.router.routes())), 0)


if __name__ == "__main__":
    unittest.main()
