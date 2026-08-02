import asyncio
import unittest
from unittest.mock import patch

from sophos.config import settings
from sophos.llm.provider import ChatResponse, FirstTokenCallback, LLMProvider, Message
from sophos.vision import (
    VisionFirstTokenTimeoutError,
    VisionGenerationTimeoutError,
    describe_image,
)


class _FakeVisionProvider(LLMProvider):
    def __init__(self, *, first_token_delay: float, completion_delay: float) -> None:
        self.first_token_delay = first_token_delay
        self.completion_delay = completion_delay
        self.cancelled = False

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_first_token: FirstTokenCallback | None = None,
    ) -> ChatResponse:
        del messages, tools, temperature, max_tokens
        try:
            await asyncio.sleep(self.first_token_delay)
            if on_first_token is not None:
                await on_first_token()
            await asyncio.sleep(self.completion_delay)
            return {
                "message": {"role": "assistant", "content": "图片描述"},
                "usage": None,
                "finish_reason": "stop",
            }
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class VisionTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_reports_first_token_and_returns_complete_description(self) -> None:
        provider = _FakeVisionProvider(first_token_delay=0, completion_delay=0)
        first_token_seen = asyncio.Event()

        async def on_first_token() -> None:
            first_token_seen.set()

        with (
            patch.object(settings, "vision_ttft_timeout", 0.1),
            patch.object(settings, "vision_generation_timeout", 0.2),
        ):
            result = await describe_image(provider, b"image", on_first_token=on_first_token)

        self.assertEqual(result, "图片描述")
        self.assertTrue(first_token_seen.is_set())

    async def test_missing_first_token_fails_and_cancels_request(self) -> None:
        provider = _FakeVisionProvider(first_token_delay=0.1, completion_delay=0)

        with (
            patch.object(settings, "vision_ttft_timeout", 0.01),
            patch.object(settings, "vision_generation_timeout", 0.2),
            self.assertRaises(VisionFirstTokenTimeoutError),
        ):
            await describe_image(provider, b"image")

        self.assertTrue(provider.cancelled)

    async def test_unfinished_stream_fails_at_total_generation_deadline(self) -> None:
        provider = _FakeVisionProvider(first_token_delay=0, completion_delay=0.1)

        with (
            patch.object(settings, "vision_ttft_timeout", 0.05),
            patch.object(settings, "vision_generation_timeout", 0.02),
            self.assertRaises(VisionGenerationTimeoutError),
        ):
            await describe_image(provider, b"image")

        self.assertTrue(provider.cancelled)


if __name__ == "__main__":
    unittest.main()
