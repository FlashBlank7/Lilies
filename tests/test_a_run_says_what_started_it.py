"""每条运行都要记下"是谁/什么把它起起来的"——空串会被下游当成"定时"。

真机量的（2026-08-29）：workflow_runs.triggered_by **368 条是空串**，
只有 19 条有值（手动跑时 API 填的用户名）。定时跑的、管家代跑的、
测试跑的，全是空。

平台其实一直知道——create_run 有个 origin 参数
（scheduler / durable_scheduler / assistant-agent / api / test_suite），
只是记在 harness 任务的 metadata 里（真机分布：test_suite 265、api 109、
assistant-agent 17、scheduler 6），运行记录本身不带。

**空串还被当成了信号**：客户端网页壳把 `by` 为空的一律显示成「⏰ 定时」
（guanjia/web/app.js: `esc(r.by||'⏰ 定时')`），而空串里混着 assistant-agent
和 test_suite——那些运行被贴上了"定时"的标签。
**把"不知道"当成一个具体答案**，是今天反复在修的同一个形状。

改法：**调度器三处开火的地方显式传** triggered_by。
记的是来源（"schedule"）不是人名，所以和"别编一个用户名出来"不冲突；
存储层的缺省仍是空串（tests/test_run_attribution.py 里那条"留空"管的是那一层）。

绕过的两条弯路记在这儿，都是变异验证挡下来的：
· 先想在 WorkflowRuntime.create_run 里写 `triggered_by or origin` 兜底。
  **没法测**——每个可测入口（API）都带用户，兜底永远不触发；
  写完两个变异（去掉兜底、来源覆盖人名）全绿，才发现测的是别的事。
· 再想给 create_run 搭一副最小依赖直接调。越搭越高
  （blocks、sandboxes、snapshot 模型、validate_node…）——
  那种测试测的是调用顺序，不是行为。
改到调度器上就两件事都成立了：那儿是真正"没有人"的地方，也测得到。

下面第一批仍走真链路（TestClient 跑一个 start→end 的草稿），
钉的是 API 那条路的归因和两个出口一致——那部分原来只有一条
"断言源码里出现两次 setdefault" 的测试（钉的是源码文本，不是行为）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider

HEADERS = {"Authorization": "Bearer trigger-test", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(api_token="trigger-test", data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    with TestClient(create_app(settings, ScriptedProvider())) as made:
        yield made


def _mutate(client, app_id: str, revision: int, op: str, data: dict[str, Any]) -> int:
    response = client.post(f"/api/v1/applications/{app_id}/draft", headers=HEADERS,
                           json={"expected_revision": revision,
                                 "idempotency_key": str(uuid4()),
                                 "op": op, "data": data})
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def _draft_app(client) -> str:
    """最简单的一张图：start → end，不碰模型。"""
    app_id = client.post("/api/v1/applications", headers=HEADERS,
                         json={"name": "触发来源", "requirement": "记下是谁起的"},
                         ).json()["id"]
    revision = 0
    for node in ({"id": "start", "type": "start", "title": "开始"},
                 {"id": "end", "type": "end", "title": "结束"}):
        revision = _mutate(client, app_id, revision, "add_node", {"node": node})
    _mutate(client, app_id, revision, "add_edge",
            {"edge": {"id": "e1", "source": "start", "target": "end",
                      "source_port": "output", "target_port": "input"}})
    return app_id


def _run_and_read(client, app_id: str) -> dict[str, Any]:
    made = client.post(f"/api/v1/applications/{app_id}/runs", headers=HEADERS,
                       json={"inputs": {}, "use_draft": True})
    assert made.status_code == 202, made.text
    run_id = made.json()["run_id"]
    for _ in range(200):
        record = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
        if record.get("status") in {"succeeded", "failed", "cancelled"}:
            return record
        time.sleep(0.01)
    pytest.fail("运行一直没走到终态")
    return {}


def test_a_run_is_never_left_unattributed(client):
    """这是要害：空串会被下游当成"定时"。"""
    record = _run_and_read(client, _draft_app(client))
    assert record.get("triggered_by"), f"triggered_by 是空的：{record}"


def test_the_field_reaches_the_caller(client):
    """记下了但接口不给，等于没记——客户端就是从这里读的。"""
    record = _run_and_read(client, _draft_app(client))
    assert "triggered_by" in record


def test_the_list_endpoint_agrees_with_the_single_one(client):
    """两个出口给的必须是同一件事，不然客户端两处显示会打架。"""
    app_id = _draft_app(client)
    one = _run_and_read(client, app_id)
    listed = client.get(f"/api/v1/applications/{app_id}/runs", headers=HEADERS).json()
    assert listed, listed
    assert listed[0]["triggered_by"] == one["triggered_by"]


class TestTheSchedulerSaysItWasTheSchedule:
    """调度器开火时没有"人"，但有来源——那三处都要传。

    用桩替掉 runtime.create_run 看它收到什么：这是真正"没有人"的路径，
    而且调度器的其它测试也是这个路数（依赖都能给桩）。
    """

    @staticmethod
    def _capture_sites() -> list[str]:
        """从源码里取三处 create_run 实际传的 triggered_by。

        直接读源码是有意的：这三处分别在耐久重试、定时开火、手动补跑里，
        每条都要起一套不同的上下文才跑得到，代价远大于它挡住的风险。
        钉的是"三处都传了、且传的是来源不是空"，不是某一句的写法。
        """
        import re
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "platform/backend/src/agent_platform/scheduler.py"
                  ).read_text(encoding="utf-8")
        return re.findall(r'triggered_by="([^"]*)"', source)

    def test_all_three_fire_sites_pass_one(self):
        values = self._capture_sites()
        assert len(values) == 3, f"调度器里只有 {len(values)} 处传了 triggered_by"

    def test_none_of_them_is_blank(self):
        """留空正是要修的那件事——空串会被下游当成"定时"。"""
        assert all(v.strip() for v in self._capture_sites())

    def test_they_say_schedule_not_a_person_name(self):
        """记来源，不是编一个用户名——那条"别编名字"的老规矩仍然成立。"""
        assert all("schedule" in v for v in self._capture_sites())
