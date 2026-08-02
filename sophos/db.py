"""数据库连接管理与 schema 初始化。

职责：
  1. 管理 asyncpg 连接池生命周期 (init / close)
  2. 为空数据库创建当前 canonical schema
  3. 拒绝在未知/旧 schema 上自动运行，数据迁移必须显式执行
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

SCHEMA_VERSION = 2

_CREATE_SCHEMA_META_TABLE = """\
CREATE TABLE schema_meta (
    singleton       BOOLEAN         PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    version         INTEGER         NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);
"""

_CREATE_USERS_TABLE = """\
CREATE TABLE users (
    id              BIGSERIAL       PRIMARY KEY,
    display_name    TEXT            NOT NULL DEFAULT '',
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);
"""

_CREATE_USER_IDENTITIES_TABLE = """\
CREATE TABLE user_identities (
    id                  BIGSERIAL       PRIMARY KEY,
    user_id             BIGINT          NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    platform            VARCHAR(32)     NOT NULL,
    identity_namespace  TEXT            NOT NULL DEFAULT 'global',
    external_user_id    TEXT            NOT NULL,
    display_name        TEXT            NOT NULL DEFAULT '',
    metadata            JSONB           NOT NULL DEFAULT '{}'::jsonb,
    verification_method TEXT,
    verified_at         TIMESTAMPTZ,
    verified_by_user_id BIGINT          REFERENCES users(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (platform, identity_namespace, external_user_id)
);
"""

_CREATE_USER_ROLES_TABLE = """\
CREATE TABLE user_roles (
    user_id         BIGINT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            VARCHAR(32)     NOT NULL,
    granted_by      BIGINT          REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role)
);
"""

_CREATE_IDENTITY_AUTH_AUDIT_TABLE = """\
CREATE TABLE identity_auth_audit (
    id                  BIGSERIAL       PRIMARY KEY,
    identity_id         BIGINT          NOT NULL,
    source_user_id      BIGINT          NOT NULL,
    target_user_id      BIGINT          NOT NULL,
    verified_by_user_id BIGINT          NOT NULL,
    verification_method TEXT            NOT NULL,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);
"""

_CREATE_PLATFORM_ACCOUNTS_TABLE = """\
CREATE TABLE platform_accounts (
    id                  BIGSERIAL       PRIMARY KEY,
    platform            VARCHAR(32)     NOT NULL,
    identity_namespace  TEXT            NOT NULL DEFAULT 'global',
    external_account_id TEXT            NOT NULL,
    display_name        TEXT            NOT NULL DEFAULT '',
    metadata            JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (platform, identity_namespace, external_account_id)
);
"""

_CREATE_ADAPTER_BINDINGS_TABLE = """\
CREATE TABLE adapter_bindings (
    id                    BIGSERIAL       PRIMARY KEY,
    account_id            BIGINT          NOT NULL REFERENCES platform_accounts(id) ON DELETE RESTRICT,
    adapter_kind          VARCHAR(32)     NOT NULL,
    name                  TEXT            NOT NULL,
    external_id_namespace TEXT            NOT NULL,
    enabled               BOOLEAN         NOT NULL DEFAULT TRUE,
    metadata              JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (account_id, name),
    UNIQUE (external_id_namespace)
);
"""

_CREATE_CONVERSATIONS_TABLE = """\
CREATE TABLE conversations (
    id                       BIGSERIAL       PRIMARY KEY,
    account_id               BIGINT          NOT NULL REFERENCES platform_accounts(id) ON DELETE RESTRICT,
    kind                     VARCHAR(16)     NOT NULL
        CHECK (kind IN ('direct', 'group', 'channel', 'thread')),
    external_conversation_id TEXT            NOT NULL,
    parent_conversation_id   BIGINT          REFERENCES conversations(id) ON DELETE RESTRICT,
    display_name             TEXT            NOT NULL DEFAULT '',
    metadata                 JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT
        (account_id, kind, parent_conversation_id, external_conversation_id)
);
"""

_CREATE_MESSAGES_TABLE = """\
CREATE TABLE messages (
    id              BIGSERIAL       PRIMARY KEY,
    adapter_binding_id BIGINT       NOT NULL REFERENCES adapter_bindings(id) ON DELETE RESTRICT,
    conversation_id BIGINT          NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
    sender_identity_id BIGINT       REFERENCES user_identities(id) ON DELETE RESTRICT,
    external_message_id TEXT,
    reply_to_message_id BIGINT      REFERENCES messages(id) ON DELETE SET NULL,
    source          VARCHAR(16)     NOT NULL DEFAULT 'user',
    content         JSONB           NOT NULL DEFAULT '[]'::jsonb,
    plain_text      TEXT,
    raw_payload     JSONB,
    enrichment_status VARCHAR(16)   NOT NULL DEFAULT 'completed'
        CHECK (enrichment_status IN ('pending', 'streaming', 'completed', 'failed')),
    enrichment_error  TEXT,
    occurred_at     TIMESTAMPTZ     NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_accessed_at TIMESTAMPTZ,
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb
);
"""

_CREATE_ATTACHMENTS_TABLE = """\
CREATE TABLE attachments (
    id              BIGSERIAL       PRIMARY KEY,
    message_id      BIGINT          NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind            VARCHAR(32)     NOT NULL,
    mime_type       TEXT,
    source_url      TEXT,
    storage_ref     TEXT,
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);
"""

_CREATE_LLM_PROVIDERS_TABLE = """\
CREATE TABLE IF NOT EXISTS llm_providers (
    id              SERIAL          PRIMARY KEY,
    alias           TEXT            UNIQUE NOT NULL,
    base_urls       JSONB           NOT NULL DEFAULT '{}'::jsonb,
    api_key         TEXT            NOT NULL,
    models          JSONB           DEFAULT '[]'::jsonb,
    stream          BOOLEAN         DEFAULT true,
    created_at      TIMESTAMPTZ     DEFAULT now()
);
"""

_CREATE_LLM_ACTIVE_TABLE = """\
CREATE TABLE IF NOT EXISTS llm_active (
    key             TEXT            PRIMARY KEY DEFAULT 'default',
    provider_id     INTEGER         REFERENCES llm_providers(id) ON DELETE SET NULL,
    model           TEXT            NOT NULL,
    api_type        TEXT            DEFAULT 'openai',
    extra_body      JSONB,
    request_timeout INTEGER         DEFAULT 60,
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

_CREATE_BOT_CONFIG_TABLE = """\
CREATE TABLE IF NOT EXISTS bot_config (
    key   VARCHAR(64) PRIMARY KEY,
    value JSONB NOT NULL
);
"""

_CREATE_MEMORY_PROFILE_CONTEXT_TABLE = """\
CREATE TABLE IF NOT EXISTS memory_profile_context (
    scope_type  VARCHAR(16) NOT NULL CHECK (scope_type IN ('global', 'conversation')),
    scope_id    BIGINT      NOT NULL,
    content     TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id)
);
"""

_CREATE_MEMORY_PROFILE_USER_TABLE = """\
CREATE TABLE IF NOT EXISTS memory_profile_user (
    user_id     BIGINT      PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT        NOT NULL,
    keywords    JSONB       DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
"""

_CREATE_MEMORY_PROFILE_SELF_TABLE = """\
CREATE TABLE IF NOT EXISTS memory_profile_self (
    id          INTEGER     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    content     TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ DEFAULT now()
);
"""

_SEED_MEMORY_PROFILE_SELF = """\
INSERT INTO memory_profile_self (id) VALUES (1) ON CONFLICT DO NOTHING;
"""

_CREATE_MEMORIES_TABLE = """\
CREATE TABLE IF NOT EXISTS memories (
    id          BIGSERIAL       PRIMARY KEY,
    content     TEXT            NOT NULL,
    embedding   vector,
    source_scope VARCHAR(16) CHECK (source_scope IS NULL OR source_scope IN ('conversation')),
    source_id   BIGINT,
    created_at  TIMESTAMPTZ     DEFAULT now(),
    last_hit    TIMESTAMPTZ,
    hit_count   INTEGER         DEFAULT 0,
    tsv         tsvector        GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
"""

# ── 权限系统 ──────────────────────────────────────────────

_CREATE_PERM_SCOPE_TABLE = """\
CREATE TABLE IF NOT EXISTS perm_scope (
    scope_type  VARCHAR(16)  NOT NULL CHECK (scope_type IN ('global', 'conversation')),
    scope_id    BIGINT       NOT NULL,
    enabled     BOOLEAN      NOT NULL DEFAULT true,
    updated_at  TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (scope_type, scope_id)
);
"""

_CREATE_PERM_GRANT_TABLE = """\
CREATE TABLE IF NOT EXISTS perm_grant (
    user_id     BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope_type  VARCHAR(16)  NOT NULL CHECK (scope_type IN ('global', 'conversation')),
    scope_id    BIGINT       NOT NULL,
    permission  VARCHAR(32)  NOT NULL,
    granted_by  BIGINT       REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (user_id, scope_type, scope_id, permission)
);
"""

_CREATE_PERM_TOOL_TABLE = """\
CREATE TABLE IF NOT EXISTS perm_tool (
    scope_type  VARCHAR(16)  NOT NULL CHECK (scope_type IN ('global', 'conversation')),
    scope_id    BIGINT       NOT NULL,
    tool_name   VARCHAR(64)  NOT NULL,
    PRIMARY KEY (scope_type, scope_id, tool_name)
);
"""

# ── 用户触发策略 ──────────────────────────────────────────

_CREATE_USER_TRIGGER_POLICY_TABLE = """\
CREATE TABLE IF NOT EXISTS user_trigger_policy (
    user_id              BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope_type           VARCHAR(16)  NOT NULL DEFAULT 'global'
        CHECK (scope_type IN ('global', 'conversation')),
    scope_id             BIGINT       NOT NULL DEFAULT 0,
    suppress_llm_trigger BOOLEAN      NOT NULL DEFAULT true,
    rate_multiplier      REAL         NOT NULL DEFAULT 0.0,
    suppress_refresh     BOOLEAN      NOT NULL DEFAULT true,
    note                 TEXT,
    updated_at           TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (user_id, scope_type, scope_id)
);
"""

_CREATE_CUSTOM_TOOLS_TABLE = """\
CREATE TABLE IF NOT EXISTS custom_tools (
    id              SERIAL          PRIMARY KEY,
    name            TEXT            UNIQUE NOT NULL,
    display_name    TEXT            NOT NULL,
    description     TEXT            NOT NULL,
    category        TEXT            NOT NULL DEFAULT 'input',
    scope           TEXT            NOT NULL DEFAULT 'all',
    tool_type       TEXT            NOT NULL DEFAULT 'image_generation',
    provider_alias  TEXT,
    model_name      TEXT,
    api_type        TEXT            DEFAULT 'openai',
    send_as         TEXT            DEFAULT 'image_url',
    parameters      JSONB,
    config          JSONB           NOT NULL DEFAULT '{}'::jsonb,
    enabled         BOOLEAN         DEFAULT FALSE,
    created_at      TIMESTAMPTZ     DEFAULT now(),
    updated_at      TIMESTAMPTZ     DEFAULT now()
);
"""

_CREATE_TOOL_DESCRIPTION_OVERRIDES_TABLE = """\
CREATE TABLE IF NOT EXISTS tool_description_overrides (
    tool_name       TEXT            PRIMARY KEY,
    description     TEXT            NOT NULL,
    updated_at      TIMESTAMPTZ     DEFAULT now()
);
"""

_CREATE_INDEXES = [
    # 完整协议定位符仅在相同适配器绑定和会话内唯一。
    """\
    CREATE UNIQUE INDEX uidx_messages_external_locator
    ON messages (adapter_binding_id, conversation_id, external_message_id)
    WHERE external_message_id IS NOT NULL;
    """,
    # 最热路径：最近上下文、MAX(id) 和 tool-loop 增量游标。
    """\
    CREATE INDEX idx_messages_conversation_id
    ON messages (conversation_id, id DESC);
    """,
    # 指定会话的时间窗口回溯。
    """\
    CREATE INDEX idx_messages_conversation_time
    ON messages (conversation_id, occurred_at DESC, id DESC);
    """,
    # 非终态消息通常很少，partial index 用于连续富化水位。
    """\
    CREATE INDEX idx_messages_pending
    ON messages (conversation_id, id)
    WHERE enrichment_status IN ('pending', 'streaming');
    """,
    # 跨会话最近背景只扫描可进入上下文的终态消息。
    """\
    CREATE INDEX idx_messages_ready_global
    ON messages (occurred_at DESC, id DESC)
    WHERE enrichment_status IN ('completed', 'failed');
    """,
    # 最近一条跨上下文 Sophos 消息；索引保持很小。
    """\
    CREATE INDEX idx_messages_cross_context
    ON messages (conversation_id, occurred_at DESC, id DESC)
    WHERE source = 'sophos'
      AND enrichment_status IN ('completed', 'failed')
      AND metadata ? 'cross_context';
    """,
    # reply_to_message_id 使用 ON DELETE SET NULL，需要反向索引。
    """\
    CREATE INDEX idx_messages_reply
    ON messages (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;
    """,
    """\
    CREATE INDEX idx_attachments_message
    ON attachments (message_id);
    """,
    """\
    CREATE INDEX idx_user_identities_user
    ON user_identities (user_id);
    """,
    # memories 全文搜索索引
    """\
    CREATE INDEX idx_memories_tsv
    ON memories USING gin(tsv);
    """,
    # 消息逻辑容量清理的 LRU 顺序
    """\
    CREATE INDEX idx_messages_lru
    ON messages (COALESCE(last_accessed_at, occurred_at), id);
    """,
    # 记忆逻辑容量清理的 LRU 顺序
    """\
    CREATE INDEX idx_memories_lru
    ON memories (COALESCE(last_hit, created_at), id);
    """,
    # 权限系统：按 scope 查询授权
    """\
    CREATE INDEX idx_perm_grant_scope
    ON perm_grant (scope_type, scope_id);
    """,
]


async def _init_schema(pool: asyncpg.Pool) -> None:
    """为空数据库创建 canonical schema；旧 schema 必须显式迁移。"""
    async with pool.acquire() as conn:
        has_schema_meta = await conn.fetchval("SELECT to_regclass('public.schema_meta') IS NOT NULL")
        if has_schema_meta:
            schema_version = await conn.fetchval("SELECT version FROM schema_meta WHERE singleton")
            if schema_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported database schema version {schema_version}; expected {SCHEMA_VERSION}. "
                    "Run the explicit offline migration before starting Sophos."
                )
            return

        if await conn.fetchval("SELECT to_regclass('public.messages') IS NOT NULL"):
            raise RuntimeError(
                "legacy database schema detected. Sophos does not migrate production data on startup; "
                "run the explicit offline migration first."
            )

        async with conn.transaction():
            # pgvector 必须在创建 memories 之前启用。
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            for ddl in (
                _CREATE_USERS_TABLE,
                _CREATE_USER_IDENTITIES_TABLE,
                _CREATE_USER_ROLES_TABLE,
                _CREATE_IDENTITY_AUTH_AUDIT_TABLE,
                _CREATE_PLATFORM_ACCOUNTS_TABLE,
                _CREATE_ADAPTER_BINDINGS_TABLE,
                _CREATE_CONVERSATIONS_TABLE,
                _CREATE_MESSAGES_TABLE,
                _CREATE_ATTACHMENTS_TABLE,
                _CREATE_LLM_PROVIDERS_TABLE,
                _CREATE_LLM_ACTIVE_TABLE,
                _CREATE_IMAGE_CACHE_TABLE,
                _CREATE_TRIGGER_CONFIG_TABLE,
                _SEED_TRIGGER_CONFIG,
                _CREATE_EMBEDDING_CONFIG_TABLE,
                _SEED_EMBEDDING_CONFIG,
                _CREATE_BOT_CONFIG_TABLE,
                _CREATE_MEMORY_PROFILE_CONTEXT_TABLE,
                _CREATE_MEMORY_PROFILE_USER_TABLE,
                _CREATE_MEMORY_PROFILE_SELF_TABLE,
                _SEED_MEMORY_PROFILE_SELF,
                _CREATE_MEMORIES_TABLE,
                _CREATE_PERM_SCOPE_TABLE,
                _CREATE_PERM_GRANT_TABLE,
                _CREATE_PERM_TOOL_TABLE,
                _CREATE_USER_TRIGGER_POLICY_TABLE,
                _CREATE_CUSTOM_TOOLS_TABLE,
                _CREATE_TOOL_DESCRIPTION_OVERRIDES_TABLE,
            ):
                await conn.execute(ddl)
            for ddl in _CREATE_INDEXES:
                await conn.execute(ddl)
            await conn.execute(_CREATE_SCHEMA_META_TABLE)
            await conn.execute(
                "INSERT INTO schema_meta (singleton, version) VALUES (TRUE, $1)",
                SCHEMA_VERSION,
            )
