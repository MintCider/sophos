"""LLM Provider 抽象基类与消息类型定义。

消息格式对齐 OpenAI Chat Completions API，作为内部标准。
TypedDict 提供类型提示但零运行时开销，可直接 json.dumps。
"""

from abc import ABC, abstractmethod
from typing import Any, TypedDict


# ── 消息类型 ─────────────────────────────────────────────


class ToolCallFunction(TypedDict):
    """tool_calls 中的 function 字段。"""

    name: str
    arguments: str  # JSON string


class ToolCall(TypedDict):
    """assistant 消息中的一个 tool call。"""

    id: str
    type: str  # "function"
    function: ToolCallFunction


class Message(TypedDict, total=False):
    """OpenAI 格式的消息。

    total=False 表示所有字段都是可选的，因为不同 role 的消息包含不同字段：
    - system:    role + content
    - user:      role + content
    - assistant: role + content（可能为 None）+ tool_calls（可选）
    - tool:      role + content + tool_call_id + name
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None
    tool_calls: list[ToolCall]
    tool_call_id: str
    name: str


class UsageInfo(TypedDict, total=False):
    """Token 用量信息。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(TypedDict):
    """LLM 调用的返回值。"""

    message: Message
    usage: UsageInfo | None
    finish_reason: str  # "stop" | "tool_calls" | "length"


# ── Provider 抽象基类 ────────────────────────────────────


class LLMProvider(ABC):
    """LLM 调用的统一接口。

    所有 provider 实现（OpenAI 兼容、Anthropic 等）都继承此类。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """发送消息给 LLM 并获取回复。

        Args:
            messages:    对话消息列表（OpenAI 格式）
            tools:       可用工具的 schema 列表（OpenAI function calling 格式），
                         为 None 时不启用 tool calling
            temperature: 生成温度，为 None 时使用 provider 默认值
            max_tokens:  最大生成 token 数，为 None 时使用 provider 默认值

        Returns:
            ChatResponse，包含 assistant 消息、用量信息和结束原因
        """
        ...

    async def close(self) -> None:
        """释放资源（HTTP session 等）。默认无操作。"""
