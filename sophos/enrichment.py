"""Per-message enrichment task registry.

Tracks async enrichment tasks (e.g. image VLM processing) by message_id,
allowing cross-pipeline-run coordination: a tool-loop context refresher can
await pending enrichment for messages it didn't originate.

A message becomes *ready* when its task finishes or when the task marks it
ready early (a slow image keeps being recognized in the background).
"""

import asyncio
import logging
from contextlib import suppress
from typing import Any

from sophos import runtime_config

logger = logging.getLogger(__name__)


class EnrichmentRegistry:
    """Singleton registry mapping message_id → pending asyncio.Tasks."""

    def __init__(self) -> None:
        self._tasks: dict[int, list[tuple[asyncio.Task[Any], asyncio.Event]]] = {}

    def register(self, message_id: int, task: asyncio.Task[Any]) -> None:
        """Register an enrichment task for a message. Auto-cleans on completion."""
        ready = asyncio.Event()
        entry = (task, ready)
        self._tasks.setdefault(message_id, []).append(entry)

        def _done(t: asyncio.Task[Any]) -> None:
            ready.set()
            self._remove(message_id, entry)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("Enrichment task failed: %s", t.exception())

        task.add_done_callback(_done)

    def mark_ready(self, message_id: int) -> None:
        """Let waiters proceed while the message's tasks keep running."""
        for _task, ready in self._tasks.get(message_id, []):
            ready.set()

    async def wait_for(
        self,
        message_ids: list[int],
        timeout: float | None = None,
    ) -> bool:
        """Wait until every given message is ready.

        Returns True if anything was actually awaited.
        """
        pending = [ready for mid in message_ids for _task, ready in self._tasks.get(mid, []) if not ready.is_set()]
        if not pending:
            return False
        logger.debug("Waiting for %d enrichment task(s) on %d message(s)", len(pending), len(message_ids))
        wait_timeout = timeout
        if wait_timeout is None:
            wait_timeout = runtime_config.get("vision_generation_timeout") + 20.0
        waiters = [asyncio.ensure_future(ready.wait()) for ready in pending]
        _done, still_pending = await asyncio.wait(waiters, timeout=wait_timeout)
        for waiter in still_pending:
            waiter.cancel()
        if still_pending:
            logger.warning(
                "Timed out after %.1fs waiting for %d enrichment task(s)",
                wait_timeout, len(still_pending),
            )
        return True

    def _remove(self, message_id: int, entry: tuple[asyncio.Task[Any], asyncio.Event]) -> None:
        entries = self._tasks.get(message_id)
        if entries is None:
            return
        with suppress(ValueError):
            entries.remove(entry)
        if not entries:
            self._tasks.pop(message_id, None)


_registry: EnrichmentRegistry | None = None


def get_enrichment_registry() -> EnrichmentRegistry:
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = EnrichmentRegistry()
    return _registry
