"""模型服务挂了，两条路都要说人话——而且要说清下一步。

实测（2026-08-29）：把 provider 换成一个必然抛 ConnectionError 的桩，

    POST /api/v1/assistant/agent          → 500 Internal Server Error
    POST /api/v1/assistant/agent/stream   → 200 + 一个中文 error 事件

流式那条早就兜住了，非流式没有。同一道兜底只装了一个出口，第 N 次；
而模型服务不通是这类平台**最常见**的一种故障，
业主看到的却是一句英文的 500，没有任何下一步。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

TOKEN = "outage-test"


class _DeadProvider(ModelProvider):
    """一调就抛——模拟模型服务不通。"""

    name = "dead"

    def capabilities(self, model):
        return ProviderCapabilities(True, True, True, False, False, 1000, 100)

    async def stream(self, **kwargs):
        raise ConnectionError("模型服务连不上")
        yield  # pragma: no cover - 让它成为异步生成器


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    app = create_app(settings, _DeadProvider())
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _ask(client, path: str):
    return client.post(path, headers=_headers(),
                       json={"messages": [{"role": "user", "text": "今天跑了几次"}]})


def test_the_plain_endpoint_does_not_return_a_bare_500(client):
    response = _ask(client, "/api/v1/assistant/agent")
    assert response.status_code != 500, response.text
    assert "Internal Server Error" not in response.text


def test_the_plain_endpoint_says_what_happened_in_chinese(client):
    detail = _ask(client, "/api/v1/assistant/agent").json()["detail"]
    assert "没答上来" in detail
    assert "模型服务" in detail


def test_the_plain_endpoint_says_what_to_do_next(client):
    """只说"失败了"没用——业主不知道该重试、该等、还是该找人。"""
    detail = _ask(client, "/api/v1/assistant/agent").json()["detail"]
    assert "再试" in detail
    assert "日志" in detail


def test_the_streaming_endpoint_still_says_the_same_kind_of_thing(client):
    """两条路要一致：同一个故障不该因为用了哪个接口而说两种话。"""
    response = _ask(client, "/api/v1/assistant/agent/stream")
    assert response.status_code == 200
    assert "没答上来" in response.text
    assert "Internal Server Error" not in response.text


def test_a_healthy_provider_still_works(client, tmp_path):
    """别把闸关死：provider 正常时不该被这层兜底影响。"""
    from tests.test_assistant_agent import ConciergeScript

    settings = Settings(api_token=TOKEN, data_dir=tmp_path / "ok-d",
                        workspace_root=tmp_path / "ok-w")
    with TestClient(create_app(settings, ConciergeScript())) as good:
        response = _ask(good, "/api/v1/assistant/agent")
        assert response.status_code == 200, response.text
        assert response.json()["text"]
