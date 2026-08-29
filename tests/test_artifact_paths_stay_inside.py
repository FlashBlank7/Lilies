"""产物下载不能越出那次运行自己的目录。

变异验证（2026-08-29）：把「解析后必须还在目录里」那道检查去掉，
**全套 1198 条测试全绿**——这道闸坏了没有任何东西会响。

实测传 `../../../../etc/passwd` 是拦住的，实现没问题；
但这类检查一旦悄悄失效，后果是任意文件读取，而且两条路都开着：
管理侧 /api/v1/runs/{id}/artifacts/{path}（要令牌）
和客户侧 /api/v1/use/{app}/runs/{id}/artifacts/{path}（只要一个使用码）。
后者更要紧：那是发给外部客户的链接。

同一段检查在文件里**出现两次**（两条路各一份）。所以这里两条路都测——
只测一条的话，另一条被改坏照样没人知道。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_workflow import SlowBuilderProvider, headers

TOKEN = "workflow-test"

# 绕法要用**百分号编码**的点。
#
# 写成裸的 `../../../../etc/passwd` 是没用的：HTTP 客户端（httpx、浏览器）
# 在发出去之前就把 `..` 规范化掉了，请求根本到不了处理器，
# 回的是路由层那个英文 `{"detail":"Not Found"}`。
# 第一版就是这么写的——五条用例全绿，而把越界检查整个删掉它们照样全绿。
# **断言"被 404 了"不够，还要断言 404 是我们这道闸打的**：
# 正文里得有「文件不存在」，那是处理器自己的话。
ESCAPES = [
    "%2e%2e/%2e%2e/secret.txt",          # 编码的点，客户端不会规范化
    "%2e%2e%2f%2e%2e%2fsecret.txt",      # 连斜杠一起编码
    "..%2f..%2fsecret.txt",              # 只编码斜杠
    "sub/%2e%2e/%2e%2e/secret.txt",      # 先进一层再回溯
    "%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
]

# 我们这道闸拒绝时说的话。用它来区分"闸拦住了"和"根本没走到闸"。
OURS = "文件不存在"


@pytest.fixture
def setup(tmp_path: Path):
    """建一个应用、一条运行记录，再在产物目录**外面**放一个诱饵文件。

    诱饵是关键：没有它，越界请求会因为"文件不存在"而 404，
    测试照样绿，但绿的理由跟越界检查无关。
    """
    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    settings.prepare()
    bait = Path(settings.workspace_root) / "secret.txt"
    bait.parent.mkdir(parents=True, exist_ok=True)
    bait.write_text("这是目录外面的东西", encoding="utf-8")

    app = create_app(settings, SlowBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=headers(),
                             json={"name": "产物越界测试",
                                   "requirement": "验证产物下载不越界"},
                             ).json()["id"]
        run_id = "run-artifact-test"
        folder = Path(settings.workspace_root) / ".workflow-run-artifacts" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.txt").write_text("这是这次运行自己的产物", encoding="utf-8")
        _insert_run(settings, app_id, run_id)
        code = client.get(f"/api/v1/applications/{app_id}/access-code",
                          headers=headers()).json()["code"]
        yield client, app_id, run_id, code, bait


def _insert_run(settings, app_id: str, run_id: str) -> None:
    """直接插一条成功的运行记录。

    不走"真跑一次工作流"是因为那要调模型；这里要验的是路径检查，
    跟运行怎么来的无关。state_json 得是合法的 WorkflowRunState——
    随手塞个 {} 会在读取时炸在 pydantic 上，而那跟越界毫无关系。
    """
    import json
    import sqlite3

    state = {
        "run_id": run_id,
        "application_id": app_id,
        "snapshot": {"name": "产物越界测试", "workflow": {"nodes": [], "edges": []}},
        "inputs": {},
        "workspace_path": str(settings.workspace_root),
    }
    conn = sqlite3.connect(Path(settings.data_dir) / "agent_platform.db")
    try:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,1,NULL,'succeeded',?,'{}',NULL,"
            "datetime('now'),datetime('now'))",
            (run_id, app_id, json.dumps(state, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def test_the_bait_is_really_outside_and_readable(setup):
    """先确认诱饵存在——否则下面全是空断言。"""
    _, _, _, _, bait = setup
    assert bait.is_file() and bait.read_text(encoding="utf-8")


def test_the_real_artifact_downloads(setup):
    """别把闸关死：目录里的文件要下得到。

    没有这一条的话，「一律 404」也能让下面全绿。
    """
    client, _, run_id, _, _ = setup
    response = client.get(f"/api/v1/runs/{run_id}/artifacts/report.txt",
                          headers=headers())
    assert response.status_code == 200, response.text
    assert "这次运行自己的产物" in response.text


@pytest.mark.parametrize("escape", ESCAPES)
def test_the_admin_route_refuses_to_escape(setup, escape):
    client, _, run_id, _, bait = setup
    response = client.get(f"/api/v1/runs/{run_id}/artifacts/{escape}",
                          headers=headers())
    assert response.status_code == 404, f"{escape} → {response.status_code}"
    assert OURS in response.text, f"没走到那道闸就 404 了：{response.text[:80]}"
    assert bait.read_text(encoding="utf-8") not in response.text


@pytest.mark.parametrize("escape", ESCAPES)
def test_the_customer_route_refuses_to_escape(setup, escape):
    """客户那条更要紧：链接是发给外部人的，只要一个使用码。"""
    client, app_id, run_id, code, bait = setup
    response = client.get(
        f"/api/v1/use/{app_id}/runs/{run_id}/artifacts/{escape}?code={code}")
    assert response.status_code == 404, f"{escape} → {response.status_code}"
    assert OURS in response.text, f"没走到那道闸就 404 了：{response.text[:80]}"
    assert bait.read_text(encoding="utf-8") not in response.text


def test_an_absolute_path_does_not_escape_either(setup):
    """绝对路径拼在后面时，Path 的 / 运算会直接丢掉左边——这是最容易漏的一种。"""
    client, _, run_id, _, bait = setup
    response = client.get(f"/api/v1/runs/{run_id}/artifacts//etc/passwd",
                          headers=headers())
    assert response.status_code == 404
    assert OURS in response.text, response.text[:80]
    assert bait.read_text(encoding="utf-8") not in response.text
