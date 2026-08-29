"""SQLite 连接工厂：全平台唯一入口。

为什么需要它（2026-08-28 实测）：此前各处直接 sqlite3.connect，**没有 busy_timeout**——
sqlite 默认撞锁立刻放弃。真实库里因此攒下 122 条 "database is locked" 事件，
其中一次直接被判成"工作流失败"。而锁排队是并发下的常态，不是故障。

PRAGMA 顺序有讲究：busy_timeout 必须在 journal_mode 之前设。
`PRAGMA journal_mode=WAL` 自己就要拿锁，它是第一个会抛 "database is locked" 的语句。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000


class _ClosingConnection(sqlite3.Connection):
    """`with conn:` 结束时先按原样提交/回滚，然后**把连接关掉**。

    标准 sqlite3 的 `with` 只管事务，不关连接——这是个老陷阱。
    全平台一百多处都写成 `with self._connect() as conn:`，
    于是每次请求都留下一个连接，等循环 GC 来收。

    真机实测（2026-08-29）：服务起来 9 分钟，指向 agent_platform.db 的
    文件句柄 192 个，跟着请求上下浮动在 166~231 之间。
    不是泄漏——数字会回落，GC 确实在收——但稳态常驻两百个连接，
    每个都带页缓存，而且回收时机全看 GC 心情。

    在这里关掉是最省事的一处：`with` 的语义完全不变，
    直接持有连接不用 with 的写法（knowledge_rag 那几处是别的类）也不受影响。
    """

    def __exit__(self, *exc_info):
        try:
            return super().__exit__(*exc_info)
        finally:
            self.close()


def connect(db_path: str | Path, *, timeout: float = 30.0,
            row_factory=sqlite3.Row) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=timeout,
                           factory=_ClosingConnection)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")   # 必须在 WAL 之前
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
