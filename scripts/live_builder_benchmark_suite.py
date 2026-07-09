#!/usr/bin/env python3
"""Run one bounded paid Builder build and evaluate it with the benchmark suite."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT_PATH = ROOT / "docs" / "workingon" / "experiment_paid_builder_benchmark_result_2026_07_09.json"
CONFIGURED_RESULT_PATH = Path(
    os.getenv("LIVE_BUILDER_BENCHMARK_RESULT_PATH", str(DEFAULT_RESULT_PATH))
)
RESULT_PATH = CONFIGURED_RESULT_PATH if CONFIGURED_RESULT_PATH.is_absolute() else ROOT / CONFIGURED_RESULT_PATH
CONFIGURED_REUSE_SOURCE_PATH = Path(os.getenv("LIVE_BUILDER_BENCHMARK_REUSE_SOURCE_PATH", str(RESULT_PATH)))
REUSE_SOURCE_PATH = (
    CONFIGURED_REUSE_SOURCE_PATH
    if CONFIGURED_REUSE_SOURCE_PATH.is_absolute()
    else ROOT / CONFIGURED_REUSE_SOURCE_PATH
)
BASE_URL = os.getenv("AGENT_PLATFORM_URL", "http://127.0.0.1:8001")
MAX_TURNS = int(os.getenv("LIVE_BUILDER_BENCHMARK_MAX_TURNS", "36"))
MAX_REPAIR_CYCLES = int(os.getenv("LIVE_BUILDER_BENCHMARK_MAX_REPAIR_CYCLES", "1"))
TIMEOUT_SECONDS = float(os.getenv("LIVE_BUILDER_BENCHMARK_TIMEOUT_SECONDS", "900"))
RUNNER_MODE = os.getenv("LIVE_BUILDER_BENCHMARK_MODE", "inprocess")
REUSE_RESULT = os.getenv("LIVE_BUILDER_BENCHMARK_REUSE_RESULT", "0") == "1"
PLANNING_MODE = os.getenv("LIVE_BUILDER_BENCHMARK_PLANNING_MODE", "auto")
BENCHMARK_CASE = os.getenv("LIVE_BUILDER_BENCHMARK_CASE", "summary_smoke")


@dataclass(frozen=True)
class BenchmarkCaseSpec:
    name: str
    application_name_prefix: str
    suite_name: str
    suite_description: str
    case_name: str
    requirement: str
    reference: dict[str, Any]
    required_node_types: list[str]
    required_harness_nodes: list[str]
    minimum_score: float = 0.8
    minimum_pass_rate: float = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request(client: httpx.Client | TestClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = BASE_URL + path if isinstance(client, httpx.Client) else path
    response = client.request(method, url, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {path}: expected JSON object")
    return data


def wait_build(client: httpx.Client | TestClient, build_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = request(client, "GET", f"/api/v1/builds/{build_id}")
        print("build", build_id, current["status"])
        if current["status"] in {"ready", "published", "needs_attention", "cancelled"}:
            return current
        time.sleep(3)
    raise TimeoutError(f"build timed out after {TIMEOUT_SECONDS} seconds")


def get_task(client: httpx.Client | TestClient, task_id: str) -> dict[str, Any] | None:
    try:
        return request(client, "GET", f"/api/v1/platform/harness/tasks/{task_id}")
    except Exception:
        return None


def task_from_event_log(task_id: str) -> dict[str, Any] | None:
    events_dir = ROOT / ".tmp" / "live_builder_benchmark_runtime" / "data" / "events"
    if not events_dir.exists():
        return None
    latest: dict[str, Any] | None = None
    for path in events_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            task = event.get("data", {}).get("task")
            if isinstance(task, dict) and task.get("id") == task_id:
                latest = task
    return latest


def summary_reference_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "Input",
                "config": {"inputs": [{"name": "text", "type": "string"}]},
            },
            {
                "id": "model",
                "type": "model_turn",
                "title": "Summarize",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["text"]}},
                    "settings": {"prompt": "Summarize the input in three concise bullets."},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "End",
                "config": {
                    "outputs": {"summary": {"$ref": {"node_id": "model", "path": ["output"]}}},
                },
            },
        ],
        "edges": [
            {
                "id": "a",
                "source": "start",
                "target": "model",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "b",
                "source": "model",
                "target": "end",
                "source_port": "output",
                "target_port": "input",
            },
        ],
    }


def complex_research_brief_reference_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "Research Inputs",
                "config": {
                    "inputs": [
                        {"name": "question", "type": "string"},
                        {"name": "source_notes", "type": "string"},
                        {"name": "audience", "type": "string", "required": False},
                    ],
                },
            },
            {
                "id": "extract_constraints",
                "type": "parameter_extractor",
                "title": "Extract Research Constraints",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["question"]}},
                    "fields": [
                        {"name": "topic", "type": "string", "description": "Main research topic"},
                        {"name": "deliverable", "type": "string", "description": "Expected output format"},
                        {"name": "constraints", "type": "array", "description": "Important constraints", "required": False},
                    ],
                    "instruction": "Extract a typed research brief request.",
                },
            },
            {
                "id": "classify_style",
                "type": "question_classifier",
                "title": "Classify Brief Style",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["question"]}},
                    "classes": ["technical", "strategy", "mixed"],
                    "instruction": "Classify the best research brief style.",
                },
            },
            {
                "id": "assemble_context",
                "type": "context_assembler",
                "title": "Assemble Evidence Context",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["source_notes"]}},
                    "settings": {
                        "include": [
                            {"$ref": {"node_id": "extract_constraints", "path": ["output"]}},
                            {"$ref": {"node_id": "classify_style", "path": ["class"]}},
                        ],
                    },
                },
            },
            {
                "id": "draft_brief",
                "type": "model_turn",
                "title": "Draft Structured Brief",
                "config": {
                    "input": {"$ref": {"node_id": "assemble_context", "path": ["output"]}},
                    "settings": {
                        "prompt": "Write a concise research brief with findings, risks, and next actions.",
                    },
                },
            },
            {
                "id": "render_markdown",
                "type": "template_transform",
                "title": "Render Markdown",
                "config": {
                    "template": "# Brief\n\nAudience: {{ audience }}\n\n{{ brief }}",
                    "variables": {
                        "audience": {"$ref": {"node_id": "start", "path": ["audience"]}},
                        "brief": {"$ref": {"node_id": "draft_brief", "path": ["output"]}},
                    },
                },
            },
            {
                "id": "record_event",
                "type": "event_recorder",
                "title": "Record Brief Event",
                "config": {
                    "input": {"$ref": {"node_id": "render_markdown", "path": ["text"]}},
                    "settings": {"event": "research_brief_generated"},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Return Brief",
                "config": {
                    "outputs": {
                        "brief": {"$ref": {"node_id": "render_markdown", "path": ["text"]}},
                        "style": {"$ref": {"node_id": "classify_style", "path": ["class"]}},
                    },
                },
            },
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "extract_constraints", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "start", "target": "classify_style", "source_port": "output", "target_port": "input"},
            {"id": "c", "source": "extract_constraints", "target": "assemble_context", "source_port": "output", "target_port": "input"},
            {"id": "d", "source": "classify_style", "target": "assemble_context", "source_port": "output", "target_port": "input"},
            {"id": "e", "source": "assemble_context", "target": "draft_brief", "source_port": "output", "target_port": "input"},
            {"id": "f", "source": "draft_brief", "target": "render_markdown", "source_port": "output", "target_port": "input"},
            {"id": "g", "source": "render_markdown", "target": "record_event", "source_port": "text", "target_port": "input"},
            {"id": "h", "source": "record_event", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }


def benchmark_cases() -> dict[str, BenchmarkCaseSpec]:
    summary_requirement = """
搭建并验证一个可编辑的文章摘要 BlockFlow：
1. Start 接收 text 字符串输入。
2. 使用模型节点把 text 总结为三条简洁要点。
3. End 返回 summary。
4. 添加带 frame 的强制结构测试，至少断言 summary 存在并且类型为 string。
5. auto_publish=false 时完成测试后停在 ready 状态。
""".strip()
    complex_requirement = """
搭建并验证一个复杂多模块研究简报 BlockFlow：
1. Start 接收 question、source_notes 和可选 audience。
2. 用参数提取模块抽取 topic、deliverable、constraints 等结构化研究约束。
3. 用问题分类模块判断 brief 风格，至少区分 technical、strategy、mixed。
4. 组装 source_notes、提取结果和分类结果，形成模型可用上下文。
5. 使用模型推理节点生成结构化研究简报，内容包含 findings、risks、next_actions。
6. 使用模板转换节点渲染最终 Markdown brief。
7. 加入 event_recorder 或等价 soft harness 节点，记录生成事件，方便测试和审计。
8. End 返回 brief 和 style。
9. 添加带 frame 的强制结构测试，测试必须包含 required_node_types，覆盖可审计架构。
10. auto_publish=false 时完成测试后停在 ready 状态。
""".strip()
    return {
        "summary_smoke": BenchmarkCaseSpec(
            name="summary_smoke",
            application_name_prefix="Live Benchmark Summary",
            suite_name="paid builder summary smoke",
            suite_description="One paid Builder-generated candidate evaluated by suite endpoint.",
            case_name="summary blockflow smoke",
            requirement=summary_requirement,
            reference=summary_reference_workflow(),
            required_node_types=["start", "model_turn", "end"],
            required_harness_nodes=[],
        ),
        "complex_research_brief": BenchmarkCaseSpec(
            name="complex_research_brief",
            application_name_prefix="Live Benchmark Complex Brief",
            suite_name="paid builder complex research brief",
            suite_description="One paid Builder-generated complex multi-module candidate evaluated by suite endpoint.",
            case_name="complex research brief blockflow",
            requirement=complex_requirement,
            reference=complex_research_brief_reference_workflow(),
            required_node_types=[
                "start",
                "parameter_extractor",
                "question_classifier",
                "context_assembler",
                "model_turn",
                "template_transform",
                "event_recorder",
                "end",
            ],
            required_harness_nodes=["event_recorder"],
        ),
    }


def get_benchmark_case(name: str) -> BenchmarkCaseSpec:
    cases = benchmark_cases()
    try:
        return cases[name]
    except KeyError as error:
        raise RuntimeError(f"unknown LIVE_BUILDER_BENCHMARK_CASE={name!r}; expected one of {sorted(cases)}") from error


def write_result(result: dict[str, Any]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("result", RESULT_PATH)


def main() -> None:
    load_dotenv(ROOT / ".env")
    api_token = os.getenv("API_TOKEN")
    if not api_token:
        raise RuntimeError("API_TOKEN is not configured")

    benchmark_case = get_benchmark_case(BENCHMARK_CASE)
    requirement = benchmark_case.requirement
    result: dict[str, Any] = {
        "status": "started",
        "started_at": utc_now(),
        "finished_at": None,
        "base_url": BASE_URL,
        "runner_mode": RUNNER_MODE,
        "benchmark_case": benchmark_case.name,
        "planning_mode": PLANNING_MODE,
        "reuse_result": REUSE_RESULT,
        "reuse_source_path": str(REUSE_SOURCE_PATH) if REUSE_RESULT else "",
        "max_turns": MAX_TURNS,
        "max_repair_cycles": MAX_REPAIR_CYCLES,
        "requirement": requirement,
        "application_id": None,
        "build_id": None,
        "build_status": None,
        "build_error": "",
        "health": {},
        "draft_counts": {},
        "builder_task": None,
        "benchmark_task": None,
        "benchmark_report": None,
        "error": "",
    }
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        if RUNNER_MODE == "http":
            client_context = httpx.Client(headers=headers, timeout=120)
        else:
            runtime_root = ROOT / ".tmp" / "live_builder_benchmark_runtime"
            settings = Settings(
                api_token=api_token,
                data_dir=runtime_root / "data",
                workspace_root=runtime_root / "workspaces",
                deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
                deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", Settings().deepseek_base_url),
                deepseek_generator_model=os.getenv(
                    "DEEPSEEK_GENERATOR_MODEL",
                    Settings().deepseek_generator_model,
                ),
                deepseek_runtime_model=os.getenv(
                    "DEEPSEEK_RUNTIME_MODEL",
                    Settings().deepseek_runtime_model,
                ),
                deepseek_timeout_seconds=float(
                    os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(Settings().deepseek_timeout_seconds))
                ),
            )
            settings.prepare()
            client_context = TestClient(create_app(settings), headers=headers)

        with client_context as client:
            health = request(client, "GET", "/health")
            result["health"] = {
                "status": health.get("status"),
                "deepseek_configured": health.get("deepseek_configured"),
                "generator_model": health.get("generator_model"),
                "runtime_model": health.get("runtime_model"),
            }
            if not health.get("deepseek_configured"):
                raise RuntimeError("backend reports deepseek_configured=false")
            if REUSE_RESULT and REUSE_SOURCE_PATH.exists():
                previous = json.loads(REUSE_SOURCE_PATH.read_text(encoding="utf-8"))
                result["application_id"] = previous.get("application_id")
                result["build_id"] = previous.get("build_id")
                if not result["application_id"] or not result["build_id"]:
                    raise RuntimeError("reuse requested but previous result has no application/build id")
                completed = request(client, "GET", f"/api/v1/builds/{result['build_id']}")
            else:
                application = request(
                    client,
                    "POST",
                    "/api/v1/applications",
                    json={
                        "name": f"{benchmark_case.application_name_prefix} {uuid4().hex[:6]}",
                        "description": f"paid Builder benchmark experiment: {benchmark_case.name}",
                        "requirement": requirement,
                        "mode": "workflow",
                    },
                )
                result["application_id"] = application["id"]
                build = request(
                    client,
                    "POST",
                    f"/api/v1/applications/{application['id']}/builds",
                    json={
                        "requirement": requirement,
                        "auto_publish": False,
                        "max_turns": MAX_TURNS,
                        "max_repair_cycles": MAX_REPAIR_CYCLES,
                        "planning_mode": PLANNING_MODE,
                    },
                )
                result["build_id"] = build["build_id"]
                completed = wait_build(client, build["build_id"])
            result["build_status"] = completed["status"]
            result["build_error"] = completed.get("error", "")
            result["builder_task"] = (
                get_task(client, str(result["build_id"]))
                or task_from_event_log(str(result["build_id"]))
            )

            draft = request(client, "GET", f"/api/v1/applications/{result['application_id']}/draft")
            snapshot = draft["snapshot"]
            workflow = snapshot["workflow"]
            tests = snapshot.get("tests", [])
            result["draft_counts"] = {
                "nodes": len(workflow.get("nodes", [])),
                "edges": len(workflow.get("edges", [])),
                "tests": len(tests),
                "node_types": sorted({node.get("type") for node in workflow.get("nodes", [])}),
            }
            model_calls = 0
            tool_calls = 0
            builder_task = result.get("builder_task") or {}
            if isinstance(builder_task, dict):
                counts = builder_task.get("usage_counts") or {}
                model_calls = int(counts.get("model_call", 0))
                tool_calls = int(counts.get("tool_call", 0))
            suite = request(
                client,
                "POST",
                "/api/v1/builder-benchmark/suites/evaluate",
                json={
                    "name": benchmark_case.suite_name,
                    "description": benchmark_case.suite_description,
                    "minimum_score": benchmark_case.minimum_score,
                    "minimum_pass_rate": benchmark_case.minimum_pass_rate,
                    "cost": {
                        "model_calls": model_calls,
                        "tool_calls": tool_calls,
                        "provider": "deepseek",
                        "model": str(health.get("generator_model") or ""),
                        "notes": "model/tool call counts come from Platform Harness usage records",
                    },
                    "cases": [
                        {
                            "name": benchmark_case.case_name,
                            "requirement": requirement,
                            "reference": benchmark_case.reference,
                            "candidate": workflow,
                            "required_node_types": benchmark_case.required_node_types,
                            "required_harness_nodes": benchmark_case.required_harness_nodes,
                            "tests": tests,
                        }
                    ],
                },
            )
            result["benchmark_report"] = suite["report"]
            result["benchmark_task"] = get_task(client, suite["task_id"])
            if completed["status"] not in {"ready", "published"}:
                result["status"] = "build_failed_benchmark_evaluated"
            else:
                result["status"] = "passed" if suite["report"].get("passed") else "benchmark_failed"
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
    finally:
        result["finished_at"] = utc_now()
        write_result(result)
        print("status", result["status"])


if __name__ == "__main__":
    main()
