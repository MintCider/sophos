"""Unified message delivery service used by agents, commands, and fallbacks."""

from __future__ import annotations

from sophos.adapter_router import AdapterRouter
from sophos.message_store import MessageStore
from sophos.platform import (
    AdapterSendRequest,
    Capability,
    CapabilityUnavailableError,
    SendMessageRequest,
    SentMessage,
)
from sophos.platform_store import PlatformStore


class MessageService:
    def __init__(self, store: MessageStore, platform_store: PlatformStore, router: AdapterRouter):
        self.store = store
        self.platform_store = platform_store
        self.router = router

    async def send_message(self, request: SendMessageRequest) -> SentMessage:
        conversation = await self.platform_store.get_conversation(request.conversation_id)
        if conversation is None:
            raise ValueError(f"unknown conversation_id: {request.conversation_id}")
        adapter = self.router.for_account(conversation.account_id, Capability.MESSAGE_SEND)

        segments: list[dict] = []
        if request.reply_to_message_id is not None:
            self._require_optional_capability(adapter, Capability.MESSAGE_REPLY)
            locator = await self.store.get_delivery_locator(request.reply_to_message_id)
            if locator is None:
                raise ValueError(f"unknown or undelivered reply_to_message_id: {request.reply_to_message_id}")
            binding_id, reply_conversation_id, external_message_id = locator
            if binding_id != adapter.binding_id or reply_conversation_id != conversation.conversation_id:
                raise ValueError("reply target belongs to another adapter binding or conversation")
            segments.append({"type": "reply", "data": {"external_message_id": external_message_id}})
        for user_id in request.mention_user_ids:
            self._require_optional_capability(adapter, Capability.MESSAGE_MENTION)
            segments.append({"type": "mention", "data": {"user_id": user_id}})
        if request.text:
            segments.append({"type": "text", "data": {"text": request.text}})
        if request.attachment_ids:
            self._require_optional_capability(adapter, Capability.MESSAGE_ATTACHMENT)
            raise ValueError("attachment materialization is not implemented")
        if request.inline_media:
            self._require_optional_capability(adapter, Capability.MESSAGE_IMAGE)
        segments.extend(request.inline_media)
        if not segments:
            raise ValueError("message content cannot be empty")

        delivered = await adapter.send_message(AdapterSendRequest(conversation=conversation, segments=segments))
        internal_id = await self.store.save_outbound_message(
            adapter_binding_id=adapter.binding_id,
            conversation_id=conversation.conversation_id,
            sender_identity_id=adapter.self_identity.identity_id,
            external_message_id=delivered.external_message_id,
            segments=segments,
            reply_to_message_id=request.reply_to_message_id,
            occurred_at=delivered.occurred_at,
            metadata=request.metadata,
            raw_payload=delivered.raw_payload,
        )
        return SentMessage(
            message_id=internal_id,
            conversation_id=conversation.conversation_id,
            external_message_id=delivered.external_message_id,
        )

    @staticmethod
    def _require_optional_capability(adapter: object, capability: Capability) -> None:
        capabilities = getattr(adapter, "capabilities", frozenset())
        if capability not in capabilities:
            binding_id = getattr(adapter, "binding_id", "unknown")
            raise CapabilityUnavailableError(
                f"adapter binding {binding_id} does not support {capability.value}"
            )

    async def recall_message(self, message_id: int) -> None:
        locator = await self.store.get_delivery_locator(message_id)
        if locator is None:
            raise ValueError(f"unknown or undelivered message_id: {message_id}")
        binding_id, conversation_id, external_message_id = locator
        conversation = await self.platform_store.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"unknown conversation_id: {conversation_id}")
        adapter = self.router.for_binding(binding_id, Capability.MESSAGE_RECALL)
        await adapter.recall_message(conversation, external_message_id)
