"""并发压测探针：N 路并发运行，实测 SQLite 单文件库的真实行为。

自建纯确定性工作流（start → 公式 → end，零模型调用），并发发起运行，
统计成功率、时长分布、database-locked 类错误。商用文档需要"已知并发
上限"数据，猜的不算。

用法：python scripts/concurrency_probe.py [--n 5] [--rounds 2]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8001"


def token() -> str:
    value = os.environ.get("API_TOKEN", "")
    if not value:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("API_TOKEN="):
                value = line.split("=", 1)[1].strip()
    return value


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def build_probe_app() -> str:
    app = call("POST", "/api/v1/applications", {
        "name": f"并发压测-{uuid4().hex[:6]}",
        "requirement": "并发压测专用：纯确定性计算流程。",
    })
    app_id = app["id"]
    revision = call("GET", f"/api/v1/applications/{app_id}/draft")["revision"]

    def mutate(op: str, data: dict) -> None:
        nonlocal revision
        result = call("POST", f"/api/v1/applications/{app_id}/draft", {
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        })
        revision = result["revision"]

    mutate("add_node", {"node": {
        "id": "start", "type": "start", "title": "开始",
        "config": {"inputs": [
            {"name": "x", "label": "数值", "type": "number", "required": True, "example": 7},
        ]}}})
    mutate("add_node", {"node": {
        "id": "calc", "type": "variable_assigner", "title": "计算",
        "config": {"assignments": {"y": {"$formula": {
            "expression": "x*x+1",
            "vars": {"x": {"$ref": {"node_id": "start", "path": ["output", "x"]}}},
        }}}}}})
    mutate("add_node", {"node": {
        "id": "end", "type": "end", "title": "结束",
        "config": {"outputs": {"y": {"$ref": {"node_id": "calc", "path": ["output", "y"]}}}}}})
    mutate("add_edge", {"edge": {"id": "e1", "source": "start", "target": "calc",
                                 "source_port": "output", "target_port": "input"}})
    mutate("add_edge", {"edge": {"id": "e2", "source": "calc", "target": "end",
                                 "source_port": "output", "target_port": "input"}})
    call("POST", f"/api/v1/applications/{app_id}/versions", {"acknowledge_warnings": True})
    return app_id


def one_run(app_id: str, x: int) -> dict:
    started = time.time()
    try:
        run = call("POST", f"/api/v1/applications/{app_id}/runs", {"inputs": {"x": x}})
        run_id = run["run_id"]
        while True:
            record = call("GET", f"/api/v1/runs/{run_id}")
            if record["status"] not in ("queued", "running"):
                break
            if time.time() - started > 60:
                return {"ok": False, "error": "timeout", "seconds": 60.0}
            time.sleep(0.2)
        outputs = (record["state"].get("outputs") or {}).get("end") or {}
        expected = x * x + 1
        ok = record["status"] == "succeeded" and outputs.get("y") == expected
        return {
            "ok": ok,
            "status": record["status"],
            "seconds": round(time.time() - started, 2),
            "error": "" if ok else f"y={outputs.get('y')} 期望 {expected} / {record['state'].get('error','')}"[:120],
        }
    except Exception as error:
        return {"ok": False, "error": str(error)[:160], "seconds": round(time.time() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()

    app_id = build_probe_app()
    print(f"压测应用 {app_id[:8]}，{args.rounds} 轮 × {args.n} 并发")
    for round_index in range(1, args.rounds + 1):
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.n) as pool:
            results = list(pool.map(lambda i: one_run(app_id, i + round_index * 100), range(args.n)))
        ok = sum(1 for r in results if r["ok"])
        times = sorted(r["seconds"] for r in results)
        print(f"第 {round_index} 轮：{ok}/{args.n} 成功 | 时长 min {times[0]}s / max {times[-1]}s")
        for r in results:
            if not r["ok"]:
                print("   ✗", r.get("status", "?"), r["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
