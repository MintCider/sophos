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
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sophos import observability, prompts, runtime_config, trigger_context
from sophos.agent.runs import get_run_tracker
from sophos.enrichment import get_enrichment_registry

if TYPE_CHECKING:
    from sophos.pipeline import PipelineContext

logger = logging.getLogger(__name__)

# ── 类型 ──────────────────────────────────────────────────────

ContextKey = tuple[str, int]  # ("conversation", internal conversation_id)


def _context_key(ctx: PipelineContext) -> ContextKey:
    return ("conversation", ctx.conversation_id)


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
    last_eval_id: int = 0  # 上次评估时可见的最大消息 id
    pending_delay: asyncio.Task[None] | None = None
    evaluation_event_id: int | None = None


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
        cap = float(runtime_config.get("trigger_bucket_capacity", 10))
        refill = float(runtime_config.get("trigger_bucket_refill", 6.0))
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(
                tokens=cap,
                max_tokens=cap,
                refill_interval=refill,
                last_refill=time.monotonic(),
            )
        bucket.max_tokens = cap
        bucket.refill_interval = refill
        return bucket

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
        event_id = await observability.trigger_event(ctx.conversation_id, "simple", ctx.state.get("message_row_id"))
        if cs.pending_delay is not None and not cs.pending_delay.done():
            await observability.update_trigger(event_id, status="coalesced")
            logger.debug("Delayed trigger already pending for %s, skipping", key)
            return

        cs.pending_delay = asyncio.create_task(self._delayed_respond(ctx, key, event_id))
        logger.debug("Scheduled delayed trigger for %s", key)

    async def _delayed_respond(self, ctx: PipelineContext, key: ContextKey, event_id: int | None = None) -> None:
        """等待 trigger_delay 秒后消耗 quota 并触发主 LLM。"""
        delay = float(runtime_config.get("trigger_delay", 2.0))
        await asyncio.sleep(delay)

        # 清除 pending 标记
        cs = self._get_state(key)
        cs.pending_delay = None

        logger.debug("Delayed trigger firing for %s after %.1fs", key, delay)
        await self._respond(ctx, key, event_id)

    async def _respond(self, ctx: PipelineContext, key: ContextKey, event_id: int | None) -> None:
        if not self._get_bucket(key).try_consume():
            await observability.update_trigger(event_id, status="rate_limited")
            return
        lock = self._get_lock(key)
        if lock.locked():
            await observability.update_trigger(event_id, status="locked")
            return
        async with lock:
            from sophos.pipeline import _handle_llm_trigger

            trace = observability.ResponseTrace()
            with observability.scope(conversation_id=ctx.conversation_id, trigger_event_id=event_id, response=trace):
                await observability.update_trigger(event_id, status="responding")
                try:
                    await _handle_llm_trigger(ctx)
                except (Exception, asyncio.CancelledError) as exc:
                    trace.failed = True
                    await observability.record_error("workflow", exc)
                    raise
                finally:
                    status = "failed" if trace.failed else "replied" if trace.sent_count else "no_reply"
                    await observability.update_trigger(
                        event_id, status=status, sent_count=trace.sent_count, run_id=trace.run_id,
                    )

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
        # 整个循环共用同一个起点：重评时「没说完的那句」和续写一起放进待判断
        since_id = cs.last_eval_id

        try:
            while True:
                # 评估期间到达的新消息会保留 set 状态，进入 WAITING 后立即重评
                cs.new_message_event.clear()
                cs.state = TriggerState.EVALUATING
                self._global_qps_last = time.monotonic()
                cs.last_eval_time = time.monotonic()

                result = await self._evaluate(ctx, key, since_id=since_id, waiting=is_reeval)
                logger.debug("LLM trigger eval for %s: %s (reeval=%s)", key, result, is_reeval)

                if result == "irrelevant":
                    cs.state = TriggerState.IDLE
                    return

                if result == "relevant_done":
                    cs.state = TriggerState.TRIGGERED
                    logger.debug("LLM trigger firing main LLM for %s", key)
                    cs.state = TriggerState.RESPONDING
                    await self._respond(ctx, key, cs.evaluation_event_id)
                    cs.state = TriggerState.IDLE
                    return

                # relevant_more → WAITING
                cs.state = TriggerState.WAITING
                timeout = float(runtime_config.get("trigger_wait_timeout", 30.0))
                logger.debug("Entering WAITING for %s (timeout=%.1fs)", key, timeout)

                try:
                    await asyncio.wait_for(cs.new_message_event.wait(), timeout=timeout)
                except TimeoutError:
                    # 超时无新消息 → 触发
                    logger.debug("WAITING timeout for %s, triggering", key)
                    cs.state = TriggerState.TRIGGERED
                    await observability.update_trigger(cs.evaluation_event_id, status="waiting_timeout")
                    event_id = await observability.trigger_event(
                        ctx.conversation_id, "timeout", cs.last_eval_id or None,
                    )
                    cs.state = TriggerState.RESPONDING
                    await self._respond(ctx, key, event_id)
                    cs.state = TriggerState.IDLE
                    return

                # 新消息到达，遵守 QPS 后 re-eval
                logger.debug("WAITING got new message for %s, will re-eval", key)
                qps = float(runtime_config.get("trigger_qps", 0.5))
                min_interval = 1.0 / qps if qps > 0 else 2.0
                elapsed = time.monotonic() - cs.last_eval_time
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
                is_reeval = True
                await observability.update_trigger(cs.evaluation_event_id, status="reevaluated")
                # 循环回到 EVALUATING

        except asyncio.CancelledError:
            await observability.update_trigger(cs.evaluation_event_id, status="interrupted")
            raise
        except Exception as exc:
            await observability.record_error("trigger_loop", exc)
            logger.exception("LLM trigger loop error for %s", key)
        finally:
            cs.state = TriggerState.IDLE

    @staticmethod
    async def _embedding_provider(ctx: PipelineContext) -> Any:
        """embedding 迁移期间向量维度不一致，跳过检索。"""
        provider = ctx.provider_mgr.get_embedding_provider()
        if provider is None:
            return None
        status = await ctx.store.pool.fetchval("SELECT migration_status FROM embedding_config WHERE id = 1")
        return None if status == "running" else provider

    async def _evaluate(
        self,
        ctx: PipelineContext,
        key: ContextKey,
        *,
        since_id: int,
        waiting: bool = False,
    ) -> str:
        """调用 trigger provider 评估是否回复。

        返回: "irrelevant" | "relevant_done" | "relevant_more"
        """
        provider = ctx.provider_mgr.get_trigger_provider()
        if provider is None:
            return "irrelevant"

        cs = self._get_state(key)
        cs.evaluation_event_id = None
        rows = await trigger_context.load_rows(ctx.store, ctx.conversation_id)
        pending = trigger_context.pending_rows(rows, since_id=since_id, self_user_id=ctx.self_user_id)
        # 待判断消息带图时等识别结果；更早的图不等
        wait = float(runtime_config.get("trigger_eval_image_wait", 8.0))
        if pending and wait > 0:
            ids = [int(r["id"]) for r in pending]
            if await get_enrichment_registry().wait_for(ids, timeout=wait):
                rows = await trigger_context.load_rows(ctx.store, ctx.conversation_id)

        tc = trigger_context.build_trigger_context(rows, self_user_id=ctx.self_user_id, since_id=since_id)
        if tc is None:
            return "irrelevant"
        cs.last_eval_id = tc.latest_id

        # system：persona（含档案 / 记忆变量）+ 判断规则与输出格式
        values = await trigger_context.persona_values(
            ctx.state.get("memory_store"),
            await self._embedding_provider(ctx),
            conversation_id=ctx.conversation_id,
            query=tc.query,
        )
        persona = prompts.render("trigger.persona", values).text.strip()
        fmt = prompts.get("trigger.waiting_format" if waiting else "trigger.eval_format")
        system_prompt = f"{persona}\n\n---\n\n{fmt}" if persona else fmt
        logger.debug("Trigger eval prompt for %s:\n%s\n=====\n%s", key, system_prompt, tc.text)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": tc.text},
        ]

        max_tokens = int(runtime_config.get("trigger_eval_max_tokens", 64))
        temperature = float(runtime_config.get("trigger_eval_temperature", 0.0))
        event_id = await observability.trigger_event(
            ctx.conversation_id, "llm", tc.latest_id, waiting=waiting,
        )
        cs.evaluation_event_id = event_id
        started = time.monotonic()
        try:
            response = await observability.chat(
                provider,
                messages,
                slot="trigger",
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            await observability.update_trigger(
                event_id, status="failed", duration_ms=round((time.monotonic() - started) * 1000),
            )
            logger.warning("Trigger eval failed for %s", key, exc_info=True)
            return "irrelevant"

        content = (response["message"].get("content") or "").strip().lower()
        logger.debug("Trigger eval raw response for %s: %r", key, content)
        result = _parse_result(content)
        status = {"irrelevant": "ignored", "relevant_more": "waiting", "relevant_done": "pending"}[result]
        if not _RE_RESULT.search(content) or len(set(_RE_RESULT.findall(content))) != 1:
            status = "invalid"
        await observability.update_trigger(
            event_id, result=result, raw_result=content[:512], status=status,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        get_run_tracker().stage("trigger", ctx.conversation_id, {"result": result, "waiting": waiting})
        return result


_RE_RESULT = re.compile(r"(?<![a-z_])(irrelevant|relevant_done|relevant_more)(?![a-z_])")


def _parse_result(content: str) -> str:
    """只认完整的结果词；出现多个不同结果词时视为无效。"""
    found = set(_RE_RESULT.findall(content))
    if len(found) == 1:
        return found.pop()
    return "irrelevant"


# ── 单例 ──────────────────────────────────────────────────────

_engine: TriggerEngine | None = None


def get_trigger_engine() -> TriggerEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = TriggerEngine()
    return _engine
