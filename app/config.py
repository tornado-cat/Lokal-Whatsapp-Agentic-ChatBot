from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=5000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    evolution_api_base_url: str = Field(
        default="http://evolution-api:8080", alias="EVOLUTION_API_BASE_URL"
    )
    evolution_api_key: str = Field(default="change-me", alias="EVOLUTION_API_KEY")
    evolution_webhook_url: str = Field(
        default="http://host.docker.internal:5000/webhook/whatsapp",
        alias="EVOLUTION_WEBHOOK_URL",
    )
    whatsapp_allowed_senders: str = Field(default="", alias="WHATSAPP_ALLOWED_SENDERS")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    llm_model: str = Field(default="gpt-4.1-mini", alias="LLM_MODEL")
    agent_system_prompt_path: str = Field(
        default="prompts/system_prompt.md",
        alias="AGENT_SYSTEM_PROMPT_PATH",
    )

    memory_max_messages: int = Field(default=20, alias="MEMORY_MAX_MESSAGES")

    @property
    def allowed_sender_set(self) -> set[str]:
        return {
            item.strip().replace("+", "")
            for item in self.whatsapp_allowed_senders.split(",")
            if item.strip()
        }

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
