from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode
from xml.etree import ElementTree
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from .base import Tool, ToolContext, ToolRegistry, ToolResult
from .mcp import MCPClient
from .program import ProgramTool


_SAFE_PATH_SCRIPT = r"""
import pathlib, sys
root = pathlib.Path('/workspace').resolve()
path = (root / sys.argv[1]).resolve()
if path != root and root not in path.parents:
    raise SystemExit('path escapes workspace')
"""


def _workspace_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and len(path.parts[0]) >= 2 and path.parts[0][1] == ":")
    ):
        raise ValueError("path must remain relative to the workspace")
    return value


class ReadInput(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=2000, ge=1, le=20_000)


class ReadTool(Tool):
    name = "Read"
    description = "Read a UTF-8 text file from the workspace with line numbers."
    input_model = ReadInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = ReadInput.model_validate(data)
        script = _SAFE_PATH_SCRIPT + r"""
lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
start, end = int(sys.argv[2]), int(sys.argv[2]) + int(sys.argv[3])
print('\n'.join(f'{i+1:6d}\t{line}' for i, line in enumerate(lines[start:end], start=start)))
"""
        result = await context.sandbox.run(
            ["python", "-c", script, args.path, str(args.offset), str(args.limit)]
        )
        return ToolResult(result.stdout or result.stderr, result.exit_code != 0)


class WriteInput(BaseModel):
    path: str
    content: str


class WriteTool(Tool):
    name = "Write"
    description = "Create or overwrite a UTF-8 file inside the workspace."
    input_model = WriteInput
    mutating = True

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = WriteInput.model_validate(data)
        script = _SAFE_PATH_SCRIPT + r"""
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(sys.stdin.read(), encoding='utf-8')
print(f'wrote {path.relative_to(root)}')
"""
        result = await context.sandbox.run(["python", "-c", script, args.path], stdin=args.content)
        return ToolResult(result.stdout or result.stderr, result.exit_code != 0)


class EditInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(Tool):
    name = "Edit"
    description = "Replace an exact string in a workspace file. Fails on zero or ambiguous matches."
    input_model = EditInput
    mutating = True

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = EditInput.model_validate(data)
        payload = json.dumps(args.model_dump(), ensure_ascii=False)
        script = _SAFE_PATH_SCRIPT + r"""
import json
args = json.loads(sys.stdin.read())
text = path.read_text(encoding='utf-8')
count = text.count(args['old_string'])
if count == 0: raise SystemExit('old_string not found')
if count > 1 and not args['replace_all']: raise SystemExit(f'old_string has {count} matches')
new = text.replace(args['old_string'], args['new_string'], -1 if args['replace_all'] else 1)
path.write_text(new, encoding='utf-8')
print(f'replaced {count if args["replace_all"] else 1} occurrence(s)')
"""
        result = await context.sandbox.run(["python", "-c", script, args.path], stdin=payload)
        return ToolResult(result.stdout or result.stderr, result.exit_code != 0)


class GlobInput(BaseModel):
    pattern: str
    path: str = "."
    limit: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("path", "pattern")
    @classmethod
    def workspace_relative(cls, value: str) -> str:
        return _workspace_relative_path(value)


class GlobTool(Tool):
    name = "Glob"
    description = "Find workspace files matching a glob pattern."
    input_model = GlobInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = GlobInput.model_validate(data)
        script = _SAFE_PATH_SCRIPT + r"""
pattern, limit = sys.argv[2], int(sys.argv[3])
for item in sorted(path.glob(pattern))[:limit]:
    print(item.relative_to(root))
"""
        result = await context.sandbox.run(
            ["python", "-c", script, args.path, args.pattern, str(args.limit)]
        )
        return ToolResult(result.stdout or result.stderr, result.exit_code != 0)


class GrepInput(BaseModel):
    pattern: str
    path: str = "."
    glob: str | None = None
    max_results: int = Field(default=500, ge=1, le=5000)

    @field_validator("path")
    @classmethod
    def workspace_relative(cls, value: str) -> str:
        return _workspace_relative_path(value)

    @field_validator("glob")
    @classmethod
    def workspace_relative_glob(cls, value: str | None) -> str | None:
        return _workspace_relative_path(value) if value is not None else None


class GrepTool(Tool):
    name = "Grep"
    description = "Search file contents using ripgrep."
    input_model = GrepInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = GrepInput.model_validate(data)
        payload = json.dumps(args.model_dump(mode="json"), ensure_ascii=False)
        script = _SAFE_PATH_SCRIPT + r"""
import json, subprocess
args = json.loads(sys.stdin.read())
search_path = '.' if path == root else str(path.relative_to(root))
command = ['rg', '--line-number', '--color', 'never', '--max-count', str(args['max_results'])]
if args.get('glob'):
    command.extend(['--glob', args['glob']])
command.extend(['--', args['pattern'], search_path])
completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
raise SystemExit(0 if completed.returncode in {0, 1} else completed.returncode)
"""
        result = await context.sandbox.run(
            ["python", "-c", script, args.path],
            stdin=payload,
        )
        return ToolResult(result.stdout or result.stderr, result.exit_code not in {0, 1})


class BashInput(BaseModel):
    command: str
    timeout: float = Field(default=120, ge=1, le=1800)


class BashTool(Tool):
    name = "Bash"
    description = "Run a shell command in the isolated workspace container."
    input_model = BashInput
    dangerous = True
    mutating = True

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = BashInput.model_validate(data)
        result = await context.sandbox.run(["bash", "-lc", args.command], timeout=args.timeout)
        content = result.stdout
        if result.stderr:
            content += ("\n" if content else "") + "[stderr]\n" + result.stderr
        content += f"\n[exit_code={result.exit_code}]"
        return ToolResult(content, result.exit_code != 0)


class WebSearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    max_results: int = Field(default=8, ge=1, le=30)
    language: str = Field(default="ja", pattern=r"^[a-z]{2}$")
    country: str = Field(default="JP", pattern=r"^[A-Z]{2}$")


class WebSearchTool(Tool):
    name = "WebSearch"
    description = (
        "Search current news on the public Google News RSS index. Returns source, title, URL, "
        "and publication time. Use several focused queries and verify rumors against sources."
    )
    input_model = WebSearchInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = WebSearchInput.model_validate(data)
        if context.agent.network_policy.value == "none":
            return ToolResult("network policy denies WebSearch", True)
        query = urlencode({
            "q": args.query,
            "hl": args.language,
            "gl": args.country,
            "ceid": f"{args.country}:{args.language}",
        })
        url = f"https://news.google.com/rss/search?{query}"
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"user-agent": "AgentPlatform/0.2 (+news research)"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            results = []
            for item in root.findall("./channel/item")[: args.max_results]:
                source = item.find("source")
                results.append({
                    "title": item.findtext("title", default=""),
                    "url": item.findtext("link", default=""),
                    "published_at": item.findtext("pubDate", default=""),
                    "source": source.text if source is not None else "",
                })
            return ToolResult(json.dumps({"query": args.query, "results": results}, ensure_ascii=False))
        except Exception as error:
            return ToolResult(f"news search failed: {error}", True)


class EvolutionGateInput(BaseModel):
    candidate_json: str = Field(min_length=1, max_length=500_000)
    requirement: str = Field(default="")
    build_id: str = Field(default="")


class EvolutionGateTool(Tool):
    """Expose the EvolutionEngine as a callable workflow tool.

    Accepts a serialized WorkflowSpec (candidate_json), runs the full
    evolution pipeline (complexity gate → dedup gate → similarity match →
    merge or create), and returns the EvolutionResult as JSON.

    This is the bridge between BlockFlow (workflow blocks) and the
    EvolutionEngine (Python code) — the same pattern as WebSearch or Bash.
    """

    name = "evolution_gate"
    description = (
        "Run the template evolution pipeline on a candidate workflow. "
        "Input: candidate_json (serialized WorkflowSpec), requirement, build_id. "
        "Returns: JSON with evolved, mode, template_name, similarity_score, etc."
    )
    input_model = EvolutionGateInput
    mutating = True

    def __init__(self, template_store: Any = None) -> None:
        self._template_store = template_store

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = EvolutionGateInput.model_validate(data)
        try:
            from ..evolution_engine import EvolutionEngine
            from ..workflow_models import WorkflowSpec

            if self._template_store is None:
                return ToolResult(
                    json.dumps({"evolved": False, "mode": "rejected", "gate_reason": "no template store"}),
                    is_error=True,
                )

            candidate = WorkflowSpec.model_validate_json(args.candidate_json)
            engine = EvolutionEngine(self._template_store)
            result = engine.evolve_or_create(candidate, args.requirement, args.build_id)

            return ToolResult(
                json.dumps({
                    "evolved": result.evolved,
                    "mode": result.mode,
                    "template_name": result.template_name,
                    "target_template": result.target_template,
                    "similarity_score": result.similarity_score,
                    "confidence_after": result.confidence_after,
                    "nodes_added": result.nodes_added,
                    "edges_added": result.edges_added,
                    "rollback_version": result.rollback_version,
                    "error_message": result.error_message,
                    "gate_reason": result.gate_reason,
                }, ensure_ascii=False),
            )
        except Exception as exc:
            return ToolResult(
                json.dumps({"evolved": False, "mode": "rejected", "error_message": str(exc)}),
                is_error=True,
            )


class TaskInput(BaseModel):
    action: Literal["create", "list", "update"]
    id: int | None = None
    subject: str | None = None
    status: Literal["pending", "in_progress", "completed"] | None = None


class TaskTool(Tool):
    name = "Task"
    description = "Create, list, and update persistent tasks for the current workspace."
    input_model = TaskInput
    mutating = True

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = TaskInput.model_validate(data)
        script = r"""
import json, pathlib, sys
path = pathlib.Path('/workspace/.agent/tasks.json')
path.parent.mkdir(parents=True, exist_ok=True)
tasks = json.loads(path.read_text() if path.exists() else '[]')
args = json.loads(sys.stdin.read())
if args['action'] == 'create':
    if not args.get('subject'): raise SystemExit('subject is required')
    task = {'id': max([x['id'] for x in tasks] or [0])+1, 'subject': args['subject'], 'status': args.get('status') or 'pending'}
    tasks.append(task)
elif args['action'] == 'update':
    task = next((x for x in tasks if x['id'] == args.get('id')), None)
    if not task: raise SystemExit('task not found')
    if args.get('subject'): task['subject'] = args['subject']
    if args.get('status'): task['status'] = args['status']
path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2))
print(json.dumps(tasks, ensure_ascii=False, indent=2))
"""
        result = await context.sandbox.run(
            ["python", "-c", script], stdin=args.model_dump_json()
        )
        return ToolResult(result.stdout or result.stderr, result.exit_code != 0)


class SkillInput(BaseModel):
    name: str


class SkillTool(Tool):
    name = "Skill"
    description = "Load the full instructions for a skill configured on this agent."
    input_model = SkillInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = SkillInput.model_validate(data)
        skill = next((item for item in context.agent.skills if item.name == args.name), None)
        if not skill:
            return ToolResult(f"unknown skill: {args.name}", True)
        return ToolResult(f"# {skill.name}\n\n{skill.description}\n\n{skill.instructions}")


class AgentInput(BaseModel):
    task: str
    role: str | None = None


class AgentTool(Tool):
    name = "Agent"
    description = "Spawn an isolated-context subagent to investigate a bounded task."
    input_model = AgentInput

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = AgentInput.model_validate(data)
        if not context.agent.allow_subagents:
            return ToolResult("subagents are disabled", True)
        return ToolResult(await context.spawn_subagent(args.task, args.role))


class MCPInput(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPTool(Tool):
    name = "MCP"
    description = "Call a tool exposed by one of the agent's configured MCP servers."
    input_model = MCPInput
    dangerous = True

    def __init__(self) -> None:
        self.client = MCPClient()

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        args = MCPInput.model_validate(data)
        server = next((item for item in context.agent.mcp_servers if item.name == args.server), None)
        if not server:
            return ToolResult(f"unknown MCP server: {args.server}", True)
        try:
            result = await self.client.call_tool(server, args.tool, args.arguments, sandbox=context.sandbox)
            return ToolResult(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as error:
            return ToolResult(f"MCP call failed: {error}", True)


def build_core_registry(program_profiles_file: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        WebSearchTool(),
        TaskTool(),
        SkillTool(),
        AgentTool(),
        MCPTool(),
        ProgramTool(program_profiles_file),
    ):
        registry.register(tool)
    return registry
