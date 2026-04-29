"""LLM 集成层。

提供统一的 LLM 调用接口、上下文构建和 tool calling 循环。
"""

from sophos.llm.context import build_chat_context
from sophos.llm.openai_compat import OpenAICompatProvider
from sophos.llm.provider import ChatResponse, LLMProvider, Message, ToolCall
from sophos.llm.tool_loop import run_tool_loop

__all__ = [
    "ChatResponse",
    "LLMProvider",
    "Message",
    "OpenAICompatProvider",
    "ToolCall",
    "build_chat_context",
    "run_tool_loop",
]
