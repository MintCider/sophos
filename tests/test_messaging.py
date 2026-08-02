import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sophos.adapter_router import AdapterRouter
from sophos.message_store import MessageStore
from sophos.messaging import MessageService
from sophos.onebot_adapter import OneBot11Adapter
from sophos.platform import (
    AdapterSendResult,
    Capability,
    ConversationKind,
    ConversationRef,
    IdentityRef,
    MessageEvent,
    SendMessageRequest,
)
from sophos.tools.messaging import SendMessageTool
from sophos.tools.registry import ToolRegistry


def _identity(identity_id: int, user_id: int, external_id: str) -> IdentityRef:
    return IdentityRef(
        identity_id=identity_id,
        user_id=user_id,
        platform="qq",
        identity_namespace="global",
        external_user_id=external_id,
        display_name=f"user-{user_id}",
    )


def _conversation(conversation_id: int = 20, external_id: str = "300") -> ConversationRef:
    return ConversationRef(
        conversation_id=conversation_id,
        account_id=10,
        kind=ConversationKind.GROUP,
        external_conversation_id=external_id,
    )


class MessageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_upsert_uses_complete_external_locator_and_returns_internal_id(self) -> None:
        pool = AsyncMock()
        pool.fetchval.return_value = 777
        store = MessageStore(pool)
        event = MessageEvent(
            adapter_binding_id=11,
            account_id=10,
            conversation=_conversation(),
            sender=_identity(31, 41, "100"),
            self_identity=_identity(32, 42, "200"),
            external_message_id="12345",
            segments=[{"type": "text", "data": {"text": "hello"}}],
            plain_text="hello",
            occurred_at=datetime.now(tz=UTC),
            source="user",
            raw_payload={"message_id": 12345},
        )

        result = await store.save_event_message(event)

        self.assertEqual(result, 777)
        query = pool.fetchval.await_args.args[0]
        self.assertIn("adapter_binding_id, conversation_id, external_message_id", query)
        self.assertIn("DO UPDATE SET", query)
        self.assertEqual(pool.fetchval.await_args.args[1:5], (11, 20, 31, "12345"))

    async def test_reply_lookup_is_scoped_to_binding_and_conversation(self) -> None:
        pool = AsyncMock()
        pool.fetchval.side_effect = [555, 777]
        store = MessageStore(pool)
        event = MessageEvent(
            adapter_binding_id=11,
            account_id=10,
            conversation=_conversation(),
            sender=_identity(31, 41, "100"),
            self_identity=_identity(32, 42, "200"),
            external_message_id="124",
            segments=[{"type": "reply", "data": {"external_message_id": "123"}}],
            plain_text="",
            occurred_at=datetime.now(tz=UTC),
            source="user",
            raw_payload={},
        )

        await store.save_event_message(event)

        lookup = pool.fetchval.await_args_list[0]
        self.assertIn("adapter_binding_id = $1 AND conversation_id = $2", lookup.args[0])
        self.assertEqual(lookup.args[1:], (11, 20, "123"))
        self.assertEqual(pool.fetchval.await_args_list[1].args[5], 555)

    async def test_mutation_uses_internal_message_id(self) -> None:
        pool = AsyncMock()
        store = MessageStore(pool)

        await store.update_plain_text(88, "expanded")

        self.assertIn("WHERE id = $1", pool.execute.await_args.args[0])
        self.assertNotIn("external_message_id", pool.execute.await_args.args[0])
        self.assertEqual(pool.execute.await_args.args[1:], (88, "expanded"))


class _FakeAdapter:
    capabilities = frozenset(
        {Capability.MESSAGE_SEND, Capability.MESSAGE_REPLY, Capability.MESSAGE_RECALL}
    )

    def __init__(self, marker: str = "a") -> None:
        self.binding_id = 11
        self.account_id = 10
        self.self_identity = _identity(32, 42, "200")
        self.marker = marker
        self.send_message = AsyncMock(
            return_value=AdapterSendResult(
                external_message_id="900",
                occurred_at=datetime.now(tz=UTC),
                raw_payload={"marker": marker},
            )
        )
        self.recall_message = AsyncMock()


class MessageServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_replaces_stale_adapter_instance(self) -> None:
        router = AdapterRouter()
        old = _FakeAdapter("old")
        new = _FakeAdapter("new")

        router.register(old)
        router.register(new)

        self.assertIs(router.for_account(10, Capability.MESSAGE_SEND), new)
        router.unregister(old)
        self.assertIs(router.for_binding(11, Capability.MESSAGE_SEND), new)

    async def test_send_persists_delivery_and_returns_internal_id(self) -> None:
        store = AsyncMock()
        store.save_outbound_message.return_value = 321
        platform_store = AsyncMock()
        platform_store.get_conversation.return_value = _conversation()
        router = AdapterRouter()
        adapter = _FakeAdapter()
        router.register(adapter)
        service = MessageService(store, platform_store, router)

        sent = await service.send_message(SendMessageRequest(conversation_id=20, text="hello"))

        self.assertEqual(sent.message_id, 321)
        self.assertEqual(sent.external_message_id, "900")
        adapter.send_message.assert_awaited_once()
        store.save_outbound_message.assert_awaited_once()
        saved = store.save_outbound_message.await_args.kwargs
        self.assertEqual(saved["conversation_id"], 20)
        self.assertEqual(saved["external_message_id"], "900")
        self.assertEqual(saved["sender_identity_id"], 32)

    async def test_reply_must_belong_to_current_delivery_scope(self) -> None:
        store = AsyncMock()
        store.get_delivery_locator.return_value = (99, 20, "10")
        platform_store = AsyncMock()
        platform_store.get_conversation.return_value = _conversation()
        router = AdapterRouter()
        adapter = _FakeAdapter()
        router.register(adapter)
        service = MessageService(store, platform_store, router)

        with self.assertRaisesRegex(ValueError, "another adapter binding"):
            await service.send_message(
                SendMessageRequest(conversation_id=20, text="reply", reply_to_message_id=1)
            )

        adapter.send_message.assert_not_awaited()


class MessagingToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_tool_uses_internal_ids_and_requires_background_cross_conversation(self) -> None:
        service = AsyncMock()
        tool = SendMessageTool()
        context = {"message_service": service, "conversation_id": 20}

        result = await tool.execute({"text": "hello", "conversation_id": 21}, context)

        self.assertIn("error", result)
        service.send_message.assert_not_awaited()

    def test_tool_schema_is_platform_neutral_and_capability_filtered(self) -> None:
        registry = ToolRegistry()
        registry.register(SendMessageTool())

        hidden = registry.get_function_schemas(capabilities=set())
        visible = registry.get_function_schemas(capabilities={Capability.MESSAGE_SEND.value})

        self.assertEqual(hidden, [])
        self.assertEqual([schema["function"]["name"] for schema in visible], ["send_message"])
        parameters = visible[0]["function"]["parameters"]["properties"]
        self.assertIn("conversation_id", parameters)
        self.assertIn("reply_to_message_id", parameters)
        self.assertIn("mention_user_ids", parameters)
        self.assertNotIn("group_id", parameters)
        self.assertNotIn("target_id", parameters)


class OneBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_onebot_segments_to_internal_content_model(self) -> None:
        platform_store = AsyncMock()
        self_identity = _identity(32, 42, "200")
        sender = _identity(31, 41, "100")
        platform_store.resolve_identity.side_effect = [sender, self_identity]
        platform_store.resolve_conversation.return_value = _conversation()
        adapter = OneBot11Adapter(
            api=AsyncMock(),
            platform_store=platform_store,
            binding_id=11,
            account_id=10,
            self_identity=self_identity,
        )

        message = await adapter.normalize_event(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 9,
                "group_id": 300,
                "user_id": 100,
                "sender": {"nickname": "sender"},
                "message": [
                    {"type": "at", "data": {"qq": "200"}},
                    {"type": "text", "data": {"text": " hello"}},
                    {"type": "reply", "data": {"id": "8"}},
                    {"type": "face", "data": {"id": "14"}},
                ],
            }
        )

        assert message is not None
        self.assertEqual(
            message.segments,
            [
                {"type": "mention", "data": {"user_id": 42, "display_name": "user-42"}},
                {"type": "text", "data": {"text": " hello"}},
                {"type": "reply", "data": {"external_message_id": "8"}},
                {"type": "emoji", "data": {"platform": "qq", "external_id": "14"}},
            ],
        )
        self.assertTrue(message.metadata["mentioned_self"])
        self.assertTrue(message.metadata["leading_self_mention"])

    async def test_private_conversation_uses_peer_not_sender_field_for_self_echo(self) -> None:
        platform_store = AsyncMock()
        self_identity = _identity(32, 42, "200")
        sender = _identity(31, 41, "100")
        platform_store.resolve_identity.return_value = sender
        platform_store.resolve_conversation.return_value = ConversationRef(
            conversation_id=21,
            account_id=10,
            kind=ConversationKind.DIRECT,
            external_conversation_id="100",
        )
        adapter = OneBot11Adapter(
            api=AsyncMock(),
            platform_store=platform_store,
            binding_id=11,
            account_id=10,
            self_identity=self_identity,
        )

        message = await adapter.normalize_event(
            {
                "post_type": "message",
                "message_type": "private",
                "message_id": 5,
                "user_id": 100,
                "sender": {"user_id": 100, "nickname": "peer"},
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.conversation.external_conversation_id, "100")  # type: ignore[union-attr]
        platform_store.resolve_conversation.assert_awaited_once_with(
            account_id=10,
            kind=ConversationKind.DIRECT,
            external_conversation_id="100",
            display_name="",
        )

        unresolved = await adapter.normalize_event(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "message_id": 6,
                "user_id": 200,
                "sender": {"user_id": 200},
                "message": [{"type": "text", "data": {"text": "out"}}],
            }
        )
        self.assertIsNone(unresolved)


if __name__ == "__main__":
    unittest.main()
