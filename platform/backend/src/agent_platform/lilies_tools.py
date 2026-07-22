from __future__ import annotations

import asyncio
import fnmatch
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
            entries: list[str] = []
            for item in sorted(path.rglob("*")):
                relative = item.relative_to(root).as_posix()
                if fnmatch.fnmatch(relative, args.pattern) or fnmatch.fnmatch(
                    item.name, args.pattern
                ):
                    entries.append(relative + ("/" if item.is_dir() else ""))
                if len(entries) >= args.limit:
                    break
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
            path = _resolve_workspace_path(context.workspace, args.path)
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
