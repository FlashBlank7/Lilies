"""401 的正文是客户端**原样印给用户**的，所以必须是人话。

回归背景（2026-08-29）：平台回 {"detail":"invalid API token"}，
而 guanjia 的 _readable() 会把 detail 取出来直接显示——
用户屏幕上就是这句英文：

    后端返回 401：invalid API token

而客户端那个函数的注释还写着"平台那边客户端会打到的报错今天已经全部中文化了"。
写下这句话的时候它是真的，后来这一处漏了，注释就成了假话。

顺带把「没带令牌」和「令牌不对」分开：前者是还没登录，
后者是登录过但失效了，下一步不一样。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_workflow import SlowBuilderProvider

ENGLISH_SENTENCE = re.compile(r"[A-Za-z]{4,}(\s+[A-Za-z]{2,}){2,}")


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "data",
                        workspace_root=tmp_path / "workspaces")
    with TestClient(create_app(settings, SlowBuilderProvider())) as c:
        yield c


def test_no_token_says_so_in_chinese(client):
    response = client.get("/api/v1/overview")
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "令牌" in detail, detail
    assert not ENGLISH_SENTENCE.search(detail), detail


def test_a_wrong_token_says_something_different(client):
    """没带和带错是两回事：一个还没登录，一个登录过但失效了。"""
    missing = client.get("/api/v1/overview").json()["detail"]
    wrong = client.get("/api/v1/overview",
                       headers={"Authorization": "Bearer wrong-token-1234"}).json()["detail"]
    assert missing != wrong
    assert "没带" in missing
    assert "失效" in wrong or "不对" in wrong


def test_a_wrong_token_is_chinese_too(client):
    response = client.get("/api/v1/overview",
                          headers={"Authorization": "Bearer wrong-token-1234"})
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "令牌" in detail and not ENGLISH_SENTENCE.search(detail), detail


def test_the_right_token_still_gets_in(client):
    """别把门焊死了。"""
    response = client.get("/api/v1/overview",
                          headers={"Authorization": "Bearer workflow-test"})
    assert response.status_code == 200


def test_the_client_would_show_the_chinese_sentence(client):
    """端到端那半步：guanjia 把 detail 取出来直接显示，所以这里就是用户看到的字。

    只断言"平台回了中文"是不够的——要断言**取出来之后**还是中文。
    """
    body = client.get("/api/v1/overview").text
    assert '"detail"' in body
    detail = client.get("/api/v1/overview").json()["detail"]
    assert isinstance(detail, str), "detail 不是字符串的话，客户端会印一坨 JSON"
    assert "令牌" in detail
