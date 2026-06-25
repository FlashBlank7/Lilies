from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .models import NetworkPolicy


class SandboxError(RuntimeError):
    pass


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


class SandboxSession:
    def __init__(
        self,
        *,
        settings: Settings,
        session_id: str,
        workspace: Path,
        mount_source: Path,
        network_policy: NetworkPolicy,
        network_allowlist: list[str],
    ) -> None:
        self.settings = settings
        self.session_id = session_id
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]
        self.container_name = f"agent-{safe_id}-{uuid4().hex[:8]}"
        self.workspace = workspace
        self.mount_source = mount_source
        self.network_policy = network_policy
        self.network_allowlist = network_allowlist
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        network = "none" if self.network_policy == NetworkPolicy.none else "bridge"
        command = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            self.container_name,
            "--network",
            network,
            "--cpus",
            str(self.settings.sandbox_cpus),
            "--memory",
            self.settings.sandbox_memory,
            "--pids-limit",
            str(self.settings.sandbox_pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{self.settings.sandbox_uid}:{self.settings.sandbox_gid}",
            "--volume",
            f"{self.mount_source}:/workspace:rw",
            "--workdir",
            "/workspace",
        ]
        if self.network_policy == NetworkPolicy.allowlist:
            command.extend(["--env", f"AGENT_NETWORK_ALLOWLIST={','.join(self.network_allowlist)}"])
        command.extend([self.settings.sandbox_image, "sleep", "infinity"])
        result = await self._host_command(command, timeout=60)
        if result.exit_code != 0:
            raise SandboxError(f"failed to start sandbox: {result.stderr or result.stdout}")
        self.started = True

    async def stop(self) -> None:
        if not self.started:
            return
        await self._host_command(["docker", "rm", "-f", self.container_name], timeout=30)
        self.started = False

    async def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        max_output: int = 200_000,
    ) -> CommandResult:
        await self.start()
        command = ["docker", "exec"]
        if stdin is not None:
            command.append("-i")
        command.extend([self.container_name, *argv])
        result = await self._host_command(
            command, stdin=stdin, timeout=timeout or self.settings.sandbox_command_timeout
        )
        result.stdout = result.stdout[:max_output]
        result.stderr = result.stderr[:max_output]
        return result

    async def _host_command(
        self, argv: list[str], *, stdin: str | None = None, timeout: float
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise SandboxError(f"command timed out after {timeout}s: {argv[0]}")
        return CommandResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=process.returncode or 0,
        )


class SandboxManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[str, SandboxSession] = {}

    def resolve_workspace(self, requested: str, *, create: bool = False) -> Path:
        root = self.settings.workspace_root.resolve()
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise SandboxError(f"workspace must be inside {root}")
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise SandboxError(f"workspace does not exist: {resolved}")
        return resolved

    async def get_or_create(
        self,
        session_id: str,
        workspace_path: str,
        network_policy: NetworkPolicy,
        network_allowlist: list[str],
    ) -> SandboxSession:
        existing = self.sessions.get(session_id)
        if existing:
            return existing
        workspace = self.resolve_workspace(workspace_path)
        sandbox = SandboxSession(
            settings=self.settings,
            session_id=session_id,
            workspace=workspace,
            mount_source=self._host_workspace(workspace),
            network_policy=network_policy,
            network_allowlist=network_allowlist,
        )
        await sandbox.start()
        self.sessions[session_id] = sandbox
        return sandbox

    def _host_workspace(self, workspace: Path) -> Path:
        if self.settings.workspace_host_root is None:
            return workspace
        relative = workspace.relative_to(self.settings.workspace_root.resolve())
        return (self.settings.workspace_host_root.resolve() / relative).resolve()

    async def remove(self, session_id: str) -> None:
        sandbox = self.sessions.pop(session_id, None)
        if sandbox:
            await sandbox.stop()

    async def close(self) -> None:
        await asyncio.gather(*(sandbox.stop() for sandbox in list(self.sessions.values())))
        self.sessions.clear()
