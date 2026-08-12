"""端到端测试:投稿双人组(有界涌现层级 2)。

验证显式团队场景可被应用并运行,形成协作闭环:
- 写手子智能体根据材料产出投稿草稿
- 审核子智能体评审草稿
- 输出同时包含草稿与审核意见

用脚本化 provider 驱动两个子智能体(写手→草稿,审核→意见),确定性、不耗 API。
"""
from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import StreamEvent
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

DRAFT = (
    "【标题】当传统企业开始用 AI 工作流\n"
    "【引言】很多企业卡在'AI 很美,落地很难'。\n"
    "【正文】一、数据管道……二、知识库……三、客户系统……\n"
    "【结尾】好流程让普通人产出好结果。"
)
REVIEW = "通过。标题吸引人,结构完整,事实准确,可直接发布。"

H = {"Authorization": "Bearer workflow-test"}


class TeamProvider(ModelProvider):
    """脚本化 provider:第 1 次模型调用返回草稿(写手),第 2 次返回审核意见(审核)。"""

    name = "team-scripted"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None):
        self.calls += 1
        text = DRAFT if self.calls == 1 else REVIEW
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 10}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}})


def test_submission_team_scenario_runs_end_to_end() -> None:
    """应用投稿双人组场景并运行:写手产草稿、审核出意见,协作闭环成立。"""
    tmp = TemporaryDirectory()
    settings = Settings(api_token="workflow-test",
                        data_dir=Path(tmp.name) / "data",
                        workspace_root=Path(tmp.name) / "workspaces")
    settings.prepare()
    app = create_app(settings, TeamProvider())

    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=H, json={
            "name": "投稿双人组", "requirement": "根据材料自动撰写并审核公众号投稿",
        }).json()["id"]
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=H).json()
        rev = draft["revision"]
        content_hash = draft["content_hash"]

        # 应用团队场景(替换空草稿)
        applied = client.post(
            f"/api/v1/applications/{app_id}/scenarios/submission_team/apply",
            headers=H, json={"expected_revision": rev, "expected_content_hash": content_hash, "replace_existing": True},
        )
        assert applied.status_code == 200, applied.text

        # 运行
        created = client.post(f"/api/v1/applications/{app_id}/runs", headers=H, json={
            "inputs": {"materials": "家装企业报价慢、交付投诉多、复购靠人脉的痛点"}, "use_draft": True,
        })
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]

        for _ in range(300):
            run = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
            if run["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)

        assert run["status"] == "succeeded", run.get("error", run)
        outputs = run.get("outputs", {})
        # 协作闭环:写手产出草稿,审核产出意见,汇总输出
        assert "【标题】" in outputs.get("draft", ""), "写手未产出草稿"
        assert "通过" in outputs.get("review", ""), "审核未产出意见"
        assert "【审核意见】" in outputs.get("result", ""), "未汇总草稿与审核"
    tmp.cleanup()
