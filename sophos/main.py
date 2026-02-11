"""Sophos — Application entry point."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from sophos.config import settings
from sophos.db import close_db, init_db
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI

logger = logging.getLogger("sophos")


async def handle_event(api: OneBotAPI, event: dict[str, Any], store: MessageStore) -> None:
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


async def ws_loop(store: MessageStore) -> None:
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
                                asyncio.create_task(handle_event(api, event, store))

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

    try:
        await ws_loop(store)
    finally:
        await close_db()


def main() -> None:
    """CLI entry point."""
    # ── 日志配置 ──────────────────────────────────────────
    # 控制台：INFO 级别，人类可读
    # 文件：  DEBUG 级别，包含 WS 原始消息等细节
    log_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_fmt))
    root_logger.addHandler(console_handler)

    # 文件 handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "sophos.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    root_logger.addHandler(file_handler)
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
