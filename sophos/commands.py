"""Dot-command 处理器。

处理以 '.' 开头的管理命令（如 .llm），通过 QQ 消息交互。
"""

import logging
from typing import Any

import asyncpg

from sophos.llm.provider_manager import ProviderManager
from sophos.onebot_api import OneBotAPI
from sophos import runtime_config, trigger

logger = logging.getLogger(__name__)


def _extract_type_flag(args: list[str]) -> str:
    """从参数列表中提取 --type 值，默认 'openai'。"""
    for i, arg in enumerate(args):
        if arg == "--type" and i + 1 < len(args):
            return args[i + 1]
    return "openai"


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
        api_type_str = f" ({info['api_type']})" if info.get("api_type", "openai") != "openai" else ""
        reply = f"当前模型: {info['alias']} / {info['model']}{api_type_str}"

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
            reply = "用法: .llm switch <alias>/<model> [--type gemini]"
        else:
            alias, _, model = parts[2].partition("/")
            api_type = _extract_type_flag(parts[3:])
            reply = await provider_mgr.switch(alias, model, api_type=api_type)

    elif sub == "vision":
        vision_sub = parts[2] if len(parts) > 2 else ""
        if vision_sub == "":
            info = provider_mgr.current_info()
            if info.get("vision_alias"):
                vtype = f" ({info['vision_api_type']})" if info.get("vision_api_type", "openai") != "openai" else ""
                reply = f"Vision 模型: {info['vision_alias']} / {info['vision_model']}{vtype}"
            else:
                reply = "Vision 未配置"
        elif vision_sub == "switch":
            if len(parts) < 4 or "/" not in parts[3]:
                reply = "用法: .llm vision switch <alias>/<model> [--type gemini]"
            else:
                alias, _, model = parts[3].partition("/")
                api_type = _extract_type_flag(parts[4:])
                reply = await provider_mgr.switch_vision(alias, model, api_type=api_type)
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
            "  .llm switch <alias>/<model> [--type gemini]\n"
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


async def handle_memory_command(
    text: str,
    *,
    api: OneBotAPI,
    event: dict[str, Any],
    provider_mgr: ProviderManager,
    pool: asyncpg.Pool,
) -> bool:
    """处理 .memory 命令。返回 True 表示已处理。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        # 统计 + 当前模型
        from sophos.memory.store import MemoryStore

        store = MemoryStore(pool, None)
        stats = await store.get_memory_stats()
        info = provider_mgr.current_info()
        embed_model = info.get("embedding_model", "未配置")
        embed_alias = info.get("embedding_alias", "")
        cfg = await pool.fetchrow("SELECT dimension, endpoint, extra_body, migration_status FROM embedding_config WHERE id = 1")
        dim = cfg["dimension"] if cfg else 0
        ep = cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings"
        eb = cfg["extra_body"] if cfg and cfg["extra_body"] else None
        mig = cfg["migration_status"] if cfg else "none"
        reply = (
            f"记忆系统:\n"
            f"  嵌入模型: {embed_alias}/{embed_model} (维度: {dim})\n"
            f"  Endpoint: {ep}\n"
            f"  Extra body: {eb}\n"
            f"  迁移状态: {mig}\n"
            f"  会话档案: {stats['profile_context']} 条\n"
            f"  用户档案: {stats['profile_user']} 条\n"
            f"  记忆: {stats['memories']} 条"
        )

    elif sub == "model":
        sub2 = parts[2] if len(parts) > 2 else ""
        if sub2 == "switch":
            if len(parts) < 4 or "/" not in parts[3]:
                reply = "用法: .memory model switch <alias>/<model>"
            else:
                alias, model = parts[3].split("/", 1)
                reply = await provider_mgr.switch_embedding(alias, model)
        else:
            info = provider_mgr.current_info()
            embed_model = info.get("embedding_model", "未配置")
            embed_alias = info.get("embedding_alias", "")
            cfg = await pool.fetchrow("SELECT dimension FROM embedding_config WHERE id = 1")
            dim = cfg["dimension"] if cfg else 0
            reply = f"嵌入模型: {embed_alias}/{embed_model}\n维度: {dim}"

    elif sub == "endpoint":
        sub2 = parts[2] if len(parts) > 2 else ""
        if sub2 == "":
            cfg = await pool.fetchrow("SELECT endpoint FROM embedding_config WHERE id = 1")
            ep = cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings"
            reply = f"当前 endpoint: {ep}"
        else:
            reply = await provider_mgr.set_embedding_endpoint(sub2)

    elif sub == "extra_body":
        arg = " ".join(parts[2:]) if len(parts) > 2 else ""
        if arg == "":
            cfg = await pool.fetchrow("SELECT extra_body FROM embedding_config WHERE id = 1")
            eb = cfg["extra_body"] if cfg and cfg["extra_body"] else None
            reply = f"当前 extra_body: {eb}"
        elif arg == "clear":
            reply = await provider_mgr.set_embedding_extra_body("")
        else:
            reply = await provider_mgr.set_embedding_extra_body(arg)

    elif sub == "migrate":
        sub2 = parts[2] if len(parts) > 2 else ""
        if sub2 == "status":
            cfg = await pool.fetchrow("SELECT migration_status, pending_model, pending_dimension FROM embedding_config WHERE id = 1")
            if not cfg:
                reply = "embedding 未配置"
            else:
                reply = (
                    f"迁移状态: {cfg['migration_status']}\n"
                    f"待迁移模型: {cfg['pending_model'] or '无'}\n"
                    f"待迁移维度: {cfg['pending_dimension'] or '无'}"
                )
        elif sub2 == "rollback":
            reply = await provider_mgr.rollback_embedding_migration()
        else:
            try:
                reply = await provider_mgr.run_embedding_migration()
            except Exception as e:
                reply = f"迁移失败: {e}"

    else:
        reply = (
            "用法:\n"
            "  .memory                              — 记忆统计\n"
            "  .memory model                        — 嵌入模型详情\n"
            "  .memory model switch <alias>/<model>  — 切换嵌入模型\n"
            "  .memory endpoint                     — 查看 API endpoint\n"
            "  .memory endpoint <path>              — 切换 endpoint\n"
            "  .memory extra_body                   — 查看 extra_body\n"
            "  .memory extra_body <json>            — 设置 extra_body\n"
            "  .memory extra_body clear             — 清除 extra_body\n"
            "  .memory migrate                      — 执行迁移\n"
            "  .memory migrate status               — 迁移状态\n"
            "  .memory migrate rollback             — 回滚迁移"
        )

    await _reply(api, event, reply)
    return True


async def handle_config_command(
    text: str,
    *,
    api: OneBotAPI,
    event: dict[str, Any],
) -> bool:
    """处理 .config 命令。返回 True 表示已处理。"""
    parts = text.split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        # 列出所有配置
        all_cfg = runtime_config.get_all()
        defaults = runtime_config.get_defaults()
        lines = []
        for k, v in sorted(all_cfg.items()):
            marker = " *" if k in runtime_config._cache else ""
            lines.append(f"  {k}: {_format_value(v)}{marker}")
        reply = "运行时配置 (* = 已自定义):\n" + "\n".join(lines)

    elif sub == "reset":
        key = parts[2] if len(parts) > 2 else ""
        if not key:
            reply = "用法: .config reset <key>"
        elif key not in runtime_config.get_defaults():
            reply = f"未知配置项: {key}"
        else:
            existed = await runtime_config.delete(key)
            default_val = runtime_config.get_defaults().get(key)
            reply = f"{key} 已恢复默认值: {_format_value(default_val)}" if existed else f"{key} 未自定义"

    elif sub in runtime_config.get_defaults():
        # .config <key> 或 .config <key> <value>
        key = sub
        if len(parts) <= 2:
            val = runtime_config.get(key)
            default = runtime_config.get_defaults().get(key)
            is_custom = key in runtime_config._cache
            reply = f"{key}: {_format_value_full(val)}"
            if is_custom:
                reply += f"\n默认值: {_format_value_full(default)}"
        else:
            raw_value = parts[2]
            try:
                parsed = _parse_value(raw_value, key)
            except ValueError as e:
                reply = str(e)
            else:
                await runtime_config.set(key, parsed)
                reply = f"{key} = {_format_value(parsed)}"

    else:
        reply = (
            "用法:\n"
            "  .config              — 列出所有配置\n"
            "  .config <key>        — 查看某项\n"
            "  .config <key> <val>  — 修改\n"
            "  .config reset <key>  — 恢复默认值"
        )

    await _reply(api, event, reply)
    return True


def _format_value(v: Any) -> str:
    """格式化配置值（列表展示用，截断长字符串）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v if len(v) <= 60 else v[:57] + "..."
    return str(v)


def _format_value_full(v: Any) -> str:
    """格式化配置值（单项查看用，不截断）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _parse_value(raw: str, key: str) -> Any:
    """根据默认值类型推断并解析用户输入。"""
    default = runtime_config.get_defaults().get(key)
    if isinstance(default, bool):
        if raw.lower() in ("true", "1", "on", "是"):
            return True
        if raw.lower() in ("false", "0", "off", "否"):
            return False
        raise ValueError("布尔值请输入 true/false")
    if isinstance(default, int):
        try:
            v = int(raw)
        except ValueError:
            raise ValueError("请输入整数")
        _validate(key, v)
        return v
    if isinstance(default, float):
        try:
            v = float(raw)
        except ValueError:
            raise ValueError("请输入数字")
        _validate(key, v)
        return v
    # str
    _validate(key, raw)
    return raw


# 每个 key 的合法性规则：(校验函数, 错误提示)
_VALIDATORS: dict[str, tuple[Any, str]] = {
    "llm_temperature":    (lambda v: 0 <= v <= 2,        "范围 0~2"),
    "llm_max_tokens":     (lambda v: v > 0,              "必须 > 0"),
    "llm_max_tool_rounds":(lambda v: v > 0,              "必须 > 0"),
    "max_context_messages":(lambda v: v > 0,             "必须 > 0"),
    "recent_global_limit":(lambda v: v >= 0,             "必须 >= 0"),
    "recent_global_min_self":(lambda v: v >= 0,          "必须 >= 0"),
    "cross_context_mode": (lambda v: v in ("system", "inline", "off"),
                           "可选值: system / inline / off"),
    "llm_user_schema":    (lambda v: "{{message}}" in v, "必须包含 {{message}} 占位符"),
    "llm_bot_schema":     (lambda v: "{{message}}" in v, "必须包含 {{message}} 占位符"),
    "vision_refine_prompt": (lambda v: "{prev_description}" in v,
                             "必须包含 {prev_description} 占位符"),
    "forward_head_count": (lambda v: v >= 1,  "必须 >= 1"),
    "forward_tail_count": (lambda v: v >= 1,  "必须 >= 1"),
    "forward_max_depth":  (lambda v: v >= 1,  "必须 >= 1"),
    "reply_max_length":   (lambda v: v > 0,   "必须 > 0"),
}


def _validate(key: str, value: Any) -> None:
    """校验配置值合法性，不合法则 raise ValueError。"""
    rule = _VALIDATORS.get(key)
    if rule is None:
        return
    check, msg = rule
    if not check(value):
        raise ValueError(f"{key}: {msg}")


async def handle_prompt_command(
    text: str,
    *,
    api: OneBotAPI,
    event: dict[str, Any],
) -> bool:
    """处理 .prompt 命令。返回 True 表示已处理。"""
    from sophos.pipeline import _load_system_prompt, clear_system_prompt_cache

    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        prompt = _load_system_prompt()
        if len(prompt) > 500:
            reply = prompt[:500] + f"\n... (共 {len(prompt)} 字，用 .prompt full 查看完整)"
        else:
            reply = prompt

    elif sub == "full":
        reply = _load_system_prompt()

    elif sub == "reload":
        clear_system_prompt_cache()
        prompt = _load_system_prompt()
        reply = f"System prompt 已重载 ({len(prompt)} 字)"

    else:
        reply = (
            "用法:\n"
            "  .prompt          — 查看当前 prompt（截断 500 字）\n"
            "  .prompt full     — 完整显示\n"
            "  .prompt reload   — 重新加载文件"
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
