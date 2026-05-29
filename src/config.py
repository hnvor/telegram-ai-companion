from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    allowed_user_id: int
    telegram_proxy: str | None = None  # http://user:pass@host:port или socks5://...

    anthropic_api_key: str
    openrouter_api_key: str | None = None
    enable_voice: bool = True

    database_url: str

    default_timezone: str = "Asia/Bangkok"
    default_display_name: str = ""

    llm_main_model: str = "claude-sonnet-4-6"
    llm_cheap_model: str = "claude-haiku-4-5-20251001"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    log_level: str = "INFO"
    env: str = Field(default="dev")


settings = Settings()  # type: ignore[call-arg]
