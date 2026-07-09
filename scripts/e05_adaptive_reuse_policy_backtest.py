#!/usr/bin/env python3
"""Backtest the adaptive E05 reuse-depth policy against current template families."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.template_store import TemplateStore  # noqa: E402
from agent_platform.template_strategy import (  # noqa: E402
    recommended_action_for_depth,
    resolve_effective_reuse_depth,
    score_template_matches,
)


DEFAULT_OUTPUT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10.json"
)


@dataclass(frozen=True)
class FamilyBacktestCase:
    family: str
    requirement: str
    expected_top_template: str
    best_known_depth: str
    success_envelope: list[str]
    evidence_summary_path: str
    note: str = ""


@dataclass
class FamilyBacktestResult:
    family: str
    requirement: str
    top_template: str
    relevance_score: float
    recommended_depth: str
    recommended_action: str
    policy_reason: str
    best_known_depth: str
    success_envelope: list[str]
    matches_best_known: bool
    within_success_envelope: bool
    alignment: str
    evidence_summary_path: str
    min_blocks_required: list[str] = field(default_factory=list)
    note: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_templates_dir() -> Path:
    return ROOT / "templates"


def default_output_path() -> Path:
    raw = os.getenv("E05_ADAPTIVE_BACKTEST_OUTPUT")
    if not raw:
        return DEFAULT_OUTPUT_PATH
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def cases() -> list[FamilyBacktestCase]:
    return [
        FamilyBacktestCase(
            family="code_reviewer",
            requirement="Review code, inspect failing tests, and produce a repair report.",
            expected_top_template="code_reviewer",
            best_known_depth="shallow",
            success_envelope=["shallow"],
            evidence_summary_path="docs/experiment-status/evidence/experiment_v0.2.41_e05_success_condition_2026_07_09_summary.md",
            note="Existing E05 evidence favors shallow over deep for the code-review family.",
        ),
        FamilyBacktestCase(
            family="customer_support_router",
            requirement="Classify support tickets and route each customer issue to the right response path.",
            expected_top_template="customer_support_router",
            best_known_depth="mixed",
            success_envelope=["none", "shallow", "deep"],
            evidence_summary_path="docs/experiment-status/evidence/experiment_v0.2.46_e05_customer_support_deep_only_teammate_governance_2026_07_10_summary.md",
            note=(
                "Customer-support evidence is mixed across governance slices, so adaptive should stay "
                "conservative instead of forcing deep by default."
            ),
        ),
        FamilyBacktestCase(
            family="data_analyzer",
            requirement="Analyze CSV statistics, extract anomalies, and produce a data report.",
            expected_top_template="data_analyzer",
            best_known_depth="deep",
            success_envelope=["deep"],
            evidence_summary_path="docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10_summary.md",
            note="The latest breadth/default slice showed deep publishing while shallow timed out.",
        ),
    ]


def evaluate_case(case: FamilyBacktestCase, store: TemplateStore) -> FamilyBacktestResult:
    scored = score_template_matches(case.requirement, store.list())
    if not scored:
        raise RuntimeError(f"no template suggestions found for {case.family}")
    score, meta = scored[0]
    effective_depth, policy_reason = resolve_effective_reuse_depth("adaptive", meta)
    if meta.name != case.expected_top_template:
        raise RuntimeError(
            f"expected top template {case.expected_top_template!r} for {case.family}, got {meta.name!r}"
        )
    matches_best_known = effective_depth == case.best_known_depth
    within_success_envelope = effective_depth in case.success_envelope
    alignment = "exact_match" if matches_best_known else (
        "within_success_envelope" if within_success_envelope else "mismatch"
    )
    return FamilyBacktestResult(
        family=case.family,
        requirement=case.requirement,
        top_template=meta.name,
        relevance_score=round(score, 3),
        recommended_depth=effective_depth,
        recommended_action=recommended_action_for_depth(effective_depth),
        policy_reason=policy_reason,
        best_known_depth=case.best_known_depth,
        success_envelope=case.success_envelope,
        matches_best_known=matches_best_known,
        within_success_envelope=within_success_envelope,
        alignment=alignment,
        evidence_summary_path=case.evidence_summary_path,
        min_blocks_required=meta.min_blocks_required,
        note=case.note,
    )


def render_summary_markdown(results: list[FamilyBacktestResult], json_path: Path) -> str:
    exact = sum(1 for item in results if item.alignment == "exact_match")
    bounded = sum(1 for item in results if item.alignment == "within_success_envelope")
    lines = [
        "# v0.2.48 E05 adaptive reuse policy backtest",
        "",
        "## Summary",
        "",
        f"- Raw evidence: `{rel(json_path)}`",
        "- Policy mode: `adaptive`",
        f"- Exact matches: `{exact}`",
        f"- Bounded matches: `{bounded}`",
        "",
        "## Families",
        "",
        "| Family | Top template | Policy | Action | Best known | Alignment | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.family,
                    item.top_template,
                    item.recommended_depth,
                    item.recommended_action,
                    item.best_known_depth,
                    item.alignment,
                    item.evidence_summary_path.replace("docs/", ""),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Notes", ""])
    for item in results:
        lines.append(f"- `{item.family}`: {item.note} Reason=`{item.policy_reason}`.")
    lines.append("")
    return "\n".join(lines)


def run_backtest(
    *,
    templates_dir: Path | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    store = TemplateStore()
    loaded = store.load_builtins(templates_dir or default_templates_dir())
    if loaded == 0:
        raise RuntimeError("no built-in templates loaded for adaptive reuse-depth backtest")
    results = [evaluate_case(case, store) for case in cases()]
    payload = {
        "experiment_id": "v0.2.48_e05_adaptive_reuse_policy_backtest",
        "status": "completed",
        "generated_at": utc_now(),
        "policy_mode": "adaptive",
        "template_count": loaded,
        "families": [asdict(item) for item in results],
        "summary": {
            "family_count": len(results),
            "exact_matches": sum(1 for item in results if item.alignment == "exact_match"),
            "bounded_matches": sum(1 for item in results if item.alignment == "within_success_envelope"),
            "mismatches": sum(1 for item in results if item.alignment == "mismatch"),
        },
    }
    target = output_path or default_output_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = target.with_name(f"{target.stem}_summary.md")
    summary_path.write_text(render_summary_markdown(results, target) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    payload = run_backtest()
    print(
        json.dumps(
            {
                "output": str(default_output_path()),
                "summary": payload["summary"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
