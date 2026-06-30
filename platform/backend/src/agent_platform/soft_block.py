"""Soft Block — a configurable block that can take on different behaviors
based on its strategy, reducing the cognitive load of choosing among 25 discrete blocks.

Design rationale:
  25 discrete blocks are correct for precision (ADR-001), but the AI Builder and
  novice users benefit from "block families" that share a common interface.
  A SoftBlock acts as a meta-block: one config schema, many strategies.

Block families (6 → replaces 25 discrete blocks when used as SoftBlock):
  context:   assemble | inject_workspace | memory | compact
  model:     call | router | stop_continue | classify_error
  tool:      execute | normalize | permission_gate | sandbox
  governance: budget | rounds | checkpoint | cancel | record
  agent:     spawn_subagent | dispatch_tasks | mailbox | dependency_gate
  skill:     load_skill | mcp_gateway | capability_registry
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Strategy types (one per family) ────────────────────────────

ContextStrategy = Literal[
    "context_assemble",
    "context_inject_workspace",
    "context_memory",
    "context_compact",
]

ModelStrategy = Literal[
    "model_call",
    "model_router",
    "model_stop_continue",
    "model_classify_error",
]

ToolStrategy = Literal[
    "tool_execute",
    "tool_normalize",
    "tool_permission_gate",
    "tool_sandbox",
]

GovernanceStrategy = Literal[
    "gov_budget",
    "gov_rounds",
    "gov_checkpoint",
    "gov_cancel",
    "gov_record",
]

AgentStrategy = Literal[
    "agent_spawn_subagent",
    "agent_dispatch_tasks",
    "agent_mailbox",
    "agent_dependency_gate",
]

SkillStrategy = Literal[
    "skill_load",
    "skill_mcp_gateway",
    "skill_capability_registry",
]

SoftBlockStrategy = (
    ContextStrategy | ModelStrategy | ToolStrategy
    | GovernanceStrategy | AgentStrategy | SkillStrategy
)


# ── Family → discrete block mapping ─────────────────────────────

FAMILY_MAP: dict[str, dict[str, str]] = {
    "context": {
        "context_assemble": "context_assembler",
        "context_inject_workspace": "workspace_context_injector",
        "context_memory": "conversation_memory",
        "context_compact": "context_compactor",
    },
    "model": {
        "model_call": "model_turn",
        "model_router": "tool_call_router",
        "model_stop_continue": "stop_continue_controller",
        "model_classify_error": "retry_error_classifier",
    },
    "tool": {
        "tool_execute": "tool_executor",
        "tool_normalize": "tool_result_normalizer",
        "tool_permission_gate": "permission_gate",
        "tool_sandbox": "sandbox_boundary",
    },
    "governance": {
        "gov_budget": "budget_gate",
        "gov_rounds": "round_limit",
        "gov_checkpoint": "checkpoint_resume",
        "gov_cancel": "cancellation_point",
        "gov_record": "event_recorder",
    },
    "agent": {
        "agent_spawn_subagent": "subagent_spawn",
        "agent_dispatch_tasks": "task_dispatcher",
        "agent_mailbox": "mailbox_wait_wake",
        "agent_dependency_gate": "dependency_gate",
    },
    "skill": {
        "skill_load": "skill_loader",
        "skill_mcp_gateway": "mcp_gateway",
        "skill_capability_registry": "capability_registry",
    },
}

# ── Config ──────────────────────────────────────────────────────

class SoftBlockConfig(BaseModel):
    """Design-time macro: one block type, many strategies.

    A SoftBlock is a *design-time convenience*, not a runtime abstraction.
    Its strategy is fixed at design time and maps 1:1 to a discrete block
    type. When published, the SoftBlock expands into the equivalent discrete
    block chain — there is no SoftBlock at runtime.

    This eliminates the semantic duplication between "workflow composition"
    and "block strategy selection": SoftBlocks exist only in the draft editor;
    published workflows contain only discrete blocks.
    """

    strategy: str = Field(
        default="context_assemble",
        description="Which discrete block behavior to use. Chosen at design time.",
    )
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific configuration.",
    )


# ── Strategy helpers ────────────────────────────────────────────

def get_family(strategy: str) -> str | None:
    """Return the family name for a strategy, or None."""
    for family, strategies in FAMILY_MAP.items():
        if strategy in strategies:
            return family
    return None


def get_discrete_block_type(strategy: str) -> str | None:
    """Map a soft-block strategy to its discrete block type."""
    for strategies in FAMILY_MAP.values():
        if strategy in strategies:
            return strategies[strategy]
    return None


def list_strategies(family: str | None = None) -> list[str]:
    """List available strategies, optionally filtered by family."""
    if family and family in FAMILY_MAP:
        return list(FAMILY_MAP[family].keys())
    result = []
    for strategies in FAMILY_MAP.values():
        result.extend(strategies.keys())
    return result


def strategy_help(strategy: str) -> str:
    """Human-readable description of a strategy."""
    helps = {
        "context_assemble": "Compose fragments and inputs into model-ready context.",
        "context_inject_workspace": "Attach workspace scope and file hints.",
        "context_memory": "Carry conversation facts between turns.",
        "context_compact": "Compact long context, preserving key decisions.",
        "model_call": "Execute one model turn with optional tools.",
        "model_router": "Parse model output and route tool-use intents.",
        "model_stop_continue": "Decide whether to stop or continue the loop.",
        "model_classify_error": "Classify errors as retryable, permission, tool, or fatal.",
        "tool_execute": "Execute a registered tool in sandbox.",
        "tool_normalize": "Normalize raw tool output into stable structure.",
        "tool_permission_gate": "Pause for approval before sensitive steps.",
        "tool_sandbox": "Declare workspace and network boundaries.",
        "gov_budget": "Stop/continue based on token/cost budgets.",
        "gov_rounds": "Enforce maximum loop rounds.",
        "gov_checkpoint": "Save resumable state for recovery.",
        "gov_cancel": "Expose a cancellable checkpoint.",
        "gov_record": "Write structured trace events.",
        "agent_spawn_subagent": "Create a subagent with independent context.",
        "agent_dispatch_tasks": "Assign tasks by dependency order.",
        "agent_mailbox": "Wait/wake on messages.",
        "agent_dependency_gate": "Block until dependencies complete.",
        "skill_load": "Load named skill instructions.",
        "skill_mcp_gateway": "Connect MCP server and discover tools.",
        "skill_capability_registry": "Aggregate all capability sources.",
    }
    return helps.get(strategy, strategy)
