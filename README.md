# Sophos

Sophos 是一个使用 PostgreSQL/pgvector 持久化数据、通过外部 IM adapter 接收消息的聊天机器人。

## 开发

复制本地配置和 system prompt：

```bash
cp .env.example .env
cp system_prompt.example.md system_prompt.md
```

日常开发只在容器中启动 PostgreSQL，Python 后端和 Vue 前端在宿主机运行：

```bash
docker compose up -d postgres
uv run python -m sophos.main
pnpm dev
```

Vite 开发服务器监听 `5173` 并将 `/api` 代理到后端的 `8080` 端口。

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

实际的 `.env`、`system_prompt.md`、数据库数据和日志不会打包进镜像或提交到 Git。
