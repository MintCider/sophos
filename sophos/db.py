"""数据库连接管理与 schema 初始化。

职责：
  1. 管理 asyncpg 连接池生命周期 (init / close)
  2. 应用启动时幂等建表 (CREATE TABLE IF NOT EXISTS)

不引入迁移框架 (alembic 等)，当前阶段用 DDL 脚本直接管理。
表结构变更时手动写 ALTER 或重建。
"""

import logging

import asyncpg

from sophos.config import settings

logger = logging.getLogger(__name__)

# 模块级连接池，由 init_db() 创建，close_db() 关闭
_pool: asyncpg.Pool | None = None


async def init_db() -> asyncpg.Pool:
    """创建连接池并初始化 schema。

    应在应用启动时调用一次。
    """
    global _pool  # noqa: PLW0603
    logger.info("Connecting to database...")
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
    )
    logger.info("Database connected, initializing schema...")
    await _init_schema(_pool)
    logger.info("Schema ready.")
    return _pool


async def close_db() -> None:
    """关闭连接池。应在应用退出时调用。"""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")


def get_pool() -> asyncpg.Pool:
    """获取当前连接池。必须在 init_db() 之后调用。"""
    if _pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _pool


# ── Schema DDL ──────────────────────────────────────────────

_CREATE_MESSAGES_TABLE = """\
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL       PRIMARY KEY,

    -- OneBot 协议字段
    message_id      BIGINT          NOT NULL,
    message_type    VARCHAR(16)     NOT NULL,

    -- 会话定位
    group_id        BIGINT,
    user_id         BIGINT          NOT NULL,

    -- 发送者信息（冗余快照，昵称/名片会变）
    nickname        VARCHAR(128),
    card            VARCHAR(128),

    -- 消息来源：'user' | 'sophos' | 'co_account'
    --   user       — 群友/对方发的正常消息
    --   sophos     — Sophos 通过 send_msg 发出的
    --   co_account — 同账号其他来源（其他 bot、手动发的等）
    source          VARCHAR(16)     NOT NULL DEFAULT 'user',

    -- 消息内容
    raw_message     JSONB           NOT NULL,
    plain_text      TEXT,

    -- 时间
    timestamp       TIMESTAMPTZ     NOT NULL,

    -- 预留扩展
    extra           JSONB
);
"""

_CREATE_LLM_PROVIDERS_TABLE = """\
CREATE TABLE IF NOT EXISTS llm_providers (
    id              SERIAL          PRIMARY KEY,
    alias           TEXT            UNIQUE NOT NULL,
    base_url        TEXT            NOT NULL,
    api_key         TEXT            NOT NULL,
    models          JSONB           DEFAULT '[]'::jsonb,
    extra_body      JSONB,
    stream          BOOLEAN         DEFAULT true,
    request_timeout INTEGER         DEFAULT 60,
    created_at      TIMESTAMPTZ     DEFAULT now()
);
"""

_CREATE_LLM_ACTIVE_TABLE = """\
CREATE TABLE IF NOT EXISTS llm_active (
    key             TEXT            PRIMARY KEY DEFAULT 'default',
    provider_id     INTEGER         REFERENCES llm_providers(id) ON DELETE SET NULL,
    model           TEXT            NOT NULL,
    api_type        TEXT            DEFAULT 'openai',
    updated_at      TIMESTAMPTZ     DEFAULT now()
);
"""

_CREATE_TRIGGER_CONFIG_TABLE = """\
CREATE TABLE IF NOT EXISTS trigger_config (
    id          INTEGER     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    base_rate   REAL        NOT NULL DEFAULT 0.05,
    at_always   BOOLEAN     NOT NULL DEFAULT true,
    keywords    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
"""

_SEED_TRIGGER_CONFIG = """\
INSERT INTO trigger_config (id) VALUES (1) ON CONFLICT DO NOTHING;
"""

_CREATE_IMAGE_CACHE_TABLE = """\
CREATE TABLE IF NOT EXISTS image_cache (
    hash                TEXT            PRIMARY KEY,
    description         TEXT            NOT NULL DEFAULT '',
    hit_count           INTEGER         NOT NULL DEFAULT 0,
    correction_hint     TEXT,
    pending_correction  BOOLEAN         NOT NULL DEFAULT false,
    first_seen          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_seen           TIMESTAMPTZ     NOT NULL DEFAULT now()
);
"""

# ── 记忆系统 ──────────────────────────────────────────────

_CREATE_EMBEDDING_CONFIG_TABLE = """\
CREATE TABLE IF NOT EXISTS embedding_config (
    id                  INTEGER     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    dimension           INTEGER     NOT NULL DEFAULT 0,
    endpoint            VARCHAR(128) DEFAULT '/embeddings',
    extra_body          JSONB,
    pending_provider_id INTEGER     REFERENCES llm_providers(id) ON DELETE SET NULL,
    pending_model       TEXT,
    pending_dimension   INTEGER,
    migration_status    TEXT        DEFAULT 'none',
    updated_at          TIMESTAMPTZ DEFAULT now()
);
"""

_SEED_EMBEDDING_CONFIG = """\
INSERT INTO embedding_config (id) VALUES (1) ON CONFLICT DO NOTHING;
"""

_CREATE_INDEXES = [
    # 按群聊查最近消息（最常用）
    """\
    CREATE INDEX IF NOT EXISTS idx_messages_group_ts
    ON messages (group_id, timestamp DESC)
    WHERE group_id IS NOT NULL;
    """,
    # 按私聊查最近消息
    """\
    CREATE INDEX IF NOT EXISTS idx_messages_private_ts
    ON messages (user_id, timestamp DESC)
    WHERE group_id IS NULL;
    """,
    # 按 message_id 查找（CQ:reply 展开用）
    """\
    CREATE INDEX IF NOT EXISTS idx_messages_msg_id
    ON messages (message_id);
    """,
    # 群聊内 message_id 唯一（用于 ON CONFLICT 去重）
    """\
    CREATE UNIQUE INDEX IF NOT EXISTS uidx_messages_group
    ON messages (group_id, message_id)
    WHERE group_id IS NOT NULL;
    """,
    # 私聊内 message_id 唯一（用于 ON CONFLICT 去重）
    """\
    CREATE UNIQUE INDEX IF NOT EXISTS uidx_messages_private
    ON messages (user_id, message_id)
    WHERE group_id IS NULL;
    """,
]


async def _init_schema(pool: asyncpg.Pool) -> None:
    """幂等创建所有表和索引。"""
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_MESSAGES_TABLE)
        await conn.execute(_CREATE_LLM_PROVIDERS_TABLE)
        await conn.execute(_CREATE_LLM_ACTIVE_TABLE)
        await conn.execute(_CREATE_IMAGE_CACHE_TABLE)
        await conn.execute(_CREATE_TRIGGER_CONFIG_TABLE)
        await conn.execute(_SEED_TRIGGER_CONFIG)
        await conn.execute(_CREATE_EMBEDDING_CONFIG_TABLE)
        await conn.execute(_SEED_EMBEDDING_CONFIG)
        # llm_active 新列（向后兼容已有 DB）
        try:
            await conn.execute(
                "ALTER TABLE llm_active ADD COLUMN api_type TEXT DEFAULT 'openai';"
            )
        except asyncpg.DuplicateColumnError:
            pass
        # embedding_config 新列（向后兼容已有 DB）
        try:
            await conn.execute(
                "ALTER TABLE embedding_config ADD COLUMN endpoint VARCHAR(128) DEFAULT '/embeddings';"
            )
        except asyncpg.DuplicateColumnError:
            pass
        try:
            await conn.execute(
                "ALTER TABLE embedding_config ADD COLUMN extra_body JSONB;"
            )
        except asyncpg.DuplicateColumnError:
            pass
        for ddl in _CREATE_INDEXES:
            await conn.execute(ddl)
