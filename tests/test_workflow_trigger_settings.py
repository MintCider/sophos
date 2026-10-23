import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

from sophos.api.routes.workflows import put_settings
from sophos.trigger import TriggerConfig


class TriggerSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_percent_updates_existing_trigger_config(self):
        pool = object()
        request = SimpleNamespace(
            app={"pool": pool}, json=AsyncMock(return_value={"values": {"random_trigger_percent": 12.5}}),
        )
        with (
            patch("sophos.api.routes.workflows.trigger.set_base_rate", new_callable=AsyncMock) as save,
            patch("sophos.api.routes.workflows.trigger.load", AsyncMock(return_value=TriggerConfig(base_rate=.125))),
        ):
            response = await put_settings(request)
        save.assert_awaited_once_with(pool, .125)
        settings = json.loads(response.text)["settings"]["trigger"]
        self.assertEqual(next(s["value"] for s in settings if s["key"] == "random_trigger_percent"), 12.5)

    async def test_invalid_probability_does_not_write(self):
        for value in (-1, 101, "NaN", "Infinity"):
            request = SimpleNamespace(json=AsyncMock(return_value={"values": {"random_trigger_percent": value}}))
            with self.assertRaises(web.HTTPBadRequest):
                await put_settings(request)
