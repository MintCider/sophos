"""Persistent call/trigger telemetry; request context follows asyncio child tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import aiohttp
import asyncpg

logger = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None


@dataclass
class ResponseTrace:
    sent_count: int = 0
    failed: bool = False
    run_id: str | None = None


@dataclass(frozen=True)
class StatsContext:
    conversation_id: int | None = None
    run_id: str | None = None
    trigger_event_id: int | None = None
    response: ResponseTrace | None = None


_EMPTY_CONTEXT = StatsContext()
_context = ContextVar("stats_context", default=_EMPTY_CONTEXT)


async def init(pool: asyncpg.Pool) -> None:
    global _pool  # noqa: PLW0603
    _pool = pool
    await pool.execute(
        "UPDATE trigger_events SET status = 'interrupted', updated_at = now() "
        "WHERE status IN ('pending', 'responding', 'waiting')"
    )


@contextmanager
def scope(**values: Any):
    token = _context.set(replace(_context.get(), **values))
    try:
        yield _context.get()
    finally:
        _context.reset(token)


async def _write(method: str, sql: str, *args: Any) -> Any:
    if _pool is None:
        return None
    try:
        return await getattr(_pool, method)(sql, *args)
    except Exception:
        # Statistics must not turn a successful delivery into a retry.
        logger.warning("Could not persist telemetry", exc_info=True)
        return None


def error_summary(exc: BaseException) -> str:
    # Do not persist upstream response bodies, request URLs, or credentials.
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"{type(exc).__name__}: HTTP {exc.status}"
    if isinstance(exc, TimeoutError):
        return "TimeoutError: 请求超时"
    if isinstance(exc, asyncio.CancelledError):
        return "CancelledError: 调用被取消或超时终止"
    return type(exc).__name__


async def record_error(stage: str, exc: BaseException) -> None:
    ctx = _context.get()
    await _write(
        "execute",
        "INSERT INTO runtime_errors (conversation_id, run_id, stage, error) VALUES ($1, $2, $3, $4)",
        ctx.conversation_id, ctx.run_id, stage, error_summary(exc),
    )


def normalize_usage(usage: dict[str, Any] | None, provider_kind: str) -> dict[str, int | None]:
    keys = ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens")
    if not usage:
        return dict.fromkeys(keys)
    raw = usage.get("raw") or {}
    cached = usage.get("cached_input_tokens", 0) or 0
    written = usage.get("cache_write_tokens", 0) or 0
    inputs = usage.get("prompt_tokens", 0) or 0
    outputs = usage.get("completion_tokens", 0) or 0
    reasoning = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    if provider_kind == "anthropic":
        # Anthropic input_tokens excludes cache reads/writes, unlike OpenAI.
        inputs += cached + written
    if provider_kind == "gemini":
        reasoning = raw.get("thoughtsTokenCount", 0) or 0
        outputs += reasoning
    total = usage.get("total_tokens")
    if total is None or (total == 0 and inputs + outputs > 0) or provider_kind == "anthropic":
        total = inputs + outputs
    return dict(zip(keys, (inputs, outputs, cached, written, reasoning, total), strict=True))


def api_usage(data: dict[str, Any], api_type: str, *, embedding: bool = False) -> dict[str, Any] | None:
    raw = data.get("usageMetadata") if api_type == "gemini" else data.get("usage")
    if not raw:
        return None
    if api_type == "gemini":
        return {
            "prompt_tokens": raw.get("promptTokenCount", 0),
            "completion_tokens": raw.get("candidatesTokenCount", 0),
            "total_tokens": raw.get("totalTokenCount"),
            "cached_input_tokens": raw.get("cachedContentTokenCount", 0),
            "raw": raw,
        }
    inputs = raw.get("input_tokens", raw.get("prompt_tokens"))
    if inputs is None and embedding:
        inputs = raw.get("total_tokens")
    details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": inputs or 0,
        "completion_tokens": raw.get("output_tokens", raw.get("completion_tokens", 0)),
        "total_tokens": raw.get("total_tokens"),
        "cached_input_tokens": details.get("cached_tokens", 0),
        "cache_write_tokens": details.get("cache_write_tokens", 0),
        "raw": raw,
    }


@dataclass
class ModelCall:
    usage: dict[str, Any] | None = None
    provider_kind: str = ""


@asynccontextmanager
async def model_call(*, slot: str, provider: str, model: str, api_type: str = ""):
    started = datetime.now(UTC)
    clock = time.monotonic()
    call = ModelCall(provider_kind=api_type)
    ctx = _context.get()
    success = False
    try:
        yield call
        success = True
    except (Exception, asyncio.CancelledError) as exc:
        await record_error(slot, exc)
        raise
    finally:
        elapsed = round((time.monotonic() - clock) * 1000)
        usage = normalize_usage(call.usage, call.provider_kind)
        await _write(
            "execute",
            """INSERT INTO llm_usage
               (conversation_id, run_id, slot, provider, model, input_tokens, output_tokens,
                cached_tokens, cache_write_tokens, reasoning_tokens, total_tokens, duration_ms, success, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            ctx.conversation_id, ctx.run_id, slot,
            provider, model,
            *usage.values(), elapsed, success, started,
        )


async def chat(provider: Any, messages: list, *, slot: str, **kwargs: Any) -> dict:
    async with model_call(
        slot=slot, provider=getattr(provider, "usage_alias", ""), model=getattr(provider, "_model", "unknown"),
    ) as call:
        response = await provider.chat(messages, **kwargs)
        call.usage = response.get("usage")
        call.provider_kind = response.get("provider", "")
        return response


@dataclass
class ToolCall:
    credits: float | None = None
    error: str | None = None


@asynccontextmanager
async def tool_call(tool: str, *, provider: str):
    started = datetime.now(UTC)
    clock = time.monotonic()
    ctx = _context.get()
    call = ToolCall()
    success = False
    try:
        yield call
        success = call.error is None
        if call.error:
            await _write(
                "execute",
                "INSERT INTO runtime_errors (conversation_id, run_id, stage, error) VALUES ($1,$2,$3,$4)",
                ctx.conversation_id, ctx.run_id, tool, call.error,
            )
    except (Exception, asyncio.CancelledError) as exc:
        call.error = error_summary(exc)
        await record_error(tool, exc)
        raise
    finally:
        await _write(
            "execute",
            """INSERT INTO tool_usage
               (conversation_id,run_id,tool,provider,credits,duration_ms,success,error,created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            ctx.conversation_id, ctx.run_id, tool, provider, call.credits,
            round((time.monotonic() - clock) * 1000), success, call.error, started,
        )


async def trigger_event(conversation_id: int, source: str, message_id: int | None = None, **values: Any) -> int | None:
    event_id = await _write(
        "fetchval",
        "INSERT INTO trigger_events (conversation_id, source, message_id) VALUES ($1,$2,$3) RETURNING id",
        conversation_id, source, message_id,
    )
    if values:
        await update_trigger(event_id, **values)
    return event_id


async def update_trigger(event_id: int | None, **values: Any) -> None:
    if event_id is None or not values:
        return
    allowed = {"result", "raw_result", "status", "waiting", "duration_ms", "run_id", "sent_count"}
    if not values.keys() <= allowed:
        raise ValueError("Unknown trigger event fields")
    columns = ", ".join(f"{key} = ${i}" for i, key in enumerate(values, 2))
    await _write(
        "execute", f"UPDATE trigger_events SET {columns}, updated_at = now() WHERE id = $1", event_id, *values.values(),
    )


def message_sent(conversation_id: int) -> None:
    ctx = _context.get()
    if ctx.response is not None and ctx.conversation_id == conversation_id:
        ctx.response.sent_count += 1


def workflow_failed() -> None:
    if (response := _context.get().response) is not None:
        response.failed = True


def workflow_started(run_id: str) -> None:
    if (response := _context.get().response) is not None:
        response.run_id = run_id
