# Sophos

Sophos 是一个使用 PostgreSQL/pgvector 持久化数据、通过外部 IM adapter 接收消息的聊天机器人。

平台中间层的数据约束、工具能力模型和后续适配顺序见 [平台中间层架构与后续计划](docs/platform-abstraction-plan.md)。

## 开发

复制本地配置：

```bash
cp .env.example .env
```

日常开发只在容器中启动 PostgreSQL，Python 后端和 Vue 前端在宿主机运行：

```bash
docker compose up -d postgres
uv run python -m sophos.main
pnpm dev
```

Vite 开发服务器监听 `5173` 并将 `/api` 代理到后端的 `8080` 端口。

## Prompt

所有 prompt 存储在数据库中，在 WebUI 的「Prompt」页编辑，保存即生效并保留完整版本历史。

模板使用 `{{name}}` 引用变量，使用 `{{prompt.<key>}}` 引入另一条 prompt。系统提示词可用的变量：

`{{nickname}}`、`{{current_time}}`、`{{timezone}}`、`{{self_user_id}}`、`{{current_user_id}}`、`{{current_user_roles}}`、`{{conversation_id}}`、`{{conversation_kind}}`、`{{master_users}}`、`{{identity_context}}`、`{{message_format}}`、`{{runtime_context}}`。

单花括号原样保留。输出格式约束类 prompt 默认锁定，需要在 WebUI 中显式解锁后编辑。

## 容器集成验证

完整镜像同时包含 Python 后端和构建后的 Vue WebUI。生产运行时由 Aiohttp 在 `8080` 端口提供 API 和前端静态资源，不运行 Vite 进程。

```bash
docker compose up -d --build
```

仓库中的 Compose 只管理 Sophos 和 PostgreSQL。NapCat 等 IM adapter 由部署环境独立管理，并通过 `CONTAINER_ONEBOT_WS_URL` 配置连接地址。

## 镜像部署

发布镜像后，可以通过 `SOPHOS_VERSION` 选择版本：

```bash
SOPHOS_VERSION=v1.0.0 docker compose up -d --pull always
```

实际的 `.env`、数据库数据和日志不会打包进镜像或提交到 Git。
