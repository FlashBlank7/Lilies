#!/usr/bin/env python3
"""实验台账：从构建记录与转录里机械生成对照表（论文原始数据）。

不手写结论——每一行都从 data/agent_platform.db 与 data/build_transcripts/
现场读出：引擎、模型组合、轮数、各角色工具调用与边界拒绝、失败归类、结局。

用法：
    python3 scripts/experiment_ledger.py                      # 全部构建
    python3 scripts/experiment_ledger.py --builder mechanical # 只看某引擎
    python3 scripts/experiment_ledger.py --json out.json      # 导出明细
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "data" / "agent_platform.db"
TRANSCRIPTS = REPO_ROOT / "data" / "build_transcripts"

# 失败归类：把 error 文本压成可统计的类别（论文里按类计数，不按文本）
FAILURE_CLASSES = [
    ("perseveration", ("model perseverating",)),
    ("stuck_readding", ("stuck re-adding",)),
    ("discovery_loop", ("discovery loop",)),
    ("scaffold_budget", ("scaffold budget exhausted",)),
    ("test_budget", ("test-author budget exhausted",)),
    ("acceptance_failed", ("acceptance still failing", "mandatory tests passed")),
    ("turn_budget", ("turn budget exhausted",)),
    ("deadline", ("timed out",)),
    ("context_limit", ("maximum context length",)),
    ("infra_restart", ("platform restarted",)),
    ("invalid_draft", ("invalid draft",)),
]


def classify(error: str) -> str:
    lowered = (error or "").lower()
    if not lowered:
        return "-"
    for name, needles in FAILURE_CLASSES:
        if any(needle.lower() in lowered for needle in needles):
            return name
    return "other"


def read_transcript(build_id: str) -> dict:
    path = TRANSCRIPTS / f"{build_id}.jsonl"
    stats = {
        "turns": 0,
        "by_actor": collections.defaultdict(
            lambda: {"turns": 0, "ok": 0, "rejected": 0, "models": set()}
        ),
        "reject_reasons": collections.Counter(),
    }
    if not path.exists():
        return stats
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") != "turn":
            continue
        stats["turns"] += 1
        actor = record.get("actor") or "coordinator"
        entry = stats["by_actor"][actor]
        entry["turns"] += 1
        model = (record.get("model") or "").split("/")[-1]
        if model:
            entry["models"].add(model)
        for call in record.get("tool_calls") or []:
            if call.get("is_error"):
                entry["rejected"] += 1
                reason = str(call.get("result") or "")[:60].split(":")[0]
                stats["reject_reasons"][f"{call.get('tool')}/{reason}"] += 1
            else:
                entry["ok"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", help="只统计某个引擎（classic / mechanical）")
    parser.add_argument("--json", help="把明细导出到该文件")
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    query = "select id, status, builder, error, requirement, team_state_json, created_at from builds"
    if args.builder:
        query += " where builder = ?"
    query += " order by created_at asc limit ?"
    params = ((args.builder, args.limit) if args.builder else (args.limit,))
    rows = conn.execute(query, params).fetchall()

    detail = []
    print(f"{'时刻':>5} {'构建':>8} {'引擎':>10} {'结局':>16} {'轮':>3} "
          f"{'ok':>4} {'拒':>4} {'拒绝率':>6}  失败类 / 模型")
    print("-" * 110)
    outcome_counts = collections.Counter()
    for row in rows:
        stats = read_transcript(row["id"])
        ok = sum(a["ok"] for a in stats["by_actor"].values())
        rejected = sum(a["rejected"] for a in stats["by_actor"].values())
        total = ok + rejected
        rate = f"{rejected / total:.0%}" if total else "-"
        models = sorted({m for a in stats["by_actor"].values() for m in a["models"]})
        failure = classify(row["error"] or "")
        outcome_counts[(row["builder"] or "?", row["status"], failure)] += 1
        print(f"{(row['created_at'] or '')[11:16]:>5} {row['id'][:8]:>8} "
              f"{(row['builder'] or '?'):>10} {row['status']:>16} {stats['turns']:>3} "
              f"{ok:>4} {rejected:>4} {rate:>6}  {failure} / {','.join(m[:18] for m in models)}")
        detail.append({
            "build_id": row["id"], "builder": row["builder"], "status": row["status"],
            "failure_class": failure, "turns": stats["turns"], "ok": ok, "rejected": rejected,
            "models": models,
            "by_actor": {
                actor: {**data, "models": sorted(data["models"])}
                for actor, data in stats["by_actor"].items()
            },
            "top_rejections": stats["reject_reasons"].most_common(5),
            "created_at": row["created_at"],
        })

    print("\n结局汇总（引擎 / 状态 / 失败类）：")
    for (builder, status, failure), count in outcome_counts.most_common():
        print(f"  {count:>3}×  {builder:>10}  {status:<16} {failure}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n明细已导出：{args.json}")


if __name__ == "__main__":
    main()
