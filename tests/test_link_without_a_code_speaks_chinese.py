"""链接里少了码，回的必须是人话，不能是 pydantic 的英文结构体。

回归背景（2026-08-29 实测，真机免登录通道）：

    GET /api/v1/owner/{id}/state
    422 {"detail":[{"type":"missing","loc":["query","code"],
                    "msg":"Field required","input":null}]}

业主通道是**免登录**的——链接被复制断了、少了 ?code=，
是这条路上最常见的一种情形。而屏幕上出现的是一段英文报文。
使用者通道（use）早就把 code 声明成可选、落到中文 403 上了，
业主通道那四条没有——同一个闸，只装了一半出口。

顺带把「少了码」和「码不对」分开说：用户的下一步不一样。
少了码要去要**完整**链接，码不对要去要**新**链接；
说成同一句，一个把链接复制断了的人会以为码过期了。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_workflow import SlowBuilderProvider, headers


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "data",
                        workspace_root=tmp_path / "workspaces")
    with TestClient(create_app(settings, SlowBuilderProvider())) as c:
        yield c


def _app_id(client) -> str:
    return client.post("/api/v1/applications", headers=headers(),
                       json={"name": "少码测试", "requirement": "验一下没带码的链接。"},
                       ).json()["id"]


OWNER_GETS = ("state", "transcript")


@pytest.mark.parametrize("tail", OWNER_GETS)
def test_an_owner_link_without_a_code_gets_chinese(client, tail):
    app_id = _app_id(client)
    response = client.get(f"/api/v1/owner/{app_id}/{tail}")
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), detail          # 不是 pydantic 的那个数组
    assert "业主码" in detail and "少了" in detail


@pytest.mark.parametrize("tail,body", [("message", {"message": "你好"}),
                                       ("repair", {})])
def test_an_owner_post_without_a_code_gets_chinese(client, tail, body):
    app_id = _app_id(client)
    response = client.post(f"/api/v1/owner/{app_id}/{tail}", json=body)
    assert response.status_code == 403, response.text
    assert "业主码" in response.json()["detail"]


def test_a_wrong_owner_code_says_something_different(client):
    """「少了码」和「码不对」要分得开——下一步不一样。"""
    app_id = _app_id(client)
    missing = client.get(f"/api/v1/owner/{app_id}/state").json()["detail"]
    wrong = client.get(f"/api/v1/owner/{app_id}/state?code=乱写的").json()["detail"]
    assert missing != wrong
    assert "完整" in missing
    assert "不对" in wrong or "更换" in wrong


def test_a_use_link_without_a_code_gets_chinese_too(client):
    app_id = _app_id(client)
    response = client.get(f"/api/v1/use/{app_id}/definition")
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "访问码" in detail and "少了" in detail


def test_a_wrong_use_code_says_something_different(client):
    app_id = _app_id(client)
    missing = client.get(f"/api/v1/use/{app_id}/definition").json()["detail"]
    wrong = client.get(f"/api/v1/use/{app_id}/definition?code=乱写的").json()["detail"]
    assert missing != wrong


def test_no_english_leaks_from_any_of_these(client):
    """机械闸：这几条路上再冒出 pydantic 的英文，这里就红。"""
    app_id = _app_id(client)
    probes = [client.get(f"/api/v1/owner/{app_id}/state"),
              client.get(f"/api/v1/owner/{app_id}/transcript"),
              client.post(f"/api/v1/owner/{app_id}/message", json={"message": "x"}),
              client.post(f"/api/v1/owner/{app_id}/repair", json={}),
              client.get(f"/api/v1/use/{app_id}/definition")]
    for response in probes:
        text = response.text
        for english in ("Field required", '"loc"', "value_error", "type_error"):
            assert english not in text, f"{response.url} 漏了英文：{text[:160]}"


def test_a_good_code_still_works(client):
    """别把门焊死了——正确的码照样进得来。"""
    app_id = _app_id(client)
    code = client.get(f"/api/v1/applications/{app_id}/owner-code",
                      headers=headers()).json()["code"]
    assert client.get(f"/api/v1/owner/{app_id}/state?code={code}").status_code == 200


# ── 码本身的比对：只对"整串一模一样"放行 ──
#
# 变异验证发现的空档（2026-08-29）：把 compare_digest 换成
# `stored.startswith(code)`，**全套用例照样绿**。
# 那个实现意味着码可以一位一位试出来——64 次一位，而不是 64^12 整串，
# 从"爆破不现实"直接变成"下午就能试完"。
# 现成的用例只测了"乱写的码不行"，而乱写的码在 startswith 下也不行；
# 断言比要保证的东西弱，坏实现就能穿过去。


def _codes(client, app_id: str) -> tuple[str, str]:
    use = client.get(f"/api/v1/applications/{app_id}/access-code",
                     headers=headers()).json()["code"]
    owner = client.get(f"/api/v1/applications/{app_id}/owner-code",
                       headers=headers()).json()["code"]
    return use, owner


@pytest.mark.parametrize("mangle,why", [
    (lambda c: c[:-1], "少最后一位"),
    (lambda c: c[:1], "只给第一位"),
    (lambda c: c[:len(c) // 2], "只给前一半"),
    (lambda c: c + "x", "多一位"),
    (lambda c: c.upper() if c.lower() != c.upper() else c + "X", "大小写变了"),
    (lambda c: c[::-1], "倒过来"),
])
def test_a_partial_use_code_is_refused(client, mangle, why):
    app_id = _app_id(client)
    use, _ = _codes(client, app_id)
    bad = mangle(use)
    if bad == use:                     # 全数字的码大小写变不了，跳过那一条
        pytest.skip("这个码没法这样改")
    response = client.get(f"/api/v1/use/{app_id}/definition?code={bad}")
    assert response.status_code == 403, f"{why} 竟然放行了：{bad}"


@pytest.mark.parametrize("mangle,why", [
    (lambda c: c[:-1], "少最后一位"),
    (lambda c: c[:1], "只给第一位"),
    (lambda c: c + "x", "多一位"),
])
def test_a_partial_owner_code_is_refused(client, mangle, why):
    app_id = _app_id(client)
    _, owner = _codes(client, app_id)
    response = client.get(f"/api/v1/owner/{app_id}/state?code={mangle(owner)}")
    assert response.status_code == 403, f"{why} 竟然放行了"


def test_the_other_channels_code_does_not_open_this_one(client):
    """使用码不能当业主码用，反之亦然——两把钥匙各开各的门。"""
    app_id = _app_id(client)
    use, owner = _codes(client, app_id)
    assert use != owner
    assert client.get(f"/api/v1/owner/{app_id}/state?code={use}").status_code == 403
    assert client.get(f"/api/v1/use/{app_id}/definition?code={owner}").status_code == 403


def test_a_code_from_another_application_does_not_work(client):
    """A 的码不能开 B 的门——码是按应用发的。"""
    first, second = _app_id(client), _app_id(client)
    use_first, _ = _codes(client, first)
    assert client.get(
        f"/api/v1/use/{second}/definition?code={use_first}").status_code == 403
