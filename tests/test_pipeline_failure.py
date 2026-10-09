import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sophos.pipeline import _notify_workflow_failure


class PipelineFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_failure_notifies_current_conversation(self) -> None:
        message_service = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(conversation_id=42, message_service=message_service)

        await _notify_workflow_failure(context)

        request = message_service.send_message.await_args.args[0]
        self.assertEqual(request.conversation_id, 42)
        self.assertEqual(request.text, "模型服务暂时无响应，请稍后再试。")

    async def test_notification_failure_is_suppressed(self) -> None:
        message_service = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        context = SimpleNamespace(conversation_id=42, message_service=message_service)

        await _notify_workflow_failure(context)


if __name__ == "__main__":
    unittest.main()
