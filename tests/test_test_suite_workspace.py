from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_models import WorkflowSpec
from agent_platform.workflow_runtime import (
    WorkflowRuntime,
    WorkflowWorkspaceBoundaryViolation,
)


def _workspace_workflow(
    declared: str,
    *,
    nested_declared: str | None = None,
) -> WorkflowSpec:
    nodes: list[dict[str, object]] = [
        {
            "id": "sandbox",
            "type": "sandbox_boundary",
            "title": "Sandbox",
            "config": {"settings": {"workspace": declared}},
        }
    ]
    if nested_declared is not None:
        nodes.append(
            {
                "id": "nested",
                "type": "iteration",
                "title": "Nested",
                "config": {
                    "workflow": {
                        "nodes": [
                            {
                                "id": "nested-tool",
                                "type": "tool_executor",
                                "title": "Nested tool",
                                "config": {
                                    "settings": {
                                        "workspace_path": nested_declared,
                                    }
                                },
                            }
                        ],
                        "edges": [],
                    }
                },
            }
        )
    return WorkflowSpec.model_validate({"nodes": nodes, "edges": []})


def test_owner_test_suite_forwards_explicit_workspace(tmp_path) -> None:
    settings = Settings(
        api_token="workspace-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.prepare()
    (settings.workspace_root / "customer-a").mkdir()
    app = create_app(settings=settings)
    observed: list[str] = []

    async def fake_run_test_suite(
        application_id: str,
        **kwargs: object,
    ) -> dict[str, object]:
        observed.append(str(kwargs["workspace_path"]))
        return {"passed": True, "application_id": application_id}

    app.state.services.workflow_runtime.run_test_suite = fake_run_test_suite
    headers = {"Authorization": "Bearer workspace-test-token"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/applications/application-a/tests/run",
            headers=headers,
            json={"workspace_path": "customer-a"},
        )
        assert response.status_code == 200
        assert response.json()["passed"] is True
        legacy = client.post(
            "/api/v1/applications/application-a/tests/run",
            headers=headers,
        )
        assert legacy.status_code == 200
    assert observed == ["customer-a", "."]


def test_test_suite_stages_workspace_tools_without_escaping(tmp_path) -> None:
    suite_base = tmp_path / "workspace"
    tools = suite_base / "tools"
    tools.mkdir(parents=True)
    executable = tools / "runner"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (tools / "runner-link").symlink_to("runner")
    cache = suite_base / ".program-cache" / "sample"
    cache.mkdir(parents=True)
    (cache / "state.json").write_text("{}", encoding="utf-8")
    case_workspace = tmp_path / "case"
    case_workspace.mkdir()

    WorkflowRuntime._stage_test_workspace_tools(suite_base, case_workspace)
    assert (case_workspace / "tools" / "runner").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\n"
    assert (case_workspace / "tools" / "runner-link").is_symlink()
    assert (
        case_workspace / ".program-cache" / "sample" / "state.json"
    ).read_text(encoding="utf-8") == "{}"

    unsafe_base = tmp_path / "unsafe"
    unsafe_tools = unsafe_base / "tools"
    unsafe_tools.mkdir(parents=True)
    (unsafe_tools / "escape").symlink_to(tmp_path / "outside")
    unsafe_case = tmp_path / "unsafe-case"
    unsafe_case.mkdir()
    with pytest.raises(
        WorkflowWorkspaceBoundaryViolation,
        match="symlink escapes workspace tools",
    ):
        WorkflowRuntime._stage_test_workspace_tools(
            unsafe_base,
            unsafe_case,
        )


def test_declared_test_workspaces_are_recursive_deduplicated_and_isolated(
    tmp_path,
) -> None:
    suite_base = tmp_path / "workspace"
    project = suite_base / "project"
    nested_project = suite_base / "nested-project"
    (project / "sub").mkdir(parents=True)
    nested_project.mkdir(parents=True)
    source_file = project / "source.txt"
    source_file.write_text("original", encoding="utf-8")
    (project / "sub" / "child.txt").write_text("child", encoding="utf-8")
    (nested_project / "nested.txt").write_text("nested", encoding="utf-8")
    workflow = _workspace_workflow("project", nested_declared="nested-project")
    workflow.nodes.append(
        _workspace_workflow("project/sub").nodes[0].model_copy(
            update={"id": "overlapping-tool"}
        )
    )

    suite_workspace = suite_base / "test-suite-fixed"
    case_a = suite_workspace / "case-a"
    case_b = suite_workspace / "case-b"
    case_a.mkdir(parents=True)
    case_b.mkdir()

    WorkflowRuntime._stage_test_declared_workspaces(
        workflow,
        suite_base,
        case_a,
    )
    WorkflowRuntime._stage_test_declared_workspaces(
        workflow,
        suite_base,
        case_b,
    )

    copied_a = case_a / "project" / "source.txt"
    copied_b = case_b / "project" / "source.txt"
    assert copied_a.read_text(encoding="utf-8") == "original"
    assert copied_b.read_text(encoding="utf-8") == "original"
    assert (case_a / "nested-project" / "nested.txt").read_text(
        encoding="utf-8"
    ) == "nested"
    assert copied_a.stat().st_ino != source_file.stat().st_ino
    assert copied_b.stat().st_ino != source_file.stat().st_ino

    copied_a.write_text("case-a", encoding="utf-8")
    assert copied_b.read_text(encoding="utf-8") == "original"
    assert source_file.read_text(encoding="utf-8") == "original"


def test_declared_test_workspace_rejects_absolute_escape_and_reserved_paths(
    tmp_path,
) -> None:
    suite_base = tmp_path / "workspace"
    project = suite_base / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    case_workspace = suite_base / "test-suite-fixed" / "case"
    case_workspace.mkdir(parents=True)

    for declared in (
        str(project.resolve()),
        "../outside",
        ".workflow-run-artifacts",
        "test-suite-old",
    ):
        with pytest.raises(
            WorkflowWorkspaceBoundaryViolation,
            match="relative|reserved",
        ):
            WorkflowRuntime._stage_test_declared_workspaces(
                _workspace_workflow(declared),
                suite_base,
                case_workspace,
            )


def test_declared_test_workspace_rejects_symlinks_and_special_files(
    tmp_path,
) -> None:
    suite_base = tmp_path / "workspace"
    project = suite_base / "project"
    project.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    case_workspace = suite_base / "test-suite-fixed" / "case"
    case_workspace.mkdir(parents=True)

    linked_project = suite_base / "linked-project"
    linked_project.symlink_to(project, target_is_directory=True)
    with pytest.raises(
        WorkflowWorkspaceBoundaryViolation,
        match="path contains a symbolic link",
    ):
        WorkflowRuntime._stage_test_declared_workspaces(
            _workspace_workflow("linked-project"),
            suite_base,
            case_workspace,
        )
    linked_project.unlink()

    (project / "escape").symlink_to(outside)
    with pytest.raises(
        WorkflowWorkspaceBoundaryViolation,
        match="symlink or special file",
    ):
        WorkflowRuntime._stage_test_declared_workspaces(
            _workspace_workflow("project"),
            suite_base,
            case_workspace,
        )

    (project / "escape").unlink()
    fifo = project / "events.pipe"
    os.mkfifo(fifo)
    with pytest.raises(
        WorkflowWorkspaceBoundaryViolation,
        match="symlink or special file",
    ):
        WorkflowRuntime._stage_test_declared_workspaces(
            _workspace_workflow("project"),
            suite_base,
            case_workspace,
        )


def test_root_workspace_snapshot_excludes_runtime_suites_and_does_not_alias_tools(
    tmp_path,
) -> None:
    suite_base = tmp_path / "workspace"
    tools = suite_base / "tools"
    tools.mkdir(parents=True)
    source_tool = tools / "runner"
    source_tool.write_text("tool", encoding="utf-8")
    (suite_base / "input.txt").write_text("input", encoding="utf-8")
    suite_workspace = suite_base / "test-suite-fixed"
    case_workspace = suite_workspace / "case"
    case_workspace.mkdir(parents=True)

    WorkflowRuntime._stage_test_declared_workspaces(
        _workspace_workflow("."),
        suite_base,
        case_workspace,
    )
    WorkflowRuntime._stage_test_workspace_tools(suite_base, case_workspace)

    copied_tool = case_workspace / "tools" / "runner"
    assert copied_tool.read_text(encoding="utf-8") == "tool"
    assert copied_tool.stat().st_ino != source_tool.stat().st_ino
    assert (case_workspace / "input.txt").read_text(encoding="utf-8") == "input"
    assert not (case_workspace / suite_workspace.name).exists()
