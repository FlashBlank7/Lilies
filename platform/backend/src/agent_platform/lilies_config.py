from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_X25519_PRIME = 2**255 - 19
_X25519_A24 = 121665


def x25519_public_key(private_key: bytes) -> bytes:
    """Derive an RFC 7748 X25519 public key for daemon identity fingerprinting."""

    if len(private_key) != 32:
        raise ValueError("X25519 private key must contain exactly 32 bytes")
    scalar_bytes = bytearray(private_key)
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 127
    scalar_bytes[31] |= 64
    scalar = int.from_bytes(scalar_bytes, "little")
    x_1 = 9
    x_2, z_2 = 1, 0
    x_3, z_3 = 9, 1
    swap = 0
    for bit_index in range(254, -1, -1):
        bit = (scalar >> bit_index) & 1
        swap ^= bit
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = bit
        a = (x_2 + z_2) % _X25519_PRIME
        aa = a * a % _X25519_PRIME
        b = (x_2 - z_2) % _X25519_PRIME
        bb = b * b % _X25519_PRIME
        e = (aa - bb) % _X25519_PRIME
        c = (x_3 + z_3) % _X25519_PRIME
        d = (x_3 - z_3) % _X25519_PRIME
        da = d * a % _X25519_PRIME
        cb = c * b % _X25519_PRIME
        x_3 = (da + cb) ** 2 % _X25519_PRIME
        z_3 = x_1 * (da - cb) ** 2 % _X25519_PRIME
        x_2 = aa * bb % _X25519_PRIME
        z_2 = e * (aa + _X25519_A24 * e) % _X25519_PRIME
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2
    public_value = x_2 * pow(z_2, _X25519_PRIME - 2, _X25519_PRIME) % _X25519_PRIME
    return public_value.to_bytes(32, "little")


class LiliesSettings(BaseSettings):
    """Configuration owned by the standalone local Lilies process.

    The local agent intentionally does not inherit :class:`agent_platform.config.Settings`.
    In particular, its data directory and client credentials are not the platform's data
    directory or global API token.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LILIES_",
        extra="ignore",
    )

    data_dir: Path = Path("~/.lilies")
    workspace_root: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65_535)
    schema_version: str = "1.0"
    system_identity_version: str = "lilies-local-v2"
    agent_version: str = "0.4.13"

    model: str = "deepseek-v4-flash"
    deepseek_api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("LILIES_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        validation_alias=AliasChoices("LILIES_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    model_egress_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "LILIES_MODEL_EGRESS_ENABLED",
            "MODEL_EGRESS_ENABLED",
        ),
    )
    model_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=16_384, ge=256, le=384_000)
    context_window: int = Field(default=128_000, ge=8_000)
    default_max_turns: int = Field(default=30, ge=1, le=200)
    default_max_tool_calls: int = Field(default=100, ge=1, le=1000)
    default_max_budget_usd: float = Field(default=5.0, gt=0, le=1000)
    default_deadline_seconds: int = Field(default=3600, ge=30, le=86_400)
    pairing_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    cli_token_ttl_seconds: int = Field(default=86_400, ge=300)
    platform_token_ttl_seconds: int = Field(default=30 * 86_400, ge=3600)
    event_poll_seconds: float = Field(default=0.2, gt=0, le=10)
    max_request_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    model_price_input_usd_per_million: float = Field(default=0.14, ge=0)
    model_price_output_usd_per_million: float = Field(default=0.28, ge=0)

    @field_validator("data_dir", "workspace_root", mode="before")
    @classmethod
    def expand_user_path(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            return Path(value).expanduser()
        return value

    @field_validator("host")
    @classmethod
    def nonempty_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host cannot be empty")
        return value

    @property
    def daemon_file(self) -> Path:
        return self.data_dir / "daemon.json"

    @property
    def identity_key_file(self) -> Path:
        return self.data_dir / "daemon.key"

    @property
    def resolved_workspace_root(self) -> Path:
        return self.workspace_root or (self.data_dir / "workspaces")

    @property
    def base_url(self) -> str:
        host = "[::1]" if self.host == "::1" else self.host
        return f"http://{host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    def prepare(self) -> None:
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.workspace_root is not None:
            self.workspace_root = self.workspace_root.expanduser().resolve()
        self._guard_platform_data_dir_alias()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.resolved_workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.resolved_workspace_root, 0o700)
        self._ensure_identity_key()

    def daemon_fingerprint(self) -> str:
        self._ensure_identity_key()
        public_key = x25519_public_key(self.identity_key_file.read_bytes())
        digest = hashlib.sha256(public_key).hexdigest()
        return f"sha256:{digest}"

    def _ensure_identity_key(self) -> None:
        if self.identity_key_file.exists():
            os.chmod(self.identity_key_file, 0o600)
            return
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.identity_key_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, secrets.token_bytes(32))
        finally:
            os.close(descriptor)

    def _guard_platform_data_dir_alias(self) -> None:
        platform_value = os.environ.get("DATA_DIR")
        platform_dirs = {Path("data").resolve()}
        if platform_value:
            platform_dirs.add(Path(platform_value).expanduser().resolve())
        contains_platform_state = (self.data_dir / "agent_platform.db").exists() or (
            self.data_dir / "events"
        ).exists()
        if self.data_dir in platform_dirs or contains_platform_state:
            raise ValueError("LILIES_DATA_DIR must not be the platform DATA_DIR")


def default_lilies_settings() -> LiliesSettings:
    settings = LiliesSettings()
    settings.prepare()
    return settings
