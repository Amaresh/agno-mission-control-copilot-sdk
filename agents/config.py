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
    # GitHub Copilot SDK — primary (no local GPU needed)
    use_copilot_sdk: bool = Field(default=True)
    copilot_model: str = Field(default="gpt-4.1")

    # Optional fallbacks (only used if Copilot SDK is unavailable)
    groq_api_key: Optional[str] = Field(default=None)
    ollama_host: str = Field(default="http://localhost:11434")
    default_model: str = Field(default="llama3.1:8b")  # Ollama model
    fallback_model: str = Field(default="llama-3.3-70b-versatile")  # Groq model

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
    vision_issue_repo: str = Field(
        default="", description="GitHub owner/repo for Vision health alerts (e.g., 'owner/repo')"
    )

    # ===========================================
    # Vision Healer — configurable health checks
    # ===========================================
    vision_monitored_services: str = Field(
        default="mc-mcp,mc-api,mc-bot,mc-scheduler",
        description="Comma-separated systemd user services for Vision to monitor",
    )
    vision_log_max_mb: int = Field(default=50, description="Log file size threshold in MB")
    vision_stale_task_hours: float = Field(default=1.5, description="Hours before a task is considered stale")
    vision_task_soft_cap_hours: float = Field(default=3.0, description="Soft cap for IN_PROGRESS tasks (warn)")
    vision_task_hard_cap_hours: float = Field(default=6.0, description="Hard cap for IN_PROGRESS tasks (reset)")
    vision_ram_threshold_pct: int = Field(default=90, description="RAM usage % to trigger alert")
    vision_swap_threshold_pct: int = Field(default=80, description="Swap usage % to trigger alert")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def database_url_async(self) -> str:
        """Convert sync URL to async."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
