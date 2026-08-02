import unittest

from sophos.llm.context import _format_image_descriptions
from sophos.message_store import MessageStore


class EnrichmentContextTests(unittest.TestCase):
    def test_non_text_message_starts_pending(self) -> None:
        segments = [{"type": "text", "data": {"text": "看图"}}, {"type": "image", "data": {"url": "x"}}]
        self.assertEqual(MessageStore._initial_enrichment_status(segments), "pending")

    def test_text_message_starts_completed(self) -> None:
        segments = [{"type": "text", "data": {"text": "文本"}}]
        self.assertEqual(MessageStore._initial_enrichment_status(segments), "completed")

    def test_failed_image_is_visible_as_explicit_placeholder(self) -> None:
        row = {
            "enrichment_status": "failed",
            "raw_message": [{"type": "image", "data": {"url": "x"}}],
            "extra": {"images": [{"status": "failed", "error": "timeout"}]},
        }
        self.assertEqual(_format_image_descriptions(row), "[图片解析失败]")

    def test_interrupted_image_without_extra_has_failure_placeholder(self) -> None:
        row = {
            "enrichment_status": "failed",
            "raw_message": [{"type": "image", "data": {"url": "x"}}],
            "extra": None,
        }
        self.assertEqual(_format_image_descriptions(row), "[图片解析失败]")


if __name__ == "__main__":
    unittest.main()
