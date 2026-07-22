from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxAuthStore,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)


CLIENT_SOURCE = r'''
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

base_url = os.environ["LILIES_TEST_BASE_URL"].rstrip("/")
token = os.environ["LILIES_TEST_TOKEN"]
assignment_id = os.environ["LILIES_TEST_ASSIGNMENT_ID"]
session_id = os.environ["LILIES_TEST_SESSION_ID"]
contract_digest = "sha256:" + "0" * 64
counter = 0
history = []
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(operation, method, path, payload=None, params=None, *, expect=(200,)):
    global counter, contract_digest
    counter += 1
    key = f"process-http-{counter:04d}-key"
    tool_call = f"process-tool-{counter:04d}"
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None
    if payload is not None:
        payload = dict(payload)
        payload.setdefault("idempotency_key", key)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Lilies-Assignment-ID": assignment_id,
            "X-Lilies-Session-ID": session_id,
            "X-Lilies-Tool-Call-ID": tool_call,
            "X-Lilies-Idempotency-Key": key,
            "X-Lilies-Contract-Digest": contract_digest,
        },
    )
    try:
        with opener.open(request, timeout=20) as response:
            status = response.status
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        status = error.code
        result = json.loads(error.read())
    required = {
        "ok",
        "operation",
        "request_id",
        "status_code",
        "contract_digest",
        "data",
        "error",
        "evidence_refs",
    }
    if set(result) != required:
        raise AssertionError(f"invalid envelope keys for {operation}: {sorted(result)}")
    if result["operation"] != operation or result["status_code"] != status:
        raise AssertionError(f"invalid envelope correlation for {operation}: {result}")
    if status not in expect:
        raise AssertionError(f"unexpected {status} for {operation}: {result}")
    history.append(
        {
            "operation": operation,
            "status_code": status,
            "ok": result["ok"],
            "request_id": result["request_id"],
            "evidence_refs": result["evidence_refs"],
        }
    )
    return result


contract = call(
    "platform_contract_get",
    "GET",
    "/api/v1/lilies/platform-contract",
)["data"]
contract_digest = contract["contract_digest"]
operation_names = {item["name"] for item in contract["operations"]}
if len(operation_names) != 16:
    raise AssertionError(f"expected 16 operations, got {sorted(operation_names)}")

blocks = call(
    "platform_block_search",
    "GET",
    "/api/v1/lilies/blocks",
    params={"query": "template"},
)["data"]
if not any(item["type"] == "template_transform" for item in blocks):
    raise AssertionError("template_transform missing from public block search")
call(
    "platform_block_get",
    "GET",
    "/api/v1/lilies/blocks/template_transform",
)
call("platform_tool_catalog", "GET", "/api/v1/lilies/tools")

application = call(
    "platform_application_create",
    "POST",
    "/api/v1/lilies/applications",
    {
        "name": "HTTP-only minimal repair",
        "requirement": "Create, test, repair, run, and publish a greeting workflow.",
    },
    expect=(201,),
)["data"]
application_id = application["id"]
call(
    "platform_application_get",
    "GET",
    f"/api/v1/lilies/applications/{application_id}",
)

revision = 0
operations = [
    (
        "add_node",
        {
            "node": {
                "id": "start",
                "type": "start",
                "title": "Input",
                "config": {"inputs": [{"name": "name", "type": "string"}]},
            }
        },
    ),
    (
        "add_node",
        {
            "node": {
                "id": "template",
                "type": "template_transform",
                "title": "Greeting",
                "config": {
                    "template": "Hi {{ name }}",
                    "variables": {
                        "name": {"$ref": {"node_id": "start", "path": ["name"]}}
                    },
                },
            }
        },
    ),
    (
        "add_node",
        {
            "node": {
                "id": "end",
                "type": "end",
                "title": "End",
                "config": {
                    "outputs": {
                        "greeting": {
                            "$ref": {"node_id": "template", "path": ["text"]}
                        }
                    }
                },
            }
        },
    ),
    (
        "add_edge",
        {
            "edge": {
                "id": "start-template",
                "source": "start",
                "target": "template",
                "source_port": "output",
                "target_port": "input",
            }
        },
    ),
    (
        "add_edge",
        {
            "edge": {
                "id": "template-end",
                "source": "template",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            }
        },
    ),
    (
        "add_test",
        {
            "test": {
                "id": "greeting-case",
                "name": "Greeting acceptance",
                "requirement": "Return the exact greeting.",
                "inputs": {"name": "Ada"},
                "assertions": [
                    {"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}
                ],
                "mandatory": True,
            }
        },
    ),
]
for op, data in operations:
    applied = call(
        "platform_draft_apply",
        "POST",
        f"/api/v1/lilies/applications/{application_id}/draft",
        {"expected_revision": revision, "op": op, "data": data},
    )
    revision = applied["data"]["revision"]

draft = call(
    "platform_draft_inspect",
    "GET",
    f"/api/v1/lilies/applications/{application_id}/draft",
)["data"]
if draft["revision"] != revision:
    raise AssertionError("draft revision did not match incremental writes")
if draft["snapshot"]["workflow"]["nodes"][1]["config"]["variables"]["name"]["$ref"]["path"] != ["name"]:
    raise AssertionError("public draft projection destroyed a workflow reference path")

failed = call(
    "platform_tests_run",
    "POST",
    f"/api/v1/lilies/applications/{application_id}/tests/run",
    {},
)["data"]
if failed["passed"] is not False:
    raise AssertionError("the deliberate first acceptance failure did not fail")
failed_run_id = failed["tests"][0]["run_id"]
call(
    "platform_trace_get",
    "GET",
    f"/api/v1/lilies/runs/{failed_run_id}/trace",
)

repaired = call(
    "platform_draft_apply",
    "POST",
    f"/api/v1/lilies/applications/{application_id}/draft",
    {
        "expected_revision": revision,
        "op": "update_node",
        "data": {
            "node_id": "template",
            "changes": {"config": {"template": "Hello {{ name }}"}},
        },
    },
)
revision = repaired["data"]["revision"]
passed = call(
    "platform_tests_run",
    "POST",
    f"/api/v1/lilies/applications/{application_id}/tests/run",
    {},
)["data"]
if passed["passed"] is not True:
    raise AssertionError(f"acceptance did not pass after one repair: {passed}")

published = call(
    "platform_publish",
    "POST",
    f"/api/v1/lilies/applications/{application_id}/versions",
    {"acknowledge_warnings": True},
)["data"]
if published["version"] != 1:
    raise AssertionError(f"unexpected published version: {published}")

# Publication adds an assignment-owned workflow tool and therefore changes the contract.
contract_digest = call(
    "platform_contract_get",
    "GET",
    "/api/v1/lilies/platform-contract",
)["data"]["contract_digest"]
started = call(
    "platform_run_start",
    "POST",
    f"/api/v1/lilies/applications/{application_id}/runs",
    {"inputs": {"name": "Ada"}},
    expect=(202,),
)["data"]
run_id = started["run_id"]
terminal = None
for _ in range(100):
    current = call(
        "platform_run_get",
        "GET",
        f"/api/v1/lilies/runs/{run_id}",
    )["data"]
    if current["status"] in {"succeeded", "failed", "cancelled"}:
        terminal = current
        break
    time.sleep(0.02)
if terminal is None or terminal["status"] != "succeeded":
    raise AssertionError(f"workflow run did not succeed: {terminal}")
if terminal["outputs"] != {"greeting": "Hello Ada"}:
    raise AssertionError(f"unexpected workflow outputs: {terminal['outputs']}")
call("platform_trace_get", "GET", f"/api/v1/lilies/runs/{run_id}/trace")

internal_request = urllib.request.Request(
    base_url + "/api/v1/applications",
    headers={"Authorization": "Bearer " + token},
)
try:
    opener.open(internal_request, timeout=10)
    raise AssertionError("task token unexpectedly reached an internal endpoint")
except urllib.error.HTTPError as error:
    internal_status = error.code
    internal_body = json.loads(error.read())
if internal_status != 403 or internal_body["error"]["code"] != "internal_endpoint_denied":
    raise AssertionError(f"internal endpoint denial mismatch: {internal_status} {internal_body}")

print(
    json.dumps(
        {
            "client_pid": os.getpid(),
            "cwd_entries": sorted(os.listdir(".")),
            "application_id": application_id,
            "failed_run_id": failed_run_id,
            "run_id": run_id,
            "draft_revision": revision,
            "failed_before_repair": True,
            "passed_after_repair": True,
            "published_version": published["version"],
            "run_status": terminal["status"],
            "outputs": terminal["outputs"],
            "internal_status": internal_status,
            "operations": history,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[str]) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(f"platform process exited early\n{stdout}\n{stderr}")
        try:
            with opener.open(url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("platform process did not become healthy")


def test_separate_process_http_only_minimal_build_failure_repair_and_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "platform-data"
    workspace_root = tmp_path / "platform-workspaces"
    client_workspace = tmp_path / "isolated-lilies-client"
    server_workspace = tmp_path / "platform-process"
    for path in (data_dir, workspace_root, client_workspace, server_workspace):
        path.mkdir(parents=True)

    assignment_id = uuid4()
    session_id = uuid4()
    auth_store = PlatformBlackboxAuthStore(data_dir / "agent_platform.db")

    async def issue() -> tuple[str, str]:
        await auth_store.initialize()
        issued = await auth_store.issue_credential(
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=list(PlatformBlackboxScope),
                application_ids=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        return issued.access_token.get_secret_value(), issued.credential.credential_ref

    token, credential_ref = asyncio.run(issue())
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_environment = os.environ.copy()
    server_environment.update(
        {
            "DATA_DIR": str(data_dir),
            "WORKSPACE_ROOT": str(workspace_root),
            "API_TOKEN": "process-internal-token",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "SCHEDULER_POLL_SECONDS": "3600",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    server_environment.pop("DEEPSEEK_API_KEY", None)
    server_module = server_workspace / "server_app.py"
    server_module.write_text(
        "import agent_platform.api as platform_api\n"
        "platform_api.app.state.services.builder = None\n"
        "app = platform_api.create_app()\n"
        "app.state.services.builder = None\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=server_workspace,
        env=server_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_health(base_url, process)
        client_script = client_workspace / "http_only_client.py"
        client_script.write_text(CLIENT_SOURCE, encoding="utf-8")
        client_environment = os.environ.copy()
        client_environment.pop("PYTHONPATH", None)
        client_environment.update(
            {
                "LILIES_TEST_BASE_URL": base_url,
                "LILIES_TEST_TOKEN": token,
                "LILIES_TEST_ASSIGNMENT_ID": str(assignment_id),
                "LILIES_TEST_SESSION_ID": str(session_id),
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        child = subprocess.run(
            [sys.executable, "-I", str(client_script)],
            cwd=client_workspace,
            env=client_environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout)
        assert result["client_pid"] != process.pid
        assert result["failed_before_repair"] is True
        assert result["passed_after_repair"] is True
        assert result["run_status"] == "succeeded"
        assert result["outputs"] == {"greeting": "Hello Ada"}
        assert result["internal_status"] == 403
        assert result["cwd_entries"] == ["http_only_client.py"]
        assert "agent_platform" not in CLIENT_SOURCE
        assert "sqlite" not in CLIENT_SOURCE.casefold()
        assert "WorkflowStore" not in CLIENT_SOURCE
        assert token not in child.stdout
        assert token not in child.stderr

        with sqlite3.connect(data_dir / "agent_platform.db") as connection:
            connection.row_factory = sqlite3.Row
            assert connection.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0
            requests = connection.execute(
                "SELECT assignment_id,session_id,tool_call_id,idempotency_key,operation "
                "FROM platform_blackbox_requests ORDER BY created_at"
            ).fetchall()
            audit_dump = "\n".join(connection.iterdump())
        assert requests
        assert all(row["assignment_id"] == str(assignment_id) for row in requests)
        assert all(row["session_id"] == str(session_id) for row in requests)
        assert all(row["tool_call_id"].startswith("process-tool-") for row in requests)
        assert all(row["idempotency_key"].startswith("process-http-") for row in requests)
        assert sum(row["operation"] == "platform_draft_apply" for row in requests) == 7
        assert token not in audit_dump
        assert credential_ref in audit_dump
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert token not in stdout
        assert token not in stderr


def test_separate_local_lilies_agent_loop_uses_assignment_scoped_http_tools(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "platform-data"
    workspace_root = tmp_path / "platform-workspaces"
    client_workspace = tmp_path / "isolated-lilies-client"
    local_data = tmp_path / "local-lilies-data"
    local_workspace = tmp_path / "local-lilies-workspaces"
    server_workspace = tmp_path / "platform-process"
    for path in (
        data_dir,
        workspace_root,
        client_workspace,
        local_data,
        local_workspace,
        server_workspace,
    ):
        path.mkdir(parents=True)

    assignment_id = uuid4()
    session_id = uuid4()
    auth_store = PlatformBlackboxAuthStore(data_dir / "agent_platform.db")

    async def issue() -> tuple[str, str]:
        await auth_store.initialize()
        issued = await auth_store.issue_credential(
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=list(PlatformBlackboxScope),
                application_ids=[],
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        return issued.access_token.get_secret_value(), issued.credential.credential_ref

    token, credential_ref = asyncio.run(issue())
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_environment = os.environ.copy()
    server_environment.update(
        {
            "DATA_DIR": str(data_dir),
            "WORKSPACE_ROOT": str(workspace_root),
            "API_TOKEN": "agent-loop-internal-token",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "SCHEDULER_POLL_SECONDS": "3600",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    server_environment.pop("DEEPSEEK_API_KEY", None)
    server_module = server_workspace / "server_app.py"
    server_module.write_text(
        "import agent_platform.api as platform_api\n"
        "platform_api.app.state.services.builder = None\n"
        "app = platform_api.create_app()\n"
        "app.state.services.builder = None\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=server_workspace,
        env=server_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_health(base_url, process)
        source_path = (
            Path(__file__).parent / "fixtures" / "v04_13_t01b_agent_client.py"
        )
        source = source_path.read_text(encoding="utf-8")
        for forbidden_import in (
            "agent_platform.api",
            "agent_platform.applications",
            "agent_platform.builder",
            "agent_platform.storage",
            "agent_platform.workflow_storage",
        ):
            assert forbidden_import not in source
        client_script = client_workspace / "agent_client.py"
        client_script.write_text(source, encoding="utf-8")
        client_environment = os.environ.copy()
        client_environment.pop("PYTHONPATH", None)
        client_environment.update(
            {
                "LILIES_TEST_BASE_URL": base_url,
                "LILIES_TEST_TOKEN": token,
                "LILIES_TEST_ASSIGNMENT_ID": str(assignment_id),
                "LILIES_TEST_SESSION_ID": str(session_id),
                "LILIES_TEST_LOCAL_DATA": str(local_data),
                "LILIES_TEST_LOCAL_WORKSPACE": str(local_workspace),
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        child = subprocess.run(
            [sys.executable, "-I", str(client_script)],
            cwd=client_workspace,
            env=client_environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout)
        assert result["client_pid"] != process.pid
        assert result["turn_status"] == "completed"
        assert result["session_status"] == "ready"
        assert result["draft_revision"] == 7
        assert result["run_status"] == "succeeded"
        assert result["outputs"] == {"greeting": "Hello Ada"}
        assert result["contract_changed_after_publish"] is True
        assert result["platform_tool_count"] == 16
        assert result["model_calls"] == len(result["platform_operations"]) + 1
        assert result["platform_tool_events"] == len(result["platform_operations"]) * 2
        assert result["platform_operations"][0] == "platform_contract_get"
        assert result["platform_operations"].count("platform_contract_get") == 2
        assert result["platform_operations"].count("platform_draft_apply") == 7
        assert token not in child.stdout
        assert token not in child.stderr
        assert sorted(path.name for path in client_workspace.iterdir()) == ["agent_client.py"]

        with sqlite3.connect(data_dir / "agent_platform.db") as connection:
            connection.row_factory = sqlite3.Row
            assert connection.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0
            requests = connection.execute(
                "SELECT assignment_id,session_id,tool_call_id,idempotency_key,operation "
                "FROM platform_blackbox_requests WHERE assignment_id=? ORDER BY created_at",
                (str(assignment_id),),
            ).fetchall()
            audit_dump = "\n".join(connection.iterdump())
        assert requests
        assert all(row["assignment_id"] == str(assignment_id) for row in requests)
        assert all(row["session_id"] == str(session_id) for row in requests)
        assert all(
            row["tool_call_id"] == "agent-bootstrap-contract"
            or row["tool_call_id"].startswith("agent-tool-")
            for row in requests
        )
        assert sum(row["operation"] == "platform_draft_apply" for row in requests) == 7
        assert token not in audit_dump
        assert credential_ref in audit_dump
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert token not in stdout
        assert token not in stderr
