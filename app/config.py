"""Runtime configuration (implement.md §12.2)."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+asyncpg://gantt:gantt@localhost:5432/gantt"
    )
    session_secret: str = Field(default="change-me")
    default_timezone: str = Field(default="Asia/Taipei")

    worker_id: str = Field(default_factory=lambda: f"{os.uname().nodename}")
    worker_poll_interval_ms: int = 1000

    #: Hosts the built-in http_request handler may reach (§6.1.1). Empty
    #: means "none": the handler refuses rather than defaulting to open.
    http_handler_allowed_hosts: list[str] = Field(default_factory=list)
    shell_handler_enabled: bool = False
    shell_handler_allowed_commands: list[str] = Field(default_factory=list)

    notification_channels: list[str] = Field(default_factory=lambda: ["email"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
