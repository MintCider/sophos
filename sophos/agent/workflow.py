"""Versioned, data-defined workflow graph IR."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

NodeKind = Literal["llm", "terminal", "router", "policy", "human_approval", "subworkflow"]
EdgeTrigger = Literal["model", "automatic", "host"]


class WorkflowValidationError(ValueError):
    """Raised when a workflow graph cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class PortSpec:
    port_id: str
    description: str = ""
    payload_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    model_callable: bool = False


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    slot: str = "default"
    temperature: float | None = None
    max_tokens: int | None = None
    require_tool_call: bool = False


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    include_categories: tuple[str, ...] = ()
    include_names: tuple[str, ...] = ()
    exclude_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    projection: Literal["full"] = "full"
    include_native_envelopes: bool = True


@dataclass(frozen=True, slots=True)
class CachePolicy:
    enabled: bool = True
    scope: Literal["workflow", "node", "conversation", "principal"] = "node"
    preferred_ttl_seconds: int | None = None
    cache_transcript_checkpoints: bool = True


@dataclass(frozen=True, slots=True)
class NodeSpec:
    node_id: str
    kind: NodeKind
    title: str = ""
    instructions: str = ""
    model: ModelPolicy | None = None
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    context: ContextPolicy = field(default_factory=ContextPolicy)
    cache: CachePolicy = field(default_factory=CachePolicy)
    input_ports: tuple[PortSpec, ...] = ()
    output_ports: tuple[PortSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    edge_id: str
    from_node: str
    from_port: str
    to_node: str
    to_port: str
    trigger: EdgeTrigger = "model"
    guard: dict[str, Any] | None = None
    max_traversals: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: str
    revision: str
    entry_node: str
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        nodes = {node.node_id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise WorkflowValidationError("workflow node_id values must be unique")
        if self.entry_node not in nodes:
            raise WorkflowValidationError(f"entry node does not exist: {self.entry_node}")

        edge_ids = {edge.edge_id for edge in self.edges}
        if len(edge_ids) != len(self.edges):
            raise WorkflowValidationError("workflow edge_id values must be unique")

        for node in self.nodes:
            if node.kind == "llm" and node.model is None:
                raise WorkflowValidationError(f"llm node requires model policy: {node.node_id}")
            self._validate_unique_ports(node, node.input_ports, "input")
            self._validate_unique_ports(node, node.output_ports, "output")

        for edge in self.edges:
            source = nodes.get(edge.from_node)
            target = nodes.get(edge.to_node)
            if source is None or target is None:
                raise WorkflowValidationError(f"edge references missing node: {edge.edge_id}")
            if edge.from_port not in {port.port_id for port in source.output_ports}:
                raise WorkflowValidationError(f"edge references missing output port: {edge.edge_id}")
            if edge.to_port not in {port.port_id for port in target.input_ports}:
                raise WorkflowValidationError(f"edge references missing input port: {edge.edge_id}")
            if edge.max_traversals is not None and edge.max_traversals < 1:
                raise WorkflowValidationError(f"edge max_traversals must be positive: {edge.edge_id}")

    @staticmethod
    def _validate_unique_ports(node: NodeSpec, ports: tuple[PortSpec, ...], direction: str) -> None:
        port_ids = {port.port_id for port in ports}
        if len(port_ids) != len(ports):
            raise WorkflowValidationError(f"duplicate {direction} port on node: {node.node_id}")

    def node(self, node_id: str) -> NodeSpec:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def outgoing(self, node_id: str) -> tuple[EdgeSpec, ...]:
        return tuple(edge for edge in self.edges if edge.from_node == node_id)

    def edge(self, edge_id: str) -> EdgeSpec:
        for edge in self.edges:
            if edge.edge_id == edge_id:
                return edge
        raise KeyError(edge_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowDefinition:
        nodes = []
        for raw_node in value.get("nodes") or []:
            model_raw = raw_node.get("model")
            nodes.append(
                NodeSpec(
                    node_id=str(raw_node["node_id"]),
                    kind=raw_node["kind"],
                    title=str(raw_node.get("title", "")),
                    instructions=str(raw_node.get("instructions", "")),
                    model=ModelPolicy(**model_raw) if model_raw else None,
                    tools=ToolPolicy(**(raw_node.get("tools") or {})),
                    context=ContextPolicy(**(raw_node.get("context") or {})),
                    cache=CachePolicy(**(raw_node.get("cache") or {})),
                    input_ports=tuple(PortSpec(**port) for port in raw_node.get("input_ports") or []),
                    output_ports=tuple(PortSpec(**port) for port in raw_node.get("output_ports") or []),
                )
            )
        return cls(
            workflow_id=str(value["workflow_id"]),
            revision=str(value["revision"]),
            entry_node=str(value["entry_node"]),
            nodes=tuple(nodes),
            edges=tuple(EdgeSpec(**edge) for edge in value.get("edges") or []),
        )

