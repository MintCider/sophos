import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sophos import prompts
from sophos.api.routes.prompts import routes


def _version(version_id: int, content: str) -> prompts.PromptVersion:
    return prompts.PromptVersion(id=version_id, content=content, note="", created_at=datetime.now(UTC))


class PromptRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._custom = patch.dict(prompts._custom, clear=True)
        self._current = patch.dict(prompts._current, clear=True)
        self._custom.start()
        self._current.start()

    def tearDown(self) -> None:
        self._custom.stop()
        self._current.stop()

    def _add_custom(self, key: str, content: str) -> None:
        prompts._custom[key] = prompts._custom_spec(key, key, "")
        prompts._current[key] = _version(len(prompts._current) + 1, content)

    def test_builtin_default_is_used_without_versions(self) -> None:
        self.assertEqual(prompts.get("vision.describe"), prompts.get_spec("vision.describe").default)

    def test_render_replaces_double_brace_variables_only(self) -> None:
        prompts._current["persona.system"] = _version(1, '你是 {{nickname}}\n{"json": {single}}')
        rendered = prompts.render("persona.system", {"nickname": "Sophos"})
        self.assertEqual(rendered.text, '你是 Sophos\n{"json": {single}}')
        self.assertEqual(rendered.versions, {"persona.system": 1})

    def test_render_expands_includes_and_records_versions(self) -> None:
        self._add_custom("persona.core", "猫娘 {{nickname}}")
        prompts._current["persona.system"] = _version(9, "人设：{{prompt.persona.core}}")
        rendered = prompts.render("persona.system", {"nickname": "Sophos"})
        self.assertEqual(rendered.text, "人设：猫娘 Sophos")
        self.assertEqual(set(rendered.versions), {"persona.system", "persona.core"})

    def test_missing_variable_is_empty_at_runtime_and_marked_in_preview(self) -> None:
        prompts._current["persona.system"] = _version(1, "时间 {{current_time}}")
        self.assertEqual(prompts.render("persona.system").text, "时间 ")
        preview = prompts.render("persona.system", mark_missing=True)
        self.assertEqual(preview.text, "时间 ‹current_time›")
        self.assertEqual(preview.missing, {"current_time"})

    def test_validate_rejects_unknown_variable(self) -> None:
        with self.assertRaisesRegex(prompts.PromptError, "不支持的变量：prev_description"):
            prompts.validate("persona.system", "{{prev_description}}")

    def test_validate_checks_variables_of_included_prompts(self) -> None:
        self._add_custom("snippet", "{{current_time}}")
        prompts.validate("persona.system", "{{prompt.snippet}}")
        with self.assertRaisesRegex(prompts.PromptError, "不支持的变量：current_time"):
            prompts.validate("vision.describe", "{{prompt.snippet}}")

    def test_validate_rejects_cycles_and_unknown_includes(self) -> None:
        self._add_custom("a", "{{prompt.b}}")
        self._add_custom("b", "x")
        with self.assertRaisesRegex(prompts.PromptError, "循环引用"):
            prompts.validate("b", "{{prompt.a}}")
        with self.assertRaisesRegex(prompts.PromptError, "不存在"):
            prompts.validate("persona.system", "{{prompt.nope}}")


class PromptSaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._current = patch.dict(prompts._current, clear=True)
        self._current.start()

    def tearDown(self) -> None:
        self._current.stop()

    def _pool(self, latest: int | None) -> MagicMock:
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetchval = AsyncMock(return_value=latest)
        conn.fetchrow = AsyncMock(
            return_value={"id": (latest or 0) + 1, "content": "new", "note": "", "created_at": datetime.now(UTC)}
        )
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        return pool

    async def test_save_appends_version_and_updates_current(self) -> None:
        with patch.object(prompts, "_pool", self._pool(latest=4)):
            version = await prompts.save("vision.describe", "new", base_version_id=4)
        self.assertEqual(version.id, 5)
        self.assertEqual(prompts.get("vision.describe"), "new")

    async def test_save_rejects_stale_base_version(self) -> None:
        with (
            patch.object(prompts, "_pool", self._pool(latest=5)),
            self.assertRaises(prompts.PromptConflictError),
        ):
            await prompts.save("vision.describe", "new", base_version_id=4)

    async def test_locked_prompt_requires_unlock(self) -> None:
        with patch.object(prompts, "_pool", self._pool(latest=None)):
            with self.assertRaises(prompts.PromptLockedError):
                await prompts.save("trigger.eval_format", "x", base_version_id=None)
            version = await prompts.save("trigger.eval_format", "new", base_version_id=None, unlock=True)
        self.assertEqual(version.id, 1)

    def test_prompt_api_routes_are_registered(self) -> None:
        registered = {(route.method, route.path) for route in routes}
        self.assertIn(("PUT", "/api/prompts/{key}"), registered)
        self.assertIn(("GET", "/api/prompts/{key}/versions"), registered)
        self.assertIn(("POST", "/api/prompts/{key}/preview"), registered)


if __name__ == "__main__":
    unittest.main()
