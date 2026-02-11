"""应用配置管理。

使用 pydantic-settings 从 .env 文件加载配置，提供类型安全的访问。
所有配置项集中在此，不再散落 os.getenv。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Sophos 全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # 不识别的环境变量直接忽略（.env 里还有 POSTGRES_USER 等给 Docker Compose 用的变量）
        extra="ignore",
    )

    # ── 数据库 ────────────────────────────────────────────
    database_url: str = "postgresql://sophos:sophos_dev@localhost:5432/sophos"

    # ── OneBot WebSocket ──────────────────────────────────
    onebot_ws_url: str = "ws://localhost:3001"
    onebot_ws_token: str = ""

    # ── Bot 身份 ──────────────────────────────────────────
    # Bot 登录的 QQ 号，用于触发判断等场景
    bot_id: int = 0
    # Bot 的昵称，用于触发判断（如被 @ 或被提及时识别自身）
    bot_nickname: str = ""

    # ── 消息上下文 ────────────────────────────────────────
    # 每个会话（群聊/私聊）查询时返回的最大消息条数
    max_context_messages: int = 50
    # 构建 LLM 上下文时，是否包含同账号其他来源的消息（其他 bot、手动发的等）
    include_co_account_in_context: bool = True

    # ── 时区 ──────────────────────────────────────────────
    # 喂给 LLM 时转换时间戳用的 UTC 偏移（小时）
    timezone_offset: int = 8


# 全局单例，import 后直接使用
settings = Settings()
