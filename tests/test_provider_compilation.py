import json
import unittest
from collections import deque

from sophos.llm.anthropic import AnthropicProvider
from sophos.llm.gemini import GeminiProvider
from sophos.llm.openai_compat import OpenAICompatProvider
from sophos.llm.provider import CachePlan, OpenAIRequestPolicy, ProviderRequestOptions
from sophos.llm.schema import (
    compile_openai_strict_schema,
    strip_boundary_nulls,
)

TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Lookup an item",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "filters": {
                    "type": "object",
                    "properties": {"owner": {"type": "string"}},
                },
            },
            "required": ["query"],
        },
    },
}


class StrictSchemaTests(unittest.TestCase):
    def test_openai_compilation_is_recursive_and_preserves_canonical_schema(self) -> None:
        canonical = TOOL["function"]["parameters"]
        compiled = compile_openai_strict_schema(canonical)

        self.assertNotIn("additionalProperties", canonical)
        self.assertEqual(compiled["required"], ["query", "limit", "filters"])
        self.assertFalse(compiled["additionalProperties"])
        self.assertEqual(compiled["properties"]["limit"]["type"], ["integer", "null"])
        nested = compiled["properties"]["filters"]
        self.assertFalse(nested["additionalProperties"])
        self.assertEqual(nested["required"], ["owner"])

    def test_boundary_nulls_are_removed_only_for_optional_fields(self) -> None:
        canonical = TOOL["function"]["parameters"]
        params = {"query": "x", "limit": None, "filters": {"owner": None}}
        self.assertEqual(strip_boundary_nulls(params, canonical), {"query": "x", "filters": {}})


class ProviderCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
        self.options = ProviderRequestOptions(
            tool_choice="required",
            cache=CachePlan(key="workflow:collector", preferred_ttl_seconds=3600),
        )

    def test_openai_compatible_enables_strict_tools_and_cache_key(self) -> None:
        provider = OpenAICompatProvider(
            base_url="https://gateway.example/v1",
            api_key="test",
            model="model",
        )
        payload = provider._build_payload(self.messages, [TOOL], None, None, self.options)

        function = payload["tools"][0]["function"]
        self.assertTrue(function["strict"])
        self.assertFalse(function["parameters"]["additionalProperties"])
        self.assertEqual(payload["tool_choice"], "required")
        self.assertEqual(payload["prompt_cache_key"], "workflow:collector")

    def test_openai_policy_filters_unsupported_body_parameters(self) -> None:
        policy = OpenAIRequestPolicy(
            allowed_body_parameters=frozenset({"model", "messages", "max_tokens", "tools"}),
            accumulated_message_fields=frozenset({"reasoning_content"}),
            requires_assistant_content_for_tool_calls=True,
            strict_optional_mode="required",
        )
        provider = OpenAICompatProvider(
            base_url="https://api.deepseek.com/beta",
            api_key="test",
            model="deepseek-v4-flash",
            default_temperature=0.0,
            extra_body={"thinking": {"type": "enabled"}},
            request_policy=policy,
        )
        payload = provider._build_payload(self.messages, [TOOL], None, None, self.options)

        self.assertEqual(set(payload), {"model", "messages", "max_tokens", "tools"})
        self.assertTrue(payload["tools"][0]["function"]["strict"])
        self.assertEqual(
            payload["tools"][0]["function"]["parameters"]["properties"]["limit"]["type"],
            "integer",
        )

    def test_openai_policy_keeps_reasoning_history_and_required_content(self) -> None:
        provider = OpenAICompatProvider(
            base_url="https://api.deepseek.com/beta",
            api_key="test",
            model="deepseek-v4-flash",
            request_policy=OpenAIRequestPolicy(
                accumulated_message_fields=frozenset({"reasoning_content"}),
                requires_assistant_content_for_tool_calls=True,
            ),
        )
        response = provider._parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "full reasoning",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )

        self.assertEqual(response["message"]["reasoning_content"], "full reasoning")
        self.assertEqual(response["message"]["content"], "")


class ProviderStreamingPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
        self.options = ProviderRequestOptions(
            tool_choice="required",
            cache=CachePlan(key="workflow:collector", preferred_ttl_seconds=3600),
        )

    async def test_reasoning_content_is_accumulated_across_stream_chunks(self) -> None:
        class Content:
            def __init__(self) -> None:
                second_chunk = {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "second",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
                self.lines = deque(
                    [
                        b'data: {"choices":[{"delta":{"reasoning_content":"first "}}]}\n',
                        f"data: {json.dumps(second_chunk)}\n".encode(),
                        b"data: [DONE]\n",
                    ]
                )

            async def readline(self) -> bytes:
                return self.lines.popleft() if self.lines else b""

        class Response:
            content = Content()

        provider = OpenAICompatProvider(
            base_url="https://api.deepseek.com/beta",
            api_key="test",
            model="deepseek-v4-flash",
            request_policy=OpenAIRequestPolicy(
                accumulated_message_fields=frozenset({"reasoning_content"}),
                requires_assistant_content_for_tool_calls=True,
            ),
        )
        response = await provider._consume_stream(Response())  # type: ignore[arg-type]

        self.assertEqual(response["message"]["reasoning_content"], "first second")
        self.assertEqual(response["message"]["content"], "")

    def test_anthropic_uses_native_strict_and_cache_controls(self) -> None:
        provider = AnthropicProvider(
            base_url="https://api.anthropic.com",
            api_key="test",
            model="model",
        )
        payload = provider._build_payload(self.messages, [TOOL], None, None, self.options)

        self.assertTrue(payload["tools"][0]["strict"])
        self.assertFalse(payload["tools"][0]["input_schema"]["additionalProperties"])
        self.assertEqual(payload["tool_choice"], {"type": "any"})
        self.assertEqual(payload["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_gemini_uses_validated_or_any_function_calling(self) -> None:
        provider = GeminiProvider(
            base_url="https://generativelanguage.googleapis.com",
            api_key="test",
            model="model",
        )
        required_payload = provider._build_payload(
            self.messages,
            [TOOL],
            None,
            None,
            self.options,
        )
        auto_payload = provider._build_payload(
            self.messages,
            [TOOL],
            None,
            None,
            ProviderRequestOptions(),
        )

        self.assertEqual(
            required_payload["toolConfig"]["functionCallingConfig"]["mode"],
            "ANY",
        )
        self.assertEqual(
            auto_payload["toolConfig"]["functionCallingConfig"]["mode"],
            "VALIDATED",
        )

    def test_native_cache_usage_is_preserved(self) -> None:
        response = GeminiProvider._parse_gemini_response(
            {
                "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 105,
                    "cachedContentTokenCount": 80,
                },
                "responseId": "response-1",
            }
        )

        usage = response["usage"]
        assert usage is not None
        self.assertEqual(usage["cached_input_tokens"], 80)
        self.assertEqual(response["native_metadata"]["responseId"], "response-1")


if __name__ == "__main__":
    unittest.main()
