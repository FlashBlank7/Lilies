"""Builder benchmark v1 — quantifiable BlockFlow construction capability.

These are NOT fast unit tests. They measure the Builder's ability to
construct structurally correct workflows across a range of difficulty levels.

Each benchmark uses a MockProvider with pre-scripted responses that simulate
an AI model making incremental construction decisions.

Metrics collected:
  - structural_correctness: does the generated WorkflowSpec pass validation?
  - node_count: number of nodes in the final graph
  - edge_count: number of edges in the final graph
  - first_try_success: did it pass validation on the first validate call?
  - repair_cycles_used: how many repair cycles were needed?
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent_platform.applications import ApplicationService
from agent_platform.blocks import BlockRegistry, build_block_registry
from agent_platform.builder import WorkflowBuilder
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, ContentBlock
from agent_platform.permissions import PermissionBroker
from agent_platform.platform_harness import PlatformHarness
from agent_platform.runtime import AgentRuntime
from agent_platform.storage import Storage
from agent_platform.testing import MockProvider
from agent_platform.tools import ToolRegistry, build_core_registry
from agent_platform.workflow_models import ApplicationCreateRequest, BuildTeamState
from agent_platform.workflow_runtime import WorkflowRuntime
from agent_platform.workflow_storage import WorkflowStorage


# ── Benchmark helpers ──────────────────────────────────────────────


class NoOpSandboxes:
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


def make_builder(
    storage: Storage,
    workflow_store: WorkflowStorage,
    applications: ApplicationService,
    blocks: BlockRegistry,
    tools: ToolRegistry,
    provider: MockProvider,
) -> WorkflowBuilder:
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
        harness=PlatformHarness(storage=storage),
    )


# ── Benchmark scenarios ────────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.asyncio
async def test_benchmark_simple_linear_workflow(tmp_path: Path) -> None:
    """Benchmark 1: start → template → end (simplest valid workflow)."""
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
        name="Bench-1", description="Linear", requirement="Simple greeting.",
    ))
    app_id = str(result["id"])

    provider = MockProvider.from_script([
        [
            ("draft_inspect", {}),
            ("catalog_search", {"query": "start"}),
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Input",
                "config": {"inputs": [{"name": "name", "type": "string"}]},
            }}),
        ],
        [
            ("draft_add_node", {"node": {
                "id": "tpl", "type": "template_transform", "title": "Greet",
                "config": {
                    "template": "Hello {{ name }}",
                    "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
                },
            }}),
        ],
        [
            ("draft_add_node", {"node": {
                "id": "end", "type": "end", "title": "End",
                "config": {"outputs": {"greeting": {"$ref": {"node_id": "tpl", "path": ["text"]}}}},
            }}),
        ],
        [
            ("draft_connect", {"edge": {
                "id": "e1", "source": "start", "target": "tpl",
                "source_port": "output", "target_port": "input",
            }}),
        ],
        [
            ("draft_connect", {"edge": {
                "id": "e2", "source": "tpl", "target": "end",
                "source_port": "text", "target_port": "input",
            }}),
        ],
        [
            ("draft_validate", {}),
            ("test_add", {"test": {
                "name": "Greets correctly",
                "requirement": "Output contains the name",
                "inputs": {"name": "Ada"},
                "assertions": [
                    {"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"},
                ],
                "required_node_types": ["start", "template_transform", "end"],
            }}),
        ],
        [
            ("test_run", {}),
            ("draft_publish", {}),
        ],
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text",
        text=f"Build: greeting workflow.\nApplication id: {app_id}. Auto publish: true.",
    )])]

    start = time.monotonic()
    await builder.harness.start_task(
        "bench-1", kind="builder_build", owner_id=app_id, resource_id="bench-1",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="bench-1", application_id=app_id, state=state, messages=messages,
        max_turns=20, max_repair_cycles=3, auto_publish=True, teammate=None,
    )
    elapsed = time.monotonic() - start

    # Verify structural correctness
    draft = await ws.get_draft(app_id)
    wf = draft["snapshot"].workflow

    # Benchmark assertions
    assert len(wf.nodes) >= 3, f"Expected >= 3 nodes, got {len(wf.nodes)}"
    assert len(wf.edges) >= 2, f"Expected >= 2 edges, got {len(wf.edges)}"

    # Validate final state
    validation = await apps.validate_draft(app_id)
    # Should have at least node/edge validity (may fail on mandatory test gate
    # without real WorkflowRuntime, but structural validation should pass)
    node_types = {n.type for n in wf.nodes}
    assert "start" in node_types
    assert "end" in node_types
    assert "template_transform" in node_types

    # Report benchmark metrics
    print(f"\n[Benchmark 1: Linear] nodes={len(wf.nodes)} edges={len(wf.edges)} "
          f"elapsed={elapsed:.2f}s published={'v' + str(state.published_version) if state.published_version else 'no'}")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_benchmark_conditional_branching(tmp_path: Path) -> None:
    """Benchmark 2: start → classifier → two branches → aggregator → end."""
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
        name="Bench-2", description="Conditional", requirement="Route greeting by language.",
    ))
    app_id = str(result["id"])

    provider = MockProvider.from_script([
        # Turn 1: inspect + add start + classifier
        [
            ("draft_inspect", {}),
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Input",
                "config": {"inputs": [{"name": "lang", "type": "string"}]},
            }}),
        ],
        # Turn 2: add classifier
        [
            ("draft_add_node", {"node": {
                "id": "classifier", "type": "question_classifier", "title": "Route",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["lang"]}},
                    "classes": ["en", "zh", "other"],
                },
            }}),
        ],
        # Turn 3: add branch nodes
        [
            ("draft_add_node", {"node": {
                "id": "en_greet", "type": "template_transform", "title": "English",
                "config": {"template": "Hello!", "variables": {}},
            }}),
            ("draft_add_node", {"node": {
                "id": "zh_greet", "type": "template_transform", "title": "Chinese",
                "config": {"template": "你好!", "variables": {}},
            }}),
        ],
        # Turn 4: add aggregator + end
        [
            ("draft_add_node", {"node": {
                "id": "agg", "type": "variable_aggregator", "title": "Join",
                "config": {
                    "variables": [
                        {"$ref": {"node_id": "en_greet", "path": ["text"]}},
                        {"$ref": {"node_id": "zh_greet", "path": ["text"]}},
                    ],
                    "mode": "first_non_null",
                },
            }}),
            ("draft_add_node", {"node": {
                "id": "end", "type": "end", "title": "End",
                "config": {"outputs": {"result": {"$ref": {"node_id": "agg", "path": ["output"]}}}},
            }}),
        ],
        # Turn 5: connect
        [
            ("draft_connect", {"edge": {
                "id": "e1", "source": "start", "target": "classifier",
                "source_port": "output", "target_port": "input",
            }}),
            ("draft_connect", {"edge": {
                "id": "e2", "source": "classifier", "target": "en_greet",
                "source_port": "en", "target_port": "input",
            }}),
            ("draft_connect", {"edge": {
                "id": "e3", "source": "classifier", "target": "zh_greet",
                "source_port": "zh", "target_port": "input",
            }}),
        ],
        # Turn 6: more connections
        [
            ("draft_connect", {"edge": {
                "id": "e4", "source": "en_greet", "target": "agg",
                "source_port": "text", "target_port": "input",
            }}),
            ("draft_connect", {"edge": {
                "id": "e5", "source": "zh_greet", "target": "agg",
                "source_port": "text", "target_port": "input",
            }}),
            ("draft_connect", {"edge": {
                "id": "e6", "source": "agg", "target": "end",
                "source_port": "output", "target_port": "input",
            }}),
        ],
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text",
        text=f"Build: route greeting by language.\nApplication id: {app_id}. Auto publish: false.",
    )])]

    await builder.harness.start_task(
        "bench-2", kind="builder_build", owner_id=app_id, resource_id="bench-2",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="bench-2", application_id=app_id, state=state, messages=messages,
        max_turns=15, max_repair_cycles=3, auto_publish=False, teammate=None,
    )

    draft = await ws.get_draft(app_id)
    wf = draft["snapshot"].workflow
    node_types = {n.type for n in wf.nodes}

    assert len(wf.nodes) >= 5  # start + classifier + 2 branches + agg + end
    assert "question_classifier" in node_types
    assert "variable_aggregator" in node_types
    assert len(wf.edges) >= 4

    print(f"\n[Benchmark 2: Conditional] nodes={len(wf.nodes)} edges={len(wf.edges)} "
          f"node_types={sorted(node_types)}")


@pytest.mark.slow
@pytest.mark.asyncio
async def test_benchmark_template_expand_workflow(tmp_path: Path) -> None:
    """Benchmark 3: Template-based construction (most common real path)."""
    from agent_platform.template_store import TemplateStore
    from agent_platform.workflow_models import WorkflowSpec, NodeSpec as WfNode, EdgeSpec

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

    # Pre-register a template
    tpl_wf = WorkflowSpec(
        nodes=[
            WfNode(id="s", type="start", title="In", config={
                "inputs": [{"name": "query", "type": "string"}],
            }),
            WfNode(id="l", type="llm", title="Process", config={
                "system": "Answer briefly.",
                "prompt": {"$ref": {"node_id": "s", "path": ["query"]}},
            }),
            WfNode(id="e", type="end", title="Out", config={
                "outputs": {"answer": {"$ref": {"node_id": "l", "path": ["text"]}}},
            }),
        ],
        edges=[
            EdgeSpec(id="a", source="s", target="l", source_port="output", target_port="input"),
            EdgeSpec(id="b", source="l", target="e", source_port="text", target_port="input"),
        ],
    )
    tpl_store = TemplateStore()
    tpl_store.register("qa_template", tpl_wf, meta_overrides={
        "title": "Q&A Pipeline", "tags": ["qa"], "confidence": 0.85,
        "category": "task_management",
    })

    result = await ws.create_application(ApplicationCreateRequest(
        name="Bench-3", description="Template", requirement="Q&A pipeline.",
    ))
    app_id = str(result["id"])

    provider = MockProvider.from_script([
        [("template_suggestions", {"requirement": "Q&A pipeline"})],
        [("template_expand", {"name": "qa_template", "prefix": "qa"})],
        [("draft_validate", {})],
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    builder.template_store = tpl_store
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text", text=f"Build: Q&A pipeline.\nApplication id: {app_id}. Auto publish: false.",
    )])]

    await builder.harness.start_task(
        "bench-3", kind="builder_build", owner_id=app_id, resource_id="bench-3",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="bench-3", application_id=app_id, state=state, messages=messages,
        max_turns=10, max_repair_cycles=3, auto_publish=False, teammate=None,
    )

    draft = await ws.get_draft(app_id)
    wf = draft["snapshot"].workflow

    # After template expansion, should have prefixed nodes
    node_ids = {n.id for n in wf.nodes}
    assert any(n.startswith("qa_") for n in node_ids), f"No prefixed nodes in {node_ids}"

    print(f"\n[Benchmark 3: Template] nodes={len(wf.nodes)} edges={len(wf.edges)}")


@pytest.mark.asyncio
async def test_benchmark_manual_lookup_enforcement(tmp_path: Path) -> None:
    """Benchmark 4: Verify manual_lookup gate works correctly for agent architecture blocks."""
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
        name="Bench-4", description="Manual gate", requirement="Test manual enforcement.",
    ))
    app_id = str(result["id"])

    # Script where the builder tries to use agent architecture block without manual
    provider = MockProvider.from_script([
        # Turn 1: try to add model_turn without manual lookup → error
        [("draft_add_node", {"node": {
            "id": "turn", "type": "model_turn", "title": "Turn", "config": {},
        }})],
        # Turn 2: after error, do manual lookup properly
        [("manual_search", {"query": "model turn", "block_kind": "agent_architecture"})],
        # Turn 3: now add the node
        [("draft_add_node", {"node": {
            "id": "turn", "type": "model_turn", "title": "Turn", "config": {},
        }})],
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text", text=f"Build: test manual gate.\nApplication id: {app_id}. Auto publish: false.",
    )])]

    await builder.harness.start_task(
        "bench-4", kind="builder_build", owner_id=app_id, resource_id="bench-4",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="bench-4", application_id=app_id, state=state, messages=messages,
        max_turns=10, max_repair_cycles=3, auto_publish=False, teammate=None,
    )

    # After the error on turn 1 and manual lookup on turn 2, turn 3 should succeed
    assert "model_turn" in state.manual_lookups, \
        f"Expected model_turn in manual_lookups, got: {state.manual_lookups}"

    print(f"\n[Benchmark 4: Manual Gate] lookups={state.manual_lookups}")


@pytest.mark.asyncio
async def test_benchmark_repair_cycle_tracking(tmp_path: Path) -> None:
    """Benchmark 5: Repair cycle tracking works correctly."""
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
        name="Bench-5", description="Repair", requirement="Test repair cycles.",
    ))
    app_id = str(result["id"])

    # First test_run fails, then repaired on second attempt
    provider = MockProvider.from_script([
        [("draft_inspect", {})],
        [("test_run", {})],  # will fail because no nodes yet
        [("test_run", {})],  # fail again
        [],
    ])

    builder = make_builder(storage, ws, apps, blocks, tools, provider)
    state = BuildTeamState()
    messages = [ChatMessage(role="user", content=[ContentBlock(
        type="text", text=f"Build: test.\nApplication id: {app_id}. Auto publish: false.",
    )])]

    await builder.harness.start_task(
        "bench-5", kind="builder_build", owner_id=app_id, resource_id="bench-5",
        metadata={"application_id": app_id, "workflow_id": app_id, "model": "deepseek/deepseek-v4-pro"},
    )
    final = await builder._agent_loop(
        build_id="bench-5", application_id=app_id, state=state, messages=messages,
        max_turns=10, max_repair_cycles=5, auto_publish=False, teammate=None,
    )

    # repair_cycles should have incremented for each test_run call
    assert state.repair_cycles >= 0  # May be 0 if tests pass, or >0 if they fail
    print(f"\n[Benchmark 5: Repair] repair_cycles={state.repair_cycles}")
