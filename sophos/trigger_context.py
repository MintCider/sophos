"""Trigger 评估上下文。

user 消息只有一条条群聊记录；档案、记忆作为变量交给 trigger.persona 模板。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sophos import prompts, runtime_config
from sophos.config import settings
from sophos.llm.context import format_image_descriptions, get_display_name
from sophos.message_store import MessageStore

if TYPE_CHECKING:
    from sophos.llm.embedding import EmbeddingProvider
    from sophos.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_RE_REPLY_ANY = re.compile(r"\[回复 ([^\]\[]*?)\((\d+)\): ")
_RE_REPLY_PREFIX = re.compile(r"^\[回复[^\]]*\]\s*")
_RE_MARKDOWN = re.compile(r"[*#`]+")
_IMAGE_BRIEF = 40
_TEXT_MAX = 200
_EMPTY = "（暂无）"


@dataclass(frozen=True)
class TriggerContext:
    text: str
    latest_id: int  # 本次可见的最大消息 id
    pending_ids: list[int]  # 待判断的新消息（不含 bot 自己的）
    query: str  # 待判断消息的文本，用于检索记忆


def bot_name() -> str:
    return settings.bot_nickname or "Sophos"


def _has_image(row: dict[str, Any]) -> bool:
    raw = row.get("raw_message") or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return any(isinstance(seg, dict) and seg.get("type") == "image" for seg in raw)


def _is_bot(row: dict[str, Any], self_user_id: int) -> bool:
    return row.get("source") == "sophos" or row.get("user_id") == self_user_id


def render_line(row: dict[str, Any], self_user_id: int) -> str:
    """`名字: 内容`。bot 自己用昵称；回复链去掉内部 uid。"""
    name = bot_name()
    text = row.get("plain_text") or ""
    if _is_bot(row, self_user_id):
        text = _RE_REPLY_PREFIX.sub("", text)
    else:
        text = _RE_REPLY_ANY.sub(
            lambda m: f"[回复 {name if m.group(2) == str(self_user_id) else m.group(1)}: ",
            text,
        )
    img = format_image_descriptions(row, brief=_IMAGE_BRIEF)
    if not img and row.get("enrichment_status") in ("pending", "streaming") and _has_image(row):
        img = "[图片识别中]"
    if img:
        text = text.replace("[图片]", "").strip()
        img = _RE_MARKDOWN.sub("", img)
    text = " ".join(text.split())
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX] + "…"
    if img:
        text = f"{text} {img}" if text else img
    speaker = name if _is_bot(row, self_user_id) else get_display_name(row)
    return f"{speaker}: {text}"


async def load_rows(store: MessageStore, conversation_id: int) -> list[dict[str, Any]]:
    limit = int(runtime_config.get("trigger_eval_context_limit", 20))
    return await store.get_recent(conversation_id=conversation_id, limit=limit)


def pending_rows(rows: list[dict[str, Any]], *, since_id: int, self_user_id: int) -> list[dict[str, Any]]:
    """上次评估之后到达的他人消息；首次评估只取最后一条。最多 8 条。"""
    others = [r for r in rows if not _is_bot(r, self_user_id)]
    if since_id <= 0:
        return others[-1:]
    return [r for r in others if int(r["id"]) > since_id][-8:]


def build_trigger_context(
    rows: list[dict[str, Any]],
    *,
    self_user_id: int,
    since_id: int,
) -> TriggerContext | None:
    pending = pending_rows(rows, since_id=since_id, self_user_id=self_user_id)
    if not pending:
        return None
    return TriggerContext(
        text="\n".join(render_line(r, self_user_id) for r in rows),
        latest_id=max(int(r["id"]) for r in rows),
        pending_ids=[int(r["id"]) for r in pending],
        query="\n".join(r.get("plain_text") or "" for r in pending),
    )


async def persona_values(
    memory_store: MemoryStore | None,
    embedding: EmbeddingProvider | None,
    *,
    conversation_id: int,
    query: str,
) -> dict[str, str]:
    """trigger.persona 模板变量：档案原样；相关记忆仅在模板引用时检索，低于阈值不附。"""
    values = {"nickname": bot_name(), "profile_self": _EMPTY, "profile_context": _EMPTY, "memories": ""}
    if memory_store is None:
        return values
    try:
        values["profile_self"] = await memory_store.get_profile_self() or _EMPTY
        values["profile_context"] = await memory_store.get_profile_context("conversation", conversation_id) or _EMPTY
    except Exception:
        logger.warning("Trigger profile lookup failed", exc_info=True)

    used, _ = prompts.references(prompts.get("trigger.persona"))
    limit = int(runtime_config.get("trigger_memory_limit", 3))
    if "memories" not in used or embedding is None or limit <= 0 or not query.strip():
        return values
    try:
        vec = await embedding.embed_single(query[:1000])
        hits = await memory_store.search_similar(
            vec,
            limit=limit,
            min_score=float(runtime_config.get("trigger_memory_min_score", 0.6)),
        )
    except Exception:
        logger.warning("Trigger memory search failed", exc_info=True)
        return values
    values["memories"] = "\n".join(
        f"- [{m['created_at'].strftime('%Y-%m-%d') if m.get('created_at') else '?'}] {m['content']}" for m in hits
    )
    return values
