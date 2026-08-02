import unittest
from typing import Any

from sophos.tools.base import Tool
from sophos.tools.registry import ToolRegistry


class _RecordingTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "record"

    @property
    def description(self) -> str:
        return "record a call"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        self.calls += 1
        return {"ok": True}


class _DirectOnlyTool(_RecordingTool):
    @property
    def conversation_kinds(self) -> frozenset[str] | None:
        return frozenset({"direct"})


class ToolRegistryExecutionFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_tool_is_rejected_at_execution(self) -> None:
        registry = ToolRegistry()
        tool = _RecordingTool()
        registry.register(tool)
        registry.set_disabled(tool.name, True)

        with self.assertRaisesRegex(PermissionError, "disabled"):
            await registry.execute(tool.name, {}, {})
        self.assertEqual(tool.calls, 0)

    async def test_context_filter_is_rechecked_at_execution(self) -> None:
        registry = ToolRegistry()
        tool = _RecordingTool()
        registry.register(tool)

        with self.assertRaisesRegex(PermissionError, "not allowed"):
            await registry.execute(tool.name, {}, {}, allowed_tools=set())
        self.assertEqual(tool.calls, 0)

    async def test_allowed_enabled_tool_executes(self) -> None:
        registry = ToolRegistry()
        tool = _RecordingTool()
        registry.register(tool)

        result = await registry.execute(tool.name, {}, {}, allowed_tools={tool.name})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(tool.calls, 1)

    async def test_conversation_kind_is_filtered_in_schema_and_execution(self) -> None:
        registry = ToolRegistry()
        tool = _DirectOnlyTool()
        registry.register(tool)

        self.assertEqual(registry.get_function_schemas(conversation_kind="group"), [])
        self.assertEqual(len(registry.get_function_schemas(conversation_kind="direct")), 1)
        with self.assertRaisesRegex(PermissionError, "conversation kind"):
            await registry.execute(tool.name, {}, {"conversation_kind": "channel"})


if __name__ == "__main__":
    unittest.main()
