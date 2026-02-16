"""Dot-command 处理器。

处理以 '.' 开头的管理命令（如 .llm），通过 QQ 消息交互。
"""

import logging
from typing import Any

import asyncpg

from sophos.llm.provider_manager import ProviderManager
from sophos.onebot_api import OneBotAPI
from sophos import trigger

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

    elif sub == "vision":
        vision_sub = parts[2] if len(parts) > 2 else ""
        if vision_sub == "":
            info = provider_mgr.current_info()
            if info.get("vision_alias"):
                reply = f"Vision 模型: {info['vision_alias']} / {info['vision_model']}"
            else:
                reply = "Vision 未配置"
        elif vision_sub == "switch":
            if len(parts) < 4 or "/" not in parts[3]:
                reply = "用法: .llm vision switch <alias>/<model>"
            else:
                alias, _, model = parts[3].partition("/")
                reply = await provider_mgr.switch_vision(alias, model)
        elif vision_sub == "off":
            reply = await provider_mgr.disable_vision()
        else:
            reply = (
                "用法:\n"
                "  .llm vision              — 当前 vision 模型\n"
                "  .llm vision switch <alias>/<model>\n"
                "  .llm vision off          — 关闭 vision"
            )

    else:
        reply = (
            "用法:\n"
            "  .llm          — 当前模型\n"
            "  .llm list     — 列出 providers\n"
            "  .llm add <alias> <url> <key>\n"
            "  .llm remove <alias>\n"
            "  .llm models <alias>\n"
            "  .llm switch <alias>/<model>\n"
            "  .llm vision  — vision 模型管理"
        )

    await _reply(api, event, reply)
    return True


async def handle_trigger_command(
    text: str,
    *,
    api: OneBotAPI,
    event: dict[str, Any],
    pool: asyncpg.Pool,
) -> bool:
    """处理 .trigger 命令。返回 True 表示已处理。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        cfg = await trigger.load(pool)
        kw_lines = [f"  {k.word} (+{k.boost})" for k in cfg.keywords]
        reply = (
            f"触发配置:\n"
            f"  基础概率: {cfg.base_rate}\n"
            f"  @必回: {'开' if cfg.at_always else '关'}\n"
            f"  关键词 ({len(cfg.keywords)}):\n"
            + ("\n".join(kw_lines) if kw_lines else "    (无)")
        )

    elif sub == "rate":
        if len(parts) < 3:
            reply = "用法: .trigger rate <0~1>"
        else:
            try:
                rate = float(parts[2])
            except ValueError:
                reply = "概率必须是数字"
            else:
                if not 0 <= rate <= 1:
                    reply = "概率范围 0~1"
                else:
                    await trigger.set_base_rate(pool, rate)
                    reply = f"基础概率已设为 {rate}"

    elif sub == "at":
        if len(parts) < 3 or parts[2] not in ("on", "off"):
            reply = "用法: .trigger at <on|off>"
        else:
            value = parts[2] == "on"
            await trigger.set_at_always(pool, value)
            reply = f"@必回已{'开启' if value else '关闭'}"

    elif sub == "add":
        if len(parts) < 4:
            reply = "用法: .trigger add <关键词> <boost>"
        else:
            try:
                boost = float(parts[3])
            except ValueError:
                reply = "boost 必须是数字"
            else:
                if not 0 < boost <= 1:
                    reply = "boost 范围 (0, 1]"
                else:
                    await trigger.add_keyword(pool, parts[2], boost)
                    reply = f"关键词 '{parts[2]}' 已添加 (boost={boost})"

    elif sub == "remove":
        if len(parts) < 3:
            reply = "用法: .trigger remove <关键词>"
        else:
            removed = await trigger.remove_keyword(pool, parts[2])
            reply = f"关键词 '{parts[2]}' 已删除" if removed else f"关键词 '{parts[2]}' 不存在"

    else:
        reply = (
            "用法:\n"
            "  .trigger              — 当前配置\n"
            "  .trigger rate <0~1>   — 基础概率\n"
            "  .trigger at <on|off>  — @必回开关\n"
            "  .trigger add <词> <boost>\n"
            "  .trigger remove <词>"
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
