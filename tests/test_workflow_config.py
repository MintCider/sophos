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
        self.assertEqual(
            registered,
            {
                ("GET", "/api/workflows/active"),
                ("PUT", "/api/workflows/active"),
                ("DELETE", "/api/workflows/active"),
            },
        )


if __name__ == "__main__":
    unittest.main()
