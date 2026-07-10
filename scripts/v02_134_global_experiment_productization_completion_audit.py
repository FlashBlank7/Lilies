#!/usr/bin/env python3
"""Generate v0.2.134 global experiment/productization completion audit evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "audit_v0.2.134_global_experiment_productization_completion"

ExperimentStatus = Literal["completed", "productized", "blocked"]


@dataclass(frozen=True)
class ExperimentAuditItem:
    experiment_id: str
    label: str
    status: ExperimentStatus
    ledger: str
    evidence: str
    blocker: str = ""
    productized: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {
            "ledger_exists": (ROOT / self.ledger).exists(),
            "evidence_exists": (ROOT / self.evidence).exists(),
        }


EXPERIMENTS = [
    ExperimentAuditItem(
        "E01",
        "plan-first vs node-by-node",
        "completed",
        "docs/experiment-status/ledgers/E01_plan_first_vs_node_by_node.md",
        "docs/experiment-status/evidence/experiment_v0.2.35_e01_required_architecture_coverage_after_json_recovery_2026_07_09_summary.md",
    ),
    ExperimentAuditItem(
        "E02",
        "readable TestFrame",
        "blocked",
        "docs/experiment-status/ledgers/E02_readable_testframe.md",
        "docs/experiment-status/evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09_summary.md",
        blocker="blocked_external_panel",
    ),
    ExperimentAuditItem(
        "E03",
        "visible architecture gate",
        "completed",
        "docs/experiment-status/ledgers/E03_visible_architecture_gate.md",
        "docs/experiment-status/evidence/experiment_v0.2.35_e01_required_architecture_coverage_after_json_recovery_2026_07_09_summary.md",
    ),
    ExperimentAuditItem(
        "E04",
        "local repair vs full rebuild",
        "completed",
        "docs/experiment-status/ledgers/E04_local_repair_vs_full_rebuild.md",
        "docs/experiment-status/evidence/experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09_summary.md",
    ),
    ExperimentAuditItem(
        "E05",
        "Template reuse depth",
        "productized",
        "docs/experiment-status/ledgers/E05_template_reuse.md",
        "docs/workingon-archives/v0.2.103/verification_v0.2.103_e05_scheduled_monitoring_hook_summary.md",
        productized=True,
    ),
    ExperimentAuditItem(
        "E06",
        "small-model translation",
        "completed",
        "docs/experiment-status/ledgers/E06_small_model_translation.md",
        "docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md",
    ),
    ExperimentAuditItem(
        "E07",
        "complexity router",
        "productized",
        "docs/experiment-status/ledgers/E07_complexity_router.md",
        "docs/workingon-archives/v0.2.93/rollout_v0.2.93_complexity_router_guarded_default_summary.md",
        productized=True,
    ),
    ExperimentAuditItem(
        "E08",
        "Harness sidecar/passmode",
        "productized",
        "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
        "docs/workingon-archives/v0.2.133/audit_v0.2.133_e08_full_sidecar_completion_summary.md",
        productized=True,
    ),
    ExperimentAuditItem(
        "E09",
        "natural-language editing",
        "completed",
        "docs/experiment-status/ledgers/E09_natural_language_editing.md",
        "docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md",
    ),
    ExperimentAuditItem(
        "E10",
        "assistant memory surface",
        "blocked",
        "docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
        "docs/experiment-status/evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md",
        blocker="blocked_governance_boundary",
    ),
]


def audit() -> dict[str, Any]:
    items = [item.to_json() for item in EXPERIMENTS]
    missing_ledgers = [item["experiment_id"] for item in items if not item["ledger_exists"]]
    missing_evidence = [item["experiment_id"] for item in items if not item["evidence_exists"]]
    blocked = [item for item in items if item["status"] == "blocked"]
    completed_or_productized = [item for item in items if item["status"] in {"completed", "productized"}]
    open_unblocked_gaps = [
        item["experiment_id"]
        for item in items
        if item["status"] not in {"completed", "productized", "blocked"}
    ]
    return {
        "version": "v0.2.134",
        "audit_id": "global_experiment_productization_completion",
        "source_stage_report": "docs/stage-reports/v0.2.133_e08_full_sidecar_completion_audit.md",
        "status": "completed" if not missing_ledgers and not missing_evidence and not open_unblocked_gaps else "needs_attention",
        "experiments": items,
        "experiment_count": len(items),
        "completed_or_productized_count": len(completed_or_productized),
        "productized_count": len([item for item in items if item["productized"]]),
        "blocked_count": len(blocked),
        "blocked_experiments": [item["experiment_id"] for item in blocked],
        "open_unblocked_gaps": open_unblocked_gaps,
        "missing_ledgers": missing_ledgers,
        "missing_evidence": missing_evidence,
        "global_completion_claimed": False,
        "reason": (
            "E01-E10 all have current dispositions and evidence. E05, E07, and E08 are productized. "
            "No open unblocked gaps remain, but global completion is not claimed because E02 and E10 remain blocked "
            "by external panel and governance prerequisites."
        ),
        "next_recommended_stage": "v0.2.135_blocked_experiment_resolution_selection",
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.134 Global experiment/productization completion audit",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Experiment count: `{result['experiment_count']}`",
        f"- Completed or productized count: `{result['completed_or_productized_count']}`",
        f"- Productized count: `{result['productized_count']}`",
        f"- Blocked count: `{result['blocked_count']}`",
        f"- Blocked experiments: `{', '.join(result['blocked_experiments'])}`",
        f"- Open unblocked gaps: `{len(result['open_unblocked_gaps'])}`",
        f"- Global completion claimed: `{result['global_completion_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Experiments",
        "",
        "| ID | Status | Productized | Blocker | Ledger |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in result["experiments"]:
        lines.append(
            f"| `{item['experiment_id']}` | `{item['status']}` | `{item['productized']}` | `{item['blocker']}` | `{item['ledger']}` |"
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = audit()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
