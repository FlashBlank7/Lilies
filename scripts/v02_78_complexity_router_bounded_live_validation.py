#!/usr/bin/env python3
"""Execute or skip the v0.2.78 bounded live validation for the complexity router."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, ContentBlock
from agent_platform.providers.deepseek import DeepSeekProvider


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.76" / "plan_v0.2.76_complexity_router_live_validation.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "live_v0.2.78_complexity_router_bounded_validation"


def load_plan() -> dict[str, Any]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def provider_configured() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY") or Settings().deepseek_api_key)


def build_skip_result(command: str) -> dict[str, Any]:
    plan = load_plan()
    safety = complexity_router_default_safety_gate()
    model = os.getenv("DEEPSEEK_GENERATOR_MODEL", Settings().deepseek_generator_model)
    return {
        "version": "v0.2.78",
        "status": "skipped",
        "reason": "DEEPSEEK_API_KEY is not configured",
        "provider": "deepseek",
        "model": model,
        "command": command,
        "source_plan": PLAN_PATH.relative_to(ROOT).as_posix(),
        "case_budget": plan["budget_boundary"]["max_live_cases"],
        "case_results": [
            {
                "case_id": case["case_id"],
                "requirement": case["requirement"],
                "expected_class": case["expected_class"],
                "status": "skipped",
                "reason": "DEEPSEEK_API_KEY is not configured",
            }
            for case in plan["validation_cases"]
        ],
        "metrics_capture": plan["metrics_capture"],
        "pass_fail": {
            "passed": False,
            "reason": "live validation was skipped because provider credentials are unavailable",
        },
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
    }


async def run_live_validation(command: str) -> dict[str, Any]:
    plan = load_plan()
    safety = complexity_router_default_safety_gate()
    settings = Settings()
    api_key = os.getenv("DEEPSEEK_API_KEY") or settings.deepseek_api_key
    if not api_key:
        return build_skip_result(command)
    model = os.getenv("DEEPSEEK_GENERATOR_MODEL", settings.deepseek_generator_model)
    provider = DeepSeekProvider(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", settings.deepseek_base_url),
        timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(settings.deepseek_timeout_seconds))),
    )
    case_results = []
    for case in plan["validation_cases"][: plan["budget_boundary"]["max_live_cases"]]:
        started = time.perf_counter()
        prompt = classify_prompt(case["requirement"])
        raw_text = await collect_text(provider, model, prompt)
        parsed = parse_json_object(raw_text)
        predicted = str(parsed.get("class", "")).strip().casefold()
        passed = predicted == case["expected_class"]
        case_results.append({
            "case_id": case["case_id"],
            "requirement": case["requirement"],
            "expected_class": case["expected_class"],
            "status": "passed" if passed else "failed",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "raw_text": raw_text[:2000],
            "parsed": parsed,
            "predicted_class": predicted,
            "passed": passed,
        })
    metrics = {
        "classification_distribution": distribution(case_results),
        "override_rate": 0.0,
        "fallback_unknown_rate": distribution(case_results).get("unknown", 0) / max(len(case_results), 1),
        "success_rate_by_class": success_rate_by_class(case_results),
        "cost_latency_by_class": latency_by_class(case_results),
    }
    passed = (
        len(case_results) == len(plan["validation_cases"])
        and all(result.get("passed") for result in case_results)
        and safety["default_enabled"] is False
    )
    return {
        "version": "v0.2.78",
        "status": "completed" if passed else "failed",
        "reason": "all live cases passed" if passed else "one or more live cases failed",
        "provider": "deepseek",
        "model": model,
        "command": command,
        "source_plan": PLAN_PATH.relative_to(ROOT).as_posix(),
        "case_budget": plan["budget_boundary"]["max_live_cases"],
        "case_results": case_results,
        "metrics_capture": plan["metrics_capture"],
        "metrics": metrics,
        "pass_fail": {"passed": passed, "reason": "all criteria satisfied" if passed else "criteria not satisfied"},
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
    }


def classify_prompt(requirement: str) -> str:
    return (
        "Classify this software-building requirement as exactly one of simple, medium, complex, or unknown. "
        "Return ONLY JSON like {\"class\":\"simple\",\"reason\":\"...\"}. "
        "Use simple for small single-surface changes, medium for bounded API/workflow/integration tasks, "
        "complex for platform/architecture/live/model-sensitive/guardrail work, and unknown for insufficient text.\n"
        f"Requirement: {requirement}"
    )


async def collect_text(provider: DeepSeekProvider, model: str, prompt: str) -> str:
    stream = provider.stream(
        model=model,
        system="Return compact, valid JSON. Do not include markdown fences.",
        messages=[ChatMessage(role="user", content=[ContentBlock(type="text", text=prompt)])],
        tools=[],
        max_output_tokens=1024,
        thinking_enabled=False,
        effort="high",
        tool_choice=None,
        user_id="v02-78-complexity-router-live-validation",
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


def distribution(case_results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in case_results:
        key = str(result.get("predicted_class") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def success_rate_by_class(case_results: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, int] = {}
    successes: dict[str, int] = {}
    for result in case_results:
        key = str(result.get("expected_class") or "unknown")
        totals[key] = totals.get(key, 0) + 1
        if result.get("passed"):
            successes[key] = successes.get(key, 0) + 1
    return {key: round(successes.get(key, 0) / total, 3) for key, total in totals.items()}


def latency_by_class(case_results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    values: dict[str, list[float]] = {}
    for result in case_results:
        key = str(result.get("expected_class") or "unknown")
        values.setdefault(key, []).append(float(result.get("duration_seconds") or 0.0))
    return {
        key: {"avg_duration_seconds": round(sum(items) / max(len(items), 1), 3)}
        for key, items in values.items()
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.78 complexity-router bounded live validation",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Reason: {result['reason']}",
        f"- Provider/model: `{result['provider']}` / `{result['model']}`",
        f"- Command: `{result['command']}`",
        f"- Default enabled: `{result['default_enabled']}`",
        f"- Allowed to enable default: `{result['allowed_to_enable_default']}`",
        "",
        "| Case | Expected class | Status | Reason / predicted |",
        "| --- | --- | --- | --- |",
    ]
    for case in result["case_results"]:
        detail = case.get("reason") or case.get("predicted_class") or "none"
        lines.append(f"| `{case['case_id']}` | `{case['expected_class']}` | `{case['status']}` | {detail} |")
    lines.extend(["", "## Pass / Fail", "", result["pass_fail"]["reason"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    command = ".venv/bin/python scripts/v02_78_complexity_router_bounded_live_validation.py"
    result = await run_live_validation(command)
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
