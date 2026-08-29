"""落盘的密钥不能全机可读。

回归背景（2026-08-29 实测，这台机器上还有别的用户）：

  -rw-r--r--  .env                     里面有 DEEPSEEK_API_KEY（付费）、
                                       API_TOKEN、LOCAL_MODEL_API_KEY
  -rw-r--r--  data/agent_platform.db   业主码、客户使用码、用户令牌哈希、
                                       全部业务数据

原因都一样：sqlite / write_text 按 umask 建文件，默认 umask 022 就是 644。
不显式收就是松的。客户端的 ~/.guanjia.json 是同一个病（另修）。

两者处理方式不同，因为归属不同：
· 库文件是**程序自己建的** → 初始化时直接收成 0600
· .env 是**用户自己建的** → 只提醒，不替他改（那是越权，
  而且他可能有意共享给同组）
"""
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class DatabaseIsPrivateTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_locks_down_the_db_and_its_directory(self):
        from agent_platform.storage import Storage

        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "d"
            data.mkdir()
            data.chmod(0o755)
            storage = Storage(data)
            await storage.initialize()
            db = Path(storage.db_path)
            self.assertTrue(db.exists())
            self.assertEqual(_mode(db), 0o600, oct(_mode(db)))
            self.assertEqual(_mode(data), 0o700, oct(_mode(data)))

    async def test_the_wal_and_shm_are_locked_down_too(self):
        """WAL 和 SHM 是同一份数据的另外两块，收了主文件不算完。

        这条最早写成「跑一次 initialize，谁存在就查谁」，结果是**空断言**：
        新建的库先被收成 0600，SQLite 随后按主文件的权限建 WAL，
        自然也是 0600——把循环删到只剩主文件，测试照样绿。

        真正要保的是**升级场景**：库是这次改动之前建的，
        主文件和 WAL 都已经是 0644 躺在盘上了。所以照那个样子摆。
        """
        import sqlite3

        from agent_platform.storage import Storage

        with TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "d")
            await storage.initialize()
            db = Path(storage.db_path)
            # 摆成"老版本留下的库"：主文件和 WAL 都是 0644
            keep = sqlite3.connect(db)
            keep.execute("PRAGMA journal_mode=WAL")
            keep.execute("CREATE TABLE IF NOT EXISTS t(x)")
            keep.execute("INSERT INTO t VALUES (1)")
            keep.commit()
            sides = [Path(str(db) + s) for s in ("-wal", "-shm")]
            for path in [db, *sides]:
                path.chmod(0o644)
            self.assertTrue(all(p.exists() for p in sides), "这条测试要靠 WAL 真在盘上")
            try:
                storage._lock_down_files()
                for path in [db, *sides]:
                    self.assertEqual(_mode(path), 0o600,
                                     f"{path.name}: {oct(_mode(path))}")
            finally:
                keep.close()

    async def test_it_survives_a_filesystem_that_refuses_chmod(self):
        """收不动就算了——起不来比权限松更糟。"""
        from unittest.mock import patch

        from agent_platform.storage import Storage

        with TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "d")
            with patch.object(Path, "chmod", side_effect=OSError("只读文件系统")):
                await storage.initialize()      # 不抛就算过


class EnvWarningTest(unittest.TestCase):
    def _check(self, mode: int) -> list[str]:
        from agent_platform.cli import warn_if_secrets_are_readable

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                Path(".env").write_text("API_TOKEN=x\n", encoding="utf-8")
                Path(".env").chmod(mode)
                return warn_if_secrets_are_readable()
            finally:
                os.chdir(here)

    def test_a_world_readable_env_is_reported(self):
        self.assertEqual(self._check(0o644), [".env"])

    def test_a_group_readable_env_is_reported(self):
        """同组可读也算——这台机器上大家在同一个组里。"""
        self.assertEqual(self._check(0o640), [".env"])

    def test_a_private_env_is_not_reported(self):
        self.assertEqual(self._check(0o600), [])

    def test_the_frontend_env_is_checked_too(self):
        """密钥文件不止根目录那一个。

        2026-08-29 扫下来：platform/frontend/.env.local 也是 0644，
        里面同样写着 API_TOKEN。第一版只看 CWD 下的两个名字——
        只查一处的检查，会让人以为"已经查过了"。
        """
        from agent_platform.cli import warn_if_secrets_are_readable

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                target = Path("platform/frontend/.env.local")
                target.parent.mkdir(parents=True)
                target.write_text("API_TOKEN=x\n", encoding="utf-8")
                target.chmod(0o644)
                self.assertEqual(warn_if_secrets_are_readable(), [str(target)])
                target.chmod(0o600)
                self.assertEqual(warn_if_secrets_are_readable(), [])
            finally:
                os.chdir(here)

    def test_an_example_template_is_not_flagged(self):
        """.env.example 本来就该进版本库、本来就该人人可读——报它是噪音，
        而噪音会让人开始无视这类提醒。

        这条一开始是**空断言**：那时候 _env_files 写死四个完整路径，
        .env.example 压根不在候选里，把排除逻辑整个删掉测试照样绿
        （变异验的时候当场发现）。改成按目录 glob 之后它才真的在测东西。
        """
        from agent_platform.cli import warn_if_secrets_are_readable

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                for name in (".env.example", ".env.sample"):
                    Path(name).write_text("API_TOKEN=change-me\n", encoding="utf-8")
                    Path(name).chmod(0o644)
                # 先确认这些文件真在那儿，否则又是一条空断言
                self.assertTrue(Path(".env.example").is_file())
                self.assertEqual(warn_if_secrets_are_readable(), [])
            finally:
                os.chdir(here)

    def test_any_env_flavour_is_checked_not_just_the_two_names(self):
        """.env.production 这种名字也得查到——写死文件名就查不到它。"""
        from agent_platform.cli import warn_if_secrets_are_readable

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                Path(".env.production").write_text("API_TOKEN=x\n", encoding="utf-8")
                Path(".env.production").chmod(0o644)
                self.assertEqual(warn_if_secrets_are_readable(), [".env.production"])
            finally:
                os.chdir(here)

    def test_it_does_not_walk_into_node_modules(self):
        """范围写死不递归：递归会走进 node_modules，启动时白等几秒。"""
        from agent_platform.cli import _env_files

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                deep = Path("node_modules/whatever/.env")
                deep.parent.mkdir(parents=True)
                deep.write_text("X=1\n", encoding="utf-8")
                self.assertNotIn(deep, _env_files())
            finally:
                os.chdir(here)

    def test_a_missing_env_is_not_reported(self):
        from agent_platform.cli import warn_if_secrets_are_readable

        with TemporaryDirectory() as tmp:
            here = Path.cwd()
            try:
                os.chdir(tmp)
                self.assertEqual(warn_if_secrets_are_readable(), [])
            finally:
                os.chdir(here)


if __name__ == "__main__":
    unittest.main()
