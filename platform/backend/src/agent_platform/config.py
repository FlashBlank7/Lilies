from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    deepseek_api_key: str | None = Field(default=None, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"
    deepseek_generator_model: str = "deepseek-v4-pro"
    deepseek_runtime_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 600.0
    model_egress_enabled: bool = False
    model_price_estimates_usd_per_million: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "deepseek-v4-pro": {"input_tokens": 0.435, "output_tokens": 0.87},
            "deepseek-v4-flash": {"input_tokens": 0.14, "output_tokens": 0.28},
        }
    )

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
    scheduler_worker_offload_enabled: bool = False
    adaptive_monitoring_refresh_interval_seconds: float = 0.0
    evaluation_live_enabled: bool = False
    evaluation_production_observation_enabled: bool = False
    evaluation_production_observation_evidence_path: Path | None = None
    templates_dir: Path | None = None
    platform_harness_max_active_tasks: int = 100
    platform_harness_max_model_calls_per_task: int = 100
    platform_harness_max_tool_calls_per_task: int = 200
    platform_harness_max_node_executions_per_task: int = 1000
    platform_harness_max_model_calls_per_owner: int = 0
    platform_harness_max_tool_calls_per_owner: int = 0
    platform_harness_max_node_executions_per_owner: int = 0
    platform_harness_stale_active_task_seconds: float = 0.0
    platform_harness_secret_policy_enabled: bool = True
    platform_harness_secret_envelope_key: str = Field(default="", repr=False)
    platform_harness_secret_envelope_key_id: str = "local"
    platform_harness_secret_envelope_previous_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    platform_harness_secret_kms_provider: str = "none"
    platform_harness_secret_kms_provider_id: str = "local-kms"
    platform_harness_secret_kms_key_id: str = "primary"
    platform_harness_secret_kms_key: str = Field(default="", repr=False)
    platform_harness_secret_kms_previous_keys: dict[str, str] = Field(default_factory=dict, repr=False)
    platform_harness_network_egress_policy: str = "full"
    platform_harness_network_egress_allowlist: list[str] = Field(default_factory=list)
    platform_harness_worker_id: str = ""
    platform_harness_worker_lease_seconds: float = 0.0
    platform_harness_worker_supervision_poll_seconds: float = 5.0
    platform_harness_worker_supervision_limit: int = 10
    platform_harness_worker_process_command: list[str] = Field(default_factory=list)
    platform_harness_worker_process_cwd: Path | None = None
    platform_harness_worker_process_stop_timeout_seconds: float = 5.0
    complexity_router_default_mode: Literal["disabled", "shadow_only", "operator_opt_in", "limited_default"] = "limited_default"
    complexity_router_limited_default_enabled: bool = True
    complexity_router_limited_default_min_confidence: float = 0.55
    lilies_platform_contract_version: int = Field(default=1, ge=1, le=2**63 - 1)
    # v0.4.13 rollout gates.  The local daemon route remains opt-in until its
    # deterministic and browser evidence is complete; collaboration and the
    # product-wide default have later, independent gates.
    lilies_local_agent_enabled: bool = False
    lilies_collaboration_enabled: bool = False
    lilies_collaboration_developer_token: str = Field(default="", repr=False)
    lilies_collaboration_verifier_token: str = Field(default="", repr=False)
    lilies_formal_hidden_seed_key: str = Field(default="", repr=False)
    lilies_developer_worker_executable: Path | None = None
    lilies_collaborative_development_enabled: bool = False
    lilies_collaborative_development_signing_key: str = Field(
        default="",
        repr=False,
    )
    lilies_autonomous_collaboration_enabled: bool = False
    lilies_local_builder_default: bool = False
    lilies_platform_base_url: str = ""

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
