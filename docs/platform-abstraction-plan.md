# 平台中间层架构与后续计划

本文记录已经确认并落地的平台抽象边界，以及新增协议或平台时必须保持的约束。Milky 适配目前仅列为 TODO，不在本阶段实现。

## 当前状态

- OneBot 11 是当前唯一实现的 adapter；NapCat、Lagrange 等协议端差异只允许出现在该 adapter 内。
- Milky 是待适配的接口标准，不作为 OneBot 协议端处理。后续应单独实现 `PlatformAdapter`。
- Sophos 核心、Agent 工具、权限、记忆和策略只使用内部 ID，不解释 QQ、Discord、Telegram、Slack、KOOK 等平台原始 ID。
- 生产数据库已经显式迁移到 schema v2；运行时代码只负责为空数据库创建当前 schema，拒绝隐式迁移旧库。

## ID 与数据约束

### 用户与身份

- `users.id` 是 Sophos 统一用户 ID。
- `user_identities` 保存平台身份，唯一键为 `(platform, identity_namespace, external_user_id)`。
- “认证为同一用户”表示把多个已验证的平台身份关联到同一个 `users.id`，不是把外部 ID 当作统一 ID。
- 首次观察到的平台身份默认创建独立用户。合并必须经过显式认证，记录认证方法、操作者和审计事件；已有角色、权限、档案或策略的身份不能被静默移动。
- `platform_accounts` 表示机器人登录账号，`adapter_bindings` 表示该账号使用的接口实例。相同平台的多个账号或多个 adapter 不共享外部消息命名空间。

### 会话

- `conversations.id` 是内部会话 ID。
- 外部定位由 `(account_id, kind, parent_conversation_id, external_conversation_id)` 构成。
- `kind` 统一为 `direct`、`group`、`channel`、`thread`。Discord/KOOK/Slack 的频道与 thread、Telegram 的群组/频道/topic 应映射到这些类型，不在核心中增加平台专用 scope。

### 消息

- `messages.id` 是内部消息主键，也是上下文游标、回复关系和 Agent 工具使用的唯一消息 ID。
- adapter 返回的 message ID 只存入 `external_message_id`。协议定位符为 `(adapter_binding_id, conversation_id, external_message_id)`，由 partial unique index 约束。
- 回复关系存储为 `reply_to_message_id -> messages.id`。发送、撤回、查询、文本更新和富化更新一律先解析或直接使用内部消息 ID，不允许只按外部 message ID 更新。
- 标准内容存入平台中立的 `content` segment；完整事件保留在 `raw_payload`，仅供对应 adapter 富化或诊断。

## 查询索引与清理策略

会话上下文是消息大表的主要读取路径：

```sql
SELECT ...
FROM messages
WHERE conversation_id = $1
ORDER BY id DESC
LIMIT $2;
```

该路径使用 `idx_messages_conversation_id (conversation_id, id DESC)`。时间窗口使用 `(conversation_id, occurred_at DESC, id DESC)`；待富化、全局终态、跨会话背景、回复反向关系和 LRU 各自使用独立的普通或 partial index。外部定位符索引只承担 adapter 事件幂等和外部回复解析，不承担上下文游标职责。

消息容量清理按全局 LRU 选择候选，但每个 conversation 最近 `max_context_messages` 条始终受保护。保护下限优先于 2 GiB 逻辑容量目标：如果所有剩余消息都受保护，清理停止并记录实际受保护行数和大小。pending/streaming 消息也不会被清理。

## 权限、记忆、策略和 Master

- 权限 scope 统一为 `global:0` 或 `conversation:<internal conversation id>`。
- 用户授权、用户档案和触发策略使用内部 `users.id`；会话档案与记忆来源使用内部 `conversations.id`。
- 会话类型权限统一为 `bot.shared` 和 `bot.direct`，不再使用 `bot.group` / `bot.private` 表达平台形态。
- Master 是 `user_roles(role='master')` 中的内部用户角色，不读取环境变量或硬编码外部账号。

推荐在 `system_prompt.md` 中放置 `{runtime_context}`。每次 Agent 调用会动态渲染为以下结构：

```text
当前时间：<本地时间> (<时区>)
[Sophos 内部身份]
你的统一用户ID：<self_user_id>
当前用户：user_id=<current_user_id>，roles=[<roles>]
当前会话：conversation_id=<conversation_id>，kind=<kind>
Master（主人）用户：
- user_id=<master_user_id>, display_name=<display_name>
工具参数中的 user_id、conversation_id、message_id 均为 Sophos 内部 ID；不要使用平台原始 ID 调用工具。
消息格式：<当前消息格式说明>
```

也可以单独放置 `{master_users}`、`{identity_context}` 等已声明变量。渲染器只替换已知变量，保留 prompt 中其他花括号。旧 prompt 没有变量时会在末尾追加完整运行时块。

## Agent 工具模型

通用工具不按平台命名，而按语义和能力声明：

- 消息：`send_message`、`recall_message`、`query_messages`。
- 目录：`get_user`、`get_conversation`、`list_conversation_members`。
- 管理：`moderate_member`。
- 记忆和平台无关的输入工具保持独立。

工具参数只接受内部 `message_id`、`conversation_id` 和 `user_id`。工具注册同时检查：

1. adapter 的 capability，例如 `message.send`、`message.reply`、`message.mention`、`message.attachment`、`member.list`、`member.moderate`；
2. 工具允许的 `conversation_kinds`；
3. Sophos 权限系统的 scope 与用户授权。

因此未来 adapter 可以按真实能力暴露工具：Discord/KOOK 可提供频道、thread、附件和成员管理；Telegram 可提供 direct/group/channel 及受限管理能力；Slack 可提供 channel/thread 和成员目录。缺失能力时工具不会向 Agent 暴露，或在跨会话执行时由 router 明确拒绝，不需要在工具内判断平台名称。

## Adapter 边界

新增 adapter 必须实现以下流程：

1. 确保平台账号和 binding，声明稳定且不冲突的 `external_id_namespace`。
2. 将入站身份、会话、消息、mention、reply 和附件标准化为平台中立模型。
3. 将内部发送请求转换为平台调用，并返回外部消息 ID。
4. 根据实际支持情况声明 capability，不声明无法稳定实现的能力。
5. 将平台特有富化限制在 adapter 内；核心和通用工具不得导入平台 API。
6. 通过同一组 adapter contract、消息定位符冲突、权限和工具可见性测试。

当前允许保留的 OneBot 耦合点仅包括连接配置、WebSocket transport 的装配和 `OneBot11Adapter` 本身。Milky adapter 应复用 `MessageService`、`PlatformStore`、`AdapterRouter` 和通用工具，不复制 pipeline。

## 后续实施顺序

1. 增加真实 PostgreSQL 集成测试，覆盖 schema v2、同一外部 message ID 在不同 conversation/binding 中共存、并发幂等和查询计划。
2. 为 `PlatformAdapter` 建立可复用 contract test，并补足附件发送/接收的端到端实现。
3. 实现统一用户认证的管理员工作流和 UI；认证协议必须先确认威胁模型，不允许仅凭用户自报外部 ID 合并。
4. 调研并实现 Milky adapter，重点验证事件 ID 命名空间、direct/group 会话定位、reply/recall、资源与富文本映射。
5. 选择首个非 QQ 平台 adapter 验证抽象。优先选择 thread 与附件语义较完整的平台，以验证 `channel/thread`、capability 和跨平台统一用户。
