#!/usr/bin/env python3
"""Run the industrial task package against a live platform and report.

For each task: create an application, start a Builder build, follow it via
the transcript API, and record what actually happened — status, turns, tool
calls, failures, node types, and (when a draft exists) a real run with the
task's sample inputs checked against the structural acceptance fields.

The output is a per-task JSON report plus a console table. This is the
measurement loop for "improve the platform until it completes the package":
run → read transcripts → fix the platform → run again.

Usage:
  python3 scripts/run_benchmark.py --token-env API_TOKEN            # all tasks
  python3 scripts/run_benchmark.py --task T1-procurement-reconciliation
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "benchmarks" / "industrial_v1" / "tasks.json"


def request(base: str, token: str, method: str, path: str, payload: dict | None = None) -> dict | list:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode())


def follow_build(base: str, token: str, build_id: str, deadline_seconds: float) -> dict:
    started = time.time()
    while time.time() - started < deadline_seconds + 120:
        build = request(base, token, "GET", f"/api/v1/builds/{build_id}")
        if build["status"] not in {"queued", "building"}:
            return build
        time.sleep(10)
    return request(base, token, "GET", f"/api/v1/builds/{build_id}")


def terminal_outputs(base: str, token: str, application_id: str, run: dict) -> dict:
    """Collect the workflow's terminal (end/answer) outputs, not every node's."""

    per_node = (run.get("state") or {}).get("outputs") or {}
    try:
        draft = request(base, token, "GET", f"/api/v1/applications/{application_id}/draft")
        terminal_ids = {
            node["id"]
            for node in draft["snapshot"]["workflow"]["nodes"]
            if node["type"] in {"end", "answer"}
        }
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError):
        terminal_ids = set()
    merged: dict = {}
    for node_id, value in per_node.items():
        if not terminal_ids or node_id in terminal_ids:
            if isinstance(value, dict):
                merged.update(value)
    return merged


def run_workflow(
    base: str,
    token: str,
    application_id: str,
    inputs: dict,
    human_input: dict | None = None,
    timeout_s: float = 300,
) -> dict:
    started = request(
        base, token, "POST",
        f"/api/v1/applications/{application_id}/runs",
        {"inputs": inputs, "use_draft": True},
    )
    run_id = started["run_id"] if isinstance(started, dict) and "run_id" in started else started.get("id")
    t0 = time.time()
    resumed = False
    while time.time() - t0 < timeout_s:
        run = request(base, token, "GET", f"/api/v1/runs/{run_id}")
        if run["status"] == "paused" and human_input is not None and not resumed:
            # Play the on-duty human: answer the waiting human_input node once.
            request(base, token, "POST", f"/api/v1/runs/{run_id}/resume", {"values": human_input})
            resumed = True
            continue
        if run["status"] in {"succeeded", "failed", "cancelled", "paused"}:
            return run
        time.sleep(5)
    return {"status": "timeout", "id": run_id}


def flatten_keys(value, prefix="") -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.add(str(key))
            keys.add(path)
            keys |= flatten_keys(item, path)
    elif isinstance(value, list):
        for item in value[:20]:
            keys |= flatten_keys(item, prefix)
    return keys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-env", default="API_TOKEN")
    parser.add_argument("--task", action="append", help="task id (repeatable); default all")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--skip-run", action="store_true", help="build only, skip the sample-input run")
    args = parser.parse_args()

    token = args.token or os.environ.get(args.token_env, "")
    if not token:
        env_file = REPO / ".env"
        for line in env_file.read_text().splitlines() if env_file.is_file() else []:
            if line.startswith("API_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if not token:
        raise SystemExit("no API token: pass --token or set API_TOKEN")

    package = json.loads(PACKAGE.read_text())
    defaults = package["defaults"]
    tasks = [t for t in package["tasks"] if not args.task or t["id"] in args.task]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = REPO / "benchmarks" / "industrial_v1" / "results" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for task in tasks:
        print(f"\n=== {task['id']} ({task['family']}) ===")
        app = request(args.base_url, token, "POST", "/api/v1/applications", {
            "name": f"[bench] {task['id']}",
            "requirement": task["requirement"],
        })
        max_turns = args.max_turns or defaults["max_turns"]
        build = request(args.base_url, token, "POST", f"/api/v1/applications/{app['id']}/builds", {
            "requirement": task["requirement"],
            "auto_publish": defaults["auto_publish"],
            "max_turns": max_turns,
            "max_elapsed_seconds": defaults["max_elapsed_seconds"],
        })
        print(f"  app={app['id']} build={build['build_id']}")
        final = follow_build(args.base_url, token, build["build_id"], defaults["max_elapsed_seconds"])
        transcript = request(args.base_url, token, "GET", f"/api/v1/builds/{build['build_id']}/transcript")
        summary = transcript["summary"]

        report = {
            "task_id": task["id"],
            "family": task["family"],
            "application_id": app["id"],
            "build_id": build["build_id"],
            "build_status": final["status"],
            "build_error": final.get("error"),
            "turns": summary["turn_count"],
            "tool_calls": summary["tool_call_count"],
            "failed_tool_calls": summary["failed_tool_call_count"],
        }

        if final["status"] in {"ready", "published"}:
            try:
                draft = request(args.base_url, token, "GET", f"/api/v1/applications/{app['id']}/draft")
                node_types = {node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
                required_types = task["acceptance"].get("required_node_types", [])
                any_of = task["acceptance"].get("required_any_node_types", [])
                report["node_types"] = sorted(node_types)
                report["architecture_missing"] = [t for t in required_types if t not in node_types]
                if any_of and not node_types.intersection(any_of):
                    report["architecture_missing"].append("any-of:" + "|".join(any_of))
                report["architecture_pass"] = not report["architecture_missing"]
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as error:
                report["architecture_pass"] = False
                report["architecture_missing"] = [f"error: {error}"]

        if not args.skip_run and final["status"] in {"ready", "published"}:
            try:
                run = run_workflow(
                    args.base_url, token, app["id"],
                    task["sample_inputs"],
                    human_input=task.get("sample_human_input"),
                )
                report["run_status"] = run.get("status")
                outputs = terminal_outputs(args.base_url, token, app["id"], run)
                report["terminal_output_keys"] = sorted(outputs.keys())
                present = flatten_keys(outputs)
                required = task["acceptance"]["structural"]
                report["structural_missing"] = [k for k in required if k not in present]
                report["structural_pass"] = not report["structural_missing"]
            except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as error:
                report["run_status"] = f"error: {error}"
                report["structural_pass"] = False

        (out_dir / f"{task['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        rows.append(report)
        print(
            f"  → {report['build_status']} | turns={report['turns']} "
            f"tools={report['tool_calls']} failed={report['failed_tool_calls']}"
            + (f" | arch={report.get('architecture_pass')}" if "architecture_pass" in report else "")
            + (f" | run={report.get('run_status')} structural_pass={report.get('structural_pass')}"
               if "run_status" in report else "")
        )

    print("\n" + "=" * 72)
    print(f"{'task':<36}{'build':<18}{'turns':<7}{'arch':<7}{'structural'}")
    for row in rows:
        print(
            f"{row['task_id']:<36}{row['build_status']:<18}{row['turns']:<7}"
            f"{str(row.get('architecture_pass', '—')):<7}"
            f"{row.get('structural_pass', '—')}"
        )
    print(f"\nreports: {out_dir}")


if __name__ == "__main__":
    main()
