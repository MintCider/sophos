"""日志查看 API。"""

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

LOG_DIR = Path("logs")
# 安全校验：只允许 sophos.log 和 sophos.log.N
_SAFE_FILENAME = re.compile(r"^sophos\.log(\.\d+)?$")

# 日志等级优先级（用于服务端过滤）
_LEVEL_PRIORITY = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# 解析日志行的等级
_LOG_LINE_RE = re.compile(r"^\S+ \S+ \| (\w+)\s+\|")
_LOG_LINE_BYTES_RE = re.compile(rb"^\S+ \S+ \| (\w+)\s+\|")

_MAX_TAIL_ENTRIES = 5000
_MAX_PAGE_LINES = 5000
_REVERSE_READ_SIZE = 64 * 1024
_STREAM_BATCH_BYTES = 512 * 1024
_STREAM_BATCH_LINES = 1000


@dataclass(frozen=True)
class _LogCursor:
    device: int
    inode: int
    offset: int

    def encode(self) -> str:
        return f"{self.device:x}:{self.inode:x}:{self.offset:x}"


def _parse_level(line: str) -> str | None:
    """从日志行中提取等级。"""
    match = _LOG_LINE_RE.match(line)
    return match.group(1) if match else None


def _parse_level_bytes(line: bytes) -> str | None:
    match = _LOG_LINE_BYTES_RE.match(line)
    return match.group(1).decode("ascii", errors="replace") if match else None


def _minimum_priority(value: str | None) -> int:
    return _LEVEL_PRIORITY.get((value or "INFO").upper(), 20)


def _parse_bounded_int(value: str | None, *, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"Invalid {name}") from exc
    if parsed < minimum or parsed > maximum:
        raise web.HTTPBadRequest(reason=f"{name} must be between {minimum} and {maximum}")
    return parsed


def _cursor_for(file: BinaryIO, offset: int | None = None) -> _LogCursor:
    stat = os.fstat(file.fileno())
    return _LogCursor(stat.st_dev, stat.st_ino, file.tell() if offset is None else offset)


def _decode_cursor(value: str | None) -> _LogCursor | None:
    if not value:
        return None
    try:
        device, inode, offset = (int(part, 16) for part in value.split(":"))
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason="Invalid log cursor") from exc
    if device < 0 or inode < 0 or offset < 0:
        raise web.HTTPBadRequest(reason="Invalid log cursor")
    return _LogCursor(device, inode, offset)


def _candidate_paths() -> list[Path]:
    if not LOG_DIR.exists():
        return []
    return [
        path
        for path in LOG_DIR.iterdir()
        if path.is_file() and _SAFE_FILENAME.fullmatch(path.name)
    ]


def _path_generation(path: Path) -> int:
    if path.name == "sophos.log":
        return 0
    return int(path.name.rsplit(".", 1)[1])


def _open_identity(cursor: _LogCursor) -> BinaryIO | None:
    """打开游标所属的文件；文件轮转改名后仍可通过 inode 找到。"""
    for path in _candidate_paths():
        try:
            file = path.open("rb")
        except OSError:
            continue
        stat = os.fstat(file.fileno())
        if stat.st_dev == cursor.device and stat.st_ino == cursor.inode:
            if cursor.offset <= stat.st_size:
                file.seek(cursor.offset)
                return file
            file.close()
            return None
        file.close()
    return None


def _open_current(*, at_end: bool) -> BinaryIO | None:
    try:
        file = (LOG_DIR / "sophos.log").open("rb")
    except OSError:
        return None
    if at_end:
        file.seek(0, os.SEEK_END)
    return file


def _open_successor(file: BinaryIO) -> tuple[BinaryIO | None, bool]:
    """返回轮转序列中的下一份文件；第二个返回值表示原文件已过期。"""
    current_cursor = _cursor_for(file)
    current_path: Path | None = None
    for path in _candidate_paths():
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_dev == current_cursor.device and stat.st_ino == current_cursor.inode:
            current_path = path
            break

    if current_path is None:
        return None, True

    generation = _path_generation(current_path)
    if generation == 0:
        return None, False

    successor_name = "sophos.log" if generation == 1 else f"sophos.log.{generation - 1}"
    try:
        successor = (LOG_DIR / successor_name).open("rb")
    except OSError:
        return None, False
    successor_stat = os.fstat(successor.fileno())
    if successor_stat.st_dev == current_cursor.device and successor_stat.st_ino == current_cursor.inode:
        successor.close()
        return None, False
    return successor, False


def _iter_reverse_lines(file: BinaryIO, end: int) -> Iterator[bytes]:
    """从文件末尾反向产生日志行，不加载无关的文件前部。"""
    position = end
    remainder = b""
    is_last_line = True

    while position > 0:
        size = min(_REVERSE_READ_SIZE, position)
        position -= size
        file.seek(position)
        parts = (file.read(size) + remainder).split(b"\n")
        remainder = parts[0]
        for line in reversed(parts[1:]):
            if is_last_line:
                is_last_line = False
                if line == b"":
                    continue
            yield line.removesuffix(b"\r")

    if remainder:
        yield remainder.removesuffix(b"\r")


def _tail_snapshot(path: Path, count: int, min_priority: int) -> tuple[list[str], int, str]:
    """反向读取最后 count 条符合等级要求的完整日志条目。"""
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        end = file.tell()
        cursor = _cursor_for(file, end).encode()
        entries: list[list[bytes]] = []
        current_reversed: list[bytes] = []

        for raw_line in _iter_reverse_lines(file, end):
            current_reversed.append(raw_line)
            level = _parse_level_bytes(raw_line)
            if level is None:
                continue

            if _LEVEL_PRIORITY.get(level, 20) >= min_priority:
                entries.append(list(reversed(current_reversed)))
                if len(entries) >= count:
                    break
            current_reversed = []
        else:
            # 文件开头若存在没有时间戳前缀的孤立内容，保持旧接口的兼容行为。
            if current_reversed and min_priority <= 20:
                entries.append(list(reversed(current_reversed)))

    selected = list(reversed(entries[:count]))
    lines = [raw.decode("utf-8", errors="replace") for entry in selected for raw in entry]
    return lines, len(selected), cursor


def _read_page(path: Path, offset: int, limit: int) -> tuple[list[str], int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return lines[offset : offset + limit], len(lines)


def _read_stream_batch(file: BinaryIO) -> tuple[list[bytes], int]:
    """读取一批完整物理行；未写完的末行留待下一轮。"""
    lines: list[bytes] = []
    size = 0
    while len(lines) < _STREAM_BATCH_LINES and size < _STREAM_BATCH_BYTES:
        start = file.tell()
        raw = file.readline()
        if not raw:
            break
        if not raw.endswith(b"\n"):
            file.seek(start)
            break
        raw = raw[:-1].removesuffix(b"\r")
        lines.append(raw)
        size += len(raw) + 1
    return lines, file.tell()


async def _write_sse(
    response: web.StreamResponse,
    *,
    event: str | None,
    data: object,
    cursor: str,
) -> None:
    fields = [f"id: {cursor}"]
    if event:
        fields.append(f"event: {event}")
    fields.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    await response.write(("\n".join(fields) + "\n\n").encode())


@routes.get("/api/logs")
async def list_logs(request: web.Request) -> web.Response:
    """列出可用日志文件。"""

    def collect() -> list[dict[str, str | int]]:
        files = []
        for path in sorted(_candidate_paths()):
            try:
                files.append({"name": path.name, "size": path.stat().st_size})
            except OSError:
                continue
        return files

    return web.json_response({"files": await asyncio.to_thread(collect)})


@routes.get("/api/logs/stream")
async def stream_logs(request: web.Request) -> web.StreamResponse:
    """以 SSE 推送新日志，支持等级过滤和历史响应返回的续读游标。"""
    min_priority = _minimum_priority(request.query.get("level"))
    requested_cursor = _decode_cursor(request.query.get("cursor") or request.headers.get("Last-Event-ID"))

    response = web.StreamResponse()
    response.content_type = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    file: BinaryIO | None = None
    cursor_expired = False
    try:
        while file is None:
            if requested_cursor is not None:
                file = await asyncio.to_thread(_open_identity, requested_cursor)
                if file is None:
                    cursor_expired = True
                    file = await asyncio.to_thread(_open_current, at_end=True)
                requested_cursor = None
            else:
                file = await asyncio.to_thread(_open_current, at_end=True)

            if file is None:
                await response.write(b"event: heartbeat\ndata: {}\n\n")
                await asyncio.sleep(1)

        if cursor_expired:
            await _write_sse(
                response,
                event="reset",
                data={"reason": "cursor_expired"},
                cursor=_cursor_for(file).encode(),
            )

        heartbeat_interval = 5.0
        poll_interval = 0.3
        last_write = asyncio.get_running_loop().time()
        skip_entry = False

        while True:
            raw_lines, _ = await asyncio.to_thread(_read_stream_batch, file)
            if raw_lines:
                selected: list[str] = []
                for raw_line in raw_lines:
                    level = _parse_level_bytes(raw_line)
                    if level is not None:
                        skip_entry = _LEVEL_PRIORITY.get(level, 20) < min_priority
                    if not skip_entry:
                        selected.append(raw_line.decode("utf-8", errors="replace"))

                if selected:
                    await _write_sse(
                        response,
                        event=None,
                        data={"lines": selected},
                        cursor=_cursor_for(file).encode(),
                    )
                    last_write = asyncio.get_running_loop().time()
                continue

            successor, expired = await asyncio.to_thread(_open_successor, file)
            if successor is not None:
                file.close()
                file = successor
                skip_entry = False
                continue
            if expired:
                file.close()
                file = await asyncio.to_thread(_open_current, at_end=True)
                if file is None:
                    await asyncio.sleep(poll_interval)
                    continue
                await _write_sse(
                    response,
                    event="reset",
                    data={"reason": "cursor_expired"},
                    cursor=_cursor_for(file).encode(),
                )
                last_write = asyncio.get_running_loop().time()
                skip_entry = False
                continue

            now = asyncio.get_running_loop().time()
            if now - last_write >= heartbeat_interval:
                await _write_sse(
                    response,
                    event="heartbeat",
                    data={},
                    cursor=_cursor_for(file).encode(),
                )
                last_write = now
            await asyncio.sleep(poll_interval)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if file is not None:
            file.close()

    return response


@routes.get("/api/logs/{filename}")
async def read_log(request: web.Request) -> web.Response:
    """读取指定日志文件，支持按等级获取末尾条目或按行分页。"""
    filename = request.match_info["filename"]

    if not _SAFE_FILENAME.fullmatch(filename):
        raise web.HTTPBadRequest(reason="Invalid filename")

    path = LOG_DIR / filename
    if not path.is_file():
        raise web.HTTPNotFound(reason="Log file not found")

    tail = request.query.get("tail")
    if tail is not None:
        count = _parse_bounded_int(
            tail,
            default=500,
            minimum=1,
            maximum=_MAX_TAIL_ENTRIES,
            name="tail",
        )
        lines, entry_count, cursor = await asyncio.to_thread(
            _tail_snapshot,
            path,
            count,
            _minimum_priority(request.query.get("level")),
        )
        return web.json_response(
            {
                "filename": filename,
                "entries": entry_count,
                "lines": lines,
                "cursor": cursor,
            }
        )

    offset = _parse_bounded_int(
        request.query.get("offset"),
        default=0,
        minimum=0,
        maximum=2**63 - 1,
        name="offset",
    )
    limit = _parse_bounded_int(
        request.query.get("limit"),
        default=200,
        minimum=1,
        maximum=_MAX_PAGE_LINES,
        name="limit",
    )
    lines, total = await asyncio.to_thread(_read_page, path, offset, limit)
    return web.json_response(
        {
            "filename": filename,
            "total": total,
            "offset": offset,
            "limit": limit,
            "lines": lines,
        }
    )
