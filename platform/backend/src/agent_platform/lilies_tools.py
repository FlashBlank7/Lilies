from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import stat
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import ToolDefinition


class LiliesToolError(RuntimeError):
    pass


class StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class LiliesToolContext:
    session_id: str
    workspace: Path
    turn_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class LiliesToolResult:
    content: str
    is_error: bool = False


_WORKSPACE_POLICY_FILE = ".lilies-workspace-policy.json"
_MAX_WORKSPACE_POLICY_BYTES = 32 * 1024


def _workspace_policy(workspace: Path) -> dict[str, tuple[str, ...]] | None:
    policy_path = workspace.resolve() / _WORKSPACE_POLICY_FILE
    if not policy_path.exists():
        return None
    if policy_path.is_symlink():
        raise LiliesToolError("workspace policy is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(policy_path, flags)
    except OSError as error:
        raise LiliesToolError("workspace policy is not readable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LiliesToolError("workspace policy is not an isolated regular file")
        raw = os.read(descriptor, _MAX_WORKSPACE_POLICY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_WORKSPACE_POLICY_BYTES:
        raise LiliesToolError("workspace policy is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiliesToolError("workspace policy is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "denied_segments", "writable_prefixes"}
        or value.get("schema_version") != "1.0"
        or not isinstance(value.get("denied_segments"), list)
        or not isinstance(value.get("writable_prefixes"), list)
        or not all(
            isinstance(item, str) and item and "/" not in item and "\\" not in item
            for item in value["denied_segments"]
        )
        or not all(
            isinstance(item, str)
            and item
            and not item.startswith("/")
            and "\\" not in item
            and ".." not in Path(item).parts
            for item in value["writable_prefixes"]
        )
    ):
        raise LiliesToolError("workspace policy is invalid")
    return {
        "denied_segments": tuple(value["denied_segments"]),
        "writable_prefixes": tuple(value["writable_prefixes"]),
    }


def _workspace_request_parts(requested: str) -> tuple[str, ...]:
    if "\x00" in requested or "\\" in requested:
        raise LiliesToolError("workspace path is not canonical")
    path = PurePosixPath(requested)
    if path.is_absolute():
        raise LiliesToolError("workspace path is not canonical")
    parts = path.parts
    if any(part in {"..", ""} for part in parts):
        raise LiliesToolError("workspace path is not canonical")
    return tuple(part for part in parts if part != ".")


def _enforce_workspace_policy(
    workspace: Path,
    requested: str,
    *,
    for_write: bool,
) -> None:
    policy = _workspace_policy(workspace)
    if policy is None:
        return
    parts = _workspace_request_parts(requested)
    denied = {item.casefold() for item in policy["denied_segments"]}
    if any(part.casefold() in denied for part in parts):
        raise LiliesToolError("path is reserved by the formal task workspace")
    if not for_write:
        return
    requested_path = PurePosixPath(*parts).as_posix() if parts else "."
    allowed = any(
        requested_path == prefix or requested_path.startswith(f"{prefix}/")
        for prefix in policy["writable_prefixes"]
    )
    if not allowed:
        raise LiliesToolError("path is read-only in the formal task workspace")


class LiliesTool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]
    dangerous: bool = False
    mutating: bool = False
    side_effecting: bool = False
    requires_permission: bool | None = None
    handles_input_validation: bool = False
    max_result_chars: int = 100_000
    preserve_result_integrity: bool = False

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )

    @abstractmethod
    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult: ...


def _resolve_workspace_path(workspace: Path, requested: str, *, for_write: bool = False) -> Path:
    _enforce_workspace_policy(workspace, requested, for_write=for_write)
    root = workspace.resolve()
    candidate = root / requested
    if for_write and not candidate.exists():
        parent = candidate.parent.resolve()
        resolved = parent / candidate.name
    else:
        resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise LiliesToolError("path escapes the session workspace")
    return resolved


class LocalTimeInput(StrictToolInput):
    pass


class LocalTimeTool(LiliesTool):
    name = "local_time"
    description = "Return the daemon's current UTC timestamp."
    input_model = LocalTimeInput

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        LocalTimeInput.model_validate(data)
        return LiliesToolResult(datetime.now(timezone.utc).isoformat())


class WorkspaceListInput(StrictToolInput):
    path: str = "."
    pattern: str = "*"
    limit: int = Field(default=500, ge=1, le=5000)


class WorkspaceListTool(LiliesTool):
    name = "workspace_list"
    description = "List files in the isolated local session workspace."
    input_model = WorkspaceListInput

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        args = WorkspaceListInput.model_validate(data)

        def run() -> str:
            path = _resolve_workspace_path(context.workspace, args.path)
            if not path.is_dir():
                raise LiliesToolError(f"not a directory: {args.path}")
            root = context.workspace.resolve()
            policy = _workspace_policy(context.workspace)
            denied = (
                {item.casefold() for item in policy["denied_segments"]}
                if policy is not None
                else set()
            )
            entries: list[str] = []
            for current, directories, files in os.walk(path, followlinks=False):
                directories[:] = sorted(
                    item for item in directories if item.casefold() not in denied
                )
                for name in [*directories, *sorted(files)]:
                    if name.casefold() in denied:
                        continue
                    item = Path(current) / name
                    relative = item.relative_to(root).as_posix()
                    if fnmatch.fnmatch(relative, args.pattern) or fnmatch.fnmatch(
                        item.name, args.pattern
                    ):
                        entries.append(relative + ("/" if item.is_dir() else ""))
                    if len(entries) >= args.limit:
                        return "\n".join(entries)
            return "\n".join(entries) or "(workspace is empty)"

        return LiliesToolResult(await asyncio.to_thread(run))


class WorkspaceReadInput(StrictToolInput):
    path: str = Field(min_length=1, max_length=1000)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=2000, ge=1, le=20_000)


class WorkspaceReadTool(LiliesTool):
    name = "workspace_read"
    description = "Read a UTF-8 file from the isolated local session workspace."
    input_model = WorkspaceReadInput

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        args = WorkspaceReadInput.model_validate(data)

        def run() -> str:
            path = _resolve_workspace_path(context.workspace, args.path)
            if not path.is_file():
                raise LiliesToolError(f"file not found: {args.path}")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            selected = lines[args.offset : args.offset + args.limit]
            return "\n".join(
                f"{number:6d}\t{line}"
                for number, line in enumerate(selected, start=args.offset + 1)
            )

        return LiliesToolResult(await asyncio.to_thread(run))


class WorkspaceWriteInput(StrictToolInput):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=1_000_000)


class WorkspaceWriteTool(LiliesTool):
    name = "workspace_write"
    description = "Create or replace a UTF-8 file in the isolated session workspace."
    input_model = WorkspaceWriteInput
    dangerous = True
    mutating = True
    side_effecting = True

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        args = WorkspaceWriteInput.model_validate(data)

        def run() -> str:
            path = _resolve_workspace_path(context.workspace, args.path, for_write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(args.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
            return f"wrote {path.relative_to(context.workspace.resolve()).as_posix()}"

        return LiliesToolResult(await asyncio.to_thread(run))


class WorkspacePatchInput(StrictToolInput):
    path: str = Field(min_length=1, max_length=1000)
    old_string: str = Field(min_length=1, max_length=500_000)
    new_string: str = Field(max_length=500_000)
    replace_all: bool = False


class WorkspacePatchTool(LiliesTool):
    name = "workspace_patch"
    description = "Replace an exact string in a UTF-8 workspace file."
    input_model = WorkspacePatchInput
    dangerous = True
    mutating = True
    side_effecting = True

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        args = WorkspacePatchInput.model_validate(data)

        def run() -> str:
            path = _resolve_workspace_path(
                context.workspace,
                args.path,
                for_write=True,
            )
            if not path.is_file():
                raise LiliesToolError(f"file not found: {args.path}")
            text = path.read_text(encoding="utf-8")
            count = text.count(args.old_string)
            if count == 0:
                raise LiliesToolError("old_string not found")
            if count > 1 and not args.replace_all:
                raise LiliesToolError(f"old_string has {count} matches")
            replacements = count if args.replace_all else 1
            updated = text.replace(args.old_string, args.new_string, -1 if args.replace_all else 1)
            path.write_text(updated, encoding="utf-8")
            return f"replaced {replacements} occurrence(s) in {args.path}"

        return LiliesToolResult(await asyncio.to_thread(run))


class LiliesToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, LiliesTool] = {}

    def register(self, tool: LiliesTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate Lilies tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> LiliesTool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"unknown Lilies tool: {name}") from error

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition() for name in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)


def build_lilies_core_registry() -> LiliesToolRegistry:
    registry = LiliesToolRegistry()
    for tool in (
        LocalTimeTool(),
        WorkspaceListTool(),
        WorkspaceReadTool(),
        WorkspaceWriteTool(),
        WorkspacePatchTool(),
    ):
        registry.register(tool)
    return registry
