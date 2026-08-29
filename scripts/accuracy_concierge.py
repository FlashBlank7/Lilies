#!/usr/bin/env python3
"""管家答得对不对：每道题的真值从库里算，逐条核对。

冒烟查的是「链路通不通、有没有泄漏」；这个查的是**答案对不对**。
两者抓的东西不一样——2026-08-29 这批题第一次跑时 8 题错 2 题，
而当时冒烟全绿：

  · 问「昨天有没有失败的运行」→ 答"没有"（当天失败 5 次）。
    它调了 health_report，而那是看"现在健不健康"的：
    一个工作流昨天 5 败 26 成，在体检里仍然正常。
  · 问「哪个工作流跑得最多」→ 答错，并如实说"只比了最近 5 条记录"。
    没有哪个工具能给出每个工作流的运行总数，它只能一个个翻。

两条都不是模型在编，是**平台没把话说清、或根本没给这个数**。
修法都在数据这一侧：体检结果自带「这份数据不回答什么」、
recent_runs 支持按天查并直接给计数、list_workflows 带上运行次数。

用法：
    python scripts/accuracy_concierge.py                 # 默认 127.0.0.1:8000
    python scripts/accuracy_concierge.py --server http://…

只读：所有题目都只查不改。退出码非零表示有答错的。
"""

from __future__ import annotations

import argparse
import json
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
    import os

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


def ask(server: str, token: str, question: str) -> tuple[str, list[str]]:
    body = json.dumps({"messages": [{"role": "user", "text": question}]}).encode()
    request = urllib.request.Request(
        f"{server}/api/v1/assistant/agent", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=240) as response:
        payload = json.load(response)
    return payload.get("text") or "", [a.get("tool") for a in payload.get("actions") or []]


def build_cases(db: sqlite3.Connection) -> list[tuple[str, object]]:
    """题目和真值一起生成——真值从库里算，不写死。

    写死真值的话，数据一变这套题就开始说谎，而它存在的理由正是"别说谎"。

    真值只放**人话里能对上的东西**（数字、工作流名）。
    出题时拿状态码当过真值（"最近一次搭建是成功还是失败"→ published），
    它答"成功的，已经发布"——答对了却判成错。是题目错了，不是它错了。
    """
    one = lambda sql: db.execute(sql).fetchone()[0]
    return [
        ("现在有几个已发布的工作流？",
         one("SELECT COUNT(*) FROM applications "
             "WHERE archived_at IS NULL AND active_version IS NOT NULL")),
        ("收起来的工作流有多少个？",
         one("SELECT COUNT(*) FROM applications WHERE archived_at IS NOT NULL")),
        ("今天有几次运行？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.created_at LIKE date('now')||'%'")),
        ("哪个已发布的工作流跑得最多？只要名字",
         one("SELECT a.name FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND a.active_version IS NOT NULL "
             "AND r.version IS NOT NULL GROUP BY a.name "
             "ORDER BY COUNT(*) DESC LIMIT 1")),
        ("有几个工作流设了定时？",
         one("SELECT COUNT(*) FROM applications a "
             "JOIN application_versions v ON v.application_id=a.id "
             "AND v.version=a.active_version "
             "WHERE a.archived_at IS NULL "
             "AND v.snapshot_json LIKE '%schedule_trigger%'")),
        ("一共有多少个生成任务（构建）？",
         one("SELECT COUNT(*) FROM builds")),
        ("哪个工作流失败次数最多？只要名字",
         one("SELECT a.name FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE r.status='failed' AND r.version IS NOT NULL "
             "AND a.archived_at IS NULL GROUP BY a.name "
             "ORDER BY COUNT(*) DESC LIMIT 1")),
        ("昨天一共有几次失败的运行？",
         one("SELECT COUNT(*) FROM workflow_runs r "
             "JOIN applications a ON a.id=r.application_id "
             "WHERE a.archived_at IS NULL AND r.version IS NOT NULL "
             "AND r.status='failed' "
             "AND r.created_at LIKE date('now','-1 day')||'%'")),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="核对管家回答的事实准确性（只读）")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="data/agent_platform.db")
    args = parser.parse_args()

    token = _token()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    print(f"管家准确性核对 {DIM}{args.server}{NORM}")
    wrong: list[str] = []
    for question, truth in build_cases(db):
        try:
            answer, tools = ask(args.server, token, question)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"{BAD} 连不上后端：{error}")
            return 1
        # 数字答案允许带千分位/空格/加粗
        flat = answer.replace(",", "").replace(" ", "").replace("*", "")
        hit = str(truth) in flat
        mark = OK if hit else BAD
        print(f"{mark} {question}")
        print(f"   {DIM}真值 {truth} · 工具 {tools}{NORM}")
        print(f"   {DIM}{answer[:110].strip()}{NORM}")
        if not hit:
            wrong.append(f"{question}（真值 {truth}）")
    print()
    if wrong:
        print(f"{BAD} {len(wrong)} 题答得不对：")
        for item in wrong:
            print(f"  · {item}")
        print(f"{DIM}  先看它调了哪个工具——多半是平台没把话说清，"
              f"或者根本没给这个数。{NORM}")
        return 1
    print(f"{OK} 全部答对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
