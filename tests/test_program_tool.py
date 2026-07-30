from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import AgentSpec, NetworkPolicy, PermissionMode
from agent_platform.sandbox import CommandResult
from agent_platform.tools import build_core_registry
from agent_platform.tools.base import ToolContext


class CapturingSandbox:
    def __init__(self, *, program_stdout: str = '{"value":42}', program_stderr: str = "") -> None:
        self.argv: list[str] = []
        self.stdin = ""
        self.program_stdout = program_stdout
        self.program_stderr = program_stderr

    async def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        **_: Any,
    ) -> CommandResult:
        self.argv = argv
        self.stdin = stdin or ""
        return CommandResult(
            stdout=json.dumps({
                "stdout": self.program_stdout,
                "stderr": self.program_stderr,
                "exit_code": 0,
            }),
            stderr="",
            exit_code=0,
        )


def write_profiles(path: Path, *, network_hosts: list[str] | None = None) -> Path:
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "profile_id": "ledger-cli",
        "version": 3,
        "title": "Pinned ledger CLI",
        "description": "A generic test profile.",
        "executable": "tools/ledger/bin/ledger",
        "fixed_arguments": ["--format", "json"],
        "allowed_argument_prefixes": [["entries", "list"], ["entries", "import"]],
        "write_argument_prefixes": [["entries", "import"]],
        "allowed_environment": ["LEDGER_TOKEN"],
        "allowed_network_hosts": network_hosts or [],
        "output_formats": ["json"],
        "max_timeout_seconds": 30,
        "package": {
            "name": "ledger-cli",
            "version": "3.0.0",
            "integrity": "sha512-test",
            "source": "https://packages.example.test/ledger-cli.tgz",
        },
    }), encoding="utf-8")
    return path


def context(sandbox: CapturingSandbox, *, network_policy: NetworkPolicy = NetworkPolicy.full) -> ToolContext:
    allowlist = ["ledger.example.test"] if network_policy == NetworkPolicy.allowlist else []
    agent = AgentSpec(
        name="Program test",
        description="Executes a registered program profile.",
        system_prompt="Execute the registered program exactly as configured.",
        tools=["Program"],
        permission_mode=PermissionMode.bypass,
        network_policy=network_policy,
        network_allowlist=allowlist,
    )

    async def emit(_: str, __: dict[str, Any]) -> None:
        return None

    async def no_spawn(_: str, __: str | None) -> str:
        raise AssertionError("Program must not spawn an agent")

    return ToolContext(
        session_id="program-test",
        agent=agent,
        sandbox=sandbox,  # type: ignore[arg-type]
        emit=emit,
        spawn_subagent=no_spawn,
    )


@pytest.mark.asyncio
async def test_program_executes_exact_argv_and_redacts_environment(tmp_path: Path) -> None:
    profiles = write_profiles(tmp_path / "profiles.json")
    tool = build_core_registry(profiles).get("Program")
    sandbox = CapturingSandbox(
        program_stdout='{"echo":"super-secret"}',
        program_stderr="diagnostic super-secret",
    )

    result = await tool.execute({
        "profile_id": "ledger-cli",
        "arguments": ["entries", "list", "--since", "2026-01-01"],
        "environment": {"LEDGER_TOKEN": "super-secret"},
        "output_format": "json",
        "stdin": {"query": "open"},
    }, context(sandbox))

    assert result.is_error is False
    assert sandbox.argv[:4] == [
        "python",
        "-c",
        sandbox.argv[2],
        "/workspace/tools/ledger/bin/ledger",
    ]
    assert sandbox.argv[4:] == [
        "--format",
        "json",
        "entries",
        "list",
        "--since",
        "2026-01-01",
    ]
    assert "super-secret" not in sandbox.argv
    assert json.loads(sandbox.stdin)["environment"] == {"LEDGER_TOKEN": "super-secret"}
    assert "super-secret" not in result.content
    payload = json.loads(result.content)
    assert payload["data"] == {"echo": "***"}
    assert payload["stderr"] == "diagnostic ***"
    assert payload["receipt"]["write"] is False
    assert payload["receipt"]["profile_version"] == 3


@pytest.mark.asyncio
async def test_program_requires_registered_write_idempotency(tmp_path: Path) -> None:
    tool = build_core_registry(write_profiles(tmp_path / "profiles.json")).get("Program")
    sandbox = CapturingSandbox()

    denied = await tool.execute({
        "profile_id": "ledger-cli",
        "arguments": ["entries", "import", "--file", "-"],
        "environment": {"LEDGER_TOKEN": "value"},
        "stdin": [{"id": "entry-1"}],
    }, context(sandbox))
    assert denied.is_error is False
    assert json.loads(denied.content)["error_class"] == "permission_denied"
    assert sandbox.argv == []

    allowed = await tool.execute({
        "profile_id": "ledger-cli",
        "arguments": ["entries", "import", "--file", "-"],
        "environment": {"LEDGER_TOKEN": "value"},
        "stdin": [{"id": "entry-1"}],
        "idempotency_key": "entry-1",
    }, context(sandbox))
    assert allowed.is_error is False
    receipt = json.loads(allowed.content)["receipt"]
    assert receipt["write"] is True
    assert receipt["idempotency_key_digest"]


@pytest.mark.asyncio
async def test_program_rejects_unknown_profile_arguments_environment_and_network(
    tmp_path: Path,
) -> None:
    profiles = write_profiles(
        tmp_path / "profiles.json",
        network_hosts=["ledger.example.test"],
    )
    tool = build_core_registry(profiles).get("Program")
    sandbox = CapturingSandbox()
    base = {
        "profile_id": "ledger-cli",
        "arguments": ["entries", "list"],
        "environment": {"LEDGER_TOKEN": "value"},
    }

    for override in (
        {"profile_id": "missing"},
        {"arguments": ["entries", "delete"]},
        {"environment": {"UNDECLARED": "value"}},
    ):
        result = await tool.execute(base | override, context(sandbox))
        assert result.is_error is False
        assert json.loads(result.content)["error_class"] == "permission_denied"

    network_denied = await tool.execute(
        base,
        context(sandbox, network_policy=NetworkPolicy.none),
    )
    assert network_denied.is_error is False
    assert json.loads(network_denied.content)["error_class"] == "permission_denied"
    assert sandbox.argv == []


@pytest.mark.asyncio
async def test_program_only_raises_retry_signal_for_transient_failures(tmp_path: Path) -> None:
    tool = build_core_registry(write_profiles(tmp_path / "profiles.json")).get("Program")
    base = {
        "profile_id": "ledger-cli",
        "arguments": ["entries", "list"],
        "environment": {"LEDGER_TOKEN": "value"},
    }

    transient_sandbox = CapturingSandbox(
        program_stdout="",
        program_stderr="service temporarily unavailable: HTTP 503",
    )
    transient_sandbox.program_stdout = ""

    async def transient_run(
        argv: list[str],
        *,
        stdin: str | None = None,
        **_: Any,
    ) -> CommandResult:
        transient_sandbox.argv = argv
        transient_sandbox.stdin = stdin or ""
        return CommandResult(
            stdout=json.dumps({
                "stdout": "",
                "stderr": "service temporarily unavailable: HTTP 503",
                "exit_code": 1,
            }),
            stderr="",
            exit_code=0,
        )

    transient_sandbox.run = transient_run  # type: ignore[method-assign]
    transient = await tool.execute(base, context(transient_sandbox))
    assert transient.is_error is True
    assert json.loads(transient.content)["error_class"] == "transient_program_error"

    permission_sandbox = CapturingSandbox()

    async def permission_run(
        argv: list[str],
        *,
        stdin: str | None = None,
        **_: Any,
    ) -> CommandResult:
        permission_sandbox.argv = argv
        permission_sandbox.stdin = stdin or ""
        return CommandResult(
            stdout=json.dumps({
                "stdout": "",
                "stderr": "Authentication required",
                "exit_code": 1,
            }),
            stderr="",
            exit_code=0,
        )

    permission_sandbox.run = permission_run  # type: ignore[method-assign]
    permission = await tool.execute(base, context(permission_sandbox))
    assert permission.is_error is False
    assert json.loads(permission.content)["error_class"] == "permission_denied"


def test_program_profile_is_public_through_tool_api(tmp_path: Path) -> None:
    profiles = write_profiles(tmp_path / "profiles.json")
    settings = Settings(
        api_token="program-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        program_tool_profiles_file=profiles,
        model_egress_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/tools/program-profiles",
            headers={"authorization": "Bearer program-test-token"},
        )
        assert response.status_code == 200
        assert response.json()[0]["profile_id"] == "ledger-cli"
        catalog = client.get(
            "/api/v1/tools",
            headers={"authorization": "Bearer program-test-token"},
        ).json()
        program = next(item for item in catalog if item["name"] == "Program")
        assert program["description"]
        assert program["input_schema"]["properties"]["profile_id"]
