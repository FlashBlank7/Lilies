from __future__ import annotations

import json
import signal
import shutil
import stat
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from agent_platform.lilies_models import LocalScope
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

    assert requests[0][0].endswith("/api/token/")
    assert requests[0][1]["method"] == "POST"
    assert requests[1][0].endswith(
        "/api/user/me/token/?name=EXP-LILIES-001-task-author"
    )
    assert requests[1][1]["basic_auth"][0] == "exp_lilies_admin"
    assert "value" not in requests[1][1]
    assert observed_settings == [
        (
            "http://127.0.0.1:18001/api/settings/global/"
            "PURCHASEORDER_REFERENCE_PATTERN/",
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
        runner.ROOT
        / "scripts"
        / "experiments"
        / "exp_lilies_001"
        / "provision_scoped_account.py"
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
    supplier_parts = [
        call for call in calls if call[0].endswith("/api/company/part/")
    ]
    po_lines = [
        call for call in calls if call[0].endswith("/api/order/po-line/")
    ]

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
    assert len(
        {
            first["platform_api_token"],
            first["collaboration_developer_token"],
            first["collaboration_verifier_token"],
            first["collaborative_development_signing_key"],
        }
    ) == 4
    environment = runner._platform_environment(
        tmp_path,
        first,
        port=18100,
        collaboration_policy="manual",
    )
    assert (
        environment["LILIES_FORMAL_HIDDEN_SEED_KEY"]
        == first["formal_hidden_seed_key"]
    )
    assert (
        environment["LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY"]
        == first["collaborative_development_signing_key"]
    )


def test_daemon_environment_projects_the_frozen_task_turn_budget(
    tmp_path: Path,
) -> None:
    environment = runner._daemon_environment(tmp_path, port=18101)

    assert runner._task_max_turns() == 120
    assert environment["LILIES_DEFAULT_MAX_TURNS"] == "120"
    assert environment["LILIES_DATA_DIR"] == str(tmp_path / "lilies-data")
    assert environment["LILIES_WORKSPACE_ROOT"] == str(
        tmp_path / "lilies-workspaces"
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
    assert {
        key: migrated[key]
        for key in original
        if key != "schema_version"
    } == {
        key: original[key]
        for key in original
        if key != "schema_version"
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

    class FakeClient:
        def __init__(self, _settings: object) -> None:
            return

        def create_pairing_code(self, scopes: object) -> dict[str, Any]:
            captured["scopes"] = tuple(scopes)
            return {
                "pairing_code": "pairing-code-0000000000000001",
                "daemon_fingerprint": "sha256:" + "a" * 64,
            }

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

    monkeypatch.setattr(runner, "LiliesClient", FakeClient)
    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._pair_daemon(
        state_root=tmp_path,
        daemon_port=18101,
        platform_url="http://127.0.0.1:18100",
        platform_token="t" * 48,
    )

    assert captured["scopes"] == (
        LocalScope.session_read.value,
        LocalScope.session_write.value,
        LocalScope.permission_resolve.value,
        LocalScope.credential_write.value,
    )
    assert captured["path"] == "/api/v1/local-lilies/connections"
    assert captured["payload"]["pairing_code"] == "pairing-code-0000000000000001"
    assert result["status"] == "connected"


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

    assert observed["path"].endswith(
        "/assignments/assignment-1/independent-verification"
    )
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
    attempts = sorted(
        (evidence_root / "attempts" / "seed-101").glob("*.json")
    )

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
    assert "outside the task-local workspace policy" in result[
        "runner_terminal_detail"
    ]


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
        match=(
            "another EXP-LILIES-001 "
            f"revision-{runner.REVISION} payload"
        ),
    ):
        runner._freeze_package(state)
