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
