"""In-memory record of recent workflow runs, streamed to the WebUI."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_RUNS = 50


@dataclass(slots=True)
class RunRecord:
    run_id: str
    workflow_id: str
    revision: str
    conversation_id: int | None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"
    current_node: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "conversation_id": self.conversation_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "current_node": self.current_node,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "events": self.events}


class RunTracker:
    """Keeps the latest runs and fans out step events to subscribers."""

    def __init__(self) -> None:
        self._runs: deque[RunRecord] = deque(maxlen=MAX_RUNS)
        self._by_id: dict[str, RunRecord] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def runs(self) -> list[RunRecord]:
        return list(reversed(self._runs))

    def get(self, run_id: str) -> RunRecord | None:
        return self._by_id.get(run_id)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def start(self, run_id: str, workflow_id: str, revision: str, conversation_id: int | None) -> None:
        if len(self._runs) == self._runs.maxlen:
            self._by_id.pop(self._runs[0].run_id, None)
        record = RunRecord(run_id, workflow_id, revision, conversation_id)
        self._runs.append(record)
        self._by_id[run_id] = record
        self._publish({"type": "run_started", "run": record.summary()})

    def event(self, run_id: str, kind: str, node_id: str | None, data: dict[str, Any]) -> None:
        record = self._by_id.get(run_id)
        if record is None:
            return
        if kind == "node_entered":
            record.current_node = node_id
        entry = {"kind": kind, "node_id": node_id, "at": time.time(), **data}
        record.events.append(entry)
        self._publish({"type": "run_event", "run_id": run_id, "event": entry})

    def finish(self, run_id: str, status: str, error: str | None = None) -> None:
        record = self._by_id.get(run_id)
        if record is None:
            return
        record.status = status
        record.error = error
        record.finished_at = time.time()
        self._publish({"type": "run_finished", "run": record.summary()})

    def stage(self, stage: str, conversation_id: int | None, data: dict[str, Any]) -> None:
        """Pre-graph stage activity (vision, trigger) for the editor's stage cards."""
        self._publish(
            {"type": "stage_event", "stage": stage, "conversation_id": conversation_id, "at": time.time(), **data}
        )

    def _publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                continue
            queue.put_nowait(message)


_tracker: RunTracker | None = None


def get_run_tracker() -> RunTracker:
    global _tracker  # noqa: PLW0603
    if _tracker is None:
        _tracker = RunTracker()
    return _tracker
