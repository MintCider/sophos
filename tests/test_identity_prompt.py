import unittest
from unittest.mock import AsyncMock, patch

from sophos.identity_prompt import build_identity_block, build_identity_variables
from sophos.prompt_template import render_system_prompt


class IdentityPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_internal_ids_and_database_roles(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = [{"role": "member"}]
        with patch(
            "sophos.identity_prompt.permission.list_masters",
            AsyncMock(return_value=[{"user_id": 7, "display_name": "Owner"}]),
        ):
            block = await build_identity_block(
                pool,
                current_user_id=11,
                self_user_id=3,
                conversation_id=19,
                conversation_kind="channel",
            )

        self.assertIn("你的统一用户ID：3", block)
        self.assertIn("user_id=11", block)
        self.assertIn("conversation_id=19", block)
        self.assertIn("user_id=7, display_name=Owner", block)
        self.assertIn("不要使用 QQ 号或其他平台原始 ID", block)

    async def test_exposes_granular_prompt_variables(self) -> None:
        pool = AsyncMock()
        pool.fetch.return_value = [{"role": "master"}]
        with patch(
            "sophos.identity_prompt.permission.list_masters",
            AsyncMock(return_value=[{"user_id": 11, "display_name": "Owner"}]),
        ):
            values = await build_identity_variables(
                pool,
                current_user_id=11,
                self_user_id=3,
                conversation_id=19,
                conversation_kind="direct",
            )

        self.assertEqual(values["current_user_roles"], "master")
        self.assertEqual(values["master_users"], "- user_id=11, display_name=Owner")
        self.assertIn("Master（主人）用户", values["identity_context"])


class PromptTemplateTests(unittest.TestCase):
    def test_runtime_placeholder_controls_placement(self) -> None:
        rendered = render_system_prompt(
            "persona\n{runtime_context}\njson example: {\"ok\": true}",
            {"runtime_context": "runtime", "nickname": "Sophos"},
        )
        self.assertEqual(rendered, 'persona\nruntime\njson example: {"ok": true}')

    def test_granular_placeholders_disable_legacy_append(self) -> None:
        rendered = render_system_prompt(
            "主人：\n{master_users}",
            {"master_users": "- user_id=7", "runtime_context": "runtime"},
        )
        self.assertEqual(rendered, "主人：\n- user_id=7")

    def test_legacy_prompt_receives_runtime_context(self) -> None:
        rendered = render_system_prompt(
            "你是 {nickname}",
            {"nickname": "Sophos", "runtime_context": "runtime"},
        )
        self.assertEqual(rendered, "你是 Sophos\n\n---\nruntime")


if __name__ == "__main__":
    unittest.main()
