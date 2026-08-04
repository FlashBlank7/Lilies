from __future__ import annotations

import json
import inspect
import os
import signal
import shutil
import stat
import subprocess
from argparse import Namespace
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_platform.lilies_models import PermissionDecisionRequest
from scripts.experiments.exp_lilies_001 import attestation_server
from scripts.experiments.exp_lilies_001 import environment_control
from scripts.experiments.exp_lilies_001 import verify_host_snapshot
from scripts import run_v04_13_enterprise_experiment as runner


def _test_provider_identity(provider: str = "deepseek") -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": "v0.4.13-t01h-provider-identity-1",
        "provider": provider,
        "model": (runner.DEFAULT_DEEPSEEK_MODEL if provider == "deepseek" else "qwen3:8b"),
        "base_url": (
            runner.DEFAULT_DEEPSEEK_BASE_URL if provider == "deepseek" else "http://127.0.0.1:11434"
        ),
        "max_output_tokens": 16_384,
        "credential_class": (
            "paid_process_environment" if provider == "deepseek" else "credential_free_loopback"
        ),
        "managed_local_process": provider == "ollama-local",
    }
    if provider == "ollama-local":
        unsigned.update(
            model_manifest_digest="sha256:" + "1" * 64,
            template_digest="sha256:" + "2" * 64,
            context_window_tokens=32_768,
            ollama_executable_name="ollama",
            ollama_executable_digest="sha256:" + "3" * 64,
            ollama_models_directory_identity_digest="sha256:" + "4" * 64,
        )
    return {
        **unsigned,
        "receipt_digest": runner._digest(runner._canonical_json(unsigned)),
    }


def _test_provider_configuration(provider: str = "deepseek") -> dict[str, Any]:
    return {
        "identity": _test_provider_identity(provider),
        "ollama_executable": None,
        "ollama_models_directory": None,
    }


def _ollama_launch_args(
    tmp_path: Path,
    *,
    models_directory: Path | None = None,
) -> Namespace:
    executable = tmp_path / "bin" / "ollama"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"test-ollama-binary")
    executable.chmod(0o755)
    if models_directory is None:
        models_directory = tmp_path / "ollama-store" / "models"
        models_directory.mkdir(parents=True, exist_ok=True)
    return Namespace(
        enable_model_egress=True,
        model_provider="ollama-local",
        model="qwen3:8b",
        provider_max_output_tokens=4_096,
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model_manifest_digest="sha256:" + "1" * 64,
        ollama_template_digest="sha256:" + "2" * 64,
        ollama_context_window_tokens=32_768,
        ollama_binary=executable,
        ollama_models_dir=models_directory,
    )


def _install_managed_ollama_test_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wait_json: Any,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def unavailable_port(*_args: object, **_kwargs: object) -> None:
        raise OSError("unused test port")

    class Process:
        pid = 4321

        @staticmethod
        def poll() -> None:
            return None

    def fake_managed_process(
        _stack: ExitStack,
        arguments: tuple[str, ...],
        *,
        environment: dict[str, str],
        log_path: Path,
    ) -> Process:
        captured.update(
            arguments=arguments,
            environment=dict(environment),
            log_path=log_path,
        )
        return Process()

    monkeypatch.setattr(runner.socket, "create_connection", unavailable_port)
    monkeypatch.setattr(runner, "_managed_process", fake_managed_process)
    monkeypatch.setattr(runner, "_wait_json", wait_json)
    return captured


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


def _environment_owner_for(state_root: Path) -> dict[str, Any]:
    return {
        "Name": environment_control.ENVIRONMENT_OWNER_VOLUME,
        "Labels": environment_control._environment_owner_labels(
            state_root,
            legacy_adopted=True,
        ),
    }


def test_environment_release_rejects_the_wrong_state_root_before_resource_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_root = tmp_path / "owner"
    other_root = tmp_path / "other"
    environment_control._secret_state(owner_root, create=True)
    environment_control._secret_state(other_root, create=True)
    owner_volume = _environment_owner_for(owner_root)
    resource_checks: list[str] = []
    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        lambda: owner_volume,
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_containers",
        lambda: resource_checks.append("containers") or [],
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_volumes",
        lambda: resource_checks.append("volumes") or [],
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="owned by another state root",
    ):
        environment_control._release_environment_owner(other_root)

    assert resource_checks == []


@pytest.mark.parametrize(
    ("containers", "project_volumes", "message"),
    [
        ([{"Id": "stopped-container"}], [], "1 container\\(s\\)"),
        ([], ["retained-data"], "1 data volume\\(s\\)"),
    ],
)
def test_environment_release_fails_closed_while_compose_resources_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    containers: list[dict[str, Any]],
    project_volumes: list[str],
    message: str,
) -> None:
    environment_control._secret_state(tmp_path, create=True)
    owner_volume = _environment_owner_for(tmp_path)
    delete_calls: list[list[str]] = []
    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        lambda: owner_volume,
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_containers",
        lambda: containers,
    )
    monkeypatch.setattr(
        environment_control,
        "_compose_project_volumes",
        lambda: project_volumes,
    )
    monkeypatch.setattr(
        environment_control,
        "_docker_output",
        lambda arguments: delete_calls.append(list(arguments)) or "",
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match=message,
    ):
        environment_control._release_environment_owner(tmp_path)

    assert delete_calls == []


def test_environment_release_deletes_only_owner_registry_and_verifies_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_control._secret_state(tmp_path, create=True)
    owner_volumes = iter([_environment_owner_for(tmp_path), None])
    delete_calls: list[list[str]] = []
    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        lambda: next(owner_volumes),
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
        lambda arguments: delete_calls.append(list(arguments)) or "",
    )

    environment_control._release_environment_owner(tmp_path)

    assert delete_calls == [["volume", "rm", environment_control.ENVIRONMENT_OWNER_VOLUME]]


def test_environment_release_fails_when_registry_deletion_is_not_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_control._secret_state(tmp_path, create=True)
    owner_volume = _environment_owner_for(tmp_path)
    delete_calls: list[list[str]] = []
    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        lambda: owner_volume,
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
        lambda arguments: delete_calls.append(list(arguments)) or "",
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="deletion could not be verified",
    ):
        environment_control._release_environment_owner(tmp_path)

    assert delete_calls == [["volume", "rm", environment_control.ENVIRONMENT_OWNER_VOLUME]]


def test_environment_release_missing_owner_has_explicit_non_idempotent_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_control._secret_state(tmp_path, create=True)
    monkeypatch.setattr(
        environment_control,
        "_environment_owner_volume",
        lambda: None,
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="ownership cannot be verified and nothing was released",
    ):
        environment_control._release_environment_owner(tmp_path)


def test_environment_release_requires_exact_task_confirmation_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "not-created"
    release_calls: list[Path] = []
    monkeypatch.setattr(
        environment_control,
        "_release_environment_owner",
        lambda root: release_calls.append(root),
    )
    monkeypatch.setattr(
        environment_control.sys,
        "argv",
        [
            "environment_control.py",
            "--state-root",
            str(state_root),
            "release",
            "--confirm-task-id",
            "EXP-LILIES-OTHER",
        ],
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="release requires --confirm-task-id EXP-LILIES-001",
    ):
        environment_control.main()

    assert release_calls == []
    assert not state_root.exists()


def test_environment_release_dispatches_with_exact_confirmation_without_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "not-created"
    release_calls: list[Path] = []
    monkeypatch.setattr(
        environment_control,
        "_release_environment_owner",
        lambda root: release_calls.append(root),
    )
    monkeypatch.setattr(
        environment_control,
        "_claim_environment_owner",
        lambda root: pytest.fail(f"unexpected owner claim for {root}"),
    )
    monkeypatch.setattr(
        environment_control.sys,
        "argv",
        [
            "environment_control.py",
            "--state-root",
            str(state_root),
            "release",
            "--confirm-task-id",
            environment_control.TASK_ID,
        ],
    )

    assert environment_control.main() == 0
    assert release_calls == [state_root.resolve()]
    assert not state_root.exists()


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
    assert '("common", "delete_attachment")' in source
    assert '("common", "add_attachment")' not in source
    assert '("common", "change_attachment")' not in source


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


def _synthetic_workflow_input_record() -> dict[str, Any]:
    return {
        "record_id": "SYN-001",
        "source_id": "SYN-DOC-001",
        "supplier": "SYNTHETIC SUPPLIER",
        "purchase_order": None,
        "part_number": "SYN-PART",
        "lot_number": "SYN-LOT",
        "quantity": 7,
        "document_date": "2026-01-02",
        "certificate_type": "SYNTHETIC CERTIFICATE",
        "ocr_confidence": 0.73,
        "scenario": "synthetic-extra-must-not-leak",
        "host_part_number": "HOST-ONLY-MUST-NOT-LEAK",
        "expected_decision": "EXPECTED-ONLY-MUST-NOT-LEAK",
    }


def test_workflow_input_cli_writes_only_the_allowlist_privately_and_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    output_directory = state_root / "workflow-input-private"
    output_directory.mkdir(parents=True, mode=0o700)
    output = output_directory / "input.json"
    record = _synthetic_workflow_input_record()
    monkeypatch.setattr(
        environment_control,
        "_load_seed_plan",
        lambda _package_root, *, seed: ([record], tmp_path / seed),
    )
    monkeypatch.setattr(
        environment_control,
        "_claim_environment_owner",
        lambda _state_root: None,
    )
    monkeypatch.setattr(
        environment_control.sys,
        "argv",
        [
            "environment_control.py",
            "--state-root",
            str(state_root),
            "--package-root",
            str(tmp_path / "synthetic-package"),
            "workflow-input",
            "--seed",
            "debug",
            "--output",
            str(output),
        ],
    )

    assert environment_control.main() == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    payload = json.loads(output.read_bytes())
    assert set(payload) == {"records", "run_label"}
    assert payload["run_label"] == "formal"
    assert payload["records"] == [
        {
            field: record[field]
            for field in environment_control.WORKFLOW_INPUT_FIELDS
        }
    ]
    assert set(payload["records"][0]) == set(
        environment_control.WORKFLOW_INPUT_FIELDS
    )


def test_workflow_input_rejects_overwrite_and_leaf_symlink_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(
        environment_control,
        "_load_seed_plan",
        lambda _package_root, *, seed: (
            [_synthetic_workflow_input_record()],
            tmp_path / seed,
        ),
    )
    existing = state_root / "existing.json"
    existing.write_bytes(b"preserve-existing")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"preserve-outside")
    symlink = state_root / "symlink.json"
    symlink.symlink_to(outside)

    for output in (existing, symlink):
        with pytest.raises(
            environment_control.EnvironmentControlError,
            match="must not already exist or be a symlink",
        ):
            environment_control._write_workflow_input(
                state_root,
                tmp_path / "synthetic-package",
                seed="debug",
                output=output,
            )

    assert existing.read_bytes() == b"preserve-existing"
    assert outside.read_bytes() == b"preserve-outside"
    assert symlink.is_symlink()


def test_workflow_input_rejects_escape_and_symlink_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    symlink_directory = state_root / "linked-directory"
    symlink_directory.symlink_to(outside_directory, target_is_directory=True)
    monkeypatch.setattr(
        environment_control,
        "_load_seed_plan",
        lambda _package_root, *, seed: (
            [_synthetic_workflow_input_record()],
            tmp_path / seed,
        ),
    )

    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="must be located within the state root",
    ):
        environment_control._write_workflow_input(
            state_root,
            tmp_path / "synthetic-package",
            seed="debug",
            output=outside_directory / "escaped.json",
        )
    with pytest.raises(
        environment_control.EnvironmentControlError,
        match="existing non-symlink directory",
    ):
        environment_control._write_workflow_input(
            state_root,
            tmp_path / "synthetic-package",
            seed="debug",
            output=symlink_directory / "escaped.json",
        )

    assert not (outside_directory / "escaped.json").exists()


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
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-secret-deepseek")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-openai")
    monkeypatch.setenv("MISTRAL_API_KEY", "provider-secret-mistral")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/imports")
    monkeypatch.setenv("HOME", "/untrusted/home")
    monkeypatch.setenv("LILIES_MODEL_EGRESS_ENABLED", "true")
    environment = runner._platform_environment(
        tmp_path,
        first,
        port=18100,
        collaboration_policy="manual",
    )
    assert environment["MODEL_EGRESS_ENABLED"] == "false"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "MISTRAL_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "PYTHONPATH" not in environment
    assert "HOME" not in environment
    assert "LILIES_MODEL_EGRESS_ENABLED" not in environment
    assert environment["LILIES_LOCAL_DISCOVERY_FILE"] == str(
        tmp_path / "lilies-data" / "daemon.json"
    )
    assert environment["LILIES_FORMAL_HIDDEN_SEED_KEY"] == first["formal_hidden_seed_key"]
    assert (
        environment["LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY"]
        == first["collaborative_development_signing_key"]
    )
    with pytest.raises(runner.EnterpriseExperimentError, match="egress is forbidden"):
        runner._platform_environment(
            tmp_path,
            first,
            port=18100,
            collaboration_policy="manual",
            enable_model_egress=True,
        )

    host_environment = runner._scrub_provider_environment(os.environ)
    assert "DEEPSEEK_API_KEY" not in host_environment
    assert "OPENAI_API_KEY" not in host_environment
    assert "MISTRAL_API_KEY" not in host_environment
    assert "GITHUB_TOKEN" not in host_environment
    assert "AWS_SECRET_ACCESS_KEY" not in host_environment
    assert "PYTHONPATH" not in host_environment
    assert "HOME" not in host_environment
    assert "LILIES_MODEL_EGRESS_ENABLED" not in host_environment


def test_daemon_environment_projects_the_frozen_task_turn_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LILIES_DEEPSEEK_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("PYTHONPATH", "/must/not/be/inherited")
    environment = runner._daemon_environment(
        tmp_path,
        port=18101,
        provider_configuration=_test_provider_configuration("ollama-local"),
    )

    assert runner._task_max_turns() == 120
    assert environment["LILIES_DEFAULT_MAX_TURNS"] == "120"
    assert environment["LILIES_MAX_SESSION_TOKENS"] == "1000000"
    assert environment["LILIES_DATA_DIR"] == str(tmp_path / "lilies-data")
    assert environment["LILIES_WORKSPACE_ROOT"] == str(tmp_path / "lilies-workspaces")
    assert environment["LILIES_WORKFLOW_STUDIO_ENABLED"] == "true"
    assert environment["LILIES_MODEL_EGRESS_ENABLED"] == "false"
    assert environment["LILIES_MODEL_PROVIDER"] == "ollama-local"
    assert environment["LILIES_MODEL"] == "qwen3:8b"
    assert environment["LILIES_OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"
    assert environment["LILIES_CLI_TOKEN_TTL_SECONDS"] == "300"
    assert "LILIES_DEEPSEEK_API_KEY" not in environment
    assert "PYTHONPATH" not in environment

    with pytest.raises(
        runner.EnterpriseExperimentError,
        match="authorized DEEPSEEK_API_KEY is unavailable",
    ):
        runner._daemon_environment(
            tmp_path,
            port=18101,
            provider_configuration=_test_provider_configuration(),
        )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "authorized-provider-key")
    paid_environment = runner._daemon_environment(
        tmp_path,
        port=18101,
        provider_configuration=_test_provider_configuration(),
    )
    assert paid_environment["LILIES_MODEL_EGRESS_ENABLED"] == "true"
    assert paid_environment["LILIES_DEEPSEEK_API_KEY"] == "authorized-provider-key"


def test_ollama_provider_configuration_binds_models_directory_without_path_disclosure(
    tmp_path: Path,
) -> None:
    args = _ollama_launch_args(tmp_path)

    configuration = runner._provider_launch_configuration(args)
    identity = configuration["identity"]
    models_directory = args.ollama_models_dir.resolve()

    assert configuration["ollama_models_directory"] == str(models_directory)
    assert identity["ollama_models_directory_identity_digest"].startswith("sha256:")
    assert str(models_directory) not in json.dumps(identity)
    assert str(models_directory) not in json.dumps(
        {
            "provider_identity": identity,
            "managed_ollama": {
                "models_directory_identity_digest": identity[
                    "ollama_models_directory_identity_digest"
                ]
            },
        }
    )


@pytest.mark.parametrize("unsafe_kind", ("missing", "symlink", "file", "broad"))
def test_ollama_provider_configuration_rejects_missing_or_unsafe_models_directory(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    args = _ollama_launch_args(tmp_path)
    if unsafe_kind == "missing":
        args.ollama_models_dir = None
        message = "requires --ollama-models-dir"
    elif unsafe_kind == "symlink":
        target = args.ollama_models_dir
        link = tmp_path / "linked-models"
        link.symlink_to(target, target_is_directory=True)
        args.ollama_models_dir = link
        message = "unsafe"
    elif unsafe_kind == "file":
        file_path = tmp_path / "not-a-model-directory"
        file_path.write_text("not a directory", encoding="utf-8")
        args.ollama_models_dir = file_path
        message = "unsafe"
    else:
        args.ollama_models_dir = Path("/")
        message = "overly broad dangerous path"

    with pytest.raises(runner.EnterpriseExperimentError, match=message):
        runner._provider_launch_configuration(args)


def test_managed_ollama_receives_only_allowlisted_environment_and_models_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = runner._provider_launch_configuration(
        _ollama_launch_args(tmp_path)
    )
    for name in (
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_REMOTES",
    ):
        monkeypatch.setenv(name, f"must-not-pass-{name.lower()}")
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "0")
    captured = _install_managed_ollama_test_doubles(
        monkeypatch,
        wait_json=lambda *_args, **_kwargs: {
            "models": [
                {
                    "name": "qwen3:8b",
                    "model": "qwen3:8b",
                    "digest": "1" * 64,
                }
            ]
        },
    )

    with ExitStack() as stack:
        receipt = runner._start_managed_ollama(
            stack,
            state_root=tmp_path,
            attempt_id="sha256:" + "a" * 64,
            provider_configuration=configuration,
            log_path=tmp_path / "ollama.log",
        )

    environment = captured["environment"]
    assert captured["arguments"] == (
        configuration["ollama_executable"],
        "serve",
    )
    assert environment["OLLAMA_MODELS"] == configuration["ollama_models_directory"]
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert environment["OLLAMA_NO_CLOUD"] == "1"
    assert environment["OLLAMA_NOHISTORY"] == "1"
    runtime_home = Path(environment["HOME"])
    assert environment["HOME"] != os.environ["HOME"]
    assert runtime_home.is_dir()
    assert not runtime_home.is_symlink()
    assert stat.S_IMODE(runtime_home.stat().st_mode) == 0o700
    assert runtime_home.stat().st_uid == os.geteuid()
    assert all(
        name not in environment
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "OLLAMA_REMOTES",
        )
    )
    assert receipt is not None
    assert receipt["directory_check"] == "pre_and_post_start"
    assert receipt["pre_start_models_directory_identity_digest"] == receipt[
        "post_start_models_directory_identity_digest"
    ]
    assert receipt["pre_start_models_directory_identity_digest"] == (
        configuration["identity"]["ollama_models_directory_identity_digest"]
    )
    assert receipt["configured_model"] == "qwen3:8b"
    assert receipt["configured_model_manifest_digest"] == "sha256:" + "1" * 64
    assert receipt["frozen_model_manifest_digest"] == "sha256:" + "1" * 64
    assert receipt["configured_model_inventory_match_count"] == 1
    assert receipt["cloud_disabled"] is True
    assert receipt["isolated_runtime_home"] is True
    assert receipt["runtime_home_check"] == "pre_and_post_start"
    assert receipt["runtime_home_identity_digest"].startswith("sha256:")
    assert str(runtime_home) not in json.dumps(receipt)
    assert configuration["ollama_models_directory"] not in json.dumps(receipt)


def test_managed_ollama_runtime_home_creates_missing_private_attempt_directory(
    tmp_path: Path,
) -> None:
    attempt_id = "sha256:" + "a" * 64

    runtime_home, identity_digest = runner._managed_ollama_runtime_home(
        tmp_path,
        attempt_id=attempt_id,
    )

    assert runtime_home == (
        tmp_path / "managed-ollama-runtime" / ("a" * 64) / "home"
    ).resolve()
    assert not runtime_home.is_symlink()
    assert stat.S_IMODE(runtime_home.stat().st_mode) == 0o700
    assert runtime_home.stat().st_uid == os.geteuid()
    assert identity_digest.startswith("sha256:")


@pytest.mark.parametrize("unsafe_kind", ("symlink", "permissions"))
def test_managed_ollama_runtime_home_rejects_unsafe_existing_directory(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    attempt_id = "sha256:" + "a" * 64
    attempt_root = tmp_path / "managed-ollama-runtime" / ("a" * 64)
    attempt_root.mkdir(parents=True, mode=0o700)
    runtime_home = attempt_root / "home"
    if unsafe_kind == "symlink":
        target = tmp_path / "outside-home"
        target.mkdir(mode=0o700)
        runtime_home.symlink_to(target, target_is_directory=True)
        message = "unsafe"
    else:
        runtime_home.mkdir(mode=0o700)
        runtime_home.chmod(0o750)
        message = "unsafe ownership or permissions"

    with pytest.raises(runner.EnterpriseExperimentError, match=message):
        runner._managed_ollama_runtime_home(
            tmp_path,
            attempt_id=attempt_id,
        )


def test_managed_ollama_revalidates_runtime_home_after_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "sha256:" + "a" * 64
    configuration = runner._provider_launch_configuration(
        _ollama_launch_args(tmp_path)
    )
    runtime_home = (
        tmp_path / "managed-ollama-runtime" / ("a" * 64) / "home"
    )

    def weaken_runtime_home(*_args: object, **_kwargs: object) -> dict[str, Any]:
        runtime_home.chmod(0o750)
        return {
            "models": [
                {
                    "name": "qwen3:8b",
                    "digest": "1" * 64,
                }
            ]
        }

    _install_managed_ollama_test_doubles(
        monkeypatch,
        wait_json=weaken_runtime_home,
    )

    with ExitStack() as stack, pytest.raises(
        runner.EnterpriseExperimentError,
        match="unsafe ownership or permissions",
    ):
        runner._start_managed_ollama(
            stack,
            state_root=tmp_path,
            attempt_id=attempt_id,
            provider_configuration=configuration,
            log_path=tmp_path / "ollama.log",
        )


def test_managed_ollama_rejects_models_directory_swap_between_start_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _ollama_launch_args(tmp_path)
    configuration = runner._provider_launch_configuration(args)
    models_directory = args.ollama_models_dir

    def swap_directory(*_args: object, **_kwargs: object) -> dict[str, Any]:
        models_directory.rename(models_directory.with_name("models-before-swap"))
        models_directory.mkdir()
        return {
            "models": [
                {
                    "name": "qwen3:8b",
                    "digest": "1" * 64,
                }
            ]
        }

    _install_managed_ollama_test_doubles(
        monkeypatch,
        wait_json=swap_directory,
    )

    with ExitStack() as stack, pytest.raises(
        runner.EnterpriseExperimentError,
        match="identity changed during startup",
    ):
        runner._start_managed_ollama(
            stack,
            state_root=tmp_path,
            attempt_id="sha256:" + "a" * 64,
            provider_configuration=configuration,
            log_path=tmp_path / "ollama.log",
        )


@pytest.mark.parametrize(
    ("inventory", "message"),
    (
        (
            {"models": [{"name": "wrong:latest", "digest": "1" * 64}]},
            "exactly one configured model",
        ),
        (
            {"models": [{"name": "qwen3:8b", "digest": "2" * 64}]},
            "manifest digest does not match",
        ),
        (
            {
                "models": [
                    {"name": "qwen3:8b", "digest": "1" * 64},
                    {"name": "qwen3:8b", "digest": "1" * 64},
                ]
            },
            "exactly one configured model",
        ),
    ),
)
def test_managed_ollama_rejects_wrong_or_duplicate_configured_model_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory: dict[str, Any],
    message: str,
) -> None:
    configuration = runner._provider_launch_configuration(
        _ollama_launch_args(tmp_path)
    )
    _install_managed_ollama_test_doubles(
        monkeypatch,
        wait_json=lambda *_args, **_kwargs: inventory,
    )

    with ExitStack() as stack, pytest.raises(
        runner.EnterpriseExperimentError,
        match=message,
    ):
        runner._start_managed_ollama(
            stack,
            state_root=tmp_path,
            attempt_id="sha256:" + "a" * 64,
            provider_configuration=configuration,
            log_path=tmp_path / "ollama.log",
        )


def test_run_and_resume_freeze_the_same_ollama_models_directory_identity(
    tmp_path: Path,
) -> None:
    original_args = _ollama_launch_args(tmp_path)
    original = runner._provider_launch_configuration(original_args)
    replay = runner._provider_launch_configuration(original_args)
    other_models = tmp_path / "other-store" / "models"
    other_models.mkdir(parents=True)
    changed_args = _ollama_launch_args(
        tmp_path,
        models_directory=other_models,
    )
    changed = runner._provider_launch_configuration(changed_args)

    assert original["identity"] == replay["identity"]
    assert original["identity"] != changed["identity"]
    for function in (runner.run_seed, runner.resume_seed):
        source = inspect.getsource(function)
        assert "provider_configuration = _provider_launch_configuration(args)" in source
        assert 'runtime_identity["provider_identity"] = provider_identity' in source
        assert "_start_managed_ollama(" in source
        assert "state_root=state_root" in source
        assert "attempt_id=attempt_id" in source
    resume_source = inspect.getsource(runner.resume_seed)
    assert resume_source.index('runtime_identity["provider_identity"] = provider_identity') < (
        resume_source.index("runtime_identity != persisted_runtime_identity")
    )


def test_platform_discovery_binds_the_exact_isolated_daemon_before_pairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = "sha256:" + "a" * 64
    observed: dict[str, Any] = {}

    def fake_request(
        base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed.update(base_url=base_url, path=path, kwargs=kwargs)
        return {
            "enabled": True,
            "connections": [],
            "discovery": {
                "status": "available",
                "base_url": "http://127.0.0.1:18101",
                "daemon_fingerprint": fingerprint,
                "pid": 4321,
            },
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._assert_platform_discovered_daemon(
        "http://127.0.0.1:18100",
        "t" * 48,
        daemon_url="http://127.0.0.1:18101",
        daemon_pid=4321,
        daemon_health={
            "daemon_fingerprint": fingerprint,
            "model_egress_enabled": True,
        },
    )

    assert result == {
        "status": "available",
        "base_url": "http://127.0.0.1:18101",
        "daemon_fingerprint": fingerprint,
        "pid": 4321,
    }
    assert observed == {
        "base_url": "http://127.0.0.1:18100",
        "path": "/api/v1/local-lilies/status",
        "kwargs": {"token": "t" * 48},
    }


@pytest.mark.parametrize(
    "discovery",
    (
        {"status": "unavailable", "reason": "stale_record"},
        {
            "status": "available",
            "base_url": "http://127.0.0.1:9999",
            "daemon_fingerprint": "sha256:" + "a" * 64,
            "pid": 4321,
        },
        {
            "status": "available",
            "base_url": "http://127.0.0.1:18101",
            "daemon_fingerprint": "sha256:" + "b" * 64,
            "pid": 4321,
        },
        {
            "status": "available",
            "base_url": "http://127.0.0.1:18101",
            "daemon_fingerprint": "sha256:" + "a" * 64,
            "pid": 9999,
        },
    ),
)
def test_platform_discovery_rejects_stale_or_different_daemon_identity(
    discovery: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "_request_json",
        lambda *_args, **_kwargs: {"discovery": discovery},
    )

    with pytest.raises(runner.EnterpriseExperimentError, match="discover"):
        runner._assert_platform_discovered_daemon(
            "http://127.0.0.1:18100",
            "t" * 48,
            daemon_url="http://127.0.0.1:18101",
            daemon_pid=4321,
            daemon_health={
                "daemon_fingerprint": "sha256:" + "a" * 64,
                "model_egress_enabled": True,
            },
        )


def test_lifecycle_evidence_has_all_safe_utc_phases_and_time_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter(value * 1_000_000_000 for value in range(9))
    timestamp = "2026-08-01T00:00:00+00:00"
    monkeypatch.setattr(runner.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(runner, "_now", lambda: timestamp)
    lifecycle = runner._LifecycleRecorder(mode="run")

    for phase in runner.LIFECYCLE_PHASES:
        lifecycle.start(phase)
        lifecycle.finish(outcome="completed")
    evidence = lifecycle.snapshot()

    assert evidence["mode"] == "run"
    assert evidence["clock"] == {
        "timestamps": "UTC",
        "durations": "monotonic",
    }
    assert evidence["private_reasoning_captured"] is False
    assert evidence["total_duration_seconds"] == 8.0
    assert evidence["measured_phase_duration_seconds"] == 8.0
    assert evidence["accounting_residual_seconds"] == 0.0
    assert evidence["accounting_residual_percent"] == 0.0
    assert evidence["phase_share_denominator"] == "total_duration_seconds"
    assert [span["phase"] for span in evidence["spans"]] == list(runner.LIFECYCLE_PHASES)
    assert [span["duration_seconds"] for span in evidence["spans"]] == [1.0] * 8
    assert evidence["phase_share_percent"] == {phase: 12.5 for phase in runner.LIFECYCLE_PHASES}
    assert evidence["execution_journal"]["sealed_after_cleanup"] is True
    assert evidence["sealing_boundary"]["human_report_derivation"] == (
        "after_lifecycle_and_journal_seal"
    )
    assert sum(evidence["phase_share_percent"].values()) == pytest.approx(100.0)
    assert all(
        datetime.fromisoformat(transition["at"]).utcoffset() == timedelta(0)
        for transition in evidence["transitions"]
    )
    assert "reasoning" not in json.dumps(evidence).casefold().replace(
        "private_reasoning_captured", ""
    )


def test_resource_scope_tears_down_processes_then_environment_without_volumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[str, ...]]] = []

    def fake_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: dict[str, str],
    ) -> None:
        assert environment == {"RUNNER": "isolated"}
        observed.append(("environment", arguments))

    monkeypatch.setattr(runner, "_environment_command", fake_environment_command)
    lifecycle = runner._LifecycleRecorder(mode="run")
    lifecycle.start("environment")
    scope = runner._RunResourceScope(
        lifecycle,
        state_root=tmp_path,
        environment={"RUNNER": "isolated"},
    )
    scope.mark_environment_up_attempted()

    with scope as stack:
        stack.callback(lambda: observed.append(("process", ("teardown",))))
        lifecycle.finish(outcome="completed")
        lifecycle.start("discovery")
        lifecycle.finish(outcome="completed")

    scope.finish_reporting()
    evidence = lifecycle.snapshot()
    assert observed == [
        ("process", ("teardown",)),
        ("environment", ("down",)),
    ]
    assert evidence["spans"][-1]["phase"] == "cleanup"
    assert evidence["spans"][-1]["outcome"] == "completed"
    assert sum(evidence["phase_share_percent"].values()) == pytest.approx(100.0)


def test_resource_scope_records_environment_down_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_down(*_args: object, **_kwargs: object) -> None:
        raise runner.EnterpriseExperimentError("injected down failure")

    monkeypatch.setattr(runner, "_environment_command", fail_down)
    lifecycle = runner._LifecycleRecorder(mode="run")
    lifecycle.start("environment")
    scope = runner._RunResourceScope(
        lifecycle,
        state_root=tmp_path,
        environment={},
    )
    scope.mark_environment_up_attempted()

    with pytest.raises(
        runner.EnterpriseExperimentError,
        match="controlled cleanup failed: environment_down",
    ):
        with scope:
            lifecycle.finish(outcome="completed")
            lifecycle.start("discovery")
            lifecycle.finish(outcome="completed")

    scope.finish_reporting()
    evidence = lifecycle.snapshot()
    assert evidence["spans"][-1]["outcome"] == "failed"


def _minimal_report_kwargs(lifecycle: runner._LifecycleRecorder) -> dict[str, Any]:
    return {
        "seed": "101",
        "started_at": lifecycle.started_at,
        "status": "run_failed",
        "package": None,
        "application": None,
        "connection": None,
        "assignment": None,
        "secret_receipts": (),
        "host_snapshots": (),
        "platform_verification": None,
        "host_verification": None,
        "error": None,
    }


def test_report_is_derived_after_cleanup_and_journal_seal(
    tmp_path: Path,
) -> None:
    lifecycle = runner._LifecycleRecorder(mode="run")
    lifecycle.start("environment")
    resources = runner._RunResourceScope(
        lifecycle,
        state_root=tmp_path / "state",
        environment={},
    )
    with resources:
        lifecycle.finish(outcome="completed")
    path, committed, error = runner._finalize_run_evidence(
        lifecycle,
        resources,
        tmp_path / "evidence",
        **_minimal_report_kwargs(lifecycle),
    )
    evidence = json.loads(path.read_bytes())
    assert committed is True
    assert error is None
    assert evidence["report_transaction"]["status"] == ("derived_after_lifecycle_seal")
    assert evidence["report_transaction"]["phase_denominator_excludes_report_derivation"] is True
    assert evidence["lifecycle"]["spans"][-1]["duration_seconds"] > 0


def test_report_commit_failure_is_persisted_with_fixed_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = runner._LifecycleRecorder(mode="run")
    lifecycle.start("environment")
    resources = runner._RunResourceScope(
        lifecycle,
        state_root=tmp_path / "state",
        environment={},
    )
    with resources:
        lifecycle.finish(outcome="completed")

    def fail_commit(*_args: Any, **_kwargs: Any) -> None:
        raise runner.EnterpriseExperimentError("ORACLE_EXPECTED must never escape")

    monkeypatch.setattr(runner, "_write_run_evidence", fail_commit)
    path, committed, error = runner._finalize_run_evidence(
        lifecycle,
        resources,
        tmp_path / "evidence",
        **_minimal_report_kwargs(lifecycle),
    )
    evidence = json.loads(path.read_bytes())
    assert committed is False
    assert isinstance(error, runner.EnterpriseExperimentError)
    assert evidence["status"] == "report_commit_failed"
    assert evidence["lifecycle"]["spans"][-1]["outcome"] == "not_required"
    assert "ORACLE_EXPECTED" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("assignment", "expected"),
    (
        ({"phase": "completed"}, "completed"),
        ({"phase": "failed"}, "failed"),
        ({"phase": "cancelled"}, "cancelled"),
        ({"phase": "waiting"}, "waiting"),
        ({"phase": "running", "runner_timeout": True}, "timeout"),
        (
            {
                "phase": "running",
                "runner_terminal": "builder_ready_without_completion_claim",
            },
            "builder_ready_without_completion_claim",
        ),
        (
            {
                "phase": "waiting",
                "runner_terminal": "unattended_permission_rejected",
            },
            "unattended_permission_rejected",
        ),
    ),
)
def test_builder_lifecycle_outcome_preserves_observable_terminal_state(
    assignment: dict[str, Any],
    expected: str,
) -> None:
    assert runner._builder_lifecycle_outcome(assignment) == expected


def test_standalone_runtime_probe_uses_fixed_isolated_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(arguments: object, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        parsed_arguments = tuple(arguments)  # type: ignore[arg-type]
        if parsed_arguments[0] == "git":
            observed.setdefault("git_arguments", []).append(parsed_arguments)
            stdout = b"a" * 40 + b"\n" if parsed_arguments[1] == "rev-parse" else b""
            return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")
        observed["arguments"] = parsed_arguments
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
    monkeypatch.setattr(
        runner,
        "_standalone_source_tree_identity",
        lambda _path: {
            "source_tree_digest": "sha256:" + "b" * 64,
            "source_file_count": 12,
            "source_total_bytes": 4_096,
        },
    )

    identity = runner._verify_standalone_lilies_runtime()
    assert identity["python"] == str(runner.STANDALONE_LILIES_ROOT / ".venv" / "bin" / "python")
    assert identity["builder_actor"] == "lilies"
    assert identity["sibling_commit"] == "a" * 40
    assert identity["package_digest"] == "sha256:" + "b" * 64
    assert identity["source_tree_digest"] == identity["package_digest"]
    assert identity["package_digest_source"] == ("executed_src/lilies_agent_path_bytes")
    assert identity["sibling_dirty"] is False
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
            "module identity",
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

    def fake_run(arguments: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        stdout = (
            b"a" * 40 + b"\n"
            if arguments[:2] == ("git", "rev-parse")
            else b""
            if arguments[0] == "git"
            else json.dumps(payload).encode()
        )
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(runner, "_run_bounded_subprocess", fake_run)
    monkeypatch.setattr(
        runner,
        "_standalone_source_tree_identity",
        lambda _path: {
            "source_tree_digest": "sha256:" + "b" * 64,
            "source_file_count": 12,
            "source_total_bytes": 4_096,
        },
    )

    with pytest.raises(runner.EnterpriseExperimentError, match=failure):
        runner._verify_standalone_lilies_runtime()


def test_builder_provenance_binds_sibling_package_and_all_public_tool_actors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = {
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "application_id": "c44b3387-d780-4986-b3d6-dc112851983b",
        "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
    }
    actor = "lilies"

    def fake_request(_base_url: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        if path.endswith("?limit=500"):
            return {
                "channels": [
                    {
                        "channel_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
                        "assignment_id": assignment["assignment_id"],
                    }
                ]
            }
        return {
            "context": {
                "assignment": {
                    **assignment,
                    "task_id": runner.TASK_ID,
                    "task_revision": runner.REVISION,
                },
                "observable_events": [
                    {
                        "seq": 1,
                        "kind": "tool",
                        "event_type": "tool.started",
                        "actor": actor,
                        "tool_name": "workflow_patch",
                        "status": "started",
                    },
                    {
                        "seq": 2,
                        "kind": "tool",
                        "event_type": "tool.completed",
                        "actor": actor,
                        "tool_name": "workflow_patch",
                        "status": "completed",
                    },
                ],
                "applications": [
                    {
                        "application_id": assignment["application_id"],
                        "draft": {"revision": 1},
                    }
                ],
            }
        }

    monkeypatch.setattr(runner, "_request_json", fake_request)
    receipt = runner._builder_provenance_receipt(
        "http://127.0.0.1:18100",
        "platform-token",
        assignment=assignment,
        discovery={"daemon_fingerprint": "sha256:" + "a" * 64},
        runtime_identity=_runner_runtime_identity(),
        qualified_verification=_runner_qualified_platform_verification(assignment["assignment_id"]),
    )
    assert receipt["builder_actor"] == "lilies"
    assert receipt["sibling_commit"] == "a" * 40
    assert receipt["sibling_package_digest"] == "sha256:" + "b" * 64
    mutation = receipt["mutation_provenance"]
    assert mutation["status"] == ("append_only_formal_mutation_chain_qualified_transitively")
    assert mutation["qualified_actor_kind"] == "lilies_blackbox"
    assert mutation["contracts"] == list(runner.FROZEN_MUTATION_QUALIFICATION_CONTRACTS)
    assert receipt["public_tool_attribution"]["status"].endswith("attributed_to_lilies")

    actor = "harness"
    with pytest.raises(runner.EnterpriseExperimentError, match="not attributed to Lilies"):
        runner._builder_provenance_receipt(
            "http://127.0.0.1:18100",
            "platform-token",
            assignment=assignment,
            discovery={"daemon_fingerprint": "sha256:" + "a" * 64},
            runtime_identity=_runner_runtime_identity(),
            qualified_verification=_runner_qualified_platform_verification(
                assignment["assignment_id"]
            ),
        )

    actor = "lilies"
    tampered = _runner_qualified_platform_verification(assignment["assignment_id"])
    tampered["task_package_digest"], tampered["environment_ready_digest"] = (
        tampered["environment_ready_digest"],
        tampered["task_package_digest"],
    )
    with pytest.raises(runner.EnterpriseExperimentError, match="receipt is invalid"):
        runner._builder_provenance_receipt(
            "http://127.0.0.1:18100",
            "platform-token",
            assignment=assignment,
            discovery={"daemon_fingerprint": "sha256:" + "a" * 64},
            runtime_identity=_runner_runtime_identity(),
            qualified_verification=tampered,
        )


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
    model_egress_enabled: bool = False,
    max_session_tokens: int = runner.MAX_SESSION_TOKENS,
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
        "model_egress_enabled": model_egress_enabled,
        "max_session_tokens": max_session_tokens,
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
            "unreaped_development_processes": 0,
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


def _runner_session_budget_snapshot(
    *,
    session_id: str = "6ea5edda-23dd-4186-9981-948788bff0e8",
    recorded_calls: int = 1,
    unknown_calls: int = 0,
    input_tokens: int = 12,
    output_tokens: int = 3,
    max_session_tokens: int = runner.MAX_SESSION_TOKENS,
    model_egress_enabled: bool = True,
    active_provider_calls: int = 0,
    include_session: bool = True,
) -> dict[str, Any]:
    before = _runner_observability_receipt(
        recorded_calls=recorded_calls,
        unknown_calls=unknown_calls,
        active_provider_calls=active_provider_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=0.01,
        model_egress_enabled=model_egress_enabled,
        max_session_tokens=max_session_tokens,
    )
    after = json.loads(json.dumps(before))
    after["captured_at"] = "2026-07-25T01:00:01+00:00"
    after["activity_revision"] = 11
    item_session_id = session_id if include_session else "37bdd358-fb44-43b2-a899-4e8989ba5cc1"
    items = [
        {
            "session_id": item_session_id,
            "stage": "builder",
            "model": "deepseek-chat",
            "recorded_calls": recorded_calls,
            "unknown_calls": unknown_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": 0.01,
        }
    ]
    return {
        "schema_version": "1.0",
        "snapshot_kind": "paired_observability_bracket",
        "before": before,
        "client_acl_usage": {
            "schema_version": "1.0",
            "group_by": ["session", "stage", "model"],
            "items": items,
            "page": 1,
            "page_size": 100,
            "returned_count": 1,
            "total_items": 1,
            "total_pages": 1,
            "truncated": False,
            "snapshot_kind": "complete_paginated_merge",
        },
        "after": after,
    }


def _runner_global_usage_baseline() -> dict[str, Any]:
    return runner._global_usage_baseline(
        {
            "after": _runner_observability_receipt(
                model_egress_enabled=True,
            )
        },
        require_fresh=True,
    )


def _runner_budget_event_checkpoint(snapshot: dict[str, Any]) -> dict[str, Any]:
    checkpoint = runner._session_usage_checkpoint(
        snapshot,
        session_id="6ea5edda-23dd-4186-9981-948788bff0e8",
        global_baseline=_runner_global_usage_baseline(),
    )
    assert checkpoint is not None
    return {
        field: checkpoint[field] for field in (*runner.SESSION_USAGE_COUNTER_FIELDS, "cost_usd")
    }


def _runner_runtime_identity() -> dict[str, Any]:
    return {
        "builder_actor": "lilies",
        "python": str(runner.STANDALONE_LILIES_PYTHON),
        "sibling_root": str(runner.STANDALONE_LILIES_ROOT),
        "sibling_commit": "a" * 40,
        "distribution": runner.STANDALONE_LILIES_DISTRIBUTION,
        "version": runner.STANDALONE_LILIES_VERSION,
        "package_digest": "sha256:" + "b" * 64,
        "package_file_count": 12,
        "package_digest_source": "executed_src/lilies_agent_path_bytes",
        "source_tree_digest": "sha256:" + "b" * 64,
        "source_file_count": 12,
        "source_total_bytes": 4_096,
        "sibling_dirty": False,
        "sibling_dirty_entry_count": 0,
        "sibling_dirty_status_digest": runner._digest(b""),
        "provider_identity": _test_provider_identity(),
    }


def _runner_qualified_platform_verification(
    assignment_id: str = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
) -> dict[str, Any]:
    digests = {
        "task_package_digest": "sha256:" + "1" * 64,
        "environment_ready_digest": "sha256:" + "2" * 64,
        "archive_manifest_digest": "sha256:" + "3" * 64,
        "frozen_context_digest": "sha256:" + "4" * 64,
        "verification_process_digest": "sha256:" + "5" * 64,
    }
    claim_id = "5c7803f8-c684-4fca-b9c7-11d8e5cf4f85"
    binding = {
        "assignment_id": assignment_id,
        "claim_id": claim_id,
        **digests,
        "validation_mode": "real_host",
    }
    unsigned = {
        "schema_version": "1.1",
        "response_schema_version": "1.0",
        "assignment_id": assignment_id,
        "claim_id": claim_id,
        "claim_status": "independently_verified",
        "verification_id": "6331b53a-3211-46a8-ae72-f6fedabfc43c",
        "verdict": "independently_verified",
        "oracle_digest": "sha256:" + "6" * 64,
        "difference_count": 0,
        **digests,
        "validation_mode": "real_host",
        "frozen_verification_binding_digest": runner._digest(runner._canonical_json(binding)),
        "stable_hidden_runs": 3,
        "consecutive_passes": 3,
        "stable_progress_digest": "sha256:" + "7" * 64,
        "stable_verdict": None,
    }
    return {
        **unsigned,
        "receipt_digest": runner._digest(runner._canonical_json(unsigned)),
    }


def _signed_runner_receipt(schema_version: str, **fields: Any) -> dict[str, Any]:
    unsigned = {"schema_version": schema_version, **fields}
    return {**unsigned, "receipt_digest": runner._digest(runner._canonical_json(unsigned))}


def _write_test_active_run(
    state_root: Path,
    *,
    seed: str,
    application_id: str,
    connection_id: str,
    assignment_id: str,
    build_id: str,
    session_id: str,
    attempt_started_at: str = "2026-08-01T00:00:00+00:00",
) -> dict[str, Any]:
    attempt_id = runner._run_attempt_id(seed, attempt_started_at)
    environment_instance_id = f"exp-lilies-001:r28:seed-{seed}:attempt-test"
    environment_generation = _signed_runner_receipt(
        "v0.4.13-t01h-environment-generation-1",
        task_id=runner.TASK_ID,
        revision=runner.REVISION,
        seed=seed,
        attempt_id=attempt_id,
        environment_instance_id=environment_instance_id,
        generation_id="sha256:" + "c" * 64,
        baseline_snapshot_digest="sha256:" + "d" * 64,
        status="fresh_reset_initialized_seeded",
        created_at=attempt_started_at,
    )
    empty_draft = _signed_runner_receipt(
        "v0.4.13-t01h-empty-draft-1",
        application_id=application_id,
        draft_revision=0,
        draft_content_hash="e" * 64,
        requirement_empty=True,
        workflow_node_count=0,
        workflow_edge_count=0,
        agent_count=0,
        test_count=0,
        active_version=None,
        observed_at=attempt_started_at,
    )
    application = {
        "id": application_id,
        "runner_empty_draft_receipt": empty_draft,
    }
    assignment = {
        "assignment_id": assignment_id,
        "build_id": build_id,
        "session_id": session_id,
    }
    runtime_identity = _runner_runtime_identity()
    historical_identity = runner._record_historical_identity(
        state_root,
        seed=seed,
        attempt_id=attempt_id,
        application=application,
        assignment=assignment,
        environment_generation=environment_generation,
        runtime_identity=runtime_identity,
    )
    runner._write_active_run(
        state_root,
        seed=seed,
        collaboration_policy="manual",
        operational_permission_policy="task_local_workspace",
        platform_port=18100,
        daemon_port=18101,
        application=application,
        connection={
            "connection_id": connection_id,
            "daemon_fingerprint": "sha256:" + "a" * 64,
        },
        assignment=assignment,
        run_attempt_id=attempt_id,
        attempt_started_at=attempt_started_at,
        environment_instance_id=environment_instance_id,
        environment_generation=environment_generation,
        empty_draft_receipt=empty_draft,
        historical_identity=historical_identity,
        global_usage_baseline=_runner_global_usage_baseline(),
        runtime_identity=runtime_identity,
    )
    return runner._validated_active_run(state_root, seed)


def _runner_complete_session_budget_sequence(
    *,
    assignment_id: str = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
    session_id: str = "6ea5edda-23dd-4186-9981-948788bff0e8",
    recorded_calls: int = 1,
    input_tokens: int = 12,
    output_tokens: int = 3,
) -> dict[str, Any]:
    unsigned = {
        "schema_version": "v0.4.13-t01h-session-budget-sequence-1",
        "attempt_id": "sha256:" + "b" * 64,
        "assignment_id": assignment_id,
        "session_id": session_id,
        "max_session_tokens": runner.MAX_SESSION_TOKENS,
        "global_baseline": _runner_global_usage_baseline(),
        "snapshot_count": 2,
        "coverage_gap_count": 0,
        "event_scan": {
            "status": "complete",
            "scanned_through_cursor": 10,
            "budget_exceeded": False,
            "event_seq": None,
            "event_aggregate": None,
            "preceding_usage_event_seq": None,
            "post_cap_usage_event_count": 0,
            "final_usage_aggregate": {
                "attempted_calls": recorded_calls,
                "recorded_calls": recorded_calls,
                "unknown_calls": 0,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": 0.01,
            },
        },
        "last_checkpoint": {
            "recorded_calls": recorded_calls,
            "unknown_calls": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": 0.01,
        },
        "hard_stop": None,
        "status": "complete",
    }
    return {**unsigned, "receipt_digest": runner._digest(runner._canonical_json(unsigned))}


def test_session_budget_requires_public_cap_and_exact_complete_session_usage() -> None:
    assignment = {
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
    }
    discovery = {"daemon_fingerprint": "sha256:" + "a" * 64}

    receipt = runner._session_budget_receipt(
        _runner_session_budget_snapshot(),
        assignment=assignment,
        discovery=discovery,
        sequence=_runner_complete_session_budget_sequence(),
        global_baseline=_runner_global_usage_baseline(),
    )

    assert receipt["status"] == "within_cap_complete_usage"
    assert receipt["runtime_cap_attested"] is True
    assert receipt["max_session_tokens"] == 1_000_000
    assert receipt["usage"] == {
        "recorded_calls": 1,
        "unknown_calls": 0,
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cost_usd": 0.01,
    }
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    assert receipt["receipt_digest"] == runner._digest(runner._canonical_json(unsigned))


@pytest.mark.parametrize(
    ("snapshot", "message"),
    (
        (
            _runner_session_budget_snapshot(max_session_tokens=999_999),
            "exact session token cap",
        ),
        (
            _runner_session_budget_snapshot(model_egress_enabled=False),
            "exact session token cap",
        ),
        (
            _runner_session_budget_snapshot(unknown_calls=1),
            "unknown provider call",
        ),
        (
            _runner_session_budget_snapshot(input_tokens=1_000_001, output_tokens=0),
            "token cap was exceeded",
        ),
        (
            _runner_session_budget_snapshot(recorded_calls=0),
            "no completed model call",
        ),
        (
            _runner_session_budget_snapshot(include_session=False),
            "not exactly attributable",
        ),
        (
            _runner_session_budget_snapshot(active_provider_calls=1),
            "in-flight provider call",
        ),
    ),
)
def test_session_budget_fails_closed_for_unattested_or_incomplete_usage(
    snapshot: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(runner.EnterpriseExperimentError, match=message):
        runner._session_budget_receipt(
            snapshot,
            assignment={
                "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
                "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
            },
            discovery={"daemon_fingerprint": "sha256:" + "a" * 64},
            sequence=_runner_complete_session_budget_sequence(),
            global_baseline=_runner_global_usage_baseline(),
        )


def test_session_budget_sequence_is_monotonic_and_attests_hard_stop(
    tmp_path: Path,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    session_id = "6ea5edda-23dd-4186-9981-948788bff0e8"
    tracker = runner._SessionBudgetSequence(
        tmp_path,
        attempt_id="sha256:" + "b" * 64,
        assignment_id=assignment_id,
        session_id=session_id,
        global_baseline=_runner_global_usage_baseline(),
    )
    first = _runner_session_budget_snapshot(
        recorded_calls=1,
        input_tokens=900_000,
        output_tokens=99_999,
    )
    at_cap = _runner_session_budget_snapshot(
        recorded_calls=2,
        input_tokens=900_000,
        output_tokens=100_000,
    )

    tracker.observe(first)
    tracker.observe(at_cap)
    assert tracker.requires_stop_confirmation is True
    first_confirmation = json.loads(json.dumps(at_cap))
    first_confirmation["after"]["captured_at"] = "2026-07-25T01:00:02+00:00"
    second_confirmation = json.loads(json.dumps(at_cap))
    second_confirmation["after"]["captured_at"] = "2026-07-25T01:00:03+00:00"
    tracker.observe(first_confirmation)
    assert tracker.requires_stop_confirmation is True
    tracker.observe(second_confirmation)
    receipt = tracker.receipt(
        event_scan={
            "status": "complete",
            "scanned_through_cursor": 12,
            "budget_exceeded": False,
            "event_seq": None,
        }
    )

    assert receipt["status"] == "complete"
    assert receipt["hard_stop"]["trigger"] == "token_cap_reached"
    assert receipt["hard_stop"]["status"] == "hard_stop_attested"
    assert receipt["hard_stop"]["post_trigger_confirmation_count"] == 2
    assert (
        tmp_path / "monitoring" / "attempts" / ("b" * 64) / "session-budget-sequence.receipt.json"
    ).is_file()


def test_session_budget_sequence_rejects_regression_unknown_and_post_stop_calls(
    tmp_path: Path,
) -> None:
    def tracker(suffix: str) -> runner._SessionBudgetSequence:
        return runner._SessionBudgetSequence(
            tmp_path / suffix,
            attempt_id="sha256:" + "c" * 64,
            assignment_id="fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
            session_id="6ea5edda-23dd-4186-9981-948788bff0e8",
            global_baseline=_runner_global_usage_baseline(),
        )

    regressing = tracker("regressing")
    regressing.observe(
        _runner_session_budget_snapshot(
            recorded_calls=2,
            input_tokens=20,
            output_tokens=5,
        )
    )
    with pytest.raises(runner.EnterpriseExperimentError, match="counter regressed"):
        regressing.observe(
            _runner_session_budget_snapshot(
                recorded_calls=1,
                input_tokens=10,
                output_tokens=2,
            )
        )

    with pytest.raises(runner.EnterpriseExperimentError, match="unknown provider call"):
        tracker("unknown").observe(_runner_session_budget_snapshot(unknown_calls=1))

    cost = tracker("cost")
    higher_cost = _runner_session_budget_snapshot()
    higher_cost["client_acl_usage"]["items"][0]["cost_usd"] = 0.02
    higher_cost["before"]["usage"]["cost_usd"] = 0.02
    higher_cost["after"]["usage"]["cost_usd"] = 0.02
    cost.observe(higher_cost)
    with pytest.raises(runner.EnterpriseExperimentError, match="cost regressed"):
        cost.observe(_runner_session_budget_snapshot())

    stopped = tracker("stopped")
    stable = _runner_session_budget_snapshot(recorded_calls=1)
    stopped.observe(stable)
    stopped.observe(
        stable,
        budget_event={
            "event_seq": 9,
            "event_created_at": "2026-07-25T01:00:00+00:00",
            "event_aggregate": _runner_budget_event_checkpoint(stable),
        },
    )
    with pytest.raises(runner.EnterpriseExperimentError, match="after the session hard stop"):
        stopped.observe(_runner_session_budget_snapshot(recorded_calls=2))


def test_session_budget_sequence_marks_any_observation_gap_incomplete(
    tmp_path: Path,
) -> None:
    tracker = runner._SessionBudgetSequence(
        tmp_path,
        attempt_id="sha256:" + "d" * 64,
        assignment_id="fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        session_id="6ea5edda-23dd-4186-9981-948788bff0e8",
        global_baseline=_runner_global_usage_baseline(),
    )
    snapshot = _runner_session_budget_snapshot()
    tracker.observe(None)
    tracker.observe(snapshot)
    tracker.observe(snapshot)
    receipt = tracker.receipt(
        event_scan={
            "status": "complete",
            "scanned_through_cursor": 1,
            "budget_exceeded": False,
            "event_seq": None,
        }
    )
    assert receipt["status"] == "incomplete"
    assert receipt["coverage_gap_count"] == 1


def test_budget_event_time_checkpoint_rejects_a_call_already_made_after_event(
    tmp_path: Path,
) -> None:
    tracker = runner._SessionBudgetSequence(
        tmp_path,
        attempt_id="sha256:" + "e" * 64,
        assignment_id="fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        session_id="6ea5edda-23dd-4186-9981-948788bff0e8",
        global_baseline=_runner_global_usage_baseline(),
    )
    at_event = _runner_session_budget_snapshot(recorded_calls=1)
    after_extra_call = _runner_session_budget_snapshot(
        recorded_calls=2,
        input_tokens=20,
        output_tokens=5,
    )
    tracker.observe(at_event)
    with pytest.raises(runner.EnterpriseExperimentError, match="advanced after budget"):
        tracker.observe(
            after_extra_call,
            budget_event={
                "event_seq": 4,
                "event_created_at": "2026-07-25T01:00:00+00:00",
                "event_aggregate": _runner_budget_event_checkpoint(at_event),
            },
        )


def test_session_budget_sequence_reloads_original_attempt_state_on_resume(
    tmp_path: Path,
) -> None:
    common = {
        "attempt_id": "sha256:" + "f" * 64,
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
        "global_baseline": _runner_global_usage_baseline(),
    }
    first_process = runner._SessionBudgetSequence(tmp_path, **common)
    first_process.observe(
        _runner_session_budget_snapshot(
            recorded_calls=2,
            input_tokens=20,
            output_tokens=5,
        )
    )
    resumed_process = runner._SessionBudgetSequence(tmp_path, **common)
    with pytest.raises(runner.EnterpriseExperimentError, match="counter regressed"):
        resumed_process.observe(_runner_session_budget_snapshot(recorded_calls=1))


def test_session_budget_rejects_daemon_global_unknown_unattributed_delta() -> None:
    snapshot = _runner_session_budget_snapshot()
    for boundary in ("before", "after"):
        snapshot[boundary]["usage"]["attempted_calls"] += 1
        snapshot[boundary]["usage"]["unknown_calls"] += 1
    with pytest.raises(runner.EnterpriseExperimentError, match="not exactly attributable"):
        runner._session_budget_receipt(
            snapshot,
            assignment={
                "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
                "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
            },
            discovery={"daemon_fingerprint": "sha256:" + "a" * 64},
            sequence=_runner_complete_session_budget_sequence(),
            global_baseline=_runner_global_usage_baseline(),
        )


def test_public_assignment_event_scan_detects_budget_exceeded_without_data_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    session_id = "6ea5edda-23dd-4186-9981-948788bff0e8"
    turn_id = "ec9357bc-1723-431d-b244-73f8d57f1801"
    rows: list[bytes] = []
    for seq, event_type in ((1, "usage.model_call"), (2, "budget.exceeded")):
        payload = {
            "event_id": f"{assignment_id}:{seq}",
            "assignment_id": assignment_id,
            "session_id": session_id,
            "seq": seq,
            "daemon_seq": seq,
            "event_type": event_type,
            "data": (
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "call_id": "call-1",
                    "stage": "assignment:builder",
                    "model": runner.DEFAULT_DEEPSEEK_MODEL,
                    "call_index": 1,
                    "usage_status": "recorded",
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "cost_usd": 0.01,
                }
                if event_type == "usage.model_call"
                else {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "budget_tokens": runner.MAX_SESSION_TOKENS,
                    "recorded_tokens": 15,
                    "reserved_tokens": runner.MAX_SESSION_TOKENS,
                    "reason": "next model call could cross the session token limit",
                }
            ),
            "replayed": True,
            "created_at": f"2026-08-01T00:00:0{seq}+00:00",
        }
        rows.extend(
            (
                f"id: {seq}\n".encode(),
                f"event: {event_type}\n".encode(),
                b"data: " + runner._canonical_json(payload) + b"\n",
                b"\n",
            )
        )

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(rows)

    observed: dict[str, Any] = {}

    def fake_open(request: Any, *, timeout: float) -> Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(runner._HTTP_OPENER, "open", fake_open)
    receipt = runner._assignment_budget_event_receipt(
        "http://127.0.0.1:18100",
        "platform-token",
        assignment_id=assignment_id,
        session_id=session_id,
        relay_cursor=2,
    )

    assert receipt["status"] == "complete"
    assert receipt["scanned_through_cursor"] == 2
    assert receipt["budget_exceeded"] is True
    assert receipt["event_seq"] == 2
    assert receipt["preceding_usage_event_seq"] == 1
    assert receipt["event_aggregate"] == {
        "attempted_calls": 1,
        "recorded_calls": 1,
        "unknown_calls": 0,
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cost_usd": 0.01,
    }
    assert receipt["post_cap_usage_event_count"] == 0
    assert observed["timeout"] == 10.0
    assert observed["request"].get_header("Authorization") == "Bearer platform-token"


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
        standalone_observability_snapshot=(
            runner._token_monitor_observability_projection(snapshot)
        ),
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
    active = _write_test_active_run(
        tmp_path,
        seed="202",
        application_id="c44b3387-d780-4986-b3d6-dc112851983b",
        connection_id="e88af94b-2a0e-42fe-80c4-0a61ed105617",
        assignment_id="fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        build_id="d1bb4250-7f94-47f2-9a06-d14870c4516d",
        session_id="6ea5edda-23dd-4186-9981-948788bff0e8",
    )

    path = runner._active_run_path(tmp_path, "202")
    assert active == runner._read_private_json(path)

    assert active["task_id"] == runner.TASK_ID
    assert active["seed"] == "202"
    assert active["collaboration_policy"] == "manual"
    assert active["operational_permission_policy"] == "task_local_workspace"
    assert active["assignment_id"] == "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any("token" in key or "secret" in key for key in active)


def test_historical_identity_ledger_rejects_fresh_attempt_id_reuse(
    tmp_path: Path,
) -> None:
    common = {
        "seed": "202",
        "application_id": "c44b3387-d780-4986-b3d6-dc112851983b",
        "connection_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "build_id": "d1bb4250-7f94-47f2-9a06-d14870c4516d",
        "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
    }
    _write_test_active_run(tmp_path, **common)
    with pytest.raises(runner.EnterpriseExperimentError, match="reused historical identity"):
        _write_test_active_run(
            tmp_path,
            **common,
            attempt_started_at="2026-08-01T00:01:00+00:00",
        )


def test_assignment_identity_rejects_cross_attempt_session_or_build_rebinding() -> None:
    assignment = {
        "assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        "application_id": "c44b3387-d780-4986-b3d6-dc112851983b",
        "build_id": "d1bb4250-7f94-47f2-9a06-d14870c4516d",
        "session_id": "6ea5edda-23dd-4186-9981-948788bff0e8",
        "connection_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
    }

    runner._assert_assignment_identity(
        assignment,
        assignment_id=assignment["assignment_id"],
        application_id=assignment["application_id"],
        connection_id=assignment["connection_id"],
        session_id=assignment["session_id"],
        build_id=assignment["build_id"],
    )
    with pytest.raises(runner.EnterpriseExperimentError, match="session_id changed"):
        runner._assert_assignment_identity(
            {**assignment, "session_id": "80fb906f-2ea1-408f-917c-b8f97d77f7ee"},
            assignment_id=assignment["assignment_id"],
            application_id=assignment["application_id"],
            connection_id=assignment["connection_id"],
            session_id=assignment["session_id"],
            build_id=assignment["build_id"],
        )


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
                    "daemon_fingerprint": "sha256:" + "a" * 64,
                }
            ]
        }

    monkeypatch.setattr(runner, "_run_bounded_subprocess", fake_run)
    monkeypatch.setattr(runner, "_request_json", fake_request)
    daemon_environment = runner._daemon_environment(
        tmp_path,
        port=18101,
        provider_configuration=_test_provider_configuration("ollama-local"),
    )
    daemon_environment["LILIES_DEEPSEEK_API_KEY"] = "must-not-reach-pair-cli"

    result = runner._pair_daemon(
        state_root=tmp_path,
        daemon_port=18101,
        platform_url="http://127.0.0.1:18100",
        platform_token="t" * 48,
        standalone_python=runner.STANDALONE_LILIES_PYTHON,
        daemon_environment=daemon_environment,
        expected_daemon_fingerprint="sha256:" + "a" * 64,
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
        "--scope",
        "lilies.observability:read",
    ]
    subprocess_options = captured["subprocess"]
    assert subprocess_options["cwd"] == runner.STANDALONE_LILIES_ROOT
    assert subprocess_options["timeout_seconds"] == runner.STANDALONE_PAIR_TIMEOUT_SECONDS
    assert subprocess_options["max_stdout_bytes"] == runner.STANDALONE_PAIR_MAX_STDOUT_BYTES
    assert subprocess_options["max_stderr_bytes"] == runner.STANDALONE_SUBPROCESS_MAX_STDERR_BYTES
    assert subprocess_options["environment"]["LILIES_MODEL_EGRESS_ENABLED"] == "false"
    assert "LILIES_DEEPSEEK_API_KEY" not in subprocess_options["environment"]
    assert "DEEPSEEK_API_KEY" not in subprocess_options["environment"]
    assert captured["path"] == "/api/v1/local-lilies/connections"
    assert captured["payload"] == {
        "idempotency_key": "exp-lilies-001.pair.18101." + "a" * 24,
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
            daemon_environment=runner._daemon_environment(
                tmp_path,
                port=18101,
                provider_configuration=_test_provider_configuration("ollama-local"),
            ),
            expected_daemon_fingerprint="sha256:" + "a" * 64,
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
            daemon_environment=runner._daemon_environment(
                tmp_path,
                port=18101,
                provider_configuration=_test_provider_configuration("ollama-local"),
            ),
            expected_daemon_fingerprint="sha256:" + "a" * 64,
        )


def test_formal_build_idempotency_is_stable_within_and_distinct_across_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    def fake_request(
        _base_url: str,
        _path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payloads.append(kwargs["value"])
        return {"assignment_id": "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"}

    monkeypatch.setattr(runner, "_request_json", fake_request)
    common = {
        "platform_url": "http://127.0.0.1:18100",
        "platform_token": "t" * 48,
        "application_id": "c44b3387-d780-4986-b3d6-dc112851983b",
        "connection_id": "e88af94b-2a0e-42fe-80c4-0a61ed105617",
        "seed": "101",
    }

    runner._start_formal_build(
        **common,
        environment_instance_id="exp-lilies-001:r28:seed-101:attempt-a",
    )
    runner._start_formal_build(
        **common,
        environment_instance_id="exp-lilies-001:r28:seed-101:attempt-a",
    )
    runner._start_formal_build(
        **common,
        environment_instance_id="exp-lilies-001:r28:seed-101:attempt-b",
    )

    assert payloads[0]["idempotency_key"] == payloads[1]["idempotency_key"]
    assert payloads[0]["idempotency_key"] != payloads[2]["idempotency_key"]
    assert payloads[0]["environment_instance_id"].endswith("attempt-a")
    assert payloads[2]["environment_instance_id"].endswith("attempt-b")


def test_application_creation_requires_public_exact_empty_draft_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_id = "c44b3387-d780-4986-b3d6-dc112851983b"
    application = {
        "id": application_id,
        "active_version": None,
        "draft_revision": 0,
    }
    draft = {
        "application_id": application_id,
        "revision": 0,
        "content_hash": "a" * 64,
        "snapshot": {
            "requirement": "",
            "capability_build_contract": None,
            "workflow": {"nodes": [], "edges": [], "viewport": {}},
            "agents": {},
            "tests": [],
        },
    }
    responses = iter((application, draft))
    monkeypatch.setattr(runner, "_request_json", lambda *_args, **_kwargs: next(responses))
    created = runner._create_application(
        "http://127.0.0.1:18100",
        "platform-token",
        seed="101",
    )
    assert created["runner_empty_draft_receipt"]["draft_revision"] == 0
    assert created["runner_empty_draft_receipt"]["receipt_digest"].startswith("sha256:")

    nonempty = json.loads(json.dumps(draft))
    nonempty["snapshot"]["workflow"]["nodes"] = [{"id": "prebuilt"}]
    responses = iter((application, nonempty))
    with pytest.raises(runner.EnterpriseExperimentError, match="exact empty draft"):
        runner._create_application(
            "http://127.0.0.1:18100",
            "platform-token",
            seed="101",
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


def _platform_verification_response(
    assignment_id: str = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
) -> dict[str, Any]:
    claim_id = "5c7803f8-c684-4fca-b9c7-11d8e5cf4f85"
    return {
        "schema_version": "1.0",
        "assignment_id": assignment_id,
        "claim_id": claim_id,
        "claim_status": "independently_verified",
        "verification": {
            "schema_version": "1.1",
            "claim_id": claim_id,
            "verification_id": "6331b53a-3211-46a8-ae72-f6fedabfc43c",
            "verdict": "independently_verified",
            "oracle_digest": "sha256:" + "6" * 64,
            "differences": [],
            "task_package_digest": "sha256:" + "1" * 64,
            "environment_ready_digest": "sha256:" + "2" * 64,
            "archive_manifest_digest": "sha256:" + "3" * 64,
            "frozen_context_digest": "sha256:" + "4" * 64,
            "verification_process_digest": "sha256:" + "5" * 64,
            "validation_mode": "real_host",
        },
        "stable_progress": {
            "stable_hidden_runs": 3,
            "consecutive_passes": 3,
            "progress_digest": "sha256:" + "7" * 64,
            "stable_verdict": None,
        },
    }


def test_runner_projects_qualified_platform_verification_without_hidden_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    assignment_id = "fe6d38a5-cdae-4ef2-be70-b796c871ea4e"

    def fake_request(
        _base_url: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed["path"] = path
        observed.update(kwargs)
        return _platform_verification_response(assignment_id)

    monkeypatch.setattr(runner, "_request_json", fake_request)

    result = runner._run_platform_independent_verification(
        "http://127.0.0.1:18100",
        "t" * 48,
        assignment_id=assignment_id,
    )

    assert observed["path"].endswith(f"/assignments/{assignment_id}/independent-verification")
    assert observed["method"] == "POST"
    assert result["schema_version"] == "1.1"
    assert result["claim_status"] == "independently_verified"
    assert result["difference_count"] == 0
    assert result["archive_manifest_digest"] == "sha256:" + "3" * 64
    assert result["verification_process_digest"] == "sha256:" + "5" * 64
    assert result["validation_mode"] == "real_host"
    assert result["stable_hidden_runs"] == 3
    assert result["consecutive_passes"] == 3
    assert "differences" not in result
    assert result["receipt_digest"] == runner._digest(
        runner._canonical_json(
            {key: value for key, value in result.items() if key != "receipt_digest"}
        )
    )


@pytest.mark.parametrize("mutation", ("missing_digest", "schema_v1"))
def test_platform_verifier_rejects_incomplete_or_downgraded_frozen_context(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _platform_verification_response()
    if mutation == "missing_digest":
        response["verification"].pop("archive_manifest_digest")
    else:
        response["verification"]["schema_version"] = "1.0"
    monkeypatch.setattr(runner, "_request_json", lambda *_args, **_kwargs: response)

    with pytest.raises(runner.EnterpriseExperimentError, match="schema-1.1"):
        runner._run_platform_independent_verification(
            "http://127.0.0.1:18100",
            "t" * 48,
            assignment_id=response["assignment_id"],
        )


def test_platform_verifier_rejects_swapped_verification_process_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _platform_verification_response()
    response["stable_progress"]["stable_verdict"] = {
        "verdict": "stably_independently_verified",
        "task_id": runner.TASK_ID,
        "revision": runner.REVISION,
        "verification_process_digest": "sha256:" + "5" * 64,
        "qualification_digest": "sha256:" + "8" * 64,
        "verdict_digest": "sha256:" + "9" * 64,
    }
    response["verification"]["verification_process_digest"] = response["verification"][
        "task_package_digest"
    ]
    monkeypatch.setattr(runner, "_request_json", lambda *_args, **_kwargs: response)

    with pytest.raises(runner.EnterpriseExperimentError, match="process binding"):
        runner._run_platform_independent_verification(
            "http://127.0.0.1:18100",
            "t" * 48,
            assignment_id=response["assignment_id"],
        )


def test_platform_verifier_rejects_inconsistent_claim_and_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _platform_verification_response()
    response["verification"]["verdict"] = "verification_failed"
    monkeypatch.setattr(runner, "_request_json", lambda *_args, **_kwargs: response)

    with pytest.raises(runner.EnterpriseExperimentError, match="schema-1.1"):
        runner._run_platform_independent_verification(
            "http://127.0.0.1:18100",
            "t" * 48,
            assignment_id="fe6d38a5-cdae-4ef2-be70-b796c871ea4e",
        )


def test_run_and_resume_bind_builder_provenance_only_after_independent_verification() -> None:
    for function in (runner.run_seed, runner.resume_seed):
        source = inspect.getsource(function)
        verifier_index = source.index("_run_platform_independent_verification(")
        provenance_index = source.index("_builder_provenance_receipt(")
        assert source.count("_builder_provenance_receipt(") == 1
        assert verifier_index < provenance_index
        assert "qualified_verification=platform_verification" in source


def test_enterprise_status_never_overrides_runner_terminal_or_verifier_failure() -> None:
    host = {"verdict": "independently_verified"}
    platform = {
        "verdict": "independently_verified",
        "claim_status": "independently_verified",
    }
    budget = {
        "status": "within_cap_complete_usage",
        "runtime_cap_attested": True,
        "sequence": _runner_complete_session_budget_sequence(),
    }

    assert (
        runner._enterprise_run_status(
            {"phase": "completed", "status": "completed", "daemon_status": "completed"},
            host_verification=host,
            platform_verification=platform,
            session_budget=budget,
        )
        == "enterprise_run_passed"
    )

    assert (
        runner._enterprise_run_status(
            {
                "phase": "completed",
                "runner_terminal": "relay_security_boundary_rejected",
            },
            host_verification=host,
            platform_verification=platform,
            session_budget=budget,
        )
        == "assignment_relay_security_rejected"
    )
    assert (
        runner._enterprise_run_status(
            {"phase": "completed", "status": "completed", "daemon_status": "completed"},
            host_verification=host,
            platform_verification={
                "verdict": "independently_verified",
                "claim_status": "verification_failed",
            },
            session_budget=budget,
        )
        == "assignment_completed_verification_failed"
    )
    assert (
        runner._enterprise_run_status(
            {"phase": "completed", "status": "completed", "daemon_status": "completed"},
            host_verification=host,
            platform_verification=platform,
            session_budget=None,
        )
        == "assignment_session_budget_unverified"
    )
    for inconsistent in (
        {"phase": "completed", "status": "failed", "daemon_status": "completed"},
        {"phase": "completed", "status": "completed", "daemon_status": "error"},
        {"phase": "completed", "status": "succeeded", "daemon_status": "completed"},
    ):
        assert (
            runner._enterprise_run_status(
                inconsistent,
                host_verification=host,
                platform_verification=platform,
                session_budget=budget,
            )
            == "assignment_terminal_status_inconsistent"
        )
    assert (
        runner._enterprise_run_status(
            {"phase": "completed", "status": "completed", "daemon_status": "completed"},
            host_verification=host,
            platform_verification=platform,
            session_budget={
                "status": "within_cap_complete_usage",
                "runtime_cap_attested": True,
                "sequence": {"status": "incomplete"},
            },
        )
        == "assignment_session_budget_unverified"
    )


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
    assert evidence["error"]["code"] == "enterprise_experiment_error"
    assert evidence["error"]["summary"] == runner.ERROR_PROJECTIONS["enterprise_experiment_error"]
    assert "DEEPSEEK_API_KEY" not in json.dumps(evidence)
    assert evidence["error"]["digest"].startswith("sha256:")
    assert "secret" not in json.dumps(evidence["secret_receipts"])
    lifecycle = evidence["lifecycle"]
    assert lifecycle["private_reasoning_captured"] is False
    assert [span["phase"] for span in lifecycle["spans"]] == list(runner.LIFECYCLE_PHASES)
    assert lifecycle["spans"][0]["outcome"] == "failed"
    assert all(span["outcome"] == "skipped" for span in lifecycle["spans"][1:-1])
    assert lifecycle["spans"][-1]["outcome"] == "not_required"


def test_run_failure_after_environment_up_always_calls_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "authorized-provider-key")
    monkeypatch.setattr(
        runner,
        "_verify_standalone_lilies_runtime",
        _runner_runtime_identity,
    )
    commands: list[str] = []

    def fake_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: dict[str, str],
    ) -> None:
        assert environment is not None
        commands.append(arguments[0])
        if arguments[0] == "initialize":
            raise runner.EnterpriseExperimentError("injected initialization failure")

    monkeypatch.setattr(runner, "_environment_command", fake_environment_command)
    evidence_root = tmp_path / "evidence"

    result = runner.run_seed(
        Namespace(
            state_root=tmp_path / "state",
            evidence_root=evidence_root,
            seed="101",
            platform_port=18100,
            daemon_port=18101,
            collaboration_policy="manual",
            operational_permission_policy="task_local_workspace",
            deadline_seconds=10_800,
            token_monitor_interval=5.0,
            enable_model_egress=True,
        )
    )

    evidence = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert result == 2
    assert commands == ["config", "reset", "up", "initialize", "down"]
    assert evidence["lifecycle"]["spans"][0]["outcome"] == "failed"
    assert evidence["lifecycle"]["spans"][-1]["outcome"] == "completed"


def test_keyboard_interrupt_after_environment_up_cleans_down_and_writes_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "authorized-provider-key")
    monkeypatch.setattr(
        runner,
        "_verify_standalone_lilies_runtime",
        _runner_runtime_identity,
    )
    commands: list[str] = []

    def interrupt_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: dict[str, str],
    ) -> None:
        assert "DEEPSEEK_API_KEY" not in environment
        commands.append(arguments[0])
        if arguments[0] == "initialize":
            raise KeyboardInterrupt

    monkeypatch.setattr(
        runner,
        "_environment_command",
        interrupt_environment_command,
    )
    evidence_root = tmp_path / "evidence"

    with pytest.raises(KeyboardInterrupt):
        runner.run_seed(
            Namespace(
                state_root=tmp_path / "state",
                evidence_root=evidence_root,
                seed="101",
                platform_port=18100,
                daemon_port=18101,
                collaboration_policy="manual",
                operational_permission_policy="task_local_workspace",
                deadline_seconds=10_800,
                token_monitor_interval=5.0,
                enable_model_egress=True,
            )
        )

    evidence = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert commands == ["config", "reset", "up", "initialize", "down"]
    assert evidence["status"] == "run_failed"
    assert evidence["error"]["code"] == "interrupted"
    assert evidence["lifecycle"]["spans"][0]["outcome"] == "failed"
    assert evidence["lifecycle"]["spans"][-1]["outcome"] == "completed"


def test_resume_invalid_active_identity_writes_safe_evidence_without_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    runner._atomic_private_json(
        runner._active_run_path(state_root, "101"),
        {
            "schema_version": "invalid",
            "task_id": runner.TASK_ID,
            "revision": runner.REVISION,
            "seed": "101",
            "updated_at": "2020-01-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        runner,
        "_environment_command",
        lambda *_args, **_kwargs: pytest.fail("invalid identity touched environment"),
    )
    evidence_root = tmp_path / "evidence"

    result = runner.resume_seed(
        Namespace(
            state_root=state_root,
            evidence_root=evidence_root,
            seed="101",
            deadline_seconds=10_800,
            token_monitor_interval=5.0,
            enable_model_egress=False,
        )
    )

    evidence = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert result == 2
    assert evidence["started_at"] != "2020-01-01T00:00:00+00:00"
    assert evidence["application_id"] is None
    assert evidence["connection"] is None
    assert evidence["assignment"] is None
    assert evidence["discovery"] is None
    assert evidence["lifecycle"]["spans"][0]["outcome"] == "failed"
    assert evidence["lifecycle"]["spans"][-1]["outcome"] == "not_required"


def test_resume_setup_failure_after_up_downs_and_gets_fresh_attempt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    active = _write_test_active_run(
        state_root,
        seed="101",
        application_id="5272d96e-6eb9-42bc-8c19-359bde02aba0",
        connection_id="5aa9ec46-b715-4fda-bdee-b5f8455ec0f7",
        assignment_id="ed109124-58d6-4c95-87a4-d41280e6f3f8",
        build_id="ac026f30-d125-480e-b497-091bbf1b36bf",
        session_id="80fb906f-2ea1-408f-917c-b8f97d77f7ee",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "authorized-provider-key")
    monkeypatch.setattr(
        runner,
        "_verify_standalone_lilies_runtime",
        lambda: active["runtime_identity"],
    )
    monkeypatch.setattr(runner, "_runner_secrets", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runner,
        "_platform_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runner.EnterpriseExperimentError("injected platform setup failure")
        ),
    )
    commands: list[str] = []

    def fake_environment_command(
        _state_root: Path,
        *arguments: str,
        environment: dict[str, str],
    ) -> None:
        assert environment is not None
        commands.append(arguments[0])

    monkeypatch.setattr(runner, "_environment_command", fake_environment_command)
    evidence_root = tmp_path / "evidence"
    args = Namespace(
        state_root=state_root,
        evidence_root=evidence_root,
        seed="101",
        deadline_seconds=10_800,
        token_monitor_interval=5.0,
        enable_model_egress=True,
    )

    assert runner.resume_seed(args) == 2
    first = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert runner.resume_seed(args) == 2
    latest = json.loads((evidence_root / "seed-101.json").read_bytes())

    assert commands == ["up", "down", "up", "down"]
    assert first["started_at"] != "2020-01-01T00:00:00+00:00"
    assert latest["attempt_id"] == first["attempt_id"] == active["run_attempt_id"]
    assert latest["previous_attempt_id"] is None
    assert latest["continuation_index"] == 1
    assert len(list((evidence_root / "attempts" / "seed-101").glob("*.json"))) == 1
    assert (
        len(
            list(
                (
                    evidence_root
                    / "attempt-continuations"
                    / "seed-101"
                    / active["run_attempt_id"].removeprefix("sha256:")
                ).glob("*.json")
            )
        )
        == 1
    )
    assert latest["lifecycle"]["spans"][-1]["outcome"] == "completed"


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


def test_run_evidence_persists_only_safe_discovery_projection(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    safe_discovery = {
        "status": "available",
        "base_url": "http://127.0.0.1:18101",
        "daemon_fingerprint": "sha256:" + "a" * 64,
        "pid": 4321,
    }
    discovery = {**safe_discovery, "pairing_code": "must-not-persist"}

    runner._write_run_evidence(
        evidence_root,
        seed="101",
        started_at="2026-08-01T00:00:00+00:00",
        status="run_failed",
        package=None,
        application=None,
        connection=None,
        assignment=None,
        secret_receipts=(),
        host_snapshots=(),
        platform_verification=None,
        host_verification=None,
        error="injected failure",
        discovery=discovery,
    )

    evidence = json.loads((evidence_root / "seed-101.json").read_bytes())
    assert evidence["discovery"] == {
        **safe_discovery,
        "provider_identity": None,
        "managed_ollama": None,
        "local_model_authorization": None,
    }
    assert set(evidence["discovery"]) == {
        "status",
        "base_url",
        "daemon_fingerprint",
        "pid",
        "provider_identity",
        "managed_ollama",
        "local_model_authorization",
    }


def test_error_projection_emits_only_fixed_category_safe_text_and_digest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = "deepseek-secret-value-that-must-never-persist"
    monkeypatch.setenv("DEEPSEEK_API_KEY", credential)

    projection = runner._safe_error_projection(
        RuntimeError(
            f"provider failed with api_key={credential} and Bearer {credential}; "
            "ORACLE_EXPECTED_7 expected=secret actual=/private/tmp/customer.json "
            "https://internal.invalid/path short_secret=x7; " + "x" * 1_000
        )
    )

    assert projection is not None
    encoded = json.dumps(projection)
    assert credential not in encoded
    assert "provider failed" not in encoded
    assert "ORACLE_EXPECTED_7" not in encoded
    assert "expected" not in encoded
    assert "/private/tmp" not in encoded
    assert "internal.invalid" not in encoded
    assert "x7" not in encoded
    assert projection["summary"] == runner.ERROR_PROJECTIONS["runner_error"]
    assert projection["digest"].startswith("sha256:")

    runner._print_safe_error(
        RuntimeError(f"provider failed with token={credential} and Bearer {credential}")
    )
    stderr = capsys.readouterr().err
    emitted = json.loads(stderr)
    assert credential not in stderr
    assert set(emitted) == {"code", "summary", "digest"}
    assert emitted["summary"] == runner.ERROR_PROJECTIONS["runner_error"]
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "print(str(error), file=sys.stderr)" not in source


def test_token_monitor_evidence_never_reuses_a_legacy_or_other_attempt_latest(
    tmp_path: Path,
) -> None:
    attempt_id = "sha256:" + "a" * 64
    runner._atomic_private_json(
        tmp_path / "monitoring" / "token-monitor.latest.json",
        {"attempt_id": attempt_id, "observed_at": "stale"},
    )

    missing = runner._token_monitor_evidence(
        tmp_path,
        interval_seconds=5.0,
        attempt_id=attempt_id,
    )
    assert missing["status"] == "missing"

    runner._atomic_private_json(
        runner._token_monitor_root(tmp_path, attempt_id) / "token-monitor.latest.json",
        {"attempt_id": "sha256:" + "b" * 64, "observed_at": "other"},
    )
    mismatch = runner._token_monitor_evidence(
        tmp_path,
        interval_seconds=5.0,
        attempt_id=attempt_id,
    )
    assert mismatch["status"] == "identity_mismatch"


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
