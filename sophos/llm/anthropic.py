"""Anthropic Messages API 的 LLM Provider 实现。

使用 /v1/messages 端点，
在 API 边界做 OpenAI ↔ Anthropic 格式双向转换。
内部消息格式统一为 OpenAI Chat Completions 格式。
"""

import json
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from sophos.llm.provider import ChatResponse, LLMProvider, Message, UsageInfo

logger = logging.getLogger(__name__)

TRACE = 5


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API Provider。"""

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
        parsed = urlparse(base_url)
        self._root = f"{parsed.scheme}://{parsed.netloc}"
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
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    # ── OpenAI → Anthropic 转换 ───────────────────────────

    @staticmethod
    def _convert_messages(
        messages: list[Message],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """将 OpenAI messages 转换为 Anthropic messages + system。

        Returns:
            (system_text, messages)
        """
        system_text: str | None = None
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")

            if role == "system":
                system_text = msg.get("content", "")
                continue

            if role == "user":
                result.append({"role": "user", "content": msg.get("content", "")})
                continue

            if role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        inp = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                result.append({
                    "role": "assistant",
                    "content": content_blocks if content_blocks else "",
                })
                continue

            if role == "tool":
                # Anthropic: tool results 放在 user message 的 tool_result blocks 中
                tr_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }
                # 合并连续 tool results 到同一个 user message
                if result and result[-1]["role"] == "user" and isinstance(
                    result[-1]["content"], list
                ) and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in result[-1]["content"]
                ):
                    result[-1]["content"].append(tr_block)
                else:
                    result.append({"role": "user", "content": [tr_block]})
                continue

        return system_text, result

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 OpenAI tool schemas 转换为 Anthropic tools。"""
        converted = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            fn = tool["function"]
            t: dict[str, Any] = {
                "name": fn["name"],
                "description": fn.get("description", ""),
            }
            if fn.get("parameters"):
                t["input_schema"] = fn["parameters"]
            converted.append(t)
        return converted

    # ── Anthropic → OpenAI 转换 ───────────────────────────

    @staticmethod
    def _parse_anthropic_response(data: dict[str, Any]) -> ChatResponse:
        """将 Anthropic API 响应转换为 OpenAI ChatResponse。"""
        content_blocks = data.get("content", [])

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(
                            block.get("input", {}), ensure_ascii=False,
                        ),
                    },
                })

        message: Message = {"role": "assistant"}  # type: ignore[typeddict-item]
        content = "".join(text_parts)
        if content:
            message["content"] = content
        if tool_calls:
            message["tool_calls"] = tool_calls

        # stop_reason 映射
        raw_reason = data.get("stop_reason", "end_turn")
        reason_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        finish_reason = reason_map.get(raw_reason, "stop")

        # usage
        usage: UsageInfo | None = None
        u = data.get("usage")
        if u:
            usage = {
                "prompt_tokens": u.get("input_tokens", 0),
                "completion_tokens": u.get("output_tokens", 0),
                "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
            }

        result: ChatResponse = {
            "message": message,
            "usage": usage,
            "finish_reason": finish_reason,
        }
        logger.log(
            TRACE,
            "Anthropic response:\n%s",
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
    ) -> ChatResponse:
        system_text, converted_msgs = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens if max_tokens is not None else self._default_max_tokens,
            "messages": converted_msgs,
        }
        temp = temperature if temperature is not None else self._default_temperature
        if temp is not None:
            payload["temperature"] = temp
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = self._convert_tools(tools)
        # extra_body 合并到顶层
        for k, v in self._extra_body.items():
            payload[k] = v

        session = self._get_session()
        url = f"{self._root}/v1/messages"
        logger.log(
            TRACE,
            "Anthropic payload:\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

        if self._stream:
            payload["stream"] = True
            logger.debug(
                "Anthropic request (stream): model=%s, messages=%d, tools=%s",
                self._model, len(messages), len(tools) if tools else 0,
            )
            timeout = aiohttp.ClientTimeout(
                total=self._request_timeout * 5,
                sock_read=float(self._request_timeout),
            )
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Anthropic API error {resp.status}: {body}")
                return await self._consume_stream(resp)
        else:
            logger.debug(
                "Anthropic request: model=%s, messages=%d, tools=%s",
                self._model, len(messages), len(tools) if tools else 0,
            )
            timeout = aiohttp.ClientTimeout(total=self._request_timeout)
            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Anthropic API error {resp.status}: {body}")
                data = await resp.json()
            return self._parse_anthropic_response(data)

    # ── Streaming ─────────────────────────────────────────

    async def _consume_stream(
        self, resp: aiohttp.ClientResponse,
    ) -> ChatResponse:
        """读取 Anthropic SSE stream，累积为完整 ChatResponse。

        Anthropic streaming 使用 named events:
        message_start, content_block_start, content_block_delta,
        content_block_stop, message_delta, message_stop
        """
        # 按 index 累积 content blocks
        blocks: dict[int, dict[str, Any]] = {}  # index → {type, text/json_parts}
        stop_reason = "stop"
        input_tokens = 0
        output_tokens = 0

        while True:
            line_bytes = await resp.content.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()

            # Anthropic SSE: "event: xxx" 行后跟 "data: {...}" 行
            if line.startswith("event: "):
                event_type = line[7:]
                # 读取对应的 data 行
                data_line_bytes = await resp.content.readline()
                if not data_line_bytes:
                    break
                data_line = data_line_bytes.decode("utf-8").strip()
                if not data_line.startswith("data: "):
                    continue
                try:
                    data = json.loads(data_line[6:])
                except json.JSONDecodeError:
                    logger.warning("Malformed Anthropic SSE: %s", data_line[:200])
                    continue

                if event_type == "message_start":
                    msg = data.get("message", {})
                    u = msg.get("usage", {})
                    input_tokens = u.get("input_tokens", 0)

                elif event_type == "content_block_start":
                    idx = data.get("index", 0)
                    block = data.get("content_block", {})
                    btype = block.get("type", "text")
                    if btype == "text":
                        blocks[idx] = {"type": "text", "parts": [block.get("text", "")]}
                    elif btype == "tool_use":
                        blocks[idx] = {
                            "type": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json_parts": [],
                        }

                elif event_type == "content_block_delta":
                    idx = data.get("index", 0)
                    delta = data.get("delta", {})
                    dtype = delta.get("type", "")
                    if dtype == "text_delta" and idx in blocks:
                        blocks[idx]["parts"].append(delta.get("text", ""))
                    elif dtype == "input_json_delta" and idx in blocks:
                        blocks[idx]["json_parts"].append(delta.get("partial_json", ""))

                elif event_type == "message_delta":
                    delta = data.get("delta", {})
                    raw = delta.get("stop_reason", "end_turn")
                    reason_map = {
                        "end_turn": "stop",
                        "tool_use": "tool_calls",
                        "max_tokens": "length",
                        "stop_sequence": "stop",
                    }
                    stop_reason = reason_map.get(raw, "stop")
                    u = data.get("usage", {})
                    output_tokens = u.get("output_tokens", output_tokens)

                elif event_type == "message_stop":
                    break

        # 组装
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(blocks):
            b = blocks[idx]
            if b["type"] == "text":
                text_parts.append("".join(b["parts"]))
            elif b["type"] == "tool_use":
                raw_json = "".join(b.get("json_parts", []))
                try:
                    inp = json.loads(raw_json) if raw_json else {}
                except json.JSONDecodeError:
                    inp = {}
                tool_calls.append({
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(inp, ensure_ascii=False),
                    },
                })

        message: Message = {"role": "assistant"}  # type: ignore[typeddict-item]
        content = "".join(text_parts)
        if content:
            message["content"] = content
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage: UsageInfo | None = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

        logger.debug(
            "Anthropic stream complete: content_len=%d, tool_calls=%d, finish=%s",
            len(content), len(tool_calls), stop_reason,
        )
        result: ChatResponse = {
            "message": message,
            "usage": usage,
            "finish_reason": stop_reason,
        }
        logger.log(
            TRACE,
            "Anthropic response:\n%s",
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
        )
        return result

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
