"""连线不能连成环——而这道闸一直没有任何测试。

2026-08-29 变异验证：把 `_edge_would_create_cycle` 改成永远返回 False，
**全套 1181 条测试照样全绿**。也就是说这道闸坏了没有任何东西会响。

后果不是"图不好看"：工作流是照着边走的，成环意味着执行会绕回去。
平台为此专门提供了「循环」积木（有次数上限、有退出条件），
而一条随手连出来的环没有任何上限——它是绕不出去的那一种。

这道闸装在草稿操作（add_edge）上，也就是搭建方和界面改图的必经之路。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider

TOKEN = "cycle-test"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    with TestClient(create_app(settings, ScriptedProvider())) as c:
        yield c


def _mutate(client, app_id: str, revision: int, op: str, data: dict):
    return client.post(f"/api/v1/applications/{app_id}/draft", headers=_headers(),
                       json={"expected_revision": revision,
                             "idempotency_key": str(uuid4()),
                             "op": op, "data": data})


def _chain(client) -> tuple[str, int]:
    """建 start → a → b → end，返回 (应用 id, 当前修订号)。

    中间两个刻意都用 variable_assigner：它们**两侧都有端口**，
    所以 b → a 这条环边在端口校验那一关是合法的，只可能被环路检测拦下。
    第一版用的是 end → start，结果被"end 没有输出端口"提前拒了——
    测试是绿的，但绿的理由跟环路无关。断言"被拒"不够，
    还要断言**为什么被拒**。
    """
    app_id = client.post("/api/v1/applications", headers=_headers(),
                         json={"name": "环路测试", "requirement": "验证连线不能成环"},
                         ).json()["id"]
    revision = 0
    nodes = [
        {"id": "start", "type": "start", "title": "开始",
         "config": {"inputs": [
             {"name": "text", "type": "string", "required": False}]}},
        {"id": "a", "type": "variable_assigner", "title": "甲", "config": {}},
        {"id": "b", "type": "variable_assigner", "title": "乙", "config": {}},
        {"id": "end", "type": "end", "title": "结束", "config": {"outputs": {}}},
    ]
    for node in nodes:
        response = _mutate(client, app_id, revision, "add_node", {"node": node})
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
    for edge_id, source, target in (("e1", "start", "a"), ("e2", "a", "b"),
                                    ("e3", "b", "end")):
        response = _mutate(client, app_id, revision, "add_edge",
                           {"edge": {"id": edge_id, "source": source,
                                     "target": target, "source_port": "output",
                                     "target_port": "input"}})
        assert response.status_code == 200, response.text
        revision = response.json()["revision"]
    return app_id, revision


def _add_edge(client, app_id, revision, source, target, edge_id="loop"):
    return _mutate(client, app_id, revision, "add_edge",
                   {"edge": {"id": edge_id, "source": source, "target": target,
                             "source_port": "output", "target_port": "input"}})


def test_a_back_edge_is_refused(client):
    """b → a：最直白的一个环。

    断言里要有"环"这个理由——只断言"被拒了"的话，
    任何别的原因（端口不对、节点不存在）都能让它绿。
    """
    app_id, revision = _chain(client)
    response = _add_edge(client, app_id, revision, "b", "a")
    assert response.status_code != 200, response.text
    assert "cycle" in response.text or "循环" in response.text, response.text


def test_a_longer_way_round_is_refused_too(client):
    """b → start 之后再连回来也是环——只查"直接前驱"的实现会放它过去。

    这里换成 end 不行（end 没有输出端口），所以用 b → a 的更长版本：
    先 a → b 已在，再试 b → a 之外的绕行——用 start 做目标。
    """
    app_id, revision = _chain(client)
    # start 没有输入端口，改用三点环：新增 c，连 b → c，再试 c → a
    response = _mutate(client, app_id, revision, "add_node",
                       {"node": {"id": "c", "type": "variable_assigner",
                                 "title": "丙", "config": {}}})
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = _add_edge(client, app_id, revision, "b", "c", edge_id="e4")
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = _add_edge(client, app_id, revision, "c", "a")
    assert response.status_code != 200, response.text
    assert "cycle" in response.text or "循环" in response.text, response.text


def test_a_self_loop_is_refused(client):
    """自己连自己——这一条另有一句更贴切的中文，走的不是环路检测那支。

    分两支是对的：连到自己对用户来说不是「成环」，是「你点错了目标」。
    断言要跟着实际那一支走，否则测的是「我以为它该说什么」。
    """
    app_id, revision = _chain(client)
    response = _add_edge(client, app_id, revision, "a", "a")
    assert response.status_code != 200, response.text
    assert "连到它自己" in response.text, response.text


def test_the_rejected_edge_is_not_left_behind(client):
    """拒了就得干净——半条边留在图里比不拒还糟。"""
    app_id, revision = _chain(client)
    _add_edge(client, app_id, revision, "b", "a")
    draft = client.get(f"/api/v1/applications/{app_id}/draft",
                       headers=_headers()).json()
    edge_ids = [e["id"] for e in draft["snapshot"]["workflow"]["edges"]]
    assert "loop" not in edge_ids, edge_ids
    assert draft["revision"] == revision, "被拒的操作不该推进修订号"


def test_a_forward_edge_is_still_allowed(client):
    """别把闸关死：不成环的连线要照常连上。

    没有这一条的话，「一律拒绝」也能让上面几条全绿。
    """
    app_id, revision = _chain(client)
    response = _mutate(client, app_id, revision, "add_node",
                       {"node": {"id": "c", "type": "variable_assigner",
                                 "title": "丙", "config": {}}})
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = _add_edge(client, app_id, revision, "a", "c", edge_id="ok")
    assert response.status_code == 200, response.text
