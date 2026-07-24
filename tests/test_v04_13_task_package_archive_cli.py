from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_platform.task_packages import (
    WORKSPACE_MANIFEST_FILE,
    TaskPackageManager,
    WorkspaceRole,
)
from tests.test_v04_13_task_packages import (
    _archive_files,
    _build_formal_assignment,
    _claim_binding,
    _manager_and_package,
    _run_real_preflight,
    _snapshot,
)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_platform.task_package_cli", *args],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_archive_inputs(root: Path, files: dict[str, bytes]) -> None:
    for relative, payload in files.items():
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def _base_archive_args(
    manager: TaskPackageManager,
    package: Any,
    *,
    run_id: str,
    input_dir: Path,
    status: str = "failed",
) -> list[str]:
    return [
        "archive",
        "--state-root",
        str(manager.state_root),
        "--task-id",
        package.task.task_id,
        "--revision",
        str(package.task.revision),
        "--run-id",
        run_id,
        "--status",
        status,
        "--validation-mode",
        "real_host",
        "--input-dir",
        str(input_dir),
    ]


def test_archive_cli_produces_a_replayable_claim_bound_manifest_digest(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    run_id = f"run-archive-cli-{uuid4().hex}"
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id="environment:archive-cli",
    )
    workspace = tmp_path / "lilies-workspace"
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
    input_dir = tmp_path / "run-input"
    _write_archive_inputs(
        input_dir,
        _archive_files(
            snapshot,
            binding,
            package=package,
            run_id=run_id,
            assignment=assignment,
        ),
    )
    claim_path = tmp_path / "claim-binding.json"
    claim_path.write_text(
        json.dumps(
            binding.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    claim_path.chmod(0o600)

    result = _run_cli(
        *_base_archive_args(
            manager,
            package,
            run_id=run_id,
            input_dir=input_dir,
            status="succeeded",
        ),
        "--environment-ready",
        str(ready_path),
        "--workspace-manifest",
        str(workspace / WORKSPACE_MANIFEST_FILE),
        "--claim-binding",
        str(claim_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    digest = payload["archive_manifest_digest"]
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert payload["manifest"]["status"] == "succeeded"
    replayed = manager.replay_registered_run(
        package.task.task_id,
        package.task.revision,
        run_id,
        expected_manifest_digest=digest,
    )
    assert replayed.claim_binding == binding


@pytest.mark.parametrize(
    "unsafe_entry",
    ["root-symlink", "file-symlink", "directory-symlink", "hardlink", "fifo"],
)
def test_archive_cli_rejects_unsafe_or_escaping_input_entries(
    tmp_path: Path,
    unsafe_entry: str,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    input_dir = tmp_path / "run-input"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.json").write_text("{}", encoding="utf-8")
    if unsafe_entry == "root-symlink":
        input_dir.symlink_to(outside, target_is_directory=True)
    else:
        input_dir.mkdir()
    if unsafe_entry == "file-symlink":
        (input_dir / "result.json").symlink_to(outside / "payload.json")
    elif unsafe_entry == "directory-symlink":
        (input_dir / "artifacts").symlink_to(outside, target_is_directory=True)
    elif unsafe_entry == "hardlink":
        os.link(outside / "payload.json", input_dir / "result.json")
    else:
        os.mkfifo(input_dir / "result.json")
    run_id = f"run-unsafe-input-{unsafe_entry}"

    result = _run_cli(
        *_base_archive_args(
            manager,
            package,
            run_id=run_id,
            input_dir=input_dir,
        )
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "task_package_rejected"
    assert not (package.root / "runs" / run_id).exists()


@pytest.mark.parametrize(
    "unsafe_claim",
    ["symlink", "hardlink", "group-readable", "duplicate-key"],
)
def test_archive_cli_rejects_unsafe_claim_binding_files(
    tmp_path: Path,
    unsafe_claim: str,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    input_dir = tmp_path / "run-input"
    input_dir.mkdir()
    (input_dir / "result.json").write_text('{"status":"failed"}', encoding="utf-8")
    claim_target = tmp_path / "claim-target.json"
    claim_target.write_text(
        '{"claim_id":"00000000-0000-0000-0000-000000000000"}',
        encoding="utf-8",
    )
    claim_target.chmod(0o600)
    claim_path = tmp_path / "claim-binding.json"
    if unsafe_claim == "symlink":
        claim_path.symlink_to(claim_target)
    elif unsafe_claim == "hardlink":
        os.link(claim_target, claim_path)
    elif unsafe_claim == "group-readable":
        claim_path.write_bytes(claim_target.read_bytes())
        claim_path.chmod(0o640)
    else:
        claim_path.write_text('{"claim_id":"one","claim_id":"two"}', encoding="utf-8")
        claim_path.chmod(0o600)
    run_id = f"run-unsafe-claim-{unsafe_claim}"

    result = _run_cli(
        *_base_archive_args(
            manager,
            package,
            run_id=run_id,
            input_dir=input_dir,
        ),
        "--claim-binding",
        str(claim_path),
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "task_package_rejected"
    assert not (package.root / "runs" / run_id).exists()


def test_archive_cli_rejects_input_that_overlaps_frozen_state(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    run_id = "run-state-overlap"

    result = _run_cli(
        *_base_archive_args(
            manager,
            package,
            run_id=run_id,
            input_dir=package.root,
        )
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "task_package_rejected"
    assert not (package.root / "runs" / run_id).exists()
