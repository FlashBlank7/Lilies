from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .workflow_models import DeliveryMode


class DeliveryPolicy(BaseModel):
    mode: DeliveryMode
    title: str
    summary: str
    publication_behavior: Literal["advisory", "advisory_confirmation", "hard_gate"]
    missing_evidence_action: Literal["warn", "confirm", "block"]
    stale_evidence_action: Literal["warn", "confirm", "block"]
    recommended_evidence: list[str]
    visible_controls: list[str]
    warning_ack_required: bool
    hard_gate_enabled: bool


def resolve_delivery_policy(
    mode: DeliveryMode | str,
    *,
    governed_hard_gate: bool = False,
) -> DeliveryPolicy:
    resolved_mode = DeliveryMode(mode)
    if resolved_mode is DeliveryMode.quick:
        return DeliveryPolicy(
            mode=resolved_mode,
            title="Quick",
            summary="Prototype and share quickly with visible evidence warnings.",
            publication_behavior="advisory_confirmation",
            missing_evidence_action="confirm",
            stale_evidence_action="confirm",
            recommended_evidence=["draft_validation", "representative_run"],
            visible_controls=["run", "evidence_status", "publish_warning"],
            warning_ack_required=True,
            hard_gate_enabled=False,
        )
    if resolved_mode is DeliveryMode.guided:
        return DeliveryPolicy(
            mode=resolved_mode,
            title="Guided",
            summary="Guide the owner through acceptance evidence before sharing.",
            publication_behavior="advisory_confirmation",
            missing_evidence_action="confirm",
            stale_evidence_action="confirm",
            recommended_evidence=[
                "draft_validation",
                "mandatory_acceptance_cases",
                "representative_run",
            ],
            visible_controls=["run", "acceptance", "repair", "evidence_status", "publish_confirmation"],
            warning_ack_required=True,
            hard_gate_enabled=False,
        )

    # Keep the stored option readable for backward compatibility, but do not
    # let platform evidence diagnostics decide whether the owner/Builder may
    # publish. Business acceptance belongs to the agent/owner; the platform
    # still enforces structural, permission, and execution-safety boundaries.
    _ = governed_hard_gate
    return DeliveryPolicy(
        mode=resolved_mode,
        title="Governed",
        summary="Expose governance evidence and require an explicit publication decision.",
        publication_behavior="advisory_confirmation",
        missing_evidence_action="confirm",
        stale_evidence_action="confirm",
        recommended_evidence=[
            "draft_validation",
            "mandatory_acceptance_cases",
            "permission_boundary",
            "trace_and_tool_evidence",
            "human_approval_record",
        ],
        visible_controls=[
            "run",
            "acceptance",
            "repair",
            "evidence_status",
            "permission_boundary",
            "publication_decision",
        ],
        warning_ack_required=True,
        hard_gate_enabled=False,
    )
