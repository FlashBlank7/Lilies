#!/usr/bin/env python3
"""Paid DeepSeek acceptance for incremental brick construction and published execution."""

from __future__ import annotations

import os
import time

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




def call(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, BASE_URL + path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def wait_for(client: httpx.Client, path: str, terminal: set[str], timeout: float = 2400):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = call(client, "GET", path)
        print(path, current["status"])
        if current["status"] in terminal:
            return current
        time.sleep(2)
    raise TimeoutError(path)


def main() -> None:
    _confirm_or_exit("跑一次增量搭建 + 发布执行的付费验收")
    API_TOKEN = _api_token()
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    requirement = """
搭建并交付一个可编辑的技术支持分流智能体工作流：
1. User Input 接收 query。
2. Question Classifier 将请求分成 coding 和 general 两类。
3. 两个分支分别调用配置不同的 Claude Agent；coding Agent 可使用 Read、Glob、Grep、Bash，
   general Agent 不允许写文件。
4. 分支输出用 Variable Aggregator 汇合，End 返回 answer。
5. 为两类输入各建立一个真实强制验收用例，至少断言 answer 存在且为字符串。
6. 必须通过增量积木工具搭建、运行测试并修复，测试全部通过后发布。
""".strip()
    with httpx.Client(headers=headers, timeout=120) as client:
        health = call(client, "GET", "/health")
        if not health["deepseek_configured"]:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured in the backend environment")
        application = call(
            client,
            "POST",
            "/api/v1/applications",
            json={
                "name": "Technical Support Router",
                "description": "DeepSeek live workflow acceptance",
                "requirement": requirement,
                "mode": "workflow",
            },
        )
        build = call(
            client,
            "POST",
            f"/api/v1/applications/{application['id']}/builds",
            json={"requirement": requirement, "auto_publish": True, "max_turns": 100},
        )
        completed = wait_for(
            client,
            f"/api/v1/builds/{build['build_id']}",
            {"published", "needs_attention", "cancelled"},
        )
        if completed["status"] != "published":
            raise RuntimeError(completed.get("error") or "builder did not publish")
        version = completed["team_state"]["published_version"]
        run = call(
            client,
            "POST",
            f"/api/v1/applications/{application['id']}/runs",
            json={"inputs": {"query": "Explain why a Python pytest fixture may not be discovered."}},
        )
        result = wait_for(
            client,
            f"/api/v1/runs/{run['run_id']}",
            {"succeeded", "failed", "paused", "cancelled"},
        )
        if result["status"] != "succeeded" or not result["outputs"]:
            raise RuntimeError(f"published run failed: {result}")
        draft_before = call(client, "GET", f"/api/v1/applications/{application['id']}/draft")
        restored = call(
            client,
            "POST",
            f"/api/v1/applications/{application['id']}/versions/{version}/restore",
        )
        if restored["revision"] != draft_before["revision"] + 1:
            raise RuntimeError("published version was not restored as a new editable draft revision")
        print(
            "LIVE WORKFLOW ACCEPTANCE PASSED",
            {"application_id": application["id"], "version": version, "run_id": run["run_id"]},
        )


if __name__ == "__main__":
    main()
