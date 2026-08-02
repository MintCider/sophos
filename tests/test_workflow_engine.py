import unittest

from sophos.agent import (
    NodeOutcome,
    Transcript,
    WorkflowEngine,
    WorkflowExecutionError,
    WorkflowRun,
    build_model_transition_tools,
    collector_actor_workflow,
    new_event,
)


class WorkflowEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_actor_cycle_keeps_complete_history(self) -> None:
        workflow = collector_actor_workflow()
        actor_calls = 0

        async def execute_llm(node, run):
            nonlocal actor_calls
            if node.node_id == "collector":
                run.transcript.append(
                    new_event(
                        "tool_call",
                        {"call_id": f"call-{run.step_count}", "name": "query_messages", "arguments": {}},
                        node_id=node.node_id,
                    )
                )
                run.transcript.append(
                    new_event(
                        "tool_result",
                        {"call_id": f"call-{run.step_count}", "result": {"data": []}},
                        node_id=node.node_id,
                    )
                )
                return NodeOutcome("complete_collection")
            actor_calls += 1
            if actor_calls == 1:
                return NodeOutcome(
                    "request_more_information",
                    {"requirements": ["更早的消息"]},
                )
            return NodeOutcome("completed")

        transcript = Transcript()
        transcript.append(new_event("message", {"role": "user", "content": "原始请求"}))
        run = WorkflowRun(workflow=workflow, transcript=transcript)
        result = await WorkflowEngine({"llm": execute_llm}).run(run)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.workflow_revision, "1")
        self.assertEqual(result.edge_traversals["more_information_requested"], 1)
        self.assertEqual(
            [event.kind for event in result.transcript.events].count("tool_result"),
            2,
        )
        self.assertEqual(result.transcript.events[0].payload["content"], "原始请求")

    async def test_edge_traversal_limit_is_enforced(self) -> None:
        workflow = collector_actor_workflow()

        async def execute_llm(node, run):
            if node.node_id == "collector":
                return NodeOutcome("complete_collection")
            return NodeOutcome("request_more_information", {"requirements": ["again"]})

        run = WorkflowRun(workflow=workflow, transcript=Transcript())
        with self.assertRaisesRegex(WorkflowExecutionError, "traversal limit"):
            await WorkflowEngine({"llm": execute_llm}, max_steps=16).run(run)
        self.assertEqual(run.status, "failed")

    def test_only_model_callable_outgoing_ports_become_tools(self) -> None:
        workflow = collector_actor_workflow()
        collector_tools = build_model_transition_tools(workflow, "collector")
        actor_tools = build_model_transition_tools(workflow, "actor")

        self.assertEqual([tool["function"]["name"] for tool in collector_tools], ["complete_collection"])
        self.assertEqual(
            [tool["function"]["name"] for tool in actor_tools],
            ["request_more_information"],
        )
        schema = actor_tools[0]["function"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["requirements"])


if __name__ == "__main__":
    unittest.main()
