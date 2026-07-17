from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.capability_contracts import legacy_intake_capability_contract
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def test_application_list_projects_business_goal_without_mutating_raw_description(
    tmp_path: Path,
) -> None:
    raw_plan = "# 工作流搭建方案\n\n## 业务目标\n旧版长 Markdown，不应直接出现在应用卡片。"
    business_goal = "自动整理投诉并生成客服主管可读的回复建议"
    contract = legacy_intake_capability_contract(
        requirement=raw_plan,
        workflow_intent={
            "target_user": "客服主管",
            "runtime_input": "投诉原文",
            "runtime_output": "回复建议",
        },
        completed_requirement=raw_plan,
    ).model_copy(update={"business_goal": business_goal})
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            scheduler_poll_seconds=3600,
        ),
        ScriptedProvider(),
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={
                "name": "Display projection",
                "description": raw_plan,
                "requirement": raw_plan,
                "capability_build_contract": contract.model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["description"] == raw_plan
        assert created.json()["display_description"] == business_goal

        listed = client.get("/api/v1/applications", headers=HEADERS)
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["description"] == raw_plan
        assert listed.json()[0]["display_description"] == business_goal
