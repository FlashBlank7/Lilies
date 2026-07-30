from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_platform.local_lilies_bridge import (
    BridgeAssignmentPhase,
    LocalLiliesBridge,
    LocalLiliesBridgeStore,
    PairLocalLiliesRequest,
    StartLocalLiliesBuildRequest,
)
from agent_platform.local_lilies_client import LocalLiliesHttpClient
from agent_platform.platform_blackbox_auth import PlatformBlackboxAuthStore
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage
from agent_platform.workflow_models import ApplicationCreateRequest
from agent_platform.workflow_storage import WorkflowStorage


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STANDALONE_ROOT = REPOSITORY_ROOT.parent / "LiliesAgent"
STANDALONE_PYTHON = STANDALONE_ROOT / ".venv" / "bin" / "python"
CONTRACT_DIGEST = "sha256:" + "d" * 64
REQUIRED_SCOPES = [
    "lilies.session:read",
    "lilies.session:write",
    "lilies.permission:resolve",
    "lilies.credential:write",
    "lilies.observability:read",
]


def _free_loopback_port(*, excluded: set[int] | None = None) -> int:
    excluded = excluded or set()
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in excluded:
            return port


def _provider_egress_canary() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.setblocking(False)
    return listener, int(listener.getsockname()[1])


def _isolated_daemon_environment(
    *,
    isolated_home: Path,
    workspace_root: Path,
    provider_canary_port: int,
) -> dict[str, str]:
    environment = {
        "HOME": str(isolated_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LILIES_DEEPSEEK_BASE_URL": f"http://127.0.0.1:{provider_canary_port}",
        "LILIES_MODEL_EGRESS_ENABLED": "false",
        "LILIES_WORKFLOW_STUDIO_ENABLED": "true",
        "LILIES_WORKSPACE_ROOT": str(workspace_root),
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(isolated_home / "tmp"),
    }
    # Deliberately do not inherit provider keys, proxy configuration, PYTHONPATH,
    # or the user's real HOME. The standalone package is resolved only inside
    # its own isolated interpreter process.
    return environment


def _start_standalone_daemon(
    *,
    data_dir: Path,
    workspace_root: Path,
    isolated_home: Path,
    port: int,
    provider_canary_port: int,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            str(STANDALONE_PYTHON),
            "-I",
            "-m",
            "lilies_agent.cli",
            "--data-dir",
            str(data_dir),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=STANDALONE_ROOT,
        env=_isolated_daemon_environment(
            isolated_home=isolated_home,
            workspace_root=workspace_root,
            provider_canary_port=provider_canary_port,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )


def _create_one_time_pairing_code(
    *,
    data_dir: Path,
    workspace_root: Path,
    isolated_home: Path,
    provider_canary_port: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(STANDALONE_PYTHON),
            "-I",
            "-m",
            "lilies_agent.cli",
            "--data-dir",
            str(data_dir),
            "pair",
            "--scope",
            REQUIRED_SCOPES[0],
            "--scope",
            REQUIRED_SCOPES[1],
            "--scope",
            REQUIRED_SCOPES[2],
            "--scope",
            REQUIRED_SCOPES[3],
            "--scope",
            REQUIRED_SCOPES[4],
        ],
        cwd=STANDALONE_ROOT,
        env=_isolated_daemon_environment(
            isolated_home=isolated_home,
            workspace_root=workspace_root,
            provider_canary_port=provider_canary_port,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _standalone_distribution_version() -> str:
    result = subprocess.run(
        [
            str(STANDALONE_PYTHON),
            "-I",
            "-c",
            ("from importlib.metadata import version; print(version('lilies-local-agent'))"),
        ],
        cwd=STANDALONE_ROOT,
        env={
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _stop_standalone_daemon(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


async def _wait_for_health(
    *,
    base_url: str,
    process: subprocess.Popen[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    async with httpx.AsyncClient(
        timeout=0.5,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=2)
                raise AssertionError(f"standalone daemon exited before health\n{stdout}\n{stderr}")
            try:
                response = await client.get(f"{base_url}/local/v1/health")
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
                continue
            if response.status_code == 200:
                payload = response.json()
                assert isinstance(payload, dict)
                return payload
            await asyncio.sleep(0.05)
    raise AssertionError("standalone daemon did not become healthy")


async def _create_platform_parts(
    root: Path,
) -> tuple[WorkflowStorage, PlatformHarness, PlatformBlackboxAuthStore]:
    platform_root = root / "platform"
    storage = Storage(platform_root)
    await storage.initialize()
    workflow = WorkflowStorage(storage)
    await workflow.initialize()
    harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="isolated-process-bridge-envelope-key",
    )
    auth = PlatformBlackboxAuthStore(platform_root / "blackbox-auth.db")
    await auth.initialize()
    return workflow, harness, auth


def _bridge(
    *,
    root: Path,
    workflow: WorkflowStorage,
    harness: PlatformHarness,
    auth: PlatformBlackboxAuthStore,
    platform_port: int,
) -> LocalLiliesBridge:
    return LocalLiliesBridge(
        enabled=True,
        store=LocalLiliesBridgeStore(root / "platform" / "local-lilies-bridge.db"),
        workflow_storage=workflow,
        harness=harness,
        auth_store=auth,
        client=LocalLiliesHttpClient(timeout_seconds=2),
        platform_base_url=f"http://127.0.0.1:{platform_port}",
        contract_digest_provider=lambda _scopes, _applications, _actions: (
            CONTRACT_DIGEST
        ),
    )


async def _empty_application(workflow: WorkflowStorage) -> UUID:
    application = await workflow.create_application(
        ApplicationCreateRequest(
            name="Standalone Lilies process-boundary replay",
            requirement=(
                "Build an auditable enterprise document review workflow with "
                "human escalation for ambiguous cases."
            ),
        )
    )
    return UUID(str(application["id"]))


def _build_request(connection_id: UUID) -> StartLocalLiliesBuildRequest:
    return StartLocalLiliesBuildRequest(
        idempotency_key="standalone-process-build-000001",
        connection_id=connection_id,
        requirement=(
            "Build an enterprise document review workflow with human escalation "
            "and a durable structured audit result."
        ),
        business_context={
            "customer_roles": ["operations reviewer"],
            "business_goal": "Review incoming documents without losing ambiguous cases.",
            "inputs": ["incoming documents"],
            "outputs": ["review decision", "audit record"],
            "constraints": ["ambiguous cases require human review"],
        },
        deliverables=[
            {
                "name": "review workflow",
                "description": "Editable workflow and its auditable decision output.",
                "media_type": "application/json",
                "required": True,
            }
        ],
    )


@pytest.mark.asyncio
async def test_platform_bridge_replays_real_standalone_daemon_sse_without_model_egress(
    tmp_path: Path,
) -> None:
    assert STANDALONE_PYTHON.is_file(), "build the sibling LiliesAgent environment first"
    assert (STANDALONE_ROOT / "src" / "lilies_agent" / "__init__.py").is_file()
    assert "lilies_agent" not in sys.modules
    assert str(STANDALONE_ROOT / "src") not in sys.path

    data_dir = tmp_path / "standalone-state"
    workspace_root = tmp_path / "standalone-workspaces"
    isolated_home = tmp_path / "isolated-home"
    for path in (data_dir, workspace_root, isolated_home, isolated_home / "tmp"):
        path.mkdir(parents=True, mode=0o700)

    provider_canary, provider_canary_port = _provider_egress_canary()
    daemon_port = _free_loopback_port(excluded={provider_canary_port})
    platform_port = _free_loopback_port(excluded={provider_canary_port, daemon_port})
    daemon_base_url = f"http://127.0.0.1:{daemon_port}"
    process = _start_standalone_daemon(
        data_dir=data_dir,
        workspace_root=workspace_root,
        isolated_home=isolated_home,
        port=daemon_port,
        provider_canary_port=provider_canary_port,
    )
    stdout = ""
    stderr = ""
    pairing_code = ""
    try:
        health = await _wait_for_health(base_url=daemon_base_url, process=process)
        assert health == {
            "schema_version": "1.0",
            "service": "lilies",
            "status": "ok",
            "distribution_id": "lilies-agent-standalone",
            "daemon_version": _standalone_distribution_version(),
            "daemon_fingerprint": health["daemon_fingerprint"],
            "model_egress_enabled": False,
        }

        pairing_process = await asyncio.to_thread(
            _create_one_time_pairing_code,
            data_dir=data_dir,
            workspace_root=workspace_root,
            isolated_home=isolated_home,
            provider_canary_port=provider_canary_port,
        )
        assert pairing_process.returncode == 0, pairing_process.stderr
        pairing = json.loads(pairing_process.stdout)
        pairing_code = str(pairing["pairing_code"])
        assert pairing["daemon_fingerprint"] == health["daemon_fingerprint"]
        assert pairing["allowed_scopes"] == sorted(REQUIRED_SCOPES)

        workflow, harness, auth = await _create_platform_parts(tmp_path)
        bridge = _bridge(
            root=tmp_path,
            workflow=workflow,
            harness=harness,
            auth=auth,
            platform_port=platform_port,
        )
        await bridge.initialize()
        connection = await bridge.pair_connection(
            PairLocalLiliesRequest(
                idempotency_key="standalone-process-pair-000001",
                base_url=daemon_base_url,
                pairing_code=pairing_code,
                expected_daemon_fingerprint=str(health["daemon_fingerprint"]),
            )
        )
        assert connection.base_url == daemon_base_url
        assert connection.status.value == "connected"
        assert sorted(scope.value for scope in connection.granted_scopes) == sorted(REQUIRED_SCOPES)

        application_id = await _empty_application(workflow)
        request = _build_request(connection.connection_id)
        assignment = await bridge.start_build(application_id, request)
        idempotent_start = await bridge.start_build(application_id, request)
        assert idempotent_start.assignment_id == assignment.assignment_id
        assert idempotent_start.session_id == assignment.session_id

        relay_deadline = time.monotonic() + 5
        while True:
            first_relay = await bridge.relay_events(assignment.assignment_id)
            first_events = await bridge.list_events(assignment.assignment_id)
            if first_events:
                break
            assert time.monotonic() < relay_deadline, (
                "small real-daemon SSE never produced its first batch; "
                "_bounded_sse_lines must consume arriving bytes without asking "
                "httpx to aggregate a fixed 16 KiB chunk"
            )
            await asyncio.sleep(0.05)
        first_event_types = {event.event_type for event in first_events}
        assert first_relay.inserted >= 1
        assert first_relay.replayed == 0
        assert first_relay.relay_cursor == first_relay.ack_cursor
        assert {"session.created", "assignment.accepted"} <= first_event_types
        assert "usage.model_call" not in first_event_types

        cancelled = await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="standalone-process-cancel-000001",
            reason="finish isolated process-boundary replay proof",
        )
        assert cancelled.phase == BridgeAssignmentPhase.cancelled
        terminal_events = await bridge.list_events(assignment.assignment_id)
        terminal_identity = [
            (event.daemon_seq, event.event_type, event.data) for event in terminal_events
        ]
        assert "assignment.cancelled" in {event.event_type for event in terminal_events}
        assert len({event.daemon_seq for event in terminal_events}) == len(terminal_events)

        rebuilt = _bridge(
            root=tmp_path,
            workflow=workflow,
            harness=harness,
            auth=auth,
            platform_port=platform_port,
        )
        await rebuilt.initialize()
        rebuilt_start = await rebuilt.start_build(application_id, request)
        assert rebuilt_start.assignment_id == assignment.assignment_id
        assert rebuilt_start.session_id == assignment.session_id
        rebuilt_relay = await rebuilt.relay_events(assignment.assignment_id)
        rebuilt_events = await rebuilt.list_events(assignment.assignment_id)
        assert rebuilt_relay.inserted == 0
        assert rebuilt_relay.replayed == 0
        assert rebuilt_relay.relay_cursor == rebuilt_relay.ack_cursor
        assert rebuilt_relay.relay_cursor == cancelled.relay_cursor
        assert [
            (event.daemon_seq, event.event_type, event.data) for event in rebuilt_events
        ] == terminal_identity
        assert (await rebuilt.recover_pending_assignments()).scanned == 0
        session = await rebuilt.get_assignment_session(assignment.assignment_id)
        assert session.usage.model_call_count == 0
        assert session.usage.token_count == 0
        assert session.usage.tool_count == 0
        assert session.usage.cost_usd == 0

        ready, _, _ = select.select([provider_canary], [], [], 0)
        assert ready == [], "standalone daemon attempted provider egress"
        assert "lilies_agent" not in sys.modules
        assert str(STANDALONE_ROOT / "src") not in sys.path
    finally:
        stdout, stderr = await asyncio.to_thread(_stop_standalone_daemon, process)
        provider_canary.close()

    assert process.poll() is not None
    assert process.returncode == 0
    assert not (data_dir / "daemon.json").exists()
    assert pairing_code not in stdout
    assert pairing_code not in stderr
