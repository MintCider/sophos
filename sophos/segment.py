"""消息段展开 — 将 OneBot 消息段数组转换为富文本。

支持的段类型：
  text, at, reply, forward, json, xml, image, face, record, video, share

展开后的文本写入 DB plain_text，供 LLM 上下文、记忆系统、关键词匹配使用。
"""

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp
import asyncpg

from sophos import runtime_config
from sophos.llm.context import get_display_name
from sophos.message_store import MessageStore
from sophos.onebot_api import OneBotAPI
from sophos.vision import process_image_segment

logger = logging.getLogger(__name__)

# 可展开的段类型（排除 text 和 image，image 由 ProcessImagesStage 处理）
_EXPANDABLE_TYPES = frozenset({"at", "reply", "forward", "json", "xml", "face", "record", "video", "share"})


def has_expandable_segments(segments: list[dict[str, Any]]) -> bool:
    """检查消息是否包含需要展开的段。"""
    return any(seg.get("type") in _EXPANDABLE_TYPES for seg in segments)


# ── 主入口 ──────────────────────────────────────────────────


async def expand_segments(
    segments: list[dict[str, Any]],
    *,
    api: OneBotAPI,
    store: MessageStore,
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    vision_provider: Any = None,
    group_id: int | None = None,
    depth: int = 0,
) -> str:
    """将消息段数组展开为富文本字符串。

    Args:
        segments:        OneBot 消息段数组
        api:             OneBot API 调用器
        store:           消息存储实例
        session:         aiohttp 会话
        pool:            数据库连接池
        vision_provider: VLM provider（可选，用于转发图片和卡片封面）
        group_id:        当前群号（用于 @ 昵称解析）
        depth:           转发嵌套深度（内部递归用）

    Returns:
        展开后的富文本字符串
    """
    parts: list[str] = []
    ctx = _ExpandContext(
        api=api,
        store=store,
        session=session,
        pool=pool,
        vision_provider=vision_provider,
        group_id=group_id,
        depth=depth,
    )
    for seg in segments:
        seg_type = seg.get("type", "")
        data = seg.get("data", {})
        handler = _HANDLERS.get(seg_type, _expand_unknown)
        try:
            text = await handler(data, seg_type, ctx)
        except Exception:
            logger.warning("Segment expansion failed for type=%s", seg_type, exc_info=True)
            text = f"[{seg_type}]" if seg_type else ""
        if text:
            parts.append(text)
    return "".join(parts).strip()


# ── 内部上下文 ──────────────────────────────────────────────


class _ExpandContext:
    """展开过程中共享的依赖和状态。"""

    __slots__ = (
        "api",
        "store",
        "session",
        "pool",
        "vision_provider",
        "group_id",
        "depth",
    )

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


# ── 各段类型 handler ────────────────────────────────────────


async def _expand_text(data: dict, _type: str, _ctx: _ExpandContext) -> str:
    return data.get("text", "")


async def _expand_at(data: dict, _type: str, _ctx: _ExpandContext) -> str:
    qq = str(data.get("qq", ""))
    if qq == "all":
        return "@全体成员"
    name = data.get("name", "")
    return f"@{name}" if name else f"@{qq}"


async def _expand_reply(data: dict, _type: str, ctx: _ExpandContext) -> str:
    mid = data.get("id")
    if not mid:
        return "[回复]"
    max_len = runtime_config.get("reply_max_length")
    try:
        # 优先从 DB 查
        row = await ctx.store.get_by_message_id(int(mid))
        if row:
            name = get_display_name(row)
            uid = row.get("user_id", "")
            text = row.get("plain_text", "") or ""
            if len(text) > max_len:
                text = text[:max_len] + "..."
            return f"[回复 {name}({uid}): {text}]"
        # DB 没有，fallback 到 API
        msg_data = await ctx.api.call("get_msg", {"message_id": int(mid)})
        if msg_data:
            sender = msg_data.get("sender", {})
            name = sender.get("card", "") or sender.get("nickname", "") or str(sender.get("user_id", ""))
            uid = sender.get("user_id", "")
            segs = msg_data.get("message", [])
            text = "".join(s["data"]["text"] for s in segs if isinstance(s, dict) and s.get("type") == "text")
            if len(text) > max_len:
                text = text[:max_len] + "..."
            return f"[回复 {name}({uid}): {text}]"
    except Exception:
        logger.debug("Reply expansion failed for mid=%s", mid, exc_info=True)
    return f"[回复 #{mid}]"


async def _expand_forward(data: dict, _type: str, ctx: _ExpandContext) -> str:
    max_depth = runtime_config.get("forward_max_depth")
    if ctx.depth >= max_depth:
        return "[合并转发: 嵌套过深，已省略]"

    fwd_id = data.get("id")
    if not fwd_id:
        return "[合并转发]"

    try:
        result = await ctx.api.call("get_forward_msg", {"id": fwd_id})
    except Exception:
        logger.debug("get_forward_msg failed for id=%s", fwd_id, exc_info=True)
        return "[合并转发: 获取失败]"

    messages = result.get("messages") or result.get("message") or []
    if not messages:
        return "[合并转发: 空]"

    total = len(messages)
    head_n = runtime_config.get("forward_head_count")
    tail_n = runtime_config.get("forward_tail_count")

    # 决定保留哪些消息
    if total <= head_n + tail_n:
        kept = list(enumerate(messages))
        skipped = 0
    else:
        head = [(i, messages[i]) for i in range(head_n)]
        tail = [(i, messages[i]) for i in range(total - tail_n, total)]
        kept = head + tail
        skipped = total - head_n - tail_n

    lines: list[str] = [f"[合并转发 共{total}条:"]
    for idx, (_orig_idx, msg) in enumerate(kept):
        sender = msg.get("sender", {})
        nickname = sender.get("nickname", "") or str(sender.get("user_id", ""))
        uid = sender.get("user_id", "")

        inner_segs = msg.get("content") or msg.get("message") or []

        # 递归展开内部段
        inner_text = await expand_segments(
            inner_segs,
            api=ctx.api,
            store=ctx.store,
            session=ctx.session,
            pool=ctx.pool,
            vision_provider=ctx.vision_provider,
            group_id=ctx.group_id,
            depth=ctx.depth + 1,
        )

        # 转发内的图片：过 VLM
        image_descs = await _process_forward_images(inner_segs, ctx)
        if image_descs:
            inner_text = f"{inner_text} {image_descs}" if inner_text else image_descs

        lines.append(f"{nickname}({uid}): {inner_text}")

        # 插入省略标记
        if skipped > 0 and idx == len([(i, m) for i, m in kept[:head_n]]) - 1:
            lines.append(f"...省略 {skipped} 条消息...")

    lines.append("]")
    return "\n".join(lines)


async def _process_forward_images(
    segments: list[dict[str, Any]],
    ctx: _ExpandContext,
) -> str:
    """处理转发消息内的图片段，返回描述文本。"""
    if ctx.vision_provider is None:
        return ""
    image_urls = [
        seg["data"]["url"] for seg in segments if seg.get("type") == "image" and seg.get("data", {}).get("url")
    ]
    if not image_urls:
        return ""
    descs: list[str] = []
    for url in image_urls:
        try:
            info = await process_image_segment(
                url,
                pool=ctx.pool,
                session=ctx.session,
                vision_provider=ctx.vision_provider,
            )
            if info and info.get("description"):
                descs.append(f"[图片: {info['description']}]")
            else:
                descs.append("[图片]")
        except Exception:
            descs.append("[图片]")
    return " ".join(descs)


async def _expand_json(data: dict, _type: str, ctx: _ExpandContext) -> str:
    raw = data.get("data", "")
    if not raw:
        return "[卡片消息]"
    try:
        card = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "[卡片消息: 解析失败]"

    meta = card.get("meta", {})
    title = ""
    desc = ""
    url = ""
    preview = ""

    # 遍历 meta 中的已知 key 提取信息
    for key in ("detail_1", "news", "music", "detail"):
        detail = meta.get(key)
        if isinstance(detail, dict):
            title = detail.get("title", "") or ""
            desc = detail.get("desc", "") or ""
            url = detail.get("qqdocurl") or detail.get("jumpUrl") or detail.get("url") or ""
            preview = detail.get("preview", "") or ""
            break
    else:
        # fallback: 顶层字段
        title = card.get("prompt", "") or ""
        desc = card.get("desc", "") or ""

    # VLM 识别封面图
    cover_desc = ""
    if preview and ctx.vision_provider:
        try:
            info = await process_image_segment(
                preview,
                pool=ctx.pool,
                session=ctx.session,
                vision_provider=ctx.vision_provider,
            )
            if info and info.get("description"):
                cover_desc = info["description"]
        except Exception:
            logger.debug("JSON card cover VLM failed", exc_info=True)

    # 组装
    parts: list[str] = ["[卡片"]
    if title:
        parts.append(f": {title}")
    if desc and desc != title:
        parts.append(f" - {desc}")
    if url:
        parts.append(f" {url}")
    if cover_desc:
        parts.append(f" [封面: {cover_desc}]")
    parts.append("]")
    return "".join(parts)


async def _expand_xml(data: dict, _type: str, _ctx: _ExpandContext) -> str:
    raw = data.get("data", "")
    if not raw:
        return "[XML卡片]"
    try:
        root = ET.fromstring(raw)
        # QQ XML 卡片常见结构：<msg> 根元素，属性中含 action/url/brief
        title = root.get("brief", "") or ""
        url = root.get("url", "") or ""
        # 尝试从子元素提取
        if not title:
            item = root.find(".//item")
            if item is not None:
                title = item.get("title", "") or ""
            summary = root.find(".//summary")
            if summary is not None and summary.text:
                title = title or summary.text
        parts = ["[XML卡片"]
        if title:
            parts.append(f": {title}")
        if url:
            parts.append(f" {url}")
        parts.append("]")
        return "".join(parts)
    except ET.ParseError:
        return "[XML卡片: 解析失败]"


async def _expand_image(_data: dict, _type: str, _ctx: _ExpandContext) -> str:
    return "[图片]"


async def _expand_face(_data: dict, _type: str, _ctx: _ExpandContext) -> str:
    return "[表情]"


async def _expand_record(_data: dict, _type: str, _ctx: _ExpandContext) -> str:
    return "[语音]"


async def _expand_video(_data: dict, _type: str, _ctx: _ExpandContext) -> str:
    return "[视频]"


async def _expand_share(data: dict, _type: str, _ctx: _ExpandContext) -> str:
    title = data.get("title", "")
    url = data.get("url", "")
    if title and url:
        return f"[分享: {title} {url}]"
    if title:
        return f"[分享: {title}]"
    if url:
        return f"[分享: {url}]"
    return "[分享]"


async def _expand_unknown(_data: dict, seg_type: str, _ctx: _ExpandContext) -> str:
    return f"[{seg_type}]" if seg_type else ""


# ── handler 分发表 ──────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "text": _expand_text,
    "at": _expand_at,
    "reply": _expand_reply,
    "forward": _expand_forward,
    "json": _expand_json,
    "xml": _expand_xml,
    "image": _expand_image,
    "face": _expand_face,
    "record": _expand_record,
    "video": _expand_video,
    "share": _expand_share,
}
