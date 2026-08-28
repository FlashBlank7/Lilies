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


def connect(db_path: str | Path, *, timeout: float = 30.0,
            row_factory=sqlite3.Row) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")   # 必须在 WAL 之前
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
