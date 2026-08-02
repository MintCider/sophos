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


def _request_timeout_from_config(raw: Any) -> int:
    config = _normalize_config(raw)
    value = config.get("request_timeout", _DEFAULT_REQUEST_TIMEOUT)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_REQUEST_TIMEOUT
    if not 5 <= timeout <= 3600:
        return _DEFAULT_REQUEST_TIMEOUT
    return timeout


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
    def scope(self) -> str:
        return "all"

    @property
    def group(self) -> str:
        return "image_generation"

    @property
    def required_capabilities(self) -> frozenset[str]:
        return frozenset({Capability.MESSAGE_SEND.value})

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
            WHERE name = 'generate_image'
            """
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

        asyncio.create_task(
            _generate_and_send(
                task_id=task_id,
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
            ),
            name=task_id,
        )

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
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"OpenAI images API failed ({resp.status}): {text[:300]}")
            data = await resp.json()

        item = (data.get("data") or [{}])[0]
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
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp,
        ):
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Gemini generateImages API failed ({resp.status}): {text[:300]}")
            data = await resp.json()

        images = data.get("generatedImages", data.get("images", []))
        if not images:
            raise RuntimeError("Gemini API returned no images")
        image_b64 = images[0].get("image", {}).get("imageBytes")
        return None, image_b64


class CheckImageQueueTool(Tool):
    """查看当前进程内的图像生成队列。"""

    @property
    def category(self) -> str:
        return "input"

    @property
    def scope(self) -> str:
        return "all"

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
