# ============================================================
# Sophos — 生产镜像（多阶段构建）
# ============================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml uv.lock* ./

# 安装依赖（利用 Docker 层缓存，代码变了不用重装依赖）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 复制源码并安装项目本身
COPY sophos/ sophos/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- 运行阶段 ----
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "sophos.main"]
