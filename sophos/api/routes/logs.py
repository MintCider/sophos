"""日志查看 API。"""

import asyncio
import json
import logging
import re
from pathlib import Path

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


def _parse_level(line: str) -> str | None:
    """从日志行中提取等级。"""
    m = _LOG_LINE_RE.match(line)
    return m.group(1) if m else None


@routes.get("/api/logs")
async def list_logs(request: web.Request) -> web.Response:
    """列出可用日志文件。"""
    if not LOG_DIR.exists():
        return web.json_response({"files": []})

    files = []
    for f in sorted(LOG_DIR.iterdir()):
        if f.is_file() and _SAFE_FILENAME.match(f.name):
            files.append({"name": f.name, "size": f.stat().st_size})
    return web.json_response({"files": files})


@routes.get("/api/logs/stream")
async def stream_logs(request: web.Request) -> web.StreamResponse:
    """SSE 实时推送新日志行。支持 ?level=INFO 服务端过滤。"""
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    min_level_name = request.query.get("level", "INFO").upper()
    min_priority = _LEVEL_PRIORITY.get(min_level_name, 20)

    log_path = LOG_DIR / "sophos.log"

    # 等待日志文件存在
    while not log_path.is_file():
        await asyncio.sleep(1)

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            # seek 到末尾，只推送新内容
            f.seek(0, 2)
            heartbeat_interval = 5.0  # 每 5 秒发送心跳
            poll_interval = 0.3
            elapsed = 0.0
            skip_entry = False  # 当前多行条目是否被等级过滤
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    # 定期发送心跳，让前端检测连接存活
                    if elapsed >= heartbeat_interval:
                        await resp.write(b"event: heartbeat\ndata: \n\n")
                        elapsed = 0.0
                    continue

                elapsed = 0.0  # 有数据时重置心跳计时
                line = line.rstrip("\n")
                if not line:
                    continue

                level = _parse_level(line)
                if level is not None:
                    # 新条目：检查等级过滤
                    priority = _LEVEL_PRIORITY.get(level, 20)
                    skip_entry = priority < min_priority
                    if skip_entry:
                        continue
                else:
                    # 续行：跟随上一条目的过滤结果
                    if skip_entry:
                        continue

                payload = json.dumps(
                    {"line": line, "level": level or ""},
                    ensure_ascii=False,
                )
                await resp.write(f"data: {payload}\n\n".encode())
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    except asyncio.CancelledError:
        pass

    return resp


def _group_entries(lines: list[str]) -> list[list[str]]:
    """将原始行按日志条目分组。有时间戳前缀的行开始新条目，无前缀的续行归入上一条。"""
    entries: list[list[str]] = []
    for line in lines:
        if _LOG_LINE_RE.match(line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            # 文件开头的孤立续行，单独成条
            entries.append([line])
    return entries


@routes.get("/api/logs/{filename}")
async def read_log(request: web.Request) -> web.Response:
    """读取指定日志文件。支持 ?tail=N（按条目计数）或 ?offset=N&limit=M（按行）。"""
    filename = request.match_info["filename"]

    if not _SAFE_FILENAME.match(filename):
        raise web.HTTPBadRequest(reason="Invalid filename")

    path = LOG_DIR / filename
    if not path.is_file():
        raise web.HTTPNotFound(reason="Log file not found")

    tail = request.query.get("tail")
    offset = request.query.get("offset")
    limit = request.query.get("limit")

    text = path.read_text(encoding="utf-8", errors="replace")
    raw_lines = text.splitlines()
    total_lines = len(raw_lines)

    if tail is not None:
        # 按条目计数：取最后 N 条日志（含续行）
        entries = _group_entries(raw_lines)
        n = min(int(tail), len(entries))
        tail_entries = entries[-n:]
        # 展平回行列表
        result_lines = [line for entry in tail_entries for line in entry]
        return web.json_response(
            {
                "filename": filename,
                "total": total_lines,
                "entries": len(entries),
                "lines": result_lines,
            }
        )

    start = int(offset) if offset else 0
    count = int(limit) if limit else 200
    sliced = raw_lines[start : start + count]

    return web.json_response(
        {
            "filename": filename,
            "total": total_lines,
            "offset": start,
            "limit": count,
            "lines": sliced,
        }
    )
