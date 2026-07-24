from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from .task_packages import (
    ArchiveClaimBinding,
    ArchiveStatus,
    TaskPackageError,
    TaskPackageManager,
    ValidationMode,
    WorkspaceRole,
)

_MAX_CLAIM_BINDING_BYTES = 1024 * 1024
_MAX_ARCHIVE_INPUT_FILE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_INPUT_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_INPUT_FILES = 100_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lilies-task",
        description=("Freeze, preflight, mount, archive, and replay formal Lilies task packages."),
    )
    parser.add_argument("--version", action="version", version="Lilies Task 0.4.13")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--state-root", type=Path, required=True)
    freeze.add_argument("--source", type=Path, required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--state-root", type=Path, required=True)
    preflight.add_argument("--task-id", required=True)
    preflight.add_argument("--revision", type=int, required=True)
    preflight.add_argument("--run-id", required=True)
    preflight.add_argument("--assignment-id", type=UUID, required=True)
    preflight.add_argument("--environment-instance-id", required=True)
    preflight.add_argument(
        "--attestation-secret-file",
        type=Path,
        required=True,
    )
    preflight.add_argument("--ttl-seconds", type=int, default=900)

    workspace = subparsers.add_parser("workspace")
    workspace.add_argument("--state-root", type=Path, required=True)
    workspace.add_argument("--task-id", required=True)
    workspace.add_argument("--revision", type=int, required=True)
    workspace.add_argument("--role", choices=[item.value for item in WorkspaceRole], required=True)
    workspace.add_argument("--destination", type=Path, required=True)
    workspace.add_argument("--run-id", required=True)
    workspace.add_argument("--assignment-id", type=UUID, required=True)
    workspace.add_argument("--environment-ready", type=Path)
    workspace.add_argument("--run-archive", type=Path)

    archive = subparsers.add_parser("archive")
    archive.add_argument("--state-root", type=Path, required=True)
    archive.add_argument("--task-id", required=True)
    archive.add_argument("--revision", type=int, required=True)
    archive.add_argument("--run-id", required=True)
    archive.add_argument(
        "--status",
        choices=[item.value for item in ArchiveStatus],
        required=True,
    )
    archive.add_argument(
        "--validation-mode",
        choices=[item.value for item in ValidationMode],
        required=True,
    )
    archive.add_argument("--input-dir", type=Path, required=True)
    archive.add_argument("--environment-ready", type=Path)
    archive.add_argument("--workspace-manifest", type=Path)
    archive.add_argument("--claim-binding", type=Path)
    archive.add_argument(
        "--forbidden-assistance-finding",
        action="append",
        default=[],
    )

    replay = subparsers.add_parser("replay")
    replay.add_argument("--state-root", type=Path, required=True)
    replay.add_argument("--task-id", required=True)
    replay.add_argument("--revision", type=int, required=True)
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--expected-manifest-digest")
    return parser


def _emit(value: object, *, stream: object = None) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stream or sys.stdout,
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys are forbidden")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON values are forbidden")


def _regular_file_snapshot(
    path: Path,
    *,
    limit: int,
    owner_only: bool = False,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    lexical = Path(path)
    if lexical.is_symlink():
        raise ValueError("symlink files are forbidden")
    try:
        descriptor = os.open(
            lexical,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ValueError("file is not safely readable") from error
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("only isolated regular files are allowed")
        if owner_only and mode not in {0o400, 0o600}:
            raise ValueError("claim binding must be an owner-only file")
        if before.st_size > limit:
            raise ValueError("file exceeds the CLI safety limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise ValueError("file exceeds the CLI safety limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        final_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity != final_identity or after.st_nlink != 1 or total != after.st_size:
            raise ValueError("file changed while it was being read")
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _read_claim_binding(path: Path) -> ArchiveClaimBinding:
    payload, _ = _regular_file_snapshot(
        path,
        limit=_MAX_CLAIM_BINDING_BYTES,
        owner_only=True,
    )
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("claim binding is not strict JSON") from error
    if not isinstance(value, dict):
        raise ValueError("claim binding must be a JSON object")
    return ArchiveClaimBinding.model_validate(value)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _scan_archive_input_tree(root: Path) -> tuple[list[str], list[str]]:
    directories: list[str] = []
    files: list[str] = []
    root_device = root.stat(follow_symlinks=False).st_dev
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        if current_path.is_symlink():
            raise ValueError("archive input directories cannot be symlinks")
        try:
            resolved_current = current_path.resolve(strict=True)
        except OSError as error:
            raise ValueError("archive input directory is not stable") from error
        if not _is_within(resolved_current, root):
            raise ValueError("archive input directory escapes its declared root")
        if current_path.stat(follow_symlinks=False).st_dev != root_device:
            raise ValueError("archive input crosses a filesystem mount boundary")
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            child = current_path / name
            metadata = child.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_dev != root_device
            ):
                raise ValueError("archive input contains an unsafe directory")
            resolved = child.resolve(strict=True)
            if not _is_within(resolved, root):
                raise ValueError("archive input directory escapes its declared root")
            directories.append(resolved.relative_to(root).as_posix())
        for name in file_names:
            child = current_path / name
            metadata = child.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != root_device
            ):
                raise ValueError("archive input contains a non-isolated regular file")
            resolved = child.resolve(strict=True)
            if not _is_within(resolved, root):
                raise ValueError("archive input file escapes its declared root")
            files.append(resolved.relative_to(root).as_posix())
            if len(files) > _MAX_ARCHIVE_INPUT_FILES:
                raise ValueError("archive input contains too many files")
    return directories, files


def _archive_input_files(
    input_dir: Path,
    *,
    state_root: Path,
) -> dict[str, bytes]:
    lexical = Path(input_dir)
    if lexical.is_symlink():
        raise ValueError("archive input root cannot be a symlink")
    try:
        root = lexical.resolve(strict=True)
    except OSError as error:
        raise ValueError("archive input root does not exist") from error
    if not root.is_dir():
        raise ValueError("archive input root must be a directory")
    state = Path(state_root).resolve(strict=False)
    if _is_within(root, state) or _is_within(state, root):
        raise ValueError("archive input root cannot overlap task-package state")

    initial_directories, initial_files = _scan_archive_input_tree(root)
    snapshots: dict[str, bytes] = {}
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    total = 0
    for relative in initial_files:
        normalized = PurePosixPath(relative).as_posix()
        if normalized != relative or normalized in {"", "."}:
            raise ValueError("archive input path is not canonical")
        payload, identity = _regular_file_snapshot(
            root.joinpath(*PurePosixPath(relative).parts),
            limit=_MAX_ARCHIVE_INPUT_FILE_BYTES,
        )
        total += len(payload)
        if total > _MAX_ARCHIVE_INPUT_TOTAL_BYTES:
            raise ValueError("archive input exceeds the aggregate safety limit")
        snapshots[relative] = payload
        identities[relative] = identity

    final_directories, final_files = _scan_archive_input_tree(root)
    if final_directories != initial_directories or final_files != initial_files:
        raise ValueError("archive input changed during inventory")
    for relative in final_files:
        metadata = root.joinpath(*PurePosixPath(relative).parts).lstat()
        current_identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        if current_identity != identities[relative] or metadata.st_nlink != 1:
            raise ValueError("archive input changed during inventory")
    return snapshots


def _read_attestation_secret(path: Path) -> bytes:
    lexical = Path(path)
    if lexical.is_symlink():
        raise ValueError("attestation secret file cannot be a symlink")
    descriptor = os.open(
        lexical,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise ValueError("attestation secret file must be an isolated owner-only file")
        payload = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    secret = payload.rstrip(b"\r\n")
    if len(secret) < 32 or len(secret) > 16_384:
        raise ValueError("attestation secret must contain 32 to 16384 bytes")
    return secret


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        attestation_secret = (
            _read_attestation_secret(args.attestation_secret_file)
            if args.command == "preflight"
            else None
        )
        manager = TaskPackageManager(
            args.state_root,
            environment_secret_resolver=(
                (lambda _secret_ref: attestation_secret) if attestation_secret is not None else None
            ),
        )
        if args.command == "freeze":
            package = manager.freeze_revision(args.source)
            _emit(package.record)
        elif args.command == "preflight":
            package = manager.load_frozen(args.task_id, args.revision)
            path, ready = manager.run_environment_preflight(
                package,
                run_id=args.run_id,
                assignment_id=args.assignment_id,
                environment_instance_id=args.environment_instance_id,
                ttl_seconds=args.ttl_seconds,
            )
            payload = path.read_bytes()
            _emit(
                {
                    "environment_ready_digest": (f"sha256:{hashlib.sha256(payload).hexdigest()}"),
                    "ready": ready,
                }
            )
        elif args.command == "workspace":
            package = manager.load_frozen(args.task_id, args.revision)
            manifest = manager.materialize_task_workspace(
                package,
                args.destination,
                role=WorkspaceRole(args.role),
                run_id=args.run_id,
                assignment_id=args.assignment_id,
                environment_ready_path=args.environment_ready,
                run_archive=args.run_archive,
            )
            _emit(manifest)
        elif args.command == "archive":
            package = manager.load_frozen(args.task_id, args.revision)
            status = ArchiveStatus(args.status)
            validation_mode = ValidationMode(args.validation_mode)
            claim_binding = (
                _read_claim_binding(args.claim_binding) if args.claim_binding is not None else None
            )
            if status is ArchiveStatus.succeeded and (
                claim_binding is None
                or args.environment_ready is None
                or args.workspace_manifest is None
            ):
                raise ValueError(
                    "successful archive requires environment, workspace, and claim binding"
                )
            findings = tuple(args.forbidden_assistance_finding)
            if len(findings) != len(set(findings)) or any(
                not item.strip() or len(item) > 2_000 for item in findings
            ):
                raise ValueError("forbidden-assistance findings must be unique bounded strings")
            run_root, manifest, manifest_digest = manager.archive_run(
                package,
                run_id=args.run_id,
                status=status,
                validation_mode=validation_mode,
                environment_ready_path=args.environment_ready,
                workspace_manifest_path=args.workspace_manifest,
                files=_archive_input_files(
                    args.input_dir,
                    state_root=args.state_root,
                ),
                claim_binding=claim_binding,
                forbidden_assistance_findings=findings,
            )
            _emit(
                {
                    "archive_manifest_digest": manifest_digest,
                    "archive_root": str(run_root),
                    "manifest": manifest.model_dump(
                        mode="json",
                        exclude_none=True,
                    ),
                }
            )
        elif args.command == "replay":
            manifest = manager.replay_registered_run(
                args.task_id,
                args.revision,
                args.run_id,
                expected_manifest_digest=args.expected_manifest_digest,
            )
            _emit(manifest)
        else:  # pragma: no cover - argparse constrains this
            raise AssertionError(f"unknown command: {args.command}")
        return 0
    except (TaskPackageError, ValueError, OSError) as error:
        _emit(
            {
                "error": {
                    "code": "task_package_rejected",
                    "reason": type(error).__name__,
                }
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
