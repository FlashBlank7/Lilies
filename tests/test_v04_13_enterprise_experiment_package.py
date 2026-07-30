from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_platform.task_packages import TaskPackageManager
from scripts.experiments.exp_lilies_001.fault_proxy import (
    _fault_status,
    _record_request,
)
from scripts.experiments.exp_lilies_001.generate_package import (
    EXPECTED_DECISIONS,
    PARENT_REVISION,
    REVISION as PACKAGE_REVISION,
    SCENARIO_COUNTS,
    TASK_ID,
    generate,
)
from scripts import run_v04_13_enterprise_experiment_preparation as preparation


def _generated(tmp_path: Path) -> Path:
    target = tmp_path / "EXP-LILIES-001" / str(PACKAGE_REVISION)
    generate(target)
    return target


def _without_keys(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in keys
    }


def test_preparation_uses_only_validated_runtime_environment_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-environment.json"
    environment = {
        "status": "blocked_by_environment",
        "docker_daemon_probe": "passed",
        "real_host_runs": 0,
    }
    denominator = {
        "required_hidden_records_per_run": 36,
        "required_runs": 3,
        "completed_runs": 0,
        "passed_runs": 0,
        "failed_runs": 0,
        "not_run_runs": 3,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "v0.4.13-t01h-runtime-environment-1",
                "stage_task_id": "V04-13-T01H",
                "experiment_task_id": TASK_ID,
                    "revision": preparation.REVISION,
                    "environment": environment,
                    "enterprise_denominator": denominator,
                }
            ),
        encoding="utf-8",
    )
    monkeypatch.setattr(preparation, "RUNTIME_ENVIRONMENT_PATH", path)

    assert preparation._runtime_environment() == {
        "environment": environment,
        "enterprise_denominator": denominator,
    }

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime-environment evidence is invalid"):
        preparation._runtime_environment()


def test_exp_lilies_001_revision_freezes_with_real_host_contract(
    tmp_path: Path,
) -> None:
    source = _generated(tmp_path)
    manager = TaskPackageManager(tmp_path / "state")
    for revision in range(1, PACKAGE_REVISION):
        manager.freeze_revision(preparation.TASK_ROOT.parent / str(revision))
    package = manager.freeze_revision(source)

    assert package.task.task_id == TASK_ID
    assert package.task.revision == PACKAGE_REVISION
    assert package.task.parent_revision == PARENT_REVISION
    assert package.task.amendment_reason
    assert package.task.cohort.value == "enterprise"
    assert package.environment.provenance == "real_host"
    assert package.allowed_actions.validation_mode == "real_host"
    assert package.budget.max_build_repair_turns == 120
    assert package.budget.max_model_cost_usd == 20
    assert package.budget.assignment_wall_clock_seconds == 10_800
    assert package.budget.max_platform_tool_calls == 800
    assert package.budget.max_report_evidence_rounds == 3
    assert package.budget.stable_hidden_runs == 3
    assert {project.release for project in package.task.source_projects} == {
        "v2.20.15",
        "1.4.2",
    }
    assert all(
        str(project.image_digest).startswith("sha256:")
        for project in package.task.source_projects
    )
    assert package.allowed_actions.model_access is False
    assert {
        "inventree.attachment_list",
        "inventree.metadata_pk_retrieve",
    } <= set(package.allowed_actions.readable_host_objects)
    assert set(package.allowed_actions.writable_host_operations) == {
        "paperless.documents_partial_update",
        "inventree.attachment_create",
        "inventree.metadata_pk_partial_update",
    }
    assert set(package.allowed_actions.permission_required_actions) == {
        "paperless.documents_partial_update",
        "inventree.attachment_create",
        "inventree.metadata_pk_partial_update",
    }
    assert set(package.allowed_actions.compensation_actions) == {
        "inventree.attachment_destroy",
        "inventree.metadata_pk_update",
    }
    assert package.allowed_actions.max_write_count == 18

    requirement = (source / "requirement.md").read_text(encoding="utf-8")
    assert "link-only 外部关联" in requirement
    assert "不是二进制文件复制" in requirement
    assert "operation-contract overlay" in requirement


def test_revision_twenty_eight_adds_only_external_builder_authority_semantics(
    tmp_path: Path,
) -> None:
    current = _generated(tmp_path)
    parent = preparation.TASK_ROOT.parent / str(PARENT_REVISION)

    parent_paths = {
        path.relative_to(parent).as_posix()
        for path in parent.rglob("*")
        if path.is_file()
    }
    current_paths = {
        path.relative_to(current).as_posix()
        for path in current.rglob("*")
        if path.is_file()
    }
    assert current_paths == parent_paths
    changed_paths = {
        relative
        for relative in current_paths
        if relative not in parent_paths
        or (current / relative).read_bytes()
        != (parent / relative).read_bytes()
    }
    assert changed_paths == {
        "allowed-actions.json",
        "CUSTOMER_REQUIREMENT_PACKAGE.json",
        "budget.json",
        "environment.lock",
        "fixtures/manifest.json",
        "protected/expected-state/seed-101.json",
        "protected/expected-state/seed-202.json",
        "protected/expected-state/seed-303.json",
        "protected/hidden-inputs/101/seed-plan.json",
        "protected/hidden-inputs/202/seed-plan.json",
        "protected/hidden-inputs/303/seed-plan.json",
        "protected/oracle/host-oracle.json",
        "protected/oracle/oracle.json",
        "task.yaml",
    }

    parent_task = yaml.safe_load((parent / "task.yaml").read_bytes())
    current_task = yaml.safe_load((current / "task.yaml").read_bytes())
    assert current_task["revision"] == PACKAGE_REVISION
    assert current_task["parent_revision"] == PARENT_REVISION
    assert "fresh external Codex may act as the isolated Builder" in current_task[
        "amendment_reason"
    ]
    assert "uses only Lilies platform public APIs and functions" in current_task[
        "amendment_reason"
    ]
    task_wrapper_keys = (
        "revision",
        "parent_revision",
        "amendment_reason",
        "created_at",
        "environment_lock_digest",
        "fixture_manifest_digest",
    )
    assert _without_keys(current_task, *task_wrapper_keys) == _without_keys(
        parent_task,
        *task_wrapper_keys,
    )

    parent_environment = yaml.safe_load(
        (parent / "environment.lock").read_bytes()
    )
    current_environment = yaml.safe_load(
        (current / "environment.lock").read_bytes()
    )
    assert current_environment["revision"] == PACKAGE_REVISION
    assert (
        current_environment["source_projects"]
        == parent_environment["source_projects"]
    )
    assert _without_keys(current_environment, "revision") == _without_keys(
        parent_environment,
        "revision",
    )

    parent_public = {
        path.relative_to(parent / "fixtures/public-inputs").as_posix():
        path.read_bytes()
        for path in (parent / "fixtures/public-inputs").rglob("*")
        if path.is_file()
    }
    current_public = {
        path.relative_to(current / "fixtures/public-inputs").as_posix():
        path.read_bytes()
        for path in (current / "fixtures/public-inputs").rglob("*")
        if path.is_file()
    }
    assert current_public == parent_public

    for name in ("budget.json",):
        parent_value = json.loads((parent / name).read_bytes())
        current_value = json.loads((current / name).read_bytes())
        assert current_value["revision"] == PACKAGE_REVISION
        assert _without_keys(current_value, "revision") == _without_keys(
            parent_value,
            "revision",
        )

    parent_allowed = json.loads(
        (parent / "allowed-actions.json").read_bytes()
    )
    current_allowed = json.loads(
        (current / "allowed-actions.json").read_bytes()
    )
    assert current_allowed["revision"] == PACKAGE_REVISION
    assert _without_keys(current_allowed, "revision") == _without_keys(
        parent_allowed,
        "revision",
    )
    for key in (
        "readable_host_objects",
        "writable_host_operations",
        "permission_required_actions",
        "compensation_actions",
        "network_hosts",
        "model_access",
        "max_write_count",
    ):
        assert current_allowed[key] == parent_allowed[key]

    parent_manual = json.loads(
        (parent / "BUILDER_API_MANUAL.json").read_bytes()
    )
    current_manual = json.loads(
        (current / "BUILDER_API_MANUAL.json").read_bytes()
    )
    assert parent_manual["platform"]["operation_count"] == 17
    assert current_manual["platform"]["operation_count"] == 17
    assert (
        current_manual["platform"]["connector_authorization"]["operation_id"]
        == "platform_connector_authorization_issue"
    )
    assert current_manual == parent_manual

    parent_fixture_manifest = json.loads(
        (parent / "fixtures/manifest.json").read_bytes()
    )
    current_fixture_manifest = json.loads(
        (current / "fixtures/manifest.json").read_bytes()
    )
    assert current_fixture_manifest["revision"] == PACKAGE_REVISION
    assert _without_keys(
        current_fixture_manifest,
        "revision",
    ) == _without_keys(parent_fixture_manifest, "revision")

    parent_debug = json.loads(
        (parent / "fixtures/public-inputs/debug-records.json").read_bytes()
    )
    current_debug = json.loads(
        (current / "fixtures/public-inputs/debug-records.json").read_bytes()
    )
    assert current_debug == parent_debug
    assert current_debug["revision"] == parent_debug["revision"] == 23

    for seed in ("101", "202", "303"):
        relative_plan = Path(
            f"protected/hidden-inputs/{seed}/seed-plan.json"
        )
        parent_plan = json.loads((parent / relative_plan).read_bytes())
        current_plan = json.loads((current / relative_plan).read_bytes())
        assert current_plan["revision"] == PACKAGE_REVISION
        assert _without_keys(current_plan, "revision") == _without_keys(
            parent_plan,
            "revision",
        )
        assert current_plan["records"] == parent_plan["records"]
        assert current_plan["documents"] == parent_plan["documents"]

        relative_expected = Path(
            f"protected/expected-state/seed-{seed}.json"
        )
        parent_expected = json.loads(
            (parent / relative_expected).read_bytes()
        )
        current_expected = json.loads(
            (current / relative_expected).read_bytes()
        )
        assert current_expected["revision"] == PACKAGE_REVISION
        assert _without_keys(current_expected, "revision") == _without_keys(
            parent_expected,
            "revision",
        )
        assert current_expected["records"] == parent_expected["records"]

    for name in ("oracle.json", "host-oracle.json"):
        relative = Path("protected/oracle") / name
        parent_oracle = json.loads((parent / relative).read_bytes())
        current_oracle = json.loads((current / relative).read_bytes())
        assert current_oracle["revision"] == PACKAGE_REVISION
        assert _without_keys(current_oracle, "revision") == _without_keys(
            parent_oracle,
            "revision",
        )
        assert current_oracle["checks"] == parent_oracle["checks"]
        assert [
            (check["check_id"], check.get("expected"))
            for check in current_oracle["checks"]
        ] == [
            (check["check_id"], check.get("expected"))
            for check in parent_oracle["checks"]
        ]

    parent_pdfs = sorted(
        path.relative_to(parent)
        for path in parent.rglob("*.pdf")
    )
    current_pdfs = sorted(
        path.relative_to(current)
        for path in current.rglob("*.pdf")
    )
    assert current_pdfs == parent_pdfs
    assert len(current_pdfs) == 24 + (3 * 36)
    for relative in current_pdfs:
        assert (current / relative).read_bytes() == (parent / relative).read_bytes()

    for relative in (
        Path("requirement.md"),
        Path("protected/leak-markers.json"),
    ):
        assert (current / relative).read_bytes() == (parent / relative).read_bytes()

    customer_package = json.loads(
        (current / "CUSTOMER_REQUIREMENT_PACKAGE.json").read_bytes()
    )
    assert customer_package["task_id"] == TASK_ID
    assert customer_package["revision"] == PACKAGE_REVISION
    assert customer_package["material_completeness"] == "partial"
    material_paths = {
        item["path"] for item in customer_package["materials"]
    }
    assert {
        "requirement.md",
        "fixtures/public-inputs/debug-records.json",
        "BUILDER_API_MANUAL.json",
        "allowed-actions.json",
        "environment.lock",
        "task.yaml",
    } <= material_paths
    assert len(
        {
            path
            for path in material_paths
            if path.startswith("fixtures/public-inputs/documents/")
        }
    ) == 24
    assert customer_package["missing_materials"]
    assert "Customer non-provision is not automatically a task gap" in (
        customer_package["clarification_policy"]
    )
    assert any(
        "required Builder deliverable" in item
        for item in customer_package["missing_materials"]
    )


def test_exp_lilies_001_has_exact_public_and_hidden_denominators(
    tmp_path: Path,
) -> None:
    source = _generated(tmp_path)
    public = json.loads(
        (source / "fixtures/public-inputs/debug-records.json").read_bytes()
    )
    public_counts = {
        scenario: sum(
            record["scenario"] == scenario for record in public["records"]
        )
        for scenario in SCENARIO_COUNTS["debug"]
    }
    assert public_counts == SCENARIO_COUNTS["debug"]
    assert len(public["records"]) == 24
    assert len(list((source / "fixtures/public-inputs/documents").glob("*.pdf"))) == 24

    for seed in ("101", "202", "303"):
        hidden_root = source / "protected/hidden-inputs" / seed
        plan = json.loads((hidden_root / "seed-plan.json").read_bytes())
        hidden_counts = {
            scenario: sum(
                record["scenario"] == scenario for record in plan["records"]
            )
            for scenario in SCENARIO_COUNTS["hidden"]
        }
        assert hidden_counts == SCENARIO_COUNTS["hidden"]
        assert len(plan["records"]) == 36
        assert len(list((hidden_root / "documents").glob("*.pdf"))) == 36
        assert {
            record["expected_decision"] for record in plan["records"]
        } == set(EXPECTED_DECISIONS.values())


def test_exp_lilies_001_oracle_covers_36_records_hosts_and_xlsx(
    tmp_path: Path,
) -> None:
    source = _generated(tmp_path)
    oracle = json.loads(
        (source / "protected/oracle/oracle.json").read_bytes()
    )
    host_oracle = json.loads(
        (source / "protected/oracle/host-oracle.json").read_bytes()
    )
    checks = oracle["checks"]
    check_ids = {check["check_id"] for check in checks}
    host_checks = host_oracle["checks"]
    host_check_ids = {check["check_id"] for check in host_checks}

    assert len(checks) == 79
    assert len(host_checks) == 39
    assert len(checks) + len(host_checks) == 118
    assert len(check_ids) == len(checks)
    assert len(host_check_ids) == len(host_checks)
    assert {
        "result-record-count",
        "result-forbidden-write-count",
        "workbook-sheet",
        "workbook-headers",
        "workbook-row-count",
        "workbook-first-record",
        "workbook-last-record",
    } <= check_ids
    assert {
        "host-state-record-count",
        "host-state-duplicate-effects",
        "host-state-forbidden-writes",
    } <= host_check_ids
    assert not any(
        check.get("evidence_selector", {}).get("label")
        == "independent-host-state.json"
        for check in checks
    )
    for index in range(1, 37):
        assert f"record-{index:03d}-identity" in check_ids
        assert f"record-{index:03d}-decision" in check_ids
        assert f"record-{index:03d}-host-write-count" in host_check_ids


def test_exp_lilies_001_contains_real_text_and_raster_pdf_inputs(
    tmp_path: Path,
) -> None:
    source = _generated(tmp_path)
    documents = source / "fixtures/public-inputs/documents"
    text_pdf = next(documents.glob("*-text_pdf.pdf")).read_bytes()
    scan_pdf = next(documents.glob("*-scan.pdf")).read_bytes()

    assert text_pdf.startswith(b"%PDF-1.4")
    assert b"/BaseFont /Helvetica" in text_pdf
    assert b"PURCHASE ORDER" in text_pdf
    assert scan_pdf.startswith(b"%PDF-1.4")
    assert b"/Subtype /Image" in scan_pdf
    assert b"/FlateDecode" in scan_pdf
    assert b"PURCHASE ORDER" not in scan_pdf


def test_public_workspace_contains_no_hidden_seed_or_oracle_canary(
    tmp_path: Path,
) -> None:
    source = _generated(tmp_path)
    public_payload = b"\n".join(
        path.read_bytes()
        for path in sorted((source / "fixtures").rglob("*"))
        if path.is_file()
    )

    assert b"EXP-LILIES-001-ORACLE-CANARY" not in public_payload
    assert b'"seed":101' not in public_payload
    assert b'"seed":202' not in public_payload
    assert b'"seed":303' not in public_payload
    assert b"HID-001" not in public_payload


def test_fault_proxy_injects_once_then_preserves_exactly_once_retry(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fault-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "active": True,
                "transient_source_ids": ["DOC-101-0031"],
                "permission_source_ids": ["DOC-101-0034"],
                "consumed_transient_source_ids": [],
            }
        ),
        encoding="utf-8",
    )

    assert (
        _fault_status(
            state_path,
            method="PATCH",
            request_text='{"source_id":"DOC-101-0031"}',
        )
        == 503
    )
    assert (
        _fault_status(
            state_path,
            method="PATCH",
            request_text='{"source_id":"DOC-101-0031"}',
        )
        is None
    )
    assert (
        _fault_status(
            state_path,
            method="PATCH",
            request_text='{"source_id":"DOC-101-0034"}',
        )
        == 403
    )
    assert (
        _fault_status(
            state_path,
            method="GET",
            request_text="DOC-101-0034",
        )
        is None
    )

    state = json.loads(state_path.read_bytes())
    state["all_source_ids"] = [
        "DOC-101-0031",
        "DOC-101-0034",
    ]
    state["request_log"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _record_request(
        state_path,
        proxy_id="inventree",
        method="PATCH",
        path="/api/order/po/7/",
        request_body=b'{"source_id":"DOC-101-0031"}',
        response_body=b'{"pk":7}',
        status=200,
        injected=False,
    )
    recorded = json.loads(state_path.read_bytes())["request_log"]
    assert recorded == [
        {
            "injected": False,
            "method": "PATCH",
            "path": "/api/order/po/7/",
            "proxy": "inventree",
            "request_digest": (
                "sha256:"
                + hashlib.sha256(
                    b'{"source_id":"DOC-101-0031"}'
                ).hexdigest()
            ),
            "response_digest": (
                "sha256:" + hashlib.sha256(b'{"pk":7}').hexdigest()
            ),
            "sequence": 1,
            "source_ids": ["DOC-101-0031"],
            "status": 200,
        }
    ]


def test_generator_refuses_to_replace_an_existing_revision(
    tmp_path: Path,
) -> None:
    target = _generated(tmp_path)

    with pytest.raises(FileExistsError):
        generate(target)
