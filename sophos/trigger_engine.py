"""触发器引擎。

三个功能：
  1. 延迟积累 — 简易触发后等待 trigger_delay 秒再响应
  2. 轻量级 LLM 触发器 — 简易触发未命中时用轻量 LLM 判断
  3. Per-context 频率限制 — Token bucket 控制回复频率
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sophos import runtime_config
from sophos.config import settings

if TYPE_CHECKING:
    from sophos.pipeline import PipelineContext

logger = logging.getLogger(__name__)

# ── 硬编码格式指令（不可通过 runtime_config 修改）────────────

_EVAL_FORMAT = (
    "接下来你会收到一系列群聊对话。大多数消息与你无关，请保守判断。\n"
    "只有当最新消息明确满足以下任一条件时才算 relevant：\n"
    "1. 直接提到你的名字或称呼\n"
    "2. 明确在回应你之前说的话（话题连贯且有指向性）\n"
    "3. 你在人设中被要求关注的话题\n"
    "不确定时一律选 irrelevant。\n"
    "如果 relevant，再判断这条消息是否完整"
    "（句子明显被截断或只说了半句 → more；问句、短句、完整表达 → done）。\n"
    "只回复以下三个词之一：irrelevant / relevant_done / relevant_more"
)

_WAITING_FORMAT = (
    "接下来你会收到一系列群聊对话。之前的消息被判定为与你有关，但对方还没说完。\n"
    "现在有新消息到达，请重新判断：\n"
    "- 如果最新消息其实与你无关 → irrelevant\n"
    "- 如果与你有关且这条消息是完整的表达 → relevant_done\n"
    "- 如果与你有关但这条消息明显没说完 → relevant_more\n"
    "不确定时选 irrelevant。\n"
    "只回复以下三个词之一：irrelevant / relevant_done / relevant_more"
)

# ── 类型 ──────────────────────────────────────────────────────

ContextKey = tuple[str, int]  # ("group", group_id) | ("private", user_id)


def _context_key(ctx: PipelineContext) -> ContextKey:
    if ctx.message_type == "group":
        return ("group", ctx.group_id)  # type: ignore[return-value]
    return ("private", ctx.user_id)


# ── Token Bucket ──────────────────────────────────────────────


@dataclass
class TokenBucket:
    """Per-context 频率限制桶。"""

    tokens: float
    max_tokens: float
    refill_interval: float  # 秒/token
    last_refill: float

    def try_consume(self) -> bool:
        """补充 token 并尝试消耗 1。"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed / self.refill_interval)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ── 状态机 ────────────────────────────────────────────────────


class TriggerState(enum.Enum):
    IDLE = "idle"
    EVALUATING = "evaluating"
    WAITING = "waiting"
    TRIGGERED = "triggered"
    RESPONDING = "responding"


@dataclass
class ContextState:
    """Per-context LLM 触发器状态。"""

    state: TriggerState = TriggerState.IDLE
    new_message_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_eval_time: float = 0.0
    pending_delay: asyncio.Task[None] | None = None


# ── 引擎 ──────────────────────────────────────────────────────


class TriggerEngine:
    """管理延迟积累、LLM 触发器、频率限制。"""

    def __init__(self) -> None:
        self._states: dict[ContextKey, ContextState] = {}
        self._buckets: dict[ContextKey, TokenBucket] = {}
        self._locks: dict[ContextKey, asyncio.Lock] = {}
        self._global_qps_last: float = 0.0

    def _get_state(self, key: ContextKey) -> ContextState:
        if key not in self._states:
            self._states[key] = ContextState()
        return self._states[key]

    def _get_lock(self, key: ContextKey) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _get_bucket(self, key: ContextKey) -> TokenBucket:
        if key not in self._buckets:
            cap = runtime_config.get("trigger_bucket_capacity", 10)
            refill = runtime_config.get("trigger_bucket_refill", 6.0)
            self._buckets[key] = TokenBucket(
                tokens=float(cap),
                max_tokens=float(cap),
                refill_interval=float(refill),
                last_refill=time.monotonic(),
            )
        return self._buckets[key]

    # ── 入口 ──────────────────────────────────────────────────

    async def on_message(
        self,
        ctx: PipelineContext,
        *,
        simple_triggered: bool,
    ) -> None:
        """TriggerLLMStage 调用入口。"""
        key = _context_key(ctx)
        logger.debug("TriggerEngine.on_message %s simple_triggered=%s", key, simple_triggered)

        if simple_triggered:
            await self._schedule_delayed(ctx, key)
        else:
            await self._feed_llm_trigger(ctx, key)

    # ── 延迟积累 ──────────────────────────────────────────────

    async def _schedule_delayed(self, ctx: PipelineContext, key: ContextKey) -> None:
        """简易触发后安排延迟响应。同一 context 去重。"""
        cs = self._get_state(key)
        if cs.pending_delay is not None and not cs.pending_delay.done():
            logger.debug("Delayed trigger already pending for %s, skipping", key)
            return

        cs.pending_delay = asyncio.create_task(self._delayed_respond(ctx, key))
        logger.debug("Scheduled delayed trigger for %s", key)

    async def _delayed_respond(self, ctx: PipelineContext, key: ContextKey) -> None:
        """等待 trigger_delay 秒后消耗 quota 并触发主 LLM。"""
        delay = float(runtime_config.get("trigger_delay", 2.0))
        await asyncio.sleep(delay)

        # 清除 pending 标记
        cs = self._get_state(key)
        cs.pending_delay = None

        bucket = self._get_bucket(key)
        if not bucket.try_consume():
            logger.debug("Rate limited (delayed), dropping for %s", key)
            return

        logger.debug("Delayed trigger firing for %s after %.1fs", key, delay)
        lock = self._get_lock(key)
        if lock.locked():
            logger.debug("Session lock held for %s, dropping delayed trigger", key)
            return
        async with lock:
            from sophos.pipeline import _handle_llm_trigger

            await _handle_llm_trigger(ctx)

    # ── LLM 触发器 ────────────────────────────────────────────

    async def _feed_llm_trigger(self, ctx: PipelineContext, key: ContextKey) -> None:
        """简易触发未命中时，尝试 LLM 触发器。"""
        provider = ctx.provider_mgr.get_trigger_provider()
        if provider is None:
            return  # 未配置 trigger provider

        cs = self._get_state(key)

        # 非 IDLE → 通知等待中的循环有新消息
        if cs.state != TriggerState.IDLE:
            cs.new_message_event.set()
            logger.debug("LLM trigger busy (%s) for %s, signaled new message", cs.state.value, key)
            return

        # 全局 QPS 检查
        qps = float(runtime_config.get("trigger_qps", 0.5))
        min_interval = 1.0 / qps if qps > 0 else 2.0
        now = time.monotonic()
        if now - self._global_qps_last < min_interval:
            logger.debug("QPS cooldown for %s, skipping", key)
            return

        # 启动评估循环
        logger.debug("Starting LLM trigger loop for %s", key)
        asyncio.create_task(self._llm_trigger_loop(ctx, key))

    async def _llm_trigger_loop(self, ctx: PipelineContext, key: ContextKey) -> None:
        """LLM 触发器状态机循环。持锁直到回到 IDLE。"""
        cs = self._get_state(key)
        is_reeval = False

        try:
            while True:
                cs.state = TriggerState.EVALUATING
                self._global_qps_last = time.monotonic()
                cs.last_eval_time = time.monotonic()

                result = await self._evaluate(ctx, key, waiting=is_reeval)
                logger.debug("LLM trigger eval for %s: %s (reeval=%s)", key, result, is_reeval)

                if result == "irrelevant":
                    cs.state = TriggerState.IDLE
                    return

                if result == "relevant_done":
                    cs.state = TriggerState.TRIGGERED
                    bucket = self._get_bucket(key)
                    if not bucket.try_consume():
                        logger.debug("Rate limited (LLM trigger), dropping for %s", key)
                        cs.state = TriggerState.IDLE
                        return
                    logger.debug("LLM trigger firing main LLM for %s", key)
                    cs.state = TriggerState.RESPONDING
                    lock = self._get_lock(key)
                    if lock.locked():
                        logger.debug("Session lock held for %s, dropping LLM trigger", key)
                        cs.state = TriggerState.IDLE
                        return
                    async with lock:
                        from sophos.pipeline import _handle_llm_trigger

                        await _handle_llm_trigger(ctx)
                    cs.state = TriggerState.IDLE
                    return

                # relevant_more → WAITING
                cs.state = TriggerState.WAITING
                cs.new_message_event.clear()
                timeout = float(runtime_config.get("trigger_wait_timeout", 30.0))
                logger.debug("Entering WAITING for %s (timeout=%.1fs)", key, timeout)

                try:
                    await asyncio.wait_for(cs.new_message_event.wait(), timeout=timeout)
                except TimeoutError:
                    # 超时无新消息 → 触发
                    logger.debug("WAITING timeout for %s, triggering", key)
                    cs.state = TriggerState.TRIGGERED
                    bucket = self._get_bucket(key)
                    if not bucket.try_consume():
                        cs.state = TriggerState.IDLE
                        return
                    cs.state = TriggerState.RESPONDING
                    lock = self._get_lock(key)
                    if lock.locked():
                        logger.debug("Session lock held for %s, dropping timeout trigger", key)
                        cs.state = TriggerState.IDLE
                        return
                    async with lock:
                        from sophos.pipeline import _handle_llm_trigger

                        await _handle_llm_trigger(ctx)
                    cs.state = TriggerState.IDLE
                    return

                # 新消息到达，遵守 QPS 后 re-eval
                logger.debug("WAITING got new message for %s, will re-eval", key)
                qps = float(runtime_config.get("trigger_qps", 0.5))
                min_interval = 1.0 / qps if qps > 0 else 2.0
                elapsed = time.monotonic() - cs.last_eval_time
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
                cs.new_message_event.clear()
                is_reeval = True
                # 循环回到 EVALUATING

        except Exception:
            logger.exception("LLM trigger loop error for %s", key)
            cs.state = TriggerState.IDLE

    async def _evaluate(self, ctx: PipelineContext, key: ContextKey, *, waiting: bool = False) -> str:
        """调用 trigger provider 评估是否回复。

        返回: "irrelevant" | "relevant_done" | "relevant_more"
        """
        provider = ctx.provider_mgr.get_trigger_provider()
        if provider is None:
            return "irrelevant"

        limit = int(runtime_config.get("trigger_eval_context_limit", 20))
        rows = await ctx.store.get_context(
            group_id=ctx.group_id if ctx.message_type == "group" else None,
            user_id=ctx.user_id if ctx.message_type == "private" else None,
            limit=limit,
        )

        # 构建评估消息
        lines: list[str] = []
        for r in rows:
            text = r.get("plain_text", "")
            if text:
                name = r.get("card") or r.get("nickname") or str(r.get("user_id", ""))
                lines.append(f"{name}: {text}")

        if not lines:
            return "irrelevant"

        # 拼接 prompt：persona + 格式指令
        persona = str(runtime_config.get("trigger_eval_persona", ""))
        nickname = settings.bot_nickname or "Sophos"
        if persona:
            persona = persona.replace("{nickname}", nickname)
        fmt = _WAITING_FORMAT if waiting else _EVAL_FORMAT
        system_prompt = f"{persona}\n\n{fmt}" if persona else fmt

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(lines)},
        ]

        max_tokens = int(runtime_config.get("trigger_eval_max_tokens", 64))
        temperature = float(runtime_config.get("trigger_eval_temperature", 0.0))
        try:
            response = await provider.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            logger.warning("Trigger eval failed for %s", key, exc_info=True)
            return "irrelevant"

        content = (response["message"].get("content") or "").strip().lower()
        logger.debug("Trigger eval raw response for %s: %r", key, content)

        if "relevant_done" in content or "done" in content:
            return "relevant_done"
        if "relevant_more" in content or "more" in content:
            return "relevant_more"
        return "irrelevant"


# ── 单例 ──────────────────────────────────────────────────────

_engine: TriggerEngine | None = None


def get_trigger_engine() -> TriggerEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = TriggerEngine()
    return _engine
