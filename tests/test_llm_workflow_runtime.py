import json
import unittest
from typing import Any

from sophos.agent import (
    AgentTool,
    LLMNodeExecutor,
    LLMWorkflowContext,
    Transcript,
    WorkflowEngine,
    WorkflowRun,
    collector_actor_workflow,
)
from sophos.llm.provider import ChatResponse, LLMProvider, Message, ProviderRequestOptions


def tool_response(name: str, arguments: dict[str, Any], call_id: str) -> ChatResponse:
    return {
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "finish_reason": "tool_calls",
        "provider": "fake",
        "native_metadata": {"model": "fake-model", "response_id": call_id},
    }


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_first_token=None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ChatResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "request_options": request_options,
            }
        )
        return self.responses.pop(0)


def agent_tool(name: str, category: str) -> AgentTool:
    return AgentTool(
        name=name,
        category=category,
        schema={
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        },
    )


class LLMWorkflowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_history_is_visible_to_actor_and_tools_are_separated(self) -> None:
        collector = FakeProvider(
            [
                tool_response("query_messages", {}, "read-1"),
                tool_response("complete_collection", {}, "handover-1"),
            ]
        )
        actor = FakeProvider([tool_response("send_message", {"text": "answer"}, "send-1")])
        executed: list[str] = []

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            executed.append(name)
            if name == "query_messages":
                return {"messages": "collected fact"}
            return {"status": "ok", "message_id": 1}

        context = LLMWorkflowContext(
            messages=[
                {"role": "system", "content": "base prompt"},
                {"role": "user", "content": "question"},
            ],
            tools=(agent_tool("query_messages", "input"), agent_tool("send_message", "output")),
            provider_resolver=lambda slot: collector if slot == "trigger" else actor,
            tool_executor=execute,
            conversation_id=7,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(executed, ["query_messages", "send_message"])
        collector_names = {
            tool["function"]["name"] for tool in collector.calls[0]["tools"]
        }
        actor_names = {tool["function"]["name"] for tool in actor.calls[0]["tools"]}
        self.assertEqual(collector_names, {"query_messages", "complete_collection"})
        self.assertEqual(actor_names, {"send_message", "request_more_information"})
        actor_history = actor.calls[0]["messages"]
        self.assertTrue(
            any(message.get("name") == "query_messages" for message in actor_history)
        )
        options = actor.calls[0]["request_options"]
        self.assertEqual(options.tool_choice, "required")
        self.assertEqual(options.cache.key, "collector-actor:1:actor")

    async def test_actor_can_return_to_collector_and_failed_send_does_not_complete(self) -> None:
        collector = FakeProvider(
            [
                tool_response("complete_collection", {}, "handover-1"),
                tool_response("query_messages", {}, "read-2"),
                tool_response("complete_collection", {}, "handover-2"),
            ]
        )
        actor = FakeProvider(
            [
                tool_response(
                    "request_more_information",
                    {"requirements": ["older messages"]},
                    "back-1",
                ),
                tool_response("send_message", {"text": "first"}, "send-failed"),
                tool_response("send_message", {"text": "second"}, "send-ok"),
            ]
        )
        send_attempts = 0

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal send_attempts
            if name == "query_messages":
                return {"messages": "older context"}
            send_attempts += 1
            if send_attempts == 1:
                return {"error": "temporary failure"}
            return {"status": "ok"}

        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "question"}],
            tools=(agent_tool("query_messages", "input"), agent_tool("send_message", "output")),
            provider_resolver=lambda slot: collector if slot == "trigger" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(send_attempts, 2)
        self.assertEqual(run.edge_traversals["more_information_requested"], 1)
        second_collection = collector.calls[1]["messages"]
        self.assertTrue(any(message.get("name") == "request_more_information" for message in second_collection))
        event_kinds = [event.kind for event in run.transcript.events]
        self.assertIn("provider_response", event_kinds)
        self.assertIn("tool_call", event_kinds)
        self.assertIn("tool_result", event_kinds)


if __name__ == "__main__":
    unittest.main()
