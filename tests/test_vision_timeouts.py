import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sophos import vision
from sophos.config import settings
from sophos.enrichment import EnrichmentRegistry
from sophos.llm.provider import ChatResponse, FirstTokenCallback, LLMProvider, Message
from sophos.vision import VisionGenerationTimeoutError, describe_image, process_image_segment


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

        with patch.object(settings, "vision_generation_timeout", 0.2):
            result = await describe_image(provider, b"image", on_first_token=on_first_token)

        self.assertEqual(result, "图片描述")
        self.assertTrue(first_token_seen.is_set())

    async def test_slow_first_token_is_allowed_within_generation_deadline(self) -> None:
        provider = _FakeVisionProvider(first_token_delay=0.05, completion_delay=0)
        with patch.object(settings, "vision_generation_timeout", 0.5):
            self.assertEqual(await describe_image(provider, b"image"), "图片描述")

    async def test_unfinished_request_fails_at_generation_deadline_and_cancels(self) -> None:
        provider = _FakeVisionProvider(first_token_delay=0, completion_delay=0.2)

        with (
            patch.object(settings, "vision_generation_timeout", 0.02),
            self.assertRaises(VisionGenerationTimeoutError),
        ):
            await describe_image(provider, b"image")

        self.assertTrue(provider.cancelled)


class VisionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_attempt_succeeds_and_updates_cache(self) -> None:
        describe = AsyncMock(side_effect=[VisionGenerationTimeoutError("slow"), "第二次描述"])
        update_cache = AsyncMock()
        with (
            patch.object(vision, "fetch_image", AsyncMock(return_value=(b"x", "image/png"))),
            patch.object(vision, "compute_hash", AsyncMock(return_value="h1")),
            patch.object(vision, "get_or_create_cache_entry", AsyncMock(return_value={"hit_count": 0})),
            patch.object(vision, "maybe_gif_to_grid", AsyncMock(return_value=(b"x", "image/png"))),
            patch.object(vision, "describe_image", describe),
            patch.object(vision, "update_cache_after_explore", update_cache),
        ):
            result = await process_image_segment(
                "u", pool=None, session=None, vision_provider=object(), attempts=2  # type: ignore[arg-type]
            )

        self.assertEqual(result, {"hash": "h1", "description": "第二次描述", "status": "completed"})
        self.assertEqual(describe.await_count, 2)
        update_cache.assert_awaited_once()


class EnrichmentRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_ready_releases_waiters_while_task_keeps_running(self) -> None:
        registry = EnrichmentRegistry()
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        registry.register(1, task)

        waiter = asyncio.create_task(registry.wait_for([1], timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        registry.mark_ready(1)
        self.assertTrue(await waiter)
        self.assertFalse(task.done())
        release.set()
        await task


class DeferredImageEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_recognition_is_released_then_finished_in_background(self) -> None:
        from types import SimpleNamespace

        from sophos import pipeline

        finished = asyncio.Event()
        store = SimpleNamespace(
            pool=None,
            get_context=AsyncMock(return_value=[]),
            set_enrichment_streaming=AsyncMock(),
            defer_image_enrichment=AsyncMock(),
            finish_image_enrichment=AsyncMock(side_effect=lambda *_: finished.set()),
        )
        ctx = SimpleNamespace(
            segments=[{"type": "image", "data": {"url": "u"}}],
            provider_mgr=SimpleNamespace(get_vision_provider=lambda: object()),
            store=store,
            session=None,
            conversation_id=1,
            state={"message_row_id": 7},
        )

        async def slow_images(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return [{"hash": "h", "description": "描述", "status": "completed"}]

        registry = EnrichmentRegistry()
        with (
            patch.object(pipeline, "process_message_images", slow_images),
            patch.object(pipeline, "get_enrichment_registry", return_value=registry),
            patch.object(settings, "vision_defer_seconds", 0.01),
            patch.object(settings, "vision_context_messages", 0),
        ):
            task = asyncio.create_task(pipeline._process_event_images(ctx))  # type: ignore[arg-type]
            registry.register(7, task)
            self.assertTrue(await registry.wait_for([7], timeout=0.03))
            store.defer_image_enrichment.assert_awaited_once_with(7)
            self.assertFalse(finished.is_set())
            await task

        store.finish_image_enrichment.assert_awaited_once()
        self.assertEqual(store.finish_image_enrichment.await_args.args[1][0]["description"], "描述")


if __name__ == "__main__":
    unittest.main()
