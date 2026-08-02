"""Generic runtime for versioned workflow graphs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from sophos.agent.transcript import Transcript, new_event
from sophos.agent.workflow import EdgeSpec, NodeSpec, WorkflowDefinition

RunStatus = Literal["running", "completed", "failed"]


class WorkflowExecutionError(RuntimeError):
    """Raised when a workflow run cannot advance deterministically."""


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """Result returned by a node executor."""

    output_port: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowRun:
    """Mutable cursor over an immutable workflow definition and transcript."""

    workflow: WorkflowDefinition
    transcript: Transcript
    run_id: str = field(default_factory=lambda: uuid4().hex)
    current_node: str | None = None
    status: RunStatus = "running"
    step_count: int = 0
    edge_traversals: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.current_node is None:
            self.current_node = self.workflow.entry_node

    @property
    def workflow_revision(self) -> str:
        return self.workflow.revision

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow.workflow_id,
            "workflow_revision": self.workflow.revision,
            "current_node": self.current_node,
            "status": self.status,
            "step_count": self.step_count,
            "edge_traversals": dict(self.edge_traversals),
        }


NodeExecutor = Callable[[NodeSpec, WorkflowRun], Awaitable[NodeOutcome]]
EdgeSelector = Callable[
    [WorkflowRun, tuple[EdgeSpec, ...], NodeOutcome],
    Awaitable[EdgeSpec],
]


class WorkflowEngine:
    """Execute arbitrary workflow graphs using registered node executors."""

    def __init__(
        self,
        executors: dict[str, NodeExecutor],
        *,
        edge_selector: EdgeSelector | None = None,
        max_steps: int = 32,
    ) -> None:
        self._executors = dict(executors)
        self._edge_selector = edge_selector
        self._max_steps = max_steps

    async def run(self, run: WorkflowRun) -> WorkflowRun:
        while run.status == "running":
            if run.step_count >= self._max_steps:
                run.status = "failed"
                raise WorkflowExecutionError(f"workflow exceeded max_steps={self._max_steps}")

            if run.current_node is None:
                run.status = "failed"
                raise WorkflowExecutionError("workflow has no current node")
            node = run.workflow.node(run.current_node)
            if node.kind == "terminal":
                run.status = "completed"
                return run

            executor = self._executors.get(node.kind)
            if executor is None:
                run.status = "failed"
                raise WorkflowExecutionError(f"no executor registered for node kind: {node.kind}")

            outcome = await executor(node, run)
            run.step_count += 1
            run.transcript.append(
                new_event(
                    "transition_requested",
                    {"from_port": outcome.output_port, "payload": outcome.payload},
                    node_id=node.node_id,
                )
            )
            candidates = tuple(
                edge
                for edge in run.workflow.outgoing(node.node_id)
                if edge.from_port == outcome.output_port
            )
            edge = await self._select_edge(run, candidates, outcome)
            self._check_traversal_limit(run, edge)
            run.edge_traversals[edge.edge_id] = run.edge_traversals.get(edge.edge_id, 0) + 1
            previous_node = run.current_node
            run.current_node = edge.to_node
            run.transcript.append(
                new_event(
                    "transition_applied",
                    {
                        "edge_id": edge.edge_id,
                        "from_node": previous_node,
                        "from_port": edge.from_port,
                        "to_node": edge.to_node,
                        "to_port": edge.to_port,
                        "payload": outcome.payload,
                    },
                    node_id=edge.to_node,
                )
            )
        return run

    async def _select_edge(
        self,
        run: WorkflowRun,
        candidates: tuple[EdgeSpec, ...],
        outcome: NodeOutcome,
    ) -> EdgeSpec:
        if not candidates:
            run.status = "failed"
            raise WorkflowExecutionError(
                f"no edge for node={run.current_node} output_port={outcome.output_port}"
            )
        if len(candidates) == 1:
            return candidates[0]
        if self._edge_selector is None:
            run.status = "failed"
            raise WorkflowExecutionError(
                f"multiple edges require selector for node={run.current_node} output_port={outcome.output_port}"
            )
        return await self._edge_selector(run, candidates, outcome)

    @staticmethod
    def _check_traversal_limit(run: WorkflowRun, edge: EdgeSpec) -> None:
        if edge.max_traversals is None:
            return
        traversed = run.edge_traversals.get(edge.edge_id, 0)
        if traversed >= edge.max_traversals:
            run.status = "failed"
            raise WorkflowExecutionError(
                f"edge traversal limit reached: {edge.edge_id} ({edge.max_traversals})"
            )


def build_model_transition_tools(workflow: WorkflowDefinition, node_id: str) -> list[dict[str, Any]]:
    """Expose model-callable output ports as strict OpenAI-style function schemas."""

    node = workflow.node(node_id)
    outgoing_ports = {edge.from_port for edge in workflow.outgoing(node_id) if edge.trigger == "model"}
    tools: list[dict[str, Any]] = []
    for port in node.output_ports:
        if not port.model_callable or port.port_id not in outgoing_ports:
            continue
        parameters = dict(port.payload_schema)
        if parameters.get("type") == "object":
            parameters.setdefault("additionalProperties", False)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": port.port_id,
                    "description": port.description,
                    "parameters": parameters,
                    "strict": True,
                },
            }
        )
    return tools

