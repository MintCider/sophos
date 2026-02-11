"""OneBot v11 WebSocket API 调用层。

职责：
  1. 在全双工 WS 连接上实现 请求-响应匹配 (通过 echo 字段)
  2. 为上层提供 `async api.call(action, params) -> data` 的同步等待语义
  3. 把收到的 WS 消息分流为 "API 响应" 与 "事件上报"

架构位置：
  WS 主循环 → 调用 api.dispatch(raw_data) → 自动分流
  Tool / 任意协程 → await api.call("send_msg", {...}) → 阻塞等待结果
"""

import asyncio
import logging
import uuid
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class OneBotAPIError(Exception):
    """OneBot API 调用返回了非成功状态。"""

    def __init__(self, retcode: int, message: str, echo: str | None = None):
        self.retcode = retcode
        self.echo = echo
        super().__init__(f"OneBot API error (retcode={retcode}, echo={echo}): {message}")


class OneBotAPI:
    """OneBot v11 WebSocket API 调用器。

    使用方式：
        api = OneBotAPI(ws)
        result = await api.call("send_msg", {"message_type": "group", ...})
        # result == {"message_id": 12345}

    WS 主循环中需要对每条入站消息调用 api.dispatch()，
    由 dispatch 判断是 API 响应还是事件，返回事件供主循环处理。
    """

    DEFAULT_TIMEOUT: float = 30.0  # API 调用默认超时 (秒)

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws
        # echo -> Future，等待 OneBot 返回匹配 echo 的响应
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ── 对外接口 ──────────────────────────────────────────

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """发起一个 OneBot API 调用，阻塞等待响应并返回 data 字段。

        Args:
            action:  API 名称，如 "send_msg", "get_group_member_info"
            params:  传给 API 的参数字典
            timeout: 超时秒数，默认 DEFAULT_TIMEOUT

        Returns:
            OneBot 响应中的 data 字段 (dict)

        Raises:
            OneBotAPIError: retcode != 0
            asyncio.TimeoutError: 超时未收到响应
        """
        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        payload: dict[str, Any] = {"action": action, "echo": echo}
        if params:
            payload["params"] = params

        await self._ws.send_json(payload)
        logger.debug("API >>> %s (echo=%s)", action, echo)

        try:
            result = await asyncio.wait_for(future, timeout=timeout or self.DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("API call %s timed out (echo=%s)", action, echo)
            raise
        finally:
            # 无论成功、失败、超时，都清理 pending
            self._pending.pop(echo, None)

        return result

    def dispatch(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """分流一条 WS 入站消息。

        如果消息包含 echo 字段且在 pending 中 → 视为 API 响应，填充 Future，返回 None
        否则 → 视为事件上报，原样返回给调用者处理

        Args:
            data: 已解析的 JSON 字典

        Returns:
            事件字典 (需要上层处理) 或 None (已内部消化的 API 响应)
        """
        echo = data.get("echo")

        if echo is not None and echo in self._pending:
            self._resolve(echo, data)
            return None  # 已消化，不是事件

        # 没有 echo，或 echo 不在 pending 中 → 事件上报
        return data

    # ── 内部方法 ──────────────────────────────────────────

    def _resolve(self, echo: str, data: dict[str, Any]) -> None:
        """用 API 响应填充对应的 Future。"""
        future = self._pending.get(echo)
        if future is None or future.done():
            return

        retcode = data.get("retcode", -1)
        if retcode == 0:
            # 成功 → 把 data 字段交给等待者
            future.set_result(data.get("data") or {})
            logger.debug("API <<< echo=%s OK", echo)
        else:
            # 失败 → 抛异常给等待者
            status = data.get("status", "failed")
            future.set_exception(OneBotAPIError(retcode, status, echo))
            logger.warning("API <<< echo=%s FAILED retcode=%s", echo, retcode)

    def cancel_all(self, reason: str = "connection closed") -> None:
        """连接断开时，取消所有 pending 的 Future。"""
        for echo, future in self._pending.items():
            if not future.done():
                future.cancel(msg=reason)
                logger.debug("Cancelled pending API call echo=%s: %s", echo, reason)
        self._pending.clear()
