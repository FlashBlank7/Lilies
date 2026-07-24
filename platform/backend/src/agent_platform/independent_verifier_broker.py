from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Iterable

from .collaboration_models import (
    VerificationClaim,
    VerificationResultPayload,
)
from .task_packages import TaskPackageManager


class IndependentVerifierBrokerError(RuntimeError):
    """The isolated verifier process did not produce trusted evidence."""


_VERIFIER_RUNTIME_MODULES = (
    "pydantic",
    "pydantic_core",
    "yaml",
    "annotated_types",
    "typing_extensions",
    "typing_inspection",
)
_SITE_PACKAGE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})


def _dependency_site_root(module: ModuleType) -> Path:
    raw_location = getattr(module, "__file__", None)
    if not isinstance(raw_location, str):
        raise IndependentVerifierBrokerError(
            "a verifier runtime dependency has no trusted file origin"
        )
    location = Path(raw_location)
    if not location.is_absolute() or not location.is_file():
        raise IndependentVerifierBrokerError(
            "a verifier runtime dependency has no trusted file origin"
        )
    resolved = location.resolve()
    root = next(
        (
            parent
            for parent in resolved.parents
            if parent.name in _SITE_PACKAGE_DIRECTORY_NAMES
        ),
        None,
    )
    if root is None or not root.is_dir():
        raise IndependentVerifierBrokerError(
            "a verifier runtime dependency is outside a site-packages root"
        )
    return root


def _verifier_dependency_roots() -> list[Path]:
    roots_by_path: dict[str, Path] = {}
    for module_name in _VERIFIER_RUNTIME_MODULES:
        module = sys.modules.get(module_name)
        if not isinstance(module, ModuleType):
            raise IndependentVerifierBrokerError(
                "a verifier runtime dependency was not loaded by the broker"
            )
        root = _dependency_site_root(module)
        roots_by_path.setdefault(str(root), root)

    ordered: list[Path] = []
    for raw_path in sys.path:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            continue
        resolved = str(path.resolve())
        if resolved in roots_by_path and roots_by_path[resolved] not in ordered:
            ordered.append(roots_by_path[resolved])
    ordered.extend(root for root in roots_by_path.values() if root not in ordered)
    return ordered


def _read_regular_snapshot(
    path: Path,
    *,
    expected_mode: int,
    limit: int = 16 * 1024 * 1024,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise IndependentVerifierBrokerError(
            "the isolated verifier output is not safely readable"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
        ):
            raise IndependentVerifierBrokerError("the isolated verifier file boundary is unsafe")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise IndependentVerifierBrokerError("the isolated verifier file exceeds its limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise IndependentVerifierBrokerError(
                "the isolated verifier file changed while being read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sandbox_profile(
    *,
    read_roots: Iterable[Path],
    output_root: Path,
    executable: Path,
) -> str:
    read_rules: list[str] = []
    seen: set[str] = set()
    for raw in read_roots:
        path = Path(raw).resolve()
        encoded = str(path)
        if encoded in seen:
            continue
        seen.add(encoded)
        operation = "subpath" if path.is_dir() else "literal"
        read_rules.append(f"({operation} {json.dumps(encoded)})")
    escaped_output = json.dumps(str(output_root.resolve()))
    lines = [
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        f"(allow process-exec (literal {json.dumps(str(executable.resolve()))}))",
        "(allow process-info*)",
        "(allow file-read-metadata)",
    ]
    lines.extend(f"(allow file-read* {rule})" for rule in read_rules)
    lines.extend(f"(allow file-map-executable {rule})" for rule in read_rules)
    lines.extend(
        (
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            f"(allow file-write* (subpath {escaped_output}))",
            "(deny network*)",
        )
    )
    return "\n".join(lines)


def _verifier_read_roots(
    *,
    state_read_roots: Iterable[Path],
    input_root: Path,
    source_root: Path,
) -> list[Path]:
    interpreter = Path(sys.executable).resolve()
    interpreter_runtime_lib = interpreter.parent.parent / "lib"
    roots = [
        *(Path(path) for path in state_read_roots),
        Path(input_root),
        Path(source_root),
        interpreter,
    ]
    if interpreter_runtime_lib.is_dir():
        roots.append(interpreter_runtime_lib)
    roots.extend(_verifier_dependency_roots())
    roots.extend(
        path
        for key, item in sysconfig.get_paths().items()
        if key in {"stdlib", "platstdlib"}
        and item
        and (path := Path(item)).is_absolute()
        and path.exists()
    )
    roots.extend(
        path
        for path in (
            Path("/System/Library"),
            Path("/usr/lib"),
            Path("/private/var/db/dyld"),
            Path("/dev/null"),
        )
        if path.exists()
    )
    return roots


def _verifier_source_root(
    *,
    state_root: Path,
    process_digest: str,
) -> Path:
    manager = TaskPackageManager(Path(state_root).resolve(), read_only=True)
    source_root, _manifest = manager.load_verification_policy_bundle(
        process_digest
    )
    return source_root


def _verifier_environment(
    output_root: Path,
    *,
    source_root: Path,
) -> dict[str, str]:
    dependency_roots = _verifier_dependency_roots()
    python_paths = list(
        dict.fromkeys(
            str(path)
            for path in (
                Path(source_root).resolve(),
                *dependency_roots,
            )
        )
    )
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(Path(output_root).resolve()),
        "PYTHONPATH": os.pathsep.join(python_paths),
    }


def _claim_state_read_roots(
    *,
    state_root: Path,
    task_id: str,
    revision: int,
    claim: VerificationClaim,
) -> list[Path]:
    state_root = Path(state_root).resolve()
    manager = TaskPackageManager(state_root, read_only=True)
    manifest = manager.validate_claim_binding(
        task_id=task_id,
        revision=revision,
        claim=claim,
    )
    if (
        manifest.claim_binding is None
        or manifest.environment_ready_digest is None
        or manifest.workspace_mount_digest is None
    ):
        raise IndependentVerifierBrokerError("the frozen claim has no exact verifier read boundary")
    run_id = manifest.run_id
    assignment_id = manifest.claim_binding.assignment_id
    workspace_key = manifest.workspace_mount_digest.removeprefix("sha256:")
    policy_source_root, _policy = manager.load_verification_policy_bundle(
        str(claim.verification_process_digest)
    )
    policy_root = policy_source_root.parent
    return [
        state_root / "packages" / task_id / str(revision),
        state_root / "registry" / task_id / f"{revision}.json",
        (state_root / "registry" / "readiness" / task_id / str(revision) / f"{run_id}.json"),
        state_root / "registry" / "workspaces" / f"{workspace_key}.json",
        (state_root / "registry" / "formal-assignments" / f"{assignment_id}.json"),
        (state_root / "preflight" / task_id / str(revision) / run_id),
        policy_root,
    ]


def run_independent_verifier_subprocess(
    *,
    state_root: Path,
    task_id: str,
    revision: int,
    claim: VerificationClaim,
    broker_root: Path,
    timeout_seconds: float = 60,
) -> VerificationResultPayload:
    """Run the verifier with read-only inputs, no network, and one output sink."""

    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise IndependentVerifierBrokerError("an OS read-only verifier sandbox is unavailable")
    broker_root = Path(broker_root).resolve()
    broker_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="verification-",
        dir=broker_root,
    ) as temporary_name:
        temporary = Path(temporary_name)
        input_root = temporary / "input"
        output_root = temporary / "output"
        input_root.mkdir(mode=0o700)
        output_root.mkdir(mode=0o700)
        claim_path = input_root / "claim.json"
        result_path = output_root / "result.json"
        claim_payload = json.dumps(
            claim.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        claim_path.write_bytes(
            claim_payload,
        )
        os.chmod(claim_path, 0o400)
        os.chmod(input_root, 0o500)
        interpreter = Path(sys.executable).resolve()
        try:
            state_read_roots = _claim_state_read_roots(
                state_root=state_root,
                task_id=task_id,
                revision=revision,
                claim=claim,
            )
            source_root = _verifier_source_root(
                state_root=state_root,
                process_digest=str(claim.verification_process_digest),
            )
        except Exception as error:
            raise IndependentVerifierBrokerError(
                "the broker rejected the frozen claim boundary"
            ) from error
        command = [
            sandbox,
            "-p",
            _sandbox_profile(
                read_roots=_verifier_read_roots(
                    state_read_roots=state_read_roots,
                    input_root=input_root,
                    source_root=source_root,
                ),
                output_root=output_root,
                executable=interpreter,
            ),
            str(interpreter),
            "-S",
            "-m",
            "agent_platform.independent_verifier",
            "verify",
            "--state-root",
            str(Path(state_root).resolve()),
            "--task-id",
            task_id,
            "--revision",
            str(revision),
            "--claim-file",
            str(claim_path),
            "--result-out",
            str(result_path),
        ]
        environment = _verifier_environment(
            output_root,
            source_root=source_root,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
            cwd=source_root,
        )
        if completed.returncode != 0 or completed.stderr:
            raise IndependentVerifierBrokerError("the isolated verifier rejected the frozen claim")
        claim_after = _read_regular_snapshot(
            claim_path,
            expected_mode=0o400,
        )
        result_payload = _read_regular_snapshot(
            result_path,
            expected_mode=0o600,
        )
        if (
            not hmac.compare_digest(claim_after, claim_payload)
            or sorted(path.name for path in input_root.iterdir()) != ["claim.json"]
            or sorted(path.name for path in output_root.iterdir()) != ["result.json"]
        ):
            raise IndependentVerifierBrokerError(
                "the isolated verifier changed its read-only input boundary"
            )
        try:
            result = VerificationResultPayload.model_validate_json(result_payload)
        except Exception as error:
            raise IndependentVerifierBrokerError(
                "the isolated verifier returned invalid evidence"
            ) from error
        expected_status = {
            "status": "verification_result_written",
            "result_digest": ("sha256:" + hashlib.sha256(result_payload).hexdigest()),
        }
        try:
            actual_status = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise IndependentVerifierBrokerError(
                "the isolated verifier returned an invalid receipt"
            ) from error
        if actual_status != expected_status:
            raise IndependentVerifierBrokerError(
                "the isolated verifier receipt did not match its result"
            )
        if not hmac.compare_digest(
            str(result.verification_process_digest),
            str(claim.verification_process_digest),
        ):
            raise IndependentVerifierBrokerError(
                "the isolated verifier used another verification policy"
            )
        return result
