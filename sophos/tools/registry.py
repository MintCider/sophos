"""ToolRegistry — 工具的注册中心。"""

import logging
from typing import Any

from sophos.tools.base import Tool

logger = logging.getLogger(__name__)

UNSAFE_DISABLE_TOOLS: frozenset[str] = frozenset({"send_msg"})


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
        self._disabled_tools: set[str] = set()

    def register(self, tool: Tool) -> None:
        """注册一个工具。重名会覆盖并警告。"""
        if tool.name in self._tools:
            logger.warning("Tool '%s' is being overridden", tool.name)
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        """按名称查找工具。"""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """列出所有已注册工具的名称。"""
        return list(self._tools.keys())

    def set_disabled(self, name: str, disabled: bool) -> bool:
        """设置工具禁用状态。返回是否成功（send_msg 不可禁用）。"""
        if disabled and name in UNSAFE_DISABLE_TOOLS:
            return False
        if disabled:
            self._disabled_tools.add(name)
        else:
            self._disabled_tools.discard(name)
        return True

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled_tools

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        context: dict[str, Any],
        *,
        allowed_tools: set[str] | None = None,
    ) -> Any:
        """按名称调用工具。

        这是代码直接调用工具的入口，也是 LLM tool calling 的执行入口。
        调用前自动校验 required 参数是否齐全。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        if name in self._disabled_tools:
            raise PermissionError(f"Tool is disabled: {name}")
        if allowed_tools is not None and name not in allowed_tools:
            raise PermissionError(f"Tool is not allowed in this context: {name}")

        # 轻量参数校验：检查 required 字段是否齐全
        schema = tool.parameters
        required = schema.get("required", [])
        missing = [r for r in required if r not in params]
        if missing:
            return {"error": f"缺少必填参数: {', '.join(missing)}"}

        return await tool.execute(params, context)

    def get_function_schemas(
        self,
        *,
        category: str | None = None,
        scope: str | None = None,
        description_overrides: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """导出工具的 schema，用于传给 LLM 的 tools 字段。

        Args:
            category: 按类别过滤，"input" 或 "output"。None 表示全部。
            scope: 按适用场景过滤，"group" 或 "private"。
                   None 表示全部；指定后会包含该 scope 和 "all" 的工具。
            description_overrides: 工具描述覆盖 {tool_name: custom_description}。
        """
        tools: list[Tool] = [t for t in self._tools.values() if t.name not in self._disabled_tools]
        if category is not None:
            tools = [t for t in tools if t.category == category]
        if scope is not None:
            tools = [t for t in tools if t.scope in (scope, "all")]

        schemas = []
        for tool in tools:
            schema = tool.to_function_schema()
            if description_overrides and tool.name in description_overrides:
                schema = {
                    **schema,
                    "function": {
                        **schema["function"],
                        "description": description_overrides[tool.name],
                    },
                }
            schemas.append(schema)
        return schemas

    def get_tools_info(self, *, description_overrides: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """导出所有工具的元信息，供 WebUI 展示。

        Args:
            description_overrides: 从 DB 加载的描述覆盖 {tool_name: custom_description}。

        Returns:
            工具信息列表，每个元素包含：
            name, description, default_description, category, scope, group,
            is_builtin, is_custom, enabled, parameters
        """
        result = []
        for tool in self._tools.values():
            default_desc = tool.description
            custom_desc = description_overrides.get(tool.name) if description_overrides else None
            effective_desc = custom_desc if custom_desc else default_desc

            result.append(
                {
                    "name": tool.name,
                    "description": effective_desc,
                    "default_description": default_desc,
                    "has_custom_description": custom_desc is not None,
                    "category": tool.category,
                    "scope": tool.scope,
                    "group": tool.group,
                    "is_builtin": tool.is_builtin,
                    "is_custom": not tool.is_builtin,
                    "enabled": tool.name not in self._disabled_tools,
                    "parameters": tool.parameters,
                    "can_disable": tool.name not in UNSAFE_DISABLE_TOOLS,
                }
            )
        return result
