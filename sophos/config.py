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

    # ── LLM（首次启动 seed 用，之后以 DB 为准）──────────────
    # 以下 6 项仅在 DB 无 provider 时用于 seed "default" provider，
    # 之后通过 .llm 命令或 WebUI 管理，不再读取 .env。
    # OpenAI 兼容 API 地址（如 https://api.openai.com/v1）
    llm_base_url: str = ""
    # API Key
    llm_api_key: str = ""
    # 模型名（如 gpt-4o, deepseek-chat）
    llm_model: str = ""
    # 是否使用流式请求（避免超时，但部分 provider 可能不兼容）
    llm_stream: bool = True
    # 额外请求体参数（JSON 字符串），会合并到每次 LLM 请求的 payload 中
    # 例如 Gemini 关闭 thinking：{"reasoning_effort":"none"}
    llm_extra_body: str = ""
    # HTTP 请求超时（秒）
    llm_request_timeout: int = 60
    # API 类型：'openai' | 'gemini'（seed 时写入 llm_active.api_type）
    llm_api_type: str = "openai"

    # ── LLM 全局行为参数（始终从 .env 读取）─────────────────
    # 生成温度
    llm_temperature: float = 0.7
    # 最大生成 token 数
    llm_max_tokens: int = 4096
    # 上下文降级：开启后将多轮消息合并为单条 user message，兼容不支持连续 user message 的模型
    llm_flatten_context: bool = False
    # Tool calling 最大循环轮次
    llm_max_tool_rounds: int = 10
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

    # ── Vision（首次启动 seed 用，之后以 DB 为准）────────────
    # 以下 6 项仅在 DB 无 'vision' slot 时用于 seed，
    # 之后通过 .llm vision 命令管理，不再读取 .env。
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    vision_stream: bool = False
    vision_extra_body: str = ""
    vision_request_timeout: int = 30
    # API 类型：'openai' | 'gemini'（seed 时写入 llm_active.api_type）
    vision_api_type: str = "openai"

    # ── Embedding（首次启动 seed 用，之后以 DB 为准）──────────
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_endpoint: str = "/embeddings"
    embedding_extra_body: str = ""
    embedding_request_timeout: int = 30

    # ── Vision 全局行为参数（始终从 .env 读取）────────────────
    vision_system_prompt: str = (
        "以下是出现在群聊中的一张图片。"
        "请用简练的中文概括图像内容，尽可能覆盖值得注意的特征。"
        "首先给出图片类型（如：照片、表情包、梗图、截图、漫画、二次元人物等），然后描述内容。"
        "如果是表情包或梗图，描述其含义和情感。"
        "如果有文字，转录文字内容。"
        "如果图片是多帧网格（多张小图拼成的网格），说明原图是动图/GIF，请描述动画内容和变化过程。"
        "你只需要进行描述，不要做出任何进一步的补充、解释、推断或反馈。"
    )
    vision_refine_prompt: str = (
        "之前对这张图片的描述是：{prev_description}\n"
        "请重新审视图片，如果描述准确则保持不变，如果有遗漏或错误请修正。"
        "同样只需描述，不要做进一步补充。"
    )
    # 喂给 VLM 的最近聊天消息数
    vision_context_messages: int = 5
    # ε-greedy 衰减参数
    vision_epsilon_init: float = 1.0
    vision_epsilon_min: float = 0.0
    vision_epsilon_decay: float = 0.7
    # VLM 最大生成 token 数
    vision_max_tokens: int = 1024
    # imagehash average_hash 的 hash_size 参数
    vision_hash_size: int = 15


# 全局单例，import 后直接使用
settings = Settings()
