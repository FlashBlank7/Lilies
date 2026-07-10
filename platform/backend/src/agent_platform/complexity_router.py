from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
E07_LEDGER = Path("docs/experiment-status/ledgers/E07_complexity_router.md")
V02_70_SELECTION = Path(
    "docs/workingon-archives/v0.2.70/selection_v0.2.70_complexity_router_guardrail_summary.md"
)


@dataclass(frozen=True)
class DefaultSafetyInputs:
    source_evidence_present: bool
    requirement_classification_contract: bool
    operator_override_plan: bool
    rollout_metrics_prerequisites: bool


def current_default_safety_inputs(root: Path | None = None) -> DefaultSafetyInputs:
    base = root or REPO_ROOT
    ledger = base / E07_LEDGER
    selection = base / V02_70_SELECTION
    ledger_text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    selection_text = selection.read_text(encoding="utf-8") if selection.exists() else ""
    source_ready = (
        "router_ready_for_default=false" in ledger_text
        and "default_safety_gate" in selection_text
        and "Router ready for default: `False`" in selection_text
    )
    return DefaultSafetyInputs(
        source_evidence_present=source_ready,
        requirement_classification_contract=False,
        operator_override_plan=False,
        rollout_metrics_prerequisites=False,
    )


def complexity_router_default_safety_gate(
    inputs: DefaultSafetyInputs | None = None,
) -> dict[str, Any]:
    resolved = inputs or current_default_safety_inputs()
    prerequisites = [
        {
            "id": "source_evidence",
            "label": "Source evidence",
            "satisfied": resolved.source_evidence_present,
            "evidence": [E07_LEDGER.as_posix(), V02_70_SELECTION.as_posix()],
            "required_for_default": True,
        },
        {
            "id": "requirement_classification_contract",
            "label": "Requirement classification contract",
            "satisfied": resolved.requirement_classification_contract,
            "evidence": [],
            "required_for_default": True,
        },
        {
            "id": "operator_override_plan",
            "label": "Operator override plan",
            "satisfied": resolved.operator_override_plan,
            "evidence": [],
            "required_for_default": True,
        },
        {
            "id": "rollout_metrics_prerequisites",
            "label": "Rollout metrics prerequisites",
            "satisfied": resolved.rollout_metrics_prerequisites,
            "evidence": [],
            "required_for_default": True,
        },
    ]
    missing = [item["id"] for item in prerequisites if not item["satisfied"]]
    allowed = not missing
    return {
        "router_id": "e07_complexity_router",
        "gate_id": "default_safety_gate",
        "policy_version": "v0.2.71_complexity_router_default_safety_gate",
        "default_enabled": False,
        "allowed_to_enable_default": allowed,
        "router_ready_for_default": allowed,
        "reason": (
            "all required prerequisites are satisfied"
            if allowed
            else "complexity-router default remains disabled until every default-safety prerequisite is satisfied"
        ),
        "missing_prerequisites": missing,
        "prerequisites": prerequisites,
        "supporting_guardrails": [
            "requirement_classification_contract",
            "operator_override_plan",
            "rollout_metrics_prerequisites",
        ],
    }
