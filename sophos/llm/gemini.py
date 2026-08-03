"""Gemini 原生 API 的 LLM Provider 实现。

使用 /v1beta/models/{model}:generateContent 端点，
在 API 边界做 OpenAI ↔ Gemini 格式双向转换。
内部消息格式统一为 OpenAI Chat Completions 格式。
"""

import json
import logging
import uuid
from typing import Any

import aiohttp

from sophos.llm.provider import (
    ChatResponse,
    FirstTokenCallback,
    LLMProvider,
    Message,
    ProviderRequestOptions,
    UsageInfo,
)
from sophos.llm.schema import compile_gemini_schema

logger = logging.getLogger(__name__)

TRACE = 5


class GeminiProvider(LLMProvider):
    """Gemini 原生 API Provider。"""

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
    ) -> None:
        self._root = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens
        self._request_timeout = request_timeout
        self._stream = stream
        self._extra_body = extra_body or {}
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
            )
        return self._session

    # ── OpenAI → Gemini 转换 ──────────────────────────────

    @staticmethod
    def _convert_messages(
        messages: list[Message],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """将 OpenAI messages 转换为 Gemini contents + systemInstruction。

        Returns:
            (system_instruction, contents)
        """
        system_instruction: dict[str, Any] | None = None
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")

            if role == "system":
                system_instruction = {"parts": [{"text": msg.get("content", "")}]}
                continue

            if role == "user":
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": msg.get("content", "")}],
                    }
                )
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                text_sig = msg.get("_gemini_text_signature")
                if msg.get("content"):
                    text_part: dict[str, Any] = {"text": msg["content"]}
                    if text_sig:
                        text_part["thoughtSignature"] = text_sig
                    parts.append(text_part)
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    fc_part: dict[str, Any] = {
                        "functionCall": {"name": fn.get("name", ""), "args": args},
                    }
                    if tc.get("thought_signature"):
                        fc_part["thoughtSignature"] = tc["thought_signature"]
                    parts.append(fc_part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                # Gemini: tool results 用 role=user + functionResponse parts
                # 连续多个 tool message 合并到同一个 content
                try:
                    result = json.loads(msg.get("content", "{}"))
                except json.JSONDecodeError:
                    result = {"result": msg.get("content", "")}
                if not isinstance(result, dict):
                    result = {"result": result}
                fr_part = {
                    "functionResponse": {
                        "name": msg.get("name", ""),
                        "response": result,
                    },
                }
                # 合并到上一个 user content（如果上一个也是 functionResponse）
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and any("functionResponse" in p for p in contents[-1]["parts"])
                ):
                    contents[-1]["parts"].append(fr_part)
                else:
                    contents.append({"role": "user", "parts": [fr_part]})
                continue

        return system_instruction, contents

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 OpenAI tool schemas 转换为 Gemini functionDeclarations。"""
        declarations = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            fn = tool["function"]
            decl: dict[str, Any] = {
                "name": fn["name"],
                "description": fn.get("description", ""),
            }
            if fn.get("parameters"):
                decl["parameters"] = compile_gemini_schema(fn["parameters"])
            declarations.append(decl)
        return [{"functionDeclarations": declarations}] if declarations else []

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        request_options: ProviderRequestOptions | None,
    ) -> dict[str, Any]:
        options = request_options or ProviderRequestOptions()
        system_instruction, contents = self._convert_messages(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        if tools and options.tool_choice != "none":
            payload["tools"] = self._convert_tools(tools)
            mode = "ANY" if options.tool_choice == "required" else "VALIDATED"
            payload["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
        elif options.tool_choice == "none":
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}

        gen_config: dict[str, Any] = {
            "maxOutputTokens": max_tokens if max_tokens is not None else self._default_max_tokens,
        }
        temp = temperature if temperature is not None else self._default_temperature
        if temp is not None:
            gen_config["temperature"] = temp
        gen_config.update(self._extra_body)
        payload["generationConfig"] = gen_config
        return payload

    # ── Gemini → OpenAI 转换 ──────────────────────────────

    @staticmethod
    def _parse_gemini_response(data: dict[str, Any]) -> ChatResponse:
        """将 Gemini API 响应转换为 OpenAI ChatResponse。"""
        candidates = data.get("candidates", [])
        if not candidates:
            # 可能被安全过滤
            return {
                "message": {"role": "assistant", "content": "[Gemini: 响应被过滤]"},
                "usage": None,
                "finish_reason": "stop",
            }

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])

        # 拆分 text 和 functionCall
        # thought parts（thought=true）跳过，不 round-trip；
        # 但保留 text/functionCall 上的 thoughtSignature 用于 round-trip。
        text_parts: list[str] = []
        text_signature: str | None = None
        tool_calls: list[dict[str, Any]] = []
        for part in parts:
            if part.get("thought"):
                continue
            if "text" in part:
                text_parts.append(part["text"])
                if "thoughtSignature" in part:
                    text_signature = part["thoughtSignature"]
            elif "functionCall" in part:
                fc = part["functionCall"]
                tc: dict[str, Any] = {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(
                            fc.get("args", {}),
                            ensure_ascii=False,
                        ),
                    },
                }
                if "thoughtSignature" in part:
                    tc["thought_signature"] = part["thoughtSignature"]
                tool_calls.append(tc)

        message: Message = {"role": "assistant"}  # type: ignore[typeddict-item]
        content = "".join(text_parts)
        if content:
            message["content"] = content
        if tool_calls:
            message["tool_calls"] = tool_calls
        if text_signature:
            message["_gemini_text_signature"] = text_signature  # type: ignore[typeddict-item]

        # finishReason 映射
        raw_reason = candidate.get("finishReason", "STOP")
        reason_map = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "stop",
            "RECITATION": "stop",
            "OTHER": "stop",
        }
        finish_reason = reason_map.get(raw_reason, "stop")
        if tool_calls and finish_reason == "stop":
            finish_reason = "tool_calls"

        # usage
        usage: UsageInfo | None = None
        um = data.get("usageMetadata")
        if um:
            usage = {
                "prompt_tokens": um.get("promptTokenCount", 0),
                "completion_tokens": um.get("candidatesTokenCount", 0),
                "total_tokens": um.get("totalTokenCount", 0),
                "cached_input_tokens": um.get("cachedContentTokenCount", 0),
                "raw": um,
            }

        result: ChatResponse = {
            "message": message,
            "usage": usage,
            "finish_reason": finish_reason,
            "provider": "gemini",
            "native_metadata": {
                key: data[key]
                for key in ("modelVersion", "responseId", "promptFeedback")
                if key in data
            },
        }
        logger.log(
            TRACE,
            "Gemini response:\n%s",
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
        )
        return result

    # ── chat 主方法 ───────────────────────────────────────

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_first_token: FirstTokenCallback | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ChatResponse:
        payload = self._build_payload(messages, tools, temperature, max_tokens, request_options)

        session = self._get_session()
        logger.log(
            TRACE,
            "Gemini payload:\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

        if self._stream:
            url = f"{self._root}/v1beta/models/{self._model}:streamGenerateContent?alt=sse"
            logger.debug(
                "Gemini request (stream): model=%s, messages=%d, tools=%s",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            timeout = aiohttp.ClientTimeout(
                total=self._request_timeout * 5,
                sock_read=float(self._request_timeout),
            )
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Gemini API error {resp.status}: {body}")
                return await self._consume_stream(resp, on_first_token=on_first_token)
        else:
            url = f"{self._root}/v1beta/models/{self._model}:generateContent"
            logger.debug(
                "Gemini request: model=%s, messages=%d, tools=%s",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            timeout = aiohttp.ClientTimeout(total=self._request_timeout)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Gemini API error {resp.status}: {body}")
                data = await resp.json()
            return self._parse_gemini_response(data)

    # ── Streaming ─────────────────────────────────────────

    async def _consume_stream(
        self,
        resp: aiohttp.ClientResponse,
        *,
        on_first_token: FirstTokenCallback | None = None,
    ) -> ChatResponse:
        """读取 Gemini SSE stream，累积为完整 ChatResponse。

        Gemini streaming 每个 chunk 是完整的 candidate 结构（非 delta），
        text 和 functionCall 需要跨 chunk 累积。
        """
        text_parts: list[str] = []
        text_signature: str | None = None
        tool_calls: list[dict[str, Any]] = []
        finish_reason = "stop"
        usage: UsageInfo | None = None
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

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                logger.warning("Malformed Gemini SSE chunk: %s", data_str[:200])
                continue

            chunk_count += 1

            # 累积 candidates
            candidates = chunk.get("candidates", [])
            if candidates:
                candidate = candidates[0]
                for part in candidate.get("content", {}).get("parts", []):
                    if part.get("thought"):
                        continue
                    if "text" in part:
                        text_parts.append(part["text"])
                        if part["text"] and not first_token_seen:
                            first_token_seen = True
                            if on_first_token is not None:
                                await on_first_token()
                        if "thoughtSignature" in part:
                            text_signature = part["thoughtSignature"]
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tc: dict[str, Any] = {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(
                                    fc.get("args", {}),
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        if "thoughtSignature" in part:
                            tc["thought_signature"] = part["thoughtSignature"]
                        tool_calls.append(tc)
                raw_reason = candidate.get("finishReason")
                if raw_reason:
                    reason_map = {
                        "STOP": "stop",
                        "MAX_TOKENS": "length",
                        "SAFETY": "stop",
                        "RECITATION": "stop",
                    }
                    finish_reason = reason_map.get(raw_reason, "stop")

            # usageMetadata（通常在最后一个 chunk）
            um = chunk.get("usageMetadata")
            if um:
                usage = {
                    "prompt_tokens": um.get("promptTokenCount", 0),
                    "completion_tokens": um.get("candidatesTokenCount", 0),
                    "total_tokens": um.get("totalTokenCount", 0),
                    "cached_input_tokens": um.get("cachedContentTokenCount", 0),
                    "raw": um,
                }

        # 组装
        message: Message = {"role": "assistant"}  # type: ignore[typeddict-item]
        content = "".join(text_parts)
        if content:
            message["content"] = content
        if tool_calls:
            message["tool_calls"] = tool_calls
            if finish_reason == "stop":
                finish_reason = "tool_calls"
        if text_signature:
            message["_gemini_text_signature"] = text_signature  # type: ignore[typeddict-item]

        logger.debug(
            "Gemini stream complete: %d chunks, content_len=%d, tool_calls=%d, finish=%s",
            chunk_count,
            len(content),
            len(tool_calls),
            finish_reason,
        )
        result: ChatResponse = {
            "message": message,
            "usage": usage,
            "finish_reason": finish_reason,
            "provider": "gemini",
        }
        logger.log(
            TRACE,
            "Gemini response:\n%s",
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
        )
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
