"""业主码 / 使用码的比较走 compare_digest，且行为不变。

回归背景（2026-08-29）：这个仓里 API 令牌、密码哈希、连接器密钥
全都用 hmac.compare_digest，只有这两处是 `==`。

码本身是 secrets.token_urlsafe(9)（约 70 位熵），爆破不现实，
所以这不是在堵一个能打通的洞。修它的理由是**口径**：
同一个做法没铺满所有点，本身就是个信号——今天已经因为这个信号
挖出好几个真问题（业主页白名单、流式清洗、体检窗口）。
"""
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_platform.config import Settings
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage


class CodeVerificationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        settings = Settings(api_token="t", data_dir=Path(self._tmp.name) / "d",
                            workspace_root=Path(self._tmp.name) / "w")
        settings.prepare()
        storage = Storage(settings.data_dir)
        await storage.initialize()
        self.store = WorkflowStorage(storage)
        await self.store.initialize()
        with storage._connect() as conn:
            conn.execute(
                "INSERT INTO applications(id,name,description,requirement,mode,"
                "created_at,updated_at) VALUES('a1','x','','','workflow',"
                "datetime('now'),datetime('now'))")

    async def test_the_right_code_passes(self):
        code = await self.store.ensure_owner_code("a1")
        self.assertTrue(await self.store.verify_owner_code("a1", code))

    async def test_a_wrong_code_fails(self):
        await self.store.ensure_owner_code("a1")
        self.assertFalse(await self.store.verify_owner_code("a1", "wrong-code"))

    async def test_an_empty_code_fails(self):
        await self.store.ensure_owner_code("a1")
        for empty in ("", None):
            self.assertFalse(await self.store.verify_owner_code("a1", empty))

    async def test_a_prefix_of_the_right_code_fails(self):
        """长度不同不能因为 compare_digest 抛异常而变成 500。"""
        code = await self.store.ensure_owner_code("a1")
        self.assertFalse(await self.store.verify_owner_code("a1", code[:4]))

    async def test_an_unknown_application_fails_rather_than_raising(self):
        self.assertFalse(await self.store.verify_owner_code("nope", "x"))

    async def test_the_access_code_behaves_the_same_way(self):
        code = await self.store.ensure_access_code("a1")
        self.assertTrue(await self.store.verify_access_code("a1", code))
        self.assertFalse(await self.store.verify_access_code("a1", code + "x"))

    async def test_the_two_codes_are_not_interchangeable(self):
        """业主码不该开得了客户面，反之亦然。"""
        owner = await self.store.ensure_owner_code("a1")
        access = await self.store.ensure_access_code("a1")
        self.assertNotEqual(owner, access)
        self.assertFalse(await self.store.verify_access_code("a1", owner))
        self.assertFalse(await self.store.verify_owner_code("a1", access))


class NoPlainEqualityLeftTest(unittest.TestCase):
    def test_both_verifiers_use_compare_digest(self):
        for verify in (WorkflowStorage.verify_owner_code,
                       WorkflowStorage.verify_access_code):
            source = inspect.getsource(verify)
            self.assertIn("compare_digest", source, verify.__name__)


if __name__ == "__main__":
    unittest.main()
