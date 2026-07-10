#!/usr/bin/env python3
"""Generate the v0.2 E01-E10 backlog closure disposition snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = (
    ROOT
    / "docs"
    / "experiment-status"
    / "evidence"
    / "experiment_v0.2.57_full_backlog_closure_2026_07_10.json"
)


@dataclass(frozen=True)
class BacklogItem:
    experiment_id: str
    title: str
    final_disposition: str
    closure_level: str
    evidence: list[str]
    conclusion: str
    remaining_boundary: str
    metrics: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "title": self.title,
            "final_disposition": self.final_disposition,
            "closure_level": self.closure_level,
            "evidence": self.evidence,
            "conclusion": self.conclusion,
            "remaining_boundary": self.remaining_boundary,
            "metrics": self.metrics,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backlog_items() -> list[BacklogItem]:
    return [
        BacklogItem(
            experiment_id="E01",
            title="Plan-first vs node-by-node",
            final_disposition="completed_with_conditional_policy",
            closure_level="existing_paid_live_plus_closure_rule",
            evidence=[
                "docs/experiment-status/ledgers/E01_plan_first_vs_node_by_node.md",
                "docs/experiment-status/evidence/experiment_v0.2.35_e01_required_architecture_coverage_after_json_recovery_2026_07_09_summary.md",
            ],
            conclusion=(
                "Plan-first should be conditional: avoid it for simple tasks, require it for complex tasks "
                "with architecture coverage needs."
            ),
            remaining_boundary="Needs product router implementation before becoming a global default.",
            metrics={"simple_default": "node_by_node_or_auto", "complex_default": "plan_first_required"},
        ),
        BacklogItem(
            experiment_id="E02",
            title="Readable TestFrame",
            final_disposition="completed_for_proxy_blocked_for_true_human_panel",
            closure_level="paid_reviewer_proxy_plus_external_blocker",
            evidence=[
                "docs/experiment-status/ledgers/E02_readable_testframe.md",
                "docs/experiment-status/evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09_summary.md",
            ],
            conclusion=(
                "Readable TestFrame is validated as the default reviewer surface; true human timing claims "
                "remain externally blocked."
            ),
            remaining_boundary="A real human panel requires recruited participants and timing protocol outside automation.",
            metrics={"proxy_raw_score": 0.375, "proxy_readable_score": 1.0},
        ),
        BacklogItem(
            experiment_id="E03",
            title="Visible architecture gate",
            final_disposition="completed",
            closure_level="deterministic_structural_fixture",
            evidence=["docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md"],
            conclusion="Explicit graph passes required visible architecture coverage while opaque agent shape fails required node coverage.",
            remaining_boundary="",
            metrics={
                "required_node_types": ["start", "llm", "parameter_extractor", "template_transform", "end"],
                "opaque_agent": {
                    "present": ["end", "start"],
                    "missing": ["llm", "parameter_extractor", "template_transform"],
                    "coverage": 0.4,
                },
                "explicit_graph": {
                    "present": ["end", "llm", "parameter_extractor", "start", "template_transform"],
                    "missing": [],
                    "coverage": 1.0,
                },
                "winner": "explicit_graph",
            },
        ),
        BacklogItem(
            experiment_id="E04",
            title="Local repair vs full rebuild",
            final_disposition="completed_with_strategy_boundary",
            closure_level="deterministic_multi_failure_fixture",
            evidence=[
                "docs/experiment-status/ledgers/E04_local_repair_vs_full_rebuild.md",
                "docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md",
            ],
            conclusion=(
                "Local repair is preferred only for isolated node failures; coupled failures need subgraph "
                "repair and misunderstood requirements need replan/full rebuild."
            ),
            remaining_boundary="",
            metrics={
                "cases": [
                    {
                        "failure_type": "single_node_config",
                        "best_strategy": "local_repair",
                        "reason": "failure is isolated to one node setting and post-test can verify the same graph",
                    },
                    {
                        "failure_type": "multi_node_coupling",
                        "best_strategy": "targeted_subgraph_repair",
                        "reason": "edges and adjacent nodes must change together; single-node local repair is unsafe",
                    },
                    {
                        "failure_type": "requirement_misunderstanding",
                        "best_strategy": "full_rebuild_or_replan",
                        "reason": "the graph encodes the wrong goal, so local edits risk preserving the wrong architecture",
                    },
                ],
                "strategy_count": 3,
            },
        ),
        BacklogItem(
            experiment_id="E05",
            title="Template reuse depth",
            final_disposition="completed_and_monitored",
            closure_level="paid_live_plus_monitoring_snapshot",
            evidence=[
                "docs/experiment-status/ledgers/E05_template_reuse.md",
                "docs/experiment-status/evidence/monitor_v0.2.56_e05_adaptive_policy_2026_07_10_summary.md",
            ],
            conclusion=(
                "Adaptive default and policy-default reliability are validated; monitoring snapshot has zero "
                "critical alerts and overrides remain visible."
            ),
            remaining_boundary="Future work is product monitoring surface, not experiment closure.",
            metrics={"critical_alerts": 0, "override_options_visible": True},
        ),
        BacklogItem(
            experiment_id="E06",
            title="Small-model translation",
            final_disposition="completed_as_deterministic_fixture",
            closure_level="slot_coverage_fixture",
            evidence=["docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md"],
            conclusion=(
                "Structured English intermediate representation improves required slot coverage over direct "
                "Chinese instruction in the fixture."
            ),
            remaining_boundary="A real small-model run remains optional if a low-cost model lane is introduced.",
            metrics={
                "required_slots": ["acceptance", "inputs", "outputs", "required_nodes", "test_frame"],
                "chinese_direct": {
                    "covered": ["acceptance", "inputs", "outputs"],
                    "missing": ["required_nodes", "test_frame"],
                    "coverage": 0.6,
                },
                "structured_english_ir": {
                    "covered": ["acceptance", "inputs", "outputs", "required_nodes", "test_frame"],
                    "missing": [],
                    "coverage": 1.0,
                },
                "winner": "structured_english_ir",
            },
        ),
        BacklogItem(
            experiment_id="E07",
            title="Complexity router",
            final_disposition="completed_as_policy_hypothesis",
            closure_level="deterministic_router_fixture",
            evidence=["docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md"],
            conclusion=(
                "Simple/medium/complex routing hypotheses are now explicit and evidence-derived, but not "
                "enabled as defaults."
            ),
            remaining_boundary="Product default routing requires guardrails and rollout design.",
            metrics={
                "cases": [
                    {
                        "complexity": "simple",
                        "policy": {"planning_mode": "auto", "max_turns": 12, "reuse_depth": "none_or_shallow"},
                        "evidence_basis": "E01 simple plan-first had no quality gain and higher cost",
                    },
                    {
                        "complexity": "medium",
                        "policy": {"planning_mode": "auto", "max_turns": 24, "reuse_depth": "adaptive"},
                        "evidence_basis": "E05 adaptive policy handles family-specific template depth",
                    },
                    {
                        "complexity": "complex",
                        "policy": {
                            "planning_mode": "required",
                            "max_turns": 42,
                            "reuse_depth": "adaptive",
                            "architecture_contract": True,
                        },
                        "evidence_basis": "E01 complex required and architecture coverage improved structural completeness",
                    },
                ],
                "router_ready_for_default": False,
                "reason": "fixture defines policy hypotheses but needs product guardrails before default routing",
            },
        ),
        BacklogItem(
            experiment_id="E08",
            title="Harness sidecar/passmode",
            final_disposition="completed_first_comparison",
            closure_level="deterministic_runtime_fixture",
            evidence=[
                "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
                "docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md",
            ],
            conclusion="Workflow-internal passmode can pause/pass by config; Platform Harness sidecar hard-blocks before external action.",
            remaining_boundary="Extended controls remain product follow-up.",
            metrics={"first_comparison_complete": True},
        ),
        BacklogItem(
            experiment_id="E09",
            title="Natural-language editing",
            final_disposition="completed_as_patch_scope_fixture",
            closure_level="deterministic_patch_scope_fixture",
            evidence=["docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md"],
            conclusion=(
                "Natural-language targeted patch is suitable for localized edits and should not replace full "
                "rebuild for wholesale goal changes."
            ),
            remaining_boundary="Live UI usability can be a product study, not an experiment blocker.",
            metrics={
                "tasks": [
                    {"edit": "rename node", "targeted_patch_ops": 1, "full_rebuild_ops": 5, "misedit_risk": "low"},
                    {"edit": "add required test node type", "targeted_patch_ops": 1, "full_rebuild_ops": 5, "misedit_risk": "low"},
                    {"edit": "change branch condition", "targeted_patch_ops": 2, "full_rebuild_ops": 6, "misedit_risk": "medium"},
                    {"edit": "replace workflow goal", "targeted_patch_ops": 6, "full_rebuild_ops": 6, "misedit_risk": "high"},
                ],
                "targeted_patch_wins": 3,
                "task_count": 4,
                "conclusion": "natural-language targeted patch is suitable for localized canvas edits, not wholesale goal replacement",
            },
        ),
        BacklogItem(
            experiment_id="E10",
            title="Assistant memory surface",
            final_disposition="blocked_until_governed_boundary",
            closure_level="deterministic_boundary_fixture",
            evidence=["docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md"],
            conclusion=(
                "Unrestricted assistant memory is not allowed; a governed memory surface requires permission, "
                "audit, revoke, retention, and source attribution."
            ),
            remaining_boundary="Implementation remains blocked until the governed boundary is accepted as product scope.",
            metrics={
                "required_controls": ["audit_log", "permission_scope", "retention_policy", "revoke", "source_attribution"],
                "unrestricted_memory": {
                    "coverage": 0.2,
                    "missing": ["audit_log", "permission_scope", "retention_policy", "revoke"],
                    "allowed": False,
                },
                "governed_memory_surface": {"coverage": 1.0, "missing": [], "allowed": True},
                "conclusion": "assistant memory surface is blocked until governed permission/audit/revoke boundaries exist",
            },
        ),
    ]


def build_snapshot() -> dict[str, Any]:
    items = backlog_items()
    blocked = [
        item for item in items
        if "blocked" in item.final_disposition or item.experiment_id == "E02"
    ]
    completed_or_validated = len(items) - len(blocked)
    return {
        "experiment": "v0.2 full experiment backlog closure",
        "version": "v0.2.57",
        "status": "completed",
        "generated_at": utc_now(),
        "items": [item.to_json() for item in items],
        "counts": {
            "total": len(items),
            "completed_or_validated": completed_or_validated,
            "external_or_scope_blocked": len(blocked),
        },
        "conclusion": (
            "All E01-E10 experiments now have a final disposition. E02 true human timing and "
            "E10 unrestricted memory are explicitly blocked by external/safety boundaries rather than "
            "left as vague open experiments."
        ),
    }


def write_summary(snapshot: dict[str, Any], path: Path) -> Path:
    summary_path = path.with_name(path.stem + "_summary.md")
    lines = [
        "# v0.2 full experiment backlog closure",
        "",
        "## Summary",
        "",
        f"- Raw evidence: `{path.relative_to(ROOT).as_posix()}`",
        f"- Status: `{snapshot['status']}`",
        f"- Total items: `{snapshot['counts']['total']}`",
        f"- Completed or validated: `{snapshot['counts']['completed_or_validated']}`",
        f"- External or scope blocked: `{snapshot['counts']['external_or_scope_blocked']}`",
        "",
        "## Matrix",
        "",
        "| ID | Disposition | Closure Level | Conclusion |",
        "| --- | --- | --- | --- |",
    ]
    for item in snapshot["items"]:
        lines.append(
            f"| {item['experiment_id']} | {item['final_disposition']} | {item['closure_level']} | {item['conclusion']} |"
        )
    lines.extend(["", "## Conclusion", "", snapshot["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot()
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = write_summary(snapshot, OUTPUT_PATH)
    print(OUTPUT_PATH)
    print(summary_path)
    print(snapshot["status"])


if __name__ == "__main__":
    main()
