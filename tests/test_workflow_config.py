import unittest
from unittest.mock import AsyncMock, patch

from sophos.agent.config import (
    load_active_workflow,
    save_active_workflow,
    validate_executable_workflow,
)
from sophos.agent.presets import collector_actor_workflow
from sophos.agent.workflow import NodeSpec, PortSpec, WorkflowDefinition, WorkflowValidationError
from sophos.api.routes.workflows import routes


class WorkflowConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_default_loads_preset(self) -> None:
        with patch("sophos.agent.config.runtime_config.get", return_value=None):
            workflow = load_active_workflow()
        self.assertEqual(workflow.workflow_id, "collector-actor")

    async def test_valid_graph_is_saved_as_versioned_data(self) -> None:
        workflow = collector_actor_workflow()
        set_value = AsyncMock()
        with patch("sophos.agent.config.runtime_config.set_value", set_value):
            saved = await save_active_workflow(workflow.to_dict())

        self.assertEqual(saved.revision, "1")
        set_value.assert_awaited_once()
        key, value = set_value.await_args.args
        self.assertEqual(key, "agent_workflow")
        self.assertEqual(value["entry_node"], "collector")

    def test_active_graph_rejects_node_kind_without_executor(self) -> None:
        workflow = WorkflowDefinition(
            workflow_id="future",
            revision="1",
            entry_node="router",
            nodes=(
                NodeSpec(
                    node_id="router",
                    kind="router",
                    output_ports=(PortSpec("next"),),
                ),
                NodeSpec(
                    node_id="done",
                    kind="terminal",
                    input_ports=(PortSpec("input"),),
                ),
            ),
            edges=(),
        )
        with self.assertRaisesRegex(WorkflowValidationError, "unsupported node kinds"):
            validate_executable_workflow(workflow)

    def test_workflow_api_routes_are_registered(self) -> None:
        registered = {(route.method, route.path) for route in routes}
        self.assertTrue(
            {
                ("GET", "/api/workflows/active"),
                ("PUT", "/api/workflows/active"),
                ("DELETE", "/api/workflows/active"),
                ("GET", "/api/workflows/catalog"),
                ("PUT", "/api/workflows/settings"),
                ("PUT", "/api/workflows/slots/{slot}"),
                ("GET", "/api/workflows/runs/stream"),
            }.issubset(registered)
        )

    def test_node_prompt_template_is_validated(self) -> None:
        workflow = collector_actor_workflow().to_dict()
        workflow["nodes"][0]["system_prompt"] = "{{prev_description}}"
        with self.assertRaisesRegex(WorkflowValidationError, "collector: 不支持的变量"):
            validate_executable_workflow(WorkflowDefinition.from_dict(workflow))

    def test_layout_and_node_overrides_round_trip(self) -> None:
        workflow = collector_actor_workflow().to_dict()
        workflow["layout"] = {"collector": {"x": 10, "y": 20}}
        workflow["nodes"][1]["model"]["max_tokens"] = 2048
        workflow["nodes"][1]["model"]["max_tool_rounds"] = 5
        parsed = WorkflowDefinition.from_dict(workflow)
        self.assertEqual(parsed.layout, {"collector": {"x": 10.0, "y": 20.0}})
        self.assertEqual(parsed.node("actor").model.max_tool_rounds, 5)
        self.assertEqual(parsed.to_dict()["nodes"][1]["model"]["max_tokens"], 2048)


class RunTrackerTests(unittest.TestCase):
    def test_tracker_records_steps_and_notifies_subscribers(self) -> None:
        from sophos.agent.runs import RunTracker

        tracker = RunTracker()
        queue = tracker.subscribe()
        tracker.start("r1", "wf", "1", 48)
        tracker.event("r1", "node_entered", "collector", {})
        tracker.finish("r1", "completed")
        record = tracker.get("r1")
        self.assertEqual(record.current_node, "collector")
        self.assertEqual(record.status, "completed")
        self.assertEqual([queue.get_nowait()["type"] for _ in range(3)], ["run_started", "run_event", "run_finished"])


if __name__ == "__main__":
    unittest.main()
