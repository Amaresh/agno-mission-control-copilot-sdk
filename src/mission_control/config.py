"""
Configuration management for Mission Control.
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===========================================
    # LLM Providers
    # ===========================================
    ollama_host: str = Field(default="http://localhost:11434")
    groq_api_key: Optional[str] = Field(default=None)

    # Default models
    default_model: str = Field(default="llama3.1:8b")
    fallback_model: str = Field(default="llama-3.3-70b-versatile")  # Groq
    
    # GitHub Copilot SDK (premium models)
    use_copilot_sdk: bool = Field(default=True)  # Use Copilot SDK by default
    copilot_model: str = Field(default="gpt-4.1")  # Premium model
    vision_model: str = Field(default="gpt-4.1")  # Vision agent model (override via VISION_MODEL env)

    # ===========================================
    # GitHub MCP
    # ===========================================
    github_token: Optional[str] = Field(default=None)

    # ===========================================
    # DigitalOcean MCP
    # ===========================================
    do_api_token: Optional[str] = Field(default=None)

    # ===========================================
    # Telegram MCP (Jarvis)
    # ===========================================
    telegram_bot_token: Optional[str] = Field(default=None)
    telegram_chat_id: Optional[str] = Field(default=None)

    # ===========================================
    # Twilio MCP (Pepper)
    # ===========================================
    twilio_account_sid: Optional[str] = Field(default=None)
    twilio_auth_token: Optional[str] = Field(default=None)
    twilio_phone_number: Optional[str] = Field(default=None)
    sendgrid_api_key: Optional[str] = Field(default=None)  # TODO: Implement

    # ===========================================
    # Tavily MCP (Fury)
    # ===========================================
    tavily_api_key: Optional[str] = Field(default=None)

    # ===========================================
    # Database
    # ===========================================
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/mission_control"
    )
    redis_url: str = Field(default="redis://localhost:6379")

    # ===========================================
    # DigitalOcean Spaces (Backups)
    # ===========================================
    do_spaces_key: Optional[str] = Field(default=None)
    do_spaces_secret: Optional[str] = Field(default=None)
    do_spaces_bucket: str = Field(default="mission-control-backups")
    do_spaces_region: str = Field(default="nyc3")

    # ===========================================
    # Application
    # ===========================================
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    heartbeat_interval_minutes: int = Field(default=15)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def database_url_async(self) -> str:
        """Convert sync URL to async driver variant."""
        url = self.database_url
        if url.startswith("sqlite"):
            # sqlite:/// → sqlite+aiosqlite:///
            return url.replace("sqlite:///", "sqlite+aiosqlite:///")
        # postgresql:// → postgresql+asyncpg://
        return url.replace("postgresql://", "postgresql+asyncpg://")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
