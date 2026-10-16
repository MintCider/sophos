import asyncio
import unittest
from typing import Any

from sophos import prompts
from sophos.trigger_context import bot_name, build_trigger_context, persona_values
from sophos.trigger_engine import _parse_result

BOT = 7


def _row(mid: int, uid: int, text: str, *, source: str = "user", **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "message_id": mid,
        "user_id": uid,
        "nickname": "九命还魂喵呜仙" if uid == BOT else f"用户{uid}",
        "card": "",
        "source": source,
        "plain_text": text,
        "enrichment_status": extra.pop("enrichment_status", "completed"),
        "raw_message": extra.pop("raw_message", []),
        "reply_to_message_id": extra.pop("reply_to_message_id", None),
        "extra": extra or None,
    }


class _Embedding:
    async def embed_single(self, text: str) -> list[float]:
        return [1.0]


class _MemoryStore:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits

    async def get_profile_self(self) -> str | None:
        return "大家有时叫我猫"

    async def get_profile_context(self, scope_type: str, scope_id: int) -> str | None:
        return None

    async def search_similar(self, vec: list[float], *, limit: int, min_score: float) -> list[dict[str, Any]]:
        return [h for h in self.hits if h["score"] >= min_score][:limit]


class TriggerContextTests(unittest.TestCase):
    def test_plain_lines(self) -> None:
        rows = [
            _row(1, 10, "陛下腿好长"),
            _row(2, BOT, "[回复 用户10(10): 陛下腿好长]腿好长喵", source="sophos"),
            _row(3, 10, "[回复 九命还魂喵呜仙(7): 腿好长喵]哈哈", reply_to_message_id=2),
        ]
        tc = build_trigger_context(rows, self_user_id=BOT, since_id=0)
        assert tc is not None
        name = bot_name()
        self.assertEqual(
            tc.text,
            f"用户10: 陛下腿好长\n{name}: 腿好长喵\n用户10: [回复 {name}: 腿好长喵]哈哈",
        )
        self.assertEqual(tc.pending_ids, [3])

    def test_pending_image_message_is_visible(self) -> None:
        rows = [_row(1, 10, "", enrichment_status="pending", raw_message=[{"type": "image", "data": {}}])]
        tc = build_trigger_context(rows, self_user_id=BOT, since_id=0)
        assert tc is not None
        self.assertEqual(tc.text, "用户10: [图片识别中]")

    def test_since_id_collects_all_new_messages(self) -> None:
        rows = [_row(i, 10, f"m{i}") for i in range(1, 5)]
        tc = build_trigger_context(rows, self_user_id=BOT, since_id=2)
        assert tc is not None
        self.assertEqual(tc.pending_ids, [3, 4])
        self.assertEqual(tc.query, "m3\nm4")

    def test_nothing_new(self) -> None:
        rows = [_row(1, 10, "a"), _row(2, BOT, "b", source="sophos")]
        self.assertIsNone(build_trigger_context(rows, self_user_id=BOT, since_id=1))


class PersonaValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(prompts._current)

    def tearDown(self) -> None:
        prompts._current.clear()
        prompts._current.update(self._saved)

    def _values(self, hits: list[dict[str, Any]]) -> dict[str, str]:
        return asyncio.run(
            persona_values(_MemoryStore(hits), _Embedding(), conversation_id=1, query="要不要换个模型")  # type: ignore[arg-type]
        )

    def test_profiles_and_memories(self) -> None:
        prompts._current["trigger.persona"] = prompts.PromptVersion(1, "{{profile_self}}\n{{memories}}", "", None)  # type: ignore[arg-type]
        values = self._values([{"content": "主人换了模型", "created_at": None, "score": 0.8}])
        self.assertEqual(values["profile_self"], "大家有时叫我猫")
        self.assertEqual(values["profile_context"], "（暂无）")
        self.assertEqual(values["memories"], "- [?] 主人换了模型")

    def test_memories_below_threshold_or_unused(self) -> None:
        prompts._current["trigger.persona"] = prompts.PromptVersion(1, "{{memories}}", "", None)  # type: ignore[arg-type]
        self.assertEqual(self._values([{"content": "无关", "created_at": None, "score": 0.1}])["memories"], "")
        prompts._current["trigger.persona"] = prompts.PromptVersion(1, "{{profile_self}}", "", None)  # type: ignore[arg-type]
        self.assertEqual(self._values([{"content": "x", "created_at": None, "score": 0.9}])["memories"], "")


class ParseResultTests(unittest.TestCase):
    def test_strict_words(self) -> None:
        self.assertEqual(_parse_result("relevant_done"), "relevant_done")
        self.assertEqual(_parse_result("答案：relevant_more。"), "relevant_more")
        self.assertEqual(_parse_result("i am done"), "irrelevant")
        self.assertEqual(_parse_result("irrelevant / relevant_done"), "irrelevant")


if __name__ == "__main__":
    unittest.main()
