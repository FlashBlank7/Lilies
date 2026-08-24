"""业主通道：业主码免登录会话面——取码/验码/状态/会话流/插话/换码作废。"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_workflow import SlowBuilderProvider, headers


def test_owner_channel_end_to_end(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, SlowBuilderProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "业主通道验证", "requirement": "给业主一个免登录会话面。"},
        ).json()["id"]

        # 取业主码（总钥匙侧）；与使用码互相独立
        owner = client.get(f"/api/v1/applications/{app_id}/owner-code", headers=headers()).json()
        code = owner["code"]
        assert owner["owner_path"] == f"/owner/{app_id}?code={code}"
        use_code = client.get(f"/api/v1/applications/{app_id}/access-code", headers=headers()).json()["code"]
        assert use_code != code

        # 错码拒绝；使用码不能当业主码用
        assert client.get(f"/api/v1/owner/{app_id}/state?code=wrong").status_code == 403
        assert client.get(f"/api/v1/owner/{app_id}/state?code={use_code}").status_code == 403

        # 无构建时的状态与消息语义
        state = client.get(f"/api/v1/owner/{app_id}/state?code={code}").json()
        assert state["application"]["name"] == "业主通道验证"
        assert state["build"] is None
        assert client.post(
            f"/api/v1/owner/{app_id}/message",
            json={"code": code, "message": "在吗"},
        ).status_code == 409

        # 发起构建（慢速假 provider 保持 building），业主插话走实时通道
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "给业主一个免登录会话面。", "auto_publish": False,
                  "max_turns": 5, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(100):
            state = client.get(f"/api/v1/owner/{app_id}/state?code={code}").json()
            if state["build"] and state["build"]["status"] in {"queued", "building"}:
                break
            time.sleep(0.01)
        assert state["build"]["id"] == build_id

        delivered = client.post(
            f"/api/v1/owner/{app_id}/message",
            json={"code": code, "message": "帮我把日报里加上门店排名"},
        ).json()
        assert delivered == {"build_id": build_id, "delivered": "live"}

        transcript = client.get(f"/api/v1/owner/{app_id}/transcript?code={code}").json()
        owner_texts = [r["text"] for r in transcript["records"] if r.get("kind") == "owner"]
        assert any("门店排名" in text for text in owner_texts)

        client.post(f"/api/v1/builds/{build_id}/cancel", headers=headers())

        # 换码即作废旧链接
        new_code = client.post(f"/api/v1/applications/{app_id}/owner-code", headers=headers()).json()["code"]
        assert new_code != code
        assert client.get(f"/api/v1/owner/{app_id}/state?code={code}").status_code == 403
        assert client.get(f"/api/v1/owner/{app_id}/state?code={new_code}").status_code == 200
