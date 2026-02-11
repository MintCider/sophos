"""ToolRegistry — 工具的注册中心。"""

import logging
from typing import Any

from sophos.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """管理所有已注册的工具。

    用法:
        registry = ToolRegistry()
        registry.register(SendMessageTool())
        registry.register(WebSearchTool())

        # 代码直接调用
        result = await registry.execute("send_message", params, context)

        # 导出给 LLM
        schemas = registry.get_function_schemas()
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。重名会覆盖并警告。"""
        if tool.name in self._tools:
            logger.warning("Tool '%s' is being overridden", tool.name)
        self._tools[tool.name] = tool
        logger.info("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        """按名称查找工具。"""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """列出所有已注册工具的名称。"""
        return list(self._tools.keys())

    async def execute(self, name: str, params: dict[str, Any], context: dict[str, Any]) -> Any:
        """按名称调用工具。

        这是代码直接调用工具的入口，也是 LLM tool calling 的执行入口。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        return await tool.execute(params, context)

    def get_function_schemas(self, *, category: str | None = None) -> list[dict[str, Any]]:
        """导出工具的 schema，用于传给 LLM 的 tools 字段。

        Args:
            category: 按类别过滤，"input" 或 "output"。None 表示全部。
        """
        tools = self._tools.values()
        if category is not None:
            tools = [t for t in tools if t.category == category]
        return [tool.to_function_schema() for tool in tools]
