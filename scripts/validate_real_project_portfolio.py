#!/usr/bin/env python3
"""Validate the v0.4.13 six-project portfolio and its separate capability lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = Path(
    "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
)
GAP_PATH = Path(
    "docs/experiments/lilies-collaboration/platform-capability-gaps.json"
)
REGISTRY_PATH = Path("docs/evolution-control/report_intents.json")
CONTRACT_PATH = Path(
    "docs/evolution-control/stage-contracts/v0.4.13-r3.json"
)

EXPECTED_PROJECT_IDS = [f"EXP-LILIES-{number:03d}" for number in range(1, 7)]
EXPECTED_CAPABILITY_FAMILIES = {
    "document_ocr_procurement_excel_writeback",
    "enterprise_rag_authorization_citation_update",
    "event_driven_monitoring_cross_system_automation",
    "ml_dl_inference_threshold_review_monitoring",
    "structured_data_artifact_customer_delivery",
    "forecasting_constrained_optimization_planning",
}
EXPECTED_PROTOCOL_SEQUENCE = [
    ("EXP-LILIES-003", "Home Assistant WebSocket"),
    ("EXP-LILIES-004", "ThingsBoard MQTT"),
    ("EXP-LILIES-005", "Actual SDK/CLI"),
]
REQUIRED_SELECTION_GATES = {
    "real_user_and_problem",
    "full_workflow_elements",
    "platform_workflow_outcome",
    "independent_business_oracle",
    "host_not_component",
    "intervention_boundary",
}
REQUIRED_PROJECT_FIELDS = {
    "project_id",
    "sequence",
    "title",
    "status",
    "cohort",
    "enterprise_denominator",
    "capability_family",
    "intervention_type",
    "customer_role",
    "business_problem",
    "host_projects",
    "inputs",
    "models",
    "external_systems",
    "human_decisions",
    "deliverables",
    "workflow_result",
    "independent_oracle",
    "source_intent_ids",
    "selection_gates",
    "task_package_root",
    "latest_revision",
    "revision_semantics",
    "next_action",
}
LEGAL_PROJECT_STATUSES = {
    "selected",
    "active",
    "active_blocked_by_environment",
    "needs_revision",
    "passed",
}
LEGAL_INTERVENTION_TYPES = {
    "augmentation",
    "replacement",
    "augmentation_and_separately_reported_replacement",
}
LEGAL_COHORTS = {
    "enterprise",
    "individual_and_small_facility",
    "individual_and_small_business",
}
REPLACEMENT_FIELDS = {
    "host_flow",
    "compatibility_contract",
    "original_flow_baseline",
    "rollback",
    "comparison",
}
CAPABILITY_STATUSES = {"proposed", "accepted", "implemented_verified", "rejected"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scalar_from_task_yaml(text: str, field: str) -> str | None:
    match = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip("'\"") if match else None


def validate_revision_chain(
    root: Path, project: dict[str, Any], errors: list[str]
) -> None:
    project_id = str(project["project_id"])
    package_root = root / str(project["task_package_root"])
    if not package_root.is_dir():
        errors.append(f"{project_id} task-package root does not exist")
        return
    numeric_dirs = sorted(
        int(path.name)
        for path in package_root.iterdir()
        if path.is_dir() and path.name.isdigit()
    )
    latest = project["latest_revision"]
    if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
        errors.append(f"{project_id} latest_revision must be a non-negative integer")
        return
    expected = list(range(1, latest + 1))
    if numeric_dirs != expected:
        errors.append(
            f"{project_id} revision directories are {numeric_dirs}, expected {expected}"
        )
        return
    for revision in numeric_dirs:
        task_path = package_root / str(revision) / "task.yaml"
        if not task_path.is_file():
            errors.append(f"{project_id} revision {revision} has no task.yaml")
            continue
        text = task_path.read_text(encoding="utf-8")
        task_id = scalar_from_task_yaml(text, "task_id")
        task_revision = scalar_from_task_yaml(text, "revision")
        parent_revision = scalar_from_task_yaml(text, "parent_revision")
        expected_parent = "null" if revision == 1 else str(revision - 1)
        if task_id != project_id:
            errors.append(
                f"{project_id} revision {revision} belongs to different project {task_id}"
            )
        if task_revision != str(revision):
            errors.append(
                f"{project_id} directory {revision} declares revision {task_revision}"
            )
        if parent_revision != expected_parent:
            errors.append(
                f"{project_id} revision {revision} parent is {parent_revision}, "
                f"expected {expected_parent}"
            )


def nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_replacement_contract(
    project_id: str, contract: object, errors: list[str]
) -> None:
    if not isinstance(contract, dict):
        errors.append(f"{project_id} replacement has no compatibility contract")
        return
    missing = sorted(REPLACEMENT_FIELDS - set(contract))
    if missing:
        errors.append(f"{project_id} replacement contract is missing {missing}")
    for field in REPLACEMENT_FIELDS:
        if field in contract and not nonempty_text(contract[field]):
            errors.append(f"{project_id} replacement contract has empty {field}")


def validate_gap_lane(
    root: Path, projects: dict[str, dict[str, Any]], errors: list[str]
) -> list[dict[str, Any]]:
    path = root / GAP_PATH
    if not path.is_file():
        errors.append("platform capability-gap registry is missing")
        return []
    gaps = load_json(path)
    if gaps.get("enterprise_denominator") is not False:
        errors.append("platform capability lane must remain outside enterprise denominator")
    admission = gaps.get("admission_contract")
    if not isinstance(admission, dict):
        errors.append("platform capability lane has no admission contract")
    else:
        required_origin = admission.get("required_origin")
        if required_origin != [
            "project_id",
            "task_revision",
            "attempt_id",
            "evidence_digest",
        ]:
            errors.append("capability admission contract has incomplete origin binding")
        required_completion = admission.get("required_completion_evidence")
        if required_completion != [
            "implementation_diff",
            "platform_tests",
            "independent_review",
            "affected_project_rerun",
        ]:
            errors.append("capability admission contract has incomplete completion evidence")
        if not nonempty_text(admission.get("required_generality")):
            errors.append("capability admission contract has no generality rule")
        if not nonempty_text(admission.get("authority")):
            errors.append("capability admission contract has no routing authority")
        if not nonempty_text(admission.get("denominator_rule")):
            errors.append("capability admission contract has no denominator rule")
    entries = gaps.get("entries")
    if not isinstance(entries, list):
        errors.append("platform capability entries must be a list")
        return []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"capability entry {index} is not an object")
            continue
        if entry.get("enterprise_denominator") is not False:
            errors.append(f"capability entry {index} enters enterprise denominator")
        for field in (
            "capability_id",
            "origin",
            "generality_evidence",
            "routing_evidence",
            "implementation_diff",
            "platform_tests",
            "independent_review",
            "affected_project_reruns",
            "status",
        ):
            if field not in entry:
                errors.append(f"capability entry {index} is missing {field}")
        if entry.get("status") not in CAPABILITY_STATUSES:
            errors.append(f"capability entry {index} has invalid status")
        origin = entry.get("origin")
        if not isinstance(origin, dict):
            errors.append(f"capability entry {index} has no bound origin")
        else:
            origin_project_id = origin.get("project_id")
            if origin_project_id not in projects:
                errors.append(f"capability entry {index} has unknown origin project")
            revision = origin.get("task_revision")
            latest = projects.get(str(origin_project_id), {}).get("latest_revision", 0)
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 1
                or revision > latest
            ):
                errors.append(f"capability entry {index} has invalid origin revision")
            if not nonempty_text(origin.get("attempt_id")):
                errors.append(f"capability entry {index} has no origin attempt")
            if not SHA256_RE.fullmatch(str(origin.get("evidence_digest", ""))):
                errors.append(f"capability entry {index} has invalid evidence digest")
        generality = entry.get("generality_evidence")
        if (
            not isinstance(generality, dict)
            or not nonempty_text(generality.get("reusable_contract"))
            or not nonempty_text(generality.get("non_source_contract_sample"))
        ):
            errors.append(f"capability entry {index} has incomplete generality evidence")
        for forbidden_field in (
            "contains_project_specific_adapter",
            "contains_field_mapping",
            "contains_webhook_handler",
            "contains_sdk_wrapper",
            "contains_final_workflow",
            "contains_oracle_material",
        ):
            if entry.get(forbidden_field) is not False:
                errors.append(
                    f"capability entry {index} does not reject {forbidden_field}"
                )
        if entry.get("status") in {"accepted", "implemented_verified"} and not nonempty_text(
            entry.get("routing_evidence")
        ):
            errors.append(f"capability entry {index} has no routing evidence")
        if entry.get("status") == "implemented_verified":
            for field in (
                "implementation_diff",
                "platform_tests",
                "independent_review",
            ):
                if not nonempty_text(entry.get(field)):
                    errors.append(
                        f"implemented capability entry {index} has no {field}"
                    )
            reruns = entry.get("affected_project_reruns")
            if not isinstance(reruns, list) or not reruns:
                errors.append(
                    f"implemented capability entry {index} has no affected-project rerun"
                )
            else:
                for rerun in reruns:
                    if not isinstance(rerun, dict):
                        errors.append(
                            f"implemented capability entry {index} has malformed rerun"
                        )
                        continue
                    project_id = rerun.get("project_id")
                    revision = rerun.get("task_revision")
                    if project_id not in projects:
                        errors.append(
                            f"implemented capability entry {index} reruns unknown project"
                        )
                    elif (
                        not isinstance(revision, int)
                        or revision < 1
                        or revision > projects[project_id]["latest_revision"]
                    ):
                        errors.append(
                            f"implemented capability entry {index} has invalid rerun revision"
                        )
                    if not SHA256_RE.fullmatch(
                        str(rerun.get("evidence_digest", ""))
                    ):
                        errors.append(
                            f"implemented capability entry {index} has invalid rerun digest"
                        )
                    if rerun.get("verdict") != "pass":
                        errors.append(
                            f"implemented capability entry {index} rerun did not pass"
                        )
    return entries


def validate_portfolio(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    portfolio_path = root / PORTFOLIO_PATH
    if not portfolio_path.is_file():
        return ["real-project portfolio registry is missing"]
    portfolio = load_json(portfolio_path)
    if portfolio.get("required_project_count") != 6:
        errors.append("portfolio must require exactly six real projects")
    if portfolio.get("stage_task_id") != "V04-13-T01H":
        errors.append("portfolio must remain under V04-13-T01H")
    if portfolio.get("contract_revision") != 3:
        errors.append("portfolio must bind stage contract revision 3")
    projects = portfolio.get("projects")
    if not isinstance(projects, list):
        return [*errors, "portfolio projects must be a list"]
    project_ids = [item.get("project_id") for item in projects if isinstance(item, dict)]
    if project_ids != EXPECTED_PROJECT_IDS:
        errors.append(
            "portfolio members must be EXP-LILIES-001 through EXP-LILIES-006 "
            "in execution order"
        )
    if len(set(project_ids)) != len(project_ids):
        errors.append("portfolio project IDs are not unique")
    if len(projects) != portfolio.get("required_project_count"):
        errors.append("portfolio project count differs from required_project_count")

    registry = load_json(root / REGISTRY_PATH)
    known_intents = {
        item.get("id")
        for item in registry.get("intents", [])
        if isinstance(item, dict)
    }
    authorized_intents = set(portfolio.get("authorized_intent_ids", []))
    unknown_authorized = sorted(authorized_intents - known_intents)
    if unknown_authorized:
        errors.append(f"portfolio authorizes unknown intents: {unknown_authorized}")

    contract = load_json(root / CONTRACT_PATH)
    t01h = next(
        (
            task
            for task in contract.get("mandatory_tasks", [])
            if task.get("task_id") == "V04-13-T01H"
        ),
        None,
    )
    if t01h is None:
        errors.append("contract revision 3 has no V04-13-T01H")
        contract_intents: set[str] = set()
    else:
        contract_intents = set(t01h.get("source_intent_ids", []))
        if authorized_intents != contract_intents:
            errors.append(
                "portfolio authorized intents differ from contract revision 3 T01H"
            )

    manifest_bytes: list[bytes] = []
    capability_families: set[str] = set()
    enterprise_projects: list[str] = []
    generalization_projects: list[str] = []
    project_manifests: dict[str, dict[str, Any]] = {}
    for expected_sequence, member in enumerate(projects, start=1):
        if not isinstance(member, dict):
            errors.append(f"portfolio member {expected_sequence} is not an object")
            continue
        project_id = str(member.get("project_id"))
        if member.get("sequence") != expected_sequence:
            errors.append(f"{project_id} has incorrect portfolio sequence")
        manifest_value = member.get("manifest")
        manifest_path = root / str(manifest_value)
        if not manifest_path.is_file():
            errors.append(f"{project_id} manifest does not exist: {manifest_value}")
            continue
        manifest_bytes.append(manifest_path.read_bytes())
        digest = sha256(manifest_path)
        if member.get("manifest_sha256") != digest:
            errors.append(f"{project_id} manifest digest does not match portfolio lock")
        project = load_json(manifest_path)
        missing_fields = sorted(REQUIRED_PROJECT_FIELDS - set(project))
        if missing_fields:
            errors.append(f"{project_id} manifest is missing fields: {missing_fields}")
            continue
        if project.get("project_id") != project_id:
            errors.append(f"{project_id} member points to a different project manifest")
        if project.get("sequence") != expected_sequence:
            errors.append(f"{project_id} manifest has incorrect sequence")
        if project.get("latest_revision") != member.get("latest_revision"):
            errors.append(f"{project_id} latest revision differs from portfolio")
        if project.get("status") != member.get("status"):
            errors.append(f"{project_id} status differs from portfolio")
        if project.get("status") not in LEGAL_PROJECT_STATUSES:
            errors.append(f"{project_id} has invalid project status")
        if project.get("intervention_type") not in LEGAL_INTERVENTION_TYPES:
            errors.append(f"{project_id} has invalid intervention type")
        cohort = project.get("cohort")
        if cohort not in LEGAL_COHORTS:
            errors.append(f"{project_id} has invalid cohort")
        if cohort == "enterprise" and project.get("enterprise_denominator") is not True:
            errors.append(f"{project_id} enterprise cohort is outside enterprise denominator")
        if cohort != "enterprise" and project.get("enterprise_denominator") is not False:
            errors.append(f"{project_id} generalization cohort enters enterprise denominator")
        for field in (
            "title",
            "customer_role",
            "business_problem",
            "workflow_result",
            "independent_oracle",
            "revision_semantics",
            "next_action",
        ):
            if not nonempty_text(project.get(field)):
                errors.append(f"{project_id} has empty {field}")
        for field in (
            "host_projects",
            "inputs",
            "models",
            "external_systems",
            "human_decisions",
            "deliverables",
            "source_intent_ids",
        ):
            value = project.get(field)
            if not isinstance(value, list) or not value:
                errors.append(f"{project_id} has empty or invalid {field}")
        intervention_type = project.get("intervention_type")
        if intervention_type == "replacement":
            validate_replacement_contract(
                project_id, project.get("replacement_contract"), errors
            )
        if intervention_type == "augmentation_and_separately_reported_replacement":
            if project.get("separate_intervention_denominators") is not True:
                errors.append(f"{project_id} does not separate intervention denominators")
            interventions = project.get("interventions")
            if not isinstance(interventions, list) or {
                item.get("kind") for item in interventions if isinstance(item, dict)
            } != {"augmentation", "replacement"}:
                errors.append(
                    f"{project_id} must define separate augmentation and replacement"
                )
            else:
                replacement = next(
                    item for item in interventions if item.get("kind") == "replacement"
                )
                validate_replacement_contract(project_id, replacement, errors)
                augmentation = next(
                    item for item in interventions if item.get("kind") == "augmentation"
                )
                if not nonempty_text(augmentation.get("boundary")) or not nonempty_text(
                    augmentation.get("oracle")
                ):
                    errors.append(f"{project_id} has incomplete augmentation boundary")
        gates = project.get("selection_gates")
        if not isinstance(gates, dict) or set(gates) != REQUIRED_SELECTION_GATES:
            errors.append(f"{project_id} does not declare the six selection gates")
        elif not all(gates.values()):
            errors.append(f"{project_id} fails a Product North Star selection gate")
        project_intents = set(project.get("source_intent_ids", []))
        unknown_project_intents = sorted(project_intents - known_intents)
        if unknown_project_intents:
            errors.append(
                f"{project_id} references unknown intents: {unknown_project_intents}"
            )
        if not project_intents <= authorized_intents:
            errors.append(f"{project_id} references intents outside portfolio authority")
        capability_families.add(str(project.get("capability_family")))
        if project.get("enterprise_denominator") is True:
            enterprise_projects.append(project_id)
        else:
            generalization_projects.append(project_id)
        validate_revision_chain(root, project, errors)
        project_manifests[project_id] = project

    manifest_set_digest = hashlib.sha256(b"".join(manifest_bytes)).hexdigest()
    if portfolio.get("member_manifest_set_sha256") != manifest_set_digest:
        errors.append("portfolio member manifest-set digest does not match")
    if capability_families != EXPECTED_CAPABILITY_FAMILIES:
        errors.append("portfolio does not cover the six distinct capability families")
    cohort_policy = portfolio.get("cohort_policy", {})
    if cohort_policy.get("enterprise_projects") != enterprise_projects:
        errors.append("enterprise cohort membership differs from project manifests")
    if (
        cohort_policy.get("separately_reported_generalization_projects")
        != generalization_projects
    ):
        errors.append("generalization cohort membership differs from project manifests")
    if cohort_policy.get("mixed_denominator_forbidden") is not True:
        errors.append("portfolio must forbid mixed enterprise/generalization denominator")

    protocol_sequence = portfolio.get("protocol_sequence")
    actual_protocol_sequence = (
        [
            (item.get("project_id"), item.get("surface"))
            for item in protocol_sequence
            if isinstance(item, dict)
        ]
        if isinstance(protocol_sequence, list)
        else []
    )
    if actual_protocol_sequence != EXPECTED_PROTOCOL_SEQUENCE:
        errors.append(
            "protocol sequence must remain Home Assistant WebSocket, "
            "ThingsBoard MQTT, then Actual SDK/CLI"
        )
    if isinstance(protocol_sequence, list) and [
        item.get("position") for item in protocol_sequence if isinstance(item, dict)
    ] != [1, 2, 3]:
        errors.append("protocol sequence positions must be 1, 2, and 3")
    execution = portfolio.get("execution_policy", {})
    if execution.get("mode") != "strictly_sequential":
        errors.append("projects must execute strictly one by one")
    if execution.get("shared_assignment_session_seed_or_archive") is not False:
        errors.append("projects may not share assignment, session, seed, or archive")
    if execution.get("all_attempts_remain_in_project_denominator") is not True:
        errors.append("project failure attempts must remain in the denominator")
    if execution.get("next_project_requires_previous_project_closure") is not True:
        errors.append("portfolio does not require previous-project closure")
    if execution.get("one_builder_context_per_project_revision") is not True:
        errors.append("portfolio does not require a project-local Builder context")
    if execution.get("portfolio_average_cannot_mask_project_failure") is not True:
        errors.append("portfolio average is allowed to mask project failure")
    if execution.get("provider_egress_default") != "disabled":
        errors.append("portfolio must keep provider egress disabled by default")
    active_project_id = execution.get("active_project_id")
    active_status_projects = [
        project_id
        for project_id, project in project_manifests.items()
        if str(project.get("status")).startswith("active")
    ]
    if active_status_projects != [active_project_id]:
        errors.append("active project ID and project statuses are inconsistent")
    for index, project_id in enumerate(EXPECTED_PROJECT_IDS):
        project = project_manifests.get(project_id)
        if project is None:
            continue
        started = project.get("latest_revision", 0) > 0 or project.get("status") != "selected"
        prior_ids = EXPECTED_PROJECT_IDS[:index]
        if started and any(
            project_manifests.get(prior_id, {}).get("status") != "passed"
            for prior_id in prior_ids
        ):
            errors.append(f"{project_id} started before every previous project passed")

    lane = portfolio.get("generic_capability_lane", {})
    if lane.get("registry") != GAP_PATH.as_posix():
        errors.append("portfolio does not reference the separate capability-gap registry")
    if lane.get("enterprise_denominator") is not False:
        errors.append("generic capability lane enters enterprise denominator")
    if lane.get("project_specific_adapter_forbidden") is not True:
        errors.append("generic capability lane does not forbid project-specific adapters")
    if lane.get("affected_project_rerun_required") is not True:
        errors.append("capability repair must require affected-project reruns")
    gap_entries = validate_gap_lane(root, project_manifests, errors)
    closure = portfolio.get("closure_policy", {})
    if closure.get("required_project_verdict") != "pass_for_every_project":
        errors.append("portfolio closure does not require every project to pass")
    if (
        closure.get("required_capability_gap_state")
        != "all_accepted_entries_terminal_and_affected_projects_rerun"
    ):
        errors.append("portfolio closure does not require capability completion and reruns")
    if closure.get("t01i_may_start_before_portfolio_closure") is not False:
        errors.append("T01I may start before portfolio closure")
    if closure.get("t01j_may_start_before_portfolio_closure") is not False:
        errors.append("T01J may start before portfolio closure")
    if closure.get("version_closure_may_average_projects") is not False:
        errors.append("version closure may average project results")
    if portfolio.get("execution_status") == "closed":
        if any(
            project.get("status") != "passed"
            for project in project_manifests.values()
        ):
            errors.append("closed portfolio contains a project that did not pass")
        if any(entry.get("status") == "accepted" for entry in gap_entries):
            errors.append("closed portfolio contains a nonterminal accepted capability")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors = validate_portfolio(args.root.resolve())
    if errors:
        print("real-project portfolio validation: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("real-project portfolio validation: PASS")
    print("- projects: 6")
    print("- active: EXP-LILIES-001 revision 19")
    print("- capability lane enterprise denominator: false")
    print("- provider egress default: disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
