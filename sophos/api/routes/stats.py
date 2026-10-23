"""Dashboard statistics, using the configured local calendar and persisted events."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web

from sophos.agent.runs import get_run_tracker
from sophos.config import settings

routes = web.RouteTableDef()

_FILTER = "created_at >= $1 AND created_at < $2 AND ($3::bigint IS NULL OR conversation_id = $3)"
_MESSAGE_FILTER = "occurred_at >= $1 AND occurred_at < $2 AND ($3::bigint IS NULL OR conversation_id = $3)"
_NAME = "COALESCE(NULLIF(c.display_name, ''), c.kind || ' · ' || c.external_conversation_id, '未关联会话')"
_USAGE_TOTALS = """
    count(*)::int AS calls,
    count(*) FILTER (WHERE NOT success)::int AS failures,
    count(*) FILTER (WHERE success AND total_tokens IS NULL)::int AS missing_usage,
    COALESCE(sum(input_tokens), 0)::bigint AS input_tokens,
    COALESCE(sum(output_tokens), 0)::bigint AS output_tokens,
    COALESCE(sum(cached_tokens), 0)::bigint AS cached_tokens,
    COALESCE(sum(cache_write_tokens), 0)::bigint AS cache_write_tokens,
    COALESCE(sum(reasoning_tokens), 0)::bigint AS reasoning_tokens,
    COALESCE(sum(total_tokens), 0)::bigint AS tokens
"""
_TOOL_TOTALS = """
    count(*)::int AS calls,
    count(*) FILTER (WHERE NOT success)::int AS failures,
    sum(credits)::float AS credits,
    count(credits)::int AS credit_calls,
    count(*) FILTER (WHERE success AND credits IS NULL)::int AS missing_credits
"""


def parse_window(query, *, now: datetime | None = None) -> tuple[datetime, datetime, int | None, str]:
    range_key = query.get("range", "today")
    if range_key not in {"today", "week", "month"}:
        raise web.HTTPBadRequest(reason="range must be today, week or month")
    conversation = query.get("conversation_id")
    try:
        conversation_id = int(conversation) if conversation is not None else None
        if conversation_id is not None and not 0 < conversation_id <= 9223372036854775807:
            raise ValueError
    except ValueError:
        raise web.HTTPBadRequest(reason="conversation_id must be a positive integer") from None
    now = (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.timezone))
    days = {"today": 1, "week": 7, "month": 30}[range_key]
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    return start, now, conversation_id, range_key


def serialize(row) -> dict:
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in dict(row).items()}


@routes.get("/api/stats/conversations")
async def conversations(request: web.Request) -> web.Response:
    rows = await request.app["pool"].fetch(
        f"SELECT c.id, {_NAME} AS name, c.kind FROM conversations c ORDER BY name, c.id"
    )
    return web.json_response({"items": [dict(r) for r in rows]})


@routes.get("/api/stats")
async def stats(request: web.Request) -> web.Response:
    start, end, conversation_id, range_key = parse_window(request.query)
    args = (start, end, conversation_id)
    pool = request.app["pool"]
    bucket = "hour" if range_key == "today" else "day"
    tz = settings.timezone

    async def fetch(sql, *params):
        return await pool.fetch(sql, *params)

    usage, message_rows, usage_trend, slots, models, trigger, recent_errors, recent, pending = await asyncio.gather(
        pool.fetchrow(f"SELECT {_USAGE_TOTALS} FROM llm_usage WHERE {_FILTER}", *args),
        fetch(f"""SELECT conversation_id,
                   count(*) FILTER (WHERE source = 'user')::int AS messages,
                   count(*) FILTER (WHERE source = 'sophos')::int AS replies,
                   max(occurred_at) AS last_active
                   FROM messages WHERE {_MESSAGE_FILTER} AND source IN ('user', 'sophos')
                   GROUP BY conversation_id""", *args),
        fetch(f"""SELECT date_trunc('{bucket}', created_at AT TIME ZONE $4) AS bucket,
                   COALESCE(sum(total_tokens), 0)::bigint AS tokens
                   FROM llm_usage WHERE {_FILTER} GROUP BY bucket""", *args, tz),
        fetch(f"""SELECT slot, {_USAGE_TOTALS} FROM llm_usage WHERE {_FILTER}
                  GROUP BY slot ORDER BY tokens DESC, slot""", *args),
        fetch(f"""SELECT provider, model, {_USAGE_TOTALS} FROM llm_usage WHERE {_FILTER}
                  GROUP BY provider, model ORDER BY tokens DESC, model""", *args),
        pool.fetchrow(f"""SELECT
            count(*) FILTER (WHERE source = 'simple')::int AS simple,
            count(*) FILTER (WHERE source = 'llm')::int AS evaluations,
            count(*) FILTER (WHERE source = 'llm' AND result = 'irrelevant' AND status != 'invalid')::int AS irrelevant,
            count(*) FILTER (WHERE source = 'llm' AND result = 'relevant_more')::int AS relevant_more,
            count(*) FILTER (WHERE source = 'llm' AND result = 'relevant_done')::int AS relevant_done,
            count(*) FILTER (WHERE source = 'llm' AND status = 'invalid')::int AS invalid,
            count(*) FILTER (WHERE source = 'llm' AND result IS NULL)::int AS incomplete,
            count(*) FILTER (WHERE status = 'rate_limited')::int AS rate_limited,
            count(*) FILTER (WHERE status = 'locked')::int AS locked,
            count(*) FILTER (WHERE sent_count > 0)::int AS replied,
            avg(duration_ms) FILTER (WHERE source = 'llm')::float AS average_ms
            FROM trigger_events WHERE {_FILTER}""", *args),
        fetch(f"""SELECT e.conversation_id, {_NAME} AS conversation_name, e.stage, e.error,
                   count(*)::int AS count, max(e.created_at) AS last_at
                   FROM (SELECT * FROM runtime_errors WHERE {_FILTER}) e
                   LEFT JOIN conversations c ON c.id = e.conversation_id
                   GROUP BY e.conversation_id, c.id, e.stage, e.error ORDER BY last_at DESC LIMIT 5""", *args),
        pool.fetchval("""SELECT max(occurred_at) FROM messages WHERE source = 'user'
                         AND ($1::bigint IS NULL OR conversation_id = $1)""", conversation_id),
        pool.fetchval("""SELECT count(*) FROM messages WHERE enrichment_status IN ('pending','streaming')
                         AND ($1::bigint IS NULL OR conversation_id = $1)""", conversation_id),
    )
    usage_by_conversation, message_trend, names, error_count, tools = await asyncio.gather(
        fetch(f"""SELECT conversation_id, COALESCE(sum(total_tokens),0)::bigint AS tokens
                  FROM llm_usage WHERE {_FILTER} GROUP BY conversation_id""", *args),
        fetch(f"""SELECT date_trunc('{bucket}', occurred_at AT TIME ZONE $4) AS bucket,
                   count(*) FILTER (WHERE source = 'user')::int AS messages,
                   count(*) FILTER (WHERE source = 'sophos')::int AS replies
                   FROM messages WHERE {_MESSAGE_FILTER} GROUP BY bucket""", *args, tz),
        fetch(f"""SELECT c.id, {_NAME} AS name, c.kind FROM conversations c
                  WHERE ($1::bigint IS NULL OR c.id = $1)""", conversation_id),
        pool.fetchval(f"SELECT count(*) FROM runtime_errors WHERE {_FILTER}", *args),
        fetch(f"""SELECT provider, tool, {_TOOL_TOTALS} FROM tool_usage WHERE {_FILTER}
                  GROUP BY provider, tool ORDER BY provider, tool""", *args),
    )
    by_id = {r["id"]: {**dict(r), "messages": 0, "replies": 0, "tokens": 0, "last_active": None} for r in names}
    for r in message_rows:
        if r["conversation_id"] in by_id:
            by_id[r["conversation_id"]].update({k: r[k] for k in ("messages", "replies", "last_active")})
    for r in usage_by_conversation:
        if r["conversation_id"] in by_id:
            by_id[r["conversation_id"]]["tokens"] = r["tokens"]
    rows = sorted(
        (r for r in by_id.values() if r["messages"] or r["replies"] or r["tokens"]),
        key=lambda r: (-r["messages"], r["id"]),
    )
    buckets = {}
    length = 24 if bucket == "hour" else {"week": 7, "month": 30}[range_key]
    local_start = start.replace(tzinfo=None)
    for i in range(length):
        dt = local_start + (timedelta(hours=i) if bucket == "hour" else timedelta(days=i))
        buckets[dt] = {"at": dt.replace(tzinfo=start.tzinfo).isoformat(), "messages": 0, "replies": 0, "tokens": 0}
    for r in message_trend:
        if r["bucket"] in buckets:
            buckets[r["bucket"]].update(messages=r["messages"], replies=r["replies"])
    for r in usage_trend:
        if r["bucket"] in buckets:
            buckets[r["bucket"]]["tokens"] = r["tokens"]
    router = request.app.get("adapter_router")
    running = [
        r for r in get_run_tracker().runs()
        if r.status == "running" and (conversation_id is None or r.conversation_id == conversation_id)
    ]
    return web.json_response({
        "range": range_key, "start": start.isoformat(), "end": end.isoformat(), "timezone": tz,
        "totals": {**dict(usage), "messages": sum(r["messages"] for r in message_rows),
                   "replies": sum(r["replies"] for r in message_rows),
                   "active_conversations": sum(1 for r in message_rows if r["messages"]), "errors": error_count},
        "status": {"connected_accounts": router.connected_accounts() if router else None,
                   "running": len(running), "recognizing": pending,
                   "last_received": recent.isoformat() if recent else None},
        "slots": [dict(r) for r in slots], "models": [dict(r) for r in models],
        "tools": [dict(r) for r in tools],
        "trend": list(buckets.values()), "conversations": [serialize(r) for r in rows],
        "trigger": dict(trigger), "errors": [serialize(r) for r in recent_errors],
    })


@routes.get("/api/stats/{kind:usage|trigger|errors|tools}")
async def records(request: web.Request) -> web.Response:
    start, end, conversation_id, _ = parse_window(request.query)
    try:
        before = int(request.query["before"]) if "before" in request.query else None
        if before is not None and not 0 < before <= 9223372036854775807:
            raise ValueError
    except ValueError:
        raise web.HTTPBadRequest(reason="before must be a positive integer") from None
    table = {
        "usage": "llm_usage", "trigger": "trigger_events", "errors": "runtime_errors", "tools": "tool_usage",
    }[request.match_info["kind"]]
    rows = await request.app["pool"].fetch(
        f"""SELECT e.*, {_NAME} AS conversation_name
            FROM (SELECT * FROM {table} WHERE {_FILTER} AND ($4::bigint IS NULL OR id < $4)
                  ORDER BY id DESC LIMIT 51) e
            LEFT JOIN conversations c ON c.id = e.conversation_id ORDER BY e.id DESC""",
        start, end, conversation_id, before,
    )
    return web.json_response({
        "items": [serialize(r) for r in rows[:50]], "next": rows[49]["id"] if len(rows) > 50 else None,
    })
