from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .factory import AgentFactory
from .permissions import PermissionBroker
from .platform_harness import PlatformHarness
from .providers import ModelProvider
from .providers.multi import MultiProvider
from .runtime import AgentRuntime
from .sandbox import SandboxManager
from .secret_kms import build_secret_kms_provider
from .storage import Storage
from .tools import ToolRegistry, build_core_registry


@dataclass(frozen=True, slots=True)
class AgentRuntimeCore:
    """The reusable platform-agent core before workflow product services are assembled."""

    storage: Storage
    provider: ModelProvider
    tools: ToolRegistry
    sandboxes: SandboxManager
    permissions: PermissionBroker
    harness: PlatformHarness
    runtime: AgentRuntime
    factory: AgentFactory


def build_agent_runtime_core(
    settings: Settings,
    provider: ModelProvider | None = None,
) -> AgentRuntimeCore:
    """Build only generic agent runtime dependencies.

    This boundary intentionally knows nothing about blocks, applications, workflow storage,
    workflow execution, connectors, governance, or the product Builder.
    """

    storage = Storage(settings.data_dir)
    tools = build_core_registry(settings.program_tool_profiles_file)
    sandboxes = SandboxManager(settings)
    permissions = PermissionBroker()
    selected_provider = provider or MultiProvider(
        deepseek_api_key=settings.deepseek_api_key,
        deepseek_base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
        egress_enabled=settings.model_egress_enabled,
    )
    secret_kms_provider = build_secret_kms_provider(
        provider=settings.platform_harness_secret_kms_provider,
        provider_id=settings.platform_harness_secret_kms_provider_id,
        key_id=settings.platform_harness_secret_kms_key_id,
        key=settings.platform_harness_secret_kms_key,
        previous_keys=settings.platform_harness_secret_kms_previous_keys,
    )
    harness = PlatformHarness(
        storage=storage,
        max_active_tasks=settings.platform_harness_max_active_tasks,
        max_model_calls_per_task=settings.platform_harness_max_model_calls_per_task,
        max_tool_calls_per_task=settings.platform_harness_max_tool_calls_per_task,
        max_node_executions_per_task=settings.platform_harness_max_node_executions_per_task,
        max_model_calls_per_owner=settings.platform_harness_max_model_calls_per_owner,
        max_tool_calls_per_owner=settings.platform_harness_max_tool_calls_per_owner,
        max_node_executions_per_owner=settings.platform_harness_max_node_executions_per_owner,
        stale_active_task_seconds=settings.platform_harness_stale_active_task_seconds,
        secret_policy_enabled=settings.platform_harness_secret_policy_enabled,
        secret_envelope_key=settings.platform_harness_secret_envelope_key or settings.api_token,
        secret_envelope_key_id=settings.platform_harness_secret_envelope_key_id,
        secret_envelope_previous_keys=settings.platform_harness_secret_envelope_previous_keys,
        secret_kms_provider=secret_kms_provider,
        network_egress_policy=settings.platform_harness_network_egress_policy,
        network_egress_allowlist=settings.platform_harness_network_egress_allowlist,
        worker_id=settings.platform_harness_worker_id or None,
        worker_lease_seconds=settings.platform_harness_worker_lease_seconds,
    )
    runtime = AgentRuntime(
        settings=settings,
        storage=storage,
        provider=selected_provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=permissions,
        harness=harness,
    )
    factory = AgentFactory(
        settings=settings,
        storage=storage,
        provider=selected_provider,
        runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
    )
    return AgentRuntimeCore(
        storage=storage,
        provider=selected_provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=permissions,
        harness=harness,
        runtime=runtime,
        factory=factory,
    )
