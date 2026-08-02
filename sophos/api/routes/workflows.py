"""Agent workflow graph API."""

from aiohttp import web

from sophos.agent.config import (
    ACTIVE_WORKFLOW_KEY,
    load_active_workflow,
    reset_active_workflow,
    save_active_workflow,
)
from sophos.agent.workflow import WorkflowValidationError

routes = web.RouteTableDef()


@routes.get("/api/workflows/active")
async def get_active_workflow(request: web.Request) -> web.Response:
    """Return the graph consumed by new message runs."""
    workflow = load_active_workflow()
    from sophos import runtime_config

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
