# Agent 工作流与 Provider IR

Sophos 的消息处理不再由固定的 tool loop 表达，而是由版本化工作流图、追加式 transcript 和 provider 编译器共同执行。当前内置图是 `collector -> actor -> terminal`，图结构本身是普通 JSON 数据，可由未来 WebUI 的节点卡片和端口连线直接生成。

## 工作流图

`WorkflowDefinition` 包含稳定的 `workflow_id`、`revision`、入口节点、节点和边。节点通过具名输入/输出端口连接；模型触发的输出端口会被编译成严格控制工具，宿主触发的端口用于表达 `send_message` 成功等确定事件。边允许形成环，并用 `max_traversals` 限制单条边的循环次数；运行器另有全局 `max_steps` 上限。

本版本可激活 `llm` 和 `terminal` 节点，图节点可用 `default`、`collector` 模型槽。IR 已预留 `router`、`policy`、`human_approval`、`subworkflow`，但在对应 executor 实现前不能激活，API 会明确拒绝。

节点的 `system_prompt` 是模板，通常写成 `{{prompt.persona.system}}` 引用 Prompt 注册表，也可以手写并混用变量；`{{context_blocks}}` 放置档案、联想记忆与最近动态，未引用时自动追加在末尾。`model` 可覆盖 `max_tokens`、`temperature` 与 `max_tool_rounds`。`layout` 保存编辑器中的节点坐标，不影响执行。

图片识别与触发判断在图之前运行，编辑器把它们显示为前置阶段卡片，设置项保存在 `bot_config`。

工作流配置接口：

- `GET /api/workflows/active`：读取当前实际使用的图，并标明 `preset` 或 `configured`。
- `PUT /api/workflows/active`：提交 `{ "workflow": <WorkflowDefinition> }`，完整校验后热激活。
- `DELETE /api/workflows/active`：删除覆盖配置，恢复内置图。
- `GET /api/workflows/catalog`：模型槽绑定、前置阶段设置、可用变量与工具。
- `PUT /api/workflows/settings`、`PUT /api/workflows/slots/{slot}`：修改前置阶段设置、热切换模型槽。
- `GET /api/workflows/runs/stream`：SSE 推送运行开始、节点进入、边、工具调用与结束事件，用于实时高亮。

配置保存在现有 `bot_config.agent_workflow` 中，不需要新增数据库表或迁移。每次新消息运行读取当时的完整 revision；正在运行的请求继续持有其不可变定义。

## Collector / Actor 语义

collector 使用独立的 `collector` 槽，与 `default` 具有相同的 provider 生命周期、常规生成上限和热切换能力，只暴露 `category=input` 的读取工具和 `complete_collection`。工具调用和结果本身就是 handover 内容。actor 使用 `default` 槽，读取聊天上下文和经过隔离的节点历史，只暴露 `category=output` 工具和 `request_more_information`。actor 缺少信息时返回 collector；`send_message` 或 `finish_without_reply` 成功后宿主沿 `completed` 端口结束。未显式调用工具时，actor 的普通文本仍自动发送。

collector 与 default 都是工作流需要显式配置的一级模型槽；collector 未配置时不会借用 default。`trigger` 槽仅供触发判定使用。`web_search` 当前作为读取工具只在 collector 开放；未来可以通过节点 `ToolPolicy` 调整，无需改运行器。

## Transcript 与跨 Provider 传递

`Transcript` 是只追加事件流，保存 message、provider response、tool call、tool result 和图转换。portable canonical payload 是跨 API 的真相来源；`NativeEnvelope` 同时保留 provider、API surface、model 和原生元数据。Gemini 的 thought signature 等必须 round-trip 的字段仍保留在 canonical message/tool call 扩展字段中。

每次 provider 请求按节点生成历史副本，原始消息和 transcript 不被交接过滤修改：

- 初始聊天记录、后续新消息保持共享；本节点自己的 assistant 文本、推理字段和原生签名保留，返回该节点继续执行时也能恢复。
- 来自其它节点的 assistant 普通文本和推理不进入请求。只传递标准工具调用字段（id、type、function.name、function.arguments）及其配对结果，移除 provider 专有字段和签名。即使两个节点使用同一 provider，也按节点隔离。
- 无参数的工作流转换（如 `complete_collection`）及其结果不跨节点传递；带参数定义的转换保留标准调用和结果，因此 `request_more_information.requirements` 仍能交给 collector。同一轮混合读取和交接调用时，仅过滤控制记录，读取结果和失败信息保留。
- 不对工具结果重新总结，不注入额外身份说明；节点 prompt 和指令不因交接而改写。原有节点指令仍只注入本次 provider 请求的 system 视图。

Gemini 编译器沿用已有的无签名工具历史兼容路径；外来调用不会借用其它节点的 thought signature。完整 provider 响应仍可在原始 transcript 中审计。

## Provider 编译与 strict tools

内部工具只维护一份 canonical JSON Schema，API 边界分别编译：

- OpenAI 及 OpenAI-compatible：每个 function 下发 `strict: true`；object 递归设置 `additionalProperties: false`；全部 property 进入 `required`，canonical 可选参数在边界变为 nullable。执行前删除仅由边界编译引入的 null 占位。
- Anthropic：工具使用原生顶层 `strict: true`，递归关闭额外 object properties，同时保留 canonical optionality。
- Gemini：原生 API 没有 `strict` 字段；自动工具选择使用 `VALIDATED`，强制工具调用使用 `ANY`，禁用工具使用 `NONE`。

Provider 返回的 cache read/write token 与原生 usage 会进入统一 `UsageInfo`，响应 ID、model 等进入 `native_metadata`。

## Prompt cache

工作流节点声明 provider-neutral `CachePolicy`，编译为 `CachePlan`：

- OpenAI-compatible 使用稳定 `prompt_cache_key`；
- Anthropic 使用 ephemeral `cache_control`，可请求 1 小时 TTL；
- Gemini GenerateContent 依赖其隐式缓存，并从 `cachedContentTokenCount` 读取命中量。显式 CachedContent 需要独立生命周期管理，暂不伪装成通用 key。

system prompt 与节点指令保持在请求前缀，动态消息和工具历史排在其后。不同节点使用不同 cache key，避免 collector/actor 的工具集与指令互相污染缓存。

## Provider 请求策略

`llm_providers.request_policy` 保存 provider 级的 JSON 能力声明，不绑定某一个模型槽。OpenAI-compatible 编译器目前识别：

- `allowed_body_parameters`：请求体允许字段白名单；必须包含 `model` 和 `messages`。
- `accumulated_message_fields`：流式 delta 中需拼接并回放的 assistant 字段，例如 DeepSeek 的 `reasoning_content`。
- `requires_assistant_content_for_tool_calls`：工具调用历史中是否必须同时携带 assistant `content`。
- `strict_optional_mode`：`nullable` 用 `null` 保留可选语义；`required` 用于不接受 `null` 类型的 strict JSON Schema 实现。

未声明白名单时保持开放的 OpenAI-compatible 行为。已声明白名单时，编译器在最后一步过滤请求体，包括 `extra_body` 中的字段；响应的 provider-native 字段仍保留在共享消息历史和 transcript 中。
