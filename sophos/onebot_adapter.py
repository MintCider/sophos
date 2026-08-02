"""OneBot 11 adapter for the platform-neutral messaging boundary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sophos.onebot_api import OneBotAPI
from sophos.platform import (
    AdapterSendRequest,
    AdapterSendResult,
    Capability,
    ConversationKind,
    ConversationRef,
    IdentityRef,
    MessageEvent,
)
from sophos.platform_store import PlatformStore

logger = logging.getLogger(__name__)


class OneBot11Adapter:
    capabilities = frozenset(
        {
            Capability.MESSAGE_SEND,
            Capability.MESSAGE_RECALL,
            Capability.MEMBER_LIST,
            Capability.MEMBER_MODERATE,
            Capability.CONVERSATION_RENAME,
        }
    )

    def __init__(
        self,
        *,
        api: OneBotAPI,
        platform_store: PlatformStore,
        binding_id: int,
        account_id: int,
        self_identity: IdentityRef,
    ) -> None:
        self.api = api
        self.platform_store = platform_store
        self.binding_id = binding_id
        self.account_id = account_id
        self.self_identity = self_identity

    @classmethod
    async def create(
        cls,
        *,
        api: OneBotAPI,
        platform_store: PlatformStore,
        self_external_id: str,
        display_name: str = "",
    ) -> OneBot11Adapter:
        self_identity = await platform_store.resolve_identity(
            platform="qq",
            identity_namespace="global",
            external_user_id=self_external_id,
            display_name=display_name,
            metadata={"is_bot_account": True},
        )
        account_id, binding_id = await platform_store.ensure_account_binding(
            platform="qq",
            identity_namespace="global",
            external_account_id=self_external_id,
            adapter_kind="onebot11",
            binding_name="default",
            external_id_namespace="onebot11:default",
            display_name=display_name,
        )
        return cls(
            api=api,
            platform_store=platform_store,
            binding_id=binding_id,
            account_id=account_id,
            self_identity=self_identity,
        )

    async def normalize_event(self, event: dict[str, Any]) -> MessageEvent | None:
        if event.get("post_type") not in {"message", "message_sent"}:
            return None
        if event.get("message_id") is None:
            return None
        message_type = event.get("message_type", "private")
        sender_external_id = str(event.get("user_id") or event.get("sender", {}).get("user_id") or "")
        if not sender_external_id:
            return None

        if message_type == "group":
            external_conversation_id = str(event.get("group_id") or "")
            kind = ConversationKind.GROUP
            conversation_name = str(event.get("group_name") or "")
        else:
            kind = ConversationKind.DIRECT
            if sender_external_id != self.self_identity.external_user_id:
                external_conversation_id = sender_external_id
            else:
                # OneBot implementations differ for self-message echoes. Only accept an
                # explicit peer; Sophos-originated sends are already stored by MessageService.
                peer = event.get("target_id") or event.get("peer_id") or event.get("peer_user_id")
                if not peer:
                    logger.warning("Ignoring OneBot private self echo without an explicit peer")
                    return None
                external_conversation_id = str(peer)
            conversation_name = ""
        if not external_conversation_id:
            return None

        sender_data = event.get("sender") or {}
        display_name = str(sender_data.get("card") or sender_data.get("nickname") or "")
        sender = await self.platform_store.resolve_identity(
            platform="qq",
            identity_namespace="global",
            external_user_id=sender_external_id,
            display_name=display_name,
            metadata={"nickname": sender_data.get("nickname", ""), "card": sender_data.get("card", "")},
        )
        conversation = await self.platform_store.resolve_conversation(
            account_id=self.account_id,
            kind=kind,
            external_conversation_id=external_conversation_id,
            display_name=conversation_name,
        )
        segments = event.get("message") if isinstance(event.get("message"), list) else []
        plain_text = "".join(
            segment.get("data", {}).get("text", "")
            for segment in segments
            if isinstance(segment, dict) and segment.get("type") == "text"
        )
        raw_time = event.get("time")
        occurred_at = datetime.fromtimestamp(raw_time, tz=UTC) if raw_time else datetime.now(tz=UTC)
        return MessageEvent(
            adapter_binding_id=self.binding_id,
            account_id=self.account_id,
            conversation=conversation,
            sender=sender,
            self_identity=self.self_identity,
            external_message_id=str(event["message_id"]),
            segments=segments,
            plain_text=plain_text.strip(),
            occurred_at=occurred_at,
            source="co_account" if sender.identity_id == self.self_identity.identity_id else "user",
            raw_payload=event,
            metadata={"onebot_post_type": event.get("post_type", "message")},
        )

    async def send_message(self, request: AdapterSendRequest) -> AdapterSendResult:
        segments = await self._to_onebot_segments(request.segments)
        params: dict[str, Any] = {"message": segments}
        conversation = request.conversation
        if conversation.kind == ConversationKind.GROUP:
            params.update(message_type="group", group_id=self._coerce_id(conversation.external_conversation_id))
        elif conversation.kind == ConversationKind.DIRECT:
            params.update(message_type="private", user_id=self._coerce_id(conversation.external_conversation_id))
        else:
            raise ValueError(f"OneBot 11 cannot send to {conversation.kind.value} conversations")
        result = await self.api.call("send_msg", params)
        external_message_id = result.get("message_id")
        if external_message_id is None:
            raise RuntimeError("OneBot send_msg returned no message_id")
        return AdapterSendResult(
            external_message_id=str(external_message_id),
            occurred_at=datetime.now(tz=UTC),
            raw_payload=result,
        )

    async def recall_message(self, conversation: ConversationRef, external_message_id: str) -> None:
        del conversation
        await self.api.call("delete_msg", {"message_id": self._coerce_id(external_message_id)})

    async def list_conversation_members(self, conversation: ConversationRef) -> list[dict[str, Any]]:
        if conversation.kind != ConversationKind.GROUP:
            raise ValueError("OneBot member listing requires a group conversation")
        rows = await self.api.call(
            "get_group_member_list",
            {"group_id": self._coerce_id(conversation.external_conversation_id)},
        )
        members: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else rows.get("members", []):
            external_user_id = str(row.get("user_id") or "")
            if not external_user_id:
                continue
            display_name = str(row.get("card") or row.get("nickname") or "")
            identity = await self.platform_store.resolve_identity(
                platform="qq",
                identity_namespace="global",
                external_user_id=external_user_id,
                display_name=display_name,
                metadata={"nickname": row.get("nickname", ""), "card": row.get("card", "")},
            )
            members.append(
                {
                    "user_id": identity.user_id,
                    "display_name": identity.display_name,
                    "role": row.get("role", "member"),
                }
            )
        return members

    async def moderate_member(
        self,
        conversation: ConversationRef,
        *,
        user_id: int,
        action: str,
        duration_seconds: int | None = None,
        reason: str | None = None,
    ) -> None:
        del reason  # OneBot 11 has no portable moderation reason field.
        if conversation.kind != ConversationKind.GROUP:
            raise ValueError("OneBot moderation requires a group conversation")
        identity = await self.platform_store.get_identity_for_account(user_id=user_id, account_id=self.account_id)
        if identity is None:
            raise ValueError(f"user {user_id} has no QQ identity")
        base = {
            "group_id": self._coerce_id(conversation.external_conversation_id),
            "user_id": self._coerce_id(identity.external_user_id),
        }
        if action == "kick":
            await self.api.call("set_group_kick", base)
        elif action in {"ban", "unban"}:
            await self.api.call(
                "set_group_ban",
                {**base, "duration": 0 if action == "unban" else int(duration_seconds or 1800)},
            )
        else:
            raise ValueError(f"unsupported moderation action: {action}")

    async def _to_onebot_segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for segment in segments:
            segment_type = segment.get("type")
            data = segment.get("data", {})
            if segment_type == "text":
                result.append({"type": "text", "data": {"text": str(data.get("text", ""))}})
            elif segment_type == "reply":
                result.append(
                    {"type": "reply", "data": {"id": self._coerce_id(str(data["external_message_id"]))}}
                )
            elif segment_type == "mention":
                identity = await self.platform_store.get_identity_for_account(
                    user_id=int(data["user_id"]),
                    account_id=self.account_id,
                )
                if identity is None:
                    raise ValueError(f"user {data['user_id']} has no identity on this account's platform")
                result.append({"type": "at", "data": {"qq": self._coerce_id(identity.external_user_id)}})
            elif segment_type == "image":
                if data.get("base64"):
                    result.append({"type": "image", "data": {"file": f"base64://{data['base64']}"}})
                elif data.get("url"):
                    result.append({"type": "image", "data": {"url": str(data["url"])}})
                else:
                    raise ValueError("canonical image segment requires base64 or url")
            else:
                raise ValueError(f"unsupported canonical segment type for OneBot: {segment_type}")
        return result

    @staticmethod
    def _coerce_id(value: str) -> int | str:
        return int(value) if value.isdecimal() else value
