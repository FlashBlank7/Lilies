"""SQLite 撞锁不该立刻放弃。

实测（2026-08-28）：连接工厂没设 busy_timeout，sqlite 默认撞锁立刻抛
"database is locked"。真实库里因此攒下 122 条锁错误事件，其中一次直接被
判成"工作流失败"——而锁排队是并发下的常态，不是故障。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from agent_platform.db import BUSY_TIMEOUT_MS, connect


def test_connect_sets_busy_timeout(tmp_path):
    conn = connect(tmp_path / "x.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_writer_waits_instead_of_failing_immediately(tmp_path):
    """别人占着写锁时，要等而不是立刻抛。"""
    path = tmp_path / "busy.db"
    setup = connect(path)
    setup.execute("CREATE TABLE t(x INTEGER)")
    setup.commit()
    setup.close()

    holding = threading.Event()
    release = threading.Event()

    def hold_lock():
        conn = connect(path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES(1)")
        holding.set()
        release.wait(timeout=10)
        conn.commit()
        conn.close()

    worker = threading.Thread(target=hold_lock, daemon=True)
    worker.start()
    assert holding.wait(timeout=5)

    writer = connect(path)
    started = time.time()
    threading.Timer(1.0, release.set).start()
    try:
        writer.execute("INSERT INTO t VALUES(2)")   # 撞锁：应当等而不是抛
        writer.commit()
    finally:
        writer.close()
        worker.join(timeout=10)
    waited = time.time() - started
    assert waited >= 0.5, f"没等就成功了？{waited:.2f}s"


def test_default_sqlite_would_fail_here(tmp_path):
    """对照：不设 busy_timeout 的连接在同样场景下立刻失败——这就是修之前的行为。"""
    path = tmp_path / "default.db"
    setup = connect(path)
    setup.execute("CREATE TABLE t(x INTEGER)")
    setup.commit()
    setup.close()

    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES(1)")
    try:
        naive = sqlite3.connect(str(path), timeout=0)
        naive.execute("PRAGMA busy_timeout=0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            naive.execute("INSERT INTO t VALUES(2)")
        naive.close()
    finally:
        holder.rollback()
        holder.close()


def test_no_bare_sqlite_connect_left():
    """新代码别再绕过工厂——绕过就等于把 busy_timeout 丢了。"""
    from pathlib import Path

    allowed = {"db.py", "field_report.py"}   # 工厂本身与只读 URI（各自已设 timeout）
    offenders = []
    for path in Path("platform/backend/src/agent_platform").glob("*.py"):
        if path.name in allowed:
            continue
        if "sqlite3.connect(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, f"这些文件绕过了连接工厂：{offenders}"
