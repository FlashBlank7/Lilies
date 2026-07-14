from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Agent Platform"
    api_token: str = "change-me"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("data")
    workspace_root: Path = Path("workspaces")
    workspace_host_root: Path | None = None

    # ── Provider API keys ──────────────────────────────────────
    deepseek_api_key: str | None = Field(default=None, repr=False)
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)

    # ── Provider base URLs ─────────────────────────────────────
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com"

    # ── Provider timeouts ──────────────────────────────────────
    deepseek_timeout_seconds: float = 600.0

    # ── Default model selection ────────────────────────────────
    # The deepseek_ prefix is historical; these are used as the platform-wide defaults.
    deepseek_generator_model: str = "deepseek-v4-pro"
    deepseek_runtime_model: str = "deepseek-v4-flash"

    sandbox_image: str = "agent-platform-sandbox:latest"
    sandbox_uid: int = 10001
    sandbox_gid: int = 10001
    sandbox_cpus: float = 2.0
    sandbox_memory: str = "1g"
    sandbox_pids_limit: int = 256
    sandbox_command_timeout: float = 120.0

    max_parallel_tools: int = 4
    max_subagent_depth: int = 2
    event_queue_size: int = 1000
    scheduler_poll_seconds: float = 30.0
    templates_dir: Path | None = None

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
