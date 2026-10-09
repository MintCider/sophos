import unittest
from unittest.mock import AsyncMock, patch

from sophos.commands import CommandContext, handle_llm_command
from sophos.llm.provider_manager import ProviderManager
from sophos.platform import ConversationKind
from sophos.tools.image_gen import (
    bind_image_model,
    default_image_config,
    format_image_binding,
    parse_request_timeout,
    persist_image_config,
    unbind_image_model,
)


def _command_context() -> CommandContext:
    return CommandContext(
        message_service=object(),  # type: ignore[arg-type]
        conversation_id=1,
        conversation_kind=ConversationKind.DIRECT,
    )


class ImageConfigHelperTests(unittest.TestCase):
    def test_format_unbound(self) -> None:
        self.assertEqual(format_image_binding(default_image_config()), "图像生成: 未绑定模型")

    def test_format_bound(self) -> None:
        text = format_image_binding(
            {
                "provider_alias": "openai",
                "model_name": "gpt-image-1",
                "api_type": "openai",
                "enabled": True,
                "request_timeout": 120,
            }
        )
        self.assertIn("openai / gpt-image-1", text)
        self.assertIn("启用", text)
        self.assertIn("120s", text)

    def test_timeout_parser(self) -> None:
        self.assertEqual(parse_request_timeout(30), 30)
        with self.assertRaises(ValueError):
            parse_request_timeout("abc")
        with self.assertRaises(ValueError):
            parse_request_timeout(2)


class ImageBindTests(unittest.IsolatedAsyncioTestCase):
    async def test_bind_rejects_missing_provider(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = None
        result = await bind_image_model(pool, "missing", "gpt-image-1")
        self.assertEqual(result, "provider 'missing' 不存在")

    async def test_bind_rejects_unsupported_type(self) -> None:
        pool = AsyncMock()
        result = await bind_image_model(pool, "openai", "gpt-image-1", api_type="anthropic")
        self.assertEqual(result, "type 必须是 openai / gemini")

    async def test_bind_persists_enabled_model(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.side_effect = [
            {"alias": "openai", "base_urls": {"openai": "https://api.openai.com/v1"}},
            None,
        ]
        with patch("sophos.tools.image_gen._after_image_config_change"):
            result = await bind_image_model(pool, "openai", "gpt-image-1")
        self.assertEqual(result, "已绑定图像生成到 openai / gpt-image-1")
        args = pool.execute.await_args.args
        self.assertEqual(args[2], "openai")
        self.assertEqual(args[3], "gpt-image-1")
        self.assertTrue(args[7])

    async def test_unbind_clears_binding(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {
            "name": "generate_image",
            "display_name": "图像生成",
            "description": "desc",
            "provider_alias": "openai",
            "model_name": "gpt-image-1",
            "api_type": "openai",
            "send_as": "image_url",
            "config": {"request_timeout": 600},
            "enabled": True,
        }
        with patch("sophos.tools.image_gen._after_image_config_change"):
            result = await unbind_image_model(pool)
        self.assertEqual(result, "已解除图像生成的模型绑定")
        args = pool.execute.await_args.args
        self.assertIsNone(args[2])
        self.assertIsNone(args[3])
        self.assertFalse(args[7])


class ImageSlotProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_providers_includes_image_slot(self) -> None:
        pool = AsyncMock()
        pool.fetch.side_effect = [
            [
                {
                    "alias": "openai",
                    "base_urls": {"openai": "https://api.openai.com/v1"},
                    "api_key": "sk-test-key",
                    "models": ["gpt-image-1"],
                    "request_policy": {},
                }
            ],
            [],
        ]
        pool.fetchrow.return_value = {
            "provider_alias": "openai",
            "model_name": "gpt-image-1",
            "api_type": "openai",
        }
        manager = ProviderManager(pool)
        providers = await manager.list_providers()
        self.assertEqual(
            providers[0]["active_slots"],
            [{"key": "image", "model": "gpt-image-1", "api_type": "openai"}],
        )

    async def test_remove_provider_blocked_by_image_binding(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = []
        pool.fetchval.return_value = 1
        manager = ProviderManager(pool)
        result = await manager.remove_provider("openai")
        self.assertIn("image", result)
        pool.execute.assert_not_called()


class ImageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_when_unbound(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = None
        manager = ProviderManager(pool)
        with patch("sophos.commands._reply", new_callable=AsyncMock) as reply:
            await handle_llm_command(".llm image", context=_command_context(), provider_mgr=manager)
        self.assertIn("未绑定模型", reply.await_args.args[1])

    async def test_switch_usage(self) -> None:
        manager = ProviderManager(AsyncMock())
        with patch("sophos.commands._reply", new_callable=AsyncMock) as reply:
            await handle_llm_command(
                ".llm image switch",
                context=_command_context(),
                provider_mgr=manager,
            )
        self.assertIn("用法: .llm image switch", reply.await_args.args[1])

    async def test_help_lists_image(self) -> None:
        manager = ProviderManager(AsyncMock())
        with patch("sophos.commands._reply", new_callable=AsyncMock) as reply:
            await handle_llm_command(".llm nope", context=_command_context(), provider_mgr=manager)
        self.assertIn(".llm image", reply.await_args.args[1])


class PersistMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_persist_merges_timeout(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {
            "name": "generate_image",
            "display_name": "图像生成",
            "description": "desc",
            "provider_alias": "openai",
            "model_name": "gpt-image-1",
            "api_type": "openai",
            "send_as": "image_url",
            "config": {"request_timeout": 600},
            "enabled": True,
        }
        with patch("sophos.tools.image_gen._after_image_config_change"):
            saved = await persist_image_config(pool, {"request_timeout": 90})
        self.assertEqual(saved["provider_alias"], "openai")
        self.assertEqual(saved["request_timeout"], 90)
        self.assertEqual(saved["enabled"], True)


if __name__ == "__main__":
    unittest.main()
