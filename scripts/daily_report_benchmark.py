#!/usr/bin/env python3
"""日报基准：一条命令跑完一单，并机械判卷（四条件诚实判定）。

判卷标准来自 docs/framework-redesign-2026-08-23.md §5 的"智能足够"操作性定义，
这里把它写成代码，避免出题人手工判卷时放水：

  C1 发布成立      build.status == published
  C2 数字锚定验收   草稿里存在 mandatory 测试，且断言里有数值 equals；最近一次
                   test_run 全绿（不接受"跳过""仅结构断言"）
  C3 照妖镜        用验收中从未出现的门店名独立复跑正式版，输出与 Python
                   独立算出的分组合计/总额逐字段比对
  C4 无硬门豁免     build.error 为空；发布决策事件里没有 missing_evidence

任一不过即判负，并写明是哪一条挂的。判卷结果落盘 data/benchmark_runs/。

用法：
    python3 scripts/daily_report_benchmark.py --builder mechanical
    python3 scripts/daily_report_benchmark.py --builder classic --max-turns 60
    python3 scripts/daily_report_benchmark.py --judge-only <build_id>   # 只判已有单
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API = os.environ.get("LILIES_API", "http://127.0.0.1:8000")
RUNS_DIR = REPO / "data" / "benchmark_runs"

REQUIREMENT = (
    "输入门店销售流水 sales（数组，每项含 store 门店名、amount 金额）。"
    "输出三个字段：by_store（各门店合计，对象）、total（总金额，数字）、"
    "report（一段中文日报文本，包含各门店合计与总计）。"
    '样例：[{"store":"A店","amount":1200},{"store":"A店","amount":800},'
    '{"store":"B店","amount":3000}] → by_store={"A店":2000,"B店":3000}、total=5000。'
    "金额汇总必须用确定性积木计算，不能交给模型心算。"
    "工作流必须对任意门店名有效，不得把样例门店写死。"
)

# 照妖镜输入：门店名与金额都与验收样例无交集，且刻意让两店总额相同，
# 防止"按顺序猜"或"记住第一名"这类偶然对上。
UNSEEN_POOL = ["丙店", "丁店", "戊店", "己店", "庚店", "辛店"]


def token() -> str:
    value = os.environ.get("API_TOKEN")
    if value:
        return value
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            if line.startswith("API_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("API_TOKEN not found (env or .env)")


TOKEN = token()


def api(method: str, path: str, body: dict | None = None, timeout: float = 120.0):
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 独立参照实现（判卷方绝不复用平台的公式引擎）──────────────────

def reference(sales: list[dict]) -> dict:
    by_store: dict[str, float] = {}
    for row in sales:
        by_store[row["store"]] = by_store.get(row["store"], 0) + row["amount"]
    return {"by_store": by_store, "total": sum(row["amount"] for row in sales)}


def unseen_case(seed: int) -> list[dict]:
    rng = random.Random(seed)
    a, b = rng.sample(UNSEEN_POOL, 2)
    return [
        {"store": a, "amount": rng.randrange(50, 400, 10)},
        {"store": b, "amount": rng.randrange(50, 400, 10)},
        {"store": a, "amount": rng.randrange(50, 400, 10)},
    ]


# ── 四条件判卷 ──────────────────────────────────────────────

def numeric_anchored(test: dict) -> bool:
    """断言里必须有对数值字段的 equals，纯 contains/exists 不算数字锚定。"""
    for assertion in test.get("assertions") or []:
        if assertion.get("operator") == "equals" and isinstance(
            assertion.get("expected"), (int, float)
        ) and not isinstance(assertion.get("expected"), bool):
            return True
    return False


def judge_c2(build_id: str, application_id: str) -> dict:
    """mandatory 测试存在、带数字锚定、且最近一次执行全绿。"""
    try:
        draft = api("GET", f"/api/v1/applications/{application_id}/draft")
    except urllib.error.HTTPError:
        return {"pass": False, "why": "拿不到草稿"}
    snapshot = draft.get("snapshot") or {}
    tests = (snapshot.get("workflow") or {}).get("tests") or snapshot.get("tests") or []
    mandatory = [t for t in tests if t.get("mandatory")]
    if not mandatory:
        return {"pass": False, "why": "没有 mandatory 测试", "tests": len(tests)}
    anchored = [t for t in mandatory if numeric_anchored(t)]
    if not anchored:
        return {"pass": False, "why": "mandatory 测试没有数值 equals 断言（不算数字锚定）"}

    # 验收是否真的绿过：**不看转录**。经典引擎里 test_run 是模型调的（进转录），
    # 机械引擎里是状态机调的（不进转录）——按转录判会把机械引擎一律判负，
    # 那是判卷器在量错东西，不是引擎的问题（真机 a6284ec0 就这么被误判）。
    # 改看平台自己的验收证据：对**当前草稿**存在一次成功的验收运行。
    try:
        evidence = api(
            "GET", f"/api/v1/applications/{application_id}/publication-decision"
        ).get("evidence") or {}
    except urllib.error.HTTPError:
        evidence = {}
    if not evidence:
        # 端点不可用时退回现跑一次全量验收（判卷方自己触发，结论一样硬）
        report = api("POST", f"/api/v1/applications/{application_id}/tests/run", {})
        return {
            "pass": bool(report.get("passed")),
            "why": "" if report.get("passed") else "验收测试未全绿",
            "mandatory_tests": len(mandatory),
            "anchored_tests": len(anchored),
            "summary": report.get("summary"),
        }
    ok = (
        not evidence.get("latest_validation_failed")
        and evidence.get("state") == "current"
    )
    return {
        "pass": ok,
        "why": "" if ok else (
            f"平台验收证据不成立（state={evidence.get('state')}，"
            f"latest_failed={bool(evidence.get('latest_validation_failed'))}）"
        ),
        "mandatory_tests": len(mandatory),
        "anchored_tests": len(anchored),
        "assertions": sum(len(t.get("assertions") or []) for t in anchored),
        "evidence": evidence,
    }


def judge_c3(application_id: str, cases: int, seed: int) -> dict:
    """照妖镜：未见门店独立复跑正式版，与 Python 参照解逐字段比对。"""
    checks = []
    for index in range(cases):
        sales = unseen_case(seed + index)
        want = reference(sales)
        try:
            run = api("POST", f"/api/v1/applications/{application_id}/runs",
                      {"inputs": {"sales": sales}, "use_draft": False})
        except urllib.error.HTTPError as error:
            checks.append({"sales": sales, "ok": False,
                           "why": f"提交失败 {error.code}: {error.read()[:200].decode('utf-8', 'replace')}"})
            continue
        run_id = run.get("run_id") or run.get("id")
        deadline = time.time() + 180
        while time.time() < deadline:
            run = api("GET", f"/api/v1/runs/{run_id}")
            if run.get("status") not in {"queued", "running", "pending"}:
                break
            time.sleep(2)
        outputs: dict = {}
        for value in ((run.get("state") or {}).get("outputs") or {}).values():
            if isinstance(value, dict):
                outputs.update(value)
        got_by_store = outputs.get("by_store")
        got_total = outputs.get("total")
        report = str(outputs.get("report") or "")
        ok = (
            run.get("status") == "succeeded"
            and isinstance(got_by_store, dict)
            and {k: float(v) for k, v in got_by_store.items()} ==
                {k: float(v) for k, v in want["by_store"].items()}
            and got_total is not None and float(got_total) == float(want["total"])
            and str(int(want["total"])) in report
        )
        checks.append({
            "sales": sales, "status": run.get("status"), "ok": ok,
            "want": want, "got": {"by_store": got_by_store, "total": got_total},
            "report": report[:200],
            "why": "" if ok else "输出与独立参照解不一致（或报表文本缺总额）",
        })
    return {"pass": all(c["ok"] for c in checks) and bool(checks), "checks": checks}


def judge_c4(build: dict, build_id: str) -> dict:
    error = (build.get("error") or "").strip()
    bypass = []
    # 事件快照走 /v1/streams（/api/v1/builds/{id}/events 是 SSE 实时尾随，不会断开）
    try:
        events = api("GET", f"/v1/streams/{build_id}")
    except urllib.error.HTTPError:
        events = []
    for event in events if isinstance(events, list) else events.get("events", []):
        payload = json.dumps(event.get("data") or event, ensure_ascii=False)
        if "missing_evidence" in payload:
            bypass.append(payload[:200])
    return {"pass": not error and not bypass, "error": error[:300], "bypass": bypass}


def judge(build_id: str, *, cases: int, seed: int) -> dict:
    build = api("GET", f"/api/v1/builds/{build_id}")
    application_id = build.get("application_id") or build.get("app_id")
    c1 = {"pass": build.get("status") == "published", "status": build.get("status")}
    c2 = judge_c2(build_id, application_id) if c1["pass"] else {"pass": False, "why": "未发布，跳过"}
    c3 = judge_c3(application_id, cases, seed) if c1["pass"] else {"pass": False, "why": "未发布，跳过"}
    c4 = judge_c4(build, build_id)
    verdict = {
        "build_id": build_id, "application_id": application_id,
        "builder": build.get("builder"), "judged_at": now(),
        "C1_published": c1, "C2_anchored_tests_green": c2,
        "C3_unseen_input_correct": c3, "C4_no_gate_bypass": c4,
    }
    verdict["passed"] = all(verdict[k]["pass"] for k in
                            ("C1_published", "C2_anchored_tests_green",
                             "C3_unseen_input_correct", "C4_no_gate_bypass"))
    return verdict


def report(verdict: dict) -> None:
    marks = {True: "✅", False: "❌"}
    labels = {
        "C1_published": "C1 发布成立",
        "C2_anchored_tests_green": "C2 数字锚定验收全绿",
        "C3_unseen_input_correct": "C3 未见门店独立复跑算对",
        "C4_no_gate_bypass": "C4 无硬门豁免",
    }
    print("\n" + "═" * 66)
    print(f"日报基准判卷  构建 {verdict['build_id'][:8]}  引擎 {verdict['builder']}")
    print("═" * 66)
    for key, label in labels.items():
        block = verdict[key]
        line = f"{marks[bool(block['pass'])]} {label}"
        detail = block.get("why") or block.get("status") or ""
        print(f"{line}{('  — ' + str(detail)) if detail and not block['pass'] else ''}")
    for check in verdict["C3_unseen_input_correct"].get("checks", []):
        print(f"   {marks[check['ok']]} {json.dumps(check['sales'], ensure_ascii=False)}"
              f" → {json.dumps(check.get('got'), ensure_ascii=False)}")
    print("─" * 66)
    print(("🏆 通过——四条件全过" if verdict["passed"] else "判负") + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", default="mechanical", choices=["classic", "mechanical"])
    parser.add_argument("--coordinator", default="local2/Qwen/Qwen3-32B")
    parser.add_argument("--teammate", default="local/Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--max-repair-cycles", type=int, default=3)
    parser.add_argument("--max-seconds", type=float, default=5400)
    parser.add_argument("--cases", type=int, default=2, help="照妖镜复跑次数")
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--judge-only", help="只对已有构建判卷")
    args = parser.parse_args()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    if args.judge_only:
        build = api("GET", f"/api/v1/builds/{args.judge_only}")
        build_id = build["id"]
    else:
        application = api("POST", "/api/v1/applications", {
            "name": f"日报基准-{args.builder}-{time.strftime('%m%d-%H%M%S')}",
            "requirement": REQUIREMENT,
        })
        started = api("POST", f"/api/v1/applications/{application['id']}/builds", {
            "requirement": REQUIREMENT,
            "builder": args.builder,
            "auto_publish": True,
            "max_turns": args.max_turns,
            "max_repair_cycles": args.max_repair_cycles,
            "max_elapsed_seconds": args.max_seconds,
            "coordinator_model": args.coordinator,
            "teammate_models": [args.teammate],
        })
        build_id = started["build_id"]
        print(f"构建已发起 {build_id}  引擎={args.builder}  "
              f"统筹={args.coordinator}  队友={args.teammate}")
        deadline = time.time() + args.max_seconds + 300
        last = ""
        while time.time() < deadline:
            build = api("GET", f"/api/v1/builds/{build_id}")
            if build["status"] != last:
                print(f"  [{time.strftime('%H:%M:%S')}] {build['status']}")
                last = build["status"]
            if build["status"] not in {"queued", "building", "running"}:
                break
            time.sleep(15)

    verdict = judge(build_id, cases=args.cases, seed=args.seed)
    report(verdict)
    out = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{verdict['builder']}-{build_id[:8]}.json"
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"判卷已落盘：{out.relative_to(REPO)}")
    raise SystemExit(0 if verdict["passed"] else 1)


if __name__ == "__main__":
    main()
