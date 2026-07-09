#!/usr/bin/env python3
"""Run the v0.2.38 E05 template reuse-depth live comparison experiment."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402

DEFAULT_RESULT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.38_e05_template_reuse_depth_2026_07_09.json"
)
RESULT_PATH = Path(os.getenv("E05_REUSE_DEPTH_RESULT_PATH", str(DEFAULT_RESULT_PATH)))
MAX_TURNS = int(os.getenv("E05_REUSE_DEPTH_MAX_TURNS", "42"))
MAX_REPAIR_CYCLES = int(os.getenv("E05_REUSE_DEPTH_MAX_REPAIR_CYCLES", "2"))
TIMEOUT_SECONDS = float(os.getenv("E05_REUSE_DEPTH_TIMEOUT_SECONDS", "900"))
PROVIDER_TIMEOUT_SECONDS = float(os.getenv("E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS", "120"))
BENCHMARK_MINIMUM_PASS_RATE = float(os.getenv("E05_BENCHMARK_MINIMUM_PASS_RATE", "1.0"))
MAX_ELAPSED_SECONDS = (
    float(os.environ["E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS"])
    if os.getenv("E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS")
    else None
)
SKIP_PAID = os.getenv("E05_REUSE_DEPTH_SKIP_PAID", "0") == "1"
RUN_ID = os.getenv("E05_REUSE_DEPTH_RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
EXPERIMENT_VERSION = os.getenv("E05_REUSE_DEPTH_EXPERIMENT_VERSION", "v0.2.38")
EXPERIMENT_CASE = os.getenv("E05_REUSE_DEPTH_CASE", "code_review")
SELECTED_ARMS_ENV = "E05_REUSE_DEPTH_ONLY_ARMS"


@dataclass(frozen=True)
class ReuseDepthArm:
    depth: str
    instruction: str
    expected_action: str


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    title: str
    template_name: str
    preflight_query: str
    base_requirement: str
    benchmark_reference: dict[str, Any]
    required_node_types: list[str]


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


def result_path() -> Path:
    return RESULT_PATH if RESULT_PATH.is_absolute() else ROOT / RESULT_PATH


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def request(client: TestClient, method: str, path: str, token: str, **kwargs: Any) -> Any:
    response = client.request(method, path, headers=headers(token), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def depth_arms() -> list[ReuseDepthArm]:
    return [
        ReuseDepthArm(
            depth="none",
            instruction=(
                "Set BuildPlan.reuse_depth to 'none'. Call template_suggestions with reuse_depth='none' "
                "to prove templates are intentionally disabled, then build the BlockFlow from scratch."
            ),
            expected_action="build_from_scratch",
        ),
        ReuseDepthArm(
            depth="shallow",
            instruction=(
                "Set BuildPlan.reuse_depth to 'shallow'. Call template_suggestions with reuse_depth='shallow' "
                "before draft mutations. If a relevant template is suggested, reuse its structure lightly while "
                "keeping the final BlockFlow editable and tested."
            ),
            expected_action="expand_template",
        ),
        ReuseDepthArm(
            depth="deep",
            instruction=(
                "Set BuildPlan.reuse_depth to 'deep'. Call template_suggestions with reuse_depth='deep' "
                "before draft mutations. Compose the workflow around reusable template modules where possible, "
                "and record the reuse decision in the BuildPlan."
            ),
            expected_action="compose_modules",
        ),
    ]


def selected_arm_depths(raw: str | None = None) -> list[str]:
    allowed = [arm.depth for arm in depth_arms()]
    source = raw if raw is not None else os.getenv(SELECTED_ARMS_ENV, "")
    if not source.strip():
        return allowed
    requested: list[str] = []
    for chunk in source.split(","):
        normalized = chunk.strip().casefold()
        if not normalized:
            continue
        if normalized not in requested:
            requested.append(normalized)
    if not requested:
        raise ValueError(f"{SELECTED_ARMS_ENV} did not contain any valid arm values")
    unknown = sorted(set(requested) - set(allowed))
    if unknown:
        raise ValueError(
            f"unknown E05 arm(s) in {SELECTED_ARMS_ENV}: {', '.join(unknown)}; "
            f"allowed values: {', '.join(allowed)}"
        )
    return [depth for depth in allowed if depth in requested]


def selected_arms(raw: str | None = None) -> list[ReuseDepthArm]:
    selected = set(selected_arm_depths(raw))
    return [arm for arm in depth_arms() if arm.depth in selected]


def code_review_case() -> ExperimentCase:
    reference = {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "Code Review Inputs",
                "config": {
                    "inputs": [
                        {"name": "task", "type": "string"},
                        {"name": "repository_path", "type": "string"},
                        {"name": "failing_test_output", "type": "string"},
                        {"name": "test_command", "type": "string", "required": False},
                    ],
                },
            },
            {
                "id": "review",
                "type": "llm",
                "title": "Review And Repair Analysis",
                "config": {
                    "system": "Analyze failing tests and produce a minimal repair plan.",
                    "prompt": {
                        "$ref": {
                            "node_id": "start",
                            "path": ["task"],
                        }
                    },
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Return Review Report",
                "config": {
                    "outputs": {
                        "report": {"$ref": {"node_id": "review", "path": ["text"]}},
                        "tests_passed": False,
                        "repair_summary": {"$ref": {"node_id": "review", "path": ["text"]}},
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "start-review",
                "source": "start",
                "target": "review",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "review-end",
                "source": "review",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            },
        ],
    }
    requirement = """
Build and validate an editable code review and repair BlockFlow.

The BlockFlow must:
1. Start with inputs: task, repository_path, failing_test_output, and test_command.
2. Analyze the code review task and failing test output.
3. Produce a minimal repair plan and a reviewer-facing final report.
4. Include an explicit tested output contract with a readable frame.
5. Include required_node_types in the mandatory test so the workflow remains auditable.
6. End with outputs: report, tests_passed, and repair_summary.

This requirement is intentionally related to the built-in code_reviewer template
and should expose whether Template reuse depth changes Builder behavior.
""".strip()
    return ExperimentCase(
        name="code_review",
        title="Code review and repair",
        template_name="code_reviewer",
        preflight_query="code review testing debugging quality",
        base_requirement=requirement,
        benchmark_reference=reference,
        required_node_types=["start", "llm", "end"],
    )


def customer_support_router_case() -> ExperimentCase:
    reference = {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "Customer Message",
                "config": {
                    "inputs": [
                        {"name": "customer_message", "type": "string"},
                        {"name": "customer_tier", "type": "string", "required": False},
                    ],
                },
            },
            {
                "id": "classify",
                "type": "question_classifier",
                "title": "Classify Customer Intent",
                "config": {
                    "classes": ["complaint", "question", "feedback", "urgent"],
                    "input": {"$ref": {"node_id": "start", "path": ["customer_message"]}},
                },
            },
            {
                "id": "route",
                "type": "if_else",
                "title": "Route By Intent",
                "config": {
                    "cases": [
                        {
                            "id": "urgent",
                            "conditions": [
                                {
                                    "value": {"$ref": {"node_id": "classify", "path": ["branch"]}},
                                    "operator": "equals",
                                    "expected": "urgent",
                                }
                            ],
                        }
                    ],
                    "default_branch": "standard",
                },
            },
            {
                "id": "format",
                "type": "template_transform",
                "title": "Format Support Response",
                "config": {
                    "template": "Intent: {{ intent }}\nPriority: {{ priority }}\nResponse: {{ response }}",
                    "variables": {
                        "intent": {"$ref": {"node_id": "classify", "path": ["branch"]}},
                        "priority": "normal",
                        "response": "Acknowledge the customer and route to the correct support queue.",
                    },
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Return Support Decision",
                "config": {
                    "outputs": {
                        "intent": {"$ref": {"node_id": "classify", "path": ["branch"]}},
                        "response": {"$ref": {"node_id": "format", "path": ["text"]}},
                        "priority": "normal",
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "start-classify",
                "source": "start",
                "target": "classify",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "classify-route",
                "source": "classify",
                "target": "route",
                "source_port": "branch",
                "target_port": "input",
            },
            {
                "id": "route-format",
                "source": "route",
                "target": "format",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "format-end",
                "source": "format",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            },
        ],
    }
    requirement = """
Build and validate an editable customer support routing BlockFlow.

The BlockFlow must:
1. Start with inputs: customer_message and optional customer_tier.
2. Classify the message into complaint, question, feedback, or urgent.
3. Route urgent/complaint cases differently from standard questions.
4. Produce a reviewer-facing support response with intent, priority, and response text.
5. Include a mandatory test with required_node_types so the workflow remains auditable.
6. End with outputs: intent, response, and priority.

This requirement is intentionally related to the built-in customer_support_router template
and should expose whether Template reuse depth generalizes beyond code-review workflows.
""".strip()
    return ExperimentCase(
        name="customer_support_router",
        title="Customer support routing",
        template_name="customer_support_router",
        preflight_query="customer support routing classification urgent complaint",
        base_requirement=requirement,
        benchmark_reference=reference,
        required_node_types=["start", "question_classifier", "if_else", "template_transform", "end"],
    )


def data_analyzer_case() -> ExperimentCase:
    reference = {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "Analysis Request",
                "config": {
                    "inputs": [
                        {"name": "data_description", "type": "string"},
                        {"name": "analysis_goal", "type": "string", "required": False},
                    ],
                },
            },
            {
                "id": "analyze",
                "type": "llm",
                "title": "Perform Analysis",
                "config": {
                    "system": "Provide a statistical summary, anomalies, and recommendations.",
                    "prompt": {
                        "$ref": {
                            "node_id": "start",
                            "path": ["data_description"],
                        }
                    },
                },
            },
            {
                "id": "extract",
                "type": "parameter_extractor",
                "title": "Extract Structured Statistics",
                "config": {
                    "input": {"$ref": {"node_id": "analyze", "path": ["text"]}},
                    "fields": [
                        {"name": "record_count", "type": "number", "required": False},
                        {"name": "anomalies_found", "type": "number", "required": False},
                        {"name": "key_findings", "type": "string", "required": True},
                    ],
                },
            },
            {
                "id": "format",
                "type": "template_transform",
                "title": "Format Report",
                "config": {
                    "template": "# Data Analysis Report\n\n{{ findings }}",
                    "variables": {
                        "findings": {
                            "$ref": {
                                "node_id": "extract",
                                "path": ["structured", "key_findings"],
                            }
                        }
                    },
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Return Analysis Outputs",
                "config": {
                    "outputs": {
                        "report": {"$ref": {"node_id": "format", "path": ["text"]}},
                        "statistics": {"$ref": {"node_id": "extract", "path": ["structured"]}},
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "start-analyze",
                "source": "start",
                "target": "analyze",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "analyze-extract",
                "source": "analyze",
                "target": "extract",
                "source_port": "text",
                "target_port": "input",
            },
            {
                "id": "extract-format",
                "source": "extract",
                "target": "format",
                "source_port": "structured",
                "target_port": "input",
            },
            {
                "id": "format-end",
                "source": "format",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            },
        ],
    }
    requirement = """
Build and validate an editable data analysis BlockFlow.

The BlockFlow must:
1. Start with inputs: data_description and optional analysis_goal.
2. Produce a statistical summary, anomaly notes, and recommendations from the described dataset.
3. Extract structured statistics including key_findings, and optionally record_count and anomalies_found.
4. Produce a reviewer-facing formatted report.
5. Include a mandatory test with required_node_types so the workflow remains auditable.
6. End with outputs: report and statistics.

This requirement is intentionally related to the built-in data_analyzer template
and should expose whether Template reuse depth generalizes beyond code-review and customer-support workflows.
""".strip()
    return ExperimentCase(
        name="data_analyzer",
        title="Data analysis reporting",
        template_name="data_analyzer",
        preflight_query="data analysis statistics anomalies reporting",
        base_requirement=requirement,
        benchmark_reference=reference,
        required_node_types=["start", "llm", "parameter_extractor", "template_transform", "end"],
    )


def experiment_case(name: str = EXPERIMENT_CASE) -> ExperimentCase:
    normalized = name.strip().casefold().replace("-", "_")
    if normalized in {"code_review", "code_reviewer"}:
        return code_review_case()
    if normalized in {"customer_support", "customer_support_router", "support_router"}:
        return customer_support_router_case()
    if normalized in {"data_analysis", "data_analyzer", "analysis"}:
        return data_analyzer_case()
    raise ValueError(f"unknown E05 experiment case: {name}")


def base_requirement(case: ExperimentCase | None = None) -> str:
    return (case or experiment_case()).base_requirement


def requirement_for_arm(arm: ReuseDepthArm, case: ExperimentCase | None = None) -> str:
    selected = case or experiment_case()
    return f"{selected.base_requirement}\n\nE05 reuse-depth instruction:\n{arm.instruction}"


def benchmark_reference(case: ExperimentCase | None = None) -> dict[str, Any]:
    return (case or experiment_case()).benchmark_reference


def build_request_payload(requirement: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requirement": requirement,
        "auto_publish": False,
        "max_turns": MAX_TURNS,
        "max_repair_cycles": MAX_REPAIR_CYCLES,
        "planning_mode": "required",
    }
    if MAX_ELAPSED_SECONDS is not None:
        payload["max_elapsed_seconds"] = MAX_ELAPSED_SECONDS
    return payload


def build_settings(api_token: str) -> Settings:
    default_settings = Settings()
    generator_model = os.getenv(
        "DEEPSEEK_GENERATOR_MODEL",
        default_settings.deepseek_generator_model,
    )
    runtime_model = os.getenv("E05_REUSE_DEPTH_RUNTIME_MODEL") or generator_model
    runtime_root = ROOT / ".tmp" / "e05_template_reuse_depth" / RUN_ID
    return Settings(
        api_token=api_token,
        data_dir=runtime_root / "data",
        workspace_root=runtime_root / "workspaces",
        templates_dir=ROOT / "templates",
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or default_settings.deepseek_api_key,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", default_settings.deepseek_base_url),
        deepseek_generator_model=generator_model,
        deepseek_runtime_model=runtime_model,
        deepseek_timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
    )


def wait_build(client: TestClient, build_id: str, token: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = request(client, "GET", f"/api/v1/builds/{build_id}", token)
        print("build", build_id, current["status"], flush=True)
        if current["status"] in {"ready", "published", "needs_attention", "cancelled"}:
            if not isinstance(current, dict):
                raise RuntimeError("build endpoint returned non-object")
            return current
        time.sleep(3)
    raise TimeoutError(f"build timed out after {TIMEOUT_SECONDS} seconds")


def parse_operation_result(event: dict[str, Any]) -> Any:
    result = event.get("data", {}).get("result")
    if not isinstance(result, str):
        return None
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    operation_counts: dict[str, int] = {}
    template_suggestions: list[dict[str, Any]] = []
    template_expands: list[dict[str, Any]] = []
    failed_operations: list[dict[str, Any]] = []
    provider_failure_events: list[dict[str, Any]] = []
    model_timeout_events: list[dict[str, Any]] = []
    needs_attention_events: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        data = event.get("data", {})
        if event_type.endswith(".model.failed") and isinstance(data, dict):
            provider_failure_events.append({
                "type": event_type,
                "model": data.get("model"),
                "error": data.get("error"),
                "error_type": data.get("error_type"),
                "retryable": data.get("retryable"),
                "status_code": data.get("status_code"),
            })
        if event_type.endswith(".model.timeout") and isinstance(data, dict):
            model_timeout_events.append({
                "type": event_type,
                "model": data.get("model"),
                "timeout_seconds": data.get("timeout_seconds"),
            })
        if event_type == "build.needs_attention" and isinstance(data, dict):
            needs_attention_events.append({
                "error": data.get("error"),
                "error_type": data.get("error_type"),
                "failure": data.get("failure"),
            })
        if event_type != "build.operation":
            continue
        tool = str(data.get("tool") or "")
        operation_counts[tool] = operation_counts.get(tool, 0) + 1
        parsed = parse_operation_result(event)
        if tool == "template_suggestions" and isinstance(parsed, dict):
            template_suggestions.append({
                "input": data.get("input", {}),
                "success": data.get("success"),
                "reuse_depth": parsed.get("reuse_depth"),
                "recommended_action": parsed.get("recommended_action"),
                "templates": parsed.get("templates", []),
            })
        if tool == "template_expand":
            template_expands.append({
                "input": data.get("input", {}),
                "success": data.get("success"),
                "result": parsed,
            })
        if not data.get("success", False):
            failed_operations.append({
                "tool": tool,
                "input": data.get("input", {}),
                "result": parsed,
            })
    return {
        "operation_counts": operation_counts,
        "template_suggestion_count": operation_counts.get("template_suggestions", 0),
        "template_expand_count": operation_counts.get("template_expand", 0),
        "template_suggestions": template_suggestions,
        "template_expands": template_expands,
        "failed_operations": failed_operations,
        "provider_failure_events": provider_failure_events,
        "model_timeout_events": model_timeout_events,
        "needs_attention_events": needs_attention_events,
    }


def summarize_failure(
    completed: dict[str, Any],
    builder_task: dict[str, Any] | None,
    event_summary: dict[str, Any],
) -> dict[str, Any]:
    build_error = str(completed.get("error") or "")
    task_failure = None
    if isinstance(builder_task, dict):
        metadata = builder_task.get("metadata")
        if isinstance(metadata, dict):
            task_failure = metadata.get("failure")
    event_failures = [
        event.get("failure")
        for event in event_summary.get("needs_attention_events", [])
        if isinstance(event, dict) and event.get("failure") is not None
    ]
    provider_failures = event_summary.get("provider_failure_events", [])
    timeout_events = event_summary.get("model_timeout_events", [])
    timeout_like = "timeout" in build_error.casefold() or "timed out" in build_error.casefold()
    for candidate in [task_failure, *event_failures, *provider_failures, *timeout_events]:
        if isinstance(candidate, dict):
            timeout_like = timeout_like or bool(candidate.get("timeout_like"))
            timeout_like = timeout_like or "timeout" in str(candidate.get("error", "")).casefold()
            timeout_like = timeout_like or "timed out" in str(candidate.get("error", "")).casefold()
    return {
        "build_status": completed.get("status"),
        "build_error": build_error,
        "task_status": builder_task.get("status") if isinstance(builder_task, dict) else None,
        "task_error": builder_task.get("error") if isinstance(builder_task, dict) else None,
        "task_failure": task_failure,
        "event_failures": event_failures,
        "provider_failure_event_count": len(provider_failures),
        "model_timeout_event_count": len(timeout_events),
        "timeout_like": timeout_like,
    }


def summarize_benchmark_report(report: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {
            "suite_passed": None,
            "suite_score": None,
            "suite_pass_rate": None,
            "case_count": 0,
            "case_passed": None,
            "case_score": None,
            "failed_cases": [],
            "missing": {},
        }
    case_reports = report.get("reports")
    first_case = case_reports[0] if isinstance(case_reports, list) and case_reports else {}
    first_case = first_case if isinstance(first_case, dict) else {}
    return {
        "suite_passed": report.get("passed"),
        "suite_score": report.get("score"),
        "suite_pass_rate": report.get("pass_rate"),
        "case_count": report.get("case_count"),
        "case_passed": first_case.get("passed"),
        "case_score": first_case.get("score"),
        "failed_cases": report.get("failed_cases", []),
        "missing": first_case.get("missing", {}),
    }


def task_usage_counts(build: dict[str, Any]) -> dict[str, int]:
    task = build.get("task") if isinstance(build.get("task"), dict) else None
    if not task:
        return {}
    counts = task.get("usage_counts")
    if not isinstance(counts, dict):
        return {}
    return {
        "model_call": int(counts.get("model_call", 0)),
        "tool_call": int(counts.get("tool_call", 0)),
    }


def get_harness_task(client: TestClient, token: str, task_id: str) -> dict[str, Any] | None:
    try:
        task = request(client, "GET", f"/api/v1/platform/harness/tasks/{task_id}", token)
    except Exception:
        return None
    return task if isinstance(task, dict) else None


def draft_counts(draft: dict[str, Any]) -> dict[str, Any]:
    workflow = draft.get("snapshot", {}).get("workflow", {})
    tests = draft.get("snapshot", {}).get("tests", [])
    nodes = workflow.get("nodes", [])
    edges = workflow.get("edges", [])
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "tests": len(tests),
        "node_types": sorted({str(node.get("type")) for node in nodes}),
    }


def run_template_preflight(client: TestClient, token: str, case: ExperimentCase | None = None) -> dict[str, Any]:
    selected = case or experiment_case()
    templates = request(client, "GET", "/api/v1/templates", token)
    server_templates = request(client, "GET", "/api/v1/templates/categories", token)
    suggestions: dict[str, Any] = {}
    for arm in selected_arms():
        suggestions[arm.depth] = request(
            client,
            "GET",
            (
                "/api/v1/templates/suggestions"
                f"?requirement={selected.preflight_query.replace(' ', '%20')}"
                f"&reuse_depth={arm.depth}"
            ),
            token,
        )
    return {
        "case": selected.name,
        "template_name": selected.template_name,
        "template_count": len(templates) if isinstance(templates, list) else 0,
        "templates": templates,
        "categories": server_templates,
        "suggestions": suggestions,
    }


def run_arm(client: TestClient, token: str, arm: ReuseDepthArm, case: ExperimentCase | None = None) -> dict[str, Any]:
    selected = case or experiment_case()
    requirement = requirement_for_arm(arm, selected)
    started = time.perf_counter()
    application = request(
        client,
        "POST",
        "/api/v1/applications",
        token,
        json={
            "name": f"E05 {arm.depth} reuse {uuid4().hex[:6]}",
            "description": f"E05 template reuse-depth experiment arm: {arm.depth}",
            "requirement": requirement,
            "mode": "workflow",
        },
    )
    build_payload = build_request_payload(requirement)
    build = request(
        client,
        "POST",
        f"/api/v1/applications/{application['id']}/builds",
        token,
        json=build_payload,
    )
    completed = wait_build(client, build["build_id"], token)
    elapsed = round(time.perf_counter() - started, 3)
    builder_task = get_harness_task(client, token, build["build_id"])
    draft = request(client, "GET", f"/api/v1/applications/{application['id']}/draft", token)
    events = request(client, "GET", f"/v1/streams/{build['build_id']}", token)
    if not isinstance(events, list):
        raise RuntimeError("stream endpoint returned non-list")
    workflow = draft["snapshot"]["workflow"]
    tests = draft["snapshot"].get("tests", [])
    usage_counts = task_usage_counts({"task": builder_task} if builder_task else completed)
    suite = request(
        client,
        "POST",
        "/api/v1/builder-benchmark/suites/evaluate",
        token,
        json={
            "name": f"E05 template reuse-depth {selected.name} comparison",
            "description": f"Benchmark arm for reuse_depth={arm.depth}.",
            "minimum_score": 0.45,
            "minimum_pass_rate": BENCHMARK_MINIMUM_PASS_RATE,
            "cost": {
                "model_calls": usage_counts.get("model_call", 0),
                "tool_calls": usage_counts.get("tool_call", 0),
                "provider": "deepseek",
                "model": "",
                "notes": "Counts come from Platform Harness task usage when available.",
            },
            "cases": [
                {
                    "name": f"{selected.name} blockflow reuse_depth={arm.depth}",
                    "requirement": requirement,
                    "reference": benchmark_reference(selected),
                    "candidate": workflow,
                    "required_node_types": selected.required_node_types,
                    "tests": tests,
                }
            ],
        },
    )
    team_state = completed.get("team_state") or {}
    build_plan = team_state.get("build_plan") if isinstance(team_state, dict) else None
    event_summary = summarize_events(events)
    failure_summary = summarize_failure(completed, builder_task, event_summary)
    benchmark_report = suite.get("report")
    return {
        "depth": arm.depth,
        "case": selected.name,
        "expected_action": arm.expected_action,
        "status": "completed",
        "elapsed_seconds": elapsed,
        "application_id": application["id"],
        "build_id": build["build_id"],
        "build_status": completed.get("status"),
        "build_error": completed.get("error", ""),
        "builder_task": builder_task,
        "build_plan": build_plan,
        "build_plan_reuse_depth": build_plan.get("reuse_depth") if isinstance(build_plan, dict) else None,
        "usage_counts": usage_counts,
        "draft_counts": draft_counts(draft),
        "event_summary": event_summary,
        "failure_summary": failure_summary,
        "benchmark_outcome": summarize_benchmark_report(benchmark_report),
        "benchmark_report": benchmark_report,
    }


def write_result(result: dict[str, Any]) -> None:
    path = result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("result", path)


def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("E05_REUSE_DEPTH_API_TOKEN") or os.getenv("API_TOKEN") or "workflow-test"
    settings = build_settings(token)
    settings.prepare()
    selected_case = experiment_case()
    result: dict[str, Any] = {
        "status": "started",
        "started_at": utc_now(),
        "finished_at": None,
        "experiment": "E05 template reuse-depth live comparison",
        "version": EXPERIMENT_VERSION,
        "case": {
            "name": selected_case.name,
            "title": selected_case.title,
            "template_name": selected_case.template_name,
            "required_node_types": selected_case.required_node_types,
        },
        "budget": {
            "arms": [arm.depth for arm in selected_arms()],
            "max_turns_per_arm": MAX_TURNS,
            "max_repair_cycles_per_arm": MAX_REPAIR_CYCLES,
            "max_elapsed_seconds_per_arm": MAX_ELAPSED_SECONDS,
            "timeout_seconds_per_arm": TIMEOUT_SECONDS,
            "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
            "benchmark_minimum_pass_rate": BENCHMARK_MINIMUM_PASS_RATE,
            "skip_paid": SKIP_PAID,
            "run_id": RUN_ID,
        },
        "health": {},
        "models": {},
        "preflight": {},
        "arms": [],
        "error": "",
    }
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            health = request(client, "GET", "/health", token)
            models = request(client, "GET", "/v1/models", token)
            result["health"] = {
                "status": health.get("status"),
                "deepseek_configured": health.get("deepseek_configured"),
            }
            result["models"] = {
                "provider": models.get("provider"),
                "generator_model": models.get("generator_model"),
                "runtime_model": models.get("runtime_model"),
                "configured_models": models.get("configured_models", []),
            }
            result["preflight"] = run_template_preflight(client, token, selected_case)
            if SKIP_PAID:
                result["status"] = "skipped"
                result["error"] = "E05_REUSE_DEPTH_SKIP_PAID=1"
                return
            if not health.get("deepseek_configured"):
                result["status"] = "blocked"
                result["error"] = "backend reports deepseek_configured=false"
                return
            for arm in selected_arms():
                try:
                    result["arms"].append(run_arm(client, token, arm, selected_case))
                except Exception as error:
                    result["arms"].append({
                        "depth": arm.depth,
                        "expected_action": arm.expected_action,
                        "status": "error",
                        "error": str(error),
                    })
            if any(arm.get("status") == "completed" for arm in result["arms"]):
                result["status"] = "completed"
            elif result["arms"]:
                result["status"] = "error"
            else:
                result["status"] = "blocked"
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["error"] = "KeyboardInterrupt"
        raise
    finally:
        result["finished_at"] = utc_now()
        write_result(result)
        print("status", result["status"])


if __name__ == "__main__":
    main()
