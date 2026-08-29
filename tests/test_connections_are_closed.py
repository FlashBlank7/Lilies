"""`with 连接:` 结束时要把连接关掉，不能只提交事务。

回归背景（2026-08-29 真机测量）：标准 sqlite3 的 `with conn:` 只管事务，
不关连接——这是个老陷阱。全平台一百多处都写成
`with self._connect() as conn:`，于是每次请求都留下一个连接等 GC 来收。

服务起来 9 分钟时，指向 agent_platform.db 的文件句柄 192 个，
随请求在 166~231 之间浮动。不是泄漏（数字会回落，GC 确实在收），
但稳态常驻两百个连接，每个带页缓存，回收时机全看 GC。

改完之后同一台机器、同样打 120 次请求：
  db 句柄 166~231 → 0，进程总句柄 207 → 15，内存 131 MB → 91 MB。
"""
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_platform.db import connect


class ConnectionClosesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "t.db"

    def test_the_connection_is_closed_after_the_with_block(self):
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE t(x)")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_the_transaction_is_still_committed(self):
        """关闭不能把提交语义弄丢——这是原来 with 的用处。"""
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE t(x)")
            conn.execute("INSERT INTO t VALUES (1)")
        with connect(self.path) as conn2:
            self.assertEqual(conn2.execute("SELECT x FROM t").fetchone()[0], 1)

    def test_an_exception_still_rolls_back(self):
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE t(x)")
        try:
            with connect(self.path) as conn:
                conn.execute("INSERT INTO t VALUES (9)")
                raise RuntimeError("中途出错")
        except RuntimeError:
            pass
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0,
                             "出错了却把那行留下了")

    def test_it_is_closed_even_when_the_block_raises(self):
        try:
            with connect(self.path) as conn:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_a_connection_held_without_with_is_untouched(self):
        """有些地方直接持有连接、自己管生命周期，不能被这个改动影响。"""
        conn = connect(self.path)
        try:
            conn.execute("CREATE TABLE t(x)")
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            conn.close()

    def test_the_pragmas_still_apply(self):
        with connect(self.path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(
                conn.execute("PRAGMA busy_timeout").fetchone()[0], 30_000)

    def test_row_factory_still_applies(self):
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE t(x)")
            conn.execute("INSERT INTO t VALUES (7)")
            row = conn.execute("SELECT x FROM t").fetchone()
            self.assertEqual(row["x"], 7, "row_factory 丢了，各处按列名取值会全炸")


class NoLingeringHandlesTest(unittest.IsolatedAsyncioTestCase):
    """跑一串真查询之后，不该留下一堆没关的连接。"""

    async def test_repeated_reads_do_not_pile_up_connections(self):
        import gc

        from agent_platform.config import Settings
        from agent_platform.storage import Storage

        with TemporaryDirectory() as tmp:
            settings = Settings(api_token="t", data_dir=Path(tmp) / "d",
                                workspace_root=Path(tmp) / "w")
            settings.prepare()
            storage = Storage(settings.data_dir)
            await storage.initialize()
            for _ in range(50):
                await storage.list_agents()
            gc.collect()
            alive = [o for o in gc.get_objects()
                     if isinstance(o, sqlite3.Connection)]
            self.assertLess(len(alive), 5, f"还留着 {len(alive)} 个连接对象")


if __name__ == "__main__":
    unittest.main()
