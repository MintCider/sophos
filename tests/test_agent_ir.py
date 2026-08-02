import unittest

from sophos.agent import (
    EdgeSpec,
    ModelPolicy,
    NativeEnvelope,
    NodeSpec,
    PortSpec,
    Transcript,
    WorkflowDefinition,
    WorkflowValidationError,
    new_event,
)


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="collector-actor",
        revision="1",
        entry_node="collector",
        nodes=(
            NodeSpec(
                node_id="collector",
                kind="llm",
                model=ModelPolicy(slot="trigger", require_tool_call=True),
                input_ports=(PortSpec("follow_up"),),
                output_ports=(PortSpec("complete", model_callable=True),),
            ),
            NodeSpec(
                node_id="actor",
                kind="llm",
                model=ModelPolicy(slot="default"),
                input_ports=(PortSpec("input"),),
                output_ports=(PortSpec("need_more", model_callable=True), PortSpec("complete")),
            ),
        ),
        edges=(
            EdgeSpec("collected", "collector", "complete", "actor", "input"),
            EdgeSpec("retry", "actor", "need_more", "collector", "follow_up", max_traversals=2),
        ),
    )


class TranscriptTests(unittest.TestCase):
    def test_round_trip_preserves_canonical_and_native_data(self) -> None:
        transcript = Transcript()
        appended = transcript.append(
            new_event(
                "tool_call",
                {"call_id": "internal-1", "name": "query_messages", "arguments": {"limit": 5}},
                node_id="collector",
                event_id="event-1",
                native_envelopes=(
                    NativeEnvelope(
                        provider="gemini",
                        api_surface="generateContent",
                        model="gemini-test",
                        payload={"functionCall": {"name": "query_messages", "args": {"limit": 5}}},
                    ),
                ),
            )
        )
        self.assertEqual(appended.sequence, 0)

        restored = Transcript.from_list(transcript.to_list())
        self.assertEqual(restored.events, transcript.events)

    def test_rejects_duplicate_ids_and_non_contiguous_sequences(self) -> None:
        transcript = Transcript()
        transcript.append(new_event("message", {"role": "user"}, event_id="same"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            transcript.append(new_event("message", {"role": "assistant"}, event_id="same"))

        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            Transcript.from_list(
                [
                    {
                        "event_id": "late",
                        "sequence": 2,
                        "kind": "message",
                        "node_id": None,
                        "payload": {},
                        "native_envelopes": [],
                    }
                ]
            )


class WorkflowDefinitionTests(unittest.TestCase):
    def test_round_trip_allows_cycles(self) -> None:
        workflow = _workflow()
        restored = WorkflowDefinition.from_dict(workflow.to_dict())
        self.assertEqual(restored, workflow)
        self.assertEqual([edge.edge_id for edge in restored.outgoing("actor")], ["retry"])

    def test_rejects_missing_ports(self) -> None:
        with self.assertRaisesRegex(WorkflowValidationError, "missing output port"):
            WorkflowDefinition(
                workflow_id="invalid",
                revision="1",
                entry_node="start",
                nodes=(
                    NodeSpec(
                        node_id="start",
                        kind="llm",
                        model=ModelPolicy(),
                        output_ports=(PortSpec("valid"),),
                    ),
                    NodeSpec(node_id="end", kind="terminal", input_ports=(PortSpec("input"),)),
                ),
                edges=(EdgeSpec("bad", "start", "missing", "end", "input"),),
            )


if __name__ == "__main__":
    unittest.main()
