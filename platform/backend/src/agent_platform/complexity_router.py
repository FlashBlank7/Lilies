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

ROLLOUT_METRIC_DEFINITIONS = {
    "classification_distribution": "Count simple / medium / complex / unknown decisions over the rollout window.",
    "override_rate": "Share of classified requirements with an operator override.",
    "override_reason_coverage": "Share of force overrides with a non-empty operator-visible reason.",
    "fallback_unknown_rate": "Share of requirements classified as unknown and handled as complex-equivalent.",
    "success_rate_by_class": "Completion or acceptance rate grouped by effective requirement class.",
    "cost_latency_by_class": "Cost and latency distribution grouped by effective requirement class.",
}

COMPLEXITY_ROUTER_DEFAULT_MODES = {
    "disabled",
    "shadow_only",
    "operator_opt_in",
    "limited_default",
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
        rollout_metrics_prerequisites=True,
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


def classify_requirement(
    requirement: str,
    *,
    default_mode: str = "disabled",
    limited_default_enabled: bool = False,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    text = requirement.strip().casefold()
    if not text:
        return _classification_result(
            requirement_class="unknown",
            effective_class="complex",
            confidence=0.0,
            signals=["empty_requirement"],
            conservative_unknown=True,
            default_mode=default_mode,
            limited_default_enabled=limited_default_enabled,
            min_confidence=min_confidence,
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
            default_mode=default_mode,
            limited_default_enabled=limited_default_enabled,
            min_confidence=min_confidence,
        )
    if complex_hits:
        return _classification_result(
            requirement_class="complex",
            effective_class="complex",
            confidence=min(0.95, 0.65 + 0.1 * len(complex_hits)),
            signals=complex_hits,
            conservative_unknown=False,
            default_mode=default_mode,
            limited_default_enabled=limited_default_enabled,
            min_confidence=min_confidence,
        )
    if medium_hits:
        return _classification_result(
            requirement_class="medium",
            effective_class="medium",
            confidence=min(0.9, 0.6 + 0.08 * len(medium_hits)),
            signals=medium_hits,
            conservative_unknown=False,
            default_mode=default_mode,
            limited_default_enabled=limited_default_enabled,
            min_confidence=min_confidence,
        )
    return _classification_result(
        requirement_class="simple",
        effective_class="simple",
        confidence=0.55 if simple_hits else 0.45,
        signals=simple_hits or ["bounded_requirement"],
        conservative_unknown=False,
        default_mode=default_mode,
        limited_default_enabled=limited_default_enabled,
        min_confidence=min_confidence,
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
    default_mode: str = "disabled",
    limited_default_enabled: bool = False,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    normalized_mode = normalize_default_mode(default_mode)
    confidence = round(confidence, 3)
    eligible = (
        normalized_mode == "limited_default"
        and limited_default_enabled
        and not conservative_unknown
        and confidence >= min_confidence
    )
    return {
        "contract_id": "requirement_classification_contract",
        "policy_version": "v0.2.72_complexity_router_requirement_classification_contract",
        "requirement_class": requirement_class,
        "effective_class": effective_class,
        "confidence": confidence,
        "signals": signals,
        "conservative_unknown": conservative_unknown,
        "configured_default_mode": normalized_mode,
        "limited_default_enabled": limited_default_enabled,
        "limited_default_eligible": eligible,
        "default_router_enabled": eligible,
        "builder_policy": REQUIREMENT_CLASS_DEFINITIONS[requirement_class]["default_builder_policy"],
        "default_builder_policy": (
            REQUIREMENT_CLASS_DEFINITIONS[requirement_class]["default_builder_policy"]
            if eligible
            else None
        ),
    }


def normalize_default_mode(mode: str) -> str:
    normalized = mode.strip().casefold()
    return normalized if normalized in COMPLEXITY_ROUTER_DEFAULT_MODES else "disabled"


def limited_default_enablement_plan_status(
    *,
    default_mode: str = "disabled",
    limited_default_enabled: bool = False,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    normalized_mode = normalize_default_mode(default_mode)
    safety = complexity_router_default_safety_gate()
    active = (
        normalized_mode == "limited_default"
        and limited_default_enabled
        and safety["allowed_to_enable_default"]
    )
    return {
        "plan_id": "complexity_router_limited_default_enablement_contract",
        "policy_version": "v0.2.89_complexity_router_limited_default_enablement_contract",
        "configured_default_mode": normalized_mode,
        "limited_default_enabled": limited_default_enabled,
        "limited_default_active": active,
        "default_router_enabled": active,
        "default_enabled": active,
        "runtime_default": "limited_default" if active else "disabled",
        "rollback_value": "disabled",
        "min_confidence": min_confidence,
        "eligible_requirement_classes": ["simple", "medium", "complex"],
        "unknown_handling": "complex_equivalent_with_conservative_policy",
        "operator_visible_controls": [
            "disable_routing_for_task",
            "force_simple_with_reason",
            "force_medium_with_reason",
            "force_complex_with_reason",
            "rollback_to_disabled_default",
        ],
        "rollback_triggers": [
            "unexpected_classification_rate_above_0.05",
            "override_reason_coverage_below_0.95",
            "frontend_verification_failure",
            "any_accidental_default_enablement_outside_config",
        ],
        "default_safety": safety,
    }


def runtime_activation_for_build(
    requirement: str,
    *,
    default_mode: str = "disabled",
    limited_default_enabled: bool = False,
    min_confidence: float = 0.55,
    requested_planning_mode: str = "auto",
) -> dict[str, Any]:
    classification = classify_requirement(
        requirement,
        default_mode=default_mode,
        limited_default_enabled=limited_default_enabled,
        min_confidence=min_confidence,
    )
    policy = classification["default_builder_policy"] if classification["default_router_enabled"] else None
    effective_planning_mode = requested_planning_mode
    planning_mode_source = "request"
    if policy is not None and requested_planning_mode == "auto":
        effective_planning_mode = "required" if policy["plan_first"] else "disabled"
        planning_mode_source = "complexity_router"
    elif policy is None and requested_planning_mode == "auto":
        planning_mode_source = "request_default"
    elif policy is not None:
        planning_mode_source = "request_override"

    return {
        "activation_id": "complexity_router_runtime_activation",
        "policy_version": "v0.2.90_complexity_router_runtime_activation_path",
        "active": policy is not None,
        "rollback_value": "disabled",
        "requested_planning_mode": requested_planning_mode,
        "effective_planning_mode": effective_planning_mode,
        "planning_mode_source": planning_mode_source,
        "classification": classification,
        "runtime_builder_policy": policy,
    }


def runtime_activation_rollout_metrics(builds: list[dict[str, Any]]) -> dict[str, Any]:
    decision_categories = {
        "active": 0,
        "bypassed": 0,
        "disabled_default": 0,
        "conservative_unknown": 0,
        "request_override": 0,
    }
    classification_distribution: dict[str, int] = {}
    planning_mode_distribution: dict[str, int] = {}
    reuse_depth_distribution: dict[str, int] = {}
    build_outcome_distribution: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    for build in builds:
        state = build.get("team_state") or {}
        if hasattr(state, "model_dump"):
            state = state.model_dump(mode="json")
        activation = state.get("complexity_router") or {}
        classification = activation.get("classification") or {}
        runtime_policy = state.get("runtime_builder_policy") or {}
        active = bool(activation.get("active"))
        conservative_unknown = bool(classification.get("conservative_unknown"))
        configured_mode = str(classification.get("configured_default_mode") or "missing")
        planning_mode_source = str(activation.get("planning_mode_source") or "missing")
        effective_class = str(
            classification.get("effective_class")
            or classification.get("requirement_class")
            or "missing"
        )
        effective_planning_mode = str(
            activation.get("effective_planning_mode")
            or state.get("planning_mode")
            or "missing"
        )
        reuse_depth = str(runtime_policy.get("reuse_depth") or "none")
        outcome = str(build.get("status") or "missing")

        if active:
            decision_categories["active"] += 1
        else:
            decision_categories["bypassed"] += 1
        if configured_mode == "disabled":
            decision_categories["disabled_default"] += 1
        if conservative_unknown:
            decision_categories["conservative_unknown"] += 1
        if planning_mode_source == "request_override":
            decision_categories["request_override"] += 1

        classification_distribution[effective_class] = classification_distribution.get(effective_class, 0) + 1
        planning_mode_distribution[effective_planning_mode] = planning_mode_distribution.get(effective_planning_mode, 0) + 1
        reuse_depth_distribution[reuse_depth] = reuse_depth_distribution.get(reuse_depth, 0) + 1
        build_outcome_distribution[outcome] = build_outcome_distribution.get(outcome, 0) + 1
        records.append({
            "build_id": build.get("id"),
            "application_id": build.get("application_id"),
            "status": outcome,
            "active": active,
            "configured_default_mode": configured_mode,
            "requirement_class": classification.get("requirement_class"),
            "effective_class": effective_class,
            "conservative_unknown": conservative_unknown,
            "planning_mode_source": planning_mode_source,
            "effective_planning_mode": effective_planning_mode,
            "runtime_reuse_depth": reuse_depth,
        })

    return {
        "metric_id": "complexity_router_runtime_activation_rollout_metrics",
        "policy_version": "v0.2.91_complexity_router_runtime_activation_observability",
        "total_builds": len(builds),
        "rollback_value": "disabled",
        "decision_categories": decision_categories,
        "classification_distribution": classification_distribution,
        "effective_planning_mode_distribution": planning_mode_distribution,
        "runtime_reuse_depth_distribution": reuse_depth_distribution,
        "build_outcome_distribution": build_outcome_distribution,
        "records": records,
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


def rollout_metrics_prerequisites_status(sample_count: int = 0) -> dict[str, Any]:
    normalized_count = max(0, int(sample_count))
    return {
        "metrics_id": "rollout_metrics_prerequisites",
        "policy_version": "v0.2.74_complexity_router_rollout_metrics_prerequisites",
        "satisfied": True,
        "default_router_enabled": False,
        "status": "ready_empty_state" if normalized_count == 0 else "ready_with_samples",
        "sample_count": normalized_count,
        "required_metrics": [
            {"id": metric_id, "description": description}
            for metric_id, description in ROLLOUT_METRIC_DEFINITIONS.items()
        ],
        "empty_state": {
            "allowed": True,
            "reason": "metrics schema is present before rollout samples exist",
        },
        "evidence": [
            "platform/backend/src/agent_platform/complexity_router.py",
            "tests/test_complexity_router_default_safety.py",
        ],
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
            "evidence": [
                "platform/backend/src/agent_platform/complexity_router.py",
                "tests/test_complexity_router_default_safety.py",
            ],
            "required_for_default": True,
        },
    ]
    missing = [item["id"] for item in prerequisites if not item["satisfied"]]
    allowed = not missing
    return {
        "router_id": "e07_complexity_router",
        "gate_id": "default_safety_gate",
        "policy_version": "v0.2.74_complexity_router_rollout_metrics_prerequisites",
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
