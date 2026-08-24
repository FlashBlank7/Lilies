#!/usr/bin/env python3
"""执行链追踪：把一次构建从头到尾还原成可读的链条，并支持两单对比。

回答的问题：这个任务是怎么被完成的——它怎么想、怎么安排、怎么调工具、
每一步被边界怎么回应、状态机在哪里接管、什么时候换的模型。

数据来源全是现场记录，不做任何推测：
  data/build_transcripts/<id>.jsonl   每轮：角色/模型/思考/文本/工具调用/结果/token
  events 表（/v1/streams/<id>）        阶段推进、守卫、发布决策等平台侧判定
  builds 表                            结局、配置指纹、模型组合

用法：
    python3 scripts/trace_build.py <build_id>              # 完整链条
    python3 scripts/trace_build.py <build_id> --brief      # 只看骨架
    python3 scripts/trace_build.py <build_id> --prompts    # 连提示词一起看
                                                           （需构建时 LILIES_TRACE_PROMPTS=1）
    python3 scripts/trace_build.py --compare <id_a> <id_b> # 两条链并排对比
    python3 scripts/trace_build.py <build_id> --html out.html
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "data" / "agent_platform.db"
TRANSCRIPTS = REPO_ROOT / "data" / "build_transcripts"

PHASE_EVENTS = {
    "build.mechanical.phase": "阶段",
    "build.published": "发布",
    "build.needs_attention": "停摆",
    "build.deadline.exceeded": "超时",
    "build.teammate.spawned": "派工",
}


def load_build(build_id: str) -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "select * from builds where id like ?", (build_id + "%",)
    ).fetchone()
    if not row:
        raise SystemExit(f"未找到构建：{build_id}")
    build = dict(row)
    events = conn.execute(
        "select event_type, data_json, created_at from events where stream_id = ? order by seq",
        (build["id"],),
    ).fetchall()
    build["_events"] = [
        {"type": e["event_type"], "data": json.loads(e["data_json"] or "{}"),
         "at": e["created_at"]}
        for e in events
    ]
    conn.close()
    return build


def load_turns(build_id: str) -> list[dict]:
    path = TRANSCRIPTS / f"{build_id}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def clock(stamp: str | None) -> str:
    return (stamp or "")[11:19]


def elapsed(a: str | None, b: str | None) -> str:
    try:
        ta = datetime.fromisoformat((a or "").replace("Z", "+00:00"))
        tb = datetime.fromisoformat((b or "").replace("Z", "+00:00"))
        return f"{(tb - ta).total_seconds():.0f}s"
    except Exception:
        return "-"


def render_chain(build: dict, *, brief: bool, prompts: bool) -> list[str]:
    out: list[str] = []
    records = load_turns(build["id"])
    turns = [r for r in records if r.get("kind") == "turn"]
    team = json.loads(build.get("team_state_json") or "{}")
    out.append("═" * 78)
    out.append(f"构建 {build['id'][:8]}  引擎={build['builder']}  结局={build['status']}")
    out.append(f"需求：{(build.get('requirement') or '')[:120]}")
    out.append(f"协调模型={team.get('coordinator_model') or '默认'}  "
               f"队友池={team.get('teammate_models')}  "
               f"预算：{build.get('max_turns')} 轮 / 修复 {build.get('max_repair_cycles')} / "
               f"{build.get('max_elapsed_seconds')}s")
    if build.get("error"):
        out.append(f"错误：{build['error'][:200]}")
    out.append("═" * 78)

    # 平台侧判定（阶段/守卫/发布）按时间插进链条
    marks = [
        (e["at"], f"【{PHASE_EVENTS[e['type']]}】" + json.dumps(e["data"], ensure_ascii=False)[:150])
        for e in build["_events"] if e["type"] in PHASE_EVENTS
    ]
    mark_index = 0
    first_at = turns[0].get("recorded_at") if turns else None

    for record in turns:
        at = record.get("recorded_at")
        while mark_index < len(marks) and marks[mark_index][0] <= (at or ""):
            out.append(f"  {clock(marks[mark_index][0])}  {marks[mark_index][1]}")
            mark_index += 1
        actor = record.get("actor") or "coordinator"
        model = (record.get("model") or "").split("/")[-1]
        usage = record.get("usage") or {}
        head = (f"[{clock(at)} +{elapsed(first_at, at)}] 第{record.get('turn')}轮 "
                f"{actor} · {model} · in={usage.get('input_tokens', '-')} "
                f"out={usage.get('output_tokens', '-')} · rev={record.get('draft_revision')}")
        out.append(head)
        if prompts and record.get("prompt"):
            out.append("   ── 平台给它看的（system）──")
            out.append("   " + (record["prompt"].get("system") or "")[:600].replace("\n", "\n   "))
            out.append("   ── 平台给它看的（user）──")
            out.append("   " + (record["prompt"].get("user") or "")[:1500].replace("\n", "\n   "))
        if not brief and record.get("thinking"):
            out.append("   💭 " + record["thinking"][:400].replace("\n", " "))
        if not brief and record.get("text"):
            out.append("   💬 " + record["text"][:300].replace("\n", " "))
        calls = record.get("tool_calls") or []
        if not calls:
            out.append(f"   （无工具调用，stop={record.get('stop_reason')}）")
        for call in calls:
            flag = "⛔" if call.get("is_error") else "✅"
            args = json.dumps(call.get("arguments"), ensure_ascii=False)
            out.append(f"   {flag} {call.get('tool')} {args[:200 if brief else 400]}")
            result = str(call.get("result") or "")
            if call.get("is_error"):
                out.append(f"      边界回应：{result[:220]}")
            elif not brief:
                out.append(f"      结果：{result[:160]}")
    while mark_index < len(marks):
        out.append(f"  {clock(marks[mark_index][0])}  {marks[mark_index][1]}")
        mark_index += 1
    return out


def summarize(build: dict) -> dict:
    records = load_turns(build["id"])
    turns = [r for r in records if r.get("kind") == "turn"]
    by_actor: dict[str, dict] = collections.defaultdict(
        lambda: {"turns": 0, "ok": 0, "rejected": 0, "models": set(), "tools": collections.Counter()}
    )
    tokens_in = tokens_out = 0
    for record in turns:
        entry = by_actor[record.get("actor") or "coordinator"]
        entry["turns"] += 1
        entry["models"].add((record.get("model") or "").split("/")[-1])
        usage = record.get("usage") or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)
        for call in record.get("tool_calls") or []:
            entry["tools"][call.get("tool")] += 1
            entry["rejected" if call.get("is_error") else "ok"] += 1
    total_ok = sum(a["ok"] for a in by_actor.values())
    total_rej = sum(a["rejected"] for a in by_actor.values())
    span = elapsed(turns[0].get("recorded_at") if turns else None,
                   turns[-1].get("recorded_at") if turns else None)
    return {
        "id": build["id"][:8], "builder": build["builder"], "status": build["status"],
        "turns": len(turns), "ok": total_ok, "rejected": total_rej,
        "reject_rate": (total_rej / (total_ok + total_rej)) if (total_ok + total_rej) else 0.0,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "span": span,
        "by_actor": by_actor,
        "phases": [e["data"].get("phase") for e in build["_events"]
                   if e["type"] == "build.mechanical.phase"],
    }


def render_compare(a: dict, b: dict) -> list[str]:
    sa, sb = summarize(a), summarize(b)
    out = ["═" * 78, "执行链对比（左 / 右）", "═" * 78]
    rows = [
        ("构建", sa["id"], sb["id"]),
        ("引擎", sa["builder"], sb["builder"]),
        ("结局", sa["status"], sb["status"]),
        ("总轮数", sa["turns"], sb["turns"]),
        ("工具调用 通过/被拒", f"{sa['ok']}/{sa['rejected']}", f"{sb['ok']}/{sb['rejected']}"),
        ("边界拒绝率", f"{sa['reject_rate']:.0%}", f"{sb['reject_rate']:.0%}"),
        ("token 入/出", f"{sa['tokens_in']}/{sa['tokens_out']}", f"{sb['tokens_in']}/{sb['tokens_out']}"),
        ("耗时", sa["span"], sb["span"]),
        ("阶段序列", "→".join(sa["phases"][:8]) or "（自由循环）",
         "→".join(sb["phases"][:8]) or "（自由循环）"),
    ]
    for label, left, right in rows:
        out.append(f"{label:<18} {str(left):<28} {str(right)}")
    out.append("")
    out.append("角色分工：")
    for tag, summary in (("左", sa), ("右", sb)):
        for actor, data in summary["by_actor"].items():
            top = "、".join(f"{t}×{n}" for t, n in data["tools"].most_common(4))
            out.append(f"  [{tag}] {actor:<14} {data['turns']:>2}轮 "
                       f"ok={data['ok']:>2} 拒={data['rejected']:>2} "
                       f"{'/'.join(sorted(m for m in data['models'] if m))} | {top}")
    return out


def to_html(lines: list[str], path: Path) -> None:
    body = "\n".join(
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for line in lines
    )
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>执行链</title>"
        "<style>body{background:#0f1115;color:#d7dce5;font:13px/1.5 ui-monospace,monospace;"
        "padding:20px;white-space:pre-wrap}</style><body>" + body,
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("build_id", nargs="?")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"))
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--prompts", action="store_true")
    parser.add_argument("--html")
    args = parser.parse_args()

    if args.compare:
        lines = render_compare(load_build(args.compare[0]), load_build(args.compare[1]))
    elif args.build_id:
        lines = render_chain(load_build(args.build_id), brief=args.brief, prompts=args.prompts)
    else:
        parser.error("给一个 build_id，或用 --compare A B")

    print("\n".join(lines))
    if args.html:
        to_html(lines, Path(args.html))
        print(f"\nHTML 已导出：{args.html}")


if __name__ == "__main__":
    main()
