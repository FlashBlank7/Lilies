#!/usr/bin/env python3
"""Validate the v0.4.13 six-project portfolio and its separate capability lane."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_BACKEND_SOURCE = ROOT / "platform/backend/src"
if str(PLATFORM_BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(PLATFORM_BACKEND_SOURCE))

from agent_platform.capability_generality_gate import (  # noqa: E402
    CapabilityGeneralityConfigurationError,
    CapabilityGeneralityGate,
)


PORTFOLIO_PATH = Path(
    "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
)
GAP_PATH = Path(
    "docs/experiments/lilies-collaboration/platform-capability-gaps.json"
)
REGISTRY_PATH = Path("docs/evolution-control/report_intents.json")
CONTRACT_PATH = Path(
    "docs/evolution-control/stage-contracts/v0.4.13-r8.json"
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
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
FINAL_EVIDENCE_ROOT = Path("docs/evidence/v0.4.13/t01h/runs/attempts")
RSA_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)
FINAL_ATTEMPT_ENVELOPE_FIELDS = {
    "receipt_id",
    "issuer",
    "key_id",
    "issued_at",
    "semantic_type",
    "semantic_payload",
    "payload_digest",
    "signature",
}
FINAL_ATTEMPT_PAYLOAD_FIELDS = {
    "project_id",
    "attempt_id",
    "contract_revision",
    "formal_builder_actor",
    "builder_actor",
    "status",
    "eligible_for_final",
    "published_version",
    "workflow_content_hash",
    "prerequisite_receipt_digest",
    "forbidden_assistance_scan_digest",
    "signed_report_digest",
    "evidence_path",
    "evidence_sha256",
    "debug_passed",
    "protected_seed_pass_count",
    "phase_percentage_sum",
}


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def validate_final_receipt_trust_root(
    portfolio: dict[str, Any],
    records: list[Any],
    errors: list[str],
) -> dict[str, Any] | None:
    trust_root = portfolio.get("r8_final_receipt_trust_root")
    if trust_root is None:
        if (
            records
            or portfolio.get("execution_status") == "closed"
            or portfolio.get("r8_final_receipt_trust_root_digest") is not None
        ):
            errors.append("r8 final attempt records have no task-author trust root")
        return None
    if not isinstance(trust_root, dict) or set(trust_root) != {
        "issuer",
        "key_id",
        "rsa_modulus",
        "rsa_exponent",
    }:
        errors.append("r8 final attempt trust root schema is invalid")
        return None
    issuer = trust_root.get("issuer")
    key_id = trust_root.get("key_id")
    modulus = trust_root.get("rsa_modulus")
    exponent = trust_root.get("rsa_exponent")
    if (
        not isinstance(issuer, str)
        or SAFE_IDENTIFIER_RE.fullmatch(issuer) is None
        or not isinstance(key_id, str)
        or SAFE_IDENTIFIER_RE.fullmatch(key_id) is None
        or not isinstance(modulus, int)
        or isinstance(modulus, bool)
        or modulus.bit_length() < 2_048
        or not isinstance(exponent, int)
        or isinstance(exponent, bool)
        or exponent < 3
        or exponent % 2 == 0
    ):
        errors.append("r8 final attempt trust root is invalid")
        return None
    trust_root_material = (
        f"rsa-sha256:{issuer}:{key_id}:{modulus}:{exponent}"
    ).encode("ascii")
    expected_digest = f"sha256:{hashlib.sha256(trust_root_material).hexdigest()}"
    if portfolio.get("r8_final_receipt_trust_root_digest") != expected_digest:
        errors.append("r8 final attempt trust root digest is not pinned")
        return None
    return trust_root


def verify_final_attempt_signature(
    envelope: dict[str, Any],
    trust_root: dict[str, Any],
) -> bool:
    if (
        envelope.get("issuer") != trust_root["issuer"]
        or envelope.get("key_id") != trust_root["key_id"]
        or envelope.get("semantic_type") != "r8_final_attempt"
    ):
        return False
    signature_value = envelope.get("signature")
    if (
        not isinstance(signature_value, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+", signature_value) is None
    ):
        return False
    try:
        signature = base64.urlsafe_b64decode(signature_value + "==")
    except (TypeError, ValueError):
        return False
    modulus = trust_root["rsa_modulus"]
    modulus_bytes = (modulus.bit_length() + 7) // 8
    if len(signature) != modulus_bytes:
        return False
    signature_integer = int.from_bytes(signature, "big")
    if signature_integer >= modulus:
        return False
    encoded = pow(
        signature_integer,
        trust_root["rsa_exponent"],
        modulus,
    ).to_bytes(modulus_bytes, "big")
    digest_info = RSA_SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(
        canonical_json_bytes(
            {
                field: envelope[field]
                for field in (
                    "receipt_id",
                    "issuer",
                    "key_id",
                    "issued_at",
                    "semantic_type",
                    "semantic_payload",
                    "payload_digest",
                )
            }
        )
    ).digest()
    padding_size = modulus_bytes - len(digest_info) - 3
    if padding_size < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_size + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def validate_final_report_evidence(
    root: Path,
    payload: dict[str, Any],
    index: int,
    errors: list[str],
) -> bool:
    relative_value = payload.get("evidence_path")
    relative_path = Path(str(relative_value))
    expected_root = (root / FINAL_EVIDENCE_ROOT).resolve()
    if (
        not isinstance(relative_value, str)
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.suffix != ".json"
        or not relative_path.is_relative_to(FINAL_EVIDENCE_ROOT)
        or any(
            fragment in part.lower()
            for part in relative_path.parts
            for fragment in ("protected", "hidden", "oracle", "seed")
        )
    ):
        errors.append(f"r8 final attempt record {index} has an unsafe evidence path")
        return False
    evidence_path = root / relative_path
    if evidence_path.is_symlink() or not evidence_path.is_file():
        errors.append(f"r8 final attempt record {index} evidence file is missing")
        return False
    if evidence_path.stat().st_size > 16 * 1024 * 1024:
        errors.append(f"r8 final attempt record {index} evidence file is too large")
        return False
    if not evidence_path.resolve().is_relative_to(expected_root):
        errors.append(f"r8 final attempt record {index} evidence path escapes its root")
        return False
    if sha256(evidence_path) != payload.get("evidence_sha256"):
        errors.append(f"r8 final attempt record {index} evidence file digest is invalid")
        return False
    try:
        evidence = load_json(evidence_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        errors.append(f"r8 final attempt record {index} evidence file is invalid")
        return False
    try:
        evidence_digest = canonical_digest(evidence)
    except (TypeError, ValueError):
        evidence_digest = None
    if evidence_digest != payload.get("signed_report_digest"):
        errors.append(f"r8 final attempt record {index} report digest is not bound to evidence")
        return False
    if (
        evidence.get("schema_version")
        != "v0.4.13-portfolio-rerun-report-body-r8-1"
        or evidence.get("project_id") != payload.get("project_id")
        or evidence.get("attempt_id") != payload.get("attempt_id")
        or evidence.get("formal_builder_actor") != "codex"
        or evidence.get("builder_actor") != "codex_fallback"
        or evidence.get("status") != "completed"
        or evidence.get("timing_complete") is not True
        or evidence.get("max_session_tokens") is not None
        or evidence.get("final_token_checkpoint") is not None
    ):
        errors.append(f"r8 final attempt record {index} evidence identity is invalid")
        return False
    phases = evidence.get("phases")
    expected_phases = [
            "environment_bootstrap",
            "daemon_discovery",
            "explicit_pairing",
            "assignment_provision",
            "builder_execution",
            "host_result_verification",
            "platform_archive_verification",
            "cleanup_reporting",
    ]
    try:
        phase_percentages = [
            float(item["duration_percentage"])
            for item in phases
            if isinstance(item, dict)
        ]
    except (KeyError, TypeError, ValueError):
        phase_percentages = []
    try:
        expected_phase_sum = float(payload["phase_percentage_sum"])
    except (KeyError, TypeError, ValueError):
        expected_phase_sum = math.nan
    if (
        not isinstance(phases, list)
        or len(phases) != 8
        or not all(isinstance(item, dict) for item in phases)
        or [item.get("phase") for item in phases] != expected_phases
        or len(phase_percentages) != 8
        or not all(math.isfinite(value) and value >= 0 for value in phase_percentages)
        or not math.isclose(
            math.fsum(phase_percentages),
            expected_phase_sum,
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        or any(
            phases[position].get("duration_seconds") != 0.0
            or phases[position].get("duration_percentage") != 0.0
            or phases[position].get("outcome") != "not_applicable"
            for position in (1, 2)
        )
    ):
        errors.append(f"r8 final attempt record {index} evidence timing is invalid")
        return False
    execution = evidence.get("execution_evidence")
    receipts = (
        execution.get("acceptance_receipts") if isinstance(execution, dict) else None
    )
    if (
        not isinstance(execution, dict)
        or execution.get("published_version") != payload.get("published_version")
        or execution.get("published_content_hash")
        != payload.get("workflow_content_hash")
        or not isinstance(execution.get("fallback_eligibility"), dict)
        or execution["fallback_eligibility"].get("prerequisite_payload_digest")
        != payload.get("prerequisite_receipt_digest")
        or execution["fallback_eligibility"].get("forbidden_assistance_scan_digest")
        != payload.get("forbidden_assistance_scan_digest")
        or not isinstance(receipts, list)
        or len(receipts) != 4
        or not all(isinstance(item, dict) for item in receipts)
        or receipts[0].get("case_id") != "debug"
        or len({item.get("case_id") for item in receipts}) != 4
        or any(item.get("status") != "passed" for item in receipts)
        or any(
            item.get("published_version") != payload.get("published_version")
            or item.get("published_content_hash")
            != payload.get("workflow_content_hash")
            for item in receipts
        )
    ):
        errors.append(f"r8 final attempt record {index} acceptance evidence is invalid")
        return False
    usage = evidence.get("final_codex_token_usage")
    if not isinstance(usage, dict) or usage.get("availability") not in {
        "exact",
        "unknown",
        "unavailable",
    }:
        errors.append(f"r8 final attempt record {index} Codex usage evidence is invalid")
        return False
    counters = tuple(
        usage.get(name)
        for name in ("attempted_calls", "input_tokens", "output_tokens", "total_tokens")
    )
    if usage["availability"] == "exact":
        if (
            usage.get("reason") is not None
            or
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in counters
            )
            or usage.get("total_tokens")
            != usage.get("input_tokens") + usage.get("output_tokens")
        ):
            errors.append(f"r8 final attempt record {index} exact Codex usage is invalid")
            return False
    elif (
        any(value is not None for value in counters)
        or not isinstance(usage.get("reason"), str)
        or not usage["reason"].strip()
    ):
        errors.append(f"r8 final attempt record {index} unavailable Codex usage is invalid")
        return False
    return True


def validate_r8_final_attempt_records(
    root: Path,
    portfolio: dict[str, Any],
    errors: list[str],
) -> set[str]:
    records = portfolio.get("r8_final_attempt_records")
    if not isinstance(records, list):
        errors.append("r8 final attempt records must be a list")
        return set()

    trust_root = validate_final_receipt_trust_root(portfolio, records, errors)
    project_ids: list[str] = []
    attempt_ids: list[str] = []
    digest_fields = {
        "workflow_content_hash",
        "prerequisite_receipt_digest",
        "forbidden_assistance_scan_digest",
        "signed_report_digest",
    }
    for index, record in enumerate(records, start=1):
        record_valid = True
        if not isinstance(record, dict) or set(record) != FINAL_ATTEMPT_ENVELOPE_FIELDS:
            errors.append(f"r8 final attempt record {index} is not an object")
            continue
        payload = record.get("semantic_payload")
        if not isinstance(payload, dict) or set(payload) != FINAL_ATTEMPT_PAYLOAD_FIELDS:
            errors.append(f"r8 final attempt record {index} payload schema is invalid")
            continue
        if not is_canonical_uuid(record.get("receipt_id")):
            errors.append(f"r8 final attempt record {index} has an invalid receipt id")
            record_valid = False
        if not is_utc_timestamp(record.get("issued_at")):
            errors.append(f"r8 final attempt record {index} has an invalid issue time")
            record_valid = False
        try:
            expected_payload_digest = canonical_digest(payload)
        except (TypeError, ValueError):
            expected_payload_digest = None
        if record.get("payload_digest") != expected_payload_digest:
            errors.append(f"r8 final attempt record {index} payload digest is invalid")
            record_valid = False
        try:
            signature_valid = (
                trust_root is not None
                and verify_final_attempt_signature(record, trust_root)
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            signature_valid = False
        if not signature_valid:
            errors.append(f"r8 final attempt record {index} signature is invalid")
            record_valid = False
        project_id = payload.get("project_id")
        attempt_id = payload.get("attempt_id")
        if project_id not in EXPECTED_PROJECT_IDS:
            errors.append(f"r8 final attempt record {index} has an unknown project")
            record_valid = False
        if not is_canonical_uuid(attempt_id):
            errors.append(f"r8 final attempt record {index} has an invalid attempt id")
            record_valid = False
        if payload.get("contract_revision") != 8:
            errors.append(f"r8 final attempt record {index} is not bound to contract r8")
            record_valid = False
        if (
            payload.get("formal_builder_actor") != "codex"
            or payload.get("builder_actor") != "codex_fallback"
        ):
            errors.append(f"r8 final attempt record {index} has an invalid Builder actor")
            record_valid = False
        if payload.get("status") != "passed" or payload.get("eligible_for_final") is not True:
            errors.append(f"r8 final attempt record {index} is not a final eligible pass")
            record_valid = False
        published_version = payload.get("published_version")
        if (
            not isinstance(published_version, int)
            or isinstance(published_version, bool)
            or published_version < 1
        ):
            errors.append(f"r8 final attempt record {index} has an invalid published version")
            record_valid = False
        for field in digest_fields:
            if SHA256_RE.fullmatch(str(payload.get(field, ""))) is None:
                errors.append(f"r8 final attempt record {index} has an invalid {field}")
                record_valid = False
        if SHA256_HEX_RE.fullmatch(str(payload.get("evidence_sha256", ""))) is None:
            errors.append(f"r8 final attempt record {index} has an invalid evidence digest")
            record_valid = False
        if payload.get("debug_passed") is not True:
            errors.append(f"r8 final attempt record {index} did not pass public debug")
            record_valid = False
        if payload.get("protected_seed_pass_count") != 3:
            errors.append(f"r8 final attempt record {index} did not pass three protected seeds")
            record_valid = False
        phase_sum = payload.get("phase_percentage_sum")
        if (
            not isinstance(phase_sum, (int, float))
            or isinstance(phase_sum, bool)
            or not math.isfinite(float(phase_sum))
            or abs(float(phase_sum) - 100.0) > 1e-6
        ):
            errors.append(f"r8 final attempt record {index} phase percentages do not sum to 100")
            record_valid = False
        if not validate_final_report_evidence(root, payload, index, errors):
            record_valid = False
        if record_valid:
            assert isinstance(project_id, str)
            assert isinstance(attempt_id, str)
            project_ids.append(project_id)
            attempt_ids.append(attempt_id)

    if len(set(project_ids)) != len(project_ids):
        errors.append("r8 final attempt records contain duplicate projects")
    if len(set(attempt_ids)) != len(attempt_ids):
        errors.append("r8 final attempt records reuse an attempt id")
    return set(project_ids)


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
    if portfolio.get("contract_revision") != 8:
        errors.append("portfolio must bind stage contract revision 8")
    if portfolio.get("selection_contract_revision") != 3:
        errors.append("portfolio must preserve its original selection contract revision")
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
        errors.append("contract revision 8 has no V04-13-T01H")
        contract_intents: set[str] = set()
    else:
        contract_intents = set(t01h.get("source_intent_ids", []))
        if authorized_intents != contract_intents:
            errors.append(
                "portfolio authorized intents differ from contract revision 8 T01H"
            )

    actor_policy = portfolio.get("r8_builder_actor_policy", {})
    lilies_actor = actor_policy.get("lilies", {})
    codex_actor = actor_policy.get("codex_fallback", {})
    if actor_policy.get("historical_attempt_relabeling_forbidden") is not True:
        errors.append("r8 actor policy allows historical attempt relabeling")
    if (
        lilies_actor.get("formal_builder_actor") != "lilies"
        or lilies_actor.get("builder_actor") != "lilies"
        or lilies_actor.get("daemon_access_required") is not True
        or lilies_actor.get("daemon_discovery_phase") != "required"
        or lilies_actor.get("explicit_pairing_phase") != "required"
    ):
        errors.append("r8 Lilies actor profile is invalid")
    if (
        codex_actor.get("formal_builder_actor") != "codex"
        or codex_actor.get("builder_actor") != "codex_fallback"
        or codex_actor.get("daemon_access_required") is not False
        or codex_actor.get("daemon_discovery_phase")
        != "zero_duration_not_applicable"
        or codex_actor.get("explicit_pairing_phase")
        != "zero_duration_not_applicable"
        or codex_actor.get("requires_bounded_failed_lilies_attempt") is not True
        or codex_actor.get(
            "requires_fresh_empty_application_environment_assignment_session"
        )
        is not True
        or codex_actor.get("requires_fresh_isolated_public_only_context") is not True
    ):
        errors.append("r8 Codex fallback actor profile is invalid")
    status_semantics = portfolio.get("project_status_semantics", {})
    if (
        status_semantics.get("projects_status_field")
        != "historical_pre_r8_manifest_status"
        or status_semantics.get("r8_final_status_source")
        != "fresh signed per-attempt evidence only"
        or status_semantics.get("pre_r8_pass_cannot_satisfy_r8_final") is not True
    ):
        errors.append("r8 project status semantics are invalid")
    r8_final_project_ids = validate_r8_final_attempt_records(root, portfolio, errors)

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
    raw_sequence_overrides = execution.get("explicit_user_sequence_overrides", [])
    sequence_overrides: dict[str, set[str]] = {}
    if not isinstance(raw_sequence_overrides, list):
        errors.append("explicit user sequence overrides must be a list")
    else:
        for index, override in enumerate(raw_sequence_overrides):
            if not isinstance(override, dict):
                errors.append(f"explicit user sequence override {index} is not an object")
                continue
            project_id = override.get("project_id")
            prior_ids = override.get("nonpassing_prior_project_ids")
            if project_id not in EXPECTED_PROJECT_IDS:
                errors.append(
                    f"explicit user sequence override {index} has unknown project"
                )
                continue
            expected_prior_ids = set(
                EXPECTED_PROJECT_IDS[: EXPECTED_PROJECT_IDS.index(project_id)]
            )
            if (
                not isinstance(prior_ids, list)
                or not prior_ids
                or not set(prior_ids) <= expected_prior_ids
            ):
                errors.append(
                    f"explicit user sequence override {project_id} has invalid prior projects"
                )
                continue
            if not nonempty_text(override.get("authority")):
                errors.append(
                    f"explicit user sequence override {project_id} has no authority"
                )
                continue
            if override.get("does_not_mark_prior_projects_passed") is not True:
                errors.append(
                    f"explicit user sequence override {project_id} may rewrite prior results"
                )
                continue
            sequence_overrides[str(project_id)] = set(str(item) for item in prior_ids)
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
    no_active_project_expected = (
        active_project_id is None
        and (
            portfolio.get("execution_status") == "closed"
            or (
                portfolio.get("execution_status")
                in {
                    "awaiting_prior_project_resolution",
                    "r8_codex_fallback_in_progress",
                }
                and any(
                    project.get("status") != "passed"
                    for project in project_manifests.values()
                )
            )
        )
    )
    if no_active_project_expected:
        if active_status_projects:
            errors.append("portfolio awaiting prior resolution still has an active project")
    elif active_status_projects != [active_project_id]:
        errors.append("active project ID and project statuses are inconsistent")
    for index, project_id in enumerate(EXPECTED_PROJECT_IDS):
        project = project_manifests.get(project_id)
        if project is None:
            continue
        started = project.get("latest_revision", 0) > 0 or project.get("status") != "selected"
        prior_ids = EXPECTED_PROJECT_IDS[:index]
        nonpassing_prior_ids = {
            prior_id
            for prior_id in prior_ids
            if project_manifests.get(prior_id, {}).get("status") != "passed"
        }
        if (
            started
            and nonpassing_prior_ids
            and sequence_overrides.get(project_id) != nonpassing_prior_ids
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
    product_source_present = any(
        path.exists()
        for path in (
            root / "platform/backend/src",
            root / "platform/frontend",
            root / "scripts/run_v04_13_codex_builder.py",
        )
    )
    if product_source_present:
        try:
            generality = CapabilityGeneralityGate.from_project_manifests(
                project_manifests
            ).inspect_repository(root)
        except CapabilityGeneralityConfigurationError as error:
            errors.append(f"capability generality policy is invalid: {error}")
        else:
            errors.extend(
                "capability generality gate rejected "
                + finding.public_detail
                for finding in generality.findings
            )
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
        if r8_final_project_ids != set(EXPECTED_PROJECT_IDS):
            errors.append("closed r8 portfolio requires six signed final attempt records")
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
    portfolio = load_json(args.root.resolve() / PORTFOLIO_PATH)
    active_project = next(
        (
            project
            for project in portfolio["projects"]
            if str(project["status"]).startswith("active")
        ),
        None,
    )
    if active_project is None:
        print(f"- active: none; status={portfolio['execution_status']}")
    else:
        print(
            f"- active: {active_project['project_id']} "
            f"revision {active_project['latest_revision']}"
        )
    print("- capability lane enterprise denominator: false")
    print("- provider egress default: disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
