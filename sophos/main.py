"""Sophos — Application entry point."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from sophos.commands import handle_llm_command
from sophos.config import settings
from sophos.db import close_db, init_db
from sophos.llm.context import build_chat_context, describe_schema
from sophos.llm.provider_manager import ProviderManager
from sophos.llm.tool_loop import run_tool_loop
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.tools.onebot import ALL_TOOLS
from sophos.tools.registry import ToolRegistry

logger = logging.getLogger("sophos")


async def handle_event(
    api: OneBotAPI,
    event: dict[str, Any],
    store: MessageStore,
    provider_mgr: ProviderManager,
) -> None:
    """处理一个 OneBot 事件上报。

    流程：
      1. 如果是消息事件 → 先存入数据库
      2. 再做业务处理（目前仅 .ping 命令）

    NapCat 对自身发出的消息使用 post_type="message_sent" 而非 "message"，
    两者都需要处理。
    """
    post_type = event.get("post_type")
    if post_type not in ("message", "message_sent"):
        return

    # ── 存储消息 ──────────────────────────────────────────
    self_id = event.get("self_id")
    await store.save_event_message(event, self_id=self_id)

    # ── 业务处理（仅处理他人消息，跳过 bot 自身发出的消息）──
    user_id = event.get("user_id")
    if user_id == self_id:
        return

    # ── 业务处理 ──────────────────────────────────────────
    segments: list[dict[str, Any]] = event.get("message", [])
    text = "".join(
        seg["data"]["text"] for seg in segments if seg.get("type") == "text"
    ).strip()

    if text == ".ping":
        msg_type = event.get("message_type", "private")
        params: dict[str, Any] = {
            "message_type": msg_type,
            "message": [{"type": "text", "data": {"text": "pong"}}],
        }
        if msg_type == "group":
            params["group_id"] = event.get("group_id")
        else:
            params["user_id"] = event.get("user_id")

        result = await api.call("send_msg", params)
        sent_message_id = result.get("message_id")
        logger.info("Replied pong (message_id=%s) to %s", sent_message_id, event.get("user_id"))

        # 主动存储 Sophos 发出的消息
        if sent_message_id is not None:
            await store.save_self_message(
                message_id=sent_message_id,
                message_type=msg_type,
                group_id=event.get("group_id") if msg_type == "group" else None,
                user_id=self_id or 0,
                raw_message=[{"type": "text", "data": {"text": "pong"}}],
            )

    elif text.startswith(".llm"):
        await handle_llm_command(
            text, api=api, event=event, provider_mgr=provider_mgr,
        )

    elif text.startswith("咕喵咕喵"):
        await _handle_llm_trigger(api, event, store, provider_mgr)


# ── LLM 触发处理（临时，后续由触发器替代）─────────────────

# 模块级 registry，避免每次请求重建
_tool_registry: ToolRegistry | None = None

_SYSTEM_PROMPT = (
    "你是 {nickname}，一个活跃在 QQ 群聊中的猫娘。\n"
    "你有两类工具可以任意使用：\n"
    "- 输入工具（获取信息）：如查询群成员信息、查询聊天记录\n"
    "- 输出工具（执行操作）：如发送消息\n"
    "你至少需要调用 send_msg 工具发送你的回复。\n"
    "向其他群或私聊发消息时，必须在 send_msg 的 background 参数中提供背景摘要。\n"
    "回复时请自然、简洁，一两句话，像一个真正的群聊成员。"
)


def _get_registry() -> ToolRegistry:
    """获取或创建 ToolRegistry 单例。"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
        for tool in ALL_TOOLS:
            _tool_registry.register(tool)
    return _tool_registry


async def _handle_llm_trigger(
    api: OneBotAPI,
    event: dict[str, Any],
    store: MessageStore,
    provider_mgr: ProviderManager,
) -> None:
    """处理 LLM 触发：构建上下文 → tool loop → LLM 通过 send_msg tool 回复。

    LLM 被要求通过调用 send_msg 等输出工具来完成操作。
    如果 LLM 没有调用 send_msg（兜底），则提取最终 assistant content 发送。
    """
    msg_type = event.get("message_type", "private")
    group_id = event.get("group_id")
    user_id = event.get("user_id")
    self_id = event.get("self_id")

    provider = provider_mgr.get_provider()
    registry = _get_registry()

    # 构建上下文
    nickname = settings.bot_nickname or "Sophos"
    # 会话 metadata：告诉 LLM 当前环境信息
    fmt_desc = describe_schema(settings.llm_user_schema)
    if msg_type == "group":
        meta = (
            f"\n---\n"
            f"当前会话：群聊 | 群号: {group_id} | 你的QQ: {self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    else:
        meta = (
            f"\n---\n"
            f"当前会话：私聊 | 对方QQ: {user_id} | 你的QQ: {self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    system_prompt = _SYSTEM_PROMPT.format(nickname=nickname) + meta
    messages = await build_chat_context(
        store,
        group_id=group_id,
        user_id=user_id if msg_type == "private" else None,
        system_prompt=system_prompt,
    )

    # tool 执行回调 — 追踪是否调用了输出工具
    tool_context: dict[str, Any] = {
        "api": api,
        "store": store,
        "self_id": self_id,
        "message_type": msg_type,
        "group_id": group_id,
        "user_id": user_id,
    }
    sent_via_tool = False

    async def tool_executor(name: str, params: dict[str, Any]) -> Any:
        nonlocal sent_via_tool
        result = await registry.execute(name, params, tool_context)
        if name == "send_msg":
            sent_via_tool = True
        return result

    # 运行 tool loop
    try:
        result_messages = await run_tool_loop(
            provider,
            messages,
            tools=registry.get_function_schemas(scope=msg_type),
            tool_executor=tool_executor,
            max_rounds=settings.llm_max_tool_rounds,
        )
    except Exception:
        logger.exception("LLM tool loop failed")
        return

    # 兜底：如果 LLM 没有通过 send_msg tool 发送回复，提取最终 content 发送
    if not sent_via_tool:
        final_msg = result_messages[-1] if result_messages else None
        reply_text = (final_msg.get("content") or "") if final_msg else ""
        if not reply_text:
            logger.warning("LLM returned empty response and didn't call send_msg")
            return

        logger.info("LLM didn't call send_msg, using fallback")
        send_params: dict[str, Any] = {
            "message_type": msg_type,
            "message": [{"type": "text", "data": {"text": reply_text}}],
        }
        if msg_type == "group":
            send_params["group_id"] = group_id
        else:
            send_params["user_id"] = user_id

        result = await api.call("send_msg", send_params)
        sent_message_id = result.get("message_id")
        logger.info("Fallback reply sent (message_id=%s)", sent_message_id)

        if sent_message_id is not None:
            await store.save_self_message(
                message_id=sent_message_id,
                message_type=msg_type,
                group_id=group_id if msg_type == "group" else None,
                user_id=self_id or 0,
                raw_message=[{"type": "text", "data": {"text": reply_text}}],
            )


async def ws_loop(store: MessageStore, provider_mgr: ProviderManager) -> None:
    """连接 NapCat WebSocket 并持续监听消息。"""
    ws_url = settings.onebot_ws_url
    ws_token = settings.onebot_ws_token
    headers = {"Authorization": f"Bearer {ws_token}"} if ws_token else {}

    async with aiohttp.ClientSession() as session:
        while True:
            api: OneBotAPI | None = None
            try:
                logger.info("Connecting to %s ...", ws_url)
                async with session.ws_connect(ws_url, headers=headers) as ws:
                    logger.info("Connected!")
                    api = OneBotAPI(ws)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data: dict[str, Any] = json.loads(msg.data)
                            logger.debug("WS ← %s", msg.data)

                            # 分流：API 响应 → 填充 Future；事件 → 返回给我们处理
                            event = api.dispatch(data)
                            if event is not None:
                                # 事件处理放到独立 Task，不阻塞 WS 读取循环
                                asyncio.create_task(handle_event(api, event, store, provider_mgr))

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("WebSocket closed or error: %s", msg.data)
                            break
            except (aiohttp.ClientError, ConnectionError, OSError) as e:
                logger.warning("Connection failed: %s. Retrying in 5s...", e)
            finally:
                if api is not None:
                    api.cancel_all("connection closed")

            await asyncio.sleep(5)  # 断线重连间隔


async def start() -> None:
    """Boot the application."""
    logger.info("Sophos is starting...")

    # 初始化数据库（建表 + 连接池）
    pool = await init_db()
    store = MessageStore(pool)

    # 初始化 LLM provider 管理器
    provider_mgr = ProviderManager(pool)
    await provider_mgr.init()

    try:
        await ws_loop(store, provider_mgr)
    finally:
        await provider_mgr.close()
        await close_db()


def main() -> None:
    """CLI entry point."""
    # ── 日志配置 ──────────────────────────────────────────
    # 自定义 TRACE 级别（比 DEBUG 更低，记录完整 prompt/response）
    TRACE = 5
    logging.addLevelName(TRACE, "TRACE")

    # 控制台：INFO 级别，人类可读
    # 文件：  TRACE 级别，包含完整 LLM 请求/响应
    log_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    root_logger = logging.getLogger()
    root_logger.setLevel(TRACE)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_fmt))
    root_logger.addHandler(console_handler)

    # 文件 handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "sophos.log", encoding="utf-8")
    file_handler.setLevel(TRACE)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    root_logger.addHandler(file_handler)
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
