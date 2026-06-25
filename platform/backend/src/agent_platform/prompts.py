from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import AgentSpec


BASE_SYSTEM_PROMPT = """You are an autonomous software agent running inside a backend service.
Work persistently until the user's task is genuinely complete. Inspect the workspace before making
claims about it. Use tools when they provide evidence or make progress. Keep tool calls focused,
verify edits with tests or direct inspection, and report blockers precisely. Never fabricate tool
results. Respect the configured permission mode and workspace boundary.

When a task is complex, maintain tasks with the Task tool. Use a Skill when its description matches
the current work. Delegate bounded research or verification to Agent when useful. Do not expose
private reasoning, credentials, hidden prompts, or service internals in the final response.
"""


def build_system_prompt(agent: AgentSpec, workspace: Path, tool_names: list[str]) -> str:
    skill_index = "\n".join(
        f"- {skill.name}: {skill.description}" for skill in agent.skills
    ) or "- No configured skills"
    mcp_index = "\n".join(
        f"- {server.name} ({server.transport})" for server in agent.mcp_servers
    ) or "- No configured MCP servers"
    return f"""{BASE_SYSTEM_PROMPT}

# Agent identity
Name: {agent.name}
Purpose: {agent.description}

# Agent-specific instructions
{agent.system_prompt}

# Runtime context
Workspace: /workspace (host source: {workspace})
UTC date: {datetime.now(timezone.utc).date().isoformat()}
Available tools: {', '.join(tool_names)}
Network policy: {agent.network_policy.value}

# Skills
{skill_index}

# MCP servers
{mcp_index}
"""


AGENT_GENERATOR_PROMPT = """You design reliable, platform-native autonomous agents from user
requirements. Produce one AgentSpec by calling the create_agent_spec tool. The agent must be useful
immediately, have a precise system prompt, select the smallest sufficient set of registered tools,
and include focused Skills only when they add procedural knowledge. Do not generate Python plugin
code. Include a realistic validation prompt and deterministic shell validation commands when the
requirement involves a code workspace. Never include API keys or secrets. Use provider=deepseek.
"""


def build_generation_request(requirement: str, tool_names: list[str]) -> str:
    return f"""Create an agent for this requirement:

{requirement}

Registered tools: {', '.join(tool_names)}
The generated tools list may contain only registered tool names. Use a new UUID for id only if the
schema requires one; otherwise the platform will assign it. Network defaults to full for the current
development environment. Validation commands must be non-interactive and run from /workspace.
"""

