import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer

from sophos.api.routes import logs


def _entry(level: str, message: str, *continuation: str) -> str:
    lines = [f"2026-08-03 12:00:00,000 | {level:<8} | test.logger | {message}"]
    lines.extend(continuation)
    return "\n".join(lines) + "\n"


async def _next_sse_event(response: ClientResponse) -> tuple[str, str, object]:
    event = "message"
    event_id = ""
    data = ""
    while True:
        raw = await asyncio.wait_for(response.content.readline(), timeout=3)
        if not raw:
            raise AssertionError("SSE stream ended before an event was received")
        line = raw.decode().rstrip("\r\n")
        if not line:
            return event, event_id, json.loads(data)
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("id: "):
            event_id = line.removeprefix("id: ")
        elif line.startswith("data: "):
            data += line.removeprefix("data: ")


class LogTailTests(unittest.TestCase):
    def test_tail_filters_entries_and_preserves_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sophos.log"
            path.write_text(
                _entry("INFO", "first")
                + _entry("TRACE", "payload", "{", '  "secret": true', "}")
                + _entry("WARNING", "second", "traceback line")
                + _entry("INFO", "third"),
                encoding="utf-8",
            )

            lines, count, cursor = logs._tail_snapshot(path, 2, 20)

            self.assertEqual(count, 2)
            self.assertEqual(
                lines,
                [
                    "2026-08-03 12:00:00,000 | WARNING  | test.logger | second",
                    "traceback line",
                    "2026-08-03 12:00:00,000 | INFO     | test.logger | third",
                ],
            )
            self.assertEqual(logs._decode_cursor(cursor).offset, path.stat().st_size)

    def test_tail_handles_entries_larger_than_reverse_read_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sophos.log"
            continuation = "x" * (logs._REVERSE_READ_SIZE + 100)
            path.write_text(
                _entry("INFO", "first") + _entry("TRACE", "large", continuation),
                encoding="utf-8",
            )

            lines, count, _ = logs._tail_snapshot(path, 1, 5)

            self.assertEqual(count, 1)
            self.assertEqual(lines[-1], continuation)
            self.assertIn("TRACE", lines[0])


class LogStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temp_dir.name)
        self.log_path = self.log_dir / "sophos.log"
        self.log_path.write_text(_entry("INFO", "snapshot"), encoding="utf-8")
        self.log_dir_patch = patch.object(logs, "LOG_DIR", self.log_dir)
        self.log_dir_patch.start()

        app = web.Application()
        app.router.add_routes(logs.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.log_dir_patch.stop()
        self.temp_dir.cleanup()

    async def test_snapshot_cursor_bridges_gap_and_rotation(self) -> None:
        history_response = await self.client.get("/api/logs/sophos.log?tail=10&level=INFO")
        self.assertEqual(history_response.status, 200)
        history = await history_response.json()
        self.assertEqual(len(history["lines"]), 1)

        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(_entry("INFO", "between snapshot and stream"))
        self.log_path.rename(self.log_dir / "sophos.log.1")
        self.log_path.write_text(_entry("INFO", "after rotation"), encoding="utf-8")

        stream_response = await self.client.get(
            "/api/logs/stream",
            params={"level": "INFO", "cursor": history["cursor"]},
        )
        try:
            first_event = await _next_sse_event(stream_response)
            second_event = await _next_sse_event(stream_response)
        finally:
            stream_response.close()

        self.assertEqual(first_event[0], "message")
        self.assertIn("between snapshot and stream", first_event[2]["lines"][0])
        self.assertEqual(second_event[0], "message")
        self.assertIn("after rotation", second_event[2]["lines"][0])
        self.assertNotEqual(first_event[1], second_event[1])

    async def test_stream_batches_lines_and_filters_complete_trace_entry(self) -> None:
        history_response = await self.client.get("/api/logs/sophos.log?tail=10&level=INFO")
        cursor = (await history_response.json())["cursor"]
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(_entry("TRACE", "payload", "{", '  "secret": true', "}"))
            file.write(_entry("WARNING", "visible", "traceback line"))

        stream_response = await self.client.get(
            "/api/logs/stream",
            params={"level": "INFO", "cursor": cursor},
        )
        try:
            event, _, data = await _next_sse_event(stream_response)
        finally:
            stream_response.close()

        self.assertEqual(event, "message")
        self.assertEqual(len(data["lines"]), 2)
        self.assertIn("WARNING", data["lines"][0])
        self.assertEqual(data["lines"][1], "traceback line")

    async def test_tail_rejects_unbounded_requests(self) -> None:
        response = await self.client.get(f"/api/logs/sophos.log?tail={logs._MAX_TAIL_ENTRIES + 1}")
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
