"""Pipeline — 中间件风格的消息处理链。

Stage 通过 next() 回调串联，不调用 next() 即终止后续 stage。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from sophos.commands import handle_llm_command
from sophos.config import settings
from sophos.llm.context import build_chat_context, describe_schema
from sophos.llm.provider_manager import ProviderManager
from sophos.llm.tool_loop import run_tool_loop
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.tools.onebot import ALL_TOOLS
from sophos.tools.registry import ToolRegistry
from sophos.vision import process_message_images

logger = logging.getLogger("sophos")

NextFn = Callable[[], Awaitable[None]]


@dataclass
class PipelineContext:
    """Pipeline 执行上下文，在 stage 之间传递。"""

    # ── 事件数据 ──
    event: dict[str, Any]
    post_type: str
    message_type: str
    self_id: int
    user_id: int
    group_id: int | None
    segments: list[dict[str, Any]]
    text: str

    # ── 依赖注入 ──
    api: OneBotAPI
    store: MessageStore
    provider_mgr: ProviderManager
    session: aiohttp.ClientSession

    # ── 可变状态（stage 间共享）──
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(
        cls,
        event: dict[str, Any],
        api: OneBotAPI,
        store: MessageStore,
        provider_mgr: ProviderManager,
        session: aiohttp.ClientSession,
    ) -> "PipelineContext | None":
        """从 OneBot 事件构建上下文。非消息事件返回 None。"""
        post_type = event.get("post_type")
        if post_type not in ("message", "message_sent"):
            return None
        segments: list[dict[str, Any]] = event.get("message", [])
        text = "".join(
            seg["data"]["text"] for seg in segments if seg.get("type") == "text"
        ).strip()
        return cls(
            event=event,
            post_type=post_type,
            message_type=event.get("message_type", "private"),
            self_id=event.get("self_id", 0),
            user_id=event.get("user_id", 0),
            group_id=event.get("group_id"),
            segments=segments,
            text=text,
            api=api,
            store=store,
            provider_mgr=provider_mgr,
            session=session,
        )


# ── Stage ABC ────────────────────────────────────────────────


class Stage(ABC):
    """Pipeline stage 抽象基类。

    不调用 next() 即终止后续 stage（短路）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """唯一标识。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """一句话描述（给 WebUI 展示）。"""
        ...

    @property
    def enabled(self) -> bool:
        """是否启用。默认 True，WebUI 可覆盖。"""
        return True

    @abstractmethod
    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        """执行 stage 逻辑。调用 await next() 继续链。"""
        ...


# ── Pipeline Runner ──────────────────────────────────────────


class Pipeline:
    """Stage 链式执行器。"""

    def __init__(self, stages: list[Stage] | None = None) -> None:
        self._stages: list[Stage] = list(stages) if stages else []

    def add(self, stage: Stage) -> "Pipeline":
        """添加 stage 到末尾。返回 self 支持链式调用。"""
        self._stages.append(stage)
        return self

    @property
    def stages(self) -> list[Stage]:
        return list(self._stages)

    async def run(self, ctx: PipelineContext) -> None:
        """执行 pipeline。跳过 disabled stage，异常终止链。"""
        active = [s for s in self._stages if s.enabled]

        async def _run_at(index: int) -> None:
            if index >= len(active):
                return
            stage = active[index]
            logger.debug("Pipeline: entering stage '%s'", stage.name)
            try:
                await stage.execute(ctx, lambda: _run_at(index + 1))
            except Exception:
                logger.exception("Stage '%s' failed", stage.name)

        await _run_at(0)


# ── 辅助函数（从 main.py 迁移）─────────────────────────────


async def _process_event_images(
    ctx: PipelineContext,
) -> None:
    """处理消息中的图片段：下载 → 哈希 → VLM 识别 → 写入 extra。"""
    if not any(seg.get("type") == "image" for seg in ctx.segments):
        return

    vision_provider = ctx.provider_mgr.get_vision_provider()
    pool = ctx.store.pool

    context_messages: list[dict[str, Any]] | None = None
    if settings.vision_context_messages > 0:
        rows = await ctx.store.get_context(
            group_id=ctx.group_id,
            user_id=ctx.user_id if ctx.message_type == "private" else None,
            limit=settings.vision_context_messages,
        )
        if rows:
            context_messages = [
                {"role": "user", "content": r.get("plain_text", "")}
                for r in rows if r.get("plain_text")
            ]

    try:
        image_infos = await process_message_images(
            ctx.segments,
            pool=pool,
            session=ctx.session,
            vision_provider=vision_provider,
            context_messages=context_messages,
        )
    except Exception:
        logger.exception(
            "Image processing failed for message_id=%s",
            ctx.event.get("message_id"),
        )
        return

    if image_infos:
        message_id = ctx.event.get("message_id")
        if message_id is not None:
            await ctx.store.update_image_extra(message_id, image_infos)
            logger.info(
                "Stored %d image description(s) for message_id=%s",
                len(image_infos), message_id,
            )


# ── LLM 触发（临时，后续由触发器 stage 替代）────────────────

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


async def _handle_llm_trigger(ctx: PipelineContext) -> None:
    """构建上下文 → tool loop → LLM 通过 send_msg tool 回复。"""
    provider = ctx.provider_mgr.get_provider()
    registry = _get_registry()

    nickname = settings.bot_nickname or "Sophos"
    fmt_desc = describe_schema(settings.llm_user_schema)
    if ctx.message_type == "group":
        meta = (
            f"\n---\n"
            f"当前会话：群聊 | 群号: {ctx.group_id} | 你的QQ: {ctx.self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    else:
        meta = (
            f"\n---\n"
            f"当前会话：私聊 | 对方QQ: {ctx.user_id} | 你的QQ: {ctx.self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    system_prompt = _SYSTEM_PROMPT.format(nickname=nickname) + meta
    messages = await build_chat_context(
        ctx.store,
        group_id=ctx.group_id,
        user_id=ctx.user_id if ctx.message_type == "private" else None,
        system_prompt=system_prompt,
    )

    tool_context: dict[str, Any] = {
        "api": ctx.api,
        "store": ctx.store,
        "self_id": ctx.self_id,
        "message_type": ctx.message_type,
        "group_id": ctx.group_id,
        "user_id": ctx.user_id,
    }
    sent_via_tool = False

    async def tool_executor(name: str, params: dict[str, Any]) -> Any:
        nonlocal sent_via_tool
        result = await registry.execute(name, params, tool_context)
        if name == "send_msg":
            sent_via_tool = True
        return result

    try:
        result_messages = await run_tool_loop(
            provider,
            messages,
            tools=registry.get_function_schemas(scope=ctx.message_type),
            tool_executor=tool_executor,
            max_rounds=settings.llm_max_tool_rounds,
        )
    except Exception:
        logger.exception("LLM tool loop failed")
        return

    # 兜底：LLM 没调 send_msg 时提取 content 直接发送
    if not sent_via_tool:
        final_msg = result_messages[-1] if result_messages else None
        reply_text = (final_msg.get("content") or "") if final_msg else ""
        if not reply_text:
            logger.warning("LLM returned empty response and didn't call send_msg")
            return

        logger.info("LLM didn't call send_msg, using fallback")
        send_params: dict[str, Any] = {
            "message_type": ctx.message_type,
            "message": [{"type": "text", "data": {"text": reply_text}}],
        }
        if ctx.message_type == "group":
            send_params["group_id"] = ctx.group_id
        else:
            send_params["user_id"] = ctx.user_id

        result = await ctx.api.call("send_msg", send_params)
        sent_message_id = result.get("message_id")
        logger.info("Fallback reply sent (message_id=%s)", sent_message_id)

        if sent_message_id is not None:
            await ctx.store.save_self_message(
                message_id=sent_message_id,
                message_type=ctx.message_type,
                group_id=ctx.group_id if ctx.message_type == "group" else None,
                user_id=ctx.self_id or 0,
                raw_message=[{"type": "text", "data": {"text": reply_text}}],
            )


# ── Stage 实现 ───────────────────────────────────────────────


class StoreMessageStage(Stage):
    """将消息事件存入数据库。"""

    @property
    def name(self) -> str:
        return "store_message"

    @property
    def description(self) -> str:
        return "将消息事件存入数据库"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        await ctx.store.save_event_message(ctx.event, self_id=ctx.self_id)
        await next()


class ProcessImagesStage(Stage):
    """异步处理消息中的图片（VLM 识别）。"""

    @property
    def name(self) -> str:
        return "process_images"

    @property
    def description(self) -> str:
        return "异步处理消息中的图片（VLM 识别）"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        asyncio.create_task(_process_event_images(ctx))
        await next()


class FilterSelfStage(Stage):
    """跳过 bot 自身发出的消息。"""

    @property
    def name(self) -> str:
        return "filter_self"

    @property
    def description(self) -> str:
        return "跳过 bot 自身发出的消息"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.user_id == ctx.self_id:
            return
        await next()


class HandleCommandStage(Stage):
    """处理 dot 命令（.ping, .llm）。"""

    @property
    def name(self) -> str:
        return "handle_command"

    @property
    def description(self) -> str:
        return "处理 dot 命令（.ping, .llm）"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.text == ".ping":
            await self._handle_ping(ctx)
            return
        if ctx.text.startswith(".llm"):
            await handle_llm_command(
                ctx.text, api=ctx.api, event=ctx.event, provider_mgr=ctx.provider_mgr,
            )
            return
        await next()

    async def _handle_ping(self, ctx: PipelineContext) -> None:
        params: dict[str, Any] = {
            "message_type": ctx.message_type,
            "message": [{"type": "text", "data": {"text": "pong"}}],
        }
        if ctx.message_type == "group":
            params["group_id"] = ctx.group_id
        else:
            params["user_id"] = ctx.user_id

        result = await ctx.api.call("send_msg", params)
        sent_message_id = result.get("message_id")
        logger.info("Replied pong (message_id=%s) to %s", sent_message_id, ctx.user_id)

        if sent_message_id is not None:
            await ctx.store.save_self_message(
                message_id=sent_message_id,
                message_type=ctx.message_type,
                group_id=ctx.group_id if ctx.message_type == "group" else None,
                user_id=ctx.self_id or 0,
                raw_message=[{"type": "text", "data": {"text": "pong"}}],
            )


class TriggerLLMStage(Stage):
    """咕喵咕喵 触发 LLM 对话（临时）。"""

    @property
    def name(self) -> str:
        return "trigger_llm"

    @property
    def description(self) -> str:
        return "咕喵咕喵 触发 LLM 对话（临时）"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.text.startswith("咕喵咕喵"):
            await _handle_llm_trigger(ctx)
            return
        await next()


# ── 默认 stage 列表 ──────────────────────────────────────────

DEFAULT_STAGES: list[Stage] = [
    StoreMessageStage(),
    ProcessImagesStage(),
    FilterSelfStage(),
    HandleCommandStage(),
    TriggerLLMStage(),
]
