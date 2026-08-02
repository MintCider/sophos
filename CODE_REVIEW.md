# 代码审查待处理问题

本文件只保留尚未解决的问题。问题修复并通过验证后，删除对应条目；历史由 Git 保存。

## 严重

- 默认容器部署将未鉴权的管理 API 发布到全部网络接口；管理 API 可读取明文 Provider 密钥和 TRACE 日志。
- 消息唯一索引按会话约束，但回复查询、文本更新和富化更新只按 `message_id`，存在跨会话误读、误更新风险。
- Embedding 迁移的切列与配置更新没有事务保护，失败或并发写入可能造成向量数据丢失和半迁移状态。

## 中等

- 私聊 fallback 回复和 `.ping` 使用 bot 自身 QQ 作为消息存储的会话 `user_id`，后续私聊上下文无法读取。
- WebSocket 事件处理任务没有并发上限、顺序控制和关闭阶段的任务回收。
- Trigger 评估任务创建后没有同步切换状态，并发消息可能重复创建评估循环并绕过 QPS 控制。
- Provider 超时热更新写入 `_timeout`，聊天、视觉和触发 Provider 实际读取 `_request_timeout`。
- 日志 SSE 不处理 RotatingFileHandler 更换文件，首次轮转后会停留在旧文件 EOF。
- 命令回复和权限错误回复没有统一经过机器人消息持久化流程，消息可能缺失或被标记为 `co_account`。
- `memories.embedding` 使用无固定维度的 `vector`，普通 HNSW 索引创建失败后被忽略，向量搜索退化为全表扫描。
- 长时间图像生成任务持有创建时的 OneBot WebSocket API；断线重连或服务关闭后无法切换到新连接。

## 低效与耦合

- 日志 tail 接口同步读取完整文件后再截断，会阻塞事件循环。
- 权限 scope 列表逐项查询工具白名单，存在 N+1 查询。
- 前端 API 超时为 10 秒，Provider 模型刷新后端允许等待 15 秒，可能出现前端报错但后端成功。
- `pipeline.py`、`commands.py`、ProviderManager 和 API 路由直接访问其他模块私有状态，且多个发送、Provider slot 切换流程重复实现，已造成持久化和热更新行为不一致。

## TODO

- 增加管理员手动触发的数据库物理空间整理功能；需要处理 `VACUUM FULL` 的独占锁、额外临时磁盘需求、二次确认和运行状态展示。

## 测试缺口

- 缺少真实 PostgreSQL schema、消息 ID 冲突、消息并发顺序、工具执行权限、日志轮转、Provider 热更新和后台任务生命周期测试。
