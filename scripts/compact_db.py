#!/usr/bin/env python3
"""把归档删掉的那些页真正还给磁盘（VACUUM）。**默认只看不动。**

为什么要有这么个东西：SQLite 的 DELETE 只把页标成空闲，文件一个字节都不会
变小。本仓 auto_vacuum=0、代码里也没有任何 VACUUM，所以事件归档一次清掉
几十万行之后，日志写着"移除 N 行"，而 `ls -lh` 看到的还是原来那么大。

真机 2026-08-29 的数字：库 978 MB，其中 845 MB 是
platform_harness.usage.recorded——那是**一个已经修好的 bug** 留下的存量
（当时每条事件都把该任务至今的全部用量明细抄一份，平方级增长；
一个任务 1006 条事件写了 70 MB）。修完不再长，但存量还在。

为什么不让后台任务自己做：
  · VACUUM 会**重写整个库**，期间拿排他锁——所有读写都要等；
  · 过程中临时占用约两倍磁盘（978 MB 的库要再腾出 1 GB）；
  · 耗时随库大小走，不是几毫秒的事。
这三条决定了它该由人挑个时间做，而不是某个后台循环偷偷做。

用法：
    python scripts/compact_db.py                 # 只报告：能收回多少
    python scripts/compact_db.py --run           # 真做（会锁库）
    python scripts/compact_db.py --db 别的.db

**跑之前**：先停掉后端（VACUUM 期间任何写都会被阻塞或失败），
并确认磁盘剩余空间大于库文件本身。这两点脚本会替你查一遍。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

OK = "\x1b[32m✓\x1b[0m"
BAD = "\x1b[31m✕\x1b[0m"
WARN = "\x1b[33m!\x1b[0m"
DIM = "\x1b[2m"
NORM = "\x1b[0m"


def survey(path: Path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        db.close()
    return {
        "file_bytes": path.stat().st_size,
        "page_bytes": page_size * page_count,
        "free_bytes": page_size * free,
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="回收 SQLite 空闲页（默认只报告）")
    parser.add_argument("--db", default="data/agent_platform.db")
    parser.add_argument("--run", action="store_true", help="真的执行 VACUUM")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"{BAD} 找不到 {path}")
        return 1

    before = survey(path)
    print(f"库 {path}")
    print(f"  文件 {before['file_bytes']/1e6:.0f} MB · 事件 {before['events']:,} 条")
    print(f"  已空闲但没还给磁盘：{before['free_bytes']/1e6:.0f} MB")

    if not args.run:
        print(f"\n{DIM}只报告，什么都没动。真要收回来加 --run。{NORM}")
        print(f"{DIM}注意：VACUUM 会重写整库并拿排他锁，跑之前先停后端。{NORM}")
        return 0

    # 真做之前的两道自查。跑到一半磁盘满，比不跑糟得多。
    free_disk = shutil.disk_usage(path.parent).free
    if free_disk < before["file_bytes"] * 1.2:
        print(f"{BAD} 磁盘只剩 {free_disk/1e6:.0f} MB，"
              f"而 VACUUM 期间大约要再占 {before['file_bytes']/1e6:.0f} MB。先腾地方。")
        return 1
    busy = _writers_present(path)
    if busy:
        print(f"{WARN} 库上还有写入者（{busy}）。VACUUM 会把它们全挡住，"
              "而且多半自己也拿不到锁。先停后端再来。")
        return 1
    # 两个检查都没抓到写入者——但那**不等于**服务已经停。
    # 这个平台的连接是用完即关的，两次请求之间既拿得到锁、也扫不到 fd。
    # 话要说清楚，别让人以为脚本替他确认过了。
    print(f"{DIM}  （没抓到写入者。但这个平台连接用完即关，"
          f"两次请求之间本来就抓不到——请自己确认后端已停。）{NORM}")

    print("\n开始 VACUUM（期间库不可写）……")
    started = time.monotonic()
    db = sqlite3.connect(str(path))
    try:
        db.execute("VACUUM")
    finally:
        db.close()
    after = survey(path)
    saved = before["file_bytes"] - after["file_bytes"]
    print(f"{OK} 用时 {time.monotonic() - started:.1f} 秒；"
          f"{before['file_bytes']/1e6:.0f} MB → {after['file_bytes']/1e6:.0f} MB"
          f"（省了 {saved/1e6:.0f} MB）")
    print(f"{DIM}  事件仍是 {after['events']:,} 条——VACUUM 只搬页，不删数据。{NORM}")
    return 0


def _writers_present(path: Path) -> str:
    """判断"现在能不能安全地 VACUUM"。两个信号一起看，各有各的盲区。

    第一版只拿排他锁试一下，**当场就被自己骗了**：后端明明在跑（8000 端口
    listening），锁却拿得到。因为这个平台的 SQLite 连接是用完即关的，
    两次请求之间根本没有连接持着锁。
    "拿得到锁"只能证明**这一瞬间**没人在写，证明不了服务已经停。
    ——今天一整天都在抓这种弱检查，工具自己也不能例外。

    所以再看一眼有没有进程开着这个库文件（Linux 上扫 /proc/*/fd）。
    这一条同样不是铁证：连接用完即关时，两次请求之间也扫不到。
    两个都过了只说明"没抓到写入者"，不等于"服务停了"——
    所以最后那句提醒照样打。
    """
    reasons = []
    try:
        db = sqlite3.connect(str(path), timeout=0.5)
        try:
            db.execute("BEGIN EXCLUSIVE")
            db.execute("ROLLBACK")
        finally:
            db.close()
    except sqlite3.OperationalError as error:
        reasons.append(f"库正被占用：{error}")

    target = path.resolve()
    for entry in Path("/proc").glob("[0-9]*/fd/*"):
        try:
            if entry.resolve() == target:
                pid = entry.parts[2]
                try:
                    name = (Path("/proc") / pid / "comm").read_text().strip()
                except OSError:
                    name = "?"
                reasons.append(f"进程 {pid}（{name}）正开着这个库文件")
                break
        except (OSError, RuntimeError):
            continue
    return "；".join(reasons)


if __name__ == "__main__":
    sys.exit(main())
