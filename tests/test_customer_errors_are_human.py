"""客户/业主通道上的报错要是人话，不能是异常原文。

真机探测（2026-08-29）：客户取一次不存在的运行，回的是

    {"detail":"'record not found'"}

英文，而且带着 KeyError repr 出来的**一对多余引号**。
来源是 `raise HTTPException(404, str(error))` ——
KeyError 的 str 永远是被引号包起来的键名，对客户没有任何意义。

这条通道上站着的是外人：他没有日志、没有后台、也不会读英文栈。
他能做的只有"照这句话决定下一步"，所以每一句都得说清下一步。

（`ValueError`/`RuntimeError`/`TableIntakeError` 那几处保持原样：
  它们携带的是上游写好的中文，换成通用句反而丢信息。）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_workflow import SlowBuilderProvider, headers

TOKEN = "workflow-test"
CHINESE = re.compile(r"[一-鿿]")


@pytest.fixture
def channel(tmp_path: Path):
    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    settings.prepare()
    with TestClient(create_app(settings, SlowBuilderProvider())) as client:
        app_id = client.post("/api/v1/applications", headers=headers(),
                             json={"name": "客户报错测试", "requirement": "x"},
                             ).json()["id"]
        use = client.get(f"/api/v1/applications/{app_id}/access-code",
                         headers=headers()).json()["code"]
        owner = client.get(f"/api/v1/applications/{app_id}/owner-code",
                           headers=headers()).json()["code"]
        yield client, app_id, use, owner


def _detail(response) -> str:
    body = response.json()
    return str(body.get("detail", body))


def test_a_missing_run_speaks_chinese(channel):
    client, app_id, use, _ = channel
    response = client.get(f"/api/v1/use/{app_id}/runs/根本没有?code={use}")
    assert response.status_code == 404
    detail = _detail(response)
    assert CHINESE.search(detail), detail
    assert "record not found" not in detail
    assert "'" not in detail, f"KeyError 的引号漏出来了：{detail}"


def test_the_missing_run_message_says_what_to_do(channel):
    """只说"找不到"没用——客户不知道是该刷新、该换链接、还是该等。"""
    client, app_id, use, _ = channel
    detail = _detail(client.get(f"/api/v1/use/{app_id}/runs/根本没有?code={use}"))
    assert "重新跑" in detail or "过期" in detail, detail


def test_an_unpublished_application_is_not_reported_as_missing(channel):
    """还没发布 ≠ 找不到。

    说成"找不到"会把人引到错误的方向（去核对链接对不对），
    而真正该做的是等服务方发布。
    """
    client, app_id, use, _ = channel
    response = client.post(f"/api/v1/use/{app_id}/runs?code={use}",
                           json={"inputs": {}})
    detail = _detail(response)
    assert "还没发布" in detail, detail
    assert "找不到" not in detail, detail


def test_the_owner_channel_speaks_chinese_too(channel):
    client, _, _, owner = channel
    response = client.get(f"/api/v1/owner/根本没有这个应用/state?code={owner}")
    detail = _detail(response)
    assert CHINESE.search(detail), detail


def test_no_customer_facing_detail_is_a_bare_key_repr(channel):
    """机械闸：这几条路的正文里再出现 KeyError 那对引号，这里就红。"""
    client, app_id, use, owner = channel
    probes = [
        client.get(f"/api/v1/use/{app_id}/runs/没有这个?code={use}"),
        client.get(f"/api/v1/use/{app_id}/runs/没有这个/artifacts?code={use}"),
        client.get(f"/api/v1/use/{app_id}/runs/没有这个/artifacts/x?code={use}"),
        client.get(f"/api/v1/owner/没有这个/state?code={owner}"),
        client.post(f"/api/v1/use/{app_id}/runs?code={use}", json={"inputs": {}}),
    ]
    for response in probes:
        detail = _detail(response)
        assert not re.fullmatch(r"'[^']*'", detail), f"{response.url} → {detail}"
        assert CHINESE.search(detail), f"{response.url} → {detail}"


# ── KeyError 的引号是纯噪音，哪条路上都不该出现 ──


def test_the_helper_strips_the_quotes():
    from agent_platform.api import _plain_key_error

    assert _plain_key_error(KeyError("record not found")) == "找不到这条记录"
    assert "'" not in _plain_key_error(KeyError("record not found"))


def test_the_helper_keeps_the_useful_tail():
    """`application has no published version: <id>` 里的 id 是有用的。

    翻没了等于让人再查一次——所以是"换说法 + 保留细节"，
    不是"一律换成通用句"。
    """
    from agent_platform.api import _plain_key_error

    got = _plain_key_error(KeyError("application has no published version: a1"))
    assert "还没发布" in got and "a1" in got


def test_an_unmapped_key_error_at_least_loses_the_quotes():
    """认不出的照旧原样给——但引号一定去掉。

    引号是 KeyError repr 的产物，对任何人都没有意义。
    """
    from agent_platform.api import _plain_key_error

    got = _plain_key_error(KeyError("从没见过的情况"))
    assert got == "从没见过的情况"


def test_running_an_unpublished_workflow_says_so(channel):
    """运营侧那条路也要说人话——guanjia run 走的正是它。

    原先回的是 {"detail":"'application has no published version: <id>'"}。
    """
    client, app_id, _, _ = channel
    response = client.post(f"/api/v1/applications/{app_id}/runs",
                           headers=headers(), json={"inputs": {}})
    assert response.status_code == 404
    detail = _detail(response)
    assert "还没发布" in detail, detail
    assert not detail.startswith("'"), detail
