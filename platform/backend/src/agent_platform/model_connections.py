"""Project-owned model connections. Credentials never enter project workspaces."""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .providers.base import ModelProvider, ProviderError


LOCAL_PROVIDERS = {"codex", "claude", "kimi"}
AGENT_PROVIDERS = LOCAL_PROVIDERS | {"api"}
project_model: ContextVar[str | None] = ContextVar("project_model", default=None)
project_model_role: ContextVar[str] = ContextVar("project_model_role", default="main")


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["codex", "claude", "kimi", "api", "classic"] = "codex"
    executable: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)
    thinking: Literal["default", "off", "enabled", "low", "medium", "high", "xhigh", "max"] = "default"
    protocol: Literal["openai", "anthropic"] = "openai"
    base_url: str = Field(default="", max_length=2000)
    api_key: SecretStr | None = Field(default=None, max_length=8000)
    runtime_enabled: bool = False

    @model_validator(mode="after")
    def valid_connection(self):
        allowed = {
            "codex": {"default", "off", "low", "medium", "high", "xhigh"},
            "claude": {"default", "off", "low", "medium", "high", "xhigh", "max"},
            "kimi": {"default", "off", "enabled"},
            "api": {"default", "off", "enabled", "low", "medium", "high", "xhigh", "max"},
            "classic": {"default"},
        }
        if self.thinking not in allowed[self.provider]:
            raise ValueError("该接入方式不支持此思考选项")
        if self.provider == "api":
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("请输入不含密钥、查询参数的 HTTP(S) API 地址")
            if not self.model.strip():
                raise ValueError("API 连接必须填写模型名称")
            if self.protocol == "openai" and self.thinking == "enabled":
                raise ValueError("OpenAI 兼容协议请选择具体思考强度或默认")
        return self


class ModelConnections:
    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve() / "model-connections"

    def path(self, project_id: str, role: str = 'main'):
        if role not in {'main', 'vision', 'generation'}:
            raise ValueError('未知模型用途')
        suffix = '' if role == 'main' else '.' + role
        return self.root / (str(UUID(project_id)) + suffix + ".json")

    def load(self, project_id: str, role: str = 'main') -> ModelConnection | None:
        path = self.path(project_id, role)
        return ModelConnection.model_validate_json(path.read_text()) if path.is_file() else None

    def save(self, project_id: str, value: ModelConnection, role: str = 'main') -> dict:
        if role in {'vision', 'generation'} and value.provider != 'api':
            raise ValueError('视觉及生成模型需要原始 API 连接')
        previous = self.load(project_id, role)
        if value.provider == "api" and not value.api_key:
            if previous and previous.provider == "api" and previous.base_url == value.base_url and previous.protocol == value.protocol:
                value = value.model_copy(update={"api_key": previous.api_key})
            if not value.api_key or not value.api_key.get_secret_value():
                raise ValueError("请填写 API Key；切换 API 地址时需要重新填写")
        if value.provider != "api":
            value = value.model_copy(update={"api_key": None, "base_url": ""})
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(project_id, role)
        raw = value.model_dump(mode="json", exclude={"api_key"})
        raw["api_key"] = value.api_key.get_secret_value() if value.api_key else None
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(raw, out, ensure_ascii=False)
        temporary.replace(path)
        return self.public(value)

    @staticmethod
    def public(value: ModelConnection) -> dict:
        return {**value.model_dump(mode="json", exclude={"api_key"}), "has_api_key": bool(value.api_key)}

    def enabled(self, project_id: str, role: str = 'main') -> bool:
        value = self.load(project_id, role)
        return bool(value and value.runtime_enabled and value.provider == 'api')

    def workflow_enabled(self, project_id: str) -> bool:
        return self.enabled(project_id) or self.enabled(project_id, 'vision')

    def generation_settings(self, project_id: str) -> dict:
        own = self.load(project_id, 'generation')
        value = own or self.load(project_id)
        return {'mode': 'independent' if own else 'inherit',
                **(self.public(value) if value else {'provider': None, 'has_api_key': False, 'runtime_enabled': False})}

    def inherit_generation(self, project_id: str) -> dict:
        self.path(project_id, 'generation').unlink(missing_ok=True)
        return self.generation_settings(project_id)

    def provider(self, project_id: str, role: str = 'main'):
        from .connected_model import ConnectedModel
        value = self.load(project_id, role)
        if role == 'generation':
            # An explicit override is authoritative, including when disabled.
            # Only the visible "inherit" mode follows the project's main model.
            value = value or self.load(project_id)
            if not value or value.provider != 'api' or not value.runtime_enabled:
                raise ProviderError('请配置并启用工作流生成模型；也可在生成模型设置中沿用项目主模型')
        if not value:
            raise ProviderError("请先配置项目模型")
        return ConnectedModel(value, self.root / "sessions" / project_id, supports_images=role == 'vision')


class ProjectModelProvider(ModelProvider):
    """Context-local routing also propagates into nested AgentRuntime tasks."""
    name = "project"

    def __init__(self, fallback: ModelProvider, connections: ModelConnections):
        self.fallback, self.connections = fallback, connections

    def __getattr__(self, name):
        return getattr(self.fallback, name)

    def current(self):
        project_id = project_model.get()
        if not project_id:
            return self.fallback
        role = project_model_role.get()
        if not self.connections.enabled(project_id, role):
            raise ProviderError("本项目尚未配置或启用视觉模型" if role == 'vision' else "本项目尚未启用工作流模型调用")
        return self.connections.provider(project_id, role='vision') if role == 'vision' else self.connections.provider(project_id)

    def capabilities(self, model):
        return self.current().capabilities(model)

    def provider_name_for(self, model):
        return self.current().provider_name_for(model)

    def selected_model(self, default):
        current = self.current()
        if current is self.fallback:
            return default
        return current.connection.model or f"{current.name}/default"

    @property
    def stream(self):
        # Preserve the selected provider signature and optional parameters.
        return self.current().stream
