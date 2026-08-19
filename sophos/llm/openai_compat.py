"""OpenAI 兼容 API 的 LLM Provider 实现。

覆盖所有兼容 OpenAI Chat Completions API 的服务：
OpenAI、DeepSeek、Gemini（via compatible endpoint）、vLLM、Ollama 等。

使用 aiohttp（已有依赖）直接调用，不引入 openai SDK。
"""

import json
import logging
from typing import Any

import aiohttp

from sophos.llm.provider import (
    ChatResponse,
    FirstTokenCallback,
    LLMProvider,
    Message,
    OpenAIRequestPolicy,
    ProviderRequestOptions,
    UsageInfo,
)
from sophos.llm.schema import compile_openai_strict_tools

logger = logging.getLogger(__name__)

# 自定义 TRACE 级别（与 main.py 一致）
TRACE = 5


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容 API 的 Provider。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        default_temperature: float | None = None,
        default_max_tokens: int = 4096,
        request_timeout: int = 60,
        stream: bool = True,
        extra_body: dict[str, Any] | None = None,
        request_policy: OpenAIRequestPolicy | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._request_timeout = request_timeout
        self._stream = stream
        self._extra_body = extra_body or {}
        self._request_policy = request_policy or OpenAIRequestPolicy()
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

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        request_options: ProviderRequestOptions | None,
    ) -> dict[str, Any]:
        options = request_options or ProviderRequestOptions()
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self._default_max_tokens,
        }
        temp = temperature if temperature is not None else self._default_temperature
        if temp is not None:
            payload["temperature"] = temp
        if tools and options.tool_choice != "none":
            payload["tools"] = (
                compile_openai_strict_tools(
                    tools,
                    optional_mode=self._request_policy.strict_optional_mode,
                )
                if options.strict_tools
                else tools
            )
            payload["tool_choice"] = options.tool_choice
        elif options.tool_choice == "none":
            payload["tool_choice"] = "none"

        if options.cache and options.cache.enabled and options.cache.key:
            payload["prompt_cache_key"] = options.cache.key

        if self._extra_body:
            payload.update(self._extra_body)
        return self._request_policy.filter_body(payload)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_first_token: FirstTokenCallback | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ChatResponse:
        """调用 OpenAI 兼容的 chat/completions 端点。

        stream=True 时使用 SSE streaming 避免长时间等待导致的超时，
        内部累积所有 delta 后返回完整 ChatResponse，对上层透明。
        stream=False 时使用传统的一次性请求（兼容不支持流式的 provider）。
        """
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(messages, tools, temperature, max_tokens, request_options)

        session = self._get_session()
        logger.log(TRACE, "LLM payload:\n%s", json.dumps(payload, ensure_ascii=False, indent=2))

        use_stream = self._stream and self._request_policy.allows("stream")
        if use_stream:
            payload["stream"] = True
            logger.debug(
                "LLM request (stream): model=%s, messages=%d, tools=%s",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            # sock_read = request_timeout：TTFT / 两个 chunk 之间的最大等待
            # total = sock_read × 5：宽松总超时，防止无限挂起
            timeout = aiohttp.ClientTimeout(
                total=self._request_timeout * 5,
                sock_read=float(self._request_timeout),
            )
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"LLM API error {resp.status}: {body}")
                return await self._consume_stream(resp, on_first_token=on_first_token)
        else:
            logger.debug(
                "LLM request: model=%s, messages=%d, tools=%s",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            timeout = aiohttp.ClientTimeout(total=self._request_timeout)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"LLM API error {resp.status}: {body}")
                data = await resp.json()
            return self._parse_response(data)

    async def _consume_stream(
        self,
        resp: aiohttp.ClientResponse,
        *,
        on_first_token: FirstTokenCallback | None = None,
    ) -> ChatResponse:
        """读取 SSE stream，累积 delta 为完整的 ChatResponse。"""
        role = "assistant"
        content_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}  # index → accumulated tool call
        extra_fields: dict[str, Any] = {}  # provider 特有字段（如 Gemini 的额外字段）
        finish_reason = "stop"
        usage: dict[str, Any] | None = None
        chunk_count = 0
        first_token_seen = False

        while True:
            line_bytes = await resp.content.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                logger.debug("Stream done, received %d chunks", chunk_count)
                break

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning("Malformed SSE chunk: %s", data_str[:200])
                continue

            chunk_count += 1
            choices = chunk.get("choices")
            if not choices:
                if chunk.get("usage"):
                    usage = chunk["usage"]
                continue

            choice = choices[0]
            delta = choice.get("delta", {})

            if "role" in delta:
                role = delta["role"]
            if delta.get("content"):
                content_parts.append(delta["content"])
                if not first_token_seen:
                    first_token_seen = True
                    if on_first_token is not None:
                        await on_first_token()
            # 透传 provider 特有的顶层字段（如 Gemini 的额外字段）
            for key, value in delta.items():
                if key not in ("role", "content", "tool_calls"):
                    if (
                        key in self._request_policy.accumulated_message_fields
                        and isinstance(value, str)
                    ):
                        extra_fields[key] = str(extra_fields.get(key, "")) + value
                    else:
                        extra_fields[key] = value
            if delta.get("tool_calls"):
                for tc_delta in delta["tool_calls"]:
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc_delta.get("id", ""),
                            "type": tc_delta.get("type", "function"),
                            "function": {
                                "name": tc_delta.get("function", {}).get("name", ""),
                                "arguments": "",
                            },
                        }
                    else:
                        if tc_delta.get("id"):
                            tool_calls_map[idx]["id"] = tc_delta["id"]
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tool_calls_map[idx]["function"]["name"] = fn["name"]
                    args_piece = tc_delta.get("function", {}).get("arguments", "")
                    if args_piece:
                        tool_calls_map[idx]["function"]["arguments"] += args_piece
                    # 保留 tool_call 中的 provider 特有字段（如 thought_signature）
                    for key, value in tc_delta.items():
                        if key not in ("index", "id", "type", "function"):
                            tool_calls_map[idx][key] = value

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]

        # 组装最终 message
        message: Message = {"role": role}  # type: ignore[typeddict-item]
        content = "".join(content_parts)
        if content:
            message["content"] = content
        if tool_calls_map:
            message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]
            if self._request_policy.requires_assistant_content_for_tool_calls and "content" not in message:
                message["content"] = ""
        # 透传 provider 特有字段（如 Gemini 的额外字段）
        for key, value in extra_fields.items():
            message[key] = value  # type: ignore[literal-required]

        # finish_reason 标准化
        if finish_reason in ("function_call",):
            finish_reason = "tool_calls"

        usage_info: UsageInfo | None = None
        if usage:
            details = usage.get("prompt_tokens_details") or {}
            usage_info = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cached_input_tokens": details.get("cached_tokens", 0),
                "cache_write_tokens": details.get("cache_write_tokens", 0),
                "raw": usage,
            }

        logger.debug(
            "Stream complete: %d chunks, content_len=%d, tool_calls=%d, finish=%s",
            chunk_count,
            len(content),
            len(tool_calls_map),
            finish_reason,
        )
        result: ChatResponse = {
            "message": message,
            "usage": usage_info,
            "finish_reason": finish_reason,
            "provider": "openai_compat",
        }
        logger.log(TRACE, "LLM response:\n%s", json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return result

    def _parse_response(self, data: dict[str, Any]) -> ChatResponse:
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
            if self._request_policy.requires_assistant_content_for_tool_calls:
                message["content"] = raw_msg.get("content") or ""
        # 透传 provider 特有的顶层字段
        for key, value in raw_msg.items():
            if key not in ("role", "content", "tool_calls"):
                message[key] = value  # type: ignore[literal-required]

        # 用量信息
        usage: UsageInfo | None = None
        if "usage" in data:
            raw_usage = data["usage"]
            details = raw_usage.get("prompt_tokens_details") or {}
            usage = {
                "prompt_tokens": raw_usage.get("prompt_tokens", 0),
                "completion_tokens": raw_usage.get("completion_tokens", 0),
                "total_tokens": raw_usage.get("total_tokens", 0),
                "cached_input_tokens": details.get("cached_tokens", 0),
                "cache_write_tokens": details.get("cache_write_tokens", 0),
                "raw": raw_usage,
            }

        # finish_reason 标准化
        finish = choice.get("finish_reason", "stop")
        if finish == "tool_calls":
            pass  # 已经是标准值
        elif finish in ("function_call",):
            finish = "tool_calls"  # 旧版 API 兼容

        native_metadata = {
            key: data[key]
            for key in ("id", "model", "created", "system_fingerprint", "service_tier")
            if key in data
        }
        result: ChatResponse = {
            "message": message,
            "usage": usage,
            "finish_reason": finish,
            "provider": "openai_compat",
            "native_metadata": native_metadata,
        }
        logger.log(TRACE, "LLM response:\n%s", json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return result

    async def close(self) -> None:
        """关闭 HTTP session。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
