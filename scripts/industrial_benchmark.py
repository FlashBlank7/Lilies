#!/usr/bin/env python3
"""工业任务包：跑一单并机械判卷（与日报基准同一套诚实判据）。

判据（缺一不可，全部写死在代码里，出题人无法临场放水）：

  C1 交付成立      build.status ∈ {published, ready}（本包默认 auto_publish=false，
                   ready 即"结构有效、端到端跑得起来、等业主验收"）
  C2 验收有锚      草稿里有 mandatory 测试，且至少一条断言了具体值
                   （equals/contains、非 structural）——只验形状的验收不算数
  C3 样例算对      用任务的 sample_inputs 复跑，输出与 benchmarks/industrial_v1/
                   reference.py 的**独立 Python 实现**逐事实比对
  C4 照妖镜        用该任务的未见输入复跑，同样逐事实比对（防硬编码样例答案）

C3/C4 的参照解与判卷器都在 reference.py 里，不 import 任何平台模块——
判卷方与被判方共用一份代码就等于没判。

用法：
    python3 scripts/industrial_benchmark.py --task T1-procurement-reconciliation
    python3 scripts/industrial_benchmark.py --all --builder mechanical
    python3 scripts/industrial_benchmark.py --judge-only <build_id> --task T1-...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO / "benchmarks" / "industrial_v1"
sys.path.insert(0, str(PACKAGE_DIR))

from reference import CHECKERS, REFERENCES  # noqa: E402

API = os.environ.get("LILIES_API", "http://127.0.0.1:8000")
RUNS_DIR = REPO / "data" / "benchmark_runs"


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
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def tasks() -> dict[str, dict]:
    data = json.loads((PACKAGE_DIR / "tasks.json").read_text("utf-8"))
    return {task["id"]: task for task in data["tasks"]}, data.get("defaults", {})


def run_workflow(application_id: str, inputs: dict, use_draft: bool) -> dict:
    run = api("POST", f"/api/v1/applications/{application_id}/runs",
              {"inputs": inputs, "use_draft": use_draft})
    run_id = run.get("run_id") or run.get("id")
    deadline = time.time() + 300
    while time.time() < deadline:
        run = api("GET", f"/api/v1/runs/{run_id}")
        if run.get("status") not in {"queued", "running", "pending"}:
            break
        time.sleep(2)
    outputs: dict = {}
    for value in ((run.get("state") or {}).get("outputs") or {}).values():
        if isinstance(value, dict):
            outputs.update(value)
    return {"status": run.get("status"), "outputs": outputs}


def anchored(application_id: str) -> tuple[bool, str]:
    draft = api("GET", f"/api/v1/applications/{application_id}/draft")
    snapshot = draft.get("snapshot") or {}
    tests = snapshot.get("tests") or (snapshot.get("workflow") or {}).get("tests") or []
    mandatory = [test for test in tests if test.get("mandatory")]
    if not mandatory:
        return False, "没有 mandatory 验收测试"
    for test in mandatory:
        for assertion in test.get("assertions") or []:
            if (
                assertion.get("operator") in {"equals", "contains"}
                and not assertion.get("structural")
                and assertion.get("expected") is not None
            ):
                return True, ""
    return False, "mandatory 验收只验了形状，没有一条断言具体值"


def judge(build_id: str, task_id: str) -> dict:
    task_map, _ = tasks()
    task = task_map[task_id]
    build = api("GET", f"/api/v1/builds/{build_id}")
    application_id = build.get("application_id")
    published = build.get("status") in {"published", "ready"}
    c1 = {"pass": published, "status": build.get("status")}
    c2 = dict(zip(("pass", "why"), anchored(application_id))) if published else {
        "pass": False, "why": "未交付，跳过"}

    reference, unseen = REFERENCES[task_id]
    checker = CHECKERS[task_id]
    use_draft = build.get("status") != "published"

    def verify(label: str, inputs: dict) -> dict:
        if not published:
            return {"pass": False, "why": "未交付，跳过"}
        try:
            result = run_workflow(application_id, inputs, use_draft)
        except urllib.error.HTTPError as error:
            return {"pass": False, "why": f"提交失败 {error.code}"}
        if result["status"] != "succeeded":
            return {"pass": False, "why": f"运行 {result['status']}", "outputs": result["outputs"]}
        want = reference(inputs)
        problems = checker(result["outputs"], want)
        return {"pass": not problems, "problems": problems,
                "want": want, "outputs": result["outputs"], "label": label}

    verdict = {
        "task": task_id, "build_id": build_id, "application_id": application_id,
        "builder": build.get("builder"), "judged_at": datetime.now(timezone.utc).isoformat(),
        "C1_delivered": c1,
        "C2_anchored_acceptance": c2,
        "C3_sample_correct": verify("样例", task["sample_inputs"]),
        "C4_unseen_correct": verify("未见", unseen),
    }
    verdict["passed"] = all(
        verdict[key]["pass"] for key in
        ("C1_delivered", "C2_anchored_acceptance", "C3_sample_correct", "C4_unseen_correct")
    )
    return verdict


def report(verdict: dict) -> None:
    mark = {True: "✅", False: "❌"}
    labels = {
        "C1_delivered": "C1 交付成立",
        "C2_anchored_acceptance": "C2 验收锚定具体值",
        "C3_sample_correct": "C3 样例输入算对",
        "C4_unseen_correct": "C4 未见输入算对（照妖镜）",
    }
    print("\n" + "═" * 68)
    print(f"{verdict['task']}  构建 {verdict['build_id'][:8]}  引擎 {verdict['builder']}")
    print("═" * 68)
    for key, label in labels.items():
        block = verdict[key]
        detail = block.get("why") or "；".join(block.get("problems") or [])
        print(f"{mark[bool(block['pass'])]} {label}"
              + (f"  — {detail[:160]}" if detail and not block["pass"] else ""))
    print("─" * 68)
    print(("🏆 通过——四条件全过" if verdict["passed"] else "判负") + "\n")


def main() -> None:
    task_map, defaults = tasks()
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=sorted(REFERENCES))
    parser.add_argument("--all", action="store_true", help="跑全部有参照解的任务")
    parser.add_argument("--builder", default="classic", choices=["classic", "mechanical"])
    parser.add_argument("--coordinator", default="local2/Qwen/Qwen3-32B")
    parser.add_argument("--teammate", default="local/Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument("--max-turns", type=int, default=int(defaults.get("max_turns", 36)))
    parser.add_argument("--max-seconds", type=float,
                        default=float(defaults.get("max_elapsed_seconds", 900)))
    parser.add_argument("--judge-only")
    args = parser.parse_args()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    targets = sorted(REFERENCES) if args.all else [args.task] if args.task else []
    if not targets:
        parser.error("给 --task，或用 --all")

    results = []
    for task_id in targets:
        task = task_map[task_id]
        if args.judge_only:
            build_id = api("GET", f"/api/v1/builds/{args.judge_only}")["id"]
        else:
            application = api("POST", "/api/v1/applications", {
                "name": f"{task_id}-{time.strftime('%m%d-%H%M%S')}",
                "requirement": task["requirement"],
            })
            started = api("POST", f"/api/v1/applications/{application['id']}/builds", {
                "requirement": task["requirement"],
                "builder": args.builder,
                "auto_publish": bool(defaults.get("auto_publish", False)),
                "max_turns": args.max_turns,
                "max_elapsed_seconds": args.max_seconds,
                "coordinator_model": args.coordinator,
                "teammate_models": [args.teammate],
            })
            build_id = started["build_id"]
            print(f"[{task_id}] 构建 {build_id[:8]} 已发起（{args.builder}）")
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

        verdict = judge(build_id, task_id)
        report(verdict)
        out = RUNS_DIR / (f"{time.strftime('%Y%m%d-%H%M%S')}-{args.builder}-"
                          f"{task_id}-{build_id[:8]}.json")
        out.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"判卷已落盘：{out.relative_to(REPO)}")
        results.append(verdict)

    if len(results) > 1:
        passed = sum(1 for item in results if item["passed"])
        print(f"\n合计：{passed}/{len(results)} 通过")
    raise SystemExit(0 if all(item["passed"] for item in results) else 1)


if __name__ == "__main__":
    main()
