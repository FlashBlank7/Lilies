"""Block family definitions — semantic grouping for discoverability.

Design rationale (from ADR-001 and the "Primitive Is The Pair" thesis):
  The 25 agent-architecture blocks are NOT merged into fewer runtime types.
  Instead they are grouped into 6 *families* for search, browsing, and the
  Orchestration Advisor.  A family is a *property of a block*, NOT a block
  itself.  There is no ``block_family`` runtime type — each family member is
  a fully discrete, independently testable block.

Families:
  context     — assemble, inject workspace, carry memory, compact
  model       — call, route tool intents, stop/continue, classify errors
  tool        — execute, normalize results, permission gate, sandbox boundary
  governance  — budget, rounds, checkpoint, cancel, event recorder
  agent       — spawn subagent, dispatch tasks, mailbox, dependency gate
  skill       — load skills, MCP gateway, capability registry
"""

from __future__ import annotations

from typing import Literal

# ── Family definitions ────────────────────────────────────────────

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

BlockFamilyStrategy = (
    ContextStrategy | ModelStrategy | ToolStrategy
    | GovernanceStrategy | AgentStrategy | SkillStrategy
)


# ── Family → block-type mapping ───────────────────────────────────

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


# ── Reverse mapping: block-type → family ──────────────────────────

def get_family(block_type: str) -> str | None:
    """Return the family name for a discrete block type, or None."""
    for family, strategies in FAMILY_MAP.items():
        if block_type in strategies.values():
            return family
    return None


def get_strategy(block_type: str) -> str | None:
    """Return the strategy name (e.g. 'model_call') for a block type, or None."""
    for strategies in FAMILY_MAP.values():
        for strategy, bt in strategies.items():
            if bt == block_type:
                return strategy
    return None


def get_discrete_block_type(strategy: str) -> str | None:
    """Map a strategy name to its discrete block type."""
    for strategies in FAMILY_MAP.values():
        if strategy in strategies:
            return strategies[strategy]
    return None


def list_strategies(family: str | None = None) -> list[str]:
    """List available strategies, optionally filtered by family."""
    if family and family in FAMILY_MAP:
        return list(FAMILY_MAP[family].keys())
    result: list[str] = []
    for strategies in FAMILY_MAP.values():
        result.extend(strategies.keys())
    return result


def list_families() -> list[str]:
    """List all family names."""
    return list(FAMILY_MAP.keys())


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
