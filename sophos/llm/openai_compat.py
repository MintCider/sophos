"""OpenAI 兼容 API 的 LLM Provider 实现。

覆盖所有兼容 OpenAI Chat Completions API 的服务：
OpenAI、DeepSeek、Gemini（via compatible endpoint）、vLLM、Ollama 等。

使用 aiohttp（已有依赖）直接调用，不引入 openai SDK。
"""

import json
import logging
from typing import Any

import aiohttp

from sophos.llm.provider import ChatResponse, LLMProvider, Message, UsageInfo

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容 API 的 Provider。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        default_temperature: float = 0.7,
        default_max_tokens: int = 4096,
        request_timeout: int = 60,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._request_timeout = request_timeout
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """懒创建 aiohttp session。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
        return self._session

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """调用 OpenAI 兼容的 chat/completions 端点。"""
        url = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._default_max_tokens,
        }
        if tools:
            payload["tools"] = tools

        session = self._get_session()
        logger.debug("LLM request: model=%s, messages=%d, tools=%s", self._model, len(messages), len(tools) if tools else 0)

        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        async with session.post(url, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM API error {resp.status}: {body}")
            data = await resp.json()

        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ChatResponse:
        """将 OpenAI API 响应解析为 ChatResponse。

        直接透传 API 返回的 message 结构，不丢弃 provider 特有字段
        （如 Gemini 的 thought_signature）。仅做最小化的标准字段校验。
        """
        choice = data["choices"][0]
        raw_msg: dict[str, Any] = choice["message"]

        # 直接使用 API 返回的 message，保留所有字段
        message: Message = {"role": raw_msg.get("role", "assistant")}  # type: ignore[typeddict-item]
        # 标准字段
        if raw_msg.get("content") is not None:
            message["content"] = raw_msg["content"]
        if raw_msg.get("tool_calls"):
            # 保留 tool_calls 原始结构（含 provider 特有字段如 thought_signature）
            message["tool_calls"] = raw_msg["tool_calls"]
        # 透传 provider 特有的顶层字段
        for key, value in raw_msg.items():
            if key not in ("role", "content", "tool_calls"):
                message[key] = value  # type: ignore[literal-required]

        # 用量信息
        usage: UsageInfo | None = None
        if "usage" in data:
            usage = {
                "prompt_tokens": data["usage"].get("prompt_tokens", 0),
                "completion_tokens": data["usage"].get("completion_tokens", 0),
                "total_tokens": data["usage"].get("total_tokens", 0),
            }

        # finish_reason 标准化
        finish = choice.get("finish_reason", "stop")
        if finish == "tool_calls":
            pass  # 已经是标准值
        elif finish in ("function_call",):
            finish = "tool_calls"  # 旧版 API 兼容

        return {"message": message, "usage": usage, "finish_reason": finish}

    async def close(self) -> None:
        """关闭 HTTP session。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
