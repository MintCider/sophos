"""Prompt registry: versioned storage and ``{{}}`` rendering.

Every prompt is identified by a stable key. Builtin keys are declared in code with
a default; custom keys are shared snippets created from the WebUI. Content is stored
append-only in ``prompt_versions``; the newest row of a key is its current value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)

INCLUDE_PREFIX = "prompt."
MAX_INCLUDE_DEPTH = 8
CUSTOM_GROUP = "custom"
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


class PromptError(ValueError):
    """Invalid prompt key, content or reference."""


class PromptConflictError(PromptError):
    """The prompt changed since the editor loaded it."""


class PromptLockedError(PromptError):
    """A locked prompt was saved without an explicit unlock."""


@dataclass(frozen=True, slots=True)
class PromptSpec:
    key: str
    title: str
    group: str
    description: str = ""
    default: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    locked: bool = False


@dataclass(frozen=True, slots=True)
class PromptVersion:
    id: int
    content: str
    note: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    text: str
    versions: dict[str, int | None]
    missing: frozenset[str]


RUNTIME_VARIABLES: dict[str, str] = {
    "nickname": "Bot 昵称",
    "current_time": "当前本地时间",
    "timezone": "时区",
    "self_user_id": "Bot 的内部用户 ID",
    "current_user_id": "当前发言用户的内部 ID",
    "current_user_roles": "当前用户角色",
    "conversation_id": "当前会话内部 ID",
    "conversation_kind": "会话类型",
    "master_users": "Master 用户列表",
    "identity_context": "完整内部身份块",
    "message_format": "上下文消息格式说明",
    "runtime_context": "时间、身份与消息格式的完整运行时块",
}

_DEFAULT_SYSTEM = """\
你是 {{nickname}}，一个活跃在群聊中的智能助手。

请结合对话上下文自然、简洁地回复。不要声称看到了上下文中不存在的内容，也不要编造无法确认的信息。

{{runtime_context}}"""

_DEFAULT_COLLECTOR = """\
# 基础设定

你是 {{nickname}} 的信息收集节点。你负责根据当前消息、对话历史和工具结果，获取后续执行节点所需的事实信息。

{{runtime_context}}

# 工具使用

工具的名称和参数以 schema 为准。工具参数中的 message_id、user_id 和 conversation_id 均为 Sophos 内部 ID。

- query_messages：按时间范围读取当前或指定会话的历史消息。需要回顾上下文窗口之前的内容时使用。
- get_user：查询内部用户、已认证或已观察的平台身份和角色。
- get_conversation：查询内部会话的类型、名称、父会话和适配器能力。
- list_conversation_members：读取会话成员目录；仅在当前平台支持时可用。
- search_memory：搜索长期记忆，返回相关记忆及其内部 ID。
- web_search：搜索互联网并返回相关结果。
- web_fetch：读取指定网页的正文。
- view_image：读取网络图片并返回图片描述。
- check_image_queue：查询异步图像生成任务状态。
- complete_collection：现有消息和工具结果已足够交给后续执行节点时调用。"""

_DEFAULT_TRIGGER_EVAL_FORMAT = """\
接下来你会收到一系列群聊对话。大多数消息与你无关，请保守判断。
只有当最新消息明确满足以下任一条件时才算 relevant：
1. 直接提到你的名字或称呼
2. 明确在回应你之前说的话（话题连贯且有指向性）
3. 你在人设中被要求关注的话题
不确定时一律选 irrelevant。
如果 relevant，再判断这条消息是否完整（句子明显被截断或只说了半句 → more；问句、短句、完整表达 → done）。
只回复以下三个词之一：irrelevant / relevant_done / relevant_more"""

_DEFAULT_TRIGGER_WAITING_FORMAT = """\
接下来你会收到一系列群聊对话。之前的消息被判定为与你有关，但对方还没说完。
现在有新消息到达，请重新判断：
- 如果最新消息其实与你无关 → irrelevant
- 如果与你有关且这条消息是完整的表达 → relevant_done
- 如果与你有关但这条消息明显没说完 → relevant_more
不确定时选 irrelevant。
只回复以下三个词之一：irrelevant / relevant_done / relevant_more"""

_DEFAULT_VISION_DESCRIBE = (
    "以下是出现在群聊中的一张图片。"
    "请用简练的中文概括图像内容，尽可能覆盖值得注意的特征。"
    "首先给出图片类型（如：照片、表情包、梗图、截图、漫画、二次元人物等），然后描述内容。"
    "如果是表情包或梗图，描述其含义和情感。"
    "如果有文字，转录文字内容。"
    "如果图片是多帧网格（多张小图拼成的网格），说明原图是动图/GIF，请描述动画内容和变化过程。"
    "你只需要进行描述，不要做出任何进一步的补充、解释、推断或反馈。"
)

_DEFAULT_VISION_REFINE = (
    "之前对这张图片的描述是：{{prev_description}}\n"
    "请重新审视图片，如果描述准确则保持不变，如果有遗漏或错误请修正。"
    "同样只需描述，不要做进一步补充。"
)

GROUP_LABELS: dict[str, str] = {
    "persona": "人设",
    "trigger": "触发",
    "vision": "视觉",
    CUSTOM_GROUP: "共享片段",
}

BUILTIN_PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(
        key="persona.system",
        title="主系统提示词",
        group="persona",
        description="actor 节点（default 模型槽）的 system prompt。",
        default=_DEFAULT_SYSTEM,
        variables=RUNTIME_VARIABLES,
    ),
    PromptSpec(
        key="persona.collector",
        title="信息收集提示词",
        group="persona",
        description="collector 节点的 system prompt。",
        default=_DEFAULT_COLLECTOR,
        variables=RUNTIME_VARIABLES,
    ),
    PromptSpec(
        key="trigger.persona",
        title="触发评估人设",
        group="trigger",
        description="轻量模型判断是否回复时使用的人设，拼接在输出格式约束之前。",
        variables={"nickname": RUNTIME_VARIABLES["nickname"]},
    ),
    PromptSpec(
        key="trigger.eval_format",
        title="触发评估格式",
        group="trigger",
        description="常规评估的输出格式约束，解析逻辑依赖其中的三个结果词。",
        default=_DEFAULT_TRIGGER_EVAL_FORMAT,
        locked=True,
    ),
    PromptSpec(
        key="trigger.waiting_format",
        title="等待续写评估格式",
        group="trigger",
        description="对方未说完时重新评估的输出格式约束。",
        default=_DEFAULT_TRIGGER_WAITING_FORMAT,
        locked=True,
    ),
    PromptSpec(
        key="vision.describe",
        title="图片描述",
        group="vision",
        description="首次识别图片时的 system prompt。",
        default=_DEFAULT_VISION_DESCRIBE,
    ),
    PromptSpec(
        key="vision.refine",
        title="图片复核",
        group="vision",
        description="复核已有描述时的 system prompt。",
        default=_DEFAULT_VISION_REFINE,
        variables={"prev_description": "上一次的图片描述"},
    ),
)

_BUILTIN: dict[str, PromptSpec] = {spec.key: spec for spec in BUILTIN_PROMPTS}
_custom: dict[str, PromptSpec] = {}
_current: dict[str, PromptVersion] = {}
_pool: asyncpg.Pool | None = None


async def init(pool: asyncpg.Pool) -> None:
    """Load custom prompt metadata and the current version of every key."""
    global _pool  # noqa: PLW0603
    _pool = pool
    _custom.clear()
    _current.clear()
    for row in await pool.fetch("SELECT key, title, description FROM custom_prompts ORDER BY key"):
        _custom[row["key"]] = _custom_spec(row["key"], row["title"], row["description"])
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (prompt_key) id, prompt_key, content, note, created_at
        FROM prompt_versions
        WHERE scope = 'global'
        ORDER BY prompt_key, id DESC
        """
    )
    for row in rows:
        _current[row["prompt_key"]] = _version(row)
    logger.info("Prompt registry loaded: %d custom, %d versioned", len(_custom), len(_current))


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Prompt registry not initialized")
    return _pool


def _custom_spec(key: str, title: str, description: str) -> PromptSpec:
    return PromptSpec(key=key, title=title or key, group=CUSTOM_GROUP, description=description)


def _version(row: asyncpg.Record) -> PromptVersion:
    return PromptVersion(
        id=row["id"],
        content=row["content"],
        note=row["note"],
        created_at=row["created_at"],
    )


def specs() -> list[PromptSpec]:
    return [*BUILTIN_PROMPTS, *sorted(_custom.values(), key=lambda spec: spec.key)]


def get_spec(key: str) -> PromptSpec:
    spec = _BUILTIN.get(key) or _custom.get(key)
    if spec is None:
        raise KeyError(key)
    return spec


def current_version(key: str) -> PromptVersion | None:
    return _current.get(key)


def get(key: str) -> str:
    """Return the raw current template of a prompt."""
    version = _current.get(key)
    return version.content if version is not None else get_spec(key).default


def references(content: str) -> tuple[set[str], set[str]]:
    """Return (variables, included prompt keys) referenced by a template."""
    variables: set[str] = set()
    includes: set[str] = set()
    for name in _TOKEN_RE.findall(content):
        if name.startswith(INCLUDE_PREFIX):
            includes.add(name.removeprefix(INCLUDE_PREFIX))
        else:
            variables.add(name)
    return variables, includes


def _expanded_variables(key: str, content: str, stack: tuple[str, ...]) -> set[str]:
    """Collect variables of a template and everything it includes, rejecting bad includes."""
    if len(stack) > MAX_INCLUDE_DEPTH:
        raise PromptError(f"引用层级超过 {MAX_INCLUDE_DEPTH} 层：{' → '.join(stack)}")
    variables, includes = references(content)
    for include in sorted(includes):
        if include in stack or include == key:
            raise PromptError(f"循环引用：{' → '.join((*stack, include))}")
        if include not in _BUILTIN and include not in _custom:
            raise PromptError(f"引用了不存在的 prompt：{include}")
        variables |= _expanded_variables(include, get(include), (*stack, include))
    return variables


def validate(key: str, content: str) -> None:
    """Reject unknown includes, cycles and variables the prompt's consumer never provides."""
    spec = get_spec(key)
    variables = _expanded_variables(key, content, (key,))
    if spec.group == CUSTOM_GROUP:
        return
    unknown = sorted(variables - set(spec.variables))
    if unknown:
        raise PromptError(f"不支持的变量：{', '.join(unknown)}")


def render(key: str, values: dict[str, str] | None = None, *, mark_missing: bool = False) -> RenderedPrompt:
    """Render a stored prompt."""
    return render_content(key, get(key), values, mark_missing=mark_missing)


def render_content(
    key: str,
    content: str,
    values: dict[str, str] | None = None,
    *,
    mark_missing: bool = False,
) -> RenderedPrompt:
    """Render ``content`` as if it were the template of ``key``.

    Missing variables become empty strings at runtime, or ``‹name›`` in previews.
    """
    values = values or {}
    versions: dict[str, int | None] = {}
    missing: set[str] = set()

    def expand(current_key: str, template: str, stack: tuple[str, ...]) -> str:
        version = _current.get(current_key)
        versions[current_key] = version.id if version is not None else None

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name.startswith(INCLUDE_PREFIX):
                include = name.removeprefix(INCLUDE_PREFIX)
                if include in stack or len(stack) > MAX_INCLUDE_DEPTH:
                    raise PromptError(f"循环引用：{' → '.join((*stack, include))}")
                try:
                    included = get(include)
                except KeyError as exc:
                    raise PromptError(f"引用了不存在的 prompt：{include}") from exc
                return expand(include, included, (*stack, include))
            if name in values:
                return values[name]
            missing.add(name)
            return f"‹{name}›" if mark_missing else ""

        return _TOKEN_RE.sub(replace, template)

    text = expand(key, content, (key,))
    if missing and not mark_missing:
        logger.warning("Prompt %s rendered without values for: %s", key, ", ".join(sorted(missing)))
    return RenderedPrompt(text=text, versions=versions, missing=frozenset(missing))


async def save(
    key: str,
    content: str,
    *,
    base_version_id: int | None,
    note: str = "",
    unlock: bool = False,
) -> PromptVersion:
    """Append a new version if nobody else saved since ``base_version_id``."""
    spec = get_spec(key)
    if spec.locked and not unlock:
        raise PromptLockedError(f"{key} 已锁定，需要显式解锁后才能修改")
    validate(key, content)
    pool = _require_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('prompt:' || $1))", key)
        latest = await conn.fetchval(
            "SELECT max(id) FROM prompt_versions WHERE prompt_key = $1 AND scope = 'global'",
            key,
        )
        if latest != base_version_id:
            raise PromptConflictError(f"{key} 已被修改，请刷新后再保存")
        row = await conn.fetchrow(
            """
            INSERT INTO prompt_versions (prompt_key, content, note)
            VALUES ($1, $2, $3)
            RETURNING id, content, note, created_at
            """,
            key,
            content,
            note,
        )
    version = _version(row)
    _current[key] = version
    logger.info("Prompt %s saved as version %d", key, version.id)
    return version


async def list_versions(key: str, limit: int = 100) -> list[PromptVersion]:
    get_spec(key)
    rows = await _require_pool().fetch(
        """
        SELECT id, content, note, created_at FROM prompt_versions
        WHERE prompt_key = $1 AND scope = 'global'
        ORDER BY id DESC LIMIT $2
        """,
        key,
        limit,
    )
    return [_version(row) for row in rows]


async def create_custom(key: str, title: str, description: str, content: str) -> PromptVersion:
    if not _KEY_RE.match(key):
        raise PromptError("key 只能包含小写字母、数字、下划线和点，并以字母开头")
    if key in _BUILTIN or key in _custom:
        raise PromptError(f"prompt 已存在：{key}")
    pool = _require_pool()
    await pool.execute(
        "INSERT INTO custom_prompts (key, title, description) VALUES ($1, $2, $3)",
        key,
        title,
        description,
    )
    _custom[key] = _custom_spec(key, title, description)
    try:
        return await save(key, content, base_version_id=None, note="创建")
    except PromptError:
        await delete_custom(key)
        raise


async def delete_custom(key: str) -> None:
    if key not in _custom:
        raise PromptError(f"只能删除共享片段：{key}")
    users = sorted(other.key for other in specs() if other.key != key and key in references(get(other.key))[1])
    if users:
        raise PromptError(f"仍被引用：{', '.join(users)}")
    pool = _require_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM prompt_versions WHERE prompt_key = $1", key)
        await conn.execute("DELETE FROM custom_prompts WHERE key = $1", key)
    _custom.pop(key, None)
    _current.pop(key, None)
