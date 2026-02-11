"""Tool calling 循环。

运行 LLM 的 tool calling 流程：调用模型 → 如果模型要调用工具 → 执行工具 →
将结果喂回模型 → 重复，直到模型不再调用工具或达到最大轮次。

返回完整的 messages 列表，包含所有 assistant/tool 轮次。
这个列表可以直接用于 handover 给其他模型。
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sophos.llm.provider import ChatResponse, LLMProvider, Message

logger = logging.getLogger(__name__)


async def run_tool_loop(
    provider: LLMProvider,
    messages: list[Message],
    tools: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
    *,
    max_rounds: int = 10,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> list[Message]:
    """运行 tool calling 循环。

    Args:
        provider:       LLM provider 实例
        messages:       初始消息列表（含 system + 上下文）
        tools:          可用工具的 schema 列表
        tool_executor:  工具执行回调，签名 (tool_name, params_dict) -> result
        max_rounds:     最大循环轮次，防止无限循环
        temperature:    覆盖 provider 默认温度
        max_tokens:     覆盖 provider 默认 max_tokens

    Returns:
        完整的 messages 列表，包含所有轮次的 assistant 和 tool 消息。
        最后一条 assistant 消息是模型的最终回复。
    """
    # 复制一份，不修改调用方的列表
    messages = list(messages)

    for round_num in range(1, max_rounds + 1):
        response: ChatResponse = await provider.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        assistant_msg = response["message"]
        messages.append(assistant_msg)

        # 如果模型没有调用工具，循环结束
        tool_calls = assistant_msg.get("tool_calls")
        if not tool_calls:
            logger.debug("Tool loop finished after %d round(s) (no tool calls)", round_num)
            return messages

        # 执行每个 tool call，将结果作为 tool message 追加
        logger.info("Tool loop round %d: %d tool call(s)", round_num, len(tool_calls))
        for i, tc in enumerate(tool_calls):
            func = tc["function"]
            tool_name = func["name"]

            # 兼容：arguments 可能是 JSON string 或已解析的 dict
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, dict):
                params: dict[str, Any] = raw_args
            else:
                try:
                    params = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Invalid tool call arguments: %s", raw_args)
                    params = {}

            logger.info("Calling tool: %s(%s)", tool_name, json.dumps(params, ensure_ascii=False))
            try:
                result = await tool_executor(tool_name, params)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                logger.exception("Tool %s failed", tool_name)
                result_str = json.dumps({"error": str(e)}, ensure_ascii=False)

            # 兼容：有些 provider 不返回 tool_call id
            tc_id = tc.get("id") or f"call_{round_num}_{i}"
            tool_msg: Message = {
                "role": "tool",
                "tool_call_id": tc_id,
                "name": tool_name,
                "content": result_str,
            }
            messages.append(tool_msg)

    # 达到最大轮次，做最后一次不带 tools 的调用让模型总结
    logger.warning("Tool loop hit max rounds (%d), forcing final response", max_rounds)
    response = await provider.chat(messages, temperature=temperature, max_tokens=max_tokens)
    messages.append(response["message"])
    return messages
