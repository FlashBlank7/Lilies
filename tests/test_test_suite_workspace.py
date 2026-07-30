from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_runtime import (
    WorkflowRuntime,
    WorkflowWorkspaceBoundaryViolation,
)


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
