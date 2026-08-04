from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests import test_v04_13_portfolio_rerun as rerun_test_support


ROOT = Path(__file__).resolve().parents[1]


def load_validator() -> Any:
    module_path = ROOT / "scripts" / "validate_real_project_portfolio.py"
    spec = importlib.util.spec_from_file_location(
        "real_project_portfolio_validator_under_test", module_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_validation_fixture(tmp_path: Path) -> Path:
    collaboration = Path("docs/experiments/lilies-collaboration")
    target = tmp_path / collaboration
    target.parent.mkdir(parents=True)
    shutil.copytree(ROOT / collaboration, target)
    evolution = tmp_path / "docs/evolution-control"
    (evolution / "stage-contracts").mkdir(parents=True)
    shutil.copy2(
        ROOT / "docs/evolution-control/report_intents.json",
        evolution / "report_intents.json",
    )
    shutil.copy2(
        ROOT / "docs/evolution-control/stage-contracts/v0.4.13-r8.json",
        evolution / "stage-contracts/v0.4.13-r8.json",
    )
    return tmp_path


def rewrite_json(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def refresh_manifest_locks(fixture: Path, module: Any) -> None:
    portfolio_path = fixture / module.PORTFOLIO_PATH
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    manifest_bytes: list[bytes] = []
    for member in portfolio["projects"]:
        manifest_path = fixture / member["manifest"]
        content = manifest_path.read_bytes()
        manifest_bytes.append(content)
        manifest = json.loads(content)
        member["manifest_sha256"] = hashlib.sha256(content).hexdigest()
        member["latest_revision"] = manifest["latest_revision"]
        member["status"] = manifest["status"]
    portfolio["member_manifest_set_sha256"] = hashlib.sha256(
        b"".join(manifest_bytes)
    ).hexdigest()
    portfolio_path.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_current_real_project_portfolio_is_valid() -> None:
    module = load_validator()

    assert module.validate_portfolio() == []


def test_project_revisions_remain_one_project_not_separate_projects() -> None:
    portfolio = json.loads(
        (
            ROOT
            / "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
        ).read_text(encoding="utf-8")
    )
    first = portfolio["projects"][0]

    assert portfolio["required_project_count"] == 6
    assert len(portfolio["projects"]) == 6
    assert first["project_id"] == "EXP-LILIES-001"
    assert first["latest_revision"] == 28
    assert [item["project_id"] for item in portfolio["projects"]] == [
        f"EXP-LILIES-{number:03d}" for number in range(1, 7)
    ]


def test_portfolio_rejects_duplicate_project_members(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.PORTFOLIO_PATH

    rewrite_json(
        path,
        lambda payload: payload["projects"].__setitem__(
            5, dict(payload["projects"][0])
        ),
    )

    errors = module.validate_portfolio(fixture)

    assert any("EXP-LILIES-001 through EXP-LILIES-006" in error for error in errors)
    assert "portfolio project IDs are not unique" in errors


def test_portfolio_rejects_non_http_protocol_reordering(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.PORTFOLIO_PATH

    rewrite_json(
        path,
        lambda payload: payload["protocol_sequence"].reverse(),
    )

    errors = module.validate_portfolio(fixture)

    assert any("Home Assistant WebSocket" in error for error in errors)


def test_capability_lane_cannot_enter_customer_denominator(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    path = fixture / module.GAP_PATH

    rewrite_json(
        path,
        lambda payload: payload.__setitem__("enterprise_denominator", True),
    )

    errors = module.validate_portfolio(fixture)

    assert "platform capability lane must remain outside enterprise denominator" in errors


def test_later_project_cannot_start_before_previous_project_passes(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-002/project.json"
    )

    def start_project(payload: dict[str, Any]) -> None:
        payload["status"] = "active"
        payload["latest_revision"] = 0

    rewrite_json(project_path, start_project)
    portfolio_path = fixture / module.PORTFOLIO_PATH

    def remove_project_override(payload: dict[str, Any]) -> None:
        payload["execution_policy"]["explicit_user_sequence_overrides"] = [
            item
            for item in payload["execution_policy"]["explicit_user_sequence_overrides"]
            if item["project_id"] != "EXP-LILIES-002"
        ]

    rewrite_json(portfolio_path, remove_project_override)
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-002 started before every previous project passed" in errors
    assert "portfolio awaiting prior resolution still has an active project" in errors


def test_explicit_user_sequence_override_preserves_prior_project_results(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    portfolio_path = fixture / module.PORTFOLIO_PATH
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    override = next(
        item
        for item in portfolio["execution_policy"]["explicit_user_sequence_overrides"]
        if item["project_id"] == "EXP-LILIES-003"
    )

    assert override["project_id"] == "EXP-LILIES-003"
    assert override["does_not_mark_prior_projects_passed"] is True
    assert {
        project["project_id"]: project["status"]
        for project in portfolio["projects"][:3]
    } == {
        "EXP-LILIES-001": "needs_revision",
        "EXP-LILIES-002": "passed",
        "EXP-LILIES-003": "passed",
    }
    assert module.validate_portfolio(fixture) == []


def test_replacement_project_requires_baseline_compatibility_and_rollback(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-003/project.json"
    )

    rewrite_json(
        project_path,
        lambda payload: payload.pop("replacement_contract"),
    )
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-003 replacement has no compatibility contract" in errors


def test_capability_entry_requires_bound_origin_review_and_rerun(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    gap_path = fixture / module.GAP_PATH

    def add_malformed_entry(payload: dict[str, Any]) -> None:
        payload["entries"] = [
            {
                "capability_id": "CAP-LILIES-BAD",
                "origin": {
                    "project_id": "EXP-LILIES-999",
                    "task_revision": 1,
                    "attempt_id": "",
                    "evidence_digest": "not-a-digest",
                },
                "generality_evidence": {},
                "routing_evidence": "",
                "implementation_diff": "",
                "platform_tests": "",
                "independent_review": "",
                "affected_project_reruns": [],
                "contains_project_specific_adapter": True,
                "contains_field_mapping": False,
                "contains_webhook_handler": False,
                "contains_sdk_wrapper": False,
                "contains_final_workflow": False,
                "contains_oracle_material": False,
                "enterprise_denominator": False,
                "status": "implemented_verified",
            }
        ]

    rewrite_json(gap_path, add_malformed_entry)

    errors = module.validate_portfolio(fixture)

    assert any("unknown origin project" in error for error in errors)
    assert any("invalid evidence digest" in error for error in errors)
    assert any("does not reject contains_project_specific_adapter" in error for error in errors)
    assert any("has no independent_review" in error for error in errors)
    assert any("has no affected-project rerun" in error for error in errors)


def test_historical_project_passes_cannot_close_r8_without_final_attempts(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    portfolio_path = fixture / module.PORTFOLIO_PATH

    for project_id in module.EXPECTED_PROJECT_IDS:
        project_path = (
            fixture
            / "docs/experiments/lilies-collaboration"
            / project_id
            / "project.json"
        )
        rewrite_json(
            project_path,
            lambda payload: payload.__setitem__("status", "passed"),
        )
    refresh_manifest_locks(fixture, module)

    rewrite_json(
        portfolio_path,
        lambda payload: payload.__setitem__("execution_status", "closed"),
    )

    errors = module.validate_portfolio(fixture)

    assert "closed r8 portfolio requires six signed final attempt records" in errors


def test_six_r8_final_records_close_without_relabeling_historical_statuses(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    portfolio_path = fixture / module.PORTFOLIO_PATH
    records: list[dict[str, Any]] = []
    phase_percentages = (10.0, 0.0, 0.0, 20.0, 30.0, 15.0, 15.0, 10.0)
    for index, project_id in enumerate(module.EXPECTED_PROJECT_IDS, start=1):
        attempt_id = f"00000000-0000-0000-0000-{index:012d}"
        workflow_hash = rerun_test_support._digest(f"workflow-{project_id}")
        prerequisite_digest = rerun_test_support._digest(
            f"prerequisite-{project_id}"
        )
        scan_digest = rerun_test_support._digest(f"scan-{project_id}")
        report = {
            "schema_version": "v0.4.13-portfolio-rerun-report-body-r8-1",
            "project_id": project_id,
            "attempt_id": attempt_id,
            "formal_builder_actor": "codex",
            "builder_actor": "codex_fallback",
            "status": "completed",
            "timing_complete": True,
            "max_session_tokens": None,
            "final_token_checkpoint": None,
            "final_codex_token_usage": {
                "availability": "unavailable",
                "attempted_calls": None,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "reason": "authoritative counter unavailable",
            },
            "phases": [
                {
                    "phase": phase,
                    "duration_seconds": phase_percentages[phase_index],
                    "duration_percentage": phase_percentages[phase_index],
                    "outcome": (
                        "not_applicable"
                        if phase_index in {1, 2}
                        else "completed"
                    ),
                }
                for phase_index, phase in enumerate(
                    (
                        "environment_bootstrap",
                        "daemon_discovery",
                        "explicit_pairing",
                        "assignment_provision",
                        "builder_execution",
                        "host_result_verification",
                        "platform_archive_verification",
                        "cleanup_reporting",
                    )
                )
            ],
            "execution_evidence": {
                "published_version": 1,
                "published_content_hash": workflow_hash,
                "fallback_eligibility": {
                    "prerequisite_payload_digest": prerequisite_digest,
                    "forbidden_assistance_scan_digest": scan_digest,
                },
                "acceptance_receipts": [
                    {
                        "case_id": case_id,
                        "status": "passed",
                        "published_version": 1,
                        "published_content_hash": workflow_hash,
                    }
                    for case_id in ("debug", "seed-1", "seed-2", "seed-3")
                ]
            },
        }
        evidence_relative = Path(
            f"docs/evidence/v0.4.13/t01h/runs/attempts/{project_id}.json"
        )
        evidence_path = fixture / evidence_relative
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        payload = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "contract_revision": 8,
            "formal_builder_actor": "codex",
            "builder_actor": "codex_fallback",
            "status": "passed",
            "eligible_for_final": True,
            "published_version": 1,
            "workflow_content_hash": workflow_hash,
            "prerequisite_receipt_digest": prerequisite_digest,
            "forbidden_assistance_scan_digest": scan_digest,
            "signed_report_digest": module.canonical_digest(report),
            "evidence_path": evidence_relative.as_posix(),
            "evidence_sha256": module.sha256(evidence_path),
            "debug_passed": True,
            "protected_seed_pass_count": 3,
            "phase_percentage_sum": 100.0,
        }
        records.append(
            asdict(
                rerun_test_support._sign_envelope(
                    f"00000000-0000-0000-1000-{index:012d}",
                    "r8_final_attempt",
                    payload,
                    float(index),
                )
            )
        )

    def close_with_r8_records(payload: dict[str, Any]) -> None:
        payload["execution_status"] = "closed"
        payload["r8_final_receipt_trust_root"] = asdict(
            rerun_test_support.TEST_TRUST_ROOT
        )
        payload["r8_final_receipt_trust_root_digest"] = (
            rerun_test_support.TEST_TRUST_ROOT.verifier_digest
        )
        payload["r8_final_attempt_records"] = records

    rewrite_json(portfolio_path, close_with_r8_records)

    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    assert portfolio["projects"][0]["status"] == "needs_revision"
    assert module.validate_portfolio(fixture) == []


def test_hand_authored_r8_summary_hashes_cannot_close_portfolio(
    tmp_path: Path,
) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    portfolio_path = fixture / module.PORTFOLIO_PATH

    def add_unsigned_rows(payload: dict[str, Any]) -> None:
        payload["execution_status"] = "closed"
        payload["r8_final_receipt_trust_root"] = asdict(
            rerun_test_support.TEST_TRUST_ROOT
        )
        payload["r8_final_receipt_trust_root_digest"] = (
            rerun_test_support.TEST_TRUST_ROOT.verifier_digest
        )
        payload["r8_final_attempt_records"] = [
            {
                "project_id": project_id,
                "attempt_id": f"00000000-0000-0000-0000-{index:012d}",
                "contract_revision": 8,
                "formal_builder_actor": "codex",
                "builder_actor": "codex_fallback",
                "status": "passed",
                "eligible_for_final": True,
                "published_version": 1,
                "workflow_content_hash": "0" * 64,
                "prerequisite_receipt_digest": "1" * 64,
                "forbidden_assistance_scan_digest": "2" * 64,
                "signed_report_digest": "3" * 64,
                "debug_passed": True,
                "protected_seed_pass_count": 3,
                "phase_percentage_sum": 100.0,
            }
            for index, project_id in enumerate(module.EXPECTED_PROJECT_IDS, start=1)
        ]

    rewrite_json(portfolio_path, add_unsigned_rows)
    errors = module.validate_portfolio(fixture)

    assert any("final attempt record 1" in error for error in errors)
    assert "closed r8 portfolio requires six signed final attempt records" in errors


def test_negative_latest_revision_is_rejected(tmp_path: Path) -> None:
    module = load_validator()
    fixture = copy_validation_fixture(tmp_path)
    project_path = (
        fixture
        / "docs/experiments/lilies-collaboration/EXP-LILIES-002/project.json"
    )

    rewrite_json(
        project_path,
        lambda payload: payload.__setitem__("latest_revision", -1),
    )
    refresh_manifest_locks(fixture, module)

    errors = module.validate_portfolio(fixture)

    assert "EXP-LILIES-002 latest_revision must be a non-negative integer" in errors
