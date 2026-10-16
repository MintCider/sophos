"""Agent workflow graph API: graph editing, node catalog, settings and live runs."""

import asyncio
import json
from typing import Any

from aiohttp import web

from sophos import prompts, runtime_config
from sophos.agent.config import (
    ACTIVE_WORKFLOW_KEY,
    SUPPORTED_MODEL_SLOTS,
    load_active_workflow,
    reset_active_workflow,
    save_active_workflow,
)
from sophos.agent.runs import get_run_tracker
from sophos.agent.workflow import WorkflowValidationError
from sophos.llm.provider_manager import ProviderManager

routes = web.RouteTableDef()

# Stages that run before the graph. Their settings are runtime_config keys.
STAGE_SETTINGS: dict[str, list[dict[str, Any]]] = {
    "vision": [
        {"key": "vision_max_tokens", "label": "最大输出 token", "type": "int", "min": 64, "max": 16384},
        {"key": "vision_generation_timeout", "label": "单次请求超时（秒）", "type": "float", "min": 5, "max": 600},
        {"key": "vision_defer_seconds", "label": "转后台等待（秒）", "type": "float", "min": 1, "max": 600},
        {"key": "vision_attempts", "label": "尝试次数", "type": "int", "min": 1, "max": 5},
        {"key": "vision_context_messages", "label": "参考聊天条数", "type": "int", "min": 0, "max": 50},
    ],
    "trigger": [
        {"key": "trigger_eval_max_tokens", "label": "最大输出 token", "type": "int", "min": 16, "max": 16384},
        {"key": "trigger_eval_temperature", "label": "温度", "type": "float", "min": 0, "max": 2},
        {"key": "trigger_eval_context_limit", "label": "评估上下文条数", "type": "int", "min": 1, "max": 200},
        {"key": "trigger_eval_image_wait", "label": "等待图片识别（秒）", "type": "float", "min": 0, "max": 60},
        {"key": "trigger_memory_limit", "label": "相关记忆条数（0 关闭）", "type": "int", "min": 0, "max": 10},
        {"key": "trigger_memory_min_score", "label": "记忆相似度阈值", "type": "float", "min": 0, "max": 1},
        {"key": "trigger_delay", "label": "触发延迟（秒）", "type": "float", "min": 0, "max": 30},
        {"key": "trigger_wait_timeout", "label": "等待续写超时（秒）", "type": "float", "min": 5, "max": 300},
        {"key": "trigger_qps", "label": "全局评估 QPS", "type": "float", "min": 0.01, "max": 10},
        {"key": "trigger_bucket_capacity", "label": "回复令牌桶容量", "type": "int", "min": 1, "max": 100},
        {"key": "trigger_bucket_refill", "label": "令牌恢复间隔（秒）", "type": "float", "min": 0.1, "max": 3600},
    ],
    "run": [
        {"key": "llm_max_tool_rounds", "label": "默认每节点工具轮数", "type": "int", "min": 1, "max": 200},
        {"key": "max_context_messages", "label": "上下文消息条数", "type": "int", "min": 1, "max": 500},
    ],
}

STAGE_SLOTS = {"vision": "vision", "trigger": "trigger"}
STAGE_PROMPTS = {
    "vision": ["vision.describe", "vision.refine"],
    "trigger": ["trigger.persona", "trigger.eval_format", "trigger.waiting_format"],
}


def _settings_payload() -> dict[str, list[dict[str, Any]]]:
    defaults = runtime_config.get_defaults()
    return {
        stage: [
            {**item, "value": runtime_config.get(item["key"]), "default": defaults.get(item["key"])} for item in items
        ]
        for stage, items in STAGE_SETTINGS.items()
    }


def _slots_payload(provider_mgr: ProviderManager) -> dict[str, dict[str, str]]:
    info = provider_mgr.current_info()
    slots = {}
    for slot in ("default", "collector", "vision", "trigger"):
        prefix = "" if slot == "default" else f"{slot}_"
        slots[slot] = {
            "alias": info.get(f"{prefix}alias", ""),
            "model": info.get(f"{prefix}model", ""),
            "api_type": info.get(f"{prefix}api_type", ""),
        }
    return slots


@routes.get("/api/workflows/active")
async def get_active_workflow(request: web.Request) -> web.Response:
    """Return the graph consumed by new message runs."""
    workflow = load_active_workflow()
    source = "configured" if runtime_config.get(ACTIVE_WORKFLOW_KEY) is not None else "preset"
    return web.json_response({"source": source, "workflow": workflow.to_dict()})


@routes.put("/api/workflows/active")
async def put_active_workflow(request: web.Request) -> web.Response:
    """Validate and activate a complete versioned graph definition."""
    payload = await request.json()
    value = payload.get("workflow") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(reason="workflow 必须是 JSON 对象")
    try:
        workflow = await save_active_workflow(value)
    except (KeyError, TypeError, ValueError, WorkflowValidationError) as exc:
        raise web.HTTPBadRequest(reason=str(exc)) from exc
    return web.json_response({"source": "configured", "workflow": workflow.to_dict()})


@routes.delete("/api/workflows/active")
async def delete_active_workflow(request: web.Request) -> web.Response:
    """Reset graph activation to the built-in collector/actor preset."""
    workflow = await reset_active_workflow()
    return web.json_response({"source": "preset", "workflow": workflow.to_dict()})


@routes.get("/api/workflows/catalog")
async def get_catalog(request: web.Request) -> web.Response:
    """Everything the editor needs besides the graph itself."""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    registry = request.app.get("tool_registry")
    tools = (
        [{"name": name, "category": registry.get(name).category} for name in registry.list_tools()]
        if registry is not None
        else []
    )
    return web.json_response(
        {
            "node_slots": sorted(SUPPORTED_MODEL_SLOTS),
            "slots": _slots_payload(provider_mgr),
            "settings": _settings_payload(),
            "stage_prompts": STAGE_PROMPTS,
            "stage_slots": STAGE_SLOTS,
            "prompt_keys": [spec.key for spec in prompts.specs()],
            "node_variables": prompts.NODE_VARIABLES,
            "tools": tools,
        }
    )


@routes.put("/api/workflows/settings")
async def put_settings(request: web.Request) -> web.Response:
    """Update stage settings (runtime_config keys declared in STAGE_SETTINGS)."""
    payload = await request.json()
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise web.HTTPBadRequest(reason="values 必须是对象")
    specs = {item["key"]: item for items in STAGE_SETTINGS.values() for item in items}
    parsed: dict[str, int | float] = {}
    for key, raw in values.items():
        spec = specs.get(key)
        if spec is None:
            raise web.HTTPBadRequest(reason=f"不支持的设置：{key}")
        try:
            value: int | float = int(raw) if spec["type"] == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(reason=f"{spec['label']} 必须是数字") from exc
        if not spec["min"] <= value <= spec["max"]:
            raise web.HTTPBadRequest(reason=f"{spec['label']} 范围 {spec['min']}~{spec['max']}")
        parsed[key] = value
    for key, value in parsed.items():
        await runtime_config.set_value(key, value)
    return web.json_response({"settings": _settings_payload()})


@routes.put("/api/workflows/slots/{slot}")
async def put_slot(request: web.Request) -> web.Response:
    """Hot-switch the provider/model bound to a model slot."""
    provider_mgr: ProviderManager = request.app["provider_mgr"]
    slot = request.match_info["slot"]
    payload = await request.json()
    alias = str(payload.get("alias", "")).strip()
    model = str(payload.get("model", "")).strip()
    api_type = str(payload.get("api_type") or "openai")
    if not alias or not model:
        raise web.HTTPBadRequest(reason="alias 和 model 不能为空")
    switch = {
        "default": provider_mgr.switch,
        "collector": provider_mgr.switch_collector,
        "vision": provider_mgr.switch_vision,
        "trigger": provider_mgr.switch_trigger,
    }.get(slot)
    if switch is None:
        raise web.HTTPNotFound(reason=f"未知模型槽：{slot}")
    message = await switch(alias, model, api_type=api_type)
    if not message.startswith("已切换"):
        raise web.HTTPBadRequest(reason=message)
    return web.json_response({"message": message, "slots": _slots_payload(provider_mgr)})


@routes.get("/api/workflows/runs")
async def list_runs(request: web.Request) -> web.Response:
    return web.json_response({"runs": [record.summary() for record in get_run_tracker().runs()]})


@routes.get("/api/workflows/runs/stream")
async def stream_runs(request: web.Request) -> web.StreamResponse:
    """SSE: run_started / run_event / run_finished for live node highlighting."""
    tracker = get_run_tracker()
    response = web.StreamResponse()
    response.content_type = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)
    queue = tracker.subscribe()
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                await response.write(b"event: heartbeat\ndata: {}\n\n")
                continue
            data = json.dumps(message, ensure_ascii=False)
            await response.write(f"data: {data}\n\n".encode())
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        tracker.unsubscribe(queue)
    return response


@routes.get("/api/workflows/runs/{run_id}")
async def get_run(request: web.Request) -> web.Response:
    record = get_run_tracker().get(request.match_info["run_id"])
    if record is None:
        raise web.HTTPNotFound(reason="运行记录不存在或已过期")
    return web.json_response(record.to_dict())
