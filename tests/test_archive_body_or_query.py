"""收起/拿回：请求体里的 archived 不能被静默忽略。

回归背景（2026-08-29 自查，我自己踩的）：archived 原本只认查询串。
我发 {"archived": false} 想把一个工作流拿回来，body 被静默忽略、
取了默认值 True——结果**又收了一次**，跟我要的正好相反。
静默做反的事比报错更糟：调用方以为恢复了，其实又归档了一遍。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings

HEADERS = {"Authorization": "Bearer archive-test"}


class ArchiveBodyOrQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(api_token="archive-test",
                                       data_dir=root / "d",
                                       workspace_root=root / "w",
                                       scheduler_poll_seconds=3600))

    def _call(self, url: str, json_body=None):
        client = TestClient(self.app)
        seen = {}

        async def fake_set_archived(app_id, archived):
            seen["archived"] = archived
            return {"application_id": app_id, "name": "x", "archived": archived,
                    "was_scheduled": False, "schedule_effect": ""}

        with client:
            self.app.state.services.workflow_store.set_archived = fake_set_archived
            response = client.post(url, headers=HEADERS, json=json_body)
        return response, seen

    def test_body_false_restores_instead_of_archiving_again(self):
        response, seen = self._call("/api/v1/applications/a1/archive",
                                    {"archived": False})
        self.assertEqual(response.status_code, 200)
        self.assertIs(seen["archived"], False)      # 关键：不能变成 True

    def test_body_true_still_archives(self):
        _, seen = self._call("/api/v1/applications/a1/archive", {"archived": True})
        self.assertIs(seen["archived"], True)

    def test_query_string_still_works(self):
        _, seen = self._call("/api/v1/applications/a1/archive?archived=false")
        self.assertIs(seen["archived"], False)

    def test_no_body_no_query_defaults_to_archiving(self):
        _, seen = self._call("/api/v1/applications/a1/archive")
        self.assertIs(seen["archived"], True)

    def test_body_wins_over_the_query_default(self):
        # body 明确说了就以 body 为准，别再拿默认值盖掉
        _, seen = self._call("/api/v1/applications/a1/archive", {"archived": False})
        self.assertIs(seen["archived"], False)
