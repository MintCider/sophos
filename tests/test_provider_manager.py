import json
import unittest
from unittest.mock import AsyncMock

from sophos.llm.openai_compat import OpenAICompatProvider
from sophos.llm.provider import normalize_provider_policy
from sophos.llm.provider_manager import ProviderManager

DEEPSEEK_POLICY = {
    "openai": {
        "allowed_body_parameters": [
            "model",
            "messages",
            "max_tokens",
            "tools",
            "stream",
            "thinking",
        ],
        "accumulated_message_fields": ["reasoning_content"],
        "requires_assistant_content_for_tool_calls": True,
        "strict_optional_mode": "required",
    }
}


class ProviderPolicyValidationTests(unittest.TestCase):
    def test_normalization_validates_known_fields_and_preserves_future_sections(self) -> None:
        policy = {
            **DEEPSEEK_POLICY,
            "future_api": {"arbitrary": True},
        }

        normalized = normalize_provider_policy(policy)

        self.assertEqual(normalized["openai"]["strict_optional_mode"], "required")
        self.assertEqual(normalized["future_api"], {"arbitrary": True})

    def test_allowed_parameters_must_keep_protocol_required_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "messages"):
            normalize_provider_policy(
                {"openai": {"allowed_body_parameters": ["model", "tools"]}}
            )


class CollectorProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_switch_uses_an_independent_slot_and_request_policy(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {
            "id": 7,
            "alias": "DeepSeekBeta",
            "base_urls": {"openai": "https://api.deepseek.com/beta"},
            "api_key": "test-key",
            "stream": True,
            "request_policy": DEEPSEEK_POLICY,
        }
        pool.fetchval.return_value = 45
        manager = ProviderManager(pool)

        result = await manager.switch_collector(
            "DeepSeekBeta",
            "deepseek-v4-flash",
            api_type="openai",
        )

        self.assertEqual(result, "已切换 collector 到 DeepSeekBeta / deepseek-v4-flash")
        collector = manager.get_collector_provider()
        self.assertIsInstance(collector, OpenAICompatProvider)
        self.assertEqual(collector._request_timeout, 45)
        self.assertNotIn("tool_choice", collector._request_policy.allowed_body_parameters or ())
        self.assertEqual(manager.current_info()["collector_alias"], "DeepSeekBeta")
        with self.assertRaisesRegex(RuntimeError, "No LLM provider"):
            manager.get_provider()
        upsert_sql = pool.execute.await_args.args[0]
        self.assertIn("VALUES ('collector'", upsert_sql)

    async def test_add_provider_persists_normalized_request_policy(self) -> None:
        pool = AsyncMock()
        manager = ProviderManager(pool)

        await manager.add_provider(
            "DeepSeekBeta",
            {"openai": "https://api.deepseek.com/beta"},
            "test-key",
            request_policy=DEEPSEEK_POLICY,
        )

        persisted = json.loads(pool.execute.await_args.args[5])
        self.assertEqual(persisted, DEEPSEEK_POLICY)


if __name__ == "__main__":
    unittest.main()
