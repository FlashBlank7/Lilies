#!/usr/bin/env python3
"""Run the v0.2.36 E02 raw JSON vs readable TestFrame review experiment."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402
from agent_platform.models import ChatMessage, ContentBlock, StreamEvent, ToolDefinition  # noqa: E402
from agent_platform.providers.base import ModelProvider, ProviderCapabilities  # noqa: E402
from agent_platform.providers.deepseek import DeepSeekProvider  # noqa: E402

DEFAULT_RESULT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json"
)
RESULT_PATH = Path(os.getenv("E02_REVIEW_EXPERIMENT_RESULT_PATH", str(DEFAULT_RESULT_PATH)))


class UnusedProvider(ModelProvider):
    name = "unused"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("E02 fixture workflow should not call a model provider")


def headers() -> dict[str, str]:
    return {"authorization": "Bearer workflow-test"}


def mutate(
    client: TestClient,
    app_id: str,
    revision: int,
    op: str,
    data: dict[str, Any],
) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": f"e02-{op}-{uuid4()}",
            "op": op,
            "data": data,
        },
    )
    response.raise_for_status()
    return int(response.json()["revision"])


def build_review_artifacts(data_dir: Path | None = None) -> dict[str, Any]:
    data_root = data_dir or ROOT / ".tmp" / "e02_readable_testframe_review"
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_root / "data",
        workspace_root=data_root / "workspaces",
    )
    settings.prepare()
    app = create_app(settings, UnusedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "E02 readable review fixture",
                "requirement": "Generate a test report with content and structure failures.",
            },
        ).json()["id"]
        revision = 0
        for node in [
            {
                "id": "start",
                "type": "start",
                "title": "Review Input",
                "config": {"inputs": [{"name": "prompt", "type": "string"}]},
            },
            {
                "id": "end",
                "type": "end",
                "title": "Draft Output",
                "config": {
                    "outputs": {
                        "outline": "Act I: a detective enters the city. Act II: conflict. Act III: resolution.",
                        "style": "plain",
                    }
                },
            },
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-end",
            "source": "start",
            "target": "end",
            "source_port": "output",
            "target_port": "input",
        }})
        tests = [
            {
                "name": "Novel setting adherence",
                "requirement": "The outline must include the required magic system setting.",
                "frame": {
                    "title": "Outline and setting adherence",
                    "category": "content",
                    "purpose": "Check whether the generated novel outline follows the required setting contract.",
                    "reviewer_guidance": "Review this before style polishing; a failure means the generation prompt or template lost key setting constraints.",
                    "reference": "E02 fixture requirement: outline must mention the magic system.",
                    "failure_target": "template_transform or model_turn prompt",
                },
                "inputs": {"prompt": "Write a fantasy outline with a magic system."},
                "assertions": [
                    {"path": ["outline"], "operator": "contains", "expected": "magic system"}
                ],
                "required_node_types": ["start", "end"],
                "feedback_hints": [
                    "Inspect the generation prompt for missing setting constraints.",
                    "Check whether the final template dropped the user's required setting.",
                ],
            },
            {
                "name": "Visible context assembly",
                "requirement": "The workflow should include an explicit context assembly step.",
                "frame": {
                    "title": "Visible context assembly gate",
                    "category": "structure",
                    "purpose": "Check whether the BlockFlow exposes context assembly instead of hiding it inside one black-box prompt.",
                    "reviewer_guidance": "A failure points to workflow architecture, not content quality.",
                    "reference": "E02 fixture requirement: context assembly must be inspectable.",
                    "failure_target": "missing context_assembler node",
                },
                "inputs": {"prompt": "Summarize notes into a brief."},
                "assertions": [{"path": ["assembled_context"], "operator": "exists"}],
                "required_node_types": ["start", "end"],
                "feedback_hints": [
                    "Add a context_assembler node before the LLM or template stage.",
                    "Do not rely on direct prompt concatenation for context assembly.",
                ],
            },
        ]
        for test in tests:
            revision = mutate(client, app_id, revision, "add_test", {"test": test})
        response = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        response.raise_for_status()
        actual_report = response.json()

    raw_packet = legacy_raw_packet(actual_report)
    readable_packet = readable_testframe_packet(actual_report)
    answer_key = [
        {
            "title": "Outline and setting adherence",
            "category": "content",
            "status": "failed",
            "failure_target": "template_transform or model_turn prompt",
            "first_repair": "inspect generation prompt/template for missing magic system constraints",
        },
        {
            "title": "Visible context assembly gate",
            "category": "structure",
            "status": "failed",
            "failure_target": "missing context_assembler node",
            "first_repair": "add a context_assembler node before generation",
        },
    ]
    return {
        "actual_report": actual_report,
        "packets": {
            "raw_legacy_json": raw_packet,
            "readable_testframe": readable_packet,
        },
        "answer_key": answer_key,
        "deterministic_metrics": {
            "raw_legacy_json": deterministic_metrics(raw_packet, "raw_legacy_json"),
            "readable_testframe": deterministic_metrics(readable_packet, "readable_testframe"),
        },
    }


def legacy_raw_packet(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": report["passed"],
        "summary": {
            key: value
            for key, value in report["summary"].items()
            if key != "frames"
        },
        "tests": [
            {
                "test_id": item["test_id"],
                "name": item["name"],
                "mandatory": item["mandatory"],
                "passed": item["passed"],
                "run_id": item["run_id"],
                "assertions": item["assertions"],
                "tool_evidence": item["tool_evidence"],
            }
            for item in report["tests"]
        ],
    }


def readable_testframe_packet(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": report["passed"],
        "summary": report["summary"],
        "tests": [
            {
                "test_id": item["test_id"],
                "name": item["name"],
                "mandatory": item["mandatory"],
                "passed": item["passed"],
                "frame": item["frame"],
                "readable_report": item["readable_report"],
                "tool_evidence": item["tool_evidence"],
            }
            for item in report["tests"]
        ],
    }


def deterministic_metrics(packet: dict[str, Any], condition: str) -> dict[str, Any]:
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2)
    if condition == "readable_testframe":
        explicit = {
            "title": True,
            "category": True,
            "status": True,
            "failure_target": True,
            "feedback_hints": True,
        }
        paths_per_test = 5
    else:
        explicit = {
            "title": False,
            "category": False,
            "status": True,
            "failure_target": False,
            "feedback_hints": False,
        }
        paths_per_test = 9
    return {
        "serialized_chars": len(serialized),
        "test_count": len(packet["tests"]),
        "explicit_fields": explicit,
        "estimated_json_paths_to_answer_per_test": paths_per_test,
        "estimated_total_json_paths": paths_per_test * len(packet["tests"]),
    }


async def run_paid_reviewer_proxy(artifacts: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    api_key = os.getenv("DEEPSEEK_API_KEY") or settings.deepseek_api_key
    if not api_key:
        return {"status": "skipped", "reason": "DEEPSEEK_API_KEY is not configured", "reviews": {}}
    model = os.getenv("DEEPSEEK_GENERATOR_MODEL", settings.deepseek_generator_model)
    provider = DeepSeekProvider(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", settings.deepseek_base_url),
        timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(settings.deepseek_timeout_seconds))),
    )
    reviews: dict[str, Any] = {}
    for condition, packet in artifacts["packets"].items():
        started = time.perf_counter()
        prompt = reviewer_prompt(condition, packet)
        text = await collect_text(provider, model, prompt)
        duration = round(time.perf_counter() - started, 3)
        parsed = parse_json_object(text)
        reviews[condition] = {
            "condition": condition,
            "model": model,
            "prompt_chars": len(prompt),
            "duration_seconds": duration,
            "raw_text": text,
            "parsed": parsed,
            "score": score_review(parsed, artifacts["answer_key"]),
        }
    return {
        "status": "completed",
        "provider": "deepseek",
        "model": model,
        "reviews": reviews,
    }


def reviewer_prompt(condition: str, packet: dict[str, Any]) -> str:
    return (
        "You are reviewing a Lilies workflow test result. Return ONLY valid JSON with this schema: "
        "{\"tests\":[{\"title\":\"\",\"category\":\"\",\"status\":\"passed|failed\","
        "\"failure_target\":\"\",\"first_repair_action\":\"\",\"confidence\":0.0}],"
        "\"overall\":\"\"}. "
        "For each test, identify the acceptance category, status, likely failure target, and first repair action. "
        f"Condition: {condition}. Packet:\n"
        f"{json.dumps(packet, ensure_ascii=False, indent=2)}"
    )


async def collect_text(provider: DeepSeekProvider, model: str, prompt: str) -> str:
    stream = provider.stream(
        model=model,
        system="Return compact, valid JSON. Do not include markdown fences.",
        messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
        tools=[],
        max_output_tokens=4096,
        thinking_enabled=False,
        effort="high",
        tool_choice=None,
        user_id="e02-readable-testframe-review",
    )
    chunks: list[str] = []
    async for event in stream:
        if event.type == "content_block_start":
            raw = event.data.get("content_block", {})
            if raw.get("type") == "text" and raw.get("text"):
                chunks.append(raw["text"])
        elif event.type == "content_block_delta":
            delta = event.data.get("delta", {})
            if delta.get("type") == "text_delta":
                chunks.append(delta.get("text", ""))
    return "".join(chunks).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"_non_object": value}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {"_non_object": value}
            except json.JSONDecodeError:
                pass
    return {"_parse_error": text[:2000]}


def score_review(parsed: dict[str, Any], answer_key: list[dict[str, str]]) -> dict[str, Any]:
    tests = parsed.get("tests")
    if not isinstance(tests, list):
        return {"score": 0.0, "matched_fields": 0, "total_fields": len(answer_key) * 4}
    matched = 0
    total = len(answer_key) * 4
    for expected in answer_key:
        candidate = best_candidate(tests, expected["title"])
        if not candidate:
            continue
        if str(candidate.get("category", "")).casefold() == expected["category"]:
            matched += 1
        if str(candidate.get("status", "")).casefold() == expected["status"]:
            matched += 1
        if expected["failure_target"].casefold() in str(candidate.get("failure_target", "")).casefold():
            matched += 1
        repair = str(candidate.get("first_repair_action", "")).casefold()
        if any(token in repair for token in expected["first_repair"].casefold().split()[:4]):
            matched += 1
    return {"score": round(matched / max(total, 1), 3), "matched_fields": matched, "total_fields": total}


def best_candidate(candidates: list[Any], title: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = -1
    title_terms = {term for term in re.split(r"\\W+", title.casefold()) if term}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_terms = {term for term in re.split(r"\\W+", str(candidate.get("title", "")).casefold()) if term}
        score = len(title_terms & candidate_terms)
        if score > best_score:
            best = candidate
            best_score = score
    return best


async def main() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    artifacts = build_review_artifacts()
    paid_review = await run_paid_reviewer_proxy(artifacts)
    result = {
        "status": "completed" if paid_review["status"] == "completed" else "completed_without_paid_review",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "experiment_id": "E02_readable_testframe_review_v0.2.36",
        "question": "Does readable TestFrame/report output improve reviewer comprehension and repair targeting compared with raw JSON-style test output?",
        "evidence_level": "deterministic_metrics_plus_paid_model_proxy",
        **artifacts,
        "paid_reviewer_proxy": paid_review,
        "conclusion_boundary": (
            "This is not a real human panel. It can support engineering confidence in readable reports "
            "and provide a review packet protocol, but it must not be reported as measured human review time."
        ),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(RESULT_PATH)
    print(result["status"])


if __name__ == "__main__":
    asyncio.run(main())
