"""图像生成工具 — 通过 OpenAI/Gemini 图像生成 API 生成图片并发送到对话。

生成过程是异步的：LLM 调用后立即返回确认，实际 API 调用 + 发图 + VLM 描述
在后台 task 中完成。
"""

import asyncio
import base64
import json
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiohttp

from sophos import observability
from sophos.llm.provider_manager import resolve_base_url
from sophos.messaging import MessageService
from sophos.platform import Capability, SendMessageRequest
from sophos.tools.base import Tool

logger = logging.getLogger(__name__)

_DEFAULT_DESCRIPTION = "根据文本描述生成图像，并将图片发送到当前对话中"
_DEFAULT_SIZE = "auto"
_DEFAULT_FORMAT = "png"
_DEFAULT_QUALITY = "auto"
_DEFAULT_REQUEST_TIMEOUT = 600
_IMAGE_TOOL_NAME = "generate_image"
_IMAGE_API_TYPES = frozenset({"openai", "gemini"})

_SIZE_OPTIONS = [
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
]
_FORMAT_OPTIONS = ["png", "jpeg", "webp"]
_QUALITY_OPTIONS = ["auto", "low", "medium", "high"]

_IMAGE_QUEUE_PENDING: dict[str, dict[str, Any]] = {}
_IMAGE_QUEUE_COMPLETED: deque[dict[str, Any]] = deque(maxlen=3)

_DEFAULT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "图像描述（英文效果更佳）",
        },
        "size": {
            "type": "string",
            "description": "图片尺寸，如 1024x1024、1536x1024、2048x2048 等，默认 auto",
        },
        "format": {
            "type": "string",
            "enum": _FORMAT_OPTIONS,
            "description": "图片格式",
        },
        "quality": {
            "type": "string",
            "enum": _QUALITY_OPTIONS,
            "description": "图片画质",
        },
        "callback_text": {
            "type": "string",
            "description": "图片发送成功后，再单独发送到当前对话的文本消息（可选）",
        },
    },
    "required": ["prompt"],
}


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _normalize_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def parse_request_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise ValueError("request_timeout 必须是整数秒") from None
    if not 5 <= timeout <= 3600:
        raise ValueError("request_timeout 范围必须是 5~3600 秒")
    return timeout


def _request_timeout_from_config(raw: Any) -> int:
    config = _normalize_config(raw)
    try:
        return parse_request_timeout(config.get("request_timeout", _DEFAULT_REQUEST_TIMEOUT))
    except ValueError:
        return _DEFAULT_REQUEST_TIMEOUT


def default_image_config() -> dict[str, Any]:
    return {
        "name": _IMAGE_TOOL_NAME,
        "display_name": "图像生成",
        "description": _DEFAULT_DESCRIPTION,
        "provider_alias": None,
        "model_name": None,
        "api_type": "openai",
        "send_as": "image_url",
        "request_timeout": _DEFAULT_REQUEST_TIMEOUT,
        "enabled": False,
    }


def format_image_binding(config: dict[str, Any]) -> str:
    alias = config.get("provider_alias")
    model = config.get("model_name")
    if not alias or not model:
        return "图像生成: 未绑定模型"
    api_type = config.get("api_type") or "openai"
    type_str = f" ({api_type})" if api_type != "openai" else ""
    enabled = "启用" if config.get("enabled") else "禁用"
    timeout = config.get("request_timeout", _DEFAULT_REQUEST_TIMEOUT)
    return (
        f"图像生成: {alias} / {model}{type_str}\n"
        f"  状态: {enabled}\n"
        f"  超时: {timeout}s"
    )


def _config_from_row(row: Any) -> dict[str, Any]:
    data = default_image_config()
    data.update(dict(row))
    data["request_timeout"] = _request_timeout_from_config(data.pop("config", None))
    data["provider_alias"] = data.get("provider_alias") or None
    data["model_name"] = data.get("model_name") or None
    data["api_type"] = data.get("api_type") or "openai"
    data["send_as"] = data.get("send_as") or "image_url"
    data["enabled"] = bool(data.get("enabled"))
    return data


def _after_image_config_change(enabled: bool) -> None:
    from sophos import pipeline

    pipeline._invalidate_tool_disabled_cache()
    if pipeline._tool_registry is not None:
        pipeline._tool_registry.set_disabled(_IMAGE_TOOL_NAME, not enabled)


async def load_image_config(pool: Any) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT name, display_name, description, provider_alias, model_name,
               api_type, send_as, config, enabled
        FROM custom_tools
        WHERE name = $1
        """,
        _IMAGE_TOOL_NAME,
    )
    if not row:
        return default_image_config()
    return _config_from_row(row)


async def persist_image_config(pool: Any, config: dict[str, Any]) -> dict[str, Any]:
    current = await load_image_config(pool)
    merged = {**current, **config}
    description = str(merged.get("description") or _DEFAULT_DESCRIPTION)
    provider_alias = merged.get("provider_alias") or None
    model_name = merged.get("model_name") or None
    api_type = merged.get("api_type") or "openai"
    send_as = merged.get("send_as") or "image_url"
    request_timeout = _request_timeout_from_config({"request_timeout": merged.get("request_timeout")})
    enabled = bool(merged.get("enabled", False))
    await pool.execute(
        """
        INSERT INTO custom_tools (
            name, display_name, description, category, conversation_kinds, tool_type,
            provider_alias, model_name, api_type, send_as, config, enabled
        )
        VALUES (
            'generate_image', '图像生成', $1, 'output', '[]'::jsonb, 'image_generation',
            $2, $3, $4, $5, $6::jsonb, $7
        )
        ON CONFLICT (name) DO UPDATE SET
            description = $1, provider_alias = $2, model_name = $3,
            api_type = $4, send_as = $5,
            config = COALESCE(custom_tools.config, '{}'::jsonb) || $6::jsonb,
            enabled = $7, updated_at = now()
        """,
        description,
        provider_alias,
        model_name,
        api_type,
        send_as,
        json.dumps({"request_timeout": request_timeout}, ensure_ascii=False),
        enabled,
    )
    saved = {
        **merged,
        "description": description,
        "provider_alias": provider_alias,
        "model_name": model_name,
        "api_type": api_type,
        "send_as": send_as,
        "request_timeout": request_timeout,
        "enabled": enabled,
    }
    _after_image_config_change(enabled)
    return saved


async def bind_image_model(pool: Any, alias: str, model: str, *, api_type: str = "openai") -> str:
    if api_type not in _IMAGE_API_TYPES:
        return "type 必须是 openai / gemini"
    row = await pool.fetchrow(
        "SELECT alias, base_urls FROM llm_providers WHERE alias = $1",
        alias,
    )
    if not row:
        return f"provider '{alias}' 不存在"
    if not resolve_base_url(row["base_urls"], api_type):
        return f"provider '{alias}' 未配置 {api_type} Base URL"
    await persist_image_config(
        pool,
        {
            "provider_alias": alias,
            "model_name": model,
            "api_type": api_type,
            "enabled": True,
        },
    )
    suffix = f" (api_type={api_type})" if api_type != "openai" else ""
    return f"已绑定图像生成到 {alias} / {model}{suffix}"


async def unbind_image_model(pool: Any) -> str:
    current = await load_image_config(pool)
    if not current.get("provider_alias") and not current.get("model_name"):
        if current.get("enabled"):
            await persist_image_config(pool, {"enabled": False})
        return "图像生成未绑定模型"
    await persist_image_config(
        pool,
        {
            "provider_alias": None,
            "model_name": None,
            "enabled": False,
        },
    )
    return "已解除图像生成的模型绑定"


async def set_image_timeout(pool: Any, seconds: int) -> str:
    timeout = parse_request_timeout(seconds)
    await persist_image_config(pool, {"request_timeout": timeout})
    return f"图像生成 timeout 已更新: {timeout}s"


def _session_from_context(context: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": context.get("conversation_id")}


def _clone_queue_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "session": dict(record.get("session") or {}),
    }


def _image_queue_snapshot() -> dict[str, Any]:
    pending = [_clone_queue_record(record) for record in _IMAGE_QUEUE_PENDING.values()]
    completed = [_clone_queue_record(record) for record in _IMAGE_QUEUE_COMPLETED]
    return {
        "pending": pending,
        "completed": completed,
        "pending_count": len(pending),
        "completed_count": len(completed),
    }


def _register_queue_task(
    *,
    session: dict[str, Any],
    prompt: str,
    callback_text: str | None,
) -> str:
    task_id = f"img_{uuid4().hex[:12]}"
    _IMAGE_QUEUE_PENDING[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "created_at": _now_iso(),
        "completed_at": None,
        "session": dict(session),
        "prompt": prompt,
        "callback_text": callback_text,
        "ok": None,
        "image_message_id": None,
        "callback_message_id": None,
        "error": None,
    }
    return task_id


def _complete_queue_task(
    task_id: str,
    *,
    ok: bool,
    image_message_id: int | None = None,
    callback_message_id: int | None = None,
    error: str | None = None,
) -> None:
    record = _IMAGE_QUEUE_PENDING.pop(task_id, None)
    if record is None:
        logger.warning("generate_image: queue task %s is missing from pending", task_id)
        return
    record.update(
        {
            "status": "completed",
            "completed_at": _now_iso(),
            "ok": ok,
            "image_message_id": image_message_id,
            "callback_message_id": callback_message_id,
            "error": error,
        }
    )
    _IMAGE_QUEUE_COMPLETED.appendleft(record)


async def _send_callback_text(
    *,
    message_service: MessageService,
    conversation_id: int,
    callback_text: str,
) -> int:
    sent = await message_service.send_message(
        SendMessageRequest(conversation_id=conversation_id, text=callback_text)
    )
    logger.info(
        "generate_image: callback text sent (message_id=%s, conversation_id=%s)",
        sent.message_id,
        conversation_id,
    )
    return sent.message_id


class ImageGenerationTool(Tool):
    """图像生成工具：调用图像生成 API 并将结果以图片消息发送到对话。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "image_generation"

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset({Capability.MESSAGE_SEND.value, Capability.MESSAGE_IMAGE.value})

    @property
    def is_builtin(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return _DEFAULT_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return _DEFAULT_PARAMETERS

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        pool = context["pool"]
        prompt = params["prompt"]
        size = params.get("size") or _DEFAULT_SIZE
        fmt = params.get("format") or _DEFAULT_FORMAT
        quality = params.get("quality") or _DEFAULT_QUALITY
        raw_callback_text = params.get("callback_text")
        callback_text = raw_callback_text.strip() if isinstance(raw_callback_text, str) else None
        if not callback_text:
            callback_text = None

        row = await pool.fetchrow(
            """
            SELECT provider_alias, model_name, api_type, send_as, config
            FROM custom_tools
            WHERE name = $1
            """,
            _IMAGE_TOOL_NAME,
        )
        if not row or not row["provider_alias"] or not row["model_name"]:
            logger.warning("generate_image called but tool is not configured")
            return {"error": "图像生成工具未配置供应商和模型"}

        provider_alias = row["provider_alias"]
        model_name = row["model_name"]
        api_type = row["api_type"] or "openai"
        request_timeout = _request_timeout_from_config(row.get("config"))

        prov_row = await pool.fetchrow(
            "SELECT base_urls, api_key FROM llm_providers WHERE alias = $1",
            provider_alias,
        )
        if not prov_row:
            logger.warning(
                "generate_image: provider '%s' no longer exists",
                provider_alias,
            )
            return {"error": f"供应商 '{provider_alias}' 不存在"}

        base_url = resolve_base_url(prov_row["base_urls"], api_type)
        if not base_url:
            logger.warning(
                "generate_image: provider '%s' has no %s base URL configured",
                provider_alias,
                api_type,
            )
            return {"error": f"供应商 '{provider_alias}' 未配置 {api_type} Base URL"}

        api_key = prov_row["api_key"]
        session = _session_from_context(context)
        task_id = _register_queue_task(
            session=session,
            prompt=prompt,
            callback_text=callback_text,
        )

        logger.info(
            "generate_image: dispatching async task %s provider=%s (%s) "
            "model=%s size=%s fmt=%s quality=%s prompt[:60]=%r",
            task_id,
            provider_alias,
            api_type,
            model_name,
            size,
            fmt,
            quality,
            prompt[:60],
        )

        with observability.scope(conversation_id=context.get("conversation_id"), response=None, trigger_event_id=None):
            asyncio.create_task(_generate_and_send(
                task_id=task_id,
                provider_alias=provider_alias,
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                api_type=api_type,
                prompt=prompt,
                callback_text=callback_text,
                request_timeout=request_timeout,
                size=size,
                fmt=fmt,
                quality=quality,
                context=context,
            ), name=task_id)

        return {
            "status": "generating",
            "task_id": task_id,
            "prompt": prompt,
            "callback_text": callback_text,
            "request_timeout": request_timeout,
        }

    async def _call_openai(
        self,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        size: str = _DEFAULT_SIZE,
        fmt: str = _DEFAULT_FORMAT,
        quality: str = _DEFAULT_QUALITY,
        request_timeout: int = _DEFAULT_REQUEST_TIMEOUT,
        *,
        provider_alias: str = "",
    ) -> tuple[str | None, str | None]:
        url = f"{base_url.rstrip('/')}/images/generations"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "format": fmt,
            "quality": quality,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        logger.debug("generate_image: POST %s (model=%s)", url, model)
        async with (
            observability.model_call(slot="image", provider=provider_alias, model=model, api_type="openai") as call,
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp,
        ):
            resp.raise_for_status()
            data = await resp.json()
            call.usage = observability.api_usage(data, "openai")

            item = (data.get("data") or [{}])[0]
            if not item.get("url") and not item.get("b64_json"):
                raise RuntimeError("OpenAI images API returned no image data")
            return item.get("url"), item.get("b64_json")

    async def _call_gemini(
        self,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        size: str = _DEFAULT_SIZE,
        fmt: str = _DEFAULT_FORMAT,
        quality: str = _DEFAULT_QUALITY,
        request_timeout: int = _DEFAULT_REQUEST_TIMEOUT,
        *,
        provider_alias: str = "",
    ) -> tuple[str | None, str | None]:
        url = f"{base_url.rstrip('/')}/models/{model}:generateImages?key={api_key}"
        config: dict[str, Any] = {"numberOfImages": 1}
        if size and size != "auto":
            config["size"] = size
        if fmt:
            config["format"] = fmt
        if quality and quality != "auto":
            config["quality"] = quality
        payload = {
            "prompt": prompt,
            "config": config,
        }
        headers = {"Content-Type": "application/json"}
        logger.debug("generate_image: POST %s (model=%s)", url.split("?")[0], model)
        async with (
            observability.model_call(slot="image", provider=provider_alias, model=model, api_type="gemini") as call,
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                raise RuntimeError(f"Gemini generateImages API failed (HTTP {resp.status})")
            data = await resp.json()
            call.usage = observability.api_usage(data, "gemini")

            images = data.get("generatedImages", data.get("images", []))
            if not images:
                raise RuntimeError("Gemini API returned no images")
            image_b64 = images[0].get("image", {}).get("imageBytes")
            if not image_b64:
                raise RuntimeError("Gemini API returned no image bytes")
            return None, image_b64


class CheckImageQueueTool(Tool):
    """查看当前进程内的图像生成队列。"""

    @property
    def category(self) -> str:
        return "input"

    @property
    def group(self) -> str:
        return "image_generation"

    @property
    def name(self) -> str:
        return "check_image_queue"

    @property
    def description(self) -> str:
        return "检查当前绘图队列，返回所有 pending 任务和最近三个 completed 任务"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        return _image_queue_snapshot()


async def _generate_and_send(
    *,
    task_id: str,
    base_url: str,
    api_key: str,
    model_name: str,
    api_type: str,
    prompt: str,
    callback_text: str | None,
    request_timeout: int,
    size: str,
    fmt: str,
    quality: str,
    context: dict[str, Any],
    provider_alias: str = "",
) -> None:
    """后台 task：调 API → 发图 → VLM 描述 → 存入 extra。"""
    tool = ImageGenerationTool()

    try:
        if api_type == "gemini":
            image_url, image_b64 = await tool._call_gemini(
                base_url,
                api_key,
                model_name,
                prompt,
                size,
                fmt,
                quality,
                request_timeout,
                provider_alias=provider_alias,
            )
        else:
            image_url, image_b64 = await tool._call_openai(
                base_url,
                api_key,
                model_name,
                prompt,
                size,
                fmt,
                quality,
                request_timeout,
                provider_alias=provider_alias,
            )
    except Exception as e:
        logger.exception(
            "generate_image: API call failed (model=%s api_type=%s)",
            model_name,
            api_type,
        )
        _complete_queue_task(task_id, ok=False, error=f"image API call failed: {e}")
        return

    if not image_url and not image_b64:
        logger.warning("generate_image: API returned no image data (model=%s)", model_name)
        _complete_queue_task(task_id, ok=False, error=f"image API returned no image data: {model_name}")
        return

    logger.debug(
        "generate_image: received image (url=%s, b64_len=%d)",
        bool(image_url),
        len(image_b64) if image_b64 else 0,
    )

    message_service: MessageService = context["message_service"]
    store = context["store"]
    pool = context["pool"]
    conversation_id = context.get("conversation_id")
    if not isinstance(conversation_id, int):
        logger.warning("generate_image: current conversation id is missing")
        _complete_queue_task(task_id, ok=False, error="current conversation id is missing")
        return

    if image_b64:
        image_segment = {"type": "image", "data": {"base64": image_b64}}
    else:
        image_segment = {"type": "image", "data": {"url": image_url}}

    try:
        sent = await message_service.send_message(
            SendMessageRequest(conversation_id=conversation_id, inline_media=(image_segment,))
        )
    except Exception as e:
        logger.exception("generate_image: failed to send image")
        _complete_queue_task(task_id, ok=False, error=f"failed to send image: {e}")
        return

    message_id = sent.message_id
    logger.info(
        "generate_image: image sent (message_id=%s, conversation_id=%s)",
        message_id,
        conversation_id,
    )

    callback_message_id: int | None = None
    callback_error: str | None = None
    if callback_text:
        try:
            callback_message_id = await _send_callback_text(
                message_service=message_service,
                conversation_id=conversation_id,
                callback_text=callback_text,
            )
        except Exception as e:
            callback_error = f"failed to send callback text: {e}"
            logger.exception("generate_image: failed to send callback text")

    _complete_queue_task(
        task_id,
        ok=callback_error is None,
        image_message_id=message_id,
        callback_message_id=callback_message_id,
        error=callback_error,
    )

    # VLM 描述
    image_data: bytes | None = None
    if image_b64:
        try:
            image_data = base64.b64decode(image_b64)
        except Exception:
            logger.exception("generate_image: failed to decode base64 image for VLM")
    elif image_url:
        vision_provider = context.get("vision_provider")
        if vision_provider is not None:
            try:
                async with aiohttp.ClientSession() as session:
                    from sophos.vision import fetch_image

                    fetch_result = await fetch_image(image_url, session)
                    if fetch_result:
                        image_data, _ = fetch_result
            except Exception:
                logger.exception("generate_image: failed to download image for VLM")

    if image_data is not None:
        vision_provider = context.get("vision_provider")
        if vision_provider is not None:
            try:
                from sophos.vision import (
                    compute_hash,
                    describe_image,
                    get_or_create_cache_entry,
                    update_cache_after_explore,
                )

                hash_str = await compute_hash(image_data)
                if hash_str:
                    entry = await get_or_create_cache_entry(pool, hash_str)
                    description = await describe_image(
                        vision_provider,
                        image_data,
                        context_messages=None,
                        prev_description=entry.get("description", ""),
                    )
                    await update_cache_after_explore(pool, hash_str, description)
                    await store.update_image_metadata(
                        message_id,
                        [{"hash": hash_str, "description": description}],
                    )
                    logger.info(
                        "generate_image: VLM described image (hash=%s, desc[:80]=%r)",
                        hash_str,
                        description[:80],
                    )
            except Exception:
                logger.exception("generate_image: VLM description failed")


IMAGE_GEN_TOOLS: list[Tool] = [ImageGenerationTool(), CheckImageQueueTool()]
