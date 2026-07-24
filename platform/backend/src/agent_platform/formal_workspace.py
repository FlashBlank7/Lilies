from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import unicodedata
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .lilies_models import (
    ArtifactRef,
    AssignmentConstraints,
    AssignmentMode,
    AssignmentNetworkPolicy,
    BuildAssignment,
    BusinessContext,
    DeliverableSpec,
    ProhibitedAction,
)
from .task_packages import (
    AllowedActionsPolicy,
    BudgetSpec,
    EnvironmentLock,
    FixtureManifest,
    MAX_ARCHIVE_FILE_BYTES,
    TaskPackageSpec,
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    WorkspaceMountManifest,
    WorkspaceRole,
    parse_task_package_yaml,
)


class FormalWorkspaceRejected(RuntimeError):
    """A public formal workspace does not match its authenticated assignment."""


_REQUIRED_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "protected",
        "oracle",
        "expected-state",
        "platform-data",
        "platform_data",
    }
)
_CONTROL_FILES = frozenset(
    {
        WORKSPACE_MANIFEST_FILE,
        WORKSPACE_POLICY_FILE,
    }
)
_FORMAL_WRITABLE_PREFIXES = ("work", "artifacts")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _safe_relative(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise FormalWorkspaceRejected("workspace path is not a POSIX relative path")
    if unicodedata.normalize("NFC", value) != value:
        raise FormalWorkspaceRejected("workspace path is not NFC-normalized")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise FormalWorkspaceRejected("workspace path escapes its public root")
    return path.as_posix()


def _identity(value: str) -> str:
    return unicodedata.normalize("NFC", _safe_relative(value)).casefold()


def _has_denied_segment(value: str) -> bool:
    denied = {
        *(_identity(item) for item in _REQUIRED_DENIED_SEGMENTS),
        *(_identity(item) for item in _CONTROL_FILES),
    }
    return any(part.casefold() in denied for part in PurePosixPath(value).parts)


def _is_formal_output_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    return bool(parts) and parts[0] in _FORMAL_WRITABLE_PREFIXES


def _read_regular(path: Path, *, limit: int = MAX_ARCHIVE_FILE_BYTES) -> bytes:
    if path.is_symlink():
        raise FormalWorkspaceRejected("workspace symlinks are forbidden")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FormalWorkspaceRejected("workspace file is not safely readable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise FormalWorkspaceRejected(
                "workspace inputs must be isolated regular files"
            )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise FormalWorkspaceRejected("workspace file exceeds its size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _resolved_child(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(_safe_relative(relative)).parts)
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise FormalWorkspaceRejected("workspace path escapes its public root")
    return candidate


def _scan_workspace(
    root: Path,
) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    identities: set[str] = set()
    for current, names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_relative = current_path.relative_to(root).as_posix()
        if current_relative != ".":
            normalized_directory = _safe_relative(current_relative)
            identity = _identity(normalized_directory)
            if identity in identities:
                raise FormalWorkspaceRejected(
                    "workspace paths collide after normalization"
                )
            identities.add(identity)
            directories.add(normalized_directory)
        for name in names:
            child = current_path / name
            metadata = child.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or child.is_symlink():
                raise FormalWorkspaceRejected(
                    "workspace directory entries must be real directories"
                )
        for name in file_names:
            child = current_path / name
            relative = _safe_relative(child.relative_to(root).as_posix())
            identity = _identity(relative)
            if identity in identities:
                raise FormalWorkspaceRejected(
                    "workspace paths collide after normalization"
                )
            identities.add(identity)
            metadata = child.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or child.is_symlink()
                or metadata.st_nlink != 1
            ):
                raise FormalWorkspaceRejected(
                    "workspace files must be isolated regular files"
                )
            files[relative] = child
    return files, directories


def _validate_public_assignment_projection(
    assignment: BuildAssignment,
    root: Path,
    manifest: WorkspaceMountManifest,
) -> None:
    task_ref = assignment.task_package
    assert task_ref is not None
    assert task_ref.environment_lock_digest is not None
    assert task_ref.allowed_actions_digest is not None
    assert task_ref.budget_digest is not None

    entries = list(manifest.entries)
    entry_paths = [entry.target_path for entry in entries]
    if entry_paths != sorted(entry_paths) or any(
        entry.logical_source != f"task-package:{entry.target_path}"
        for entry in entries
    ):
        raise FormalWorkspaceRejected(
            "formal workspace entries are not the canonical package projection"
        )
    aggregate = {
        "schema_version": "1.0",
        "entries": [
            {
                "path": entry.target_path,
                "digest": entry.digest,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
    }
    if not hmac.compare_digest(
        _digest(_canonical_json(aggregate)),
        task_ref.public_summary_digest,
    ):
        raise FormalWorkspaceRejected(
            "formal workspace public package digest differs from the assignment"
        )

    required_controls = {
        "task.yaml",
        "requirement.md",
        "environment.lock",
        "fixtures/manifest.json",
        "allowed-actions.json",
        "budget.json",
    }
    if not required_controls <= set(entry_paths):
        raise FormalWorkspaceRejected(
            "formal workspace omits required public package controls"
        )
    try:
        task_payload = _read_regular(root / "task.yaml")
        environment_payload = _read_regular(root / "environment.lock")
        fixture_payload = _read_regular(root / "fixtures/manifest.json")
        allowed_payload = _read_regular(root / "allowed-actions.json")
        budget_payload = _read_regular(root / "budget.json")
        task = TaskPackageSpec.model_validate(
            parse_task_package_yaml(task_payload)
        )
        environment = EnvironmentLock.model_validate(
            parse_task_package_yaml(environment_payload)
        )
        fixtures = FixtureManifest.model_validate_json(fixture_payload)
        allowed = AllowedActionsPolicy.model_validate_json(allowed_payload)
        budget = BudgetSpec.model_validate_json(budget_payload)
        requirement = (
            _read_regular(root / task.requirement_file).decode("utf-8").strip()
        )
    except Exception as error:
        raise FormalWorkspaceRejected(
            "formal workspace public package controls are invalid"
        ) from error

    identities = {
        (item.task_id, item.revision)
        for item in (task, environment, fixtures, allowed, budget)
    }
    if identities != {(task_ref.task_id, task_ref.revision)}:
        raise FormalWorkspaceRejected(
            "formal workspace controls disagree on the task revision"
        )
    control_digests = {
        "environment": _digest(environment_payload),
        "fixtures": _digest(fixture_payload),
        "allowed": _digest(allowed_payload),
        "budget": _digest(budget_payload),
    }
    if (
        not hmac.compare_digest(
            task.environment_lock_digest,
            control_digests["environment"],
        )
        or not hmac.compare_digest(
            task.fixture_manifest_digest,
            control_digests["fixtures"],
        )
        or not hmac.compare_digest(
            task_ref.environment_lock_digest,
            control_digests["environment"],
        )
        or not hmac.compare_digest(
            task_ref.allowed_actions_digest,
            control_digests["allowed"],
        )
        or not hmac.compare_digest(
            task_ref.budget_digest,
            control_digests["budget"],
        )
    ):
        raise FormalWorkspaceRejected(
            "formal workspace control digest differs from the assignment"
        )
    if task.source_projects != environment.source_projects:
        raise FormalWorkspaceRejected(
            "formal workspace source project locks disagree"
        )

    declared_fixture_files = {
        f"fixtures/{entry.path}": entry for entry in fixtures.files
    }
    actual_entries = {entry.target_path: entry for entry in entries}
    if set(declared_fixture_files) != {
        path
        for path in actual_entries
        if path.startswith("fixtures/public-inputs/")
    }:
        raise FormalWorkspaceRejected(
            "formal workspace fixture manifest does not cover public inputs"
        )
    for path, declared in declared_fixture_files.items():
        actual = actual_entries[path]
        if (
            declared.digest != actual.digest
            or declared.size_bytes != actual.size_bytes
        ):
            raise FormalWorkspaceRejected(
                "formal workspace fixture digest differs from its manifest"
            )
    if {
        entry.path: entry for entry in environment.fixture_files
    } != {
        entry.path: entry for entry in fixtures.files
    }:
        raise FormalWorkspaceRejected(
            "formal workspace environment fixture inventory differs"
        )

    try:
        expected_business_context = BusinessContext(
            customer_roles=[task.customer_role],
            business_goal=task.business_goal,
            inputs=[entry.path for entry in fixtures.files],
            outputs=[item.name for item in task.deliverables],
            constraints=[task.acceptance_summary],
        )
        expected_fixture_refs = [
            ArtifactRef(
                artifact_id=(
                    "fixture:"
                    + hashlib.sha256(entry.path.encode("utf-8")).hexdigest()[:32]
                ),
                digest=entry.digest,
                media_type="application/octet-stream",
                display_name=PurePosixPath(entry.path).name,
            )
            for entry in fixtures.files
        ]
        expected_deliverables = [
            DeliverableSpec(
                name=item.name,
                description=item.description,
                media_type=item.media_type,
            )
            for item in task.deliverables
        ]
        network_policy = (
            AssignmentNetworkPolicy.allowlist
            if allowed.network_hosts
            else AssignmentNetworkPolicy.none
        )
        expected_constraints = AssignmentConstraints(
            deadline_at=assignment.created_at
            + timedelta(seconds=budget.assignment_wall_clock_seconds),
            max_turns=budget.max_build_repair_turns,
            max_budget_usd=budget.max_model_cost_usd,
            max_tool_calls=budget.max_platform_tool_calls,
            network_policy=network_policy,
            allowed_hosts=allowed.network_hosts,
            allowed_actions=allowed.platform_actions,
            prohibited_actions=[
                ProhibitedAction(action)
                for action in allowed.prohibited_actions
            ],
            no_substitute_validation=True,
            readable_host_objects=allowed.readable_host_objects,
            writable_host_operations=allowed.writable_host_operations,
            model_access=allowed.model_access,
            file_access=allowed.file_access,
            connector_access=allowed.connector_access,
            permission_required_actions=allowed.permission_required_actions,
            max_write_count=allowed.max_write_count,
            max_payload_bytes=allowed.max_payload_bytes,
            compensation_actions=allowed.compensation_actions,
            max_report_evidence_rounds=budget.max_report_evidence_rounds,
            stable_hidden_runs=budget.stable_hidden_runs,
        )
    except Exception as error:  # pragma: no cover - schemas already validated
        raise FormalWorkspaceRejected(
            "formal workspace could not project its public assignment"
        ) from error
    if (
        assignment.requirement != requirement
        or assignment.business_context != expected_business_context
        or assignment.fixture_refs != expected_fixture_refs
        or assignment.deliverables != expected_deliverables
        or assignment.constraints != expected_constraints
    ):
        raise FormalWorkspaceRejected(
            "formal assignment differs from its public package projection"
        )


def validate_public_formal_workspace(
    assignment: BuildAssignment,
    workspace: Path,
) -> dict[str, str]:
    """Validate only public, assignment-bound bytes inside a Lilies workspace.

    This function deliberately has no task-package state-root parameter.  The
    standalone daemon can prove that the staged public workspace matches the
    platform-authenticated assignment without gaining a path to sealed oracle
    content or platform data.
    """

    if assignment.mode is not AssignmentMode.formal_experiment:
        raise FormalWorkspaceRejected("only formal assignments use this gate")
    task_ref = assignment.task_package
    if (
        task_ref is None
        or task_ref.run_id is None
        or task_ref.environment_ready_digest is None
        or task_ref.workspace_mount_digest is None
        or task_ref.workspace_policy_digest is None
    ):
        raise FormalWorkspaceRejected(
            "formal assignment lacks its public workspace binding"
        )
    lexical_root = Path(workspace)
    try:
        root_metadata = lexical_root.lstat()
    except OSError as error:
        raise FormalWorkspaceRejected(
            "formal workspace root is unavailable"
        ) from error
    if not stat.S_ISDIR(root_metadata.st_mode) or lexical_root.is_symlink():
        raise FormalWorkspaceRejected("formal workspace root is not a real directory")
    root = lexical_root.resolve(strict=True)
    if stat.S_IMODE(root_metadata.st_mode) & 0o022:
        raise FormalWorkspaceRejected(
            "formal workspace root is writable by group or other users"
        )

    manifest_payload = _read_regular(root / WORKSPACE_MANIFEST_FILE)
    policy_payload = _read_regular(root / WORKSPACE_POLICY_FILE)
    manifest_digest = _digest(manifest_payload)
    policy_digest = _digest(policy_payload)
    if not hmac.compare_digest(
        manifest_digest,
        task_ref.workspace_mount_digest,
    ) or not hmac.compare_digest(
        policy_digest,
        task_ref.workspace_policy_digest,
    ):
        raise FormalWorkspaceRejected(
            "formal workspace control digests differ from the assignment"
        )
    try:
        manifest = WorkspaceMountManifest.model_validate_json(manifest_payload)
        policy: Any = json.loads(policy_payload)
    except Exception as error:
        raise FormalWorkspaceRejected(
            "formal workspace controls are invalid"
        ) from error
    expected_binding = {
        "task_id": task_ref.task_id,
        "revision": task_ref.revision,
        "role": WorkspaceRole.lilies,
        "run_id": task_ref.run_id,
        "assignment_id": assignment.assignment_id,
        "public_summary_digest": task_ref.public_summary_digest,
        "environment_ready_digest": task_ref.environment_ready_digest,
        "environment_instance_id": task_ref.environment_instance_id,
    }
    if any(
        getattr(manifest, field) != expected
        for field, expected in expected_binding.items()
    ):
        raise FormalWorkspaceRejected(
            "formal workspace does not bind the exact assignment"
        )
    if manifest.archive_manifest_digest is not None:
        raise FormalWorkspaceRejected(
            "Lilies workspace cannot contain verifier archive authority"
        )
    denied = {item.casefold() for item in manifest.denied_segments}
    if not {item.casefold() for item in _REQUIRED_DENIED_SEGMENTS} <= denied:
        raise FormalWorkspaceRejected(
            "formal workspace manifest omits a mandatory denied segment"
        )
    if tuple(manifest.writable_prefixes) != _FORMAL_WRITABLE_PREFIXES:
        raise FormalWorkspaceRejected(
            "formal workspace writable prefixes are not the fixed Lilies set"
        )
    expected_policy = {
        "schema_version": "1.0",
        "denied_segments": sorted(
            _REQUIRED_DENIED_SEGMENTS | _CONTROL_FILES
        ),
        "writable_prefixes": list(_FORMAL_WRITABLE_PREFIXES),
    }
    if policy != expected_policy:
        raise FormalWorkspaceRejected(
            "formal workspace policy widens the manager projection"
        )

    files, directories = _scan_workspace(root)
    declared_paths: set[str] = set()
    allowed_directories = set(_FORMAL_WRITABLE_PREFIXES)
    for entry in manifest.entries:
        relative = _safe_relative(entry.target_path)
        if (
            not entry.read_only
            or _has_denied_segment(relative)
            or relative in _CONTROL_FILES
        ):
            raise FormalWorkspaceRejected(
                "formal workspace manifest exposes a forbidden entry"
            )
        if relative in declared_paths:
            raise FormalWorkspaceRejected(
                "formal workspace manifest repeats an entry"
            )
        declared_paths.add(relative)
        target = files.get(relative)
        if target is None:
            raise FormalWorkspaceRejected(
                "formal workspace is missing a declared public file"
            )
        payload = _read_regular(target)
        if (
            len(payload) != entry.size_bytes
            or not hmac.compare_digest(_digest(payload), entry.digest)
            or stat.S_IMODE(target.stat(follow_symlinks=False).st_mode) & 0o222
        ):
            raise FormalWorkspaceRejected(
                "formal workspace public file bytes or permissions changed"
            )
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            allowed_directories.add(parent.as_posix())
            parent = parent.parent

    expected_files = declared_paths | _CONTROL_FILES
    output_files = set(files) - expected_files
    if any(
        not _is_formal_output_path(relative)
        or _has_denied_segment(relative)
        or files[relative].stat(follow_symlinks=False).st_size
        > MAX_ARCHIVE_FILE_BYTES
        or stat.S_IMODE(
            files[relative].stat(follow_symlinks=False).st_mode
        )
        & 0o022
        for relative in output_files
    ):
        raise FormalWorkspaceRejected(
            "formal workspace contains undeclared files"
        )
    if not allowed_directories <= directories or any(
        (
            relative not in allowed_directories
            and not _is_formal_output_path(relative)
        )
        or _has_denied_segment(relative)
        for relative in directories
    ):
        raise FormalWorkspaceRejected(
            "formal workspace contains undeclared directories"
        )
    for relative in directories:
        directory = _resolved_child(root, relative)
        mode = stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode)
        writable = _is_formal_output_path(relative)
        if writable and mode & 0o700 != 0o700:
            raise FormalWorkspaceRejected(
                "formal workspace output directory is not owner-writable"
            )
        if not writable and mode & 0o222:
            raise FormalWorkspaceRejected(
                "formal workspace public directory is writable"
            )
    for control_file in _CONTROL_FILES:
        if stat.S_IMODE(
            (root / control_file).stat(follow_symlinks=False).st_mode
        ) & 0o222:
            raise FormalWorkspaceRejected(
                "formal workspace control files must be read-only"
            )
    _validate_public_assignment_projection(assignment, root, manifest)
    return {
        "task_package_digest": task_ref.public_summary_digest,
        "environment_ready_digest": task_ref.environment_ready_digest,
        "workspace_mount_digest": manifest_digest,
        "workspace_policy_digest": policy_digest,
    }
