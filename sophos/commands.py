"""Dot-command 处理器。

处理以 '.' 开头的管理命令（如 .llm），通过 QQ 消息交互。
"""

import logging
from typing import Any

from sophos.llm.provider_manager import ProviderManager
from sophos.onebot_api import OneBotAPI

logger = logging.getLogger(__name__)


async def handle_llm_command(
    text: str,
    *,
    api: OneBotAPI,
    event: dict[str, Any],
    provider_mgr: ProviderManager,
) -> bool:
    """处理 .llm 命令。返回 True 表示已处理。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        info = provider_mgr.current_info()
        reply = f"当前模型: {info['alias']} / {info['model']}"

    elif sub == "list":
        providers = await provider_mgr.list_providers()
        if not providers:
            reply = "暂无 provider"
        else:
            lines = []
            for p in providers:
                marker = " ← 活跃" if p["is_active"] else ""
                model_info = f" ({p['active_model']})" if p["is_active"] else ""
                lines.append(
                    f"  {p['alias']}: {p['base_url']} "
                    f"[{p['model_count']} models]{model_info}{marker}"
                )
            reply = "Providers:\n" + "\n".join(lines)

    elif sub == "add":
        if len(parts) < 5:
            reply = "用法: .llm add <alias> <base_url> <api_key>"
        else:
            reply = await provider_mgr.add_provider(parts[2], parts[3], parts[4])

    elif sub == "remove":
        if len(parts) < 3:
            reply = "用法: .llm remove <alias>"
        else:
            reply = await provider_mgr.remove_provider(parts[2])

    elif sub == "models":
        if len(parts) < 3:
            reply = "用法: .llm models <alias>"
        else:
            result = await provider_mgr.fetch_models(parts[2])
            if isinstance(result, str):
                reply = result
            elif not result:
                reply = "该 provider 无可用模型"
            else:
                lines = [f"  {m}" for m in result]
                reply = f"模型列表 ({len(result)}):\n" + "\n".join(lines)

    elif sub == "switch":
        if len(parts) < 3 or "/" not in parts[2]:
            reply = "用法: .llm switch <alias>/<model>"
        else:
            alias, _, model = parts[2].partition("/")
            reply = await provider_mgr.switch(alias, model)

    else:
        reply = (
            "用法:\n"
            "  .llm          — 当前模型\n"
            "  .llm list     — 列出 providers\n"
            "  .llm add <alias> <url> <key>\n"
            "  .llm remove <alias>\n"
            "  .llm models <alias>\n"
            "  .llm switch <alias>/<model>"
        )

    await _reply(api, event, reply)
    return True


async def _reply(api: OneBotAPI, event: dict[str, Any], text: str) -> None:
    """向来源会话发送回复。"""
    msg_type = event.get("message_type", "private")
    params: dict[str, Any] = {
        "message_type": msg_type,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    if msg_type == "group":
        params["group_id"] = event.get("group_id")
    else:
        params["user_id"] = event.get("user_id")
    await api.call("send_msg", params)
