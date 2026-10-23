# 首页统计

首页支持今天 / 7 天 / 30 天、会话筛选，按 `TIMEZONE` 的本地自然日聚合，每 30 秒刷新。

- 收到消息：保留在 `messages` 中的 `source=user` 记录；Bot 发言：`source=sophos` 的实际发送记录。消息清理后，历史消息统计也会相应减少。
- 模型调用：actor、collector、trigger、vision、image、embedding 的每次 API 请求，重试分别计数。Embedding 批量请求计一次；multimodal 逐条请求逐次计数，维度检测和重建也计入。未返回用量的调用记录为未知，不补估算值。
- 输入包含缓存读取 / 写入；输出包含已报告的推理 token，推理和缓存不重复加入总量。接口报告总量时保留其总量。费用未配置单价，暂不估算。
- Tavily：搜索 / 网页提取按 HTTP 请求分别计数（一次提取多个 URL 仍计一次）。失败及部分提取失败可在详情查看。启用 `include_usage`，只记录返回的 credits；未返回与明确返回 0 区分保存。不与 Token 相加。
- Trigger：简易命中、LLM 评估及等待超时分别留记录。评估结果与实际发送、限流、锁占用、合并、失败分开记录；重启时将未完成事件标记为中断。
- 异常：模型请求、发送、工作流及流水线失败。首页合并同类异常，详情逐条查看；不保存上游错误响应体和凭据。
- 模型 / 工具调用、Trigger 和异常记录保留 90 天。旧版未保存的历史用量和 Trigger 数据不会回填。

## 部署已有 v4 数据库

新空库直接初始化 v5。已有库必须先备份，停止机器人，用新代码显式迁移，再启动；启动时不自动迁移。

```bash
python -m sophos.migrate_resource_usage
```

服务器 Compose 部署，在项目目录执行（先完成数据库备份）：

```bash
docker compose -f compose.server.yml build sophos
docker compose -f compose.server.yml stop sophos
docker compose -f compose.server.yml run --rm --no-deps sophos python -m sophos.migrate_resource_usage
docker compose -f compose.server.yml up -d sophos
```

迁移以事务新增 `tool_usage` 及索引，将 schema 版本从 4 更新到 5，保留原有模型用量及其它数据。仍在 v3 的数据库需先执行 `python -m sophos.migrate_stats` 升到 v4，再执行上述迁移。

## 测试

```bash
python -m unittest discover -s tests
```

数据库集成测试使用**专用空测试数据库**（会清理其中的测试会话及统计记录），不要指向生产库：

```bash
STATS_TEST_DATABASE_URL=postgresql://... python -m unittest discover -s tests -p test_stats.py
```
