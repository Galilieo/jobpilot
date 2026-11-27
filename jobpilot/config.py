"""读取仓库根目录配置，提供数据库连接信息和各用途的模型参数。"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

REPO_ROOT = Path(__file__).resolve().parent.parent


class ModelPurpose(StrEnum):
    """声明网关支持的调用用途，枚举值与 Settings 的配置字段名一致。"""

    EXTRACT_FACTS = "extract_facts"
    PARSE_JD = "parse_jd"
    ORDER_OCR = "order_ocr"
    MATCH = "match"
    PLAN = "plan"
    REWRITE = "rewrite"
    EDIT_BULLET = "edit_bullet"
    GROUND_CHECK = "ground_check"
    AGENT = "agent"
    COMPACT = "compact"


class ProtocolAdapter(StrEnum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"


class ModelProfile(BaseModel):
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    adapter: ProtocolAdapter


def default_model_profiles() -> dict[str, ModelProfile]:
    # 窗口和输出上限与服务商 /models 返回的单位一致。
    return {
        model: ModelProfile(
            context_window=1_048_576,
            max_output_tokens=393_216,
            adapter=ProtocolAdapter.DEEPSEEK,
        )
        for model in ("deepseek-flash", "deepseek-v4-pro")
    }


class ModelSettings(BaseModel):
    """声明可由环境变量调整的模型参数；网关按用途读取。

    输出格式等与提示词、解析器绑定的约定不在此处，由调用方传给网关。
    """

    model_name: str = "deepseek-flash"
    thinking: bool = False
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=2048, gt=0)


class AgentLimits(BaseModel):
    """同一轮对话及其工具共享的预算与历史窗口。"""

    model_calls: int = Field(default=65, ge=1, le=150)
    # 每轮 token 上限可选，默认不设：防失控靠调用和工具次数，费用靠服务商用量上限，
    # 与 Hermes、Claude Agent SDK、Pydantic AI 的做法一致。设了就是固定数，不随窗口变。
    model_tokens: int | None = Field(default=None, gt=0)
    tool_calls: int = Field(default=12, ge=1, le=30)
    seconds: float = Field(default=180, gt=0)
    context_window_ratio: float = Field(default=0.5, gt=0, le=1)
    # 目前只有本人使用，不另设上限，有效窗口即模型窗口的一半。
    context_window_cap_tokens: int | None = Field(default=None, gt=0)
    compact_trigger_ratio: float = Field(default=0.8, gt=0, lt=1)
    compact_target_ratio: float = Field(default=0.5, gt=0, lt=1)
    compact_summary_share: float = Field(default=0.2, gt=0, lt=1)
    compact_input_tokens: int = Field(default=24_000, gt=0)
    compact_model_calls: int = Field(default=3, ge=1)

    match_job_calls: int = Field(default=2, ge=1)
    tailor_resume_calls: int = Field(default=1, ge=1)
    parse_jd_without_attachments: int = Field(default=1, ge=1)
    tool_result_bytes: int = Field(default=6000, ge=512)


class Settings(BaseSettings):
    """加载本地配置；环境变量优先于 .env，同名默认值最后使用。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="JOBPILOT_",
        env_nested_delimiter="__",
        extra="ignore",
        hide_input_in_errors=True,
    )

    postgres_user: str = Field(validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(validation_alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(validation_alias="POSTGRES_DB")
    postgres_host: str = Field(default="127.0.0.1", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(
        default=5432, validation_alias="POSTGRES_PORT", ge=1, le=65535
    )
    postgres_test_db: str = Field(
        default="jobpilot_test", validation_alias="POSTGRES_TEST_DB"
    )

    deepseek_api_key: SecretStr = Field(default="", validation_alias="OPENAI_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias="OPENAI_BASE_URL",
    )
    cookie_name: str = "jobpilot_session"
    cookie_max_age_seconds: int = Field(default=604800, gt=0)
    # 为 True 时浏览器只在 HTTPS 下发送 Cookie；本地 http 开发保持 False。
    cookie_secure: bool = False
    database_connect_timeout_seconds: int = Field(default=5, gt=0)

    # 一份简历常拆出几十条事实，2048 容易截断输出，默认放宽到 8192。
    extract_facts: ModelSettings = Field(
        default_factory=lambda: ModelSettings(max_tokens=8192)
    )
    # 一份 JD 常有二三十条要求和职责，每条带原文片段，2048 不够。
    parse_jd: ModelSettings = Field(
        default_factory=lambda: ModelSettings(max_tokens=4096)
    )
    # 逐条返回要求 ID、引用事实 ID、原因与建议，十几到三十条要求会超过 2048。
    match: ModelSettings = Field(default_factory=lambda: ModelSettings(max_tokens=4096))
    plan: ModelSettings = Field(default_factory=ModelSettings)
    rewrite: ModelSettings = Field(default_factory=ModelSettings)
    edit_bullet: ModelSettings = Field(default_factory=ModelSettings)
    ground_check: ModelSettings = Field(default_factory=ModelSettings)
    order_ocr: ModelSettings = Field(default_factory=ModelSettings)
    agent: ModelSettings = Field(default_factory=ModelSettings)
    compact: ModelSettings = Field(
        default_factory=lambda: ModelSettings(max_tokens=4096)
    )
    model_profiles: dict[str, ModelProfile] = Field(
        default_factory=default_model_profiles
    )

    agent_limits: AgentLimits = Field(default_factory=AgentLimits)

    model_max_attempts: int = Field(default=2, ge=1)
    model_retry_delay_seconds: float = Field(default=0.5, ge=0)
    # 每个账号每天的预扣上限；不设置时不限，开放给他人前要设上。
    daily_token_limit: int | None = Field(
        default=None, gt=0, validation_alias="DAILY_TOKEN_LIMIT"
    )

    def model_for_purpose(self, purpose: str) -> ModelSettings:
        """按用途查找模型配置，未知用途立即报错。"""
        return getattr(self, ModelPurpose(purpose).value)

    def model_profile(self, purpose: str) -> ModelProfile:
        return self.model_profiles[self.model_for_purpose(purpose).model_name]

    def effective_window(self, purpose: str) -> int:
        window = self.model_profile(purpose).context_window
        reduced = int(window * self.agent_limits.context_window_ratio)
        return max(
            1,
            min(window, reduced, self.agent_limits.context_window_cap_tokens or window),
        )

    @model_validator(mode="after")
    def validate_model_windows(self) -> "Settings":
        for purpose in ModelPurpose:
            model = self.model_for_purpose(purpose).model_name
            if model not in self.model_profiles:
                raise ValueError(f"模型 {model} 没有登记档案")
        limits = self.agent_limits
        if limits.compact_target_ratio >= limits.compact_trigger_ratio:
            raise ValueError("压缩目标水位必须低于触发水位")
        return self

    @property
    def database_url(self) -> URL:
        """组装开发库 URL，让 SQLAlchemy 正确处理密码中的特殊字符。"""
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def test_database_url(self) -> URL:
        """复用连接参数，只把目标切换到独立测试库。"""
        return self.database_url.set(database=self.postgres_test_db)


settings = Settings()
