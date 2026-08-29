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
def _api_token() -> str:
    """读 API_TOKEN，没有就说人话。

    原先是模块层 os.environ["API_TOKEN"]——导入时就炸，
    连 `--help` 都看不了，报错还是裸的 KeyError: 'API_TOKEN'。
    脚本是给人用的，第一次用的人不该靠猜。
    """
    token = os.environ.get("API_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "要先给 API_TOKEN：\n"
            "  API_TOKEN=<平台的令牌> python " + __file__.split("/")[-1] + " …\n"
            "令牌就是平台启动时用的那个（默认在 .env 或启动命令里）。")
    return token


def _confirm_or_exit(what: str) -> None:
    """这类脚本会**真的建应用、开构建、花模型的钱**，别让它被随手触发。

    2026-08-29：我拿 `--help` 探这批脚本的可用性，结果它们不解析参数，
    传什么都直接开跑（那次因为连不上目标地址才没造成后果）。
    脚本名里带 live 不构成提醒——提醒要在它动手之前打出来。
    """
    import sys

    if os.getenv("LIVE_ACCEPTANCE_YES") == "1" or "--yes" in sys.argv:
        return
    print(f"这个脚本会真的动线上平台：{what}")
    print(f"  目标：{BASE_URL}")
    print("  它会建新应用、开构建、调用付费模型，并留下运行记录。")
    print("确认要跑就加 --yes（或 LIVE_ACCEPTANCE_YES=1）。")
    raise SystemExit(2)


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
    _confirm_or_exit("跑一次完整的付费验收")
    API_TOKEN = _api_token()
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
