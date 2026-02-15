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

    # ── LLM ──────────────────────────────────────────────
    # OpenAI 兼容 API 地址（如 https://api.openai.com/v1）
    llm_base_url: str = ""
    # API Key
    llm_api_key: str = ""
    # 模型名（如 gpt-4o, deepseek-chat）
    llm_model: str = ""
    # 生成温度
    llm_temperature: float = 0.7
    # 最大生成 token 数
    llm_max_tokens: int = 4096
    # 上下文降级：开启后将多轮消息合并为单条 user message，兼容不支持连续 user message 的模型
    llm_flatten_context: bool = False
    # HTTP 请求超时（秒）
    llm_request_timeout: int = 60
    # Tool calling 最大循环轮次
    llm_max_tool_rounds: int = 10
    # 是否使用流式请求（避免超时，但部分 provider 可能不兼容）
    llm_stream: bool = True
    # 额外请求体参数（JSON 字符串），会合并到每次 LLM 请求的 payload 中
    # 例如 Gemini 关闭 thinking：{"reasoning_effort":"none"}
    llm_extra_body: str = ""
    # 用户消息格式模板（多轮和拍平模式都使用）
    # 可用占位符：{{time}} {{mid}} {{name}} {{uid}} {{message}}
    llm_user_schema: str = "[{{time}}] #{{mid}} {{name}}({{uid}})：{{message}}"
    # Bot 消息格式模板（仅拍平模式使用，多轮模式下 assistant role 自带身份）
    llm_bot_schema: str = "[{{time}}] #{{mid}} {{name}}：{{message}}"

    # ── 跨 Context ─────────────────────────────────────────
    # 跨 context 背景注入模式：
    #   "system"  — 最近一条 cross_context 背景追加到 system prompt
    #   "inline"  — 每条带 background 的消息都附带背景信息
    #   "off"     — 不注入
    cross_context_mode: str = "system"


# 全局单例，import 后直接使用
settings = Settings()
