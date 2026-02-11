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
    """

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
