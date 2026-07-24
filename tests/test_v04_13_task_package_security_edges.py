from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

import agent_platform.task_packages as task_packages_module
from agent_platform.lilies_tools import (
    LiliesToolContext,
    LiliesToolError,
    build_lilies_core_registry,
)
from agent_platform.task_package_cli import main as task_package_cli_main
from agent_platform.task_packages import (
    WORKSPACE_MANIFEST_FILE,
    ArchiveStatus,
    TaskPackageConflict,
    TaskPackageError,
    TaskPackageManager,
    TaskPackageSecurityError,
    ValidationMode,
    WorkspaceRole,
)
from tests.test_v04_13_task_packages import (
    DIGEST_A,
    ORACLE_MARKER,
    TASK_ID,
    _archive_files,
    _build_formal_assignment,
    _claim_binding,
    _claim_for_archive,
    _json_bytes,
    _make_task_source,
    _manager_and_package,
    _run_real_preflight,
    _sha256,
    _snapshot,
    _successful_archive,
    _write_json,
    _write_yaml,
)


def _canonical_json_variant(payload: bytes) -> bytes:
    return json.dumps(
        json.loads(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    ).encode()


def _nfkc_compatibility_variant(payload: bytes) -> bytes:
    return "".join(
        chr(ord(character) + 0xFEE0) if 0x21 <= ord(character) <= 0x7E else character
        for character in payload.decode("utf-8")
    ).encode("utf-8")


@pytest.mark.parametrize(
    "generated_entry",
    ["runs", "archive-manifest.json"],
)
def test_source_package_rejects_manager_owned_generated_entries(
    tmp_path: Path,
    generated_entry: str,
) -> None:
    source = _make_task_source(tmp_path / "source")
    path = source / generated_entry
    if generated_entry == "runs":
        path.mkdir()
    else:
        _write_json(path, {"runs": []})

    with pytest.raises(TaskPackageError, match="unknown package root entries"):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_source_package_rejects_oracle_marker_in_public_file(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    (source / "requirement.md").write_text(
        f"Public requirement accidentally contains {ORACLE_MARKER}.\n",
        encoding="utf-8",
    )

    with pytest.raises(
        TaskPackageSecurityError,
        match="public task file contains protected oracle material",
    ):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


@pytest.mark.parametrize(
    ("encoding", "wrap"),
    [
        ("exact", lambda payload: payload),
        ("canonical", _canonical_json_variant),
        ("embedded", lambda payload: b"Leaked evaluator contract follows:\n" + payload),
        ("base64", base64.b64encode),
        ("urlsafe-base64", base64.urlsafe_b64encode),
        ("hex", lambda payload: payload.hex().encode("ascii")),
        ("json-string", lambda payload: json.dumps(payload.decode("utf-8")).encode("utf-8")),
        ("nfkc", _nfkc_compatibility_variant),
    ],
)
def test_oracle_contract_is_rejected_before_freeze_or_lilies_workspace(
    tmp_path: Path,
    encoding: str,
    wrap: Any,
) -> None:
    source = _make_task_source(tmp_path / "source")
    oracle_path = source / "protected" / "oracle" / "oracle.json"
    raw = oracle_path.read_bytes()
    (source / "requirement.md").write_bytes(wrap(raw))
    manager = TaskPackageManager(tmp_path / "state")
    workspace = tmp_path / f"lilies-workspace-{encoding}"

    with pytest.raises(
        TaskPackageSecurityError,
        match="public task file contains protected oracle material",
    ):
        manager.freeze_revision(source)

    assert manager.has_frozen_revision(TASK_ID, 1) is False
    with pytest.raises(TaskPackageError, match="frozen task revision is not registered"):
        manager.load_frozen(TASK_ID, 1)
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("leak_kind", "wrap"),
    [
        ("exact", lambda payload: payload),
        ("embedded", lambda payload: b"prefix\n" + payload + b"\nsuffix"),
        ("base64", base64.b64encode),
        ("json-string", lambda payload: json.dumps(payload.decode()).encode()),
    ],
)
def test_raw_oracle_contract_in_run_archive_invalidates_the_run(
    tmp_path: Path,
    leak_kind: str,
    wrap: Any,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    run_id = "run-raw-oracle-leak"
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id="environment:raw-oracle-leak",
    )
    workspace = tmp_path / "leak-test-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=binding.assignment_id,
    )
    leaked_oracle = wrap((package.root / "protected" / "oracle" / "oracle.json").read_bytes())
    _, manifest, _ = manager.archive_run(
        package,
        run_id=run_id,
        status=ArchiveStatus.succeeded,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=ready_path,
        workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
        files={
            **_archive_files(
                snapshot,
                binding,
                package=package,
                run_id=run_id,
                assignment=assignment,
            ),
            f"debug/oracle-copy-{leak_kind}.txt": leaked_oracle,
        },
        claim_binding=binding,
    )

    assert manifest.status is ArchiveStatus.invalid
    assert manifest.security_findings == [
        f"protected_oracle_content:debug/oracle-copy-{leak_kind}.txt"
    ]


@pytest.mark.parametrize(
    "secret_ref",
    ["sk-plain-text-token", "vault:paperless-test-token"],
)
def test_environment_lock_rejects_plaintext_or_non_secret_references(
    tmp_path: Path,
    secret_ref: str,
) -> None:
    source = _make_task_source(tmp_path / "source")
    environment_path = source / "environment.lock"
    environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
    environment["secret_refs"] = [secret_ref]
    environment_payload = _write_yaml(environment_path, environment)

    task_path = source / "task.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    task["environment_lock_digest"] = _sha256(environment_payload)
    _write_yaml(task_path, task)

    with pytest.raises(ValidationError):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_frozen_revision_rejects_permission_only_drift(tmp_path: Path) -> None:
    manager, package = _manager_and_package(tmp_path)
    frozen_requirement = package.root / "requirement.md"
    original = frozen_requirement.read_bytes()
    frozen_requirement.chmod(0o600)

    with pytest.raises(
        TaskPackageConflict,
        match="permissions changed",
    ):
        manager.load_frozen(TASK_ID, 1)
    assert frozen_requirement.read_bytes() == original


@pytest.mark.parametrize(
    "run_id",
    ["../run-escape", " run-with-spaces "],
)
def test_archive_rejects_noncanonical_run_identity(
    tmp_path: Path,
    run_id: str,
) -> None:
    manager, package = _manager_and_package(tmp_path)

    with pytest.raises(ValidationError):
        manager.archive_run(
            package,
            run_id=run_id,
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files={"result.json": b'{"status":"failed"}'},
            claim_binding=None,
        )


def test_replay_cli_rejects_parent_segment_run_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, package = _manager_and_package(tmp_path)

    exit_code = task_package_cli_main(
        [
            "replay",
            "--state-root",
            str(manager.state_root),
            "--task-id",
            package.task.task_id,
            "--revision",
            str(package.task.revision),
            "--run-id",
            "../run-escape",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["code"] == "task_package_rejected"


def _remove_archive_tree(run_root: Path) -> None:
    for directory in sorted(
        (path for path in run_root.rglob("*") if path.is_dir()),
        reverse=True,
    ):
        os.chmod(directory, 0o700)
    os.chmod(run_root, 0o700)
    shutil.rmtree(run_root)


@pytest.mark.parametrize(
    "tamper",
    ["drop-index-history", "change-index-digest", "delete-run-directory"],
)
def test_archive_registry_tampering_fails_closed_for_find_replay_and_claim(
    tmp_path: Path,
    tamper: str,
) -> None:
    archive = _successful_archive(tmp_path)
    manager: TaskPackageManager = archive["manager"]
    package = archive["package"]
    run_root: Path = archive["run_root"]
    manifest_digest: str = archive["manifest_digest"]
    claim = _claim_for_archive(archive)

    manager.archive_run(
        package,
        run_id="run-after-claimed-history",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": _json_bytes({"status": "failed"})},
        claim_binding=None,
    )
    index_path = package.root / "archive-manifest.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["run_id"] for entry in index["runs"]] == [
        run_root.name,
        "run-after-claimed-history",
    ]

    if tamper == "drop-index-history":
        index["runs"] = index["runs"][1:]
        _write_json(index_path, index)
    elif tamper == "change-index-digest":
        index["runs"][0]["manifest_digest"] = DIGEST_A
        _write_json(index_path, index)
    else:
        _remove_archive_tree(run_root)

    with pytest.raises(TaskPackageError):
        manager.find_archive_by_digest(
            package.task.task_id,
            package.task.revision,
            manifest_digest,
        )
    with pytest.raises(TaskPackageError):
        manager.replay_registered_run(
            package.task.task_id,
            package.task.revision,
            run_root.name,
            expected_manifest_digest=manifest_digest,
        )
    with pytest.raises(TaskPackageError):
        manager.validate_claim_binding(
            task_id=package.task.task_id,
            revision=package.task.revision,
            claim=claim,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "cannot be empty"),
        (b'{"kind":"missing-sequence"}\n', "not contiguous"),
        (b"\n", "blank line"),
        (b'{"seq":true}\n', "not contiguous"),
        (b'{"seq":99,"seq":1}\n', "invalid strict record"),
        (b'{"seq":1,"payload":NaN}\n', "invalid strict record"),
    ],
)
def test_archive_replay_rejects_empty_or_unsequenced_jsonl(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    run_root, _, _ = manager.archive_run(
        package,
        run_id="run-invalid-jsonl",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={
            "messages.jsonl": payload,
            "result.json": b'{"status":"failed"}',
        },
        claim_binding=None,
    )

    with pytest.raises(TaskPackageConflict, match=message):
        manager.replay_archive(run_root)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("application_id", uuid4()),
        ("published_version", 99),
        ("test_run_ids", ["test-run:forged"]),
        ("business_run_ids", ["business-run:forged"]),
        ("resolved_report_ids", [uuid4()]),
        ("remaining_limits", ["forged-limit"]),
    ],
)
def test_successful_archive_rejects_claim_fields_not_proven_by_typed_sources(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    run_id = f"run-forged-binding-{field.replace('_', '-')}"
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id=f"environment:{field}",
    )
    workspace = tmp_path / f"workspace-{field}"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=binding.assignment_id,
    )
    files = _archive_files(
        snapshot,
        binding,
        package=package,
        run_id=run_id,
        assignment=assignment,
    )
    forged = binding.model_copy(update={field: replacement})

    with pytest.raises(TaskPackageConflict):
        manager.archive_run(
            package,
            run_id=run_id,
            status=ArchiveStatus.succeeded,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=ready_path,
            workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
            files=files,
            claim_binding=forged,
        )


def test_successful_archive_rejects_missing_artifact_or_receipt_bytes(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    run_id = "run-missing-frozen-output"
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id="environment:missing-output",
    )
    workspace = tmp_path / "workspace-missing-output"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=binding.assignment_id,
    )
    files = _archive_files(
        snapshot,
        binding,
        package=package,
        run_id=run_id,
        assignment=assignment,
    )
    files.pop("artifacts/00000000-0000-4000-8000-000000000101.bin")

    with pytest.raises(
        TaskPackageConflict,
        match="artifacts do not match",
    ):
        manager.archive_run(
            package,
            run_id=run_id,
            status=ArchiveStatus.succeeded,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=ready_path,
            workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
            files=files,
            claim_binding=binding,
        )


def test_archive_rejects_path_input_changed_between_digest_and_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    volatile = tmp_path / "volatile-result.json"
    volatile.write_bytes(b'{"attempt":1,"status":"failed"}')
    original_copy = task_packages_module._copy_regular
    mutated = False

    def mutate_before_copy(source: Path, destination: Path) -> Any:
        nonlocal mutated
        if not mutated and source.resolve() == volatile.resolve():
            volatile.write_bytes(b'{"attempt":2,"status":"failed"}')
            mutated = True
        return original_copy(source, destination)

    monkeypatch.setattr(task_packages_module, "_copy_regular", mutate_before_copy)

    with pytest.raises(
        TaskPackageConflict,
        match="archive input changed while being sealed",
    ):
        manager.archive_run(
            package,
            run_id="run-input-race",
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files={"result.json": volatile},
            claim_binding=None,
        )


@pytest.mark.parametrize(
    ("field", "relative"),
    [
        ("environment_ready_digest", "environment-ready.json"),
        ("workspace_mount_digest", "workspace-mount.json"),
    ],
)
def test_archive_replay_cross_checks_control_file_digest_bindings(
    tmp_path: Path,
    field: str,
    relative: str,
) -> None:
    archive = _successful_archive(tmp_path)
    manifest_path = archive["run_root"] / "archive-manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    payload[field] = "sha256:" + "f" * 64
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(_json_bytes(payload))
    manifest_path.chmod(0o400)

    with pytest.raises(
        TaskPackageConflict,
        match=f"control-file digest changed: {relative}",
    ):
        archive["manager"].replay_archive(archive["run_root"])


def test_unicode_escaped_oracle_marker_invalidates_archive(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    escaped_marker = "".join(f"\\u{ord(character):04x}" for character in ORACLE_MARKER)
    payload = ('{"status":"failed","diagnostic":"' + escaped_marker + '"}').encode("ascii")

    _, manifest, _ = manager.archive_run(
        package,
        run_id="run-unicode-escaped-marker",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": payload},
        claim_binding=None,
    )

    assert manifest.status is ArchiveStatus.invalid
    assert manifest.security_findings == ["protected_oracle_marker:result.json"]


def test_encoded_protected_oracle_scalar_invalidates_control_surface(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    oracle_path = source / "protected/oracle/oracle.json"
    oracle = json.loads(oracle_path.read_bytes())
    hidden_scalar = "private-oracle-scalar-7f13d2c9"
    oracle["checks"][0]["expected"] = hidden_scalar
    oracle_path.write_bytes(_json_bytes(oracle))
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)
    encoded = base64.b64encode(hidden_scalar.encode("utf-8")).decode("ascii")

    _, manifest, _ = manager.archive_run(
        package,
        run_id="run-encoded-protected-scalar",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"diagnostic.json": _json_bytes({"opaque": encoded})},
        claim_binding=None,
    )

    assert manifest.status is ArchiveStatus.invalid
    assert manifest.security_findings == [
        "protected_oracle_scalar:diagnostic.json"
    ]


@pytest.mark.parametrize(
    "reserved_path",
    [
        "archive-manifest.json",
        "environment-ready.json",
        "workspace-mount.json",
        "task/requirement.md",
    ],
)
def test_archive_rejects_manager_owned_input_paths(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    manager, package = _manager_and_package(tmp_path)

    with pytest.raises(
        TaskPackageSecurityError,
        match="manager-owned path",
    ):
        manager.archive_run(
            package,
            run_id="run-reserved-input",
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files={reserved_path: b"untrusted"},
            claim_binding=None,
        )


@pytest.mark.parametrize(
    "reserved_path",
    [
        "fixtures/public-inputs/Protected/leak.txt",
        "fixtures/public-inputs/.GIT/config",
    ],
)
def test_source_package_rejects_case_variant_reserved_public_paths(
    tmp_path: Path,
    reserved_path: str,
) -> None:
    source = _make_task_source(tmp_path / "source")
    path = source / reserved_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must never enter a public mount", encoding="utf-8")

    with pytest.raises(
        TaskPackageSecurityError,
        match="public task path uses a reserved segment",
    ):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


@pytest.mark.asyncio
async def test_lilies_workspace_hides_mount_manifest_and_denies_case_variants(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    run_id = "run-hidden-mount-control"
    assignment_id = uuid4()
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_instance_id="environment:hidden-mount-control",
    )
    workspace = tmp_path / "lilies-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_ready_path=ready_path,
    )
    assert (workspace / WORKSPACE_MANIFEST_FILE).is_file()

    registry = build_lilies_core_registry()
    context = LiliesToolContext(
        session_id="formal-security-edge-session",
        workspace=workspace,
    )
    listing = await registry.get("workspace_list").execute(
        {"path": ".", "pattern": "*"},
        context,
    )
    assert WORKSPACE_MANIFEST_FILE not in listing.content

    for reserved_path in (
        WORKSPACE_MANIFEST_FILE,
        "Protected/oracle/oracle.json",
        ".GIT/config",
    ):
        with pytest.raises(LiliesToolError, match="reserved"):
            await registry.get("workspace_read").execute(
                {"path": reserved_path},
                context,
            )
