#!/usr/bin/env python3
"""Run a paid, real DeepSeek + Docker end-to-end acceptance test."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx


BASE_URL = os.getenv("AGENT_PLATFORM_URL", "http://127.0.0.1:8000")
API_TOKEN = os.environ["API_TOKEN"]
ROOT = Path(__file__).resolve().parents[1]


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, BASE_URL + path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def wait_generation(client: httpx.Client, generation_id: str) -> dict:
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        current = request(client, "GET", f"/v1/agent-generations/{generation_id}")
        print(f"generation={current['status']}")
        if current["status"] in {"published", "draft"}:
            return current
        if current["status"] == "failed":
            raise RuntimeError(current.get("error") or "generation failed")
        time.sleep(3)
    raise TimeoutError("generation timed out")


def wait_turn(client: httpx.Client, session_id: str) -> None:
    after = 0
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        events = request(client, "GET", f"/v1/streams/{session_id}?after={after}")
        for event in events:
            after = max(after, event["id"])
            event_type, data = event["type"], event["data"]
            print(event_type, json.dumps(data, ensure_ascii=False)[:500])
            if event_type == "permission.requested":
                request(
                    client,
                    "POST",
                    f"/v1/sessions/{session_id}/permissions/{data['request_id']}",
                    json={"behavior": "allow"},
                )
            if event_type == "turn.completed":
                return
            if event_type == "turn.failed":
                raise RuntimeError(data.get("error", "turn failed"))
        time.sleep(1)
    raise TimeoutError("agent turn timed out")


def main() -> None:
    workspace_root = Path(os.getenv("WORKSPACE_ROOT", ROOT / "workspaces")).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    relative_workspace = f"acceptance-{uuid4().hex[:8]}"
    workspace = workspace_root / relative_workspace
    shutil.copytree(ROOT / "examples" / "broken_python_project", workspace)

    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    with httpx.Client(headers=headers, timeout=60) as client:
        health = request(client, "GET", "/health")
        if not health["deepseek_configured"]:
            raise RuntimeError("backend has no DEEPSEEK_API_KEY")
        generation = request(
            client,
            "POST",
            "/v1/agent-generations",
            json={
                "requirement": (
                    "生成一个可靠的 Python 测试修复智能体。它必须先运行 python -m pytest -q，"
                    "阅读失败及相关源码，做最小根因修复，再运行同一测试确认。允许 Read、Edit、"
                    "Grep、Glob、Bash、Task。验证命令必须包含 python -m pytest -q。"
                ),
                "workspace_path": relative_workspace,
                "validation_prompt": "运行测试，定位并修复失败，然后重新运行测试确认全部通过。",
                "auto_publish": True,
            },
        )
        generated = wait_generation(client, generation["generation_id"])
        session = request(
            client,
            "POST",
            "/v1/sessions",
            json={"agent_id": generated["agent_id"], "workspace_path": relative_workspace},
        )
        request(
            client,
            "POST",
            f"/v1/sessions/{session['session_id']}/messages",
            json={"content": "再次独立检查这个项目；运行测试，修复所有失败，并验证结果。"},
        )
        wait_turn(client, session["session_id"])

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(workspace)],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    print(result.stdout, result.stderr)
    if result.returncode != 0:
        raise SystemExit("acceptance failed: workspace tests still fail")
    print(f"LIVE ACCEPTANCE PASSED: {workspace}")


if __name__ == "__main__":
    main()
