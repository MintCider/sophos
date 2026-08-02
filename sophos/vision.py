"""图片识别模块 — 渐进式图片描述（ε-greedy 衰减）。

流水线：
  OneBot 图片段 → 下载 → 感知哈希 → 缓存查询
    ├─ 探索 → VLM(图片 + 聊天上下文 + 上次描述) → 更新缓存
    └─ 利用 → 直接用缓存描述
  → 返回 {hash, description} 列表
"""

import asyncio
import base64
import io
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import asyncpg

from sophos import runtime_config
from sophos.config import settings
from sophos.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

FirstTokenCallback = Callable[[], Awaitable[None]]


class VisionFirstTokenTimeoutError(TimeoutError):
    """VLM 在首 token 时限内没有产生有效内容。"""


class VisionGenerationTimeoutError(TimeoutError):
    """VLM 在总生成时限内没有完成响应。"""


# ── 图片下载 ──────────────────────────────────────────────


async def fetch_image(url: str, session: aiohttp.ClientSession) -> tuple[bytes, str] | None:
    """下载图片，返回 (bytes, mime_type) 或 None。"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("Image fetch failed: %s -> %d", url, resp.status)
                return None
            mime = resp.content_type or "image/png"
            data = await resp.read()
            return data, mime
    except Exception:
        logger.exception("Image fetch error: %s", url)
        return None


# ── 感知哈希 ──────────────────────────────────────────────


async def compute_hash(image_data: bytes) -> str | None:
    """计算感知哈希（CPU 密集，在 executor 中运行）。"""
    loop = asyncio.get_running_loop()
    try:

        def _hash() -> str:
            import imagehash
            from PIL import Image

            img = Image.open(io.BytesIO(image_data))
            return str(imagehash.average_hash(img, hash_size=settings.vision_hash_size))

        return await loop.run_in_executor(None, _hash)
    except Exception:
        logger.exception("Image hash computation failed")
        return None


# ── GIF 帧采样 → 网格图 ──────────────────────────────────


async def maybe_gif_to_grid(image_data: bytes, mime_type: str) -> tuple[bytes, str]:
    """如果是多帧 GIF，抽帧拼网格；否则原样返回。

    返回 (image_bytes, mime_type)。网格图输出为 PNG。
    """
    if mime_type != "image/gif":
        return image_data, mime_type

    loop = asyncio.get_running_loop()

    def _process() -> tuple[bytes, str]:
        from PIL import Image

        img = Image.open(io.BytesIO(image_data))
        n_frames = getattr(img, "n_frames", 1)
        if n_frames <= 2:
            # 静态 GIF 或只有 2 帧，直接转 PNG
            img.seek(0)
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
            return buf.getvalue(), "image/png"

        # 均匀抽取关键帧（最多 16 帧）
        max_frames = min(16, n_frames)
        indices = [round(i * (n_frames - 1) / (max_frames - 1)) for i in range(max_frames)]
        frames: list[Image.Image] = []
        for idx in indices:
            img.seek(idx)
            frames.append(img.convert("RGBA").copy())

        # 拼网格：4×4
        cols = 4
        rows = (len(frames) + cols - 1) // cols
        fw, fh = frames[0].size
        grid = Image.new("RGBA", (fw * cols, fh * rows), (0, 0, 0, 0))
        for i, frame in enumerate(frames):
            # 统一尺寸（GIF 帧可能大小不一）
            if frame.size != (fw, fh):
                frame = frame.resize((fw, fh), Image.LANCZOS)
            grid.paste(frame, ((i % cols) * fw, (i // cols) * fh))

        buf = io.BytesIO()
        grid.convert("RGB").save(buf, format="PNG")
        return buf.getvalue(), "image/png"

    return await loop.run_in_executor(None, _process)


# ── ε-greedy 决策 ─────────────────────────────────────────


def should_explore(entry: dict[str, Any]) -> bool:
    """决定是否调用 VLM（探索）还是用缓存（利用）。"""
    if entry.get("pending_correction"):
        return True  # 有待消费的用户纠正，强制探索
    if not entry.get("description"):
        return True  # 无描述，必须探索
    hit_count = entry.get("hit_count", 0)
    epsilon = max(
        settings.vision_epsilon_min,
        settings.vision_epsilon_init * (settings.vision_epsilon_decay**hit_count),
    )
    return random.random() < epsilon


# ── VLM 调用 ──────────────────────────────────────────────


async def describe_image(
    provider: LLMProvider,
    image_data: bytes,
    mime_type: str = "image/png",
    *,
    context_messages: list[dict[str, Any]] | None = None,
    prev_description: str = "",
    correction_hint: str = "",
    on_first_token: FirstTokenCallback | None = None,
) -> str:
    """调用 VLM 描述图片。返回描述文本。"""
    data_uri = f"data:{mime_type};base64,{base64.b64encode(image_data).decode()}"

    # 构建 system prompt
    if prev_description:
        system_text = runtime_config.get("vision_refine_prompt").format(prev_description=prev_description)
        if correction_hint:
            system_text += f"\n用户指出：{correction_hint}"
    else:
        system_text = runtime_config.get("vision_system_prompt")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_text},
    ]
    # 注入聊天上下文（纯文本，帮助 VLM 理解语境）
    if context_messages:
        for msg in context_messages:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    # 图片
    messages.append(
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": data_uri}}],
        }
    )

    first_token = asyncio.Event()

    async def _handle_first_token() -> None:
        if first_token.is_set():
            return
        first_token.set()
        if on_first_token is not None:
            await on_first_token()

    request_started = asyncio.get_running_loop().time()
    chat_task = asyncio.create_task(
        provider.chat(
            messages,
            tools=None,
            temperature=0.3,
            max_tokens=settings.vision_max_tokens,
            on_first_token=_handle_first_token,
        ),
    )
    first_token_task = asyncio.create_task(first_token.wait())

    try:
        done, _ = await asyncio.wait(
            {chat_task, first_token_task},
            timeout=settings.vision_ttft_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise VisionFirstTokenTimeoutError(
                f"VLM first token timed out after {settings.vision_ttft_timeout:.1f}s",
            )

        if chat_task.done():
            response = await chat_task
        else:
            remaining = settings.vision_generation_timeout - (
                asyncio.get_running_loop().time() - request_started
            )
            if remaining <= 0:
                raise VisionGenerationTimeoutError(
                    f"VLM generation timed out after {settings.vision_generation_timeout:.1f}s",
                )
            try:
                response = await asyncio.wait_for(chat_task, timeout=remaining)
            except TimeoutError as exc:
                raise VisionGenerationTimeoutError(
                    f"VLM generation timed out after {settings.vision_generation_timeout:.1f}s",
                ) from exc
    finally:
        first_token_task.cancel()
        if not chat_task.done():
            chat_task.cancel()
        await asyncio.gather(first_token_task, chat_task, return_exceptions=True)

    content = response["message"].get("content", "") or ""
    if not content.strip():
        raise RuntimeError("VLM returned an empty description")
    return content


# ── 缓存操作 ──────────────────────────────────────────────


async def get_or_create_cache_entry(pool: asyncpg.Pool, hash_str: str) -> dict[str, Any]:
    """获取或创建缓存条目。"""
    row = await pool.fetchrow("SELECT * FROM image_cache WHERE hash = $1", hash_str)
    if row:
        return dict(row)
    await pool.execute(
        "INSERT INTO image_cache (hash) VALUES ($1) ON CONFLICT DO NOTHING",
        hash_str,
    )
    row = await pool.fetchrow("SELECT * FROM image_cache WHERE hash = $1", hash_str)
    return dict(row)  # type: ignore[arg-type]


async def update_cache_after_explore(
    pool: asyncpg.Pool,
    hash_str: str,
    description: str,
) -> None:
    """探索后更新缓存：写入描述，清除纠正标志，递增 hit_count。"""
    await pool.execute(
        """
        UPDATE image_cache
        SET description = $2, hit_count = hit_count + 1,
            pending_correction = false, correction_hint = NULL,
            last_seen = now()
        WHERE hash = $1
        """,
        hash_str,
        description,
    )


async def bump_hit_count(pool: asyncpg.Pool, hash_str: str) -> None:
    """利用路径：仅递增 hit_count。"""
    await pool.execute(
        "UPDATE image_cache SET hit_count = hit_count + 1, last_seen = now() WHERE hash = $1",
        hash_str,
    )


# ── 单张图片完整流水线 ────────────────────────────────────


async def process_image_segment(
    url: str,
    *,
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    vision_provider: LLMProvider | None,
    context_messages: list[dict[str, Any]] | None = None,
    on_first_token: FirstTokenCallback | None = None,
) -> dict[str, str] | None:
    """处理单张图片：下载 → 哈希 → 缓存查询 → 探索/利用。

    返回 {"hash": "...", "description": "..."} 或 None。
    """
    result = await fetch_image(url, session)
    if result is None:
        return {"status": "failed", "error": "图片下载失败"}
    image_data, mime_type = result

    hash_str = await compute_hash(image_data)
    if hash_str is None:
        return {"status": "failed", "error": "图片哈希计算失败"}

    entry = await get_or_create_cache_entry(pool, hash_str)

    if vision_provider is not None and should_explore(entry):
        # GIF → 帧网格（哈希用原始数据，VLM 用网格图）
        vlm_data, vlm_mime = await maybe_gif_to_grid(image_data, mime_type)
        try:
            description = await describe_image(
                vision_provider,
                vlm_data,
                vlm_mime,
                context_messages=context_messages,
                prev_description=entry.get("description", ""),
                correction_hint=entry.get("correction_hint", "") or "",
                on_first_token=on_first_token,
            )
            await update_cache_after_explore(pool, hash_str, description)
            logger.info("VLM explored image hash=%s: %s", hash_str, description[:200])
        except Exception as exc:
            logger.warning("VLM call failed for hash=%s: %s", hash_str, exc, exc_info=True)
            description = entry.get("description", "")
            await bump_hit_count(pool, hash_str)
            if not description:
                return {
                    "hash": hash_str,
                    "status": "failed",
                    "error": str(exc) or type(exc).__name__,
                }
    else:
        description = entry.get("description", "")
        await bump_hit_count(pool, hash_str)
        if description:
            logger.debug("Cache hit for hash=%s (hit_count=%d)", hash_str, entry.get("hit_count", 0) + 1)

    if not description:
        return {
            "hash": hash_str,
            "status": "failed",
            "error": "视觉模型未配置或没有可用的缓存描述",
        }
    return {"hash": hash_str, "description": description, "status": "completed"}


# ── 批量处理消息中的所有图片 ──────────────────────────────


async def process_message_images(
    segments: list[dict[str, Any]],
    *,
    pool: asyncpg.Pool,
    session: aiohttp.ClientSession,
    vision_provider: LLMProvider | None,
    context_messages: list[dict[str, Any]] | None = None,
    on_first_token: FirstTokenCallback | None = None,
) -> list[dict[str, str]]:
    """处理消息中所有图片段，返回 [{hash, description}, ...]。"""
    image_urls = [
        seg["data"]["url"] for seg in segments if seg.get("type") == "image" and seg.get("data", {}).get("url")
    ]
    if not image_urls:
        return []

    tasks = [
        process_image_segment(
            url,
            pool=pool,
            session=session,
            vision_provider=vision_provider,
            context_messages=context_messages,
            on_first_token=on_first_token,
        )
        for url in image_urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    image_infos: list[dict[str, str]] = []
    for r in results:
        if isinstance(r, dict):
            image_infos.append(r)
        elif isinstance(r, Exception):
            logger.warning("Image processing failed: %s", r)
            image_infos.append({"status": "failed", "error": str(r) or type(r).__name__})
    return image_infos
