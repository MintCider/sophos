"""Sophos — Application entry point."""

import asyncio
import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, override
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from sophos import permission, runtime_config
from sophos.adapter_router import AdapterRouter
from sophos.api import create_app
from sophos.cleanup import run_cleanup_loop
from sophos.config import settings
from sophos.db import close_db, init_db
from sophos.llm.provider_manager import ProviderManager
from sophos.memory.store import MemoryStore
from sophos.message_store import MessageStore
from sophos.messaging import MessageService
from sophos.onebot_adapter import OneBot11Adapter
from sophos.onebot_api import OneBotAPI
from sophos.pipeline import INGEST_STAGES, RESPONSE_STAGES, Pipeline, PipelineContext, _get_registry
from sophos.platform import PlatformAdapter
from sophos.platform_store import PlatformStore

logger = logging.getLogger("sophos")

_ingest = Pipeline(INGEST_STAGES)
_response = Pipeline(RESPONSE_STAGES)


async def handle_event(
    adapter: PlatformAdapter,
    event: dict[str, Any],
    message_service: MessageService,
    store: MessageStore,
    provider_mgr: ProviderManager,
    session: aiohttp.ClientSession,
    memory_store: MemoryStore | None = None,
) -> None:
    """Normalize and process one platform event through the core pipeline."""
    message = await adapter.normalize_event(event)
    if message is None:
        return
    ctx = PipelineContext.from_event(message, adapter, message_service, store, provider_mgr, session)
    if memory_store is not None:
        ctx.state["memory_store"] = memory_store
    await _ingest.run(ctx)
    await _response.run(ctx)


async def ws_loop(
    store: MessageStore,
    platform_store: PlatformStore,
    router: AdapterRouter,
    message_service: MessageService,
    provider_mgr: ProviderManager,
    memory_store: MemoryStore | None = None,
) -> None:
    """连接 NapCat WebSocket 并持续监听消息。"""
    ws_url = settings.onebot_ws_url
    ws_token = settings.onebot_ws_token
    headers = {"Authorization": f"Bearer {ws_token}"} if ws_token else {}

    async with aiohttp.ClientSession() as session:
        while True:
            api: OneBotAPI | None = None
            adapter: OneBot11Adapter | None = None
            try:
                logger.info("Connecting to %s ...", ws_url)
                async with session.ws_connect(ws_url, headers=headers) as ws:
                    logger.info("Connected!")
                    api = OneBotAPI(ws)
                    if settings.bot_id:
                        adapter = await OneBot11Adapter.create(
                            api=api,
                            platform_store=platform_store,
                            self_external_id=str(settings.bot_id),
                            display_name=settings.bot_nickname,
                        )
                        router.register(adapter)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data: dict[str, Any] = json.loads(msg.data)
                            logger.log(5, "WS ← %s", msg.data)

                            # 分流：API 响应 → 填充 Future；事件 → 返回给我们处理
                            event = api.dispatch(data)
                            if event is not None:
                                if adapter is None:
                                    self_id = str(event.get("self_id") or settings.bot_id or "")
                                    if not self_id:
                                        logger.warning("Ignoring OneBot event without self_id")
                                        continue
                                    adapter = await OneBot11Adapter.create(
                                        api=api,
                                        platform_store=platform_store,
                                        self_external_id=self_id,
                                        display_name=settings.bot_nickname,
                                    )
                                    router.register(adapter)
                                # 事件处理放到独立 Task，不阻塞 WS 读取循环
                                asyncio.create_task(
                                    handle_event(
                                        adapter,
                                        event,
                                        message_service,
                                        store,
                                        provider_mgr,
                                        session,
                                        memory_store=memory_store,
                                    )
                                )

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("WebSocket closed or error: %s", msg.data)
                            break
            except (aiohttp.ClientError, ConnectionError, OSError) as e:
                logger.warning("Connection failed: %s. Retrying in 5s...", e)
            finally:
                if adapter is not None:
                    router.unregister(adapter)
                if api is not None:
                    api.cancel_all("connection closed")

            await asyncio.sleep(5)  # 断线重连间隔


async def start() -> None:
    """Boot the application."""
    logger.info("Sophos is starting...")

    # 初始化数据库（建表 + 连接池）
    pool = await init_db()
    store = MessageStore(pool)
    platform_store = PlatformStore(pool)
    router = AdapterRouter()
    message_service = MessageService(store, platform_store, router)
    interrupted = await store.fail_interrupted_enrichments()
    if interrupted:
        logger.warning("Marked %d interrupted enrichment message(s) as failed", interrupted)

    # 初始化运行时配置
    await runtime_config.init(pool)
    await permission.init(pool)

    # 初始化 LLM provider 管理器
    provider_mgr = ProviderManager(pool)
    await provider_mgr.init()

    # 初始化记忆存储
    memory_store = MemoryStore(pool, provider_mgr.get_embedding_provider())

    # 启动 HTTP API server（非阻塞）
    app = await create_app(
        pool=pool,
        provider_mgr=provider_mgr,
        memory_store=memory_store,
        tool_registry=_get_registry(),
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.api_host, settings.api_port)
    await site.start()
    logger.info("API server listening on %s:%d", settings.api_host, settings.api_port)

    # 启动后台数据清理任务
    cleanup_task = asyncio.create_task(run_cleanup_loop(pool))

    try:
        await ws_loop(
            store,
            platform_store,
            router,
            message_service,
            provider_mgr,
            memory_store=memory_store,
        )
    finally:
        cleanup_task.cancel()
        await runner.cleanup()
        await provider_mgr.close()
        await close_db()


def main() -> None:
    """CLI entry point."""
    # ── 日志配置 ──────────────────────────────────────────
    # 自定义 TRACE 级别（比 DEBUG 更低，记录完整 prompt/response）
    trace_level = 5
    logging.addLevelName(trace_level, "TRACE")

    # 控制台：INFO 级别，人类可读
    # 文件：  TRACE 级别，包含完整 LLM 请求/响应
    log_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    class _TzFormatter(logging.Formatter):
        """用配置时区替代本地时间的 Formatter。"""

        _tz = ZoneInfo(settings.timezone)

        @override
        def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
            dt = datetime.fromtimestamp(record.created, tz=self._tz)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"

    root_logger = logging.getLogger()
    root_logger.setLevel(trace_level)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_TzFormatter(log_fmt))
    root_logger.addHandler(console_handler)

    # 文件 handler
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "sophos.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(trace_level)
    file_handler.setFormatter(_TzFormatter(log_fmt))
    root_logger.addHandler(file_handler)

    # 压制 aiohttp access log（每个 HTTP 请求都输出 INFO，WebUI 连上就刷屏）
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
