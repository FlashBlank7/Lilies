"""会话抽取模板：messages 是 ChatMessage 模型列表，不是 dict。

真机缺陷（2026-08-28 审计）：这个端点对模型调 .get()，
AttributeError 逃过 except KeyError → 500。与今天修掉的
run_workflow 是同一类缺陷（store 层返回的容器里装着 pydantic 模型）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage


def test_extract_template_handles_model_messages(tmp_path) -> None:
    settings = Settings(api_token="tpl-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    headers = {"Authorization": "Bearer tpl-test"}
    with TestClient(create_app(settings)) as client:
        storage = client.app.state.services.storage
        session_id = "s-1"
        client.portal.call(storage.create_session, session_id, "agent-1", 1, str(tmp_path / "ws"))
        messages = [ChatMessage(role=role, content=[{"type": "text", "text": text}])
                    # 用户消息要 >20 字符才算一个决策点（端点自己的门槛）
                    for role, text in [
                        ("user", "帮我做一个每日对账工作流，数据来自门店销售系统的导出文件"),
                        ("assistant", "好的，需要哪些字段？"),
                        ("user", "需要门店名称和当日金额两列，按门店分组汇总后出一张差异表"),
                        ("assistant", "明白了，这就搭。")]]
        client.portal.call(lambda: storage.update_session(session_id, messages=messages))

        response = client.post(f"/api/v1/sessions/{session_id}/extract-template",
                               headers=headers, json={})

    assert response.status_code == 200, response.text
    body = response.json()
    # 关键：不是 500，且真的把模型里的文本读出来了（不是静默返回空）
    assert body["decision_points"] == 2, body   # 两轮问答都被读出来了
    assert "AttributeError" not in response.text
