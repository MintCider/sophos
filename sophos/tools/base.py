"""Tool 抽象基类 — 所有工具的统一接口。"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """一个可被 LLM 或代码调用的工具。

    每个 Tool 子类需要定义：
    - name:        工具名（唯一标识，LLM 用这个名字来调用）
    - description: 一句话描述（给 LLM 看的，帮它决定什么时候用这个工具）
    - parameters:  参数的 JSON Schema（告诉 LLM 要传什么参数、什么类型）
    - execute():   实际执行逻辑

    可选覆盖：
    - category:    工具类别，"input"（查询）或 "output"（操作），默认 "input"
    - scope:       适用场景，"all" / "group" / "private"，默认 "all"
    - group:       工具分组，用于 WebUI 展示分类，默认根据 category 推导
    """

    @property
    def category(self) -> str:
        return "input"

    @property
    def scope(self) -> str:
        return "all"

    @property
    def group(self) -> str:
        """工具分组：WebUI 按此字段归类展示。

        内置工具应覆盖此属性返回具体分组名，如 "messaging"、"memory" 等。
        自定义工具由 DB 的 tool_type 字段决定。
        """
        return "other"

    @property
    def required_capabilities(self) -> frozenset[str]:
        """Adapter capabilities required to expose and execute this tool."""
        return frozenset()

    @property
    def is_builtin(self) -> bool:
        """是否为内置工具（不可删除）。"""
        return True

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名，如 "send_message"。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，给 LLM 看的。"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """参数的 JSON Schema。

        例:
            {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "message":  {"type": "string",  "description": "消息内容"},
                },
                "required": ["message"],
            }
        """
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        """执行工具。

        Args:
            params:  调用参数（已通过 JSON Schema 校验）
            context: 运行时上下文（WS 连接、bot 信息等，由调用方注入）

        Returns:
            工具执行结果，会被序列化后返回给 LLM 或调用方。
        """
        ...

    def to_function_schema(self) -> dict[str, Any]:
        """导出为 LLM function calling 的 schema。

        输出格式兼容 OpenAI API 的 tools 字段，
        同时也兼容大多数中转服务和本地部署方案。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
