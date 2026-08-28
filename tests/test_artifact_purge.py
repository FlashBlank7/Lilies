"""删产物这件事：不能在启动路径上做，不能把删不掉的算成删掉了，
保留天数配成 0 时不能把刚产出的东西也删了。

回归背景（2026-08-29）：这段清理直接写在 startup 里，
同步 iterdir + stat + rmtree。产物少时看不出来，多了就是每次重启卡在这——
和事件归档那次 90 分钟停机是同一类错误（阻塞调用挂在启动路径上），
那次已经付过学费。真机此刻产物目录还不存在，所以是"攒到一定量就中"的雷。

另外两处：
· rmtree(..., ignore_errors=True) 之后照样 purged += 1，
  日志里的数字是"想删几个"不是"删掉几个"。
· run_artifacts_keep_days 配成 0 的话 cutoff 就是"现在"，
  会把刚跑完那一刻的产物一起删掉。删数据的地方不能这么松。
"""
import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


class ArtifactPurgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.artifacts = self.root / "w" / ".workflow-run-artifacts"
        self.artifacts.mkdir(parents=True)

    def _make(self, name: str, *, age_days: float) -> Path:
        run_dir = self.artifacts / name
        run_dir.mkdir()
        (run_dir / "out.txt").write_text("x", encoding="utf-8")
        stamp = time.time() - age_days * 86_400
        import os
        os.utime(run_dir, (stamp, stamp))
        return run_dir

    def _boot(self, *, keep_days: int = 14) -> None:
        app = create_app(Settings(api_token="t", data_dir=self.root / "d",
                                  workspace_root=self.root / "w",
                                  scheduler_poll_seconds=3600,
                                  run_artifacts_keep_days=keep_days))
        with TestClient(app) as client:
            client.get("/health")
            # 清理是后台任务：给它一点时间跑完
            for _ in range(50):
                if not (self.artifacts / "old").exists():
                    break
                asyncio.run(asyncio.sleep(0.02))

    def test_an_expired_run_directory_is_removed(self):
        self._make("old", age_days=30)
        self._make("fresh", age_days=1)
        self._boot()
        self.assertFalse((self.artifacts / "old").exists())
        self.assertTrue((self.artifacts / "fresh").exists(), "把没过期的也删了")

    def test_keep_days_zero_deletes_nothing(self):
        """配成 0 时 cutoff 就是"现在"——刚跑完的产物会被一起删掉。

        看不懂的配置一律当成"别删"。这是删数据的地方该有的默认方向。
        """
        self._make("just_now", age_days=0)
        self._make("old", age_days=30)
        self._boot(keep_days=0)
        self.assertTrue((self.artifacts / "just_now").exists())
        self.assertTrue((self.artifacts / "old").exists(), "keep_days=0 却动手删了")

    def test_a_negative_keep_days_also_deletes_nothing(self):
        self._make("old", age_days=90)
        self._boot(keep_days=-1)
        self.assertTrue((self.artifacts / "old").exists())

    def test_a_missing_artifacts_root_is_not_an_error(self):
        import shutil
        shutil.rmtree(self.artifacts)
        self._boot()      # 不抛就算过

    def test_startup_does_not_wait_for_the_purge(self):
        """判据是「启动没等清理跑完」，不是「启动还挺快」。

        初稿写的是"造 300 个过期目录，断言启动 < 10 秒"——
        300 个空目录同步删也用不了 10 秒，那条断言同步异步都绿，
        等于什么也没验。又是「断言比要保证的弱」。

        改成把删除本身弄慢：每个目录删 0.3 秒，5 个就是 1.5 秒。
        清理若还在启动路径上，健康检查必然要等这 1.5 秒。
        """
        import shutil
        from unittest.mock import patch

        for index in range(5):
            self._make(f"old{index}", age_days=30)

        # api.py 里写的是 `import shutil`，所以补丁打在 shutil 模块本身上。
        # 必须先把原函数抓在手里再打补丁——初稿在替身里调 `shutil.rmtree`，
        # 那时它已经是替身了，于是自己调自己，递归 246 层。
        # 74 秒的"慢"是这么来的，产品代码一点问题没有。
        original = shutil.rmtree

        def slow_rmtree(path, *args, **kwargs):
            time.sleep(0.3)
            return original(path, *args, **kwargs)

        app = create_app(Settings(api_token="t", data_dir=self.root / "d",
                                  workspace_root=self.root / "w",
                                  scheduler_poll_seconds=3600))
        with patch("agent_platform.api.shutil.rmtree", side_effect=slow_rmtree):
            started = time.monotonic()
            with TestClient(app) as client:
                elapsed = time.monotonic() - started
                self.assertEqual(client.get("/health").status_code, 200)
        self.assertLess(elapsed, 1.0,
                        f"启动等清理跑完了：{elapsed:.2f} 秒（清理本身要 1.5 秒）")


if __name__ == "__main__":
    unittest.main()
