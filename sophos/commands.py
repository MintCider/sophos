"""Dot-command 处理器。

处理以 '.' 开头的管理命令（如 .llm），通过来源会话交互。
"""

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from sophos import permission, runtime_config, trigger
from sophos.llm.provider_manager import ProviderManager
from sophos.messaging import MessageService
from sophos.platform import Capability, ConversationKind, SendMessageRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Platform-neutral dependencies shared by dot-command handlers."""

    message_service: MessageService
    conversation_id: int
    conversation_kind: ConversationKind


def _extract_type_flag(args: list[str]) -> str:
    """从参数列表中提取 --type 值，默认 'openai'。"""
    for i, arg in enumerate(args):
        if arg == "--type" and i + 1 < len(args):
            return args[i + 1]
    return "openai"


async def handle_llm_command(
    text: str,
    *,
    context: CommandContext,
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
                active_slots = p["active_slots"]
                active_info = ", ".join(
                    f"{slot['key']}={slot['model']}" for slot in active_slots
                )
                marker = f" ← {active_info}" if active_info else ""
                urls = p["base_urls"]
                url_display = urls.get("openai", "?")
                extra_urls = [f"{k}={v}" for k, v in urls.items() if k != "openai"]
                if extra_urls:
                    url_display += f" ({', '.join(extra_urls)})"
                lines.append(f"  {p['alias']}: {url_display} [{p['model_count']} models]{marker}")
            reply = "Providers:\n" + "\n".join(lines)

    elif sub == "add":
        if len(parts) < 5:
            reply = "用法: .llm add <alias> <url> <key> [--gemini <url>] [--anthropic <url>]"
        else:
            alias_arg, url_arg, key_arg = parts[2], parts[3], parts[4]
            base_urls: dict[str, str] = {"openai": url_arg}
            extra_args = parts[5:]
            for i, arg in enumerate(extra_args):
                if arg == "--gemini" and i + 1 < len(extra_args):
                    base_urls["gemini"] = extra_args[i + 1]
                elif arg == "--anthropic" and i + 1 < len(extra_args):
                    base_urls["anthropic"] = extra_args[i + 1]
            reply = await provider_mgr.add_provider(alias_arg, base_urls, key_arg)

    elif sub == "remove":
        if len(parts) < 3:
            reply = "用法: .llm remove <alias>"
        else:
            reply = await provider_mgr.remove_provider(parts[2])

    elif sub == "url":
        if len(parts) < 3:
            reply = (
                "用法:\n"
                "  .llm url <alias>                — 查看所有 URL\n"
                "  .llm url <alias> <type> <url>   — 设置 per-type URL\n"
                "  .llm url <alias> <type> clear    — 清除（回退到自动推导）"
            )
        elif len(parts) == 3:
            result = await provider_mgr.get_provider_urls(parts[2])
            if isinstance(result, str):
                reply = result
            else:
                if not result:
                    reply = f"'{parts[2]}' 无 URL 配置"
                else:
                    lines = [f"  {k}: {v}" for k, v in result.items()]
                    reply = f"'{parts[2]}' URLs:\n" + "\n".join(lines)
        elif len(parts) >= 5:
            api_type_arg = parts[3]
            if api_type_arg not in ("openai", "gemini", "anthropic"):
                reply = "type 必须是 openai / gemini / anthropic"
            elif parts[4] == "clear":
                reply = await provider_mgr.set_provider_url(parts[2], api_type_arg, None)
            else:
                reply = await provider_mgr.set_provider_url(parts[2], api_type_arg, parts[4])
        else:
            reply = "用法: .llm url <alias> <type> <url|clear>"

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
            reply = "用法: .llm switch <alias>/<model> [--type openai|gemini|anthropic]"
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
                reply = "用法: .llm vision switch <alias>/<model> [--type openai|gemini|anthropic]"
            else:
                alias, _, model = parts[3].partition("/")
                api_type = _extract_type_flag(parts[4:])
                reply = await provider_mgr.switch_vision(alias, model, api_type=api_type)
        elif vision_sub == "off":
            reply = await provider_mgr.disable_vision()
        elif vision_sub == "extra_body":
            if len(parts) < 4:
                reply = await provider_mgr.get_active_extra_body("vision")
            else:
                arg = " ".join(parts[3:])
                if arg == "clear":
                    reply = await provider_mgr.set_provider_extra_body("vision", "")
                else:
                    reply = await provider_mgr.set_provider_extra_body("vision", arg)
        elif vision_sub == "timeout":
            if len(parts) < 4:
                t = await provider_mgr._pool.fetchval("SELECT request_timeout FROM llm_active WHERE key = 'vision'")
                reply = f"Vision timeout: {t or 30}s"
            else:
                try:
                    reply = await provider_mgr.set_timeout("vision", int(parts[3]))
                except ValueError:
                    reply = "用法: .llm vision timeout <秒>"
        else:
            reply = (
                "用法:\n"
                "  .llm vision              — 当前 vision 模型\n"
                "  .llm vision switch <alias>/<model>\n"
                "  .llm vision extra_body [json|clear]\n"
                "  .llm vision timeout <秒>\n"
                "  .llm vision off          — 关闭 vision"
            )

    elif sub == "collector":
        collector_sub = parts[2] if len(parts) > 2 else ""
        if collector_sub == "":
            info = provider_mgr.current_info()
            if info.get("collector_alias"):
                ctype = (
                    f" ({info['collector_api_type']})"
                    if info.get("collector_api_type", "openai") != "openai"
                    else ""
                )
                reply = (
                    f"Collector 模型: {info['collector_alias']} / "
                    f"{info['collector_model']}{ctype}"
                )
            else:
                reply = "Collector 未配置"
        elif collector_sub == "switch":
            if len(parts) < 4 or "/" not in parts[3]:
                reply = "用法: .llm collector switch <alias>/<model> [--type openai|gemini|anthropic]"
            else:
                alias, _, model = parts[3].partition("/")
                api_type = _extract_type_flag(parts[4:])
                reply = await provider_mgr.switch_collector(alias, model, api_type=api_type)
        elif collector_sub == "extra_body":
            if len(parts) < 4:
                reply = await provider_mgr.get_active_extra_body("collector")
            else:
                arg = " ".join(parts[3:])
                reply = await provider_mgr.set_provider_extra_body(
                    "collector",
                    "" if arg == "clear" else arg,
                )
        elif collector_sub == "timeout":
            if len(parts) < 4:
                timeout = await provider_mgr._pool.fetchval(
                    "SELECT request_timeout FROM llm_active WHERE key = 'collector'"
                )
                reply = f"Collector timeout: {timeout or 60}s"
            else:
                try:
                    reply = await provider_mgr.set_timeout("collector", int(parts[3]))
                except ValueError:
                    reply = "用法: .llm collector timeout <秒>"
        else:
            reply = (
                "用法:\n"
                "  .llm collector              — 当前 collector 模型\n"
                "  .llm collector switch <alias>/<model>\n"
                "  .llm collector extra_body [json|clear]\n"
                "  .llm collector timeout <秒>"
            )

    elif sub == "trigger":
        trigger_sub = parts[2] if len(parts) > 2 else ""
        if trigger_sub == "":
            info = provider_mgr.current_info()
            if info.get("trigger_alias"):
                ttype = f" ({info['trigger_api_type']})" if info.get("trigger_api_type", "openai") != "openai" else ""
                reply = f"Trigger 模型: {info['trigger_alias']} / {info['trigger_model']}{ttype}"
            else:
                reply = "Trigger 未配置"
        elif trigger_sub == "switch":
            if len(parts) < 4 or "/" not in parts[3]:
                reply = "用法: .llm trigger switch <alias>/<model> [--type openai|gemini|anthropic]"
            else:
                alias, _, model = parts[3].partition("/")
                api_type = _extract_type_flag(parts[4:])
                reply = await provider_mgr.switch_trigger(alias, model, api_type=api_type)
        elif trigger_sub == "off":
            reply = await provider_mgr.disable_trigger()
        elif trigger_sub == "extra_body":
            if len(parts) < 4:
                reply = await provider_mgr.get_active_extra_body("trigger")
            else:
                arg = " ".join(parts[3:])
                if arg == "clear":
                    reply = await provider_mgr.set_provider_extra_body("trigger", "")
                else:
                    reply = await provider_mgr.set_provider_extra_body("trigger", arg)
        elif trigger_sub == "timeout":
            if len(parts) < 4:
                t = await provider_mgr._pool.fetchval("SELECT request_timeout FROM llm_active WHERE key = 'trigger'")
                reply = f"Trigger timeout: {t or 15}s"
            else:
                try:
                    reply = await provider_mgr.set_timeout("trigger", int(parts[3]))
                except ValueError:
                    reply = "用法: .llm trigger timeout <秒>"
        else:
            reply = (
                "用法:\n"
                "  .llm trigger              — 当前 trigger 模型\n"
                "  .llm trigger switch <alias>/<model>\n"
                "  .llm trigger extra_body [json|clear]\n"
                "  .llm trigger timeout <秒>\n"
                "  .llm trigger off          — 关闭 trigger"
            )

    elif sub == "extra_body":
        # .llm extra_body [json|clear]  — 操作当前 default slot
        if len(parts) < 3:
            reply = await provider_mgr.get_active_extra_body("default")
        else:
            arg = " ".join(parts[2:])
            if arg == "clear":
                reply = await provider_mgr.set_provider_extra_body("default", "")
            else:
                reply = await provider_mgr.set_provider_extra_body("default", arg)

    elif sub == "timeout":
        if len(parts) < 3:
            t = await provider_mgr._pool.fetchval("SELECT request_timeout FROM llm_active WHERE key = 'default'")
            reply = f"Timeout: {t or 60}s"
        else:
            try:
                reply = await provider_mgr.set_timeout("default", int(parts[2]))
            except ValueError:
                reply = "用法: .llm timeout <秒>"

    else:
        reply = (
            "用法:\n"
            "  .llm          — 当前模型\n"
            "  .llm list     — 列出 providers\n"
            "  .llm add <alias> <url> <key> [--gemini <url>] [--anthropic <url>]\n"
            "  .llm remove <alias>\n"
            "  .llm url <alias> [<type> <url|clear>]  — 管理 per-type URL\n"
            "  .llm models <alias>\n"
            "  .llm switch <alias>/<model> [--type openai|gemini|anthropic]\n"
            "  .llm extra_body [json|clear]  — 当前模型的 extra_body\n"
            "  .llm timeout [秒]  — 请求超时\n"
            "  .llm collector — collector 模型管理\n"
            "  .llm vision   — vision 模型管理\n"
            "  .llm trigger  — trigger 模型管理"
        )

    await _reply(context, reply)
    return True


async def handle_trigger_command(
    text: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
    provider_mgr: ProviderManager,
) -> bool:
    """处理 .trigger 命令。返回 True 表示已处理。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        cfg = await trigger.load(pool)
        kw_lines = [f"  {k.word} (+{k.boost})" for k in cfg.keywords]
        # LLM trigger 状态
        info = provider_mgr.current_info()
        if info.get("trigger_alias"):
            ttype = f" ({info['trigger_api_type']})" if info.get("trigger_api_type", "openai") != "openai" else ""
            llm_line = f"  LLM 触发: 已启用 ({info['trigger_alias']}/{info['trigger_model']}{ttype})"
        else:
            llm_line = "  LLM 触发: 未配置"
        delay = runtime_config.get("trigger_delay")
        qps = runtime_config.get("trigger_qps")
        wait_timeout = runtime_config.get("trigger_wait_timeout")
        bucket_cap = runtime_config.get("trigger_bucket_capacity")
        bucket_refill = runtime_config.get("trigger_bucket_refill")
        reply = (
            f"触发配置:\n"
            f"  基础概率: {cfg.base_rate}\n"
            f"  @必回: {'开' if cfg.at_always else '关'}\n"
            f"  关键词 ({len(cfg.keywords)}):\n" + ("\n".join(kw_lines) if kw_lines else "    (无)") + f"\n{llm_line}\n"
            f"  延迟: {delay}s | QPS: {qps} | 等待超时: {wait_timeout}s\n"
            f"  配额桶: {bucket_cap}/{bucket_refill}s"
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

    elif sub == "mute":
        if len(parts) < 3:
            reply = "用法: .trigger mute <内部用户ID> [备注]"
        else:
            target = parts[2]
            if not target.isdigit():
                reply = "内部用户ID必须是数字"
            else:
                uid = int(target)
                if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id = $1)", uid):
                    reply = f"内部用户不存在: {uid}"
                else:
                    note = " ".join(parts[3:]) if len(parts) > 3 else None
                    from sophos import user_policy

                    await user_policy.set_policy(
                        pool,
                        uid,
                        suppress_llm_trigger=True,
                        rate_multiplier=0.0,
                        suppress_refresh=True,
                        note=note,
                    )
                    reply = f"已静默用户 {uid}"
                    if note:
                        reply += f" ({note})"

    elif sub == "unmute":
        if len(parts) < 3:
            reply = "用法: .trigger unmute <内部用户ID>"
        else:
            target = parts[2]
            if not target.isdigit():
                reply = "内部用户ID必须是数字"
            else:
                uid = int(target)
                from sophos import user_policy

                removed = await user_policy.remove_policy(pool, uid)
                reply = f"已解除用户 {uid} 的静默" if removed else f"用户 {uid} 无静默策略"

    elif sub == "policy":
        from sophos import user_policy

        sub2 = parts[2] if len(parts) > 2 else ""
        if sub2 == "":
            policies = await user_policy.get_all_policies(pool)
            if not policies:
                reply = "无用户触发策略"
            else:
                lines = []
                for p in policies:
                    scope = f"{p.scope_type}:{p.scope_id}" if p.scope_type != "global" else "全局"
                    flags = []
                    if p.suppress_llm_trigger:
                        flags.append("禁LLM触发")
                    if p.rate_multiplier == 0.0:
                        flags.append("概率=0")
                    elif p.rate_multiplier != 1.0:
                        flags.append(f"概率×{p.rate_multiplier}")
                    if p.suppress_refresh:
                        flags.append("禁刷新注入")
                    flag_str = ", ".join(flags) if flags else "无限制"
                    note_str = f" ({p.note})" if p.note else ""
                    lines.append(f"  {p.user_id} [{scope}]: {flag_str}{note_str}")
                reply = "用户触发策略:\n" + "\n".join(lines)
        elif sub2.isdigit():
            uid = int(sub2)
            if len(parts) > 3:
                field = parts[3]
                valid_fields = {
                    "suppress_llm_trigger",
                    "rate_multiplier",
                    "suppress_refresh",
                    "note",
                }
                if field not in valid_fields:
                    reply = f"未知字段: {field}\n可用: {', '.join(sorted(valid_fields))}"
                elif len(parts) < 5:
                    reply = f"用法: .trigger policy <内部用户ID> {field} <值>"
                else:
                    raw_val = " ".join(parts[4:]) if field == "note" else parts[4]
                    try:
                        parsed = _parse_policy_value(field, raw_val)
                    except ValueError as e:
                        reply = str(e)
                    else:
                        updated = await user_policy.update_field(
                            pool,
                            uid,
                            "global",
                            0,
                            field,
                            parsed,
                        )
                        if updated:
                            reply = f"用户 {uid} 的 {field} 已设为 {parsed}"
                        else:
                            reply = f"用户 {uid} 无策略，请先 .trigger mute {uid}"
            else:
                p = await user_policy.get_policy(pool, uid, "global", 0)
                if p is None:
                    reply = f"用户 {uid} 无触发策略"
                else:
                    reply = (
                        f"用户 {uid} 的触发策略:\n"
                        f"  抑制LLM触发: {'是' if p.suppress_llm_trigger else '否'}\n"
                        f"  概率倍率: {p.rate_multiplier}\n"
                        f"  抑制刷新注入: {'是' if p.suppress_refresh else '否'}\n"
                        f"  备注: {p.note or '(无)'}"
                    )
        else:
            reply = (
                "用法:\n"
                "  .trigger policy                              — 列出所有策略\n"
                "  .trigger policy <内部用户ID>                  — 查看用户策略\n"
                "  .trigger policy <内部用户ID> <字段> <值>       — 修改字段\n"
                "字段: suppress_llm_trigger, rate_multiplier, suppress_refresh, note"
            )

    else:
        reply = (
            "用法:\n"
            "  .trigger              — 当前配置\n"
            "  .trigger rate <0~1>   — 基础概率\n"
            "  .trigger at <on|off>  — @必回开关\n"
            "  .trigger add <词> <boost>\n"
            "  .trigger remove <词>\n"
            "  .trigger mute <内部用户ID> [备注]  — 静默用户\n"
            "  .trigger unmute <内部用户ID>       — 解除静默\n"
            "  .trigger policy              — 用户策略管理"
        )

    await _reply(context, reply)
    return True


def _parse_policy_value(field: str, raw: str) -> bool | float | str | None:
    """解析策略字段值。"""
    if field in ("suppress_llm_trigger", "suppress_refresh"):
        if raw.lower() in ("true", "1", "on", "是"):
            return True
        if raw.lower() in ("false", "0", "off", "否"):
            return False
        raise ValueError("布尔值请输入 true/false")
    if field == "rate_multiplier":
        try:
            v = float(raw)
        except ValueError:
            raise ValueError("倍率必须是数字") from None
        if not 0.0 <= v <= 10.0:
            raise ValueError("倍率范围 0~10")
        return v
    # note
    return raw if raw != "clear" else None


async def handle_memory_command(
    text: str,
    *,
    context: CommandContext,
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
        cfg = await pool.fetchrow("SELECT dimension, endpoint, migration_status FROM embedding_config WHERE id = 1")
        dim = cfg["dimension"] if cfg else 0
        ep = cfg["endpoint"] if cfg and cfg["endpoint"] else "/embeddings"
        mig = cfg["migration_status"] if cfg else "none"
        eb = await pool.fetchval("SELECT extra_body FROM llm_active WHERE key = 'embedding'")
        reply = (
            f"记忆系统:\n"
            f"  嵌入模型: {embed_alias}/{embed_model} (维度: {dim})\n"
            f"  Endpoint: {ep}\n"
            f"  Extra body: {eb}\n"
            f"  迁移状态: {mig}\n"
            f"  自我档案: {'已设置' if stats['profile_self'] else '未设置'}\n"
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
            eb = await pool.fetchval("SELECT extra_body FROM llm_active WHERE key = 'embedding'")
            reply = f"当前 extra_body: {eb}"
        elif arg == "clear":
            reply = await provider_mgr.set_embedding_extra_body("")
        else:
            reply = await provider_mgr.set_embedding_extra_body(arg)

    elif sub == "migrate":
        sub2 = parts[2] if len(parts) > 2 else ""
        if sub2 == "status":
            cfg = await pool.fetchrow(
                "SELECT migration_status, pending_model, pending_dimension FROM embedding_config WHERE id = 1",
            )
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

    await _reply(context, reply)
    return True


async def handle_config_command(
    text: str,
    *,
    context: CommandContext,
) -> bool:
    """处理 .config 命令。返回 True 表示已处理。"""
    parts = text.split(maxsplit=2)
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        # 列出所有配置
        all_cfg = runtime_config.get_all()
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
                await runtime_config.set_value(key, parsed)
                reply = f"{key} = {_format_value(parsed)}"

    else:
        reply = (
            "用法:\n"
            "  .config              — 列出所有配置\n"
            "  .config <key>        — 查看某项\n"
            "  .config <key> <val>  — 修改\n"
            "  .config reset <key>  — 恢复默认值"
        )

    await _reply(context, reply)
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
            raise ValueError("请输入整数") from None
        _validate(key, v)
        return v
    if isinstance(default, float):
        try:
            v = float(raw)
        except ValueError:
            raise ValueError("请输入数字") from None
        _validate(key, v)
        return v
    # str
    _validate(key, raw)
    return raw


# 每个 key 的合法性规则：(校验函数, 错误提示)
_VALIDATORS: dict[str, tuple[Any, str]] = {
    "llm_temperature": (lambda v: 0 <= v <= 2, "范围 0~2"),
    "llm_max_tokens": (lambda v: v > 0, "必须 > 0"),
    "llm_max_tool_rounds": (lambda v: v > 0, "必须 > 0"),
    "max_context_messages": (lambda v: v > 0, "必须 > 0"),
    "recent_global_limit": (lambda v: v >= 0, "必须 >= 0"),
    "recent_global_min_self": (lambda v: v >= 0, "必须 >= 0"),
    "cross_context_mode": (lambda v: v in ("system", "inline", "off"), "可选值: system / inline / off"),
    "llm_user_schema": (lambda v: "{{message}}" in v, "必须包含 {{message}} 占位符"),
    "llm_bot_schema": (lambda v: "{{message}}" in v, "必须包含 {{message}} 占位符"),
    "vision_refine_prompt": (lambda v: "{prev_description}" in v, "必须包含 {prev_description} 占位符"),
    "forward_head_count": (lambda v: v >= 1, "必须 >= 1"),
    "forward_tail_count": (lambda v: v >= 1, "必须 >= 1"),
    "forward_max_depth": (lambda v: v >= 1, "必须 >= 1"),
    "reply_max_length": (lambda v: v > 0, "必须 > 0"),
    "trigger_delay": (lambda v: 0 <= v <= 30, "范围 0~30"),
    "trigger_qps": (lambda v: 0.01 <= v <= 10, "范围 0.01~10"),
    "trigger_wait_timeout": (lambda v: 5 <= v <= 300, "范围 5~300"),
    "trigger_eval_context_limit": (lambda v: v > 0, "必须 > 0"),
    "trigger_eval_max_tokens": (lambda v: v > 0, "必须 > 0"),
    "trigger_eval_temperature": (lambda v: 0 <= v <= 2, "范围 0~2"),
    "trigger_bucket_capacity": (lambda v: v > 0, "必须 > 0"),
    "trigger_bucket_refill": (lambda v: v > 0, "必须 > 0"),
    "tavily_max_results": (lambda v: 1 <= v <= 20, "范围 1~20"),
    "cleanup_messages_max_bytes": (lambda v: v > 0, "必须 > 0"),
    "cleanup_memories_max_bytes": (lambda v: v > 0, "必须 > 0"),
    "cleanup_interval_seconds": (lambda v: v > 0, "必须 > 0"),
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
    context: CommandContext,
) -> bool:
    """处理 .prompt 命令。返回 True 表示已处理。"""
    from sophos.pipeline import _load_system_prompt, clear_system_prompt_cache

    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        prompt = _load_system_prompt()
        reply = prompt[:500] + f"\n... (共 {len(prompt)} 字，用 .prompt full 查看完整)" if len(prompt) > 500 else prompt

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

    await _reply(context, reply)
    return True


# ── 权限相关常量 ──────────────────────────────────────────

_ALL_PERMS: frozenset[str] = frozenset(
    {
        "bot",
        "bot.shared",
        "bot.direct",
        "cmd.tools",
        "cmd.config",
        "cmd.llm",
        "cmd.trigger",
        "cmd.memory",
        "cmd.prompt",
        "delegate",
    }
)

_PERM_ALIASES: dict[str, str] = {
    "bot": "bot",
    "bot.shared": "bot.shared",
    "bot.direct": "bot.direct",
    "tools": "cmd.tools",
    "config": "cmd.config",
    "llm": "cmd.llm",
    "trigger": "cmd.trigger",
    "memory": "cmd.memory",
    "prompt": "cmd.prompt",
    "delegate": "delegate",
}


async def _resolve_targets(
    target: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
) -> list[int] | str:
    """解析目标用户。返回 user_id 列表或错误消息字符串。"""
    if target in {"admins", "all"}:
        conversation = await context.message_service.platform_store.get_conversation(context.conversation_id)
        if conversation is None or conversation.kind == ConversationKind.DIRECT:
            return f"{target} 仅限非私聊会话使用"
        adapter = context.message_service.router.for_account(conversation.account_id, Capability.MEMBER_LIST)
        list_members = getattr(adapter, "list_conversation_members", None)
        if list_members is None:
            return "当前平台不支持成员目录"
        members = await list_members(conversation)
        if target == "admins":
            members = [member for member in members if member.get("role") in {"owner", "admin"}]
        return [int(member["user_id"]) for member in members]

    # 逗号分隔的内部用户 ID
    ids: list[int] = []
    for part in target.split(","):
        part = part.strip()
        if not part.isdigit():
            return f"无效的内部用户ID: {part}"
        ids.append(int(part))
    existing = {
        int(row["id"])
        for row in await pool.fetch("SELECT id FROM users WHERE id = ANY($1::bigint[])", ids)
    }
    missing = [user_id for user_id in ids if user_id not in existing]
    if missing:
        return f"内部用户不存在: {', '.join(map(str, missing))}"
    return list(dict.fromkeys(ids))


# ── .help ─────────────────────────────────────────────────


async def handle_help_command(
    text: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
) -> bool:
    """处理 .help 命令。根据用户权限动态显示可用命令。"""
    lines = [
        "可用命令:",
        "  .ping   — 连通测试",
        "  .help   — 显示此帮助",
        "  .bot    — 会话开关",
    ]
    is_m = permission.is_master(user_id)
    cmd_map = [
        ("cmd.tools", ".tools", "工具白名单"),
        ("cmd.llm", ".llm", "LLM provider 管理"),
        ("cmd.trigger", ".trigger", "触发器配置"),
        ("cmd.memory", ".memory", "记忆系统管理"),
        ("cmd.config", ".config", "运行时配置"),
        ("cmd.prompt", ".prompt", "System prompt 管理"),
    ]
    for perm, cmd, desc in cmd_map:
        if is_m or await permission.has_permission(pool, user_id, scope_type, scope_id, perm):
            lines.append(f"  {cmd:<10}— {desc}")
    if is_m or await permission.has_permission(pool, user_id, scope_type, scope_id, "delegate"):
        lines.append("  .perm    — 权限管理")
    await _reply(context, "\n".join(lines))
    return True


# ── .bot ──────────────────────────────────────────────────


async def handle_bot_command(
    text: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
) -> bool:
    """处理 .bot 命令。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        enabled = await permission.is_scope_enabled(pool, scope_type, scope_id)
        reply = f"当前会话: {'已启用' if enabled else '已禁用'}"
    elif sub == "on":
        await permission.set_scope_enabled(pool, scope_type, scope_id, True)
        reply = "会话已启用"
    elif sub == "off":
        await permission.set_scope_enabled(pool, scope_type, scope_id, False)
        reply = "会话已禁用"
    else:
        reply = "用法: .bot [on|off]"

    await _reply(context, reply)
    return True


# ── .tools ────────────────────────────────────────────────


async def handle_tools_command(
    text: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
    scope_type: str,
    scope_id: int,
    all_tool_names: list[str],
) -> bool:
    """处理 .tools 命令。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "":
        wl = await permission.get_tool_whitelist(pool, scope_type, scope_id)
        if wl is None:
            reply = "工具白名单: 未启用（全部工具可用）"
        else:
            reply = f"工具白名单 ({len(wl)}):\n" + "\n".join(f"  {t}" for t in sorted(wl))
    elif sub == "add":
        if len(parts) < 3:
            reply = "用法: .tools add <tool1,tool2,...>"
        else:
            names = [n.strip() for n in parts[2].split(",") if n.strip()]
            invalid = [n for n in names if n not in all_tool_names]
            if invalid:
                reply = f"未知工具: {', '.join(invalid)}\n用 .tools list-all 查看可用工具"
            else:
                added = await permission.add_tools(pool, scope_type, scope_id, names)
                reply = f"已添加 {added} 个工具到白名单"
    elif sub == "remove":
        if len(parts) < 3:
            reply = "用法: .tools remove <tool1,tool2,...>"
        else:
            names = [n.strip() for n in parts[2].split(",") if n.strip()]
            removed = await permission.remove_tools(pool, scope_type, scope_id, names)
            reply = f"已移除 {removed} 个工具"
    elif sub == "reset":
        await permission.reset_tools(pool, scope_type, scope_id)
        reply = "工具白名单已重置（全部工具可用）"
    elif sub == "list-all":
        reply = f"可用工具 ({len(all_tool_names)}):\n" + "\n".join(f"  {t}" for t in sorted(all_tool_names))
    else:
        reply = (
            "用法:\n"
            "  .tools                    — 查看白名单\n"
            "  .tools add <t1,t2>        — 添加工具\n"
            "  .tools remove <t1,t2>     — 移除工具\n"
            "  .tools reset              — 重置（全部可用）\n"
            "  .tools list-all           — 列出所有工具"
        )

    await _reply(context, reply)
    return True


# ── .perm ─────────────────────────────────────────────────


async def _can_delegate_perm(
    perm_name: str,
    user_id: int,
    scope_type: str,
    scope_id: int,
    pool: asyncpg.Pool,
    *,
    has_perm_fn: Any,
) -> bool:
    """检查用户是否有权分发指定权限。

    规则：
    - bot + delegate → 可分发 bot 和 bot.shared
    - bot.shared + delegate → 可分发当前会话的 bot.shared
    - bot.direct → 仅 master（调用方已处理）
    - 其他 cmd.* → 拥有该权限即可分发
    """
    # 必须有 delegate 权限
    if not await has_perm_fn(pool, user_id, scope_type, scope_id, "delegate"):
        return False
    if perm_name in ("bot", "bot.shared"):
        # 有 bot 全局权限 → 可分发 bot 和 bot.shared
        if await has_perm_fn(pool, user_id, "global", 0, "bot"):
            return True
        # 有当前会话 bot.shared → 只能分发当前会话 bot.shared
        if perm_name == "bot.shared" and scope_type == "conversation":
            return await has_perm_fn(pool, user_id, scope_type, scope_id, "bot.shared")
        return False
    # 其他权限：拥有即可分发
    return await has_perm_fn(pool, user_id, scope_type, scope_id, perm_name)


async def handle_perm_command(
    text: str,
    *,
    context: CommandContext,
    pool: asyncpg.Pool,
    user_id: int,
    scope_type: str,
    scope_id: int,
) -> bool:
    """处理 .perm 命令。"""
    parts = text.split()
    sub = parts[1] if len(parts) > 1 else ""
    is_m = permission.is_master(user_id)

    if sub == "":
        enabled = await permission.is_scope_enabled(pool, scope_type, scope_id)
        grants = await permission.list_grants(pool, scope_type, scope_id)
        lines = [f"会话状态: {'启用' if enabled else '禁用'}"]
        if grants:
            from collections import defaultdict

            by_user: dict[int, list[str]] = defaultdict(list)
            for uid, perm in grants:
                by_user[uid].append(perm)
            for uid, perms in by_user.items():
                lines.append(f"  {uid}: {', '.join(perms)}")
        else:
            lines.append("  (无授权)")
        reply = "\n".join(lines)

    elif sub == "grant":
        if len(parts) < 4:
            reply = "用法: .perm grant <权限> <目标>"
        else:
            perm_alias = parts[2]
            perm_name = _PERM_ALIASES.get(perm_alias)
            if not perm_name:
                reply = f"未知权限: {perm_alias}\n可用: {', '.join(sorted(_PERM_ALIASES))}"
            elif perm_name == "delegate" and not is_m:
                reply = "仅 master 可授予 delegate 权限"
            elif perm_name == "bot.direct" and not is_m:
                reply = "仅 master 可授予 bot.direct 权限"
            elif not is_m and not await _can_delegate_perm(
                perm_name,
                user_id,
                scope_type,
                scope_id,
                pool,
                has_perm_fn=permission.has_permission,
            ):
                reply = f"你没有权限授予 {perm_alias}"
            else:
                targets = await _resolve_targets(parts[3], context=context, pool=pool)
                if isinstance(targets, str):
                    reply = targets
                elif perm_name == "bot.direct":
                    count = await permission.batch_grant(
                        pool,
                        targets,
                        "global",
                        0,
                        "bot.direct",
                        user_id,
                    )
                    reply = f"已授予 {count} 人私聊启用权限"
                elif perm_name == "bot.shared":
                    if context.conversation_kind == ConversationKind.DIRECT:
                        reply = "bot.shared 仅限非私聊会话中使用"
                    else:
                        count = await permission.batch_grant(
                            pool,
                            targets,
                            scope_type,
                            scope_id,
                            "bot.shared",
                            user_id,
                        )
                        reply = f"已授予 {count} 人当前会话 bot.shared 权限"
                elif perm_name == "bot":
                    # 全局 bot 权限
                    count = await permission.batch_grant(
                        pool,
                        targets,
                        "global",
                        0,
                        "bot",
                        user_id,
                    )
                    reply = f"已授予 {count} 人 bot 权限"
                else:
                    count = await permission.batch_grant(
                        pool,
                        targets,
                        scope_type,
                        scope_id,
                        perm_name,
                        user_id,
                    )
                    reply = f"已授予 {count} 人 {perm_alias} 权限"

    elif sub == "revoke":
        if len(parts) < 4:
            reply = "用法: .perm revoke <权限> <目标>"
        else:
            perm_alias = parts[2]
            perm_name = _PERM_ALIASES.get(perm_alias)
            if not perm_name:
                reply = f"未知权限: {perm_alias}\n可用: {', '.join(sorted(_PERM_ALIASES))}"
            elif perm_name == "delegate" and not is_m:
                reply = "仅 master 可撤销 delegate 权限"
            elif perm_name == "bot.direct" and not is_m:
                reply = "仅 master 可撤销 bot.direct 权限"
            elif not is_m and not await _can_delegate_perm(
                perm_name,
                user_id,
                scope_type,
                scope_id,
                pool,
                has_perm_fn=permission.has_permission,
            ):
                reply = f"你没有权限撤销 {perm_alias}"
            else:
                targets = await _resolve_targets(parts[3], context=context, pool=pool)
                if isinstance(targets, str):
                    reply = targets
                elif perm_name == "bot.direct":
                    count = await permission.batch_revoke(
                        pool,
                        targets,
                        "global",
                        0,
                        "bot.direct",
                    )
                    reply = f"已撤销 {count} 人的私聊启用权限"
                elif perm_name == "bot.shared":
                    if context.conversation_kind == ConversationKind.DIRECT:
                        reply = "bot.shared 仅限非私聊会话中使用"
                    else:
                        count = await permission.batch_revoke(
                            pool,
                            targets,
                            scope_type,
                            scope_id,
                            "bot.shared",
                        )
                        reply = f"已撤销 {count} 人当前会话 bot.shared 权限"
                elif perm_name == "bot":
                    count = await permission.batch_revoke(
                        pool,
                        targets,
                        "global",
                        0,
                        "bot",
                    )
                    reply = f"已撤销 {count} 人 bot 权限"
                else:
                    count = await permission.batch_revoke(
                        pool,
                        targets,
                        scope_type,
                        scope_id,
                        perm_name,
                    )
                    reply = f"已撤销 {count} 人的 {perm_alias} 权限"

    elif sub == "list":
        target_user = parts[2] if len(parts) > 2 else None
        if target_user:
            if not target_user.isdigit():
                reply = "用法: .perm list [内部用户ID]"
            else:
                uid = int(target_user)
                perms = await permission.list_user_grants(pool, uid, scope_type, scope_id)
                reply = f"{uid} 的权限: {', '.join(perms)}" if perms else f"{uid} 无权限"
        else:
            grants = await permission.list_grants(pool, scope_type, scope_id)
            if not grants:
                reply = "当前会话无授权"
            else:
                from collections import defaultdict

                by_user: dict[int, list[str]] = defaultdict(list)
                for uid, perm in grants:
                    by_user[uid].append(perm)
                lines = [f"{uid}: {', '.join(perms)}" for uid, perms in by_user.items()]
                reply = "\n".join(lines)
    else:
        reply = (
            "用法:\n"
            "  .perm                          — 权限概览\n"
            "  .perm grant <权限> <目标>       — 授予权限\n"
            "  .perm revoke <权限> <目标>      — 撤销权限\n"
            "  .perm list [内部用户ID]         — 列出权限\n"
            f"权限: {', '.join(sorted(_PERM_ALIASES))}\n"
            "目标: 内部用户ID | ID1,ID2 | admins | all"
        )

    await _reply(context, reply)
    return True


async def _reply(context: CommandContext, text: str) -> None:
    """向来源会话发送回复。"""
    await context.message_service.send_message(
        SendMessageRequest(conversation_id=context.conversation_id, text=text)
    )
