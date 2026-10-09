"""Prompt registry API."""

import asyncpg
from aiohttp import web

from sophos import prompts
from sophos.config import settings
from sophos.identity_prompt import build_prompt_values

routes = web.RouteTableDef()


def _version_dict(version: prompts.PromptVersion | None) -> dict | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "content": version.content,
        "note": version.note,
        "created_at": version.created_at.isoformat(),
    }


def _summary(spec: prompts.PromptSpec) -> dict:
    version = prompts.current_version(spec.key)
    content = prompts.get(spec.key)
    return {
        "key": spec.key,
        "title": spec.title,
        "group": spec.group,
        "description": spec.description,
        "locked": spec.locked,
        "is_custom": spec.group == prompts.CUSTOM_GROUP,
        "modified": version is not None and content != spec.default,
        "updated_at": version.created_at.isoformat() if version else None,
        "length": len(content),
    }


def _detail(spec: prompts.PromptSpec) -> dict:
    return {
        **_summary(spec),
        "content": prompts.get(spec.key),
        "default": spec.default,
        "variables": spec.variables,
        "version": _version_dict(prompts.current_version(spec.key)),
        "used_by": sorted(
            other.key for other in prompts.specs() if spec.key in prompts.references(prompts.get(other.key))[1]
        ),
    }


def _spec_or_404(key: str) -> prompts.PromptSpec:
    try:
        return prompts.get_spec(key)
    except KeyError as exc:
        raise web.HTTPNotFound(reason=f"prompt 不存在：{key}") from exc


@routes.get("/api/prompts")
async def list_prompts(request: web.Request) -> web.Response:
    groups: dict[str, list[dict]] = {}
    for spec in prompts.specs():
        groups.setdefault(spec.group, []).append(_summary(spec))
    return web.json_response(
        {
            "groups": [
                {"key": key, "label": prompts.GROUP_LABELS.get(key, key), "prompts": items}
                for key, items in groups.items()
            ]
        }
    )


@routes.post("/api/prompts")
async def create_prompt(request: web.Request) -> web.Response:
    payload = await request.json()
    key = str(payload.get("key", "")).strip()
    try:
        await prompts.create_custom(
            key,
            str(payload.get("title", "")).strip(),
            str(payload.get("description", "")).strip(),
            str(payload.get("content", "")),
        )
    except prompts.PromptError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response(_detail(prompts.get_spec(key)), status=201)


@routes.get("/api/prompts/{key}")
async def get_prompt(request: web.Request) -> web.Response:
    return web.json_response(_detail(_spec_or_404(request.match_info["key"])))


@routes.put("/api/prompts/{key}")
async def save_prompt(request: web.Request) -> web.Response:
    spec = _spec_or_404(request.match_info["key"])
    payload = await request.json()
    base = payload.get("base_version_id")
    try:
        await prompts.save(
            spec.key,
            str(payload.get("content", "")),
            base_version_id=int(base) if base is not None else None,
            note=str(payload.get("note", "")).strip(),
            unlock=bool(payload.get("unlock")),
        )
    except prompts.PromptConflictError as exc:
        raise web.HTTPConflict(reason=str(exc)) from exc
    except prompts.PromptError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response(_detail(spec))


@routes.delete("/api/prompts/{key}")
async def delete_prompt(request: web.Request) -> web.Response:
    spec = _spec_or_404(request.match_info["key"])
    try:
        await prompts.delete_custom(spec.key)
    except prompts.PromptError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"ok": True})


@routes.get("/api/prompts/{key}/versions")
async def list_versions(request: web.Request) -> web.Response:
    spec = _spec_or_404(request.match_info["key"])
    versions = await prompts.list_versions(spec.key)
    return web.json_response({"versions": [_version_dict(version) for version in versions]})


@routes.post("/api/prompts/{key}/preview")
async def preview_prompt(request: web.Request) -> web.Response:
    """Render draft content with real runtime values of a chosen conversation."""
    spec = _spec_or_404(request.match_info["key"])
    payload = await request.json()
    content = str(payload.get("content", prompts.get(spec.key)))
    conversation_id = payload.get("conversation_id")
    if conversation_id is not None:
        values = await _conversation_values(request.app["pool"], int(conversation_id))
    else:
        values = {"nickname": settings.bot_nickname or "Sophos"}
    try:
        prompts.validate(spec.key, content)
        rendered = prompts.render_content(spec.key, content, values, mark_missing=True)
    except prompts.PromptError as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response(
        {
            "text": rendered.text,
            "length": len(rendered.text),
            "missing": sorted(rendered.missing),
            "includes": sorted(key for key in rendered.versions if key != spec.key),
        }
    )


async def _conversation_values(pool: asyncpg.Pool, conversation_id: int) -> dict[str, str]:
    row = await pool.fetchrow(
        """
        SELECT c.kind,
               (SELECT i.user_id FROM messages m JOIN user_identities i ON i.id = m.sender_identity_id
                WHERE m.conversation_id = c.id AND m.source = 'sophos'
                ORDER BY m.id DESC LIMIT 1) AS self_user_id,
               (SELECT i.user_id FROM messages m JOIN user_identities i ON i.id = m.sender_identity_id
                WHERE m.conversation_id = c.id AND m.source = 'user'
                ORDER BY m.id DESC LIMIT 1) AS current_user_id
        FROM conversations c WHERE c.id = $1
        """,
        conversation_id,
    )
    if row is None:
        raise web.HTTPNotFound(reason=f"会话不存在：{conversation_id}")
    return await build_prompt_values(
        pool,
        current_user_id=row["current_user_id"] or 0,
        self_user_id=row["self_user_id"] or 0,
        conversation_id=conversation_id,
        conversation_kind=row["kind"],
    )
