#!/usr/bin/env python3
"""跨端点对账：同一件事，几个接口说的数必须一样。

存在的理由：**口径漂移每个接口自己看都对，合起来才现形。**
真机上发生过——面板说本周失败 25 次，而失败清单最多解释得了 13 次，
另外 12 次属于已归档的工作流：数字在，出处查不到。
那次是「归档过滤只加在三个查询里的一个」，而三个查询各自的单元测试
都是绿的（各自的桩数据里根本没有归档的应用）。

所以这个脚本用**真库真接口**对账，不用桩：
桩会把"两处口径本来就一致"这个假设固化进去，而那正是要验的东西。

**它只查得出今天的数据能暴露的漂移。**这一点必须说在前面：
验证它灵不灵的时候，把 runs_today 的归档过滤删掉，脚本照样全绿——
因为今天恰好没有"已归档工作流的运行"。换成让 published_workflows
把草稿也算进去（15 vs 3），三条当场变红、退出码 1。
所以全绿的意思是「今天这批数据没照出漂移」，不是「没有漂移」。
真正的覆盖靠单元测试的合成数据，这个脚本管的是**桩测不到的那一半**：
真库里那些谁也没想到的形状。

只读：所有请求都是 GET，一个字节都不写。
退出码 0 全对得上 · 1 有对不上的。

用法：
    API_TOKEN=… python scripts/reconcile_endpoints.py
    python scripts/reconcile_endpoints.py --server http://… --db data/agent_platform.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

OK = "\x1b[32m✓\x1b[0m"
BAD = "\x1b[31m✕\x1b[0m"
DIM = "\x1b[2m"
NORM = "\x1b[0m"


def _token() -> str:
    for name in ("API_TOKEN", "AGENT_PLATFORM_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("要先给 API_TOKEN（环境变量或 .env）")


def _get(server: str, token: str, path: str):
    request = urllib.request.Request(
        server + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _checks(overview: dict, apps: list, health: dict, one) -> list[tuple]:
    """每条是 (这件事叫什么, 甲方的数, 甲方是谁, 乙方的数, 乙方是谁)。

    刻意让"同一个数"至少有两个独立来源：一个来自接口、一个来自库，
    或者来自两个不同的接口。只跟自己比的话，错了也一致。
    """
    runs_today = overview["runs_today"]
    week_fail = sum(day["fail"] for day in overview.get("week") or [])
    split_fail = sum(row["failed"] for row in overview.get("week_failures") or [])
    listed_kinds = len(overview.get("recent_failures") or [])
    total_kinds = int(overview.get("recent_failures_total") or listed_kinds)
    return [
        ("已发布工作流数",
         overview["published_workflows"], "overview",
         sum(1 for app in apps if app.get("active_version")), "applications 列表"),
        ("已发布工作流数（对库）",
         overview["published_workflows"], "overview",
         one("SELECT COUNT(*) FROM applications WHERE archived_at IS NULL "
             "AND active_version IS NOT NULL"), "数据库"),
        ("体检覆盖的工作流数",
         len(health.get("items") or []), "health-report",
         overview["published_workflows"], "overview"),
        ("今日运行总数",
         runs_today["total"], "overview",
         one("SELECT COUNT(*) FROM workflow_runs r JOIN applications a "
             "ON a.id=r.application_id WHERE a.archived_at IS NULL "
             "AND r.version IS NOT NULL AND substr(r.created_at,1,10)=date('now')"),
         "数据库"),
        # 内部自洽：成 + 败 + 在跑 必须等于总数。
        # 不等于就说明有一类状态没被算进任何一栏——那一类会在面板上凭空消失。
        ("今日成败之和 = 今日总数",
         runs_today["total"], "overview.total",
         runs_today["succeeded"] + runs_today["failed"] + runs_today["running"],
         "成+败+进行中"),
        # 这一条正对着真机上发生过的那次：数字在，出处查不到。
        ("近7日失败总数 = 拆到工作流之和",
         week_fail, "week", split_fail, "week_failures"),
        ("失败清单条数 = 种类总数截到 8",
         listed_kinds, "recent_failures", min(total_kinds, 8), "total 截到 8"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="跨端点对账（只读）")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="data/agent_platform.db")
    args = parser.parse_args()

    token = _token()
    try:
        overview = _get(args.server, token, "/api/v1/overview")
        raw_apps = _get(args.server, token, "/api/v1/applications")
        health = _get(args.server, token, "/api/v1/health-report")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"{BAD} 连不上后端：{error}")
        return 1
    apps = raw_apps if isinstance(raw_apps, list) else raw_apps.get("applications", [])
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print(f"跨端点对账 {DIM}{args.server}{NORM}")
    wrong = []
    for name, left, left_from, right, right_from in _checks(
            overview, apps, health, lambda sql: db.execute(sql).fetchone()[0]):
        agree = left == right
        print(f"  {OK if agree else BAD} {name}"
              f"  {DIM}{left_from}={left} / {right_from}={right}{NORM}")
        if not agree:
            wrong.append(f"{name}：{left_from} 说 {left}，{right_from} 说 {right}")
    print()
    if wrong:
        print(f"{BAD} {len(wrong)} 处对不上：")
        for item in wrong:
            print(f"  · {item}")
        print(f"{DIM}  先查过滤条件（归档？草稿自测？时间窗口？）——"
              f"这类不一致几乎都是某个查询少了一条 WHERE。{NORM}")
        return 1
    print(f"{OK} 全部对得上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
