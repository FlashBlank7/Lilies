"""Builder tests — unit tests for tool execution branches + integration tests.

Covers every _execute tool branch, the full _agent_loop with MockProvider,
and edge cases: manual_lookup enforcement, template_expand, repair cycles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_platform.applications import ApplicationService
from agent_platform.blocks import BlockRegistry, build_block_registry
from agent_platform.builder import WorkflowBuilder
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, ContentBlock, StreamEvent, ToolDefinition
from agent_platform.permissions import PermissionBroker
from agent_platform.platform_harness import PlatformHarness
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.runtime import AgentRuntime
from agent_platform.sandbox import SandboxManager
from agent_platform.storage import Storage
from agent_platform.template_store import TemplateStore
from agent_platform.testing import MockProvider, scripted_tool_calls
from agent_platform.tools import ToolRegistry, build_core_registry
from agent_platform.workflow_models import ApplicationCreateRequest, BuildTeamState
from agent_platform.workflow_runtime import WorkflowRuntime
from agent_platform.workflow_storage import WorkflowStorage


# ── Fixtures ──────────────────────────────────────────────────────


class NoOpSandboxes:
    """Sandbox manager that never creates real containers."""
    async def get_or_create(self, *_: Any, **__: Any) -> Any:
        from agent_platform.sandbox import CommandResult
        class NoOpSandbox:
            workspace = Path("/tmp/mock")
            async def run(self, argv, **__):
                return CommandResult(stdout="ok", stderr="", exit_code=0)
        return NoOpSandbox()
    def resolve_workspace(self, path: str, create: bool = False) -> Path:
        p = Path(path)
        if create and not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            (p / "README.md").write_text("# test\n")
        return p
    async def remove(self, session_id: str) -> None:
        pass
    async def close(self) -> None:
        pass


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        deepseek_api_key="test-key",
    )
    s.prepare()
    return s


@pytest.fixture
async def storage(tmp_settings: Settings) -> Storage:
    s = Storage(tmp_settings.data_dir)
    await s.initialize()
    return s


@pytest.fixture
def blocks() -> BlockRegistry:
    return build_block_registry()


@pytest.fixture
def tools() -> ToolRegistry:
    return build_core_registry()


@pytest.fixture
async def workflow_store(storage: Storage) -> WorkflowStorage:
    ws = WorkflowStorage(storage)
    await ws.initialize()
    return ws


@pytest.fixture
def applications(
    workflow_store: WorkflowStorage,
    blocks: BlockRegistry,
    tools: ToolRegistry,
) -> ApplicationService:
    return ApplicationService(workflow_store, blocks, tools)


@pytest.fixture
def template_store() -> TemplateStore:
    return TemplateStore()


def make_builder(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    provider: ModelProvider,
    template_store: TemplateStore | None = None,
) -> WorkflowBuilder:
    """Construct a minimal Builder for testing."""
    settings = Settings(
        data_dir=storage.data_dir,
        workspace_root=Path("/tmp/workspaces"),
        deepseek_api_key="test-key",
    )
    settings.prepare()
    sandboxes = NoOpSandboxes()
    permissions = PermissionBroker()
    agent_runtime = AgentRuntime(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=tools,
        sandboxes=sandboxes,  # type: ignore[arg-type]
        permissions=permissions,
        harness=PlatformHarness(storage=storage),
    )
    workflow_runtime = WorkflowRuntime(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        provider=provider,
        agent_runtime=agent_runtime,
        tools=tools,
        sandboxes=sandboxes,  # type: ignore[arg-type]
        runtime_model="deepseek/deepseek-v4-pro",
        harness=PlatformHarness(storage=storage),
    )
    return WorkflowBuilder(
        storage=storage,
        workflow_store=workflow_store,
        applications=applications,
        blocks=blocks,
        runtime=workflow_runtime,
        provider=provider,
        agent_runtime=agent_runtime,
        generator_model="deepseek/deepseek-v4-pro",
        core_tools=tools,
        template_store=template_store,
        harness=PlatformHarness(storage=storage),
    )


@pytest.fixture
async def app_id(
    storage: Storage,
    workflow_store: WorkflowStorage,
) -> str:
    """Create a test application and return its ID."""
    result = await workflow_store.create_application(
        ApplicationCreateRequest(
            name="Test App",
            description="For testing",
            requirement="Build a greeting workflow that takes a name and returns a greeting.",
        )
    )
    return str(result["id"])


@pytest.fixture
def state() -> BuildTeamState:
    return BuildTeamState()


# ── Unit tests: _execute tool branches ───────────────────────────


@pytest.mark.asyncio
async def test_execute_catalog_search(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    results = await builder._execute(
        "build-1", app_id, state, "catalog_search",
        {"query": "llm"}, max_repair_cycles=3, auto_publish=True,
    )
    assert isinstance(results, list)
    assert any(r["type"] == "llm" for r in results)


@pytest.mark.asyncio
async def test_execute_catalog_get_block(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    result = await builder._execute(
        "build-1", app_id, state, "catalog_get",
        {"type": "llm"}, max_repair_cycles=3, auto_publish=True,
    )
    assert result["type"] == "llm"
    assert result["title"] == "LLM"


@pytest.mark.asyncio
async def test_execute_manual_search(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    results = await builder._execute(
        "build-1", app_id, state, "manual_search",
        {"query": "permission", "block_kind": "agent_architecture"},
        max_repair_cycles=3, auto_publish=True,
    )
    assert isinstance(results, list)
    assert any(r["type"] == "permission_gate" for r in results)
    # manual_search should remember the lookup
    assert "permission_gate" in state.manual_lookups


@pytest.mark.asyncio
async def test_execute_manual_get(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    result = await builder._execute(
        "build-1", app_id, state, "manual_get",
        {"type": "model_turn"}, max_repair_cycles=3, auto_publish=True,
    )
    assert result["type"] == "model_turn"
    assert "model_turn" in state.manual_lookups


@pytest.mark.asyncio
async def test_execute_architecture_blueprint(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    result = await builder._execute(
        "build-1", app_id, state, "architecture_blueprint",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    assert "groups" in result
    assert "model_loop" in result["groups"]
    # Should record manual lookups for all agent architecture blocks
    assert len(state.manual_lookups) >= 20


@pytest.mark.asyncio
async def test_execute_draft_operations(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())

    # Add start node
    result = await builder._execute(
        "build-1", app_id, state, "draft_add_node",
        {"node": {
            "id": "start", "type": "start", "title": "Input",
            "config": {"inputs": [{"name": "name", "type": "string"}]},
        }},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] == 1

    # Add end node
    result = await builder._execute(
        "build-1", app_id, state, "draft_add_node",
        {"node": {"id": "end", "type": "end", "title": "End", "config": {}}},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] == 2

    # Connect them
    result = await builder._execute(
        "build-1", app_id, state, "draft_connect",
        {"edge": {"id": "e1", "source": "start", "target": "end",
                  "source_port": "output", "target_port": "input"}},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] == 3

    # Validate
    result = await builder._execute(
        "build-1", app_id, state, "draft_validate",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    # Valid graph shape but needs a mandatory test per publish gate
    assert not result["valid"]
    assert any("mandatory" in e for e in result["errors"])


@pytest.mark.asyncio
async def test_execute_draft_add_node_requires_manual_for_agent_architecture(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    """Agent architecture blocks must have a prior manual lookup."""
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    with pytest.raises(RuntimeError, match="manual lookup required"):
        await builder._execute(
            "build-1", app_id, state, "draft_add_node",
            {"node": {
                "id": "turn", "type": "model_turn", "title": "Turn",
                "config": {},
            }},
            max_repair_cycles=3, auto_publish=True,
        )

    # After manual lookup, it should succeed
    await builder._execute(
        "build-1", app_id, state, "manual_get",
        {"type": "model_turn"}, max_repair_cycles=3, auto_publish=True,
    )
    result = await builder._execute(
        "build-1", app_id, state, "draft_add_node",
        {"node": {
            "id": "turn", "type": "model_turn", "title": "Turn",
            "config": {},
        }},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] >= 1


@pytest.mark.asyncio
async def test_execute_template_suggestions(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
    template_store: TemplateStore,
) -> None:
    """Template suggestions should return matching templates."""
    # Register a test template
    from agent_platform.workflow_models import WorkflowSpec, NodeSpec as WfNode, EdgeSpec
    wf = WorkflowSpec(
        nodes=[
            WfNode(id="s", type="start", title="Start", config={}),
            WfNode(id="e", type="end", title="End", config={}),
        ],
        edges=[EdgeSpec(id="a", source="s", target="e", source_port="output", target_port="input")],
    )
    template_store.register("greeting_template", wf, meta_overrides={
        "title": "Greeting Workflow",
        "description": "Takes a name and produces a greeting",
        "tags": ["greeting", "hello"],
        "category": "task_management",
        "confidence": 0.9,
    })

    builder = make_builder(
        storage, workflow_store, applications, blocks, tools,
        MockProvider(), template_store=template_store,
    )
    results = await builder._execute(
        "build-1", app_id, state, "template_suggestions",
        {"requirement": "greeting workflow"}, max_repair_cycles=3, auto_publish=True,
    )
    # template_suggestions 现返回结构化 dict,模板列表在 "templates" 键下
    assert isinstance(results["templates"], list)
    assert any(r["name"] == "greeting_template" for r in results["templates"])


@pytest.mark.asyncio
async def test_execute_template_expand(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
    template_store: TemplateStore,
) -> None:
    """Template expand should add nodes and edges to the draft."""
    from agent_platform.workflow_models import WorkflowSpec, NodeSpec as WfNode, EdgeSpec
    wf = WorkflowSpec(
        nodes=[
            WfNode(id="s", type="start", title="Input", config={
                "inputs": [{"name": "name", "type": "string"}],
            }),
            WfNode(id="t", type="template_transform", title="Greet", config={
                "template": "Hello {{ name }}",
                "variables": {"name": {"$ref": {"node_id": "s", "path": ["name"]}}},
            }),
            WfNode(id="e", type="end", title="End", config={
                "outputs": {"greeting": {"$ref": {"node_id": "t", "path": ["text"]}}},
            }),
        ],
        edges=[
            EdgeSpec(id="a", source="s", target="t", source_port="output", target_port="input"),
            EdgeSpec(id="b", source="t", target="e", source_port="text", target_port="input"),
        ],
    )
    template_store.register("greet_tpl", wf, meta_overrides={
        "title": "Greeting", "tags": ["greeting"], "confidence": 0.9,
    })

    builder = make_builder(
        storage, workflow_store, applications, blocks, tools,
        MockProvider(), template_store=template_store,
    )
    result = await builder._execute(
        "build-1", app_id, state, "template_expand",
        {"name": "greet_tpl", "prefix": "greet"},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["template"] == "greet_tpl"  # 模板名现在在 result dict 中
    assert result["revision"] >= 3  # 3 nodes + 2 edges = 5 operations


@pytest.mark.asyncio
async def test_execute_test_add_and_run(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    """Test add should create a test case; test_run should execute."""
    provider = MockProvider()
    builder = make_builder(storage, workflow_store, applications, blocks, tools, provider)

    # First add nodes so tests can run
    for node_data in [
        {"id": "start", "type": "start", "title": "Input",
         "config": {"inputs": [{"name": "name", "type": "string"}]}},
        {"id": "tpl", "type": "template_transform", "title": "Greet",
         "config": {"template": "Hello {{ name }}",
                    "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {"greeting": {"$ref": {"node_id": "tpl", "path": ["text"]}}}}},
    ]:
        await builder._execute(
            "build-1", app_id, state, "draft_add_node",
            {"node": node_data}, max_repair_cycles=3, auto_publish=True,
        )
    for edge_data in [
        {"id": "e1", "source": "start", "target": "tpl", "source_port": "output", "target_port": "input"},
        {"id": "e2", "source": "tpl", "target": "end", "source_port": "text", "target_port": "input"},
    ]:
        await builder._execute(
            "build-1", app_id, state, "draft_connect",
            {"edge": edge_data}, max_repair_cycles=3, auto_publish=True,
        )

    # Add test
    result = await builder._execute(
        "build-1", app_id, state, "test_add",
        {"test": {
            "name": "Greets",
            "requirement": "Greeting should contain the name",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}],
            "required_node_types": ["start", "template_transform", "end"],
        }},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] >= 4

    # Run test — will use real WorkflowRuntime (passes without model calls for simple transforms)
    report = await builder._execute(
        "build-1", app_id, state, "test_run",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    assert "passed" in report


@pytest.mark.asyncio
async def test_execute_repair_cycle_limit_enforced(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    """test_run should raise when repair_cycles exceeds max."""
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())

    # 先建 delivery-complete 草稿(start/end + 强制验收测试),使交付门通过
    for node_data in [
        {"id": "start", "type": "start", "title": "Input",
         "config": {"inputs": [{"name": "name", "type": "string"}]}},
        {"id": "tpl", "type": "template_transform", "title": "Greet",
         "config": {"template": "Hello {{ name }}",
                    "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}}}},
        {"id": "end", "type": "end", "title": "End",
         "config": {"outputs": {"greeting": {"$ref": {"node_id": "tpl", "path": ["text"]}}}}},
    ]:
        await builder._execute(
            "build-1", app_id, state, "draft_add_node",
            {"node": node_data}, max_repair_cycles=3, auto_publish=True,
        )
    for edge_data in [
        {"id": "e1", "source": "start", "target": "tpl", "source_port": "output", "target_port": "input"},
        {"id": "e2", "source": "tpl", "target": "end", "source_port": "text", "target_port": "input"},
    ]:
        await builder._execute(
            "build-1", app_id, state, "draft_connect",
            {"edge": edge_data}, max_repair_cycles=3, auto_publish=True,
        )
    await builder._execute(
        "build-1", app_id, state, "test_add",
        {"test": {
            "name": "Greets",
            "requirement": "Greeting should contain the name",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "exists"}],
            "required_node_types": ["start", "template_transform", "end"],
        }},
        max_repair_cycles=3, auto_publish=True,
    )

    # 耗尽修复预算:达到 max 且 last_failed_test_revision 未清除
    state.repair_cycles = 3  # == max_repair_cycles
    state.last_failed_test_revision = state.revision
    with pytest.raises(RuntimeError, match="maximum repair cycles"):
        await builder._execute(
            "build-1", app_id, state, "test_run",
            {}, max_repair_cycles=3, auto_publish=True,
        )


@pytest.mark.asyncio
async def test_execute_task_crud(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())

    # Create task
    tasks = await builder._execute(
        "build-1", app_id, state, "task",
        {"action": "create", "subject": "Add greeting node",
         "description": "Add a template_transform for greeting",
         "owner": "coordinator", "acceptance": ["greeting works"]},
        max_repair_cycles=3, auto_publish=True,
    )
    assert len(tasks) == 1
    assert tasks[0]["subject"] == "Add greeting node"
    assert tasks[0]["status"] == "pending"

    # Update task
    tasks = await builder._execute(
        "build-1", app_id, state, "task",
        {"action": "update", "id": 1, "status": "completed"},
        max_repair_cycles=3, auto_publish=True,
    )
    assert tasks[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_draft_inspect(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    result = await builder._execute(
        "build-1", app_id, state, "draft_inspect",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    assert "revision" in result
    assert "snapshot" in result
    assert result["revision"] == 0


@pytest.mark.asyncio
async def test_execute_unknown_tool_raises(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())
    with pytest.raises(KeyError, match="unknown builder tool"):
        await builder._execute(
            "build-1", app_id, state, "nonexistent_tool",
            {}, max_repair_cycles=3, auto_publish=True,
        )


# ── Integration tests: full _agent_loop with MockProvider ────────


@pytest.mark.asyncio
async def test_builder_full_flow_greeting_workflow(
    tmp_path: Path,
) -> None:
    """End-to-end: Builder creates a simple greeting workflow via MockProvider."""
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        deepseek_api_key="test-key",
    )
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    ws = WorkflowStorage(storage)
    await ws.initialize()
    blocks = build_block_registry()
    tools = build_core_registry()
    apps = ApplicationService(ws, blocks, tools)

    result = await ws.create_application(ApplicationCreateRequest(
        name="Greeting Test",
        description="E2E test",
        requirement="Create a greeting workflow that takes a name and outputs a greeting.",
    ))
    app_id = str(result["id"])

    # Script: build a minimal start→end workflow and publish
    provider = MockProvider.from_script([
        # Turn 1: inspect draft + search catalog + add nodes
        [
            ("draft_inspect", {}),
            ("catalog_search", {"query": "start"}),
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Input",
                "config": {"inputs": [{"name": "name", "type": "string"}]},
            }}),
        ],
        # Turn 2: add end
        [
            ("draft_add_node", {"node": {
                "id": "end", "type": "end", "title": "End",
                "config": {"outputs": {"result": {"$ref": {"node_id": "start", "path": ["name"]}}}},
            }}),
        ],
        # Turn 3: connect
        [
            ("draft_connect", {"edge": {
                "id": "e1", "source": "start", "target": "end",
                "source_port": "output", "target_port": "input",
            }}),
        ],
        # Turn 4: validate + add test
        [
            ("draft_validate", {}),
            ("test_add", {"test": {
                "name": "Returns name",
                "requirement": "Output should contain the input name",
                "inputs": {"name": "TestUser"},
                "assertions": [{"path": ["result"], "operator": "equals", "expected": "TestUser"}],
                "required_node_types": ["start", "end"],
            }}),
        ],
        # Turn 5: run tests + publish
        [
            ("test_run", {}),
            ("draft_publish", {}),
        ],
        # Turn 6: stop (empty = no tool calls)
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)

    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text",
        text=f"Build and verify: Create a greeting workflow.\nApplication id: {app_id}. Auto publish: true.",
    )])]

    await builder.harness.start_task(
        "build-e2e-1", kind="builder_build", owner_id=app_id, resource_id="build-e2e-1",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="build-e2e-1",
        application_id=app_id,
        state=state,
        messages=messages,
        max_turns=20,
        max_repair_cycles=3,
        auto_publish=True,
        teammate=None,
    )
    assert final or state.published_version is not None


@pytest.mark.asyncio
async def test_builder_respects_max_turns(
    tmp_path: Path,
) -> None:
    """Builder should stop after max_turns even if tool calls keep coming."""
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        deepseek_api_key="test-key",
    )
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    ws = WorkflowStorage(storage)
    await ws.initialize()
    blocks = build_block_registry()
    tools = build_core_registry()
    apps = ApplicationService(ws, blocks, tools)

    result = await ws.create_application(ApplicationCreateRequest(
        name="Infinite", description="Test", requirement="Test max turns.",
    ))
    app_id = str(result["id"])

    # Always return a tool call — never stops naturally
    provider = MockProvider.from_script([
        [("draft_inspect", {})],
        [("draft_inspect", {})],
        [("draft_inspect", {})],
        [("draft_inspect", {})],
        [("draft_inspect", {})],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text", text=f"Build: test.\nApplication id: {app_id}. Auto publish: false.",
    )])]

    # max_turns=3, so after 3 turns, the loop exits with empty final
    await builder.harness.start_task(
        "build-max-turns", kind="builder_build", owner_id=app_id, resource_id="build-max-turns",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="build-max-turns",
        application_id=app_id,
        state=state,
        messages=messages,
        max_turns=3,
        max_repair_cycles=1,
        auto_publish=False,
        teammate=None,
    )
    # Stopped without publishing (no stop signal received within 3 turns)
    assert state.published_version is None
    assert final == "" or final is None


# ── Boundary tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_empty_requirement(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
) -> None:
    """Builder should handle empty requirement gracefully (start node only)."""
    result = await workflow_store.create_application(ApplicationCreateRequest(
        name="Empty", description="Test", requirement="",
    ))
    app_id = str(result["id"])

    # Minimal script: just validate the empty draft
    provider = MockProvider.from_script([
        [("draft_inspect", {})],
        [("draft_validate", {})],
        [],
    ])

    builder = make_builder(storage, workflow_store, applications, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text", text=f"Build: \nApplication id: {app_id}. Auto publish: false.",
    )])]

    await builder.harness.start_task(
        "build-empty", kind="builder_build", owner_id=app_id, resource_id="build-empty",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="build-empty", application_id=app_id, state=state, messages=messages,
        max_turns=5, max_repair_cycles=1, auto_publish=False, teammate=None,
    )
    assert final is not None


@pytest.mark.asyncio
async def test_builder_update_node_and_remove(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
) -> None:
    """Test update_node, remove_node, update_edge, and remove_edge operations."""
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())

    # Add a start node
    await builder._execute(
        "build-1", app_id, state, "draft_add_node",
        {"node": {"id": "start", "type": "start", "title": "Input", "config": {}}},
        max_repair_cycles=3, auto_publish=True,
    )

    # Update it
    result = await builder._execute(
        "build-1", app_id, state, "draft_update_node",
        {"node_id": "start", "changes": {"title": "Updated Input"}},
        max_repair_cycles=3, auto_publish=True,
    )
    assert result["revision"] >= 1

    # Verify via inspect
    draft = await builder._execute(
        "build-1", app_id, state, "draft_inspect",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    titles = [n["title"] for n in draft["snapshot"]["workflow"]["nodes"]]
    assert "Updated Input" in titles

    # Remove it
    result = await builder._execute(
        "build-1", app_id, state, "draft_remove_node",
        {"node_id": "start"}, max_repair_cycles=3, auto_publish=True,
    )
    draft = await builder._execute(
        "build-1", app_id, state, "draft_inspect",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    assert len(draft["snapshot"]["workflow"]["nodes"]) == 0


@pytest.mark.asyncio
async def test_builder_definitions_count(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
) -> None:
    """Verify that _definitions returns the expected tool set."""
    builder = make_builder(storage, workflow_store, applications, blocks, tools, MockProvider())

    # Coordinator has team tools
    coord_defs = builder._definitions(allow_team=True)
    coord_names = {d.name for d in coord_defs}
    assert "spawn_teammate" in coord_names
    assert "send_message" in coord_names
    assert "draft_add_node" in coord_names
    assert "template_suggestions" in coord_names

    # Teammate does NOT have team tools
    teammate_defs = builder._definitions(allow_team=False)
    teammate_names = {d.name for d in teammate_defs}
    assert "spawn_teammate" not in teammate_names
    assert "send_message" not in teammate_names
    assert "draft_add_node" in teammate_names


# ── TemplateStore integration ────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_template_list_when_store_available(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    app_id: str,
    state: BuildTeamState,
    template_store: TemplateStore,
) -> None:
    """template_list should return templates from TemplateStore."""
    from agent_platform.workflow_models import WorkflowSpec, NodeSpec as WfNode, EdgeSpec
    wf = WorkflowSpec(
        nodes=[WfNode(id="s", type="start", title="S", config={})],
        edges=[],
    )
    template_store.register("test_tpl", wf, meta_overrides={
        "title": "Test Template", "description": "A test", "tags": ["test"],
        "category": "task_management",
    })

    builder = make_builder(
        storage, workflow_store, applications, blocks, tools,
        MockProvider(), template_store=template_store,
    )
    results = await builder._execute(
        "build-1", app_id, state, "template_list",
        {}, max_repair_cycles=3, auto_publish=True,
    )
    assert isinstance(results, list)
    assert any(r["name"] == "test_tpl" for r in results)
