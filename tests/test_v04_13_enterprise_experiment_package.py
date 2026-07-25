from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_platform.task_packages import TaskPackageManager
from scripts.experiments.exp_lilies_001.fault_proxy import (
    _fault_status,
    _record_request,
)
from scripts.experiments.exp_lilies_001.generate_package import (
    EXPECTED_DECISIONS,
    SCENARIO_COUNTS,
    TASK_ID,
    generate,
)
from scripts import run_v04_13_enterprise_experiment_preparation as preparation


def _generated(tmp_path: Path) -> Path:
    target = tmp_path / "EXP-LILIES-001" / str(preparation.REVISION)
    generate(target)
    return target


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
    for revision in range(1, preparation.REVISION):
        manager.freeze_revision(preparation.TASK_ROOT.parent / str(revision))
    package = manager.freeze_revision(source)

    assert package.task.task_id == TASK_ID
    assert package.task.revision == preparation.REVISION
    assert package.task.parent_revision == preparation.REVISION - 1
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
