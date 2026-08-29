"""收起来的工作流不能还被触发——花着钱，面板却看不见。

回归背景（2026-08-29 独立复查）：定时那条路已经不再触发归档的工作流了
（调度器走 list_applications，本来就过滤归档），但客户侧的
`POST /api/v1/use/{id}/runs` 没砍——两个入口不对称。

后果不是「多跑一次」这么轻：today、体检、失败告警昨天全都加了
archived_at IS NULL 过滤，所以这些运行**一条都看不见**。
跑通了、产出了、花了模型的钱，而所有观测面都是盲的。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


class ArchivedCannotRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.app = create_app(Settings(api_token="run-test", data_dir=root / "d",
                                       workspace_root=root / "w",
                                       scheduler_poll_seconds=3600))

    def _post(self, application: dict):
        client = TestClient(self.app)
        with client:
            services = self.app.state.services
            services.workflow_store.verify_access_code = AsyncMock(return_value=True)
            services.workflow_store.get_application = AsyncMock(return_value=application)
            services.workflow_runtime.create_run = AsyncMock(
                return_value={"run_id": "r1", "status": "queued"})
            response = client.post(f"/api/v1/use/{application['id']}/runs?code=x",
                                   json={"inputs": {}})
            return response, services.workflow_runtime.create_run

    def test_archived_workflow_is_refused(self):
        response, create_run = self._post(
            {"id": "a1", "name": "退休了的", "archived_at": "2026-08-28T00:00:00+00:00"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("收起来", response.json()["detail"])
        create_run.assert_not_awaited()      # 关键：钱一分没花

    def test_the_refusal_says_how_to_undo_it(self):
        response, _ = self._post(
            {"id": "a1", "name": "退休了的", "archived_at": "2026-08-28T00:00:00+00:00"})
        self.assertIn("拿回", response.json()["detail"])

    def test_a_live_workflow_still_runs(self):
        response, create_run = self._post({"id": "a1", "name": "在用的",
                                           "archived_at": None})
        self.assertEqual(response.status_code, 202)
        create_run.assert_awaited_once()

    def test_missing_application_is_404_not_409(self):
        client = TestClient(self.app)
        with client:
            services = self.app.state.services
            services.workflow_store.verify_access_code = AsyncMock(return_value=True)
            services.workflow_store.get_application = AsyncMock(
                side_effect=KeyError("no such app"))
            response = client.post("/api/v1/use/nope/runs?code=x", json={"inputs": {}})
        self.assertEqual(response.status_code, 404)


class EveryRunPathIsGuardedTest(unittest.IsolatedAsyncioTestCase):
    """闸挪到了 create_run——所有入口共用一条路。

    回归背景（2026-08-29 下午）：上午这道闸只装在 /use/{id}/runs 上。
    可运营侧的 POST /applications/{id}/runs 是另一条路，guanjia run 走的正是它。
    同一个后果：运行照跑、照花钱，而 today / 体检 / 失败告警全都过滤归档，
    对这些运行完全是盲的。

    31 个针对单个工作流的写入端点里，当时只有 1 个查了 archived_at。
    """

    def _runtime(self, archived: str | None, *, draft: bool = False):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from agent_platform.workflow_runtime import WorkflowRuntime

        snapshot = {"name": "退休了的", "workflow": {"nodes": [
            {"id": "s", "type": "start", "config": {"inputs": []}}]}}
        runtime = WorkflowRuntime.__new__(WorkflowRuntime)
        runtime.workflow_store = SimpleNamespace(
            get_application=AsyncMock(return_value={"id": "a1", "name": "退休了的",
                                                    "archived_at": archived}),
            get_version=AsyncMock(return_value={"snapshot": snapshot, "version": 1}),
            get_draft=AsyncMock(return_value={"snapshot": snapshot, "revision": 1}))
        return runtime

    async def _run(self, archived, *, draft=False):
        from agent_platform.workflow_models import WorkflowRunRequest

        runtime = self._runtime(archived, draft=draft)
        return await runtime.create_run(
            "a1", WorkflowRunRequest(inputs={}, use_draft=draft))

    async def test_a_published_run_on_an_archived_workflow_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            await self._run("2026-08-28T00:00:00+00:00")
        self.assertIn("收起来", str(caught.exception))
        self.assertIn("拿回", str(caught.exception), "只说不行，没说怎么恢复")

    async def test_a_live_workflow_is_not_refused_here(self):
        """挡得太宽就是修出个新 bug。"""
        try:
            await self._run(None)
        except ValueError as error:
            self.assertNotIn("收起来", str(error))
        except Exception:
            pass      # 往下缺别的桩会报错，那不是这条关心的

    async def test_a_draft_self_test_is_not_refused(self):
        """搭建中途被归档，不该让搭建方的自测莫名其妙地失败。"""
        try:
            await self._run("2026-08-28T00:00:00+00:00", draft=True)
        except ValueError as error:
            self.assertNotIn("收起来", str(error))
        except Exception:
            pass
