"""Platform-neutral identities, conversations, messages, and adapter contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ConversationKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"
    THREAD = "thread"


class Capability(StrEnum):
    MESSAGE_SEND = "message.send"
    MESSAGE_REPLY = "message.reply"
    MESSAGE_MENTION = "message.mention"
    MESSAGE_IMAGE = "message.image"
    MESSAGE_ATTACHMENT = "message.attachment"
    MESSAGE_RECALL = "message.recall"
    MEMBER_LIST = "member.list"
    MEMBER_MODERATE = "member.moderate"
    CONVERSATION_RENAME = "conversation.rename"


@dataclass(frozen=True, slots=True)
class IdentityRef:
    identity_id: int
    user_id: int
    platform: str
    identity_namespace: str
    external_user_id: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ConversationRef:
    conversation_id: int
    account_id: int
    kind: ConversationKind
    external_conversation_id: str
    parent_conversation_id: int | None = None
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class MessageEvent:
    adapter_binding_id: int
    account_id: int
    conversation: ConversationRef
    sender: IdentityRef
    self_identity: IdentityRef
    external_message_id: str
    segments: list[dict[str, Any]]
    plain_text: str
    occurred_at: datetime
    source: str
    raw_payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendMessageRequest:
    conversation_id: int
    text: str = ""
    reply_to_message_id: int | None = None
    mention_user_ids: tuple[int, ...] = ()
    attachment_ids: tuple[int, ...] = ()
    inline_media: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterSendRequest:
    conversation: ConversationRef
    segments: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class AdapterSendResult:
    external_message_id: str
    occurred_at: datetime
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int
    conversation_id: int
    external_message_id: str


@runtime_checkable
class MessagingAdapter(Protocol):
    binding_id: int
    account_id: int
    self_identity: IdentityRef
    capabilities: frozenset[Capability]

    async def send_message(self, request: AdapterSendRequest) -> AdapterSendResult: ...

    async def recall_message(self, conversation: ConversationRef, external_message_id: str) -> None: ...


@runtime_checkable
class PlatformAdapter(MessagingAdapter, Protocol):
    """Full inbound/outbound adapter boundary consumed by the core pipeline."""

    async def normalize_event(self, event: dict[str, Any]) -> MessageEvent | None: ...

    async def enrich_message_content(
        self,
        message: MessageEvent,
        *,
        store: Any,
        http_session: Any,
        vision_provider: Any,
        on_first_token: Callable[[], Awaitable[None]] | None = None,
    ) -> str: ...


class AdapterUnavailableError(RuntimeError):
    """No live adapter can serve the requested account/capability."""


class CapabilityUnavailableError(RuntimeError):
    """The active adapter does not expose a requested capability."""


# TODO(milky): implement a Milky PlatformAdapter after the Milky protocol integration is scheduled.
