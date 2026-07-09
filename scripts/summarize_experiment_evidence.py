#!/usr/bin/env python3
"""Generate compact Markdown summaries for experiment evidence JSON files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_DIR = ROOT / "docs" / "experiment-status" / "evidence"


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def scalar(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (int, str)):
        return str(value)
    return default


def compact_error(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value[:180]
    if isinstance(value, dict):
        for key in ("type", "error_type", "message", "error"):
            if value.get(key):
                return scalar(value.get(key))[:180]
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:180]
    return scalar(value)[:180]


def benchmark_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if "case_passed" in value or "case_score" in value:
        return f"case={scalar(value.get('case_passed'))}, score={scalar(value.get('case_score'))}"
    if "passed" in value or "score" in value:
        return f"passed={scalar(value.get('passed'))}, score={scalar(value.get('score'))}"
    if "summary" in value and isinstance(value["summary"], dict):
        summary = value["summary"]
        return f"passed={scalar(summary.get('passed'))}, score={scalar(summary.get('score'))}"
    return ""


def calls_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    model = value.get("model_call") or value.get("model_calls") or value.get("model")
    tool = value.get("tool_call") or value.get("tool_calls") or value.get("tool")
    if model is None and tool is None:
        return ""
    return f"{scalar(model, '0')}/{scalar(tool, '0')}"


def template_summary(event_summary: Any) -> str:
    if not isinstance(event_summary, dict):
        return ""
    suggestions = event_summary.get("template_suggestion_count")
    expands = event_summary.get("template_expand_count")
    if suggestions is None and expands is None:
        return ""
    return f"suggest={scalar(suggestions, '0')}, expand={scalar(expands, '0')}"


def row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("\n", " ") for cell in cells) + " |"


def summarize_arms(data: dict[str, Any]) -> list[str]:
    arms = data.get("arms")
    if not isinstance(arms, list) or not arms:
        return []
    lines = [
        "## Arms",
        "",
        row(["Arm", "Status", "Build", "Elapsed", "Calls", "Template", "Benchmark", "Failure"]),
        row(["---", "---", "---", "---", "---", "---", "---", "---"]),
    ]
    for arm in arms:
        if not isinstance(arm, dict):
            continue
        failure = ""
        failure_summary = arm.get("failure_summary")
        if isinstance(failure_summary, dict):
            failure = compact_error(failure_summary.get("task_failure")) or compact_error(
                failure_summary.get("build_error")
            )
        failure = failure or compact_error(arm.get("build_error")) or compact_error(arm.get("error"))
        lines.append(
            row(
                [
                    scalar(arm.get("depth") or arm.get("name") or arm.get("arm")),
                    scalar(arm.get("status")),
                    scalar(arm.get("build_status")),
                    scalar(arm.get("elapsed_seconds")),
                    calls_summary(arm.get("usage_counts")),
                    template_summary(arm.get("event_summary")),
                    benchmark_summary(arm.get("benchmark_outcome") or arm.get("benchmark_report")),
                    failure,
                ]
            )
        )
    return lines


def summarize_known_sections(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in (
        "local_repair",
        "full_rebuild",
        "local_repair_arm",
        "full_rebuild_arm",
        "raw_condition",
        "readable_condition",
        "paid_builder",
        "benchmark",
        "comparison",
    ):
        value = data.get(key)
        if not isinstance(value, dict):
            continue
        lines.extend([f"## {key}", ""])
        fields = []
        local_repair = value.get("local_repair")
        if isinstance(local_repair, dict):
            fields.extend(
                [
                    ("local_repair.status", scalar(local_repair.get("status"))),
                    ("local_repair.operation_count", scalar(local_repair.get("operation_count"))),
                    ("local_repair.elapsed_seconds", scalar(local_repair.get("elapsed_seconds"))),
                ]
            )
        for report_key in ("before_test_report", "after_test_report", "test_report"):
            report = value.get(report_key)
            if isinstance(report, dict):
                fields.append((f"{report_key}.passed", scalar(report.get("passed"))))
                summary = report.get("summary")
                if isinstance(summary, dict):
                    fields.append((f"{report_key}.summary", f"passed={scalar(summary.get('passed'))}, failed={scalar(summary.get('failed'))}"))
        for field in (
            "status",
            "build_status",
            "passed",
            "score",
            "duration_seconds",
            "elapsed_seconds",
            "model_call",
            "tool_call",
            "error",
        ):
            if field in value:
                fields.append((field, scalar(value.get(field)) or compact_error(value.get(field))))
        if not fields:
            fields = [(k, scalar(v) or compact_error(v)) for k, v in value.items() if not isinstance(v, (dict, list))][:8]
        for k, v in fields:
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    return lines


def summarize_paid_reviewer_proxy(data: dict[str, Any]) -> list[str]:
    proxy = data.get("paid_reviewer_proxy")
    if not isinstance(proxy, dict):
        return []
    reviews = proxy.get("reviews")
    if not isinstance(reviews, dict):
        return []
    lines = [
        "## Paid Reviewer Proxy",
        "",
        row(["Condition", "Model", "Duration", "Score", "Matched", "Main failure target"]),
        row(["---", "---", "---", "---", "---", "---"]),
    ]
    for name, review in reviews.items():
        if not isinstance(review, dict):
            continue
        score = review.get("score") if isinstance(review.get("score"), dict) else {}
        parsed = review.get("parsed") if isinstance(review.get("parsed"), dict) else {}
        tests = parsed.get("tests") if isinstance(parsed.get("tests"), list) else []
        target = ""
        if tests and isinstance(tests[0], dict):
            target = scalar(tests[0].get("failure_target"))
        lines.append(
            row(
                [
                    scalar(review.get("condition") or name),
                    scalar(review.get("model")),
                    scalar(review.get("duration_seconds")),
                    scalar(score.get("score")),
                    f"{scalar(score.get('matched_fields'))}/{scalar(score.get('total_fields'))}",
                    target,
                ]
            )
        )
    lines.append("")
    return lines


def summarize_file(path: Path, output: Path | None = None) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    out = output or path.with_name(f"{path.stem}_summary.md")
    title = (
        data.get("experiment_id")
        or data.get("experiment")
        or (data.get("case") or {}).get("name")
        if isinstance(data.get("case"), dict)
        else None
    )
    title = title or path.stem
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        f"- Raw evidence: `{rel(path)}`",
        f"- Status: `{scalar(data.get('status'))}`",
        f"- Started: `{scalar(data.get('started_at'))}`",
        f"- Finished: `{scalar(data.get('finished_at'))}`",
    ]
    if models:
        lines.append(f"- Provider/model: `{scalar(models.get('provider'))}` / `{scalar(models.get('generator_model') or models.get('runtime_model'))}`")
    if data.get("build_status") is not None:
        lines.append(f"- Build status: `{scalar(data.get('build_status'))}`")
    if data.get("build_error"):
        lines.append(f"- Build error: {compact_error(data.get('build_error'))}")
    if data.get("question"):
        lines.append(f"- Question: {scalar(data.get('question'))[:240]}")
    if data.get("error"):
        lines.append(f"- Error: {compact_error(data.get('error'))}")
    lines.append("")

    arm_lines = summarize_arms(data)
    if arm_lines:
        lines.extend(arm_lines)
        lines.append("")

    known_lines = summarize_known_sections(data)
    if known_lines:
        lines.extend(known_lines)

    reviewer_lines = summarize_paid_reviewer_proxy(data)
    if reviewer_lines:
        lines.extend(reviewer_lines)

    benchmark = data.get("benchmark_report")
    if isinstance(benchmark, dict):
        lines.extend(["## Benchmark", "", f"- {benchmark_summary(benchmark) or 'see raw evidence'}", ""])

    lines.extend(
        [
            "## Reader Guidance",
            "",
            "Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.",
            "",
        ]
    )
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    args = parser.parse_args()
    paths = args.paths or sorted(args.evidence_dir.glob("*.json"))
    for path in paths:
        if "_summary" in path.stem:
            continue
        out = summarize_file(path)
        print(rel(out))


if __name__ == "__main__":
    main()
