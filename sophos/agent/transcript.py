"""Append-only, provider-neutral transcript event log.

Canonical payloads are the portable source of truth. ``NativeEnvelope`` keeps
provider/interface-specific data for exact replay when the target supports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

EventKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "transition_requested",
    "transition_applied",
    "provider_response",
]


@dataclass(frozen=True, slots=True)
class NativeEnvelope:
    """Opaque provider data retained alongside a canonical event."""

    provider: str
    api_surface: str
    payload: dict[str, Any]
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "api_surface": self.api_surface,
            "model": self.model,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NativeEnvelope:
        return cls(
            provider=str(value["provider"]),
            api_surface=str(value["api_surface"]),
            model=str(value["model"]) if value.get("model") is not None else None,
            payload=dict(value.get("payload") or {}),
        )


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One immutable event in a transcript."""

    event_id: str
    kind: EventKind
    payload: dict[str, Any]
    sequence: int | None = None
    node_id: str | None = None
    native_envelopes: tuple[NativeEnvelope, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "node_id": self.node_id,
            "payload": self.payload,
            "native_envelopes": [envelope.to_dict() for envelope in self.native_envelopes],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TranscriptEvent:
        sequence = value.get("sequence")
        return cls(
            event_id=str(value["event_id"]),
            sequence=int(sequence) if sequence is not None else None,
            kind=value["kind"],
            node_id=str(value["node_id"]) if value.get("node_id") is not None else None,
            payload=dict(value.get("payload") or {}),
            native_envelopes=tuple(
                NativeEnvelope.from_dict(item) for item in value.get("native_envelopes") or []
            ),
        )


def new_event(
    kind: EventKind,
    payload: dict[str, Any],
    *,
    node_id: str | None = None,
    native_envelopes: tuple[NativeEnvelope, ...] = (),
    event_id: str | None = None,
) -> TranscriptEvent:
    """Create an unsequenced event ready to append to a transcript."""

    return TranscriptEvent(
        event_id=event_id or uuid4().hex,
        kind=kind,
        payload=payload,
        node_id=node_id,
        native_envelopes=native_envelopes,
    )


class Transcript:
    """An event log that only permits appending new immutable events."""

    def __init__(self, events: list[TranscriptEvent] | tuple[TranscriptEvent, ...] = ()) -> None:
        self._events: list[TranscriptEvent] = []
        self._event_ids: set[str] = set()
        for event in events:
            self.append(event)

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._events)

    def append(self, event: TranscriptEvent) -> TranscriptEvent:
        if event.event_id in self._event_ids:
            raise ValueError(f"duplicate transcript event_id: {event.event_id}")
        expected_sequence = len(self._events)
        if event.sequence is not None and event.sequence != expected_sequence:
            raise ValueError(
                f"non-contiguous transcript sequence: expected {expected_sequence}, got {event.sequence}"
            )
        sequenced = event if event.sequence is not None else replace(event, sequence=expected_sequence)
        self._events.append(sequenced)
        self._event_ids.add(sequenced.event_id)
        return sequenced

    def extend(self, events: list[TranscriptEvent] | tuple[TranscriptEvent, ...]) -> None:
        for event in events:
            self.append(event)

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]

    @classmethod
    def from_list(cls, values: list[dict[str, Any]]) -> Transcript:
        return cls([TranscriptEvent.from_dict(value) for value in values])

