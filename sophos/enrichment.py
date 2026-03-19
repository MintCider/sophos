"""Per-message enrichment task registry.

Tracks async enrichment tasks (e.g. image VLM processing) by message_id,
allowing cross-pipeline-run coordination: a tool-loop context refresher can
await pending enrichment for messages it didn't originate.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class EnrichmentRegistry:
    """Singleton registry mapping message_id → pending asyncio.Tasks."""

    def __init__(self) -> None:
        self._tasks: dict[int, list[asyncio.Task[Any]]] = {}

    def register(self, message_id: int, task: asyncio.Task[Any]) -> None:
        """Register an enrichment task for a message. Auto-cleans on completion."""
        self._tasks.setdefault(message_id, []).append(task)
        task.add_done_callback(lambda _t: self._remove(message_id, _t))

    async def wait_for(
        self,
        message_ids: list[int],
        timeout: float = 15.0,
    ) -> bool:
        """Await all pending enrichment tasks for the given message_ids.

        Returns True if any tasks were actually awaited.
        """
        pending = [
            t
            for mid in message_ids
            for t in self._tasks.get(mid, [])
            if not t.done()
        ]
        if not pending:
            return False
        logger.debug("Waiting for %d enrichment task(s) on %d message(s)", len(pending), len(message_ids))
        done, _ = await asyncio.wait(pending, timeout=timeout)
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc:
                logger.warning("Enrichment task failed: %s", exc)
        return True

    def _remove(self, message_id: int, task: asyncio.Task[Any]) -> None:
        tasks = self._tasks.get(message_id)
        if tasks is None:
            return
        try:
            tasks.remove(task)
        except ValueError:
            pass
        if not tasks:
            self._tasks.pop(message_id, None)


_registry: EnrichmentRegistry | None = None


def get_enrichment_registry() -> EnrichmentRegistry:
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = EnrichmentRegistry()
    return _registry
