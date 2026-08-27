"""用户体系：管理员建用户发令牌、用户令牌可用且身份正确、禁用即失效、非管理员无权建人。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_assistant_chat import EchoProvider


def _app(tmp_path: Path):
    settings = Settings(api_token="admin-boot", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    return create_app(settings, EchoProvider())


def test_user_lifecycle(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        admin = {"Authorization": "Bearer admin-boot"}
        created = client.post("/api/v1/users", headers=admin, json={"name": "小张"})
        assert created.status_code == 200, created.text
        token = created.json()["token"]
        assert token.startswith("lil_")

        me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["user"]["name"] == "小张"
        assert me.json()["user"]["role"] == "member"

        chat = client.post("/api/v1/assistant/chat", headers={"Authorization": f"Bearer {token}"},
                           json={"messages": [{"role": "user", "text": "hi"}]})
        assert chat.status_code == 200

        forbidden = client.post("/api/v1/users", headers={"Authorization": f"Bearer {token}"},
                                json={"name": "越权"})
        assert forbidden.status_code == 403

        uid = created.json()["user"]["id"]
        client.post(f"/api/v1/users/{uid}/status", headers=admin, json={"status": "disabled"})
        dead = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert dead.status_code == 401

        wrong = client.get("/api/v1/me", headers={"Authorization": "Bearer nonsense"})
        assert wrong.status_code == 401


def test_register_and_login_flow(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        bad = client.post("/api/v1/auth/register", json={"register_token": "wrong", "name": "甲", "password": "secret1"})
        assert bad.status_code == 401

        first = client.post("/api/v1/auth/register", json={"register_token": "admin-boot", "name": "甲", "password": "secret1"})
        assert first.status_code == 200, first.text
        assert first.json()["user"]["role"] == "admin"  # 首个注册者是管理员
        token_a = first.json()["token"]

        second = client.post("/api/v1/auth/register", json={"register_token": "admin-boot", "name": "乙", "password": "secret2"})
        assert second.json()["user"]["role"] == "member"

        dup = client.post("/api/v1/auth/register", json={"register_token": "admin-boot", "name": "甲", "password": "x12345"})
        assert dup.status_code == 409

        wrong = client.post("/api/v1/auth/login", json={"name": "甲", "password": "nope!!"})
        assert wrong.status_code == 401

        login = client.post("/api/v1/auth/login", json={"name": "甲", "password": "secret1"})
        assert login.status_code == 200
        token_a2 = login.json()["token"]
        assert token_a2 != token_a
        # 轮换后旧令牌失效、新令牌可用
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_a}"}).status_code == 401
        me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token_a2}"})
        assert me.status_code == 200 and me.json()["user"]["name"] == "甲"
