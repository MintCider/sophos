import unittest
from unittest.mock import AsyncMock, patch

from sophos.identity_prompt import build_identity_block


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


if __name__ == "__main__":
    unittest.main()
