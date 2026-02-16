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


def _make_error_result(error: str) -> str:
    """统一构造 tool 错误反馈 JSON。"""
    return json.dumps({"error": error}, ensure_ascii=False)


def _sanitize_tool_calls(assistant_msg: Message) -> None:
    """清理 assistant message 中畸形的 tool_calls arguments。

    某些 LLM 会生成非法 JSON arguments（如多个 JSON 对象拼接），
    导致下一轮发回 API 时被拒绝。此函数将非法 arguments 替换为 "{}"，
    确保对话历史始终合法，错误信息通过 tool result message 反馈给 LLM。
    """
    tool_calls = assistant_msg.get("tool_calls")
    if not tool_calls:
        return
    for tc in tool_calls:
        func = tc.get("function", {})
        raw = func.get("arguments", "{}")
        if isinstance(raw, str):
            try:
                json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                func["arguments"] = "{}"


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
        try:
            response: ChatResponse = await provider.chat(
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            # 上一轮的 assistant message 可能包含畸形 tool_calls，
            # 导致 API 拒绝整个请求。回滚畸形消息后不带 tools 做最终调用。
            logger.warning("LLM API error in tool loop round %d, attempting recovery: %s", round_num, e)
            while messages and messages[-1].get("role") == "tool":
                messages.pop()
            if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                messages.pop()
            try:
                response = await provider.chat(messages, temperature=temperature, max_tokens=max_tokens)
                messages.append(response["message"])
            except Exception:
                logger.exception("Recovery call also failed")
            return messages

        assistant_msg = response["message"]
        # 清理畸形 arguments，确保对话历史合法
        _sanitize_tool_calls(assistant_msg)
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

            # 解析参数
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, dict):
                params: dict[str, Any] = raw_args
                parse_error = False
            else:
                try:
                    params = json.loads(raw_args)
                    parse_error = False
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Invalid tool call arguments for %s: %s", tool_name, raw_args[:200])
                    params = {}
                    parse_error = True

            # 统一错误处理：解析失败或执行失败都反馈给 LLM
            if parse_error:
                result_str = _make_error_result(
                    f"工具 {tool_name} 的参数 JSON 格式错误，无法解析。请检查参数格式后重试。"
                )
            else:
                try:
                    logger.info("Calling tool: %s(%s)", tool_name, json.dumps(params, ensure_ascii=False))
                    result = await tool_executor(tool_name, params)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as e:
                    logger.exception("Tool %s execution failed", tool_name)
                    result_str = _make_error_result(f"工具 {tool_name} 执行失败: {e}")

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