from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
for import_root in (ROOT, BACKEND_SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from agent_platform.task_packages import (  # noqa: E402
    BUILDER_API_MANUAL_FILE,
    PUBLIC_BUILDER_GUIDANCE_FILE,
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
)
from agent_platform.lilies_platform_contract import (  # noqa: E402
    PUBLIC_OPERATION_SPECS,
)
from scripts.run_v04_13_live_development_handoff import (  # noqa: E402
    CODEX_ALLOWED_PROVIDER_HOSTS,
    _AllowlistedConnectProxy,
    _clean_codex_environment,
    _prepare_isolated_codex_identity,
)


MAX_HANDOFF_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_PUBLIC_FILE_BYTES = 64 * 1024 * 1024
MAX_PUBLIC_GUIDANCE_BYTES = 32 * 1024
MAX_SESSION_STATE_BYTES = 512 * 1024 * 1024
DEFAULT_MODEL = "gpt-5.6-terra"
MACOS_SANDBOX = Path("/usr/bin/sandbox-exec")
RUNTIME_HANDOFF_FILE = "BUILDER_HANDOFF.json"
AUTHORITY_TRANSITION_SCHEMA_VERSION = (
    "v0.4.13-codex-resume-authority-transition-1"
)
SEATBELT_PROBE_FILE = "seatbelt-boundary-probe.sh"
PROCESS_GROUP_GRACE_SECONDS = 3.0
INVOCATION_PROCESS_POLL_SECONDS = 0.05
MAX_ROLLOUT_TOKEN_LIMIT = 1_000_000
DEFAULT_ROLLOUT_TOKEN_LIMIT = MAX_ROLLOUT_TOKEN_LIMIT
INVOCATION_ENVIRONMENT_KEY = "LILIES_EXTERNAL_BUILDER_INVOCATION"
RUNTIME_DIGEST_ENVIRONMENT_KEY = "LILIES_EXTERNAL_BUILDER_RUNTIME_SHA256"
WORKSPACE_DIGEST_ENVIRONMENT_KEY = "LILIES_EXTERNAL_BUILDER_WORKSPACE_SHA256"
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_STALE_AUTHORITY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:lpt_|lcc_|collaboration-)[A-Za-z0-9_-]{24,}"
)
_BEARER_AUTHORITY_PATTERN = re.compile(
    r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{16,}"
)
_REQUIRED_DENIED_SEGMENTS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "expected-state",
    "oracle",
    "platform-data",
    "platform_data",
    "protected",
}
_SYSTEM_RUNTIME_READ_PATHS = {
    Path("/System"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/Library/Apple"),
    Path("/Library/Developer/CommandLineTools"),
    Path("/etc/ssl"),
    Path("/private/etc/ssl"),
    Path("/etc/codex/requirements.toml"),
    Path("/private/etc/codex/requirements.toml"),
}


class CodexBuilderChildError(RuntimeError):
    """The external Codex process could not stay inside its public boundary."""


class _ForwardedTermination(CodexBuilderChildError):
    def __init__(self, signum: int) -> None:
        super().__init__(f"received termination signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class WorkspaceVerification:
    manual_path: Path
    manual_digest: str
    guidance_path: Path
    guidance_digest: str
    guidance_text: str
    public_probe_path: Path


@dataclass(frozen=True)
class InvocationBinding:
    invocation_id: str
    runtime_digest: str
    workspace_digest: str

    @classmethod
    def create(
        cls,
        *,
        runtime_root: Path,
        public_workspace: Path,
    ) -> InvocationBinding:
        return cls(
            invocation_id=f"t01h-{secrets.token_hex(16)}",
            runtime_digest=hashlib.sha256(
                os.fsencode(_absolute_lexical_path(runtime_root))
            ).hexdigest(),
            workspace_digest=hashlib.sha256(
                os.fsencode(_absolute_lexical_path(public_workspace))
            ).hexdigest(),
        )

    def environment(self) -> dict[str, str]:
        return {
            INVOCATION_ENVIRONMENT_KEY: self.invocation_id,
            RUNTIME_DIGEST_ENVIRONMENT_KEY: self.runtime_digest,
            WORKSPACE_DIGEST_ENVIRONMENT_KEY: self.workspace_digest,
        }

    def codex_config_arguments(self) -> tuple[str, ...]:
        return (
            "-c",
            f'lilies.external_builder_invocation="{self.invocation_id}"',
            "-c",
            f'lilies.external_builder_runtime_sha256="{self.runtime_digest}"',
            "-c",
            f'lilies.external_builder_workspace_sha256="{self.workspace_digest}"',
        )

    def matches_command(self, command: str) -> bool:
        return all(
            value in command
            for value in (
                f'lilies.external_builder_invocation="{self.invocation_id}"',
                f'lilies.external_builder_runtime_sha256="{self.runtime_digest}"',
                f'lilies.external_builder_workspace_sha256="{self.workspace_digest}"',
            )
        )


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    parent_pid: int
    process_group_id: int
    started_at: str
    command_digest: str
    command: str


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory without following any path-component symlink."""

    if not path.is_absolute() or ".." in path.parts:
        raise CodexBuilderChildError("secure directory path must be absolute")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise CodexBuilderChildError("secure no-follow directory opens are unavailable")
    flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            replacement = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = replacement
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise CodexBuilderChildError("secure directory path is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_fd_bounded(descriptor: int, *, size_limit: int) -> bytes:
    parts: list[bytes] = []
    consumed = 0
    while consumed <= size_limit:
        chunk = os.read(descriptor, min(64 * 1024, size_limit + 1 - consumed))
        if not chunk:
            break
        parts.append(chunk)
        consumed += len(chunk)
    value = b"".join(parts)
    if len(value) > size_limit:
        raise CodexBuilderChildError("secure file exceeds its size limit")
    return value


def _read_nofollow_regular(
    path: Path,
    *,
    size_limit: int,
    required_mode: int | None = None,
    require_owner: bool = True,
) -> tuple[bytes, os.stat_result]:
    """Read one regular file once from its no-follow descriptor."""

    lexical = _absolute_lexical_path(path)
    if lexical.name in {"", ".", ".."}:
        raise CodexBuilderChildError("secure file path is invalid")
    parent_descriptor = _open_directory_nofollow(lexical.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        os.close(parent_descriptor)
        raise CodexBuilderChildError("secure no-follow file opens are unavailable")
    flags |= nofollow
    try:
        descriptor = os.open(lexical.name, flags, dir_fd=parent_descriptor)
    except BaseException:
        os.close(parent_descriptor)
        raise
    os.close(parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 0
            or opened.st_size > size_limit
            or (require_owner and opened.st_uid != os.getuid())
            or (
                required_mode is not None
                and stat.S_IMODE(opened.st_mode) != required_mode
            )
        ):
            raise CodexBuilderChildError("secure file metadata is unsafe")
        raw = _read_fd_bounded(descriptor, size_limit=size_limit)
        finished = os.fstat(descriptor)
        if (
            finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
            or len(raw) != opened.st_size
        ):
            raise CodexBuilderChildError("secure file changed while it was read")
        return raw, opened
    finally:
        os.close(descriptor)


def _digest_nofollow_regular(
    path: Path,
    *,
    size_limit: int,
) -> str:
    lexical = _absolute_lexical_path(path)
    parent_descriptor = _open_directory_nofollow(lexical.parent)
    try:
        descriptor = os.open(
            lexical.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_size < 0
            or opened.st_size > size_limit
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise CodexBuilderChildError("resume state metadata is unsafe")
        digest = hashlib.sha256()
        consumed = 0
        while consumed <= size_limit:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > size_limit:
                raise CodexBuilderChildError("resume state exceeds its size limit")
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
            or consumed != opened.st_size
        ):
            raise CodexBuilderChildError("resume state changed while it was digested")
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(descriptor)


def _read_private_handoff(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise CodexBuilderChildError("external Builder handoff must be absolute")
    try:
        raw, _ = _read_nofollow_regular(
            path,
            size_limit=MAX_HANDOFF_BYTES,
            required_mode=0o600,
        )
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError(
            "external Builder handoff is unavailable or unsafe"
        ) from error
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderChildError(
            "external Builder handoff is invalid JSON"
        ) from error
    if not isinstance(value, dict):
        raise CodexBuilderChildError("external Builder handoff is not an object")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CodexBuilderChildError(f"external Builder handoff omitted {label}")
    return value


def _safe_manifest_path(value: Any, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CodexBuilderChildError(f"workspace {label} path is invalid")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(part.casefold() in _REQUIRED_DENIED_SEGMENTS for part in relative.parts)
    ):
        raise CodexBuilderChildError(f"workspace {label} path escaped its boundary")
    return relative


def _open_relative_directory(root_descriptor: int, parts: Sequence[str]) -> int:
    descriptor = os.dup(root_descriptor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for component in parts:
            replacement = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = replacement
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_workspace_entry(
    root_descriptor: int,
    relative: PurePosixPath,
    *,
    size_limit: int,
) -> tuple[bytes, os.stat_result]:
    parent_descriptor = _open_relative_directory(root_descriptor, relative.parts[:-1])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(relative.name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise CodexBuilderChildError(
            "workspace public entry is unavailable or a symlink"
        ) from error
    finally:
        os.close(parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 0
            or opened.st_size > size_limit
            or opened.st_uid != os.getuid()
        ):
            raise CodexBuilderChildError("workspace public entry metadata is unsafe")
        raw = _read_fd_bounded(descriptor, size_limit=size_limit)
        finished = os.fstat(descriptor)
        if (
            finished.st_dev != opened.st_dev
            or finished.st_ino != opened.st_ino
            or finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
            or len(raw) != opened.st_size
        ):
            raise CodexBuilderChildError(
                "workspace public entry changed while it was read"
            )
        return raw, opened
    finally:
        os.close(descriptor)


def _scan_workspace_tree(
    descriptor: int,
    *,
    prefix: PurePosixPath = PurePosixPath("."),
) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    try:
        entries = list(os.scandir(descriptor))
    except OSError as error:
        raise CodexBuilderChildError("public workspace cannot be enumerated") from error
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for entry in entries:
        relative = (
            PurePosixPath(entry.name)
            if prefix.as_posix() == "."
            else prefix / entry.name
        )
        if (
            entry.name in {"", ".", ".."}
            or entry.is_symlink()
            or any(
                part.casefold() in _REQUIRED_DENIED_SEGMENTS for part in relative.parts
            )
        ):
            raise CodexBuilderChildError(
                "public workspace contains a symlink or denied segment"
            )
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            files.add(relative.as_posix())
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise CodexBuilderChildError(
                "public workspace contains a non-regular filesystem entry"
            )
        directories.add(relative.as_posix())
        child = os.open(entry.name, directory_flags, dir_fd=descriptor)
        try:
            nested_files, nested_directories = _scan_workspace_tree(
                child,
                prefix=relative,
            )
        finally:
            os.close(child)
        files.update(nested_files)
        directories.update(nested_directories)
    return files, directories


def _workspace_binding_matches(
    *,
    manifest: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> bool:
    task = _require_mapping(handoff.get("task"), "task")
    assignment = _require_mapping(handoff.get("assignment"), "assignment")
    expected = {
        "task_id": task.get("task_id"),
        "revision": task.get("revision"),
        "run_id": task.get("run_id"),
        "assignment_id": assignment.get("assignment_id"),
        "environment_instance_id": assignment.get("environment_instance_id"),
        "role": "lilies",
    }
    return all(manifest.get(key) == value for key, value in expected.items())


def _verify_public_workspace(
    *,
    handoff: Mapping[str, Any],
    public_workspace: Path,
) -> WorkspaceVerification:
    workspace = _require_mapping(handoff.get("workspace"), "workspace")
    try:
        root_descriptor = _open_directory_nofollow(public_workspace)
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError(
            "public workspace root is unavailable or unsafe"
        ) from error
    try:
        root_metadata = os.fstat(root_descriptor)
        if (
            root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise CodexBuilderChildError("public workspace root permissions are unsafe")
        manifest_relative = PurePosixPath(WORKSPACE_MANIFEST_FILE)
        policy_relative = PurePosixPath(WORKSPACE_POLICY_FILE)
        manifest_payload, manifest_metadata = _read_workspace_entry(
            root_descriptor,
            manifest_relative,
            size_limit=MAX_HANDOFF_BYTES,
        )
        policy_payload, policy_metadata = _read_workspace_entry(
            root_descriptor,
            policy_relative,
            size_limit=MAX_HANDOFF_BYTES,
        )
        expected_manifest_digest = workspace.get("manifest_digest")
        expected_policy_digest = workspace.get("policy_digest")
        if (
            not isinstance(expected_manifest_digest, str)
            or not isinstance(expected_policy_digest, str)
            or not hmac.compare_digest(
                _digest(manifest_payload),
                expected_manifest_digest,
            )
            or not hmac.compare_digest(
                _digest(policy_payload),
                expected_policy_digest,
            )
            or stat.S_IMODE(manifest_metadata.st_mode) & 0o222
            or stat.S_IMODE(policy_metadata.st_mode) & 0o222
        ):
            raise CodexBuilderChildError("public workspace control binding is invalid")
        try:
            manifest = json.loads(manifest_payload)
            policy = json.loads(policy_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CodexBuilderChildError(
                "public workspace controls are invalid JSON"
            ) from error
        if (
            not isinstance(manifest, dict)
            or not isinstance(policy, dict)
            or manifest.get("schema_version") != "1.0"
            or policy.get("schema_version") != "1.0"
            or not _workspace_binding_matches(manifest=manifest, handoff=handoff)
            or manifest.get("archive_manifest_digest") is not None
        ):
            raise CodexBuilderChildError(
                "public workspace manifest identity is invalid"
            )
        denied_segments = manifest.get("denied_segments")
        writable_prefixes = manifest.get("writable_prefixes")
        policy_denied = policy.get("denied_segments")
        policy_writable = policy.get("writable_prefixes")
        if (
            not isinstance(denied_segments, list)
            or not all(isinstance(item, str) for item in denied_segments)
            or not _REQUIRED_DENIED_SEGMENTS
            <= {item.casefold() for item in denied_segments}
            or policy_denied
            != sorted(
                {
                    *denied_segments,
                    WORKSPACE_MANIFEST_FILE,
                    WORKSPACE_POLICY_FILE,
                }
            )
            or not isinstance(writable_prefixes, list)
            or policy_writable != writable_prefixes
            or not all(isinstance(item, str) for item in writable_prefixes)
        ):
            raise CodexBuilderChildError("public workspace policy widens its manifest")
        safe_writable = {
            _safe_manifest_path(item, label="writable prefix").as_posix()
            for item in writable_prefixes
        }
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > 100_000:
            raise CodexBuilderChildError(
                "public workspace manifest entries are invalid"
            )
        supplemental = manifest.get("supplemental_public_materials")
        supplemental_digest = manifest.get(
            "supplemental_public_materials_digest"
        )
        if (
            not isinstance(supplemental, list)
            or not supplemental
            or len(supplemental) > 20
            or not isinstance(supplemental_digest, str)
            or _DIGEST_PATTERN.fullmatch(supplemental_digest) is None
            or not hmac.compare_digest(
                _digest(
                    _canonical_json(
                        {
                            "schema_version": "1.0",
                            "entries": supplemental,
                        }
                    )
                ),
                supplemental_digest,
            )
        ):
            raise CodexBuilderChildError(
                "supplemental public material binding is invalid"
            )
        declared: dict[str, tuple[str, int, bytes]] = {}
        bound_entries = [
            (item, "task-package") for item in entries
        ]
        bound_entries.extend(
            (item, "runner-public") for item in supplemental
        )
        for raw_entry, source_prefix in bound_entries:
            if not isinstance(raw_entry, dict):
                raise CodexBuilderChildError(
                    "public workspace manifest entry is invalid"
                )
            relative = _safe_manifest_path(
                raw_entry.get("target_path"),
                label="public entry",
            )
            relative_value = relative.as_posix()
            digest = raw_entry.get("digest")
            size_bytes = raw_entry.get("size_bytes")
            if (
                relative_value in {WORKSPACE_MANIFEST_FILE, WORKSPACE_POLICY_FILE}
                or relative_value in declared
                or raw_entry.get("logical_source")
                != f"{source_prefix}:{relative_value}"
                or raw_entry.get("read_only") is not True
                or not isinstance(digest, str)
                or _DIGEST_PATTERN.fullmatch(digest) is None
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or size_bytes > MAX_PUBLIC_FILE_BYTES
            ):
                raise CodexBuilderChildError(
                    "public workspace manifest entry is unsafe"
                )
            payload, metadata = _read_workspace_entry(
                root_descriptor,
                relative,
                size_limit=MAX_PUBLIC_FILE_BYTES,
            )
            if (
                len(payload) != size_bytes
                or not hmac.compare_digest(_digest(payload), digest)
                or stat.S_IMODE(metadata.st_mode) & 0o222
            ):
                raise CodexBuilderChildError(
                    "public workspace entry bytes or permissions changed"
                )
            declared[relative_value] = (digest, size_bytes, payload)

        files, directories = _scan_workspace_tree(root_descriptor)
        expected_files = {
            *declared,
            WORKSPACE_MANIFEST_FILE,
            WORKSPACE_POLICY_FILE,
        }
        if files != expected_files:
            raise CodexBuilderChildError(
                "public workspace contains an undeclared or missing file"
            )
        expected_directories = set(safe_writable)
        for relative_value in {*declared, *safe_writable}:
            parent = PurePosixPath(relative_value).parent
            while parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if directories != expected_directories:
            raise CodexBuilderChildError(
                "public workspace contains an undeclared or missing directory"
            )
        manual = declared.get(BUILDER_API_MANUAL_FILE)
        if manual is None:
            raise CodexBuilderChildError(
                "public Builder API manual is not manifest-bound"
            )
        try:
            manual_value = json.loads(manual[2])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CodexBuilderChildError(
                "public Builder API manual is invalid JSON"
            ) from error
        if (
            not isinstance(manual_value, dict)
            or manual_value.get("schema_version")
            != "v0.4.13-t01h-external-builder-api-manual-1"
            or not isinstance(manual_value.get("platform"), dict)
            or manual_value["platform"].get("operation_count")
            not in {16, len(PUBLIC_OPERATION_SPECS)}
        ):
            raise CodexBuilderChildError(
                "public Builder API manual identity is invalid"
            )
        guidance = declared.get(PUBLIC_BUILDER_GUIDANCE_FILE)
        if guidance is None:
            raise CodexBuilderChildError(
                "public Builder guidance is not manifest-bound"
            )
        if len(guidance[2]) > MAX_PUBLIC_GUIDANCE_BYTES:
            raise CodexBuilderChildError(
                "public Builder guidance exceeds its prompt limit"
            )
        try:
            guidance_text = guidance[2].decode("utf-8")
        except UnicodeDecodeError as error:
            raise CodexBuilderChildError(
                "public Builder guidance is not valid UTF-8"
            ) from error
        if not guidance_text.startswith(
            "# Public Builder Operating Guide\n\nVersion: 1.0\n"
        ):
            raise CodexBuilderChildError(
                "public Builder guidance identity is invalid"
            )
        public_probe_path = next(
            public_workspace / relative
            for relative in sorted(declared)
            if relative
            not in {
                BUILDER_API_MANUAL_FILE,
                PUBLIC_BUILDER_GUIDANCE_FILE,
            }
        )
        return WorkspaceVerification(
            manual_path=public_workspace / BUILDER_API_MANUAL_FILE,
            manual_digest=manual[0],
            guidance_path=(
                public_workspace / PUBLIC_BUILDER_GUIDANCE_FILE
            ),
            guidance_digest=guidance[0],
            guidance_text=guidance_text,
            public_probe_path=public_probe_path,
        )
    finally:
        os.close(root_descriptor)


def _validate_handoff(
    handoff: Mapping[str, Any],
) -> tuple[Path, str, int, tuple[str, ...]]:
    task = _require_mapping(handoff.get("task"), "task")
    assignment = _require_mapping(handoff.get("assignment"), "assignment")
    workspace = _require_mapping(handoff.get("workspace"), "workspace")
    platform = _require_mapping(handoff.get("platform"), "platform")
    collaboration = _require_mapping(handoff.get("collaboration"), "collaboration")
    if (
        handoff.get("schema_version") != "1.0"
        or handoff.get("builder_actor") != "codex"
        or handoff.get("formal_archive_supported") is not True
        or not isinstance(task.get("task_id"), str)
        or not isinstance(task.get("revision"), int)
        or not isinstance(assignment.get("session_id"), str)
    ):
        raise CodexBuilderChildError("external Builder handoff identity is invalid")
    raw_workspace = workspace.get("path")
    if not isinstance(raw_workspace, str):
        raise CodexBuilderChildError("public workspace path is unavailable")
    public_workspace = _absolute_lexical_path(Path(raw_workspace))
    denied_segments = {"protected", "oracle", "platform-data", "platform_data"}
    if not public_workspace.is_absolute() or any(
        part.casefold() in denied_segments for part in public_workspace.parts
    ):
        raise CodexBuilderChildError("public workspace escaped its filtered boundary")
    base_url = platform.get("base_url")
    contract_url = platform.get("contract_url")
    task_token = platform.get("access_token")
    collaboration_token = collaboration.get("access_token")
    if (
        not isinstance(base_url, str)
        or not isinstance(contract_url, str)
        or not contract_url.startswith("/api/")
        or not isinstance(task_token, str)
        or not task_token
        or not isinstance(collaboration_token, str)
        or not collaboration_token
        or task_token == collaboration_token
    ):
        raise CodexBuilderChildError("public API authority is invalid")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise CodexBuilderChildError("public platform base URL is not loopback HTTP")
    platform_port = parsed.port
    if platform_port is None:
        raise CodexBuilderChildError("public platform base URL omitted its port")
    redactions = (
        task_token,
        collaboration_token,
        str(public_workspace),
    )
    return public_workspace, base_url.rstrip("/"), platform_port, redactions


def _sandbox_literal(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _sandboxed_arguments(
    *,
    executable: Path,
    codex_arguments: Sequence[str],
    public_workspace: Path,
    handoff_path: Path,
    runtime_root: Path,
    provider_proxy_port: int,
    platform_port: int,
) -> tuple[str, ...]:
    if not MACOS_SANDBOX.is_file() or not os.access(MACOS_SANDBOX, os.X_OK):
        raise CodexBuilderChildError("isolated Codex Builder requires macOS Seatbelt")
    resolved_executable = executable.resolve(strict=True)
    executable_metadata = resolved_executable.stat()
    if (
        not stat.S_ISREG(executable_metadata.st_mode)
        or executable_metadata.st_uid not in {0, os.getuid()}
        or executable_metadata.st_mode & 0o022
    ):
        raise CodexBuilderChildError("isolated executable metadata is unsafe")
    read_paths = {
        *_SYSTEM_RUNTIME_READ_PATHS,
        resolved_executable,
        public_workspace,
        handoff_path,
        runtime_root,
    }
    metadata_paths = {Path("/")}
    for path in read_paths:
        metadata_paths.add(path)
        metadata_paths.update(path.parents)
    read_filters = "\n".join(
        f"  (subpath {_sandbox_literal(path)})" for path in sorted(read_paths, key=str)
    )
    metadata_filters = "\n".join(
        f"  (literal {_sandbox_literal(path)})"
        for path in sorted(metadata_paths, key=str)
    )
    profile = "\n".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow process-info*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm-read*)",
            "(allow file-read-metadata",
            metadata_filters,
            ")",
            "(allow file-read*",
            read_filters,
            '  (literal "/")',
            '  (literal "/dev/null")',
            '  (literal "/dev/urandom")',
            '  (literal "/private/var/db/timezone/zoneinfo/UTC"))',
            (
                "(allow file-write* "
                f"(subpath {_sandbox_literal(runtime_root)}) "
                '(literal "/dev/null"))'
            ),
            (f'(allow network-outbound (remote ip "localhost:{provider_proxy_port}"))'),
            (f'(allow network-outbound (remote ip "localhost:{platform_port}"))'),
        )
    )
    return (
        str(MACOS_SANDBOX),
        "-p",
        profile,
        "--",
        str(resolved_executable),
        *codex_arguments,
    )


def _signal_process_group(process_group_id: int, signum: int) -> bool:
    try:
        os.killpg(process_group_id, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise CodexBuilderChildError(
            "isolated Codex process group could not be signalled"
        ) from error


def _process_identity_snapshot() -> dict[int, _ProcessIdentity]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin"},
    )
    if result.returncode != 0:
        raise CodexBuilderChildError("cannot audit isolated Codex invocation processes")
    processes: dict[int, _ProcessIdentity] = {}
    for row in result.stdout.splitlines():
        match = re.match(
            r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+"
            r"(\S+\s+\S+\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.*)$",
            row,
        )
        if match is None:
            continue
        pid, parent_pid, process_group_id, started_at, command = match.groups()
        processes[int(pid)] = _ProcessIdentity(
            pid=int(pid),
            parent_pid=int(parent_pid),
            process_group_id=int(process_group_id),
            started_at=started_at,
            command_digest=hashlib.sha256(command.encode("utf-8")).hexdigest(),
            command=command,
        )
    return processes


def _same_process(
    expected: _ProcessIdentity,
    current: _ProcessIdentity,
) -> bool:
    return (
        expected.pid == current.pid
        and expected.started_at == current.started_at
        and expected.command_digest == current.command_digest
    )


class _InvocationProcessTracker:
    """Track invocation descendants across process-group and session escape."""

    def __init__(
        self,
        *,
        root_pid: int,
        binding: InvocationBinding,
    ) -> None:
        self.root_pid = root_pid
        self.binding = binding
        self._records: dict[int, _ProcessIdentity] = {}
        self._conflicts: set[int] = set()
        self._failure: BaseException | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        snapshot = _process_identity_snapshot()
        root = snapshot.get(self.root_pid)
        if root is None or not self.binding.matches_command(root.command):
            raise CodexBuilderChildError(
                "isolated Codex root process lacks its invocation binding"
            )
        with self._lock:
            self._records[root.pid] = root
        self._observe(snapshot)
        self._thread = threading.Thread(
            target=self._watch,
            name=f"codex-invocation-{self.root_pid}",
            daemon=True,
        )
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(INVOCATION_PROCESS_POLL_SECONDS):
            try:
                self._observe(_process_identity_snapshot())
            except BaseException as error:
                with self._lock:
                    self._failure = error
                return

    def _observe(self, snapshot: Mapping[int, _ProcessIdentity]) -> None:
        with self._lock:
            bound_pids = {
                pid
                for pid, process in snapshot.items()
                if self.binding.matches_command(process.command)
            }
            for pid, expected in tuple(self._records.items()):
                current = snapshot.get(pid)
                if current is None:
                    continue
                if current.started_at != expected.started_at:
                    self._conflicts.add(pid)
                    continue
                if current.command_digest == expected.command_digest:
                    bound_pids.add(pid)
            changed = True
            while changed:
                changed = False
                for pid, process in snapshot.items():
                    if pid in bound_pids or process.parent_pid not in bound_pids:
                        continue
                    bound_pids.add(pid)
                    changed = True
            for pid in bound_pids:
                current = snapshot[pid]
                previous = self._records.get(pid)
                if previous is not None and previous.started_at != current.started_at:
                    self._conflicts.add(pid)
                    continue
                self._records[pid] = current

    def _signal_current(self, identity: _ProcessIdentity, signum: int) -> bool:
        snapshot = _process_identity_snapshot()
        self._observe(snapshot)
        current = snapshot.get(identity.pid)
        if current is None:
            return False
        if not _same_process(identity, current):
            with self._lock:
                self._conflicts.add(identity.pid)
            return False
        try:
            os.kill(identity.pid, signum)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise CodexBuilderChildError(
                "bound Codex invocation process could not be signalled"
            ) from error

    def terminate_and_verify(self) -> None:
        deadline = time.monotonic() + (PROCESS_GROUP_GRACE_SECONDS * 2)
        sent_term: set[tuple[int, str, str]] = set()
        while time.monotonic() < deadline:
            snapshot = _process_identity_snapshot()
            self._observe(snapshot)
            with self._lock:
                failure = self._failure
                conflicts = set(self._conflicts)
                records = tuple(self._records.values())
            if failure is not None:
                raise CodexBuilderChildError(
                    "invocation process audit failed"
                ) from failure
            if conflicts:
                raise CodexBuilderChildError(
                    "invocation process identity changed; refusing unsafe PID signal"
                )
            alive = [
                identity
                for identity in records
                if (
                    (current := snapshot.get(identity.pid)) is not None
                    and _same_process(identity, current)
                )
            ]
            if not alive:
                return
            for identity in sorted(
                alive,
                key=lambda item: item.parent_pid == self.root_pid,
            ):
                key = (
                    identity.pid,
                    identity.started_at,
                    identity.command_digest,
                )
                signum = signal.SIGTERM if key not in sent_term else signal.SIGKILL
                self._signal_current(identity, signum)
                sent_term.add(key)
            time.sleep(0.05)
        raise CodexBuilderChildError(
            "bound Codex invocation processes remained alive after cleanup"
        )

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise CodexBuilderChildError(
                    "invocation process audit thread did not stop"
                )


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.02)
    return not _process_group_exists(process_group_id)


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    initial_signal: int | None = signal.SIGTERM,
) -> tuple[bytes, bytes]:
    """Terminate, escalate, drain, and reap one independently-sessioned process."""

    process_group_id = process.pid
    if initial_signal is not None:
        _signal_process_group(process_group_id, initial_signal)
    try:
        stdout, stderr = process.communicate(timeout=PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process_group_id, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=PROCESS_GROUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise CodexBuilderChildError(
                "isolated Codex process group did not die after SIGKILL"
            ) from error
    if _process_group_exists(process_group_id):
        _signal_process_group(process_group_id, signal.SIGKILL)
    if not _wait_for_process_group_exit(
        process_group_id,
        timeout_seconds=PROCESS_GROUP_GRACE_SECONDS,
    ):
        raise CodexBuilderChildError(
            "isolated Codex process group remained alive after cleanup"
        )
    return stdout, stderr


class _ProcessGroupSignalGuard:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.previous: dict[int, Any] = {}
        self.handling_signal = False

    def __enter__(self) -> _ProcessGroupSignalGuard:
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._forward)
        return self

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def _forward(self, signum: int, _frame: Any) -> None:
        process = self.process
        if process is not None:
            _signal_process_group(process.pid, signum)
            if self.handling_signal:
                _signal_process_group(process.pid, signal.SIGKILL)
                return
        self.handling_signal = True
        raise _ForwardedTermination(signum)

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        for signum, previous in self.previous.items():
            signal.signal(signum, previous)


def _communicate_isolated_process(
    process: subprocess.Popen[bytes],
    *,
    input_bytes: bytes | None,
    timeout_seconds: int,
    invocation_binding: InvocationBinding | None = None,
) -> tuple[bytes, bytes, bool]:
    tracker: _InvocationProcessTracker | None = None
    if invocation_binding is not None:
        tracker = _InvocationProcessTracker(
            root_pid=process.pid,
            binding=invocation_binding,
        )
        try:
            tracker.start()
        except BaseException:
            _terminate_process_group(process)
            tracker.close()
            raise
    try:
        with _ProcessGroupSignalGuard() as guard:
            guard.attach(process)
            deadline = time.monotonic() + timeout_seconds
            pending_input = input_bytes
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        stdout, stderr = _terminate_process_group(process)
                        return stdout, stderr, True
                    try:
                        stdout, stderr = process.communicate(
                            input=pending_input,
                            timeout=min(0.25, remaining),
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
                        if process.poll() is not None and tracker is not None:
                            # A detached descendant can keep inherited stdio open
                            # after the root exits. Reap it while the provider
                            # proxy is still alive instead of waiting for the
                            # full model timeout.
                            tracker.terminate_and_verify()
            except _ForwardedTermination:
                _terminate_process_group(process, initial_signal=None)
                raise
            except BaseException:
                _terminate_process_group(process)
                raise
            if _process_group_exists(process.pid):
                _terminate_process_group(process)
                raise CodexBuilderChildError(
                    "isolated Codex left a background process after exit"
                )
            return stdout, stderr, False
    finally:
        if tracker is not None:
            try:
                tracker.terminate_and_verify()
            finally:
                tracker.close()


def _clean_external_builder_environment(
    *,
    codex_home: Path,
    user_home: Path,
    temporary_directory: Path,
    proxy_port: int,
) -> tuple[dict[str, str], list[str]]:
    environment, _ = _clean_codex_environment(
        codex_home=codex_home,
        user_home=user_home,
        temporary_directory=temporary_directory,
        proxy_port=proxy_port,
    )
    # System TLS defaults are already present in the fixed Seatbelt profile.
    # Never inherit an arbitrary SSL_CERT_* path as a new readable subtree.
    environment.pop("SSL_CERT_DIR", None)
    environment.pop("SSL_CERT_FILE", None)
    return environment, sorted(environment)


def _safe_runtime_directory(
    path: Path,
    *,
    label: str,
    exact_mode: int | None = 0o700,
) -> Path:
    lexical = _absolute_lexical_path(path)
    try:
        descriptor = _open_directory_nofollow(lexical)
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError(f"{label} is unavailable or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or (
            stat.S_IMODE(metadata.st_mode) != exact_mode
            if exact_mode is not None
            else bool(stat.S_IMODE(metadata.st_mode) & 0o022)
        ):
            raise CodexBuilderChildError(f"{label} permissions are unsafe")
    finally:
        os.close(descriptor)
    return lexical


def _resume_runtime_identity(
    runtime_root: Path,
    *,
    thread_id: str,
) -> tuple[Path, Path, dict[str, Any]]:
    try:
        normalized_thread_id = str(UUID(thread_id))
    except (TypeError, ValueError) as error:
        raise CodexBuilderChildError("resume thread id must be a UUID") from error
    if normalized_thread_id != thread_id.casefold():
        raise CodexBuilderChildError("resume thread id must be canonical")
    runtime_root = _safe_runtime_directory(
        runtime_root,
        label="isolated Codex runtime root",
    )
    codex_home = _safe_runtime_directory(
        runtime_root / "codex-home",
        label="isolated CODEX_HOME",
    )
    user_home = _safe_runtime_directory(
        runtime_root / "user-home",
        label="isolated user HOME",
    )
    _read_nofollow_regular(
        codex_home / "auth.json",
        size_limit=1024 * 1024,
        required_mode=0o600,
    )
    sessions_root = _safe_runtime_directory(
        codex_home / "sessions",
        label="isolated Codex sessions",
    )
    _resume_state_binding(
        sessions_root=sessions_root,
        thread_id=normalized_thread_id,
    )
    return (
        codex_home,
        user_home,
        {
            "auth_mode": "chatgpt",
            "api_key_present": False,
            "tokens_present": True,
            "billing_mode": "chatgpt_subscription",
            "credential_identity": "reused-isolated-codex-subscription",
        },
    )


def _resume_state_binding(
    *,
    sessions_root: Path,
    thread_id: str,
) -> tuple[Path, str]:
    descriptor = _open_directory_nofollow(sessions_root)
    try:
        files, _ = _scan_workspace_tree(descriptor)
    finally:
        os.close(descriptor)
    suffix = f"-{thread_id}.jsonl"
    matches = [
        sessions_root / PurePosixPath(relative)
        for relative in files
        if PurePosixPath(relative).name.endswith(suffix)
    ]
    if len(matches) != 1:
        raise CodexBuilderChildError(
            "resume thread is not uniquely present in the isolated CODEX_HOME"
        )
    try:
        digest = _digest_nofollow_regular(
            matches[0],
            size_limit=MAX_SESSION_STATE_BYTES,
        )
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError("resume session storage is unsafe") from error
    return matches[0], digest


def _runtime_authority_path(
    runtime_root: Path,
    *,
    handoff: Mapping[str, Any],
    resume: bool,
    resume_thread_id: str | None = None,
    authority_transition_path: Path | None = None,
) -> Path:
    path = runtime_root / RUNTIME_HANDOFF_FILE
    payload = _canonical_json(handoff)
    if not resume:
        if authority_transition_path is not None:
            raise CodexBuilderChildError(
                "an initial Builder runtime cannot consume an authority transition"
            )
        _write_private_bytes(path, payload)
        return path
    try:
        existing, _ = _read_nofollow_regular(
            path,
            size_limit=MAX_HANDOFF_BYTES,
            required_mode=0o600,
        )
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError(
            "resumed runtime omitted its original private handoff"
        ) from error
    if hmac.compare_digest(existing, payload):
        if authority_transition_path is not None:
            raise CodexBuilderChildError(
                "resume authority transition is unnecessary for an unchanged handoff"
            )
        return path
    if authority_transition_path is None or resume_thread_id is None:
        raise CodexBuilderChildError(
            "resume handoff changed; a new authority cannot reuse the Builder thread"
        )
    _validate_resume_authority_transition(
        existing_handoff=existing,
        successor_handoff=payload,
        transition_path=authority_transition_path,
        resume_thread_id=resume_thread_id,
    )
    _replace_private_runtime_handoff(path, payload)
    return path


def _authority_transition_projection(
    handoff: Mapping[str, Any],
    *,
    handoff_digest: str,
) -> dict[str, Any]:
    task = handoff.get("task")
    assignment = handoff.get("assignment")
    platform = handoff.get("platform")
    collaboration = handoff.get("collaboration")
    workspace = handoff.get("workspace")
    if not all(
        isinstance(value, Mapping)
        for value in (task, assignment, platform, collaboration, workspace)
    ):
        raise CodexBuilderChildError(
            "authority transition handoff structure is invalid"
        )
    projection = {
        "task_id": task.get("task_id"),
        "revision": task.get("revision"),
        "application_id": assignment.get("application_id"),
        "assignment_id": assignment.get("assignment_id"),
        "session_id": assignment.get("session_id"),
        "channel_id": collaboration.get("channel_id"),
        "task_credential_ref": platform.get("credential_ref"),
        "collaboration_credential_ref": collaboration.get("credential_ref"),
        "platform_base_url": platform.get("base_url"),
        "platform_contract_url": platform.get("contract_url"),
        "platform_contract_digest": platform.get("contract_digest"),
        "workspace_policy_digest": workspace.get("policy_digest"),
        "handoff_digest": handoff_digest,
    }
    if (
        handoff.get("schema_version") != "1.0"
        or handoff.get("builder_actor") != "codex"
        or not isinstance(projection["task_id"], str)
        or type(projection["revision"]) is not int
        or projection["revision"] < 1
        or any(
            not isinstance(projection[key], str) or not projection[key]
            for key in (
                "application_id",
                "assignment_id",
                "session_id",
                "channel_id",
                "task_credential_ref",
                "collaboration_credential_ref",
                "platform_base_url",
                "platform_contract_url",
                "platform_contract_digest",
                "workspace_policy_digest",
            )
        )
        or not _DIGEST_PATTERN.fullmatch(handoff_digest)
    ):
        raise CodexBuilderChildError(
            "authority transition handoff identity is invalid"
        )
    return projection


def _validate_resume_authority_transition(
    *,
    existing_handoff: bytes,
    successor_handoff: bytes,
    transition_path: Path,
    resume_thread_id: str,
) -> None:
    try:
        predecessor = json.loads(existing_handoff)
        successor = json.loads(successor_handoff)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderChildError(
            "authority transition handoff is invalid JSON"
        ) from error
    if not isinstance(predecessor, dict) or not isinstance(successor, dict):
        raise CodexBuilderChildError(
            "authority transition handoff must be a JSON object"
        )
    try:
        transition_payload, _ = _read_nofollow_regular(
            _absolute_lexical_path(transition_path),
            size_limit=MAX_HANDOFF_BYTES,
            require_owner=True,
        )
        transition = json.loads(transition_payload)
    except (CodexBuilderChildError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderChildError(
            "resume authority transition evidence is unreadable"
        ) from error
    if not isinstance(transition, dict):
        raise CodexBuilderChildError(
            "resume authority transition evidence must be an object"
        )
    predecessor_projection = _authority_transition_projection(
        predecessor,
        handoff_digest=_digest(existing_handoff),
    )
    successor_projection = _authority_transition_projection(
        successor,
        handoff_digest=_digest(successor_handoff),
    )
    retirement = transition.get("authority_retirement")
    environment_adoption_digest = transition.get(
        "environment_adoption_receipt_digest"
    )
    if (
        transition.get("schema_version")
        != AUTHORITY_TRANSITION_SCHEMA_VERSION
        or transition.get("thread_id") != resume_thread_id
        or transition.get("predecessor") != predecessor_projection
        or transition.get("successor") != successor_projection
        or not isinstance(retirement, dict)
        or retirement.get("task_id") != predecessor_projection["task_id"]
        or retirement.get("predecessor_revision")
        != predecessor_projection["revision"]
        or retirement.get("successor_revision")
        != successor_projection["revision"]
        or retirement.get("application_id")
        != predecessor_projection["application_id"]
        or retirement.get("assignment_id")
        != predecessor_projection["assignment_id"]
        or retirement.get("session_id")
        != predecessor_projection["session_id"]
        or retirement.get("collaboration_channel_id")
        != predecessor_projection["channel_id"]
        or retirement.get("task_credential_ref")
        != predecessor_projection["task_credential_ref"]
        or retirement.get("collaboration_credential_ref")
        != predecessor_projection["collaboration_credential_ref"]
        or retirement.get("active_predecessor_retirement_authorized") is not True
        or any(
            not isinstance(retirement.get(key), str)
            or not retirement[key]
            for key in (
                "task_credential_revoked_at",
                "collaboration_credential_revoked_at",
                "collaboration_channel_closed_at",
                "retirement_reason",
            )
        )
        or not isinstance(environment_adoption_digest, str)
        or not _DIGEST_PATTERN.fullmatch(environment_adoption_digest)
    ):
        raise CodexBuilderChildError(
            "resume authority transition evidence binding is invalid"
        )
    if (
        successor_projection["task_id"] != predecessor_projection["task_id"]
        or successor_projection["revision"]
        != predecessor_projection["revision"] + 1
        or successor_projection["application_id"]
        != predecessor_projection["application_id"]
        or successor_projection["platform_base_url"]
        != predecessor_projection["platform_base_url"]
        or successor_projection["platform_contract_url"]
        != predecessor_projection["platform_contract_url"]
        or successor_projection["platform_contract_digest"]
        != predecessor_projection["platform_contract_digest"]
        or successor_projection["workspace_policy_digest"]
        != predecessor_projection["workspace_policy_digest"]
        or any(
            successor_projection[key] == predecessor_projection[key]
            for key in (
                "assignment_id",
                "session_id",
                "channel_id",
                "task_credential_ref",
                "collaboration_credential_ref",
                "handoff_digest",
            )
        )
    ):
        raise CodexBuilderChildError(
            "resume authority transition is not an adjacent governed revision"
        )


def _replace_private_runtime_handoff(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_HANDOFF_BYTES:
        raise CodexBuilderChildError(
            "successor Builder handoff exceeds its size limit"
        )
    parent_descriptor = _open_directory_nofollow(
        _absolute_lexical_path(path).parent
    )
    temporary_name = (
        f".{path.name}.authority-transition-{secrets.token_hex(12)}"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise CodexBuilderChildError(
                    "successor Builder handoff write failed"
                )
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


_SEATBELT_PROBE_SOURCE = b"""\
#!/bin/sh
set -eu
handoff_path=$1
public_path=$2
base_url=$3
contract_url=$4
shift 4
/usr/bin/head -c 1 "$public_path" >/dev/null || exit 31
for forbidden_path in "$@"; do
  if /usr/bin/head -c 1 "$forbidden_path" >/dev/null 2>&1; then
    exit 32
  fi
done
task_token=$(/usr/bin/plutil -extract platform.access_token raw -o - "$handoff_path")
assignment_id=$(/usr/bin/plutil -extract assignment.assignment_id raw -o - "$handoff_path")
session_id=$(/usr/bin/plutil -extract assignment.session_id raw -o - "$handoff_path")
request_id=$(/usr/bin/uuidgen)
response_file="${TMPDIR}/seatbelt-probe-response.json"
status=$(/usr/bin/curl --silent --show-error --max-time 5 \
  --noproxy '*' --output "$response_file" --write-out '%{http_code}' \
  --header "Authorization: Bearer ${task_token}" \
  --header "X-Lilies-Request-ID: ${request_id}" \
  --header "X-Lilies-Assignment-ID: ${assignment_id}" \
  --header "X-Lilies-Session-ID: ${session_id}" \
  --header "X-Lilies-Tool-Call-ID: seatbelt-boundary-probe" \
  --header "X-Lilies-Idempotency-Key: seatbelt.boundary.probe.0001" \
  --header "X-Lilies-Contract-Digest: sha256:0000000000000000000000000000000000000000000000000000000000000000" \
  "${base_url%/}${contract_url}") || exit 33
[ "$status" = 200 ] && [ -s "$response_file" ] || exit 34
/bin/rm -f "$response_file"
/usr/bin/printf '{"api_status":200,"forbidden_denied":%s,"public_read":true}\\n' "$#"
"""


def _regular_nofollow_exists(path: Path) -> bool:
    try:
        parent = _open_directory_nofollow(_absolute_lexical_path(path).parent)
    except (CodexBuilderChildError, OSError):
        return False
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
    except OSError:
        os.close(parent)
        return False
    os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        return stat.S_ISREG(metadata.st_mode)
    finally:
        os.close(descriptor)


def _state_root_for_workspace(public_workspace: Path) -> Path:
    for candidate in (public_workspace, *public_workspace.parents):
        if candidate.name == "platform-workspaces":
            return candidate.parent
    raise CodexBuilderChildError(
        "public workspace does not reveal its isolated state-root boundary"
    )


def _seatbelt_forbidden_probe_paths(
    *,
    handoff: Mapping[str, Any],
    public_workspace: Path,
) -> tuple[Path, ...]:
    task = _require_mapping(handoff.get("task"), "task")
    task_id = task.get("task_id")
    revision = task.get("revision")
    if not isinstance(task_id, str) or not isinstance(revision, int):
        raise CodexBuilderChildError("probe task binding is invalid")
    state_root = _state_root_for_workspace(public_workspace)
    repository_source = ROOT / "pyproject.toml"
    platform_database = state_root / "platform-data" / "agent_platform.db"
    protected_root = (
        state_root
        / "platform-data"
        / "task-packages"
        / "packages"
        / task_id
        / str(revision)
        / "protected"
    )
    protected_candidates = sorted(
        candidate
        for candidate in protected_root.rglob("*")
        if candidate.is_file()
        and "oracle" not in {part.casefold() for part in candidate.parts}
    )
    oracle_candidates = sorted(
        candidate
        for candidate in (protected_root / "oracle").rglob("*")
        if candidate.is_file()
    )
    targets = [
        repository_source,
        platform_database,
        protected_candidates[0] if protected_candidates else Path(),
        oracle_candidates[0] if oracle_candidates else Path(),
    ]
    if any(not target.is_absolute() for target in targets) or not all(
        _regular_nofollow_exists(target) for target in targets
    ):
        raise CodexBuilderChildError(
            "Seatbelt probe requires real repository, database, protected, and oracle targets"
        )
    return tuple(targets)


def _ensure_private_fixture(path: Path, payload: bytes) -> None:
    if not path.exists() and not path.is_symlink():
        _write_private_bytes(path, payload)
        return
    try:
        existing, _ = _read_nofollow_regular(
            path,
            size_limit=MAX_HANDOFF_BYTES,
            required_mode=0o600,
        )
    except (CodexBuilderChildError, OSError) as error:
        raise CodexBuilderChildError("isolated runtime fixture is unsafe") from error
    if not hmac.compare_digest(existing, payload):
        raise CodexBuilderChildError("isolated runtime fixture changed")


def _run_seatbelt_negative_probe(
    *,
    public_workspace: Path,
    public_probe_path: Path,
    handoff_path: Path,
    handoff: Mapping[str, Any],
    runtime_root: Path,
    user_home: Path,
    provider_proxy_port: int,
    platform_port: int,
    forbidden_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    probe_path = runtime_root / SEATBELT_PROBE_FILE
    _ensure_private_fixture(probe_path, _SEATBELT_PROBE_SOURCE)
    targets = (
        tuple(forbidden_paths)
        if forbidden_paths is not None
        else _seatbelt_forbidden_probe_paths(
            handoff=handoff,
            public_workspace=public_workspace,
        )
    )
    if len(targets) < 4 or not all(
        target.is_absolute() and _regular_nofollow_exists(target) for target in targets
    ):
        raise CodexBuilderChildError("Seatbelt negative probe targets are incomplete")
    probe_shell = Path("/bin/sh")
    command = _sandboxed_arguments(
        executable=probe_shell,
        codex_arguments=(
            str(probe_path),
            str(handoff_path),
            str(public_probe_path),
            str(_require_mapping(handoff.get("platform"), "platform").get("base_url")),
            str(
                _require_mapping(handoff.get("platform"), "platform").get(
                    "contract_url"
                )
            ),
            *(str(path) for path in targets),
        ),
        public_workspace=public_workspace,
        handoff_path=handoff_path,
        runtime_root=runtime_root,
        provider_proxy_port=provider_proxy_port,
        platform_port=platform_port,
    )
    environment = {
        "HOME": str(user_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(runtime_root / "tmp"),
        "no_proxy": "127.0.0.1,localhost",
    }
    process = subprocess.Popen(
        command,
        cwd=public_workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        umask=0o077,
    )
    stdout, stderr, timed_out = _communicate_isolated_process(
        process,
        input_bytes=None,
        timeout_seconds=15,
    )
    if timed_out or process.returncode != 0:
        diagnostic = _sanitize_bytes(
            stderr,
            redactions=(
                str(
                    _require_mapping(handoff.get("platform"), "platform").get(
                        "access_token"
                    )
                ),
                str(
                    _require_mapping(handoff.get("collaboration"), "collaboration").get(
                        "access_token"
                    )
                ),
                *(str(path) for path in targets),
            ),
            handoff_path=handoff_path,
        ).decode("utf-8", errors="replace")[-500:]
        raise CodexBuilderChildError(
            "macOS Seatbelt boundary probe failed closed "
            f"(exit={process.returncode}, stderr={diagnostic!r})"
        )
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderChildError(
            "macOS Seatbelt boundary probe returned invalid evidence"
        ) from error
    if (
        not isinstance(result, dict)
        or result.get("api_status") != 200
        or result.get("forbidden_denied") != len(targets)
        or result.get("public_read") is not True
    ):
        raise CodexBuilderChildError(
            "macOS Seatbelt boundary probe did not prove the expected boundary"
        )
    return {
        "schema_version": "v0.4.13-t01h-seatbelt-probe-1",
        "api_status": 200,
        "forbidden_read_count": len(targets),
        "public_workspace_read": True,
    }


def _public_api_manual() -> dict[str, Any]:
    return {
        "schema_version": "v0.4.13-t01h-external-builder-api-manual-1",
        "authority": {
            "platform_bearer": "handoff.platform.access_token",
            "collaboration_bearer": "handoff.collaboration.access_token",
            "never_print_bearers": True,
            "formal_archive_supported": True,
            "human_monitoring_required": False,
            "approval_mode_required": "auto_forward",
            "permission_auto_expansion_enabled": False,
        },
        "platform": {
            "contract": "GET {platform.base_url}{platform.contract_url}",
            "operation_count": len(PUBLIC_OPERATION_SPECS),
            "required_headers": {
                "Authorization": "Bearer {platform.access_token}",
                "X-Lilies-Request-ID": "UUID",
                "X-Lilies-Assignment-ID": "{assignment.assignment_id}",
                "X-Lilies-Session-ID": "{assignment.session_id}",
                "X-Lilies-Tool-Call-ID": "1..200 safe correlation characters",
                "X-Lilies-Idempotency-Key": "16..128 safe correlation characters",
                "X-Lilies-Contract-Digest": "{platform.contract_digest}; use zero digest only for initial contract GET",
            },
            "rule": (
                "Fetch the contract first, then use only the operations it declares and "
                "their rendered request schemas. Do not call internal endpoints."
            ),
        },
        "collaboration": {
            "required_headers": {
                "Authorization": "Bearer {collaboration.access_token}",
                "Content-Type": "application/json",
            },
            "endpoints": {
                "channel_state": "GET /api/v1/collaboration/channels/{channel_id}",
                "submit_report": "POST /api/v1/collaboration/channels/{channel_id}/reports",
                "revise_report": "POST /api/v1/collaboration/channels/{channel_id}/reports/{report_id}/revisions",
                "events": "GET /api/v1/collaboration/channels/{channel_id}/events?format=json&after={cursor}&limit=100",
                "ack": "POST /api/v1/collaboration/channels/{channel_id}/acks",
                "reprobe": "POST /api/v1/collaboration/channels/{channel_id}/reports/{report_id}/reprobes",
            },
            "channel_state_response_schema": {
                "schema_version": "1.0",
                "channel_id": "UUID",
                "task_id": "string",
                "task_revision": "integer >= 1",
                "assignment_id": "UUID",
                "lilies_session_id": "UUID",
                "application_ids": ["UUID"],
                "approval_mode": ["manual", "auto_forward"],
                "max_report_evidence_rounds": "integer >= 1",
                "status": "channel status enum",
                "revision": "integer >= 1; use as expected_channel_revision",
                "next_seq": "integer >= 1",
                "created_at": "UTC timestamp",
                "reader_cursor": {
                    "schema_version": "1.0",
                    "channel_id": "UUID",
                    "reader_role": "lilies",
                    "reader_id": "opaque actor id",
                    "ack_seq": "integer >= 0",
                    "revision": "integer >= 0; use as expected_cursor_revision",
                    "updated_at": "optional UTC timestamp",
                },
            },
            "events_response_schema": {
                "channel_id": "UUID",
                "after": "integer >= 0",
                "next_cursor": "integer >= after",
                "events": [
                    "redacted CollaborationMessageEnvelope objects in sequence order"
                ],
                "history_replay": "optional true only when explicitly requested",
            },
            "developer_response_revision_transition": {
                "applies_only_when": (
                    "the latest same-report event has payload_schema="
                    "collaboration.developer_response.v1, message_type="
                    "developer_response, and sender_role=codex"
                ),
                "consumed_report_revision": "event.payload.report_revision",
                "resulting_report_revision": (
                    "event.payload.report_revision + 1; developer response persistence "
                    "atomically performs exactly this one report transition"
                ),
                "reprobe_expected_report_revision": "resulting_report_revision",
                "guard": (
                    "Do not increment an arbitrary report revision, do not subtract from "
                    "the developer response revision, and require event.correlation_id="
                    "event.payload.report_id for the same reprobed report."
                ),
            },
            "ack_request_schema": {
                "additionalProperties": False,
                "required": [
                    "idempotency_key",
                    "expected_cursor_revision",
                    "reader_role",
                    "reader_id",
                    "ack_seq",
                ],
                "properties": {
                    "idempotency_key": "16..128 safe characters",
                    "expected_cursor_revision": "channel_state.reader_cursor.revision",
                    "reader_role": "lilies",
                    "reader_id": "channel_state.reader_cursor.reader_id",
                    "ack_seq": "events.next_cursor",
                },
                "response": "updated ReaderCursor matching channel and reader",
            },
            "report_submit_schema": {
                "additionalProperties": False,
                "required": [
                    "idempotency_key",
                    "expected_channel_revision",
                    "report",
                ],
                "properties": {
                    "idempotency_key": "16..128 safe characters",
                    "expected_channel_revision": "integer >= 1 from channel_state",
                    "report": {
                        "additionalProperties": False,
                        "required": [
                            "schema_version",
                            "report_id",
                            "category",
                            "phase",
                            "severity",
                            "summary",
                            "original_goal",
                            "requirement_digest",
                            "platform_contract_digest",
                            "manuals_checked",
                            "attempted_routes",
                            "expected",
                            "actual",
                            "missing_contract",
                            "blocking_scope",
                            "independent_work",
                            "workaround_considered",
                            "workaround_loss",
                            "requested_outcome",
                            "confidence",
                            "secret_redactions",
                            "evidence_refs",
                        ],
                        "properties": {
                            "schema_version": "1.0",
                            "report_id": "UUID",
                            "category": [
                                "task_spec_gap",
                                "environment_gap",
                                "platform_capability_gap",
                                "platform_defect_suspected",
                            ],
                            "phase": [
                                "preflight",
                                "planning",
                                "draft_mutation",
                                "run",
                                "acceptance",
                                "resume",
                            ],
                            "severity": ["blocking", "major", "minor"],
                            "summary": "1..500 characters",
                            "original_goal": "1..10000 characters",
                            "requirement_digest": "sha256:<64 lowercase hex>",
                            "platform_contract_digest": "sha256:<64 lowercase hex>",
                            "manuals_checked": [
                                {
                                    "manual_id": "opaque reference",
                                    "version": "1..80 characters",
                                    "digest": "sha256:<64 lowercase hex>",
                                }
                            ],
                            "attempted_routes": [
                                {
                                    "attempt_id": "UUID",
                                    "route": "3..500 characters",
                                    "input_digest": "sha256:<64 lowercase hex>",
                                    "outcome": "1..5000 characters",
                                    "evidence_refs": [
                                        "one or more EvidenceRef objects"
                                    ],
                                    "attempted_at": "UTC timestamp",
                                }
                            ],
                            "expected": "non-empty, <=20000 characters",
                            "actual": "non-empty, <=20000 characters",
                            "reproduction": "required non-empty list for platform_defect_suspected",
                            "missing_contract": "required non-empty string for platform_capability_gap",
                            "blocking_scope": "1..10000 characters",
                            "independent_work": "unique strings, <=100",
                            "workaround_considered": "one or more unique strings",
                            "workaround_loss": "1..10000 characters",
                            "requested_outcome": "1..10000 characters",
                            "confidence": "number 0..1",
                            "secret_redactions": "unique strings, no plaintext secret",
                            "evidence_refs": [
                                {
                                    "evidence_id": "opaque reference",
                                    "kind": [
                                        "artifact",
                                        "archive",
                                        "trace",
                                        "run",
                                        "test_run",
                                        "contract",
                                        "manual",
                                        "task_package",
                                        "host_receipt",
                                        "health_check",
                                        "source_commit",
                                        "browser",
                                        "other",
                                    ],
                                    "digest": "sha256:<64 lowercase hex>",
                                    "media_type": "1..200 characters",
                                    "label": "1..240 characters",
                                    "captured_at": "UTC timestamp",
                                }
                            ],
                        },
                    },
                },
            },
            "report_submit_response_schema": {
                "status_code": 201,
                "body": (
                    "persisted CollaborationReport plus completeness_issues; "
                    "auto_forward may already advance route/status/revision"
                ),
            },
            "report_revision_schema": {
                "additionalProperties": False,
                "required": [
                    "idempotency_key",
                    "expected_report_revision",
                    "report",
                ],
                "properties": {
                    "idempotency_key": "16..128 safe characters",
                    "expected_report_revision": "latest persisted report revision",
                    "report": (
                        "the complete report_submit_schema.report object with the "
                        "same report_id, category, original_goal, and requirement_digest"
                    ),
                },
                "response": (
                    "updated CollaborationReport plus completeness_issues; require "
                    "completeness_issues=[] before waiting for enablement"
                ),
            },
            "complete_platform_capability_gap": (
                "A 201 response must have completeness_issues=[] before the "
                "attempt may stop for development enablement. Include non-empty "
                "attempted_routes, expected, actual, evidence_refs, "
                "platform_contract_digest, manuals_checked, and missing_contract."
            ),
            "platform_capability_gap_template": {
                "idempotency_key": "replace.with.16plus.safe.characters",
                "expected_channel_revision": 1,
                "report": {
                    "schema_version": "1.0",
                    "report_id": "replace-with-new-uuid",
                    "category": "platform_capability_gap",
                    "phase": "planning",
                    "severity": "blocking",
                    "summary": "replace with the observed capability gap",
                    "original_goal": "replace with the current frozen project goal",
                    "requirement_digest": "sha256:<digest current public requirement>",
                    "platform_contract_digest": "{platform.contract_digest}",
                    "manuals_checked": [
                        {
                            "manual_id": "external-builder-api-manual",
                            "version": "v0.4.13-t01h-external-builder-api-manual-1",
                            "digest": "sha256:<digest BUILDER_API_MANUAL.json>",
                        }
                    ],
                    "attempted_routes": [
                        {
                            "attempt_id": "replace-with-new-uuid",
                            "route": "replace with exact public operation attempted",
                            "input_digest": "sha256:<digest redacted request>",
                            "outcome": "replace with factual public response",
                            "evidence_refs": [
                                {
                                    "evidence_id": "public-attempt-evidence",
                                    "kind": "trace",
                                    "digest": "sha256:<digest redacted evidence>",
                                    "media_type": "application/json",
                                    "label": "redacted public operation evidence",
                                    "captured_at": "replace-with-current-utc-timestamp",
                                }
                            ],
                            "attempted_at": "replace-with-current-utc-timestamp",
                        }
                    ],
                    "expected": "replace with the contract behavior required",
                    "actual": "replace with the observed public behavior",
                    "missing_contract": "replace with the exact missing capability",
                    "blocking_scope": "replace with impact on the current project",
                    "independent_work": [],
                    "workaround_considered": [
                        "replace with a considered contract-compliant workaround"
                    ],
                    "workaround_loss": "replace with why the workaround is insufficient",
                    "requested_outcome": "replace with the minimum platform enablement",
                    "confidence": 1.0,
                    "secret_redactions": [],
                    "evidence_refs": [
                        {
                            "evidence_id": "public-gap-evidence",
                            "kind": "trace",
                            "digest": "sha256:<digest redacted evidence>",
                            "media_type": "application/json",
                            "label": "redacted capability-gap evidence",
                            "captured_at": "replace-with-current-utc-timestamp",
                        }
                    ],
                },
            },
            "auto_forward": (
                "Verify channel_state.approval_mode=auto_forward. Do not call "
                "Studio owner endpoints and do not auto-approve permissions."
            ),
            "reprobe": (
                "After a development response, rerun the same frozen project and use "
                "the reprobe endpoint with idempotency_key, the resulting/current "
                "report revision defined by developer_response_revision_transition, "
                "and a result matching the public response schema."
            ),
            "reprobe_request_schema": {
                "additionalProperties": False,
                "required": [
                    "idempotency_key",
                    "expected_report_revision",
                    "result",
                ],
                "properties": {
                    "idempotency_key": "16..128 safe characters",
                    "expected_report_revision": (
                        "resulting/current revision from the latest exact same-report "
                        "developer_response transition; do not reuse its consumed "
                        "event.payload.report_revision"
                    ),
                    "result": {
                        "additionalProperties": False,
                        "required": [
                            "schema_version",
                            "reprobe_id",
                            "outcome",
                            "contract_digest",
                            "steps",
                            "expected",
                            "actual",
                            "evidence_refs",
                        ],
                        "properties": {
                            "schema_version": "1.0",
                            "reprobe_id": "UUID",
                            "outcome": [
                                "lilies_verified",
                                "verification_failed",
                            ],
                            "contract_digest": (
                                "sha256 digest required by the latest developer response"
                            ),
                            "steps": [
                                {
                                    "order": "contiguous integer from 1",
                                    "action": "1..4000 characters",
                                    "expected": "1..10000 characters",
                                }
                            ],
                            "expected": "1..20000 characters",
                            "actual": "1..20000 characters",
                            "evidence_refs": (
                                "one or more complete EvidenceRef objects using "
                                "the report evidence schema"
                            ),
                        },
                    },
                },
                "response": (
                    "persisted LiliesReprobeResult; process completion alone is "
                    "not a successful reprobe"
                ),
            },
        },
    }


def _codex_prompt(
    handoff_path: Path,
    manual_path: Path,
    guidance_path: Path,
    guidance_digest: str,
    guidance_text: str,
    *,
    resume_thread_id: str | None = None,
) -> str:
    continuity = (
        "Continue the same isolated Builder context and frozen assignment; do "
        "not reinterpret this resume as a new Builder or a new attempt. "
        if resume_thread_id is not None
        else ""
    )
    return (
        continuity
        + "You are the Builder for one enterprise workflow task. The current "
        "directory is your complete public work directory. Read the task requirement, "
        f"the public API manual at {manual_path}, and the operating guide at "
        f"{guidance_path} (verified digest {guidance_digest}). The guide is:\n"
        "<public_builder_operating_guide>\n"
        f"{guidance_text.rstrip()}\n"
        "</public_builder_operating_guide>\n"
        f"Read the private API handoff once at {handoff_path}; never print or save "
        "its bearers. The launcher already checked the work directory and its two "
        "platform control files. Do not audit manifests, inventory files, or inspect "
        "parent directories. Do not read source code, databases, hidden seeds, "
        "or oracle data. Fetch the public platform contract, then build the smallest "
        "safe workflow that fully meets the task. Aim to finish within ten minutes: "
        "apply coherent draft changes with few API round trips, validate immediately, "
        "run the public debug case, inspect public traces and customer-system receipts, "
        "repair factual failures, and publish. Use only the task-scoped public APIs. "
        "If those APIs genuinely cannot express a required reusable capability, "
        "submit one concise capability-gap report and stop. Finish with the public "
        "application, run, artifact, and version IDs."
    )


def _sanitize_bytes(
    value: bytes,
    *,
    redactions: Sequence[str],
    handoff_path: Path,
) -> bytes:
    text = value.decode("utf-8", errors="replace")
    replacements = [(secret, "<redacted-authority>") for secret in redactions if secret]
    replacements.append((str(handoff_path), "<private-handoff>"))
    for raw, replacement in replacements:
        text = text.replace(raw, replacement)
    text = _BEARER_AUTHORITY_PATTERN.sub(
        r"\1<redacted-authority>",
        text,
    )
    text = _STALE_AUTHORITY_PATTERN.sub(
        "<redacted-authority>",
        text,
    )
    return text.encode("utf-8")


def _codex_usage_details(
    transcript: bytes,
) -> tuple[dict[str, int], dict[str, Literal["reported", "not_reported"]]]:
    usage: dict[str, int] = {}
    support: dict[str, Literal["reported", "not_reported"]] = {
        key: "not_reported" for key in USAGE_FIELDS
    }
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        candidate = event.get("usage")
        if not isinstance(candidate, dict):
            continue
        for key in USAGE_FIELDS:
            value = candidate.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[key] = value
                support[key] = "reported"
            else:
                usage.pop(key, None)
                support[key] = "not_reported"
    return usage, support


def _codex_usage(transcript: bytes) -> dict[str, int]:
    """Compatibility view for older callers; persisted output uses field support."""

    usage, _ = _codex_usage_details(transcript)
    return {key: usage.get(key, 0) for key in USAGE_FIELDS}


def _safe_codex_transcript(
    transcript: bytes,
    *,
    redactions: Sequence[str],
    handoff_path: Path,
) -> bytes:
    allowed_event_types = {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.completed",
    }
    safe_lines: list[bytes] = []
    for raw_line in transcript.splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") not in allowed_event_types:
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            continue
        sanitized = _sanitize_bytes(
            _canonical_json(event),
            redactions=redactions,
            handoff_path=handoff_path,
        )
        safe_lines.append(sanitized)
    return b"\n".join(safe_lines) + (b"\n" if safe_lines else b"")


def _write_private_bytes(path: Path, value: bytes) -> None:
    if len(value) > MAX_TRANSCRIPT_BYTES:
        raise CodexBuilderChildError("Codex transcript exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise CodexBuilderChildError("Codex transcript target already exists")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(value):
            written += os.write(descriptor, value[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _thread_id(transcript: bytes) -> str | None:
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        candidate = event.get("thread_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _codex_security_config_arguments(
    rollout_token_limit: int,
) -> tuple[str, ...]:
    if rollout_token_limit < 2 or rollout_token_limit > MAX_ROLLOUT_TOKEN_LIMIT:
        raise CodexBuilderChildError(
            f"rollout token limit must be between 2 and {MAX_ROLLOUT_TOKEN_LIMIT}"
        )
    reminder_tokens = tuple(
        dict.fromkeys(
            value
            for value in (
                rollout_token_limit // 2,
                rollout_token_limit // 10,
            )
            if 0 < value < rollout_token_limit
        )
    )
    return (
        "-c",
        "features.rollout_budget.enabled=true",
        "-c",
        f"features.rollout_budget.limit_tokens={rollout_token_limit}",
        "-c",
        "features.rollout_budget.sampling_token_weight=1.0",
        "-c",
        "features.rollout_budget.prefill_token_weight=1.0",
        "-c",
        "features.rollout_budget.reminder_at_remaining_tokens="
        f"{json.dumps(reminder_tokens, separators=(',', ':'))}",
        "-c",
        "features.collab=false",
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.multi_agent_v2=false",
    )


def _verify_codex_security_features(
    *,
    executable: Path,
    environment: Mapping[str, str],
    cwd: Path,
    rollout_token_limit: int,
) -> dict[str, bool]:
    result = subprocess.run(
        (
            str(executable),
            *_codex_security_config_arguments(rollout_token_limit),
            "features",
            "list",
        ),
        cwd=cwd,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise CodexBuilderChildError(
            "Codex CLI rejected mandatory rollout-budget configuration"
        )
    states: dict[str, bool] = {}
    for row in result.stdout.splitlines():
        match = re.match(r"^\s*(\S+)\s+.*\s+(true|false)\s*$", row)
        if match is not None:
            states[match.group(1)] = match.group(2) == "true"
    expected = {
        "rollout_budget": True,
        "multi_agent": False,
        "multi_agent_v2": False,
    }
    if any(states.get(name) is not enabled for name, enabled in expected.items()):
        raise CodexBuilderChildError(
            "Codex CLI cannot prove mandatory budget and single-agent enforcement"
        )
    return expected


def _codex_execution_arguments(
    *,
    public_workspace: Path,
    model: str,
    resume_thread_id: str | None,
    rollout_token_limit: int = DEFAULT_ROLLOUT_TOKEN_LIMIT,
    invocation_binding: InvocationBinding | None = None,
) -> tuple[str, ...]:
    budget_arguments = _codex_security_config_arguments(rollout_token_limit)
    binding_arguments = (
        invocation_binding.codex_config_arguments()
        if invocation_binding is not None
        else ()
    )
    common_arguments = (
        "-a",
        "never",
        "-C",
        str(public_workspace),
        "--sandbox",
        "danger-full-access",
        *budget_arguments,
        *binding_arguments,
        "exec",
    )
    execution_options = (
        "-m",
        model,
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
    )
    if resume_thread_id is not None:
        return (
            *common_arguments,
            "resume",
            *execution_options,
            resume_thread_id,
            "-",
        )
    return (
        *common_arguments,
        *execution_options,
        "--color",
        "never",
        "-",
    )


def _run(args: argparse.Namespace) -> int:
    if args.timeout_seconds < 1 or args.timeout_seconds > 86_400:
        raise CodexBuilderChildError(
            "Codex timeout must be between 1 and 86400 seconds"
        )
    if (
        args.rollout_token_limit < 2
        or args.rollout_token_limit > MAX_ROLLOUT_TOKEN_LIMIT
    ):
        raise CodexBuilderChildError(
            f"rollout token limit must be between 2 and {MAX_ROLLOUT_TOKEN_LIMIT}"
        )
    source_handoff_path = _absolute_lexical_path(args.handoff)
    handoff = _read_private_handoff(source_handoff_path)
    public_workspace, platform_url, platform_port, redactions = _validate_handoff(
        handoff
    )
    workspace_verification = _verify_public_workspace(
        handoff=handoff,
        public_workspace=public_workspace,
    )
    codex = shutil.which(args.codex_executable)
    if codex is None:
        raise CodexBuilderChildError("Codex CLI is unavailable")
    runtime_root = _absolute_lexical_path(args.runtime_root)
    resume = args.resume_thread_id is not None
    if resume:
        codex_home, user_home, billing = _resume_runtime_identity(
            runtime_root,
            thread_id=args.resume_thread_id,
        )
    else:
        if runtime_root.exists() or runtime_root.is_symlink():
            raise CodexBuilderChildError("isolated Codex runtime root already exists")
        runtime_root.mkdir(parents=True, mode=0o700)
        codex_home, user_home, billing = _prepare_isolated_codex_identity(runtime_root)
    temporary_directory = runtime_root / "tmp"
    if resume:
        _safe_runtime_directory(
            temporary_directory,
            label="isolated temporary directory",
        )
    else:
        temporary_directory.mkdir(mode=0o700)
    handoff_path = _runtime_authority_path(
        runtime_root,
        handoff=handoff,
        resume=resume,
        resume_thread_id=args.resume_thread_id,
        authority_transition_path=args.resume_authority_transition,
    )
    manual_path = workspace_verification.manual_path
    manual_digest = workspace_verification.manual_digest
    guidance_path = workspace_verification.guidance_path
    guidance_digest = workspace_verification.guidance_digest
    guidance_text = workspace_verification.guidance_text
    prompt = _codex_prompt(
        handoff_path,
        manual_path,
        guidance_path,
        guidance_digest,
        guidance_text,
        resume_thread_id=args.resume_thread_id,
    )
    invocation_binding = InvocationBinding.create(
        runtime_root=runtime_root,
        public_workspace=public_workspace,
    )
    codex_arguments = _codex_execution_arguments(
        public_workspace=public_workspace,
        model=args.model,
        resume_thread_id=args.resume_thread_id,
        rollout_token_limit=args.rollout_token_limit,
        invocation_binding=invocation_binding,
    )
    with _AllowlistedConnectProxy(CODEX_ALLOWED_PROVIDER_HOSTS) as proxy:
        environment, _ = _clean_external_builder_environment(
            codex_home=codex_home,
            user_home=user_home,
            temporary_directory=temporary_directory,
            proxy_port=proxy.port,
        )
        environment["LILIES_EXTERNAL_BUILDER_HANDOFF"] = str(handoff_path)
        environment["LILIES_PUBLIC_PLATFORM_URL"] = platform_url
        environment.update(invocation_binding.environment())
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["no_proxy"] = "127.0.0.1,localhost"
        environment_keys = sorted(environment)
        security_feature_support = _verify_codex_security_features(
            executable=Path(codex),
            environment=environment,
            cwd=public_workspace,
            rollout_token_limit=args.rollout_token_limit,
        )
        seatbelt_probe = _run_seatbelt_negative_probe(
            public_workspace=public_workspace,
            public_probe_path=workspace_verification.public_probe_path,
            handoff_path=handoff_path,
            handoff=handoff,
            runtime_root=runtime_root,
            user_home=user_home,
            provider_proxy_port=proxy.port,
            platform_port=platform_port,
        )
        command = _sandboxed_arguments(
            executable=Path(codex),
            codex_arguments=codex_arguments,
            public_workspace=public_workspace,
            handoff_path=handoff_path,
            runtime_root=runtime_root,
            provider_proxy_port=proxy.port,
            platform_port=platform_port,
        )
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            cwd=public_workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
        )
        stdout, stderr, timed_out = _communicate_isolated_process(
            process,
            input_bytes=prompt.encode("utf-8"),
            timeout_seconds=args.timeout_seconds,
            invocation_binding=invocation_binding,
        )
        returncode = 124 if timed_out else int(process.returncode or 0)
    usage, usage_field_support = _codex_usage_details(stdout)
    sanitized_stdout = _safe_codex_transcript(
        stdout,
        redactions=redactions,
        handoff_path=handoff_path,
    )
    sanitized_stderr = _sanitize_bytes(
        stderr,
        redactions=redactions,
        handoff_path=handoff_path,
    )
    _write_private_bytes(args.transcript.resolve(), sanitized_stdout)
    _write_private_bytes(args.stderr_log.resolve(), sanitized_stderr)
    observed_thread_id = _thread_id(sanitized_stdout)
    if resume and observed_thread_id not in {None, args.resume_thread_id}:
        raise CodexBuilderChildError(
            "resumed Codex process reported another Builder thread"
        )
    current_thread_id = observed_thread_id or args.resume_thread_id
    resume_state_path: Path | None = None
    resume_state_digest: str | None = None
    if current_thread_id is not None:
        resume_state_path, resume_state_digest = _resume_state_binding(
            sessions_root=codex_home / "sessions",
            thread_id=current_thread_id,
        )
    elif returncode == 0 and not timed_out:
        raise CodexBuilderChildError(
            "successful Codex execution omitted its persistent Builder thread"
        )
    result = {
        "schema_version": "v0.4.13-t01h-codex-builder-child-1",
        "builder_actor": "codex",
        "thread_id": current_thread_id,
        "resumed_thread": resume,
        "resume_state_path": (
            str(resume_state_path) if resume_state_path is not None else None
        ),
        "resume_state_digest": resume_state_digest,
        "exit_code": returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
        "usage": usage,
        "usage_field_support": usage_field_support,
        "rollout_budget": {
            "enforcement": "codex_cli_rollout_budget",
            "limit_tokens": args.rollout_token_limit,
            "maximum_allowed_limit_tokens": MAX_ROLLOUT_TOKEN_LIMIT,
            # This counter belongs to this Codex CLI process. The parent
            # runner persists reported usage and supplies only the remaining
            # cumulative budget to a later exact-thread resume process.
            "continues_on_exact_thread_resume": False,
            "token_weights": {
                "sampling": 1.0,
                "prefill": 1.0,
            },
            "multi_agent_enabled": False,
            "config_supported": security_feature_support,
        },
        "public_api_manual_digest": manual_digest,
        "public_api_manual_source": "workspace_manifest",
        "public_builder_guidance_digest": guidance_digest,
        "public_builder_guidance_source": "workspace_manifest",
        "transcript_digest": _digest(sanitized_stdout),
        "stderr_digest": _digest(sanitized_stderr),
        "sandbox": "macos-seatbelt",
        "sandbox_probe": seatbelt_probe,
        "inner_codex_sandbox": "danger-full-access-inside-seatbelt",
        "filesystem_read_boundary": "public-workspace+private-handoff+runtime",
        "network_boundary": {
            "platform": "loopback-only",
            "provider_hosts": list(CODEX_ALLOWED_PROVIDER_HOSTS),
        },
        "clean_environment_keys": environment_keys,
        "billing_mode": billing["billing_mode"],
        "formal_archive_supported": True,
    }
    _write_private_bytes(args.result.resolve(), _canonical_json(result))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if returncode == 0 and not timed_out else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch isolated Codex against one external Builder handoff."
    )
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--stderr-log", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=10_800)
    parser.add_argument(
        "--rollout-token-limit",
        type=int,
        default=DEFAULT_ROLLOUT_TOKEN_LIMIT,
        help=(
            "Codex CLI-local rollout token limit for this process; the parent "
            "runner supplies persisted cross-invocation remaining budget, "
            f"never exceeding {MAX_ROLLOUT_TOKEN_LIMIT}."
        ),
    )
    parser.add_argument(
        "--resume-thread-id",
        help=(
            "Resume this exact thread from an existing isolated runtime root; "
            "never creates a new Builder context."
        ),
    )
    parser.add_argument(
        "--resume-authority-transition",
        type=Path,
        help=(
            "Private parent-runner evidence authorizing one adjacent immutable "
            "project-revision authority rollover for the exact resumed thread."
        ),
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        return _run(args)
    except _ForwardedTermination as error:
        print(
            f"isolated Codex Builder child terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except (CodexBuilderChildError, OSError, ValueError) as error:
        print(
            f"isolated Codex Builder child failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
