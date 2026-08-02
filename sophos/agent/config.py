"""Persistence and activation rules for workflow definitions."""

from __future__ import annotations

from typing import Any

from sophos import runtime_config
from sophos.agent.presets import collector_actor_workflow
from sophos.agent.workflow import WorkflowDefinition, WorkflowValidationError

ACTIVE_WORKFLOW_KEY = "agent_workflow"
SUPPORTED_NODE_KINDS = frozenset({"llm", "terminal"})
SUPPORTED_MODEL_SLOTS = frozenset({"default", "collector", "trigger"})


def load_active_workflow() -> WorkflowDefinition:
    """Load the active graph, or the built-in graph when no override exists."""
    raw = runtime_config.get(ACTIVE_WORKFLOW_KEY)
    if raw is None:
        return collector_actor_workflow()
    if not isinstance(raw, dict):
        raise WorkflowValidationError("agent_workflow must be a JSON object")
    workflow = WorkflowDefinition.from_dict(raw)
    validate_executable_workflow(workflow)
    return workflow


async def save_active_workflow(value: dict[str, Any]) -> WorkflowDefinition:
    """Validate and atomically activate a workflow through runtime config."""
    workflow = WorkflowDefinition.from_dict(value)
    validate_executable_workflow(workflow)
    await runtime_config.set_value(ACTIVE_WORKFLOW_KEY, workflow.to_dict())
    return workflow


async def reset_active_workflow() -> WorkflowDefinition:
    """Remove the override and return the built-in workflow."""
    await runtime_config.delete(ACTIVE_WORKFLOW_KEY)
    return collector_actor_workflow()


def validate_executable_workflow(workflow: WorkflowDefinition) -> None:
    """Validate constraints of the executors available in this release."""
    unsupported_kinds = sorted({node.kind for node in workflow.nodes} - SUPPORTED_NODE_KINDS)
    if unsupported_kinds:
        raise WorkflowValidationError(
            f"active workflow contains unsupported node kinds: {', '.join(unsupported_kinds)}"
        )

    for node in workflow.nodes:
        if node.model is not None and node.model.slot not in SUPPORTED_MODEL_SLOTS:
            raise WorkflowValidationError(
                f"active workflow uses unsupported model slot {node.model.slot}: {node.node_id}"
            )
        if node.kind != "terminal" and not workflow.outgoing(node.node_id):
            raise WorkflowValidationError(f"non-terminal node has no outgoing edge: {node.node_id}")

    reachable = _reachable_nodes(workflow)
    unreachable = sorted({node.node_id for node in workflow.nodes} - reachable)
    if unreachable:
        raise WorkflowValidationError(f"active workflow contains unreachable nodes: {', '.join(unreachable)}")
    if not any(node.kind == "terminal" and node.node_id in reachable for node in workflow.nodes):
        raise WorkflowValidationError("active workflow has no reachable terminal node")


def _reachable_nodes(workflow: WorkflowDefinition) -> set[str]:
    reachable: set[str] = set()
    pending = [workflow.entry_node]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(edge.to_node for edge in workflow.outgoing(node_id))
    return reachable
