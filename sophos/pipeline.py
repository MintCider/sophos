"""Pipeline — 中间件风格的消息处理链。

Stage 通过 next() 回调串联，不调用 next() 即终止后续 stage。
"""

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

from sophos.commands import (
    handle_bot_command, handle_config_command, handle_help_command,
    handle_llm_command, handle_memory_command, handle_perm_command,
    handle_prompt_command, handle_tools_command, handle_trigger_command,
)
from sophos.config import settings
from sophos.db import get_pool
from sophos.llm.context import build_chat_context, describe_schema, format_timestamp, get_display_name
from sophos.llm.provider_manager import ProviderManager
from sophos.llm.tool_loop import run_tool_loop
from sophos.memory.association import auto_retrieve, format_association_block
from sophos.memory.profile import build_profile_block
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos import runtime_config
from sophos.segment import expand_segments, has_expandable_segments
from sophos.tools.memory import MEMORY_TOOLS
from sophos.tools.onebot import ALL_TOOLS
from sophos.tools.registry import ToolRegistry
from sophos.tools.vision import VISION_TOOLS
from sophos.tools.web import WEB_TOOLS
from sophos import trigger
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

_SYSTEM_PROMPT_FILE = Path(__file__).resolve().parent.parent / "system_prompt.md"
_system_prompt_cache: str | None = None
_system_prompt_mtime: float = 0.0


def _load_system_prompt() -> str:
    """加载 system prompt。优先 runtime_config（webUI），其次文件（mtime 缓存）。"""
    global _system_prompt_cache, _system_prompt_mtime

    # webUI 接口：runtime_config 中有 system_prompt 则优先使用
    db_prompt = runtime_config.get("system_prompt")
    if db_prompt is not None:
        return db_prompt

    # 文件模式：mtime 变化时自动重载
    try:
        current_mtime = _SYSTEM_PROMPT_FILE.stat().st_mtime
    except FileNotFoundError:
        if _system_prompt_cache is None:
            logger.warning("system_prompt.md not found, using fallback")
            _system_prompt_cache = "你是 {nickname}，一个活跃在 QQ 群聊中的猫娘。回复时请自然、简洁。"
        return _system_prompt_cache

    if _system_prompt_cache is None or current_mtime != _system_prompt_mtime:
        _system_prompt_cache = _SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
        _system_prompt_mtime = current_mtime
        logger.info("Loaded system prompt from %s (mtime=%.0f)", _SYSTEM_PROMPT_FILE, current_mtime)

    return _system_prompt_cache


def clear_system_prompt_cache() -> None:
    """清除 system prompt 缓存，下次触发时重新读取。"""
    global _system_prompt_cache, _system_prompt_mtime
    _system_prompt_cache = None
    _system_prompt_mtime = 0.0


# ── 最近动态 ──────────────────────────────────────────────


def _format_recent_global_block(
    rows: list[dict[str, Any]],
    nickname: str,
) -> str:
    """将跨上下文最近消息格式化为 [最近动态] 块。

    按 (message_type, group_id/user_id) 分组，每组内时间正序。
    """
    from collections import defaultdict

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("message_type") == "group":
            key = f"group:{r.get('group_id', 0)}"
        else:
            key = f"private:{r.get('user_id', 0)}"
        groups[key].append(r)

    sections: list[str] = []
    for key, msgs in groups.items():
        kind, id_str = key.split(":", 1)
        if kind == "group":
            header = f"## 群聊 {id_str}"
        else:
            header = f"## 私聊 {id_str}"

        lines: list[str] = [header]
        for m in msgs:
            ts = format_timestamp(m)
            if m.get("source") == "sophos":
                name = nickname
            else:
                name = get_display_name(m)
            uid = m.get("user_id", "")
            text = m.get("plain_text", "")
            if m.get("source") == "sophos":
                lines.append(f"[{ts}] {name}：{text}")
            else:
                lines.append(f"[{ts}] {name}({uid})：{text}")
        sections.append("\n".join(lines))

    return "[最近动态]\n" + "\n\n".join(sections)


def _get_registry() -> ToolRegistry:
    """获取或创建 ToolRegistry 单例。"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
        for tool in ALL_TOOLS + MEMORY_TOOLS + VISION_TOOLS + WEB_TOOLS:
            _tool_registry.register(tool)
    return _tool_registry


_RAW_TOOL_CALL_RE = re.compile(
    r"^(send_msg|set_profile_self|set_profile_context|set_profile_user|write_memory|"
    r"search_memory|delete_memory|correct_image_description|"
    r"query_messages|set_group_name|web_search|web_fetch|view_image)\s*[\(\{]",
)


_RE_REPLY_PREFIX = re.compile(r"^\[回复[^\]]*\]\s*")


def _sanitize_fallback_reply(text: str) -> str | None:
    """如果 fallback 文本像 raw tool call，尝试提取实际回复；无法提取则返回 None。

    Gemini 有时会把工具调用以文本形式输出而非使用 function calling 机制。
    """
    stripped = _RE_REPLY_PREFIX.sub("", text).strip()

    # 以工具名开头 → 大概率是 raw tool call
    m = _RAW_TOOL_CALL_RE.match(stripped)
    if m:
        tool_name = m.group(1)
        if tool_name == "send_msg":
            json_m = re.search(r"\{.*\}", stripped, re.DOTALL)
            if json_m:
                try:
                    data = json.loads(json_m.group())
                    if isinstance(data, dict) and isinstance(data.get("text"), str):
                        logger.info("Extracted reply text from raw send_msg call")
                        return data["text"]
                except (json.JSONDecodeError, TypeError):
                    pass
        logger.warning("Fallback content looks like raw tool call (%s), skipping", m.group(1))
        return None

    # 纯 JSON 对象 → 可能是 send_msg 参数
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                logger.info("Extracted reply text from JSON fallback content")
                return data["text"]
        except (json.JSONDecodeError, TypeError):
            pass
        logger.warning("Fallback content is raw JSON, skipping")
        return None

    return text


async def _handle_llm_trigger(ctx: PipelineContext) -> None:
    """构建上下文 → tool loop → LLM 通过 send_msg tool 回复。"""
    provider = ctx.provider_mgr.get_provider()
    registry = _get_registry()

    nickname = settings.bot_nickname or "Sophos"
    fmt_desc = describe_schema(runtime_config.get("llm_user_schema"))
    tz = timezone(timedelta(hours=settings.timezone_offset))
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    tz_label = f"UTC+{settings.timezone_offset}" if settings.timezone_offset >= 0 else f"UTC{settings.timezone_offset}"
    if ctx.message_type == "group":
        meta = (
            f"\n---\n"
            f"当前时间：{now_str} ({tz_label})\n"
            f"当前会话：群聊 | 群号: {ctx.group_id} | 你的QQ: {ctx.self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    else:
        meta = (
            f"\n---\n"
            f"当前时间：{now_str} ({tz_label})\n"
            f"当前会话：私聊 | 对方QQ: {ctx.user_id} | 你的QQ: {ctx.self_id}\n"
            f"消息格式：{fmt_desc}"
        )
    system_prompt = _load_system_prompt().replace("{nickname}", nickname)

    # ── 记忆注入 ──
    memory_store = ctx.state.get("memory_store")
    if memory_store:
        # 保持 embedding provider 同步
        memory_store.update_embedding_provider(ctx.provider_mgr.get_embedding_provider())

        scope_type = "group" if ctx.message_type == "group" else "private"
        scope_id: int = ctx.group_id if ctx.message_type == "group" else ctx.user_id  # type: ignore[assignment]
        if scope_id is None:
            scope_id = ctx.user_id

        # 获取最近消息用于档案和联想
        recent_rows = await ctx.store.get_context(
            group_id=ctx.group_id,
            user_id=ctx.user_id if ctx.message_type == "private" else None,
        )
        context_user_ids = list({r["user_id"] for r in recent_rows if r.get("user_id")})
        context_text = "\n".join(r.get("plain_text", "") for r in recent_rows if r.get("plain_text"))

        # 档案块
        try:
            profile_block = await build_profile_block(
                memory_store,
                scope_type=scope_type,
                scope_id=scope_id,
                context_user_ids=context_user_ids,
                context_text=context_text,
            )
            if profile_block:
                system_prompt += f"\n---\n{profile_block}"
        except Exception:
            logger.warning("Failed to build profile block", exc_info=True)

        # 联想块（迁移期间跳过）
        embed_provider = ctx.provider_mgr.get_embedding_provider()
        if embed_provider:
            pool = get_pool()
            cfg = await pool.fetchrow("SELECT migration_status FROM embedding_config WHERE id = 1")
            is_migrating = cfg and cfg["migration_status"] == "running"
            if not is_migrating:
                try:
                    assoc_memories = await auto_retrieve(memory_store, embed_provider, recent_rows)
                    if assoc_memories:
                        system_prompt += f"\n---\n{format_association_block(assoc_memories)}"
                except Exception:
                    logger.warning("Failed to retrieve associations", exc_info=True)

    # ── 最近动态注入 ──
    try:
        recent_global = await ctx.store.get_recent_global(
            exclude_group_id=ctx.group_id if ctx.message_type == "group" else None,
            exclude_private_user_id=ctx.user_id if ctx.message_type == "private" else None,
            limit=runtime_config.get("recent_global_limit"),
            min_self_messages=runtime_config.get("recent_global_min_self"),
        )
        if recent_global:
            block = _format_recent_global_block(recent_global, nickname)
            system_prompt += f"\n---\n{block}"
    except Exception:
        logger.warning("Failed to build recent global block", exc_info=True)

    system_prompt += meta

    # 等待图片处理完成（如有），确保 LLM 能拿到图片描述
    image_task: asyncio.Task[None] | None = ctx.state.get("image_task")
    if image_task and not image_task.done():
        try:
            await image_task
        except Exception:
            logger.warning("Image processing failed, continuing without descriptions")

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
    if memory_store:
        tool_context["memory_store"] = memory_store
        tool_context["embedding_provider"] = ctx.provider_mgr.get_embedding_provider()
    vision_provider = ctx.provider_mgr.get_vision_provider()
    if vision_provider:
        tool_context["vision_provider"] = vision_provider
    sent_via_tool = False

    async def tool_executor(name: str, params: dict[str, Any]) -> Any:
        nonlocal sent_via_tool
        result = await registry.execute(name, params, tool_context)
        if name == "send_msg":
            sent_via_tool = True
        return result

    tool_schemas = registry.get_function_schemas(scope=ctx.message_type)

    # 工具白名单过滤
    from sophos import permission as _perm
    _pool = get_pool()
    _scope_type = "group" if ctx.message_type == "group" else "private"
    _scope_id: int = ctx.group_id if ctx.message_type == "group" else ctx.user_id  # type: ignore[assignment]
    whitelist = await _perm.get_tool_whitelist(_pool, _scope_type, _scope_id)
    if whitelist is not None:
        allowed = set(whitelist)
        tool_schemas = [s for s in tool_schemas if s["function"]["name"] in allowed]

    # 条件工具过滤（API key / provider 未配置时隐藏）
    _disabled: set[str] = set()
    if not runtime_config.get("tavily_api_key"):
        _disabled.update({"web_search", "web_fetch"})
    if not vision_provider:
        _disabled.add("view_image")
    if _disabled:
        tool_schemas = [s for s in tool_schemas if s["function"]["name"] not in _disabled]

    try:
        result_messages = await run_tool_loop(
            provider,
            messages,
            tools=tool_schemas,
            tool_executor=tool_executor,
            max_rounds=runtime_config.get("llm_max_tool_rounds"),
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

        reply_text = _sanitize_fallback_reply(reply_text)
        if not reply_text:
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
        ctx.state["image_task"] = asyncio.create_task(_process_event_images(ctx))
        await next()


class EnrichMessageStage(Stage):
    """展开消息段（@、回复、转发、卡片等）为富文本。"""

    @property
    def name(self) -> str:
        return "enrich_message"

    @property
    def description(self) -> str:
        return "展开消息段（@、回复、转发、卡片等）为富文本"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        if not has_expandable_segments(ctx.segments):
            await next()
            return
        try:
            enriched = await expand_segments(
                ctx.segments,
                api=ctx.api,
                store=ctx.store,
                session=ctx.session,
                pool=ctx.store.pool,
                vision_provider=ctx.provider_mgr.get_vision_provider(),
                group_id=ctx.group_id,
            )
            if enriched != ctx.text:
                message_id = ctx.event.get("message_id")
                if message_id is not None:
                    await ctx.store.update_plain_text(message_id, enriched)
                ctx.text = enriched
        except Exception:
            logger.warning("Message enrichment failed", exc_info=True)
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


def _strip_at_prefix(ctx: PipelineContext) -> str:
    """去除消息开头的 @bot，使 '@sophos .help' 等命令正常工作。"""
    text = ctx.text
    if not ctx.segments:
        return text
    first = ctx.segments[0]
    if first.get("type") != "at":
        return text
    if str(first.get("data", {}).get("qq")) != str(ctx.self_id):
        return text
    m = re.match(r"^@\S+\s*", text)
    return text[m.end():] if m else text


class CheckScopeStage(Stage):
    """检查会话是否启用。禁用时仅放行 .bot 命令（按权限）。"""

    @property
    def name(self) -> str:
        return "check_scope"

    @property
    def description(self) -> str:
        return "检查会话开关（权限系统）"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        from sophos import permission as _perm

        scope_type = "group" if ctx.message_type == "group" else "private"
        scope_id: int = ctx.group_id if ctx.message_type == "group" else ctx.user_id  # type: ignore[assignment]
        pool = get_pool()

        # 会话已启用 → 放行
        if await _perm.is_scope_enabled(pool, scope_type, scope_id):
            await next()
            return

        # 会话禁用 — 仅放行 .bot 命令
        cmd_text = _strip_at_prefix(ctx)
        if not cmd_text.startswith(".bot"):
            return  # 静默丢弃

        parts = cmd_text.split()
        sub = parts[1] if len(parts) > 1 else ""

        # .bot（查看状态）和 .bot off — 无条件放行
        if sub in ("", "off"):
            ctx.state["_cmd_text"] = cmd_text
            await next()
            return

        # .bot on — 需要权限
        if sub == "on":
            if _perm.is_master(ctx.user_id):
                ctx.state["_cmd_text"] = cmd_text
                await next()
                return
            # bot 权限（全局开关权限）
            if await _perm.has_permission(pool, ctx.user_id, "global", 0, "bot"):
                ctx.state["_cmd_text"] = cmd_text
                await next()
                return
            # 群聊：bot.group 权限
            if scope_type == "group" and await _perm.has_permission(
                pool, ctx.user_id, scope_type, scope_id, "bot.group",
            ):
                ctx.state["_cmd_text"] = cmd_text
                await next()
                return
            # 私聊：bot.private 权限
            if scope_type == "private" and await _perm.has_permission(
                pool, ctx.user_id, "global", 0, "bot.private",
            ):
                ctx.state["_cmd_text"] = cmd_text
                await next()
                return
        # 无权限 → 静默丢弃


class HandleCommandStage(Stage):
    """处理 dot 命令（权限检查 + 分发）。"""

    _COMMAND_PERMS: dict[str, str] = {
        ".tools": "cmd.tools",
        ".llm": "cmd.llm", ".trigger": "cmd.trigger",
        ".memory": "cmd.memory", ".config": "cmd.config",
        ".prompt": "cmd.prompt", ".perm": "delegate",
    }

    @property
    def name(self) -> str:
        return "handle_command"

    @property
    def description(self) -> str:
        return "处理 dot 命令（权限检查 + 分发）"

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        from sophos import permission as _perm

        text = ctx.state.get("_cmd_text") or _strip_at_prefix(ctx)
        pool = get_pool()
        scope_type = "group" if ctx.message_type == "group" else "private"
        scope_id: int = ctx.group_id if ctx.message_type == "group" else ctx.user_id  # type: ignore[assignment]

        # 无需权限
        if text == ".ping":
            await self._handle_ping(ctx)
            return
        if text.startswith(".help"):
            await handle_help_command(
                text, api=ctx.api, event=ctx.event, pool=pool,
                user_id=ctx.user_id, scope_type=scope_type, scope_id=scope_id,
            )
            return

        # .bot 命令 — 特殊权限逻辑
        if text.startswith(".bot"):
            parts = text.split()
            sub = parts[1] if len(parts) > 1 else ""
            # .bot（状态查看）和 .bot off — 无需权限
            if sub in ("", "off"):
                await self._dispatch(
                    ".bot", text, ctx=ctx, pool=pool,
                    scope_type=scope_type, scope_id=scope_id,
                )
                return
            # .bot on — 需要 bot / bot.group / bot.private 权限
            if sub == "on":
                can = (
                    _perm.is_master(ctx.user_id)
                    or await _perm.has_permission(pool, ctx.user_id, "global", 0, "bot")
                    or (scope_type == "group" and await _perm.has_permission(
                        pool, ctx.user_id, scope_type, scope_id, "bot.group"))
                    or (scope_type == "private" and await _perm.has_permission(
                        pool, ctx.user_id, "global", 0, "bot.private"))
                )
                if not can:
                    await self._send_text(ctx, "权限不足")
                    return
            await self._dispatch(
                ".bot", text, ctx=ctx, pool=pool,
                scope_type=scope_type, scope_id=scope_id,
            )
            return

        # 权限检查 + 分发
        for prefix, perm in self._COMMAND_PERMS.items():
            if text.startswith(prefix):
                if not await _perm.has_permission(pool, ctx.user_id, scope_type, scope_id, perm):
                    await self._send_text(ctx, "权限不足")
                    return
                await self._dispatch(
                    prefix, text, ctx=ctx, pool=pool,
                    scope_type=scope_type, scope_id=scope_id,
                )
                return

        await next()

    async def _dispatch(
        self, prefix: str, text: str, *, ctx: PipelineContext,
        pool: asyncpg.Pool, scope_type: str, scope_id: int,
    ) -> None:
        if prefix == ".bot":
            await handle_bot_command(
                text, api=ctx.api, event=ctx.event, pool=pool,
                scope_type=scope_type, scope_id=scope_id,
            )
        elif prefix == ".tools":
            registry = _get_registry()
            all_names = [s["function"]["name"] for s in registry.get_function_schemas()]
            await handle_tools_command(
                text, api=ctx.api, event=ctx.event, pool=pool,
                scope_type=scope_type, scope_id=scope_id, all_tool_names=all_names,
            )
        elif prefix == ".llm":
            await handle_llm_command(
                text, api=ctx.api, event=ctx.event, provider_mgr=ctx.provider_mgr,
            )
        elif prefix == ".trigger":
            await handle_trigger_command(
                text, api=ctx.api, event=ctx.event, pool=pool,
                provider_mgr=ctx.provider_mgr,
            )
        elif prefix == ".memory":
            await handle_memory_command(
                text, api=ctx.api, event=ctx.event,
                provider_mgr=ctx.provider_mgr, pool=pool,
            )
        elif prefix == ".config":
            await handle_config_command(
                text, api=ctx.api, event=ctx.event,
            )
        elif prefix == ".prompt":
            await handle_prompt_command(
                text, api=ctx.api, event=ctx.event,
            )
        elif prefix == ".perm":
            await handle_perm_command(
                text, api=ctx.api, event=ctx.event, pool=pool,
                user_id=ctx.user_id, scope_type=scope_type,
                scope_id=scope_id, group_id=ctx.group_id,
            )

    async def _send_text(self, ctx: PipelineContext, text: str) -> None:
        params: dict[str, Any] = {
            "message_type": ctx.message_type,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        if ctx.message_type == "group":
            params["group_id"] = ctx.group_id
        else:
            params["user_id"] = ctx.user_id
        await ctx.api.call("send_msg", params)

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
    """概率触发 LLM 对话（委托给 TriggerEngine）。"""

    @property
    def name(self) -> str:
        return "trigger_llm"

    @property
    def description(self) -> str:
        return "概率触发 LLM 对话"

    def _is_at_bot(self, ctx: PipelineContext) -> bool:
        self_qq = str(ctx.self_id)
        return any(
            seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == self_qq
            for seg in ctx.segments
        )

    async def execute(self, ctx: PipelineContext, next: NextFn) -> None:
        from sophos.trigger_engine import get_trigger_engine

        pool = get_pool()
        cfg = await trigger.load(pool)
        engine = get_trigger_engine()

        # 计算简易触发
        simple_triggered = False

        if ctx.message_type == "private":
            simple_triggered = True
            reason = "private"
        elif cfg.at_always and self._is_at_bot(ctx):
            simple_triggered = True
            reason = "@bot"
        else:
            max_boost = 0.0
            matched = ""
            for kw in cfg.keywords:
                if kw.word in ctx.text:
                    if kw.boost > max_boost:
                        max_boost = kw.boost
                        matched = kw.word
            rate = min(1.0, cfg.base_rate + max_boost)
            reason = f"keyword '{matched}'" if matched else "base"

            roll = random.random()
            if roll < rate:
                simple_triggered = True
                logger.debug(
                    "Simple trigger fired: reason=%s rate=%.3f roll=%.3f",
                    reason, rate, roll,
                )
            else:
                logger.debug(
                    "Simple trigger skipped: reason=%s rate=%.3f roll=%.3f",
                    reason, rate, roll,
                )

        await engine.on_message(ctx, simple_triggered=simple_triggered)


# ── 默认 stage 列表 ──────────────────────────────────────────

DEFAULT_STAGES: list[Stage] = [
    StoreMessageStage(),
    EnrichMessageStage(),
    ProcessImagesStage(),
    FilterSelfStage(),
    CheckScopeStage(),
    HandleCommandStage(),
    TriggerLLMStage(),
]
