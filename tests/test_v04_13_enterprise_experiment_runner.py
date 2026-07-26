from __future__ import annotations

import json
import signal
import shutil
import stat
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_platform.lilies_models import PermissionDecisionRequest
from scripts.experiments.exp_lilies_001 import attestation_server
from scripts.experiments.exp_lilies_001 import environment_control
from scripts.experiments.exp_lilies_001 import verify_host_snapshot
from scripts import run_v04_13_enterprise_experiment as runner


def test_attestation_and_platform_resolve_the_exact_same_secret_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = environment_control._secret_state(tmp_path, create=True)
    monkeypatch.setenv(
        "EXP_LILIES_ATTESTATION_SECRET",
        state["attestation_secret"],
    )
    monkeypatch.setenv("EXP_LILIES_PAPERLESS_VERIFIER_TOKEN", "p" * 32)
    monkeypatch.setenv("EXP_LILIES_INVENTREE_VERIFIER_TOKEN", "i" * 32)

    configuration = attestation_server._load_configuration()

    assert configuration.attestation_secret == state["attestation_secret"].encode()
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600


def _legacy_compose_container(
    service: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    return {
        "Config": {
            "Labels": {
                "com.docker.compose.project": (
                    environment_control.COMPOSE_PROJECT_NAME
                ),
                "com.docker.compose.service": service,
            },
            "Env": [
                f"{name}={value}"
                for name, value in sorted(environment.items())
            ],
        }
    }


def _matching_legacy_compose_containers(
    secret_state: dict[str, str],
) -> list[dict[str, Any]]:
    containers = []
    for service in sorted(environment_control.EXPECTED_COMPOSE_SERVICES):
        bindings = environment_control.COMPOSE_SECRET_BINDINGS.get(
            service,
            {},
        )
        containers.append(
            _legacy_compose_container(
                service,
                {
                    environment_name: secret_state[secret_name]
                    for environment_name, secret_name in bindings.items()
                },
            )
        )
    return containers


def test_environment_owner_claim_is_atomic_exact_replay_and_path_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    environment_control._secret_state(first_root, create=True)
    environment_control._secret_state(second_root, create=True)
    owner: dict[str, Any] | None = None
    create_calls = 0

    def fake_owner_volume() -> dict[str, Any] | None:
        return owner

    def fake_docker_output(arguments: list[str]) -> str:
        nonlocal create_calls, owner
        assert arguments[:2] == ["volume", "create"]
        create_calls += 1
        labels: dict[str, str] = {}
        for index, argument in enumerate(arguments):
            if argument == "--label":
                name, value = arguments[index + 1].split("=", 1)
                labels[name] = value
        owner = {
            "Name": environment_control.ENVIRONMENT_OWNER_VOLUME,
            "Labels": labels,
        }
        return environment_control.ENVIRONMENT_OWNER_VOLUME + "\n"

    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        fake_owner_volume,
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_containers",
        lambda: [],
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_volumes",
        lambda: [],
    )
    monkeypatch.setattr(
        environment_control,
        "_docker_output",
        fake_docker_output,
    )

    labels = environment_control._environment_owner_labels(first_root)
    assert str(first_root.resolve()) not in labels.values()
    environment_control._claim_environment_owner(first_root)
    environment_control._claim_environment_owner(first_root)
    assert create_calls == 1

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="owned by another state root",
    ):
        environment_control._claim_environment_owner(second_root)


def test_legacy_environment_adoption_requires_complete_matching_containers(
    tmp_path: Path,
) -> None:
    secret_state = environment_control._secret_state(
        tmp_path,
        create=True,
    )
    containers = _matching_legacy_compose_containers(secret_state)

    environment_control._validate_legacy_compose_adoption(
        tmp_path,
        containers=containers,
        project_volumes=["existing-project-data"],
    )

    incomplete = containers[:-1]
    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="incomplete or unexpected",
    ):
        environment_control._validate_legacy_compose_adoption(
            tmp_path,
            containers=incomplete,
            project_volumes=["existing-project-data"],
        )

    mismatched = _matching_legacy_compose_containers(secret_state)
    paperless = next(
        item
        for item in mismatched
        if item["Config"]["Labels"]["com.docker.compose.service"]
        == "paperless"
    )
    paperless["Config"]["Env"] = [
        (
            "PAPERLESS_DBPASS=" + "x" * 48
            if item.startswith("PAPERLESS_DBPASS=")
            else item
        )
        for item in paperless["Config"]["Env"]
    ]
    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="do not match this state root",
    ):
        environment_control._validate_legacy_compose_adoption(
            tmp_path,
            containers=mismatched,
            project_volumes=["existing-project-data"],
        )


def test_legacy_environment_adoption_rejects_unbound_data_volumes(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="refusing ambiguous state-root adoption",
    ):
        environment_control._validate_legacy_compose_adoption(
            tmp_path,
            containers=[],
            project_volumes=["exp-lilies-001-r7_paperless-db"],
        )


def test_environment_control_claims_owner_before_compose_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, object]] = []
    state_root = tmp_path / "environment"
    monkeypatch.setattr(
        environment_control,
        "_claim_environment_owner",
        lambda root: observed.append(("claim", root)),
    )
    monkeypatch.setattr(
        environment_control,
        "_compose",
        lambda root, arguments, *, create_secrets: observed.append(
            ("compose", (root, list(arguments), create_secrets))
        ),
    )
    monkeypatch.setattr(
        environment_control.sys,
        "argv",
        [
            "environment_control.py",
            "--state-root",
            str(state_root),
            "config",
        ],
    )

    assert environment_control.main() == 0
    assert observed == [
        ("claim", state_root.resolve()),
        (
            "compose",
            (state_root.resolve(), ["config", "--quiet"], True),
        ),
    ]
    assert (
        environment_control.ENVIRONMENT_OWNER_VOLUME
        not in environment_control.COMPOSE_PATH.read_text(encoding="utf-8")
    )


def test_attestation_uses_real_minimum_permission_version_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []

    def fake_read(url: str, *, authorization: str) -> tuple[dict[str, Any], dict[str, str]]:
        observed.append((url, authorization))
        if "paperless" in url:
            return {"count": 0, "results": []}, {"x-version": "2.20.15"}
        return {"version": "1.4.2"}, {"x-inventree-version": "1.4.2"}

    monkeypatch.setattr(attestation_server, "_read_json", fake_read)
    attestation_server.verify_real_hosts(
        attestation_server.HostConfiguration(
            paperless_url="http://paperless.example.invalid",
            paperless_token="p" * 32,
            inventree_url="http://inventree.example.invalid",
            inventree_token="i" * 32,
            attestation_secret=b"a" * 32,
        )
    )

    assert observed == [
        (
            "http://paperless.example.invalid/api/documents/?page_size=1",
            "Token " + "p" * 32,
        ),
        (
            "http://inventree.example.invalid/api/",
            "Token " + "i" * 32,
        ),
    ]


def test_initialize_uses_inventree_basic_token_endpoint_and_persists_user_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_control._secret_state(tmp_path, create=True)
    requests: list[tuple[str, dict[str, Any]]] = []
    observed_settings: list[tuple[str, dict[str, Any]]] = []

    def fake_retry(url: str, **kwargs: Any) -> dict[str, str]:
        requests.append((url, kwargs))
        return {"token": "t" * 32}

    def fake_json(url: str, **kwargs: Any) -> dict[str, str]:
        observed_settings.append((url, kwargs))
        return {
            "key": "PURCHASEORDER_REFERENCE_PATTERN",
            "value": "{?:PO-017}-{ref:04d}",
        }

    user_id = 100

    def fake_provision(
        _state_root: Path,
        *,
        host: str,
        role: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal user_id
        user_id += 1
        return {
            "schema_version": "1.0",
            "host": host,
            "role": role,
            "token": f"{host}-{role}-" + "x" * 32,
            "user_id": user_id,
            "permission_codenames": [f"{host}:{role}:view"],
        }

    monkeypatch.setattr(
        environment_control,
        "_json_request_with_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        environment_control,
        "_provision_scoped_account",
        fake_provision,
    )
    monkeypatch.setattr(environment_control, "_json_request", fake_json)

    environment_control._initialize(tmp_path)
    credentials = runner._read_private_json(tmp_path / "credentials.json")

    assert [request[0] for request in requests] == [
        "http://127.0.0.1:18000/api/schema/",
        "http://127.0.0.1:18001/api/",
        "http://127.0.0.1:18000/api/token/",
        (
            "http://127.0.0.1:18001/api/user/me/token/"
            "?name=EXP-LILIES-001-task-author"
        ),
    ]
    assert requests[0][1] == {"timeout_seconds": 1_200}
    assert requests[1][1] == {"timeout_seconds": 1_200}
    assert requests[2][1]["method"] == "POST"
    assert requests[3][1]["basic_auth"][0] == "exp_lilies_admin"
    assert "value" not in requests[3][1]
    assert observed_settings == [
        (
            "http://127.0.0.1:18001/api/settings/global/PURCHASEORDER_REFERENCE_PATTERN/",
            {
                "method": "PATCH",
                "token": "t" * 32,
                "value": {"value": "{?:PO-017}-{ref:04d}"},
            },
        )
    ]
    assert credentials["paperless_builder_user_id"] == 101
    assert credentials["paperless_verifier_user_id"] == 102
    assert credentials["inventree_builder_user_id"] == 103
    assert credentials["inventree_verifier_user_id"] == 104


def test_paperless_task_poll_uses_frozen_related_document_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, dict[str, Any]]] = []

    def fake_json(url: str, **kwargs: Any) -> dict[str, Any]:
        observed.append((url, kwargs))
        return {
            "count": 1,
            "results": [
                {
                    "task_id": "celery-task-1",
                    "status": "SUCCESS",
                    "related_document": "47",
                }
            ],
        }

    monkeypatch.setattr(environment_control, "_json_request", fake_json)

    assert (
        environment_control._paperless_document_id(
            token="p" * 32,
            task_id="celery-task-1",
            timeout_seconds=1,
        )
        == 47
    )
    assert observed[0][0].endswith("/api/tasks/?task_id=celery-task-1")
    assert observed[0][1]["token"] == "p" * 32


def test_scoped_account_source_uses_exact_paperless_task_codename() -> None:
    source = (
        runner.ROOT / "scripts" / "experiments" / "exp_lilies_001" / "provision_scoped_account.py"
    ).read_text(encoding="utf-8")

    assert '"view_paperlesstask"' in source
    assert '"view_papertask"' not in source
    assert "UserProfile.objects.get_or_create(user=user)" in source


def test_scoped_account_provisioning_uses_each_frozen_image_manage_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def fake_compose_environment(
        _state_root: Path,
        *,
        create: bool,
    ) -> dict[str, str]:
        assert create is False
        return {}

    class Completed:
        returncode = 0
        stderr = b""

        def __init__(self, host: str, role: str) -> None:
            self.stdout = (
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "host": host,
                        "role": role,
                        "token": "t" * 32,
                    }
                ).encode()
                + b"\n"
            )

    def fake_run(arguments: list[str], **kwargs: Any) -> Completed:
        observed.append(arguments)
        environment = kwargs["env"]
        return Completed(
            environment["EXP_LILIES_ACCOUNT_HOST"],
            environment["EXP_LILIES_ACCOUNT_ROLE"],
        )

    monkeypatch.setattr(
        environment_control,
        "_compose_environment",
        fake_compose_environment,
    )
    monkeypatch.setattr(environment_control.subprocess, "run", fake_run)

    for service, host, expected_path in (
        ("paperless", "paperless", "manage.py"),
        ("inventree", "inventree", "src/backend/InvenTree/manage.py"),
    ):
        result = environment_control._provision_scoped_account(
            tmp_path,
            service=service,
            host=host,
            role="builder",
            username=f"{host}-builder",
            password="p" * 32,
        )
        assert result["host"] == host
        assert observed[-1][-3:] == ["python", expected_path, "shell"]


def test_seed_creates_supplier_parts_and_waits_for_document_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    package_root = tmp_path / "package"
    documents = package_root / "fixtures" / "public-inputs" / "documents"
    documents.mkdir(parents=True)
    records = [
        {
            "record_id": "PUB-001",
            "source_id": "DOC-001",
            "supplier": "ALPHA",
            "host_part_number": "AX-100",
            "host_purchase_order": "PO-001",
            "purchase_line_quantity": 5,
            "document_date": "2026-01-01",
            "scenario": "exact_match",
        },
        {
            "record_id": "PUB-002",
            "source_id": "DOC-001-DUP",
            "supplier": "ALPHA",
            "host_part_number": "AX-100",
            "host_purchase_order": "PO-001",
            "purchase_line_quantity": 5,
            "document_date": "2026-01-02",
            "scenario": "duplicate",
        },
    ]
    (package_root / "fixtures" / "public-inputs" / "debug-records.json").write_text(
        json.dumps({"records": records}),
        encoding="utf-8",
    )
    for index in (1, 2):
        (documents / f"pub-{index:03d}-text_pdf.pdf").write_bytes(b"%PDF-1.4\n")
    runner._atomic_private_json(
        state_root / "credentials.json",
        {
            "paperless_admin_token": "p" * 32,
            "inventree_admin_token": "i" * 32,
            "paperless_builder_user_id": 11,
            "paperless_verifier_user_id": 12,
        },
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    next_pk = 20

    def fake_json(url: str, **kwargs: Any) -> dict[str, int]:
        nonlocal next_pk
        next_pk += 1
        calls.append((url, kwargs))
        return {"pk": next_pk}

    uploads: list[str] = []

    def fake_upload(**kwargs: Any) -> str:
        uploads.append(str(kwargs["path"]))
        return f"task-{len(uploads)}"

    consumed: list[str] = []

    def fake_document_id(*, task_id: str, **_kwargs: Any) -> int:
        consumed.append(task_id)
        return 100 + len(consumed)

    grants: list[dict[str, Any]] = []
    monkeypatch.setattr(environment_control, "_json_request", fake_json)
    monkeypatch.setattr(
        environment_control,
        "_multipart_document_upload",
        fake_upload,
    )
    monkeypatch.setattr(
        environment_control,
        "_paperless_document_id",
        fake_document_id,
    )
    monkeypatch.setattr(
        environment_control,
        "_grant_paperless_document_access",
        lambda **kwargs: grants.append(kwargs),
    )

    environment_control._seed(state_root, package_root, seed="debug")
    receipt = runner._read_private_json(state_root / "seed-receipts-debug.json")
    supplier_parts = [call for call in calls if call[0].endswith("/api/company/part/")]
    po_lines = [call for call in calls if call[0].endswith("/api/order/po-line/")]

    assert len(supplier_parts) == 1
    assert supplier_parts[0][1]["value"]["supplier"] == 21
    assert supplier_parts[0][1]["value"]["part"] == 22
    assert len(po_lines) == 1
    assert po_lines[0][1]["value"]["part"] == 23
    assert po_lines[0][1]["value"]["merge_items"] is False
    assert consumed == ["task-1", "task-2"]
    assert [item["document_id"] for item in grants] == [101, 102]
    assert [item["paperless_document_id"] for item in receipt["records"]] == [
        101,
        102,
    ]


def test_snapshot_uses_only_verifier_tokens_and_preserves_proxy_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    package_root = tmp_path / "package"
    plan_root = package_root / "fixtures" / "public-inputs"
    plan_root.mkdir(parents=True)
    plan_root.joinpath("debug-records.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "record_id": "PUB-001",
                        "source_id": "DOC-001",
                        "scenario": "transient_error",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    runner._atomic_private_json(
        state_root / "credentials.json",
        {
            "paperless_verifier_token": "paperless-verifier",
            "inventree_verifier_token": "inventree-verifier",
        },
    )
    runner._atomic_private_json(
        state_root / "seed-receipts-debug.json",
        {
            "records": [
                {
                    "record_id": "PUB-001",
                    "paperless_document_id": 7,
                    "inventree_purchase_order_id": 8,
                    "inventree_purchase_order_line_id": 9,
                }
            ]
        },
    )
    runner._atomic_private_json(
        state_root / "fault-state.json",
        {
            "schema_version": "1.0",
            "request_log": [
                {
                    "source_ids": ["DOC-001"],
                    "status": 200,
                    "injected": False,
                },
                {
                    "source_ids": ["DOC-001"],
                    "status": 503,
                    "injected": True,
                },
            ],
        },
    )
    observed_tokens: list[str] = []

    def fake_json(url: str, **kwargs: Any) -> dict[str, Any]:
        observed_tokens.append(kwargs["token"])
        return {"url": url, "state": "real-host"}

    monkeypatch.setattr(environment_control, "_json_request", fake_json)

    path = environment_control._snapshot_host_state(
        state_root,
        package_root,
        seed="debug",
        phase="final",
    )
    snapshot = runner._read_private_json(path)
    record = snapshot["records"][0]

    assert observed_tokens == [
        "paperless-verifier",
        "inventree-verifier",
        "inventree-verifier",
    ]
    assert record["successful_proxy_mutations"] == 1
    assert record["write_count"] == 1
    assert record["injected_transient_failures"] == 1
    assert record["injected_permission_denials"] == 0
    assert snapshot["duplicate_effect_count"] == 0
    assert snapshot["forbidden_write_count"] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_host_snapshot_oracle_binds_write_counts_records_and_fault_gates() -> None:
    snapshot = {
        "schema_version": "1.0",
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "seed": "101",
        "phase": "final",
        "duplicate_effect_count": 0,
        "forbidden_write_count": 0,
        "records": [
            {
                "record_id": "HID-001",
                "scenario": "exact_match",
                "write_count": 1,
                "injected_transient_failures": 0,
                "injected_permission_denials": 0,
            },
            {
                "record_id": "HID-002",
                "scenario": "transient_error",
                "write_count": 1,
                "injected_transient_failures": 1,
                "injected_permission_denials": 0,
            },
        ],
    }
    oracle = {
        "schema_version": "1.0",
        "oracle_id": "host-oracle",
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "validation_mode": "real_host",
        "snapshot_phase": "final",
        "checks": [
            {
                "check_id": "host-state-record-count",
                "kind": "json_length",
                "json_pointer": "/records",
                "expected": 2,
            },
            {
                "check_id": "record-001-host-write-count",
                "kind": "json_equals",
                "json_pointer": "/records/0/write_count",
                "expected": 1,
                "record_id": "HID-001",
                "scenario": "exact_match",
            },
            {
                "check_id": "record-002-host-write-count",
                "kind": "json_equals",
                "json_pointer": "/records/1/write_count",
                "expected": 1,
                "record_id": "HID-002",
                "scenario": "transient_error",
            },
        ],
    }

    result = verify_host_snapshot.verify_snapshot(
        snapshot,
        oracle,
        snapshot_digest="sha256:" + "a" * 64,
        oracle_digest="sha256:" + "b" * 64,
    )

    assert result["verdict"] == "independently_verified"
    assert result["check_count"] == 3
    assert result["record_binding_gate_count"] == 2
    assert result["fault_gate_count"] == 4

    snapshot["records"][1]["injected_transient_failures"] = 0
    failed = verify_host_snapshot.verify_snapshot(
        snapshot,
        oracle,
        snapshot_digest="sha256:" + "c" * 64,
        oracle_digest="sha256:" + "b" * 64,
    )
    assert failed["verdict"] == "verification_failed"
    assert failed["differences"][0]["check_id"].endswith("-transient-gate")


def test_runner_secret_state_is_private_stable_and_contains_no_model_key(
    tmp_path: Path,
) -> None:
    first = runner._runner_secrets(tmp_path, create=True)
    second = runner._runner_secrets(tmp_path, create=False)

    assert first == second
    assert first["schema_version"] == "1.1"
    assert stat.S_IMODE((tmp_path / "runner-secrets.json").stat().st_mode) == 0o600
    assert "deepseek_api_key" not in first
    assert all(
        len(first[key]) >= 32
        for key in (
            "platform_api_token",
            "platform_envelope_key",
            "collaboration_developer_token",
            "collaboration_verifier_token",
            "formal_hidden_seed_key",
            "collaborative_development_signing_key",
        )
    )
    assert (
        len(
            {
                first["platform_api_token"],
                first["collaboration_developer_token"],
                first["collaboration_verifier_token"],
                first["collaborative_development_signing_key"],
            }
        )
        == 4
    )
    environment = runner._platform_environment(
        tmp_path,
        first,
        port=18100,
        collaboration_policy="manual",
    )
    assert environment["MODEL_EGRESS_ENABLED"] == "false"
    assert environment["LILIES_FORMAL_HIDDEN_SEED_KEY"] == first["formal_hidden_seed_key"]
    assert (
        environment["LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY"]
        == first["collaborative_development_signing_key"]
    )


def test_daemon_environment_projects_the_frozen_task_turn_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LILIES_DEEPSEEK_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("PYTHONPATH", "/must/not/be/inherited")
    environment = runner._daemon_environment(tmp_path, port=18101)

    assert runner._task_max_turns() == 120
    assert environment["LILIES_DEFAULT_MAX_TURNS"] == "120"
    assert environment["LILIES_DATA_DIR"] == str(tmp_path / "lilies-data")
    assert environment["LILIES_WORKSPACE_ROOT"] == str(tmp_path / "lilies-workspaces")
    assert environment["LILIES_WORKFLOW_STUDIO_ENABLED"] == "true"
    assert environment["LILIES_MODEL_EGRESS_ENABLED"] == "false"
    assert "LILIES_DEEPSEEK_API_KEY" not in environment
    assert "PYTHONPATH" not in environment

    with pytest.raises(
        runner.EnterpriseExperimentError,
        match="authorized DEEPSEEK_API_KEY is unavailable",
    ):
        runner._daemon_environment(
            tmp_path,
            port=18101,
            enable_model_egress=True,
        )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "authorized-provider-key")
    paid_environment = runner._daemon_environment(
        tmp_path,
        port=18101,
        enable_model_egress=True,
    )
    assert paid_environment["LILIES_MODEL_EGRESS_ENABLED"] == "true"
    assert paid_environment["LILIES_DEEPSEEK_API_KEY"] == "authorized-provider-key"


def test_standalone_runtime_probe_uses_fixed_isolated_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(arguments: object, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed["arguments"] = tuple(arguments)  # type: ignore[arg-type]
        observed["kwargs"] = kwargs
        payload = {
            "distribution": "lilies-local-agent",
            "version": "0.1.1",
            "distribution_root": str(runner.STANDALONE_LILIES_ROOT / ".venv"),
            "module_file": str(
                runner.STANDALONE_LILIES_ROOT / "src" / "lilies_agent" / "__init__.py"
            ),
        }
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(runner, "_run_bounded_subprocess", fake_run)

    assert runner._verify_standalone_lilies_runtime() == (
        runner.STANDALONE_LILIES_ROOT / ".venv" / "bin" / "python"
    )
    assert observed["arguments"][:3] == (
        str(runner.STANDALONE_LILIES_PYTHON),
        "-I",
        "-c",
    )
    options = observed["kwargs"]
    assert options["cwd"] == runner.STANDALONE_LILIES_ROOT
    assert options["timeout_seconds"] == runner.STANDALONE_PROBE_TIMEOUT_SECONDS
    assert options["max_stdout_bytes"] == runner.STANDALONE_PROBE_MAX_STDOUT_BYTES
    assert options["max_stderr_bytes"] == runner.STANDALONE_SUBPROCESS_MAX_STDERR_BYTES
    assert "PYTHONPATH" not in options["environment"]
    assert "PYTHONHOME" not in options["environment"]


@pytest.mark.parametrize(
    ("identity_update", "failure"),
    (
        ({"distribution": "agent-platform"}, "distribution identity"),
        ({"version": "9.9.9"}, "distribution identity"),
        (
            {"module_file": str(runner.ROOT / "scripts" / "run_v04_13_enterprise_experiment.py")},
            "escaped the fixed sibling",
        ),
    ),
)
def test_standalone_runtime_probe_fails_closed_on_wrong_identity(
    identity_update: dict[str, str],
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "distribution": "lilies-local-agent",
        "version": "0.1.1",
        "distribution_root": str(runner.STANDALONE_LILIES_ROOT / ".venv"),
        "module_file": str(runner.STANDALONE_LILIES_ROOT / "src" / "lilies_agent" / "__init__.py"),
    }
    payload.update(identity_update)
    monkeypatch.setattr(
        runner,
        "_run_bounded_subprocess",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(runner.EnterpriseExperimentError, match=failure):
        runner._verify_standalone_lilies_runtime()


def test_bounded_subprocess_enforces_stdout_and_wall_clock_limits(
    tmp_path: Path,
) -> None:
    environment = runner._standalone_base_environment()
    completed = runner._run_bounded_subprocess(
        (runner.sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        environment=environment,
        timeout_seconds=5,
        max_stdout_bytes=16,
        max_stderr_bytes=16,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"ok\n"
    assert completed.stderr == b""

    with pytest.raises(runner._BoundedSubprocessOutputError):
        runner._run_bounded_subprocess(
            (runner.sys.executable, "-c", "print('x' * 1000)"),
            cwd=tmp_path,
            environment=environment,
            timeout_seconds=5,
            max_stdout_bytes=16,
            max_stderr_bytes=16,
        )

    with pytest.raises(subprocess.TimeoutExpired):
        runner._run_bounded_subprocess(
            (runner.sys.executable, "-c", "import time; time.sleep(5)"),
            cwd=tmp_path,
            environment=environment,
            timeout_seconds=0.1,
            max_stdout_bytes=16,
            max_stderr_bytes=16,
        )


def test_standalone_daemon_command_is_fixed_isolated_installed_module(
    tmp_path: Path,
) -> None:
    assert runner._standalone_daemon_command(
        runner.STANDALONE_LILIES_PYTHON,
        state_root=tmp_path,
        port=18101,
    ) == (
        str(runner.STANDALONE_LILIES_ROOT / ".venv" / "bin" / "python"),
        "-I",
        "-m",
        "lilies_agent.cli",
        "--data-dir",
        str(tmp_path / "lilies-data"),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "18101",
    )


def test_enterprise_runner_source_has_no_legacy_platform_lilies_runtime() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")

    assert "agent_platform.lilies_cli" not in source
    assert "from agent_platform.lilies_client import" not in source
    assert "from agent_platform.lilies_config import" not in source
    assert "from agent_platform.lilies_models import" not in source
    assert 'state_root / "lilies-data" / "lilies.db"' not in source
    assert "lilies_db=" not in source
    assert runner.PLATFORM_BRIDGE_SCOPES == (
        "lilies.session:read",
        "lilies.session:write",
        "lilies.permission:resolve",
        "lilies.credential:write",
        "lilies.observability:read",
    )

    legacy_cli_source = (
        Path(runner.__file__).parents[1]
        / "platform"
        / "backend"
        / "src"
        / "agent_platform"
        / "lilies_cli.py"
    ).read_text(encoding="utf-8")
    assert "raise SystemExit(main())" not in legacy_cli_source
    assert "agent_platform.lilies_cli is retired" in legacy_cli_source


def _runner_observability_receipt(
    *,
    recorded_calls: int = 0,
    unknown_calls: int = 0,
    active_provider_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    captured_at: str = "2026-07-25T01:00:00+00:00",
    activity_revision: int = 10,
) -> dict[str, Any]:
    active_model_turns = 1 if active_provider_calls else 0
    return {
        "schema_version": "1.0",
        "scope": "daemon_global",
        "coverage_complete": True,
        "daemon_fingerprint": "sha256:" + "a" * 64,
        "daemon_instance_id": "e8be0136-9185-41a6-81e8-f7c9a2bfce76",
        "captured_at": captured_at,
        "activity_revision": activity_revision,
        "model_egress_enabled": False,
        "usage": {
            "attempted_calls": recorded_calls + unknown_calls + active_provider_calls,
            "recorded_calls": recorded_calls,
            "unknown_calls": unknown_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
            "ledger_cursor": max(
                20,
                recorded_calls + unknown_calls + active_provider_calls,
            ),
        },
        "runtime": {
            "active_sessions": active_model_turns,
            "active_model_turns": active_model_turns,
            "active_provider_calls": active_provider_calls,
            "active_development_model_calls": 0,
        },
        "startup": {
            "recovery_completed": True,
            "automatic_resume_policy": "explicit_request_only",
            "automatic_model_resume_count": 0,
            "explicit_resume_candidate_count": 0,
            "interrupted_sessions": 0,
            "interrupted_turns": 0,
            "interrupted_development_assignments": 0,
            "reconciliation_required_development_invocations": 0,
        },
    }


def _runner_empty_usage_page() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": [],
        "page": 1,
        "page_size": 100,
        "returned_count": 0,
        "total_items": 0,
        "total_pages": 0,
        "truncated": False,
    }


def test_token_monitor_brackets_platform_authenticated_public_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []
    before = _runner_observability_receipt()
    after = _runner_observability_receipt(
        captured_at="2026-07-25T01:00:01+00:00",
        activity_revision=11,
    )

    def fake_request(
        base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert base_url == "http://127.0.0.1:18100"
        assert kwargs == {"token": "platform-token", "timeout": 2.0}
        paths.append(path)
        if path.endswith("/observability-snapshot"):
            return before if paths.count(path) == 1 else after
        return _runner_empty_usage_page()

    monkeypatch.setattr(runner, "_request_json", fake_request)

    snapshot = runner._standalone_observability_snapshot(
        platform_url="http://127.0.0.1:18100",
        platform_token="platform-token",
        connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
    )

    assert snapshot == {
        "schema_version": "1.0",
        "snapshot_kind": "paired_observability_bracket",
        "before": before,
        "client_acl_usage": {
            **_runner_empty_usage_page(),
            "snapshot_kind": "complete_paginated_merge",
        },
        "after": after,
    }
    assert paths == [
        (
            "/api/v1/local-lilies/connections/"
            "5f86b55a-b84d-4e99-805f-1f225ee0eed2/observability-snapshot"
        ),
        (
            "/api/v1/local-lilies/connections/"
            "5f86b55a-b84d-4e99-805f-1f225ee0eed2/usage"
            "?group_by=session&group_by=stage&group_by=model&page=1&page_size=100"
        ),
        (
            "/api/v1/local-lilies/connections/"
            "5f86b55a-b84d-4e99-805f-1f225ee0eed2/observability-snapshot"
        ),
    ]


def test_token_monitor_merges_more_than_one_complete_public_usage_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        {
            "session_id": f"00000000-0000-0000-0000-{index:012d}",
            "stage": f"stage-{index}",
            "model": "model-a",
            "recorded_calls": 1,
            "unknown_calls": 0,
            "input_tokens": index,
            "output_tokens": 1,
            "total_tokens": index + 1,
            "cost_usd": 0.001,
        }
        for index in range(1, 102)
    ]
    paths: list[str] = []
    global_input = sum(item["input_tokens"] for item in items)
    global_output = sum(item["output_tokens"] for item in items)
    before = _runner_observability_receipt(
        recorded_calls=101,
        input_tokens=global_input,
        output_tokens=global_output,
        cost_usd=0.101,
    )
    after = json.loads(json.dumps(before))
    after["captured_at"] = "2026-07-25T01:00:01+00:00"
    after["activity_revision"] = 11

    def fake_request(
        _base_url: str,
        path: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        paths.append(path)
        if path.endswith("/observability-snapshot"):
            return before if len(paths) == 1 else after
        page = 1 if "&page=1&" in path else 2
        page_items = items[(page - 1) * 100 : page * 100]
        return {
            "schema_version": "1.0",
            "group_by": ["session", "stage", "model"],
            "items": page_items,
            "page": page,
            "page_size": 100,
            "returned_count": len(page_items),
            "total_items": len(items),
            "total_pages": 2,
            "truncated": False,
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)

    snapshot = runner._standalone_observability_snapshot(
        platform_url="http://127.0.0.1:18100",
        platform_token="platform-token",
        connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
    )

    assert snapshot is not None
    assert snapshot["snapshot_kind"] == "paired_observability_bracket"
    usage = snapshot["client_acl_usage"]
    assert usage["returned_count"] == 101
    assert usage["total_items"] == 101
    assert usage["total_pages"] == 2
    assert usage["items"] == items
    assert len(paths) == 4
    assert paths[0].endswith("/observability-snapshot")
    assert "&page=1&page_size=100" in paths[1]
    assert "&page=2&page_size=100" in paths[2]
    assert paths[3].endswith("/observability-snapshot")
    collected = runner.collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_observability_snapshot=snapshot,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )
    assert collected["sources"]["standalone_lilies"]["available"] is True
    assert collected["usage"]["totals"]["model_calls"] == 101
    assert collected["usage"]["totals"]["tokens"] == sum(range(2, 103))


def test_token_monitor_rejects_mid_pagination_usage_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_calls = 0
    receipt = _runner_observability_receipt(
        recorded_calls=102,
        input_tokens=102,
        output_tokens=102,
        cost_usd=0.102,
    )

    def fake_request(
        _base_url: str,
        path: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal usage_calls
        if path.endswith("/observability-snapshot"):
            return receipt
        usage_calls += 1
        total_items = 101 if usage_calls == 1 else 102
        returned_count = 100 if usage_calls == 1 else 2
        items = [
            {
                "session_id": f"00000000-0000-0000-0000-{index:012d}",
                "stage": f"stage-{index}",
                "model": "model-a",
                "recorded_calls": 1,
                "unknown_calls": 0,
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "cost_usd": 0.001,
            }
            for index in range(
                1 if usage_calls == 1 else 101,
                101 if usage_calls == 1 else 103,
            )
        ]
        return {
            "schema_version": "1.0",
            "group_by": ["session", "stage", "model"],
            "items": items,
            "page": usage_calls,
            "page_size": 100,
            "returned_count": returned_count,
            "total_items": total_items,
            "total_pages": 2,
            "truncated": False,
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)

    assert (
        runner._standalone_observability_snapshot(
            platform_url="http://127.0.0.1:18100",
            platform_token="platform-token",
            connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
        )
        is None
    )
    assert usage_calls == 2


@pytest.mark.parametrize(
    "drift",
    ["fingerprint", "instance", "ledger_cursor", "cost_usd"],
)
def test_token_monitor_rejects_observability_bracket_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    before = _runner_observability_receipt()
    after = json.loads(json.dumps(before))
    after["captured_at"] = "2026-07-25T01:00:01+00:00"
    after["activity_revision"] = 11
    if drift == "fingerprint":
        after["daemon_fingerprint"] = "sha256:" + "b" * 64
    elif drift == "instance":
        after["daemon_instance_id"] = "4d512ddd-2521-4dc5-a710-83226eb77021"
    elif drift == "ledger_cursor":
        after["usage"]["ledger_cursor"] += 1
    else:
        after["usage"]["cost_usd"] = 0.1
    observability_calls = 0

    def fake_request(
        _base_url: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, Any]:
        nonlocal observability_calls
        if path.endswith("/observability-snapshot"):
            observability_calls += 1
            return before if observability_calls == 1 else after
        return _runner_empty_usage_page()

    monkeypatch.setattr(runner, "_request_json", fake_request)
    assert (
        runner._standalone_observability_snapshot(
            platform_url="http://127.0.0.1:18100",
            platform_token="platform-token",
            connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
        )
        is None
    )


@pytest.mark.parametrize(
    ("section", "field", "invalid"),
    [
        ("runtime", "active_sessions", True),
        ("runtime", "active_provider_calls", "1"),
        ("runtime", "active_sessions", 9_223_372_036_854_775_808),
        ("runtime", "active_model_turns", 1),
        ("runtime", "active_development_model_calls", 2),
        ("usage", "ledger_cursor", 0),
        ("usage", "attempted_calls", 1),
        ("usage", "total_tokens", 1),
        pytest.param(
            "usage",
            "cost_usd",
            10**10_000,
            id="oversized-cost",
        ),
    ],
)
def test_runner_observability_receipt_rejects_types_hierarchy_and_cursor(
    section: str,
    field: str,
    invalid: object,
) -> None:
    receipt = _runner_observability_receipt(
        recorded_calls=1 if field == "ledger_cursor" else 0
    )
    receipt[section][field] = invalid

    with pytest.raises(
        runner.EnterpriseExperimentError,
        match="observability .* (?:counters|accounting)",
    ):
        runner._standalone_observability_receipt(receipt)


@pytest.mark.parametrize("status", [403, 503])
def test_token_monitor_marks_unavailable_observability_unknown(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise runner.EnterpriseExperimentError(f"observability proxy returned {status}")

    monkeypatch.setattr(runner, "_request_json", unavailable)

    assert (
        runner._standalone_observability_snapshot(
            platform_url="http://127.0.0.1:18100",
            platform_token="platform-token",
            connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
        )
        is None
    )


def test_token_monitor_rejects_acl_usage_above_daemon_global_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _runner_observability_receipt(
        recorded_calls=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.01,
    )
    usage = {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": [
            {
                "session_id": "a1011039-df1c-4ceb-bca8-8ee50bfe50c4",
                "stage": "planning",
                "model": "model-a",
                "recorded_calls": 2,
                "unknown_calls": 0,
                "input_tokens": 2,
                "output_tokens": 1,
                "total_tokens": 3,
                "cost_usd": 0.02,
            }
        ],
        "page": 1,
        "page_size": 100,
        "returned_count": 1,
        "total_items": 1,
        "total_pages": 1,
        "truncated": False,
    }

    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda _base_url, path, **_kwargs: (
            receipt if path.endswith("/observability-snapshot") else usage
        ),
    )

    assert (
        runner._standalone_observability_snapshot(
            platform_url="http://127.0.0.1:18100",
            platform_token="platform-token",
            connection_id="5f86b55a-b84d-4e99-805f-1f225ee0eed2",
        )
        is None
    )


def test_runner_secret_state_migrates_existing_v1_without_rotating_authority(
    tmp_path: Path,
) -> None:
    original = {
        "schema_version": "1.0",
        "task_id": runner.TASK_ID,
        "platform_api_token": "a" * 48,
        "platform_envelope_key": "b" * 48,
        "collaboration_developer_token": "c" * 48,
        "collaboration_verifier_token": "d" * 48,
        "formal_hidden_seed_key": "e" * 48,
    }
    runner._atomic_private_json(tmp_path / "runner-secrets.json", original)

    migrated = runner._runner_secrets(tmp_path, create=False)
    stable = runner._runner_secrets(tmp_path, create=False)

    assert migrated == stable
    assert migrated["schema_version"] == "1.1"
    assert len(migrated["collaborative_development_signing_key"]) >= 32
    assert {key: migrated[key] for key in original if key != "schema_version"} == {
        key: original[key] for key in original if key != "schema_version"
    }
    assert stat.S_IMODE((tmp_path / "runner-secrets.json").stat().st_mode) == 0o600


def test_host_snapshot_verifier_receives_only_a_minimal_fixed_environment() -> None:
    environment = runner._host_snapshot_verifier_environment()

    assert environment == {
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    assert not any(
        marker in key
        for key in environment
        for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
    )


def test_managed_process_termination_cleans_the_entire_isolated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = 12345
        waits = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                assert timeout == 15
                raise subprocess.TimeoutExpired(cmd="boundary", timeout=timeout)
            assert timeout == 10
            return 0

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    runner._terminate(FakeProcess())  # type: ignore[arg-type]

    assert signals == [
        (12345, signal.SIGTERM),
        (12345, signal.SIGKILL),
    ]


def test_active_run_receipt_preserves_exact_resume_identity_without_secrets(
    tmp_path: Path,
) -> None:
    runner._write_active_run(
        tmp_path,
        seed="202",
        collaboration_policy="manual",
        operational_permission_policy="task_local_workspace",
        platform_port=18100,
        daemon_port=18101,
        application={"id": "c44b3387-d780-4986-b3d6-dc112851983b"},
        connection={
            "connection_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
        },
        assignment={
            "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        },
    )

    path = runner._active_run_path(tmp_path, "202")
    active = runner._read_private_json(path)

    assert active["task_id"] == runner.TASK_ID
    assert active["seed"] == "202"
    assert active["collaboration_policy"] == "manual"
    assert active["operational_permission_policy"] == "task_local_workspace"
    assert active["assignment_id"] == "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any("token" in key or "secret" in key for key in active)


def test_host_secret_projection_uses_scoped_builder_and_verifier_tokens(
    tmp_path: Path,
) -> None:
    environment_root = tmp_path / "environment"
    runner._atomic_private_json(
        environment_root / "secrets.json",
        {
            "schema_version": "1.0",
            "task_id": runner.TASK_ID,
            "attestation_secret": "a" * 48,
        },
    )
    runner._atomic_private_json(
        environment_root / "credentials.json",
        {
            "paperless_builder_token": "pb" * 16,
            "inventree_builder_token": "ib" * 16,
            "paperless_verifier_token": "pv" * 16,
            "inventree_verifier_token": "iv" * 16,
        },
    )

    projected = runner._host_secrets(tmp_path)

    assert projected == {
        "exp-lilies-001-environment-attestation": "a" * 48,
        "exp-lilies-001-paperless-builder-token": "pb" * 16,
        "exp-lilies-001-inventree-builder-token": "ib" * 16,
        "exp-lilies-001-paperless-verifier-token": "pv" * 16,
        "exp-lilies-001-inventree-verifier-token": "iv" * 16,
    }


def test_pairing_requests_the_exact_platform_bridge_scope_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = arguments
        captured["subprocess"] = kwargs
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=json.dumps(
                {
                    "allowed_scopes": sorted(runner.PLATFORM_BRIDGE_SCOPES),
                    "daemon_fingerprint": "sha256:" + "a" * 64,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    "pairing_code": "ABCD-EFGH-2345-6789",
                }
            ).encode(),
            stderr=b"",
        )

    def fake_request(
        _base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["payload"] = kwargs["value"]
        return {
            "connections": [
                {
                    "connection_id": "bd9f0b8f-e2c8-42a6-a019-a4ebc64acbbc",
                    "base_url": "http://127.0.0.1:18101",
                    "status": "connected",
                }
            ]
        }

    monkeypatch.setattr(runner, "_run_bounded_subprocess", fake_run)
    monkeypatch.setattr(runner, "_request_json", fake_request)
    daemon_environment = runner._daemon_environment(tmp_path, port=18101)
    daemon_environment["LILIES_DEEPSEEK_API_KEY"] = "must-not-reach-pair-cli"

    result = runner._pair_daemon(
        state_root=tmp_path,
        daemon_port=18101,
        platform_url="http://127.0.0.1:18100",
        platform_token="t" * 48,
        standalone_python=runner.STANDALONE_LILIES_PYTHON,
        daemon_environment=daemon_environment,
    )

    assert captured["command"] == [
        str(runner.STANDALONE_LILIES_PYTHON),
        "-I",
        "-m",
        "lilies_agent.cli",
        "--data-dir",
        str(tmp_path / "lilies-data"),
        "pair",
        "--scope",
        "lilies.session:read",
        "--scope",
        "lilies.session:write",
        "--scope",
        "lilies.permission:resolve",
        "--scope",
        "lilies.credential:write",
    ]
    subprocess_options = captured["subprocess"]
    assert subprocess_options["cwd"] == runner.STANDALONE_LILIES_ROOT
    assert subprocess_options["timeout_seconds"] == runner.STANDALONE_PAIR_TIMEOUT_SECONDS
    assert subprocess_options["max_stdout_bytes"] == runner.STANDALONE_PAIR_MAX_STDOUT_BYTES
    assert subprocess_options["max_stderr_bytes"] == runner.STANDALONE_SUBPROCESS_MAX_STDERR_BYTES
    assert subprocess_options["environment"]["LILIES_MODEL_EGRESS_ENABLED"] == "false"
    assert not any(
        marker in name
        for name in subprocess_options["environment"]
        for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    )
    assert captured["path"] == "/api/v1/local-lilies/connections"
    assert captured["payload"] == {
        "idempotency_key": "exp-lilies-001.pair.18101",
        "base_url": "http://127.0.0.1:18101",
        "pairing_code": "ABCD-EFGH-2345-6789",
        "expected_daemon_fingerprint": "sha256:" + "a" * 64,
    }
    assert result["status"] == "connected"


@pytest.mark.parametrize(
    "stdout",
    (
        b"x" * (runner.STANDALONE_PAIR_MAX_STDOUT_BYTES + 1),
        b"{}",
        json.dumps(
            {
                "allowed_scopes": sorted(runner.PLATFORM_BRIDGE_SCOPES),
                "daemon_fingerprint": "not-a-fingerprint",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "pairing_code": "ABCD-EFGH-2345-6789",
            }
        ).encode(),
    ),
)
def test_pairing_subprocess_response_fails_closed_before_platform_exchange(
    stdout: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "_run_bounded_subprocess",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=stdout,
            stderr=b"",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda *args, **kwargs: pytest.fail("invalid pairing reached the platform"),
    )

    with pytest.raises(runner.EnterpriseExperimentError):
        runner._pair_daemon(
            state_root=tmp_path,
            daemon_port=18101,
            platform_url="http://127.0.0.1:18100",
            platform_token="t" * 48,
            standalone_python=runner.STANDALONE_LILIES_PYTHON,
            daemon_environment=runner._daemon_environment(tmp_path, port=18101),
        )


def test_pairing_subprocess_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout_seconds"],
        )

    monkeypatch.setattr(runner, "_run_bounded_subprocess", timeout)
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda *args, **kwargs: pytest.fail("timed-out pairing reached the platform"),
    )

    with pytest.raises(runner.EnterpriseExperimentError, match="timed out"):
        runner._pair_daemon(
            state_root=tmp_path,
            daemon_port=18101,
            platform_url="http://127.0.0.1:18100",
            platform_token="t" * 48,
            standalone_python=runner.STANDALONE_LILIES_PYTHON,
            daemon_environment=runner._daemon_environment(tmp_path, port=18101),
        )


def test_auto_forward_replay_accepts_an_already_confirmed_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_request(
        _base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((path, kwargs))
        return {
            "channels": [
                {
                    "assignment_id": "assignment-1",
                    "channel_id": "channel-1",
                    "revision": 4,
                    "approval_mode": "auto_forward",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._set_auto_forward(
        "http://127.0.0.1:18100",
        "t" * 48,
        assignment_id="assignment-1",
    )

    assert result["channel_id"] == "channel-1"
    assert len(calls) == 1
    assert calls[0][0].startswith("/api/v1/studio/collaboration/channels?")


def test_runner_projects_platform_verification_without_hidden_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_request(
        _base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed["path"] = path
        observed.update(kwargs)
        return {
            "claim_id": "claim-1",
            "claim_status": "verification_failed",
            "verification": {
                "verification_id": "verification-1",
                "verdict": "verification_failed",
                "oracle_digest": "sha256:" + "a" * 64,
                "differences": [
                    {
                        "check_id": "hidden-check",
                        "expected": "hidden",
                        "actual": "wrong",
                    }
                ],
            },
            "stable_progress": {
                "stable_hidden_runs": 3,
                "consecutive_passes": 0,
                "progress_digest": "sha256:" + "b" * 64,
                "stable_verdict": None,
            },
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._run_platform_independent_verification(
        "http://127.0.0.1:18100",
        "t" * 48,
        assignment_id="assignment-1",
    )

    assert observed["path"].endswith("/assignments/assignment-1/independent-verification")
    assert observed["method"] == "POST"
    assert result["claim_status"] == "verification_failed"
    assert result["difference_count"] == 1
    assert result["stable_hidden_runs"] == 3
    assert result["consecutive_passes"] == 0
    assert "differences" not in result


def test_missing_model_authority_records_not_run_without_touching_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("environment must not be touched without model authority")

    monkeypatch.setattr(runner, "_environment_command", forbidden)
    evidence_root = tmp_path / "evidence"
    result = runner.run_seed(
        Namespace(
            state_root=tmp_path / "state",
            evidence_root=evidence_root,
            seed="101",
            platform_port=18100,
            daemon_port=18101,
            collaboration_policy="manual",
            deadline_seconds=10_800,
        )
    )

    evidence = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert result == 2
    assert evidence["status"] == "run_failed"
    assert evidence["assignment"] is None
    assert evidence["attempt_id"].startswith("sha256:")
    assert evidence["previous_attempt_id"] is None
    assert len(list((evidence_root / "attempts" / "seed-101").glob("*.json"))) == 1
    assert "DEEPSEEK_API_KEY is required" in evidence["error"]
    assert "secret" not in json.dumps(evidence["secret_receipts"])


def test_run_evidence_preserves_every_failed_attempt_and_updates_latest(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    common = {
        "seed": "101",
        "package": None,
        "application": None,
        "connection": None,
        "assignment": None,
        "secret_receipts": (),
        "host_snapshots": (),
        "platform_verification": None,
        "host_verification": None,
    }

    runner._write_run_evidence(
        evidence_root,
        started_at="2026-07-25T00:00:00+00:00",
        status="run_failed",
        error="first failure",
        **common,
    )
    first = json.loads((evidence_root / "seed-101.json").read_bytes())
    runner._write_run_evidence(
        evidence_root,
        started_at="2026-07-25T00:01:00+00:00",
        status="enterprise_run_passed",
        error=None,
        **common,
    )
    latest = json.loads((evidence_root / "seed-101.json").read_bytes())
    attempts = sorted((evidence_root / "attempts" / "seed-101").glob("*.json"))

    assert len(attempts) == 2
    assert {json.loads(path.read_bytes())["status"] for path in attempts} == {
        "run_failed",
        "enterprise_run_passed",
    }
    assert latest["status"] == "enterprise_run_passed"
    assert latest["previous_attempt_id"] == first["attempt_id"]


def test_run_evidence_preserves_previous_revision_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "evidence"
    common = {
        "seed": "101",
        "package": None,
        "application": None,
        "connection": None,
        "assignment": None,
        "secret_receipts": (),
        "host_snapshots": (),
        "platform_verification": None,
        "host_verification": None,
    }
    monkeypatch.setattr(runner, "REVISION", 3)
    runner._write_run_evidence(
        evidence_root,
        started_at="2026-07-25T00:00:00+00:00",
        status="run_failed",
        error="revision three failure",
        **common,
    )
    first = json.loads((evidence_root / "seed-101.json").read_bytes())

    monkeypatch.setattr(runner, "REVISION", 4)
    runner._write_run_evidence(
        evidence_root,
        started_at="2026-07-25T00:01:00+00:00",
        status="run_failed",
        error="revision four failure",
        **common,
    )
    latest = json.loads((evidence_root / "seed-101.json").read_bytes())

    assert first["revision"] == 3
    assert latest["revision"] == 4
    assert latest["previous_attempt_id"] == first["attempt_id"]
    assert len(list((evidence_root / "attempts" / "seed-101").glob("*.json"))) == 2


def test_runner_prepare_freezes_the_real_revision_without_starting_hosts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = runner.prepare(Namespace(state_root=tmp_path))
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["status"] == "prepared_environment_not_started"
    assert output["task_id"] == runner.TASK_ID
    assert output["package_public_summary_digest"].startswith("sha256:")
    assert output["package_sealed_digest"].startswith("sha256:")
    assert not (tmp_path / "environment").exists()


def test_poll_treats_ready_builder_without_completion_claim_as_incomplete_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = {
        "assignment_id": "assignment-12345678",
        "phase": "running",
        "status": "ready",
        "daemon_status": "ready",
    }
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_request(url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        requests.append((path, kwargs))
        if path.endswith("/relay"):
            return {"assignment": assignment}
        return dict(assignment)

    monkeypatch.setattr(runner, "_request_json", fake_request)
    monkeypatch.setattr(
        runner.time,
        "sleep",
        lambda _seconds: pytest.fail("ready Builder terminal must not sleep"),
    )

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment["assignment_id"],
        deadline_seconds=60,
    )

    assert result == {
        **assignment,
        "runner_terminal": "builder_ready_without_completion_claim",
        "runner_auto_permissions": [],
    }
    assert [path for path, _ in requests] == [
        "/api/v1/local-lilies/assignments/assignment-12345678/relay",
        "/api/v1/local-lilies/assignments/assignment-12345678",
    ]
    assert runner._safe_assignment_projection(result)["runner_terminal"] == (
        "builder_ready_without_completion_claim"
    )


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("work/model-config.json", True),
        ("artifacts/reconciliation.xlsx", True),
        ("work/nested/output.json", True),
        ("work/../protected/oracle.json", False),
        ("work/.git/config", False),
        ("protected/oracle.json", False),
        ("/tmp/output.json", False),
        ("work\\output.json", False),
        ("work//output.json", False),
    ],
)
def test_unattended_workspace_permission_path_is_frozen_and_canonical(
    path: str,
    accepted: bool,
) -> None:
    if accepted:
        assert runner._task_local_workspace_path(path) == path
    else:
        with pytest.raises(runner.EnterpriseExperimentError):
            runner._task_local_workspace_path(path)


def test_task_local_permission_idempotency_key_is_bounded_and_fully_bound() -> None:
    bindings: dict[str, Any] = {
        "task_id": runner.TASK_ID,
        "task_revision": runner.REVISION,
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "session_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
        "request_id": "c44b3387-d780-4986-b3d6-dc112851983b",
        "input_digest": "sha256:" + "a" * 64,
    }

    key = runner._task_local_permission_idempotency_key(**bindings)

    assert key == runner._task_local_permission_idempotency_key(**bindings)
    assert 16 <= len(key) <= 128
    PermissionDecisionRequest.model_validate(
        {
            "idempotency_key": key,
            "behavior": "allow",
            "expected_input_digest": bindings["input_digest"],
            "message": "Exact unattended task-local workspace decision.",
        }
    )
    mutations = {
        "task_id": "EXP-LILIES-999",
        "task_revision": int(bindings["task_revision"]) + 1,
        "assignment_id": "c0bc886a-e86c-46e9-98b7-ee2be72d88ca",
        "session_id": "dcff7107-ceec-4f84-a8c9-f5a022c49933",
        "request_id": "b5af9203-a51c-44b4-b284-a2453529948d",
        "input_digest": "sha256:" + "b" * 64,
    }
    changed_keys = {
        runner._task_local_permission_idempotency_key(**{**bindings, field: value})
        for field, value in mutations.items()
    }
    assert len(changed_keys) == len(mutations)
    assert key not in changed_keys


def test_poll_allows_one_exact_task_local_workspace_permission_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    session_id = "e88af94b-2a0e-42fe-80c4-0a61ed105617"
    request_id = "c44b3387-d780-4986-b3d6-dc112851983b"
    input_digest = "sha256:" + "a" * 64
    waiting = {
        "assignment_id": assignment_id,
        "session_id": session_id,
        "phase": "waiting",
        "status": "waiting",
        "daemon_status": "waiting_permission",
    }
    completed = {
        **waiting,
        "phase": "completed",
        "status": "completed",
        "daemon_status": "completed",
    }
    assignment_reads = iter((waiting, completed))
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_request(_url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        requests.append((path, kwargs))
        if path.endswith("/relay"):
            return {}
        if path == f"/api/v1/local-lilies/assignments/{assignment_id}":
            return dict(next(assignment_reads))
        if path.startswith("/api/v1/studio/collaboration/channels?"):
            return {
                "channels": [
                    {
                        "assignment_id": assignment_id,
                        "channel_id": "channel-12345678",
                    }
                ]
            }
        if path.endswith("/channel-12345678"):
            return {
                "context": {
                    "assignment": {
                        "task_id": runner.TASK_ID,
                        "task_revision": runner.REVISION,
                        "assignment_id": assignment_id,
                        "session_id": session_id,
                        "daemon_status": "waiting_permission",
                    },
                    "observable_events": [
                        {
                            "seq": 9,
                            "permission_request": {
                                "request_id": request_id,
                                "tool_name": "workspace_write",
                                "input_digest": input_digest,
                                "redacted_input": {
                                    "path": "work/model-config.json",
                                },
                                "status": "pending",
                            },
                        }
                    ],
                }
            }
        if path.endswith(f"/permissions/{request_id}"):
            assert kwargs["method"] == "POST"
            assert kwargs["value"]["behavior"] == "allow"
            assert kwargs["value"]["expected_input_digest"] == input_digest
            expected_key = runner._task_local_permission_idempotency_key(
                task_id=runner.TASK_ID,
                task_revision=runner.REVISION,
                assignment_id=assignment_id,
                session_id=session_id,
                request_id=request_id,
                input_digest=input_digest,
            )
            assert kwargs["value"]["idempotency_key"] == expected_key
            PermissionDecisionRequest.model_validate(kwargs["value"])
            assert "updated_input" not in kwargs["value"]
            return {
                "permission": {
                    "request_id": request_id,
                    "status": "allowed",
                    "input_digest": input_digest,
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(runner, "_request_json", fake_request)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment_id,
        deadline_seconds=60,
        operational_permission_policy="task_local_workspace",
    )

    assert result["phase"] == "completed"
    assert result["runner_auto_permissions"] == [
        {
            "request_id": request_id,
            "tool_name": "workspace_write",
            "input_digest": input_digest,
            "path": "work/model-config.json",
            "status": "allowed",
        }
    ]


def test_poll_synchronizes_permission_event_after_waiting_status_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    session_id = "e88af94b-2a0e-42fe-80c4-0a61ed105617"
    request_id = "c44b3387-d780-4986-b3d6-dc112851983b"
    input_digest = "sha256:" + "a" * 64
    relay_count = 0
    assignment_reads = iter(
        (
            {
                "assignment_id": assignment_id,
                "session_id": session_id,
                "phase": "waiting",
                "status": "waiting",
                "daemon_status": "waiting_permission",
                "relay_cursor": 472,
            },
            {
                "assignment_id": assignment_id,
                "session_id": session_id,
                "phase": "completed",
                "status": "completed",
                "daemon_status": "completed",
                "relay_cursor": 481,
            },
        )
    )

    def fake_request(_url: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal relay_count
        if path.endswith("/relay"):
            relay_count += 1
            return {"relay_cursor": 472 if relay_count == 1 else 478}
        if path == f"/api/v1/local-lilies/assignments/{assignment_id}":
            return dict(next(assignment_reads))
        if path.startswith("/api/v1/studio/collaboration/channels?"):
            return {
                "channels": [
                    {
                        "assignment_id": assignment_id,
                        "channel_id": "channel-12345678",
                    }
                ]
            }
        if path.endswith("/channel-12345678"):
            assert relay_count >= 2
            return {
                "context": {
                    "assignment": {
                        "task_id": runner.TASK_ID,
                        "task_revision": runner.REVISION,
                        "assignment_id": assignment_id,
                        "session_id": session_id,
                        "daemon_status": "waiting_permission",
                    },
                    "observable_events": [
                        {
                            "seq": 478,
                            "permission_request": {
                                "request_id": request_id,
                                "tool_name": "workspace_write",
                                "input_digest": input_digest,
                                "redacted_input": {"path": "work/cbc.json"},
                                "status": "pending",
                            },
                        }
                    ],
                }
            }
        if path.endswith(f"/permissions/{request_id}"):
            return {
                "permission": {
                    "request_id": request_id,
                    "status": "allowed",
                    "input_digest": input_digest,
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(runner, "_request_json", fake_request)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment_id,
        deadline_seconds=60,
        operational_permission_policy="task_local_workspace",
    )

    assert relay_count == 3
    assert result["phase"] == "completed"
    assert result["runner_auto_permissions"] == [
        {
            "request_id": request_id,
            "tool_name": "workspace_write",
            "input_digest": input_digest,
            "path": "work/cbc.json",
            "status": "allowed",
        }
    ]


def test_poll_rejects_security_violation_during_post_wait_permission_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    relay_count = 0

    def fake_request(_url: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal relay_count
        if path.endswith("/relay"):
            relay_count += 1
            if relay_count == 2:
                raise runner.EnterpriseExperimentError(
                    "platform request failed: security_boundary_violation: "
                    "daemon relay event contained a plaintext credential"
                )
            return {"relay_cursor": 472}
        if path == f"/api/v1/local-lilies/assignments/{assignment_id}":
            return {
                "assignment_id": assignment_id,
                "session_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
                "phase": "waiting",
                "status": "waiting",
                "daemon_status": "waiting_permission",
                "relay_cursor": 472,
            }
        raise AssertionError(f"security rejection must precede Studio read: {path}")

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment_id,
        deadline_seconds=60,
        operational_permission_policy="task_local_workspace",
    )

    assert relay_count == 2
    assert result["runner_terminal"] == "relay_security_boundary_rejected"
    assert result["runner_auto_permissions"] == []
    assert "security_boundary_violation" in result["runner_terminal_detail"]


def test_unattended_permission_rejects_non_workspace_tool_without_deciding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    session_id = "e88af94b-2a0e-42fe-80c4-0a61ed105617"
    request_id = "c44b3387-d780-4986-b3d6-dc112851983b"
    input_digest = "sha256:" + "a" * 64

    def fake_request(_url: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        if path.endswith("/relay"):
            return {}
        if path == f"/api/v1/local-lilies/assignments/{assignment_id}":
            return {
                "assignment_id": assignment_id,
                "session_id": session_id,
                "phase": "waiting",
                "status": "waiting",
                "daemon_status": "waiting_permission",
            }
        if path.startswith("/api/v1/studio/collaboration/channels?"):
            return {
                "channels": [
                    {
                        "assignment_id": assignment_id,
                        "channel_id": "channel-12345678",
                    }
                ]
            }
        if path.endswith("/channel-12345678"):
            return {
                "context": {
                    "assignment": {
                        "task_id": runner.TASK_ID,
                        "task_revision": runner.REVISION,
                        "assignment_id": assignment_id,
                        "session_id": session_id,
                        "daemon_status": "waiting_permission",
                    },
                    "observable_events": [
                        {
                            "seq": 9,
                            "permission_request": {
                                "request_id": request_id,
                                "tool_name": "connector.execute",
                                "input_digest": input_digest,
                                "redacted_input": {"action": "external-write"},
                                "status": "pending",
                            },
                        }
                    ],
                }
            }
        raise AssertionError(f"unexpected decision request: {path}")

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment_id,
        deadline_seconds=60,
        operational_permission_policy="task_local_workspace",
    )

    assert result["runner_terminal"] == "unattended_permission_rejected"
    assert result["runner_auto_permissions"] == []
    assert "outside the task-local workspace policy" in result["runner_terminal_detail"]


def test_poll_terminates_a_durable_relay_security_boundary_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = {
        "assignment_id": "assignment-12345678",
        "phase": "running",
        "status": "running",
        "daemon_status": "running",
        "relay_cursor": 706,
    }

    def fake_request(_url: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        if path.endswith("/relay"):
            raise runner.EnterpriseExperimentError(
                "platform request failed: security_boundary_violation: "
                "daemon relay event contained a plaintext credential"
            )
        return dict(assignment)

    monkeypatch.setattr(runner, "_request_json", fake_request)
    monkeypatch.setattr(
        runner.time,
        "sleep",
        lambda _seconds: pytest.fail("durable relay rejection must not sleep"),
    )

    result = runner._poll_assignment(
        "http://platform.invalid",
        "platform-token",
        assignment_id=assignment["assignment_id"],
        deadline_seconds=60,
        operational_permission_policy="task_local_workspace",
    )

    assert result["runner_terminal"] == "relay_security_boundary_rejected"
    assert result["runner_auto_permissions"] == []
    assert "security_boundary_violation" in result["runner_terminal_detail"]


def test_runner_rejects_reusing_state_after_frozen_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    shutil.copytree(runner.TASK_ROOT, source)
    monkeypatch.setattr(runner, "TASK_ROOT", source)
    state = tmp_path / "platform"
    runner._freeze_package(state)
    requirement = source / "requirement.md"
    requirement.chmod(0o600)
    requirement.write_text(
        requirement.read_text(encoding="utf-8") + "\nunauthorized drift\n",
        encoding="utf-8",
    )

    with pytest.raises(
        runner.EnterpriseExperimentError,
        match=(f"another EXP-LILIES-001 revision-{runner.REVISION} payload"),
    ):
        runner._freeze_package(state)
