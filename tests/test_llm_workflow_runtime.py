import json
import unittest
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

from sophos.agent import (
    AgentTool,
    LLMNodeExecutor,
    LLMWorkflowContext,
    Transcript,
    WorkflowEngine,
    WorkflowRun,
    collector_actor_workflow,
)
from sophos.llm.anthropic import AnthropicProvider
from sophos.llm.gemini import GeminiProvider
from sophos.llm.openai_compat import OpenAICompatProvider
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


class TimeoutThenSuccessProvider(FakeProvider):
    def __init__(self, responses: list[ChatResponse], failures: int) -> None:
        super().__init__(responses)
        self.failures = failures
        self.attempts = 0

    async def chat(self, *args, **kwargs) -> ChatResponse:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TimeoutError("provider timed out")
        return await super().chat(*args, **kwargs)


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
    async def test_silent_finish_cancels_parallel_send(self):
        collector = FakeProvider([tool_response("complete_collection", {}, "collect")])
        finish = tool_response("finish_without_reply", {}, "finish")
        finish["message"]["tool_calls"].insert(
            0, tool_response("send_message", {"text": "不发言"}, "send")["message"]["tool_calls"][0],
        )
        finish["message"]["content"] = "保持沉默"
        actor = FakeProvider([finish])
        execute = AsyncMock(return_value={"status": "completed", "sent": False})
        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "群聊"}],
            tools=(agent_tool("send_message", "output"), agent_tool("finish_without_reply", "output")),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)
        self.assertEqual(run.status, "completed")
        execute.assert_awaited_once_with("finish_without_reply", {})
        self.assertNotIn("finish_without_reply", str(collector.calls[0]["tools"]))

    async def test_plain_text_is_sent_unless_silence_is_explicit(self):
        collector = FakeProvider([tool_response("complete_collection", {}, "collect")])
        actor = FakeProvider([{"message": {"role": "assistant", "content": "普通回复"}}])
        execute = AsyncMock(return_value={"status": "ok"})
        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "群聊"}],
            tools=(agent_tool("send_message", "output"), agent_tool("finish_without_reply", "output")),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)
        self.assertEqual(run.status, "completed")
        execute.assert_awaited_once_with("send_message", {"text": "普通回复"})

    async def test_provider_timeout_is_retried_three_times(self) -> None:
        collector = TimeoutThenSuccessProvider(
            [tool_response("complete_collection", {}, "handover-1")],
            failures=3,
        )
        actor = FakeProvider([tool_response("send_message", {"text": "answer"}, "send-1")])

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok"}

        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "question"}],
            tools=(agent_tool("send_message", "output"),),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())

        with patch("sophos.agent.llm_runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(collector.attempts, 4)
        self.assertEqual(sleep.await_count, 3)

    async def test_collector_history_is_visible_to_actor_and_tools_are_separated(self) -> None:
        read = tool_response("query_messages", {}, "read-1")
        read["message"].update({
            "content": "准备查询，建议不要回复",
            "reasoning_content": "collector private reasoning",
            "_gemini_text_signature": "collector-text-signature",
            "provider_specific_fields": {"thinking": "private"},
        })
        read["message"]["tool_calls"][0]["thought_signature"] = "collector-tool-signature"
        handover = tool_response("complete_collection", {}, "handover-1")
        handover["message"]["content"] = "收集完毕，请保持沉默"
        original_read = deepcopy(read)
        collector = FakeProvider(
            [
                read,
                handover,
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
                {"role": "assistant", "content": "earlier public reply"},
                {"role": "user", "content": "question"},
            ],
            tools=(agent_tool("query_messages", "input"), agent_tool("send_message", "output")),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
            conversation_id=7,
            system_prompt_renderer=lambda node: "collector prompt" if node.node_id == "collector" else None,
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
        self.assertTrue(collector.calls[0]["messages"][0]["content"].startswith("collector prompt"))
        self.assertTrue(actor.calls[0]["messages"][0]["content"].startswith("base prompt"))
        actor_history = actor.calls[0]["messages"]
        self.assertEqual(actor_history[1:], [
            {"role": "assistant", "content": "earlier public reply"},
            {"role": "user", "content": "question"},
            tool_response("query_messages", {}, "read-1")["message"],
            {"role": "tool", "tool_call_id": "read-1", "name": "query_messages",
             "content": json.dumps({"messages": "collected fact"})},
        ])
        # Private messages survive for the originating node and the raw audit trail.
        self.assertIn(original_read["message"], collector.calls[1]["messages"])
        self.assertIn(original_read["message"], context.messages)
        self.assertEqual(read, original_read)
        recorded = [e.payload["message"] for e in run.transcript.events if e.kind == "provider_response"]
        self.assertIn(original_read["message"], recorded)
        self.assertIn(handover["message"], recorded)
        # Provider adaptation accepts the canonical cross-node call/result pair.
        openai = OpenAICompatProvider(base_url="https://example.test", api_key="test", model="test")
        self.assertEqual(openai._build_payload(actor_history, None, None, None, None)["messages"][-1]["content"],
                         json.dumps({"messages": "collected fact"}))
        _, anthropic_messages = AnthropicProvider._convert_messages(actor_history)
        self.assertEqual(anthropic_messages[-2]["content"][0]["type"], "tool_use")
        self.assertEqual(anthropic_messages[-1]["content"][0]["tool_use_id"], "read-1")
        _, gemini_messages = GeminiProvider._convert_messages(actor_history)
        self.assertIn("query_messages", json.dumps(gemini_messages))
        self.assertIn("collected fact", json.dumps(gemini_messages))
        self.assertNotIn("thoughtSignature", json.dumps(gemini_messages))
        options = actor.calls[0]["request_options"]
        self.assertEqual(options.tool_choice, "required")
        self.assertEqual(options.cache.key, "collector-actor:1:actor")

    async def test_actor_can_return_to_collector_and_failed_send_does_not_complete(self) -> None:
        first_collection = tool_response("complete_collection", {}, "handover-1")
        first_collection["message"]["reasoning_content"] = "collector private reasoning"
        collector = FakeProvider(
            [
                first_collection,
                tool_response("query_messages", {}, "read-2"),
                tool_response("complete_collection", {}, "handover-2"),
            ]
        )
        follow_up = tool_response("request_more_information", {"requirements": ["older messages"]}, "back-1")
        follow_up["message"].update({"content": "actor private draft", "reasoning_content": "actor reasoning"})
        follow_up["message"]["tool_calls"][0]["thought_signature"] = "actor signature"
        actor = FakeProvider(
            [
                follow_up,
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
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(send_attempts, 2)
        self.assertEqual(run.edge_traversals["more_information_requested"], 1)
        second_collection = collector.calls[1]["messages"]
        self.assertTrue(any(message.get("name") == "request_more_information" for message in second_collection))
        serialized = json.dumps(second_collection)
        self.assertIn("older messages", serialized)
        self.assertNotIn("actor private draft", serialized)
        self.assertNotIn("actor reasoning", serialized)
        self.assertNotIn("actor signature", serialized)
        self.assertIn(first_collection["message"], second_collection)
        self.assertIn(follow_up["message"], actor.calls[1]["messages"])
        self.assertNotIn("collector private reasoning", json.dumps(actor.calls[1]["messages"]))
        # Actor's own failed send still has its matching result on the next round.
        self.assertTrue(any(m.get("tool_call_id") == "send-failed" for m in actor.calls[2]["messages"]))
        event_kinds = [event.kind for event in run.transcript.events]
        self.assertIn("provider_response", event_kinds)
        self.assertIn("tool_call", event_kinds)
        self.assertIn("tool_result", event_kinds)

    async def test_mixed_tool_batch_keeps_pairs_errors_and_refreshed_context(self) -> None:
        collected = tool_response("query_messages", {"page": 1}, "read-1")
        collected["message"]["content"] = "private collection summary"
        second_read = tool_response("query_messages", {"page": 2}, "")["message"]["tool_calls"][0]
        second_read["function"]["native_reasoning"] = "private function metadata"
        collected["message"]["tool_calls"].extend([
            tool_response("complete_collection", {}, "handoff")["message"]["tool_calls"][0],
            second_read,
        ])
        original = deepcopy(collected)
        # Isolation is by node, even when both nodes use exactly the same provider.
        provider = FakeProvider([collected, tool_response("send_message", {"text": "answer"}, "send")])
        execute = AsyncMock(side_effect=[
            {"fact": "first page"}, {"error": "second page unavailable"}, {"status": "ok"},
        ])
        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "question"}],
            tools=(agent_tool("query_messages", "input"), agent_tool("send_message", "output")),
            provider_resolver=lambda slot: provider,
            tool_executor=execute,
            context_refresher=AsyncMock(side_effect=["new user message", None]),
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)
        history = provider.calls[1]["messages"]
        calls = next(m["tool_calls"] for m in history if m.get("tool_calls"))
        results = [m for m in history if m.get("role") == "tool"]
        self.assertEqual([c["function"]["name"] for c in calls], ["query_messages"] * 2)
        self.assertEqual([c["id"] for c in calls], [r["tool_call_id"] for r in results])
        self.assertTrue(all(c["id"] for c in calls))
        self.assertIn("second page unavailable", results[1]["content"])
        self.assertEqual(history[-1], {"role": "user", "content": "new user message"})
        self.assertNotIn("private", json.dumps(history))
        self.assertNotIn("handoff", json.dumps(history))
        self.assertEqual(collected, original)
        # Mutating a provider's request view must not alter raw history or audit events.
        calls[0]["function"]["arguments"] = "changed"
        self.assertEqual(context.messages[1]["tool_calls"][0]["function"]["arguments"], '{"page": 1}')
        self.assertEqual(run.transcript.events[0].payload["message"], original["message"])

    async def test_actor_sends_plain_text_when_model_skips_tools(self) -> None:
        collector = FakeProvider([tool_response("complete_collection", {}, "handover-1")])
        actor = FakeProvider(
            [
                {
                    "message": {"role": "assistant", "content": "plain reply"},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    "finish_reason": "stop",
                    "provider": "fake",
                }
            ]
        )
        sent: list[str] = []

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            if name == "send_message":
                sent.append(str(params["text"]))
                return {"status": "ok"}
            return {"status": "ok"}

        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "question"}],
            tools=(agent_tool("send_message", "output"),),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(sent, ["plain reply"])

    async def test_collector_plain_text_completes_collection(self) -> None:
        collector = FakeProvider(
            [
                {
                    "message": {"role": "assistant", "content": "enough context"},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    "finish_reason": "stop",
                    "provider": "fake",
                }
            ]
        )
        actor = FakeProvider([tool_response("send_message", {"text": "ok"}, "send-1")])
        executed: list[str] = []

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            executed.append(name)
            return {"status": "ok"}

        context = LLMWorkflowContext(
            messages=[{"role": "user", "content": "question"}],
            tools=(agent_tool("send_message", "output"),),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        run = WorkflowRun(collector_actor_workflow(), Transcript())
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}).run(run)

        self.assertEqual(run.status, "completed")
        self.assertEqual(executed, ["send_message"])
        self.assertNotIn("enough context", json.dumps(actor.calls[0]["messages"]))
        self.assertIn({"role": "assistant", "content": "enough context"}, context.messages)


class EngineObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_reports_node_entries_and_edges(self) -> None:
        from sophos.agent.runs import RunTracker

        collector = FakeProvider([tool_response("complete_collection", {}, "c1")])
        actor = FakeProvider([tool_response("send_message", {"text": "hi"}, "s1")])

        async def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"status": "ok"}

        context = LLMWorkflowContext(
            messages=[{"role": "system", "content": ""}, {"role": "user", "content": "q"}],
            tools=(agent_tool("send_message", "output"),),
            provider_resolver=lambda slot: collector if slot == "collector" else actor,
            tool_executor=execute,
        )
        tracker = RunTracker()
        run = WorkflowRun(collector_actor_workflow(), Transcript(), conversation_id=5)
        await WorkflowEngine({"llm": LLMNodeExecutor(context)}, observer=tracker).run(run)

        record = tracker.get(run.run_id)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.conversation_id, 5)
        entered = [e["node_id"] for e in record.events if e["kind"] == "node_entered"]
        self.assertEqual(entered, ["collector", "actor", "done"])


if __name__ == "__main__":
    unittest.main()
