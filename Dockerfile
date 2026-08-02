# Build the WebUI once; production does not run a Vite process.
FROM node:22-bookworm-slim AS web-builder

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

RUN corepack enable && corepack prepare pnpm@11.8.0 --activate

WORKDIR /build
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY web/package.json web/package.json
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY web/ web/
RUN pnpm --filter @sophos/web build


FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS python-builder

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY sophos/ sophos/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY --from=python-builder /app/sophos /app/sophos
COPY --from=web-builder /build/web/dist /app/web/dist

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "sophos.main"]
