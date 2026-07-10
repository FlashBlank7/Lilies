from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
E07_LEDGER = Path("docs/experiment-status/ledgers/E07_complexity_router.md")
V02_70_SELECTION = Path(
    "docs/workingon-archives/v0.2.70/selection_v0.2.70_complexity_router_guardrail_summary.md"
)

REQUIREMENT_CLASS_DEFINITIONS = {
    "simple": {
        "description": "Small single-surface work with bounded acceptance and no live/external dependency.",
        "default_builder_policy": {
            "plan_first": False,
            "reuse_depth": "shallow",
            "model_tier": "standard",
        },
    },
    "medium": {
        "description": "Multi-step workflow, API, or integration work with bounded tests and limited cross-boundary risk.",
        "default_builder_policy": {
            "plan_first": True,
            "reuse_depth": "adaptive",
            "model_tier": "standard",
        },
    },
    "complex": {
        "description": "Multi-module, architecture, live/external, model-sensitive, or broad platform-boundary work.",
        "default_builder_policy": {
            "plan_first": True,
            "reuse_depth": "adaptive",
            "model_tier": "strong",
        },
    },
    "unknown": {
        "description": "Insufficient requirement text; handled conservatively as complex-equivalent.",
        "default_builder_policy": {
            "plan_first": True,
            "reuse_depth": "adaptive",
            "model_tier": "strong",
        },
    },
}

SIMPLE_SIGNALS = {
    "copy",
    "fix",
    "format",
    "one-line",
    "rename",
    "single",
    "small",
    "text",
    "translate",
    "typo",
}
MEDIUM_SIGNALS = {
    "api",
    "dashboard",
    "database",
    "endpoint",
    "form",
    "integration",
    "report",
    "test",
    "ui",
    "workflow",
}
COMPLEX_SIGNALS = {
    "agent",
    "architecture",
    "cross-boundary",
    "default",
    "guardrail",
    "live",
    "model-sensitive",
    "multi-module",
    "paid",
    "platform",
    "rollout",
    "router",
}

OPERATOR_OVERRIDE_MODES = {
    "disabled": {
        "target_class": None,
        "requires_reason": False,
        "description": "Keep automatic complexity routing disabled.",
    },
    "force_simple": {
        "target_class": "simple",
        "requires_reason": True,
        "description": "Force simple handling for a requirement with an operator-visible reason.",
    },
    "force_medium": {
        "target_class": "medium",
        "requires_reason": True,
        "description": "Force medium handling for a requirement with an operator-visible reason.",
    },
    "force_complex": {
        "target_class": "complex",
        "requires_reason": True,
        "description": "Force complex handling for a requirement with an operator-visible reason.",
    },
}


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
        requirement_classification_contract=True,
        operator_override_plan=True,
        rollout_metrics_prerequisites=False,
    )


def requirement_classification_contract_status() -> dict[str, Any]:
    return {
        "contract_id": "requirement_classification_contract",
        "policy_version": "v0.2.72_complexity_router_requirement_classification_contract",
        "satisfied": True,
        "default_router_enabled": False,
        "classes": REQUIREMENT_CLASS_DEFINITIONS,
        "conservative_unknown_handling": {
            "requirement_class": "unknown",
            "effective_class": "complex",
            "reason": "insufficient requirement text is treated as complex-equivalent until clarified",
        },
        "evidence": [
            "platform/backend/src/agent_platform/complexity_router.py",
            "tests/test_complexity_router_default_safety.py",
        ],
    }


def classify_requirement(requirement: str) -> dict[str, Any]:
    text = requirement.strip().casefold()
    if not text:
        return _classification_result(
            requirement_class="unknown",
            effective_class="complex",
            confidence=0.0,
            signals=["empty_requirement"],
            conservative_unknown=True,
        )

    simple_hits = _signal_hits(text, SIMPLE_SIGNALS)
    medium_hits = _signal_hits(text, MEDIUM_SIGNALS)
    complex_hits = _signal_hits(text, COMPLEX_SIGNALS)
    token_count = len(text.split())
    if token_count > 80:
        complex_hits.append("long_requirement")
    if token_count < 4 and not (simple_hits or medium_hits or complex_hits):
        return _classification_result(
            requirement_class="unknown",
            effective_class="complex",
            confidence=0.2,
            signals=["underspecified_requirement"],
            conservative_unknown=True,
        )
    if complex_hits:
        return _classification_result(
            requirement_class="complex",
            effective_class="complex",
            confidence=min(0.95, 0.65 + 0.1 * len(complex_hits)),
            signals=complex_hits,
            conservative_unknown=False,
        )
    if medium_hits:
        return _classification_result(
            requirement_class="medium",
            effective_class="medium",
            confidence=min(0.9, 0.6 + 0.08 * len(medium_hits)),
            signals=medium_hits,
            conservative_unknown=False,
        )
    return _classification_result(
        requirement_class="simple",
        effective_class="simple",
        confidence=0.55 if simple_hits else 0.45,
        signals=simple_hits or ["bounded_requirement"],
        conservative_unknown=False,
    )


def _signal_hits(text: str, signals: set[str]) -> list[str]:
    return sorted(signal for signal in signals if signal in text)


def _classification_result(
    *,
    requirement_class: str,
    effective_class: str,
    confidence: float,
    signals: list[str],
    conservative_unknown: bool,
) -> dict[str, Any]:
    return {
        "contract_id": "requirement_classification_contract",
        "policy_version": "v0.2.72_complexity_router_requirement_classification_contract",
        "requirement_class": requirement_class,
        "effective_class": effective_class,
        "confidence": round(confidence, 3),
        "signals": signals,
        "conservative_unknown": conservative_unknown,
        "default_router_enabled": False,
        "builder_policy": REQUIREMENT_CLASS_DEFINITIONS[requirement_class]["default_builder_policy"],
    }


def operator_override_plan_status() -> dict[str, Any]:
    return {
        "plan_id": "operator_override_plan",
        "policy_version": "v0.2.73_complexity_router_operator_override_plan",
        "satisfied": True,
        "default_router_enabled": False,
        "allowed_modes": OPERATOR_OVERRIDE_MODES,
        "operator_visible_reason_required_for": [
            mode for mode, config in OPERATOR_OVERRIDE_MODES.items() if config["requires_reason"]
        ],
        "evidence": [
            "platform/backend/src/agent_platform/complexity_router.py",
            "tests/test_complexity_router_default_safety.py",
        ],
    }


def validate_operator_override(mode: str, reason: str = "") -> dict[str, Any]:
    normalized_mode = mode.strip().casefold()
    normalized_reason = reason.strip()
    config = OPERATOR_OVERRIDE_MODES.get(normalized_mode)
    if config is None:
        return {
            "plan_id": "operator_override_plan",
            "policy_version": "v0.2.73_complexity_router_operator_override_plan",
            "mode": normalized_mode,
            "valid": False,
            "target_class": None,
            "reason_required": False,
            "operator_visible_reason": normalized_reason,
            "error": "unsupported_override_mode",
            "default_router_enabled": False,
        }
    if config["requires_reason"] and not normalized_reason:
        return {
            "plan_id": "operator_override_plan",
            "policy_version": "v0.2.73_complexity_router_operator_override_plan",
            "mode": normalized_mode,
            "valid": False,
            "target_class": config["target_class"],
            "reason_required": True,
            "operator_visible_reason": normalized_reason,
            "error": "operator_visible_reason_required",
            "default_router_enabled": False,
        }
    return {
        "plan_id": "operator_override_plan",
        "policy_version": "v0.2.73_complexity_router_operator_override_plan",
        "mode": normalized_mode,
        "valid": True,
        "target_class": config["target_class"],
        "reason_required": config["requires_reason"],
        "operator_visible_reason": normalized_reason,
        "error": None,
        "default_router_enabled": False,
    }


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
            "evidence": [
                "platform/backend/src/agent_platform/complexity_router.py",
                "tests/test_complexity_router_default_safety.py",
            ],
            "required_for_default": True,
        },
        {
            "id": "operator_override_plan",
            "label": "Operator override plan",
            "satisfied": resolved.operator_override_plan,
            "evidence": [
                "platform/backend/src/agent_platform/complexity_router.py",
                "tests/test_complexity_router_default_safety.py",
            ],
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
        "policy_version": "v0.2.73_complexity_router_operator_override_plan",
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
