"""停住的构建也要有出口：不在跑 ≠ 取消不了。

回归背景（2026-08-28 真机）：一个构建停在「等业主回话」，
cancel 只认此刻正在跑的任务，于是永远 404。业主想说「这个不要了」，
平台没有这句话——构建一直挂在列表里，一直提示要不要续跑。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agent_platform.api import TERMINAL_BUILD_STATUSES, create_app
from agent_platform.config import Settings

HEADERS = {"Authorization": "Bearer cancel-idle-test"}


class CancelIdleBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(
            api_token="cancel-idle-test",
            data_dir=root / "data",
            workspace_root=root / "workspaces",
            scheduler_poll_seconds=3600,
        ))

    def _client(self, status: str, active: bool) -> tuple[TestClient, MagicMock]:
        client = TestClient(self.app)
        services = self.app.state.services
        services.workflow_store.get_build = AsyncMock(
            return_value={"id": "b1", "status": status, "error": ""})
        services.workflow_store.update_build = AsyncMock()
        engine = MagicMock()
        if not active:
            engine.cancel.side_effect = KeyError("active build not found")
        services.builders.for_build = MagicMock(return_value=engine)
        return client, services

    def test_running_build_is_cancelled_through_the_engine(self):
        client, services = self._client("building", active=True)
        with client:
            response = client.post("/api/v1/builds/b1/cancel", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelling")
        services.workflow_store.update_build.assert_not_awaited()

    def test_paused_build_can_still_be_abandoned(self):
        client, services = self._client("needs_attention", active=False)
        with client:
            response = client.post("/api/v1/builds/b1/cancel", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        services.workflow_store.update_build.assert_awaited_once()

    def test_orphaned_build_can_be_abandoned_too(self):
        # 进程重启后，还标着 building 的构建在内存里已经没有任务了
        client, services = self._client("building", active=False)
        with client:
            response = client.post("/api/v1/builds/b1/cancel", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")

    def test_finished_build_is_not_rewritten(self):
        for status in sorted(TERMINAL_BUILD_STATUSES):
            client, services = self._client(status, active=False)
            with client:
                response = client.post("/api/v1/builds/b1/cancel", headers=HEADERS)
            self.assertEqual(response.status_code, 200, status)
            self.assertEqual(response.json()["status"], status, status)
            services.workflow_store.update_build.assert_not_awaited()

    def test_unknown_build_still_404s(self):
        client, services = self._client("building", active=True)
        services.workflow_store.get_build = AsyncMock(
            side_effect=KeyError("no such build"))
        with client:
            response = client.post("/api/v1/builds/b1/cancel", headers=HEADERS)
        self.assertEqual(response.status_code, 404)
