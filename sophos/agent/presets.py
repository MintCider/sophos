"""Built-in workflow definitions.

These are ordinary graph data and may later be replaced by WebUI-authored
definitions without changing the runtime.
"""

from sophos.agent.workflow import (
    CachePolicy,
    EdgeSpec,
    ModelPolicy,
    NodeSpec,
    PortSpec,
    ToolPolicy,
    WorkflowDefinition,
)


def collector_actor_workflow() -> WorkflowDefinition:
    """Default read-collect then output-act workflow."""

    return WorkflowDefinition(
        workflow_id="collector-actor",
        revision="1",
        entry_node="collector",
        nodes=(
            NodeSpec(
                node_id="collector",
                kind="llm",
                title="Collect information",
                instructions=(
                    "只调用读取工具收集回答当前请求所需的信息。"
                    "不要总结、建议或回答用户；收集完成后调用 complete_collection。"
                ),
                model=ModelPolicy(slot="trigger", require_tool_call=True),
                tools=ToolPolicy(include_categories=("input",), exclude_names=("generate_image",)),
                cache=CachePolicy(scope="node"),
                input_ports=(PortSpec("initial"), PortSpec("follow_up")),
                output_ports=(
                    PortSpec(
                        "complete_collection",
                        "已有工具调用历史足以交给输出节点时调用。",
                        model_callable=True,
                    ),
                ),
            ),
            NodeSpec(
                node_id="actor",
                kind="llm",
                title="Decide and act",
                instructions=(
                    "根据完整对话和读取工具历史完成请求。只使用当前开放的输出工具。"
                    "若缺少必要信息，调用 request_more_information 返回收集节点。"
                ),
                model=ModelPolicy(slot="default"),
                tools=ToolPolicy(include_categories=("output",)),
                cache=CachePolicy(scope="node"),
                input_ports=(PortSpec("collected"),),
                output_ports=(
                    PortSpec(
                        "request_more_information",
                        "现有工具结果不足时，列出需要补充的具体信息。",
                        payload_schema={
                            "type": "object",
                            "properties": {
                                "requirements": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "需要收集节点补充的具体信息",
                                }
                            },
                            "required": ["requirements"],
                        },
                        model_callable=True,
                    ),
                    PortSpec("completed", "输出工具成功执行后由宿主触发。"),
                ),
            ),
            NodeSpec(
                node_id="done",
                kind="terminal",
                title="Completed",
                input_ports=(PortSpec("input"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="collection_complete",
                from_node="collector",
                from_port="complete_collection",
                to_node="actor",
                to_port="collected",
                trigger="model",
            ),
            EdgeSpec(
                edge_id="more_information_requested",
                from_node="actor",
                from_port="request_more_information",
                to_node="collector",
                to_port="follow_up",
                trigger="model",
                max_traversals=3,
            ),
            EdgeSpec(
                edge_id="response_completed",
                from_node="actor",
                from_port="completed",
                to_node="done",
                to_port="input",
                trigger="host",
            ),
        ),
    )

