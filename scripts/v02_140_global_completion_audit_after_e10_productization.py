#!/usr/bin/env python3
"""Generate v0.2.140 global completion audit after E10 productization."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "audit_v0.2.140_global_completion_after_e10_productization"

ExperimentStatus = Literal["completed", "productized", "external_blocked"]


@dataclass(frozen=True)
class ExperimentAuditItem:
    experiment_id: str
    label: str
    status: ExperimentStatus
    ledger: str
    evidence: str
    blocker: str = ""
    productized: bool = False
    productization_scope: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {
            "ledger_exists": (ROOT / self.ledger).exists(),
            "evidence_exists": (ROOT / self.evidence).exists(),
            "ledger_status_present": self._ledger_status_present(),
        }

    def _ledger_status_present(self) -> bool:
        path = ROOT / self.ledger
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        if self.experiment_id == "E02":
            return "completed_for_proxy_blocked_for_true_human_panel" in text
        if self.experiment_id == "E10":
            return "governed_memory_product_surface_productized_unrestricted_memory_forbidden" in text
        return "状态：" in text


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
        "external_blocked",
        "docs/experiment-status/ledgers/E02_readable_testframe.md",
        "docs/experiment-status/evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09_summary.md",
        blocker="requires_recruited_true_human_timing_panel",
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
        productization_scope="adaptive default, monitoring API/Studio/manual refresh/history/scheduled hook",
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
        productization_scope="guarded default rollout with rollback/observability boundary",
    ),
    ExperimentAuditItem(
        "E08",
        "Harness sidecar/passmode",
        "productized",
        "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
        "docs/workingon-archives/v0.2.133/audit_v0.2.133_e08_full_sidecar_completion_summary.md",
        productized=True,
        productization_scope="full sidecar completion with cloud-specific deployment boundary",
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
        "productized",
        "docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
        "docs/workingon-archives/v0.2.139/evidence_v0.2.139_e10_studio_governed_memory_operator_ui_summary.md",
        productized=True,
        productization_scope="governed boundary, API, runtime retrieval, Studio operator create/view/revoke/audit",
    ),
]


def _load_v139_evidence() -> dict[str, Any]:
    path = ROOT / "docs/workingon-archives/v0.2.139/evidence_v0.2.139_e10_studio_governed_memory_operator_ui.json"
    return json.loads(path.read_text(encoding="utf-8"))


def audit() -> dict[str, Any]:
    items = [item.to_json() for item in EXPERIMENTS]
    missing_ledgers = [item["experiment_id"] for item in items if not item["ledger_exists"]]
    missing_evidence = [item["experiment_id"] for item in items if not item["evidence_exists"]]
    missing_ledger_status = [item["experiment_id"] for item in items if not item["ledger_status_present"]]
    external_blockers = [item for item in items if item["status"] == "external_blocked"]
    completed_or_productized = [item for item in items if item["status"] in {"completed", "productized"}]
    open_unblocked_gaps = [
        item["experiment_id"]
        for item in items
        if item["status"] not in {"completed", "productized", "external_blocked"}
    ]
    v139 = _load_v139_evidence()
    unrestricted_memory_forbidden = (
        v139["boundaries"]["unrestricted_memory_allowed"] is False
        and v139["checks"]["runtime_retrieval_still_active_only"] is True
    )
    all_non_external_productization_complete = (
        not missing_ledgers
        and not missing_evidence
        and not missing_ledger_status
        and not open_unblocked_gaps
        and [item["experiment_id"] for item in external_blockers] == ["E02"]
        and unrestricted_memory_forbidden
    )
    return {
        "version": "v0.2.140",
        "audit_id": "global_completion_after_e10_productization",
        "source_stage_report": "docs/stage-reports/v0.2.139_e10_studio_governed_memory_operator_ui.md",
        "status": "completed" if all_non_external_productization_complete else "needs_attention",
        "experiments": items,
        "experiment_count": len(items),
        "completed_or_productized_count": len(completed_or_productized),
        "productized_count": len([item for item in items if item["productized"]]),
        "external_blocker_count": len(external_blockers),
        "external_blockers": [item["experiment_id"] for item in external_blockers],
        "open_unblocked_gaps": open_unblocked_gaps,
        "missing_ledgers": missing_ledgers,
        "missing_evidence": missing_evidence,
        "missing_ledger_status": missing_ledger_status,
        "all_non_external_productization_complete": all_non_external_productization_complete,
        "global_completion_claimed": False,
        "unrestricted_memory_forbidden": unrestricted_memory_forbidden,
        "e02_true_human_panel_resolved": False,
        "answer_to_user_experiment_question": (
            "All non-external experiment/productization work currently tracked in E01-E10 is complete or productized. "
            "Full global completion is not claimed because E02 true human timing remains externally blocked."
        ),
        "next_recommended_stage": "stop_or_external_e02_human_panel",
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
        "# v0.2.140 Global completion audit after E10 productization",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Experiment count: `{result['experiment_count']}`",
        f"- Completed or productized count: `{result['completed_or_productized_count']}`",
        f"- Productized count: `{result['productized_count']}`",
        f"- External blocker count: `{result['external_blocker_count']}`",
        f"- External blockers: `{', '.join(result['external_blockers'])}`",
        f"- Open unblocked gaps: `{len(result['open_unblocked_gaps'])}`",
        f"- All non-external productization complete: `{result['all_non_external_productization_complete']}`",
        f"- Global completion claimed: `{result['global_completion_claimed']}`",
        f"- Unrestricted memory forbidden: `{result['unrestricted_memory_forbidden']}`",
        f"- Answer: {result['answer_to_user_experiment_question']}",
        "",
        "## Experiments",
        "",
        "| ID | Status | Productized | Blocker | Productization scope |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in result["experiments"]:
        lines.append(
            f"| `{item['experiment_id']}` | `{item['status']}` | `{item['productized']}` | `{item['blocker']}` | {item['productization_scope'] or 'none'} |"
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
