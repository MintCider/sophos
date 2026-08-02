"""LLM Provider 抽象基类与消息类型定义。

消息格式对齐 OpenAI Chat Completions API，作为内部标准。
TypedDict 提供类型提示但零运行时开销，可直接 json.dumps。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

FirstTokenCallback = Callable[[], Awaitable[None]]


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
    cached_input_tokens: int
    cache_write_tokens: int
    raw: dict[str, Any]


class ChatResponse(TypedDict, total=False):
    """LLM 调用的返回值。"""

    message: Message
    usage: UsageInfo | None
    finish_reason: str  # "stop" | "tool_calls" | "length"
    provider: str
    native_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CachePlan:
    """Provider-neutral prompt cache intent.

    Provider adapters compile this intent to native cache controls where the API
    exposes them. ``key`` describes a stable routing namespace; it is not a cache
    object identifier and providers that do not expose such a key may ignore it.
    """

    enabled: bool = True
    key: str | None = None
    preferred_ttl_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderRequestOptions:
    """Semantic request controls compiled independently by each provider."""

    tool_choice: Literal["auto", "required", "none"] = "auto"
    strict_tools: bool = True
    cache: CachePlan | None = None


@dataclass(frozen=True, slots=True)
class OpenAIRequestPolicy:
    """Provider-declared compatibility rules for an OpenAI-style endpoint."""

    allowed_body_parameters: frozenset[str] | None = None
    accumulated_message_fields: frozenset[str] = frozenset()
    requires_assistant_content_for_tool_calls: bool = False
    strict_optional_mode: Literal["nullable", "required"] = "nullable"

    def allows(self, parameter: str) -> bool:
        return self.allowed_body_parameters is None or parameter in self.allowed_body_parameters

    def filter_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.allowed_body_parameters is None:
            return payload
        return {key: value for key, value in payload.items() if key in self.allowed_body_parameters}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "accumulated_message_fields": sorted(self.accumulated_message_fields),
            "requires_assistant_content_for_tool_calls": (
                self.requires_assistant_content_for_tool_calls
            ),
            "strict_optional_mode": self.strict_optional_mode,
        }
        if self.allowed_body_parameters is not None:
            result["allowed_body_parameters"] = sorted(self.allowed_body_parameters)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> OpenAIRequestPolicy:
        value = value or {}
        allowed = value.get("allowed_body_parameters")
        accumulated = value.get("accumulated_message_fields")
        return cls(
            allowed_body_parameters=(
                frozenset(str(item) for item in allowed)
                if isinstance(allowed, list)
                else None
            ),
            accumulated_message_fields=frozenset(
                str(item) for item in accumulated
            )
            if isinstance(accumulated, list)
            else frozenset(),
            requires_assistant_content_for_tool_calls=bool(
                value.get("requires_assistant_content_for_tool_calls", False)
            ),
            strict_optional_mode=(
                "required"
                if value.get("strict_optional_mode") == "required"
                else "nullable"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Extensible provider policy, grouped by native API surface."""

    openai: OpenAIRequestPolicy = OpenAIRequestPolicy()

    def to_dict(self) -> dict[str, Any]:
        return {"openai": self.openai.to_dict()}

    @classmethod
    def from_value(cls, value: Any) -> ProviderPolicy:
        if isinstance(value, str):
            import json

            value = json.loads(value)
        if not isinstance(value, dict):
            value = {}
        openai = value.get("openai")
        return cls(
            openai=OpenAIRequestPolicy.from_dict(
                openai if isinstance(openai, dict) else None
            )
        )


def normalize_provider_policy(value: Any) -> dict[str, Any]:
    """Validate known policy fields while preserving future API-surface sections."""
    if value is None:
        return {}
    if isinstance(value, str):
        import json

        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("request_policy must be a JSON object")
    normalized = deepcopy(value)
    openai = normalized.get("openai")
    if openai is None:
        return normalized
    if not isinstance(openai, dict):
        raise ValueError("request_policy.openai must be a JSON object")

    allowed = openai.get("allowed_body_parameters")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(
            isinstance(item, str) and item.strip() for item in allowed
        ):
            raise ValueError("allowed_body_parameters must be an array of non-empty strings")
        normalized_allowed = list(dict.fromkeys(item.strip() for item in allowed))
        missing = sorted({"model", "messages"} - set(normalized_allowed))
        if missing:
            raise ValueError(
                f"allowed_body_parameters must include: {', '.join(missing)}"
            )
        openai["allowed_body_parameters"] = normalized_allowed

    accumulated = openai.get("accumulated_message_fields")
    if accumulated is not None:
        if not isinstance(accumulated, list) or not all(
            isinstance(item, str) and item.strip() for item in accumulated
        ):
            raise ValueError("accumulated_message_fields must be an array of non-empty strings")
        openai["accumulated_message_fields"] = list(
            dict.fromkeys(item.strip() for item in accumulated)
        )

    required_content = openai.get("requires_assistant_content_for_tool_calls")
    if required_content is not None and not isinstance(required_content, bool):
        raise ValueError("requires_assistant_content_for_tool_calls must be boolean")

    optional_mode = openai.get("strict_optional_mode")
    if optional_mode is not None and optional_mode not in {"nullable", "required"}:
        raise ValueError("strict_optional_mode must be nullable or required")
    return normalized


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
        on_first_token: FirstTokenCallback | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ChatResponse:
        """发送消息给 LLM 并获取回复。

        Args:
            messages:    对话消息列表（OpenAI 格式）
            tools:       可用工具的 schema 列表（OpenAI function calling 格式），
                         为 None 时不启用 tool calling
            temperature: 生成温度，为 None 时使用 provider 默认值
            max_tokens:  最大生成 token 数，为 None 时使用 provider 默认值
            on_first_token: 流式响应出现首个有效内容 token 时调用一次
            request_options: provider-neutral tool selection and cache intent

        Returns:
            ChatResponse，包含 assistant 消息、用量信息和结束原因
        """
        ...

    async def close(self) -> None:
        """释放资源（HTTP session 等）。默认无操作。"""
        return None
