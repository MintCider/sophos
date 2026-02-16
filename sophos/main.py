"""Sophos — Application entry point."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from sophos.config import settings
from sophos.db import close_db, init_db
from sophos.llm.provider_manager import ProviderManager
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.pipeline import DEFAULT_STAGES, Pipeline, PipelineContext

logger = logging.getLogger("sophos")

_pipeline = Pipeline(DEFAULT_STAGES)


async def handle_event(
    api: OneBotAPI,
    event: dict[str, Any],
    store: MessageStore,
    provider_mgr: ProviderManager,
    session: aiohttp.ClientSession,
) -> None:
    """处理一个 OneBot 事件上报。"""
    ctx = PipelineContext.from_event(event, api, store, provider_mgr, session)
    if ctx is None:
        return
    await _pipeline.run(ctx)


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
                                asyncio.create_task(handle_event(api, event, store, provider_mgr, session))

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
