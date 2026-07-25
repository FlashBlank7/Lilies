from __future__ import annotations

import asyncio
import ast
import inspect
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import agent_platform.collaborative_development_storage as storage_module
import agent_platform.collaborative_development_worker as worker_module
from agent_platform.collaborative_development_cli import main
from agent_platform.collaborative_development_dispatcher import (
    CollaborativeDevelopmentDispatchJournal,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RoleBoundDispatchContext,
)
from agent_platform.collaborative_development_models import (
    AgentRole,
    AgentRoleGrant,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    ExecutionMode,
    SideEffect,
    WorkItemKind,
    WorkspaceGrant,
    utc_now,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentStore,
)
from agent_platform.collaborative_development_worker import (
    ExternalJsonArgvDispatchHandler,
    run_dispatch_worker,
)


BASELINE = "a" * 40


def test_worker_module_has_no_workflow_builder_or_application_dependency() -> None:
    tree = ast.parse(inspect.getsource(worker_module))
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in ("workflow", "builder", "application")
    )


def _assignment(tmp_path: Path) -> DevelopmentAssignment:
    created = utc_now()
    (tmp_path / "lilies").mkdir(exist_ok=True)
    (tmp_path / "codex").mkdir(exist_ok=True)
    common = {
        "baseline_commit": BASELINE,
        "allowed_paths": ("src", "tests"),
        "allowed_argv": ((sys.executable, "-m", "pytest", "-q"),),
        "allowed_side_effects": (
            SideEffect.workspace_write,
            SideEffect.process_execute,
        ),
        "created_at": created,
    }
    return DevelopmentAssignment(
        assignment_id=uuid4(),
        goal="Repair a parser and independently review the evidence.",
        software_id="worker-fixture",
        baseline_commit=BASELINE,
        agent_roles=(
            AgentRoleGrant(
                agent_role=AgentRole.lilies,
                task_roles=(
                    DevelopmentTaskRole.implementer,
                    DevelopmentTaskRole.reviewer,
                ),
            ),
            AgentRoleGrant(
                agent_role=AgentRole.codex,
                task_roles=(DevelopmentTaskRole.implementer,),
            ),
        ),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.lilies,
                workspace_root=str(tmp_path / "lilies"),
                **common,
            ),
            WorkspaceGrant(
                workspace_id=uuid4(),
                agent_role=AgentRole.codex,
                workspace_root=str(tmp_path / "codex"),
                **common,
            ),
        ),
        budget=DevelopmentBudget(
            max_work_items=10,
            max_commands=20,
            max_tool_calls=100,
            max_wall_seconds=3_600,
            max_cost_usd=5,
        ),
        deadline=created + timedelta(hours=1),
        execution_mode=ExecutionMode.autonomous,
        created_at=created,
        updated_at=created,
    )


def _work_item(
    assignment: DevelopmentAssignment,
    *,
    objective: str = "Fix parser behavior.",
) -> DevelopmentWorkItem:
    created = utc_now()
    return DevelopmentWorkItem(
        work_item_id=uuid4(),
        assignment_id=assignment.assignment_id,
        kind=WorkItemKind.bug,
        objective=objective,
        acceptance=("Focused test passes.",),
        assigned_role=AgentRole.codex,
        created_at=created,
        updated_at=created,
    )


def _with_role_argv(
    assignment: DevelopmentAssignment,
    *,
    role: AgentRole,
    argv: tuple[str, ...],
) -> DevelopmentAssignment:
    return assignment.model_copy(
        update={
            "workspace_grants": tuple(
                grant.model_copy(update={"allowed_argv": (*grant.allowed_argv, argv)})
                if grant.agent_role == role
                else grant
                for grant in assignment.workspace_grants
            )
        }
    )


async def _seed(
    database_path: Path,
    assignment: DevelopmentAssignment,
    item: DevelopmentWorkItem,
    *,
    suffix: str,
) -> CollaborativeDevelopmentStore:
    store = CollaborativeDevelopmentStore(database_path)
    await store.initialize()
    if suffix == "0001":
        await store.create_assignment(
            assignment,
            actor_id="user",
            idempotency_key=f"worker-assignment-{suffix}",
        )
    await store.create_work_item(
        item,
        actor_role="lilies",
        actor_id="lilies-agent",
        idempotency_key=f"worker-item-{suffix}",
    )
    return store


def test_worker_cli_once_invokes_bound_external_argv_adapter(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "collaborative-development.db"
    assignment = _assignment(tmp_path)
    assignment = assignment.model_copy(
        update={
            "workspace_grants": tuple(
                grant.model_copy(
                    update={"secret_refs": (f"{grant.agent_role.value}-private-secret",)}
                )
                for grant in assignment.workspace_grants
            )
        }
    )
    adapter_path = tmp_path / "adapter.py"
    adapter_path.write_text(
        "\n".join(
            (
                "import json",
                "import sys",
                "request = json.load(sys.stdin)",
                (
                    "assert request['outbox_idempotency_key'] == "
                    "request['outbox']['idempotency_key']"
                ),
                "assert request['grant_digest'].startswith('sha256:')",
                "assert 'workspace_grants' not in request['assignment']",
                "assert 'agent_roles' not in request['assignment']",
                "assert 'grant' not in request",
                "assert 'allowed_hosts' not in request['role']",
                "assert 'secret_refs' not in request['role']",
                "assert request['role']['agent_role'] == 'codex'",
                "assert 'lilies-private-secret' not in json.dumps(request)",
                "assert 'codex-private-secret' not in json.dumps(request)",
                "response = {",
                "    'schema_version': '1.0',",
                "    'outbox_id': request['outbox_id'],",
                ("    'outbox_idempotency_key': request['outbox_idempotency_key'],"),
                "    'grant_digest': request['grant_digest'],",
                "    'outcome': {",
                "        'status': 'delivered',",
                "        'evidence_refs': ['sha256:' + 'b' * 64],",
                "        'detail': 'bounded adapter accepted the work item',",
                "    },",
                "}",
                "json.dump(response, sys.stdout, sort_keys=True)",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
    assignment = _with_role_argv(
        assignment,
        role=AgentRole.codex,
        argv=adapter_argv,
    )
    grant = next(
        grant for grant in assignment.workspace_grants if grant.agent_role == AgentRole.codex
    )
    item = _work_item(assignment)
    store = asyncio.run(_seed(database_path, assignment, item, suffix="0001"))
    outbox = asyncio.run(store.list_pending_outbox())[0]
    stored_item = asyncio.run(store.get_work_item(item.work_item_id))
    handler = ExternalJsonArgvDispatchHandler(adapter_argv)
    response = asyncio.run(
        handler.invoke_autonomous(
            context=RoleBoundDispatchContext.from_assignment(
                outbox=outbox,
                assignment=assignment,
                work_item=stored_item,
                workspace_grant=grant,
            ),
            usage_meter=store,
        )
    )
    outcome = response.outcome
    assert outcome.status == DispatchOutcomeStatus.delivered
    assert outcome.evidence_refs == (f"sha256:{'b' * 64}",)
    usage = asyncio.run(store.list_development_tool_usage(assignment.assignment_id))
    assert len(usage) == 1
    assert usage[0].command_argv == adapter_argv
    assert usage[0].command_cwd == "."
    assert usage[0].status == "completed"


def test_external_adapter_rejects_executable_and_entrypoint_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_MACOS_SANDBOX", Path("/usr/bin/true"))
    assignment = _assignment(tmp_path)
    grant = next(
        item
        for item in assignment.workspace_grants
        if item.agent_role == AgentRole.codex
    )
    input_path = tmp_path / "request.json"
    input_path.write_text("{}\n", encoding="utf-8")
    executable_target = Path(sys.executable).resolve()
    executable_link = tmp_path / "python-link"
    executable_link.symlink_to(executable_target)
    with pytest.raises(ValueError, match="symlink"):
        ExternalJsonArgvDispatchHandler._sandbox_command(
            argv=(str(executable_link),),
            grant=grant,
            input_path=input_path,
        )

    entrypoint_target = tmp_path / "adapter-target.py"
    entrypoint_target.write_text("raise SystemExit(0)\n", encoding="utf-8")
    entrypoint_link = tmp_path / "adapter-link.py"
    entrypoint_link.symlink_to(entrypoint_target)
    with pytest.raises(ValueError, match="symlink"):
        ExternalJsonArgvDispatchHandler._sandbox_command(
            argv=(str(executable_target), str(entrypoint_link)),
            grant=grant,
            input_path=input_path,
        )


def test_external_adapter_rejects_bound_file_replacement_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_MACOS_SANDBOX", Path("/usr/bin/true"))
    assignment = _assignment(tmp_path)
    grant = next(
        item
        for item in assignment.workspace_grants
        if item.agent_role == AgentRole.codex
    )
    input_path = tmp_path / "request.json"
    input_path.write_text("{}\n", encoding="utf-8")
    adapter_path = tmp_path / "bound-adapter.py"
    adapter_path.write_text("raise SystemExit(0)\n", encoding="utf-8")
    executable = str(Path(sys.executable).resolve())
    _, cwd, bindings = ExternalJsonArgvDispatchHandler._sandbox_command(
        argv=(executable, str(adapter_path)),
        grant=grant,
        input_path=input_path,
    )

    adapter_path.unlink()
    adapter_path.write_text("raise SystemExit(9)\n", encoding="utf-8")
    invoked_marker = tmp_path / "replacement-was-invoked"
    marker_command = (
        executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(invoked_marker)!r}).touch()"
        ),
    )
    with input_path.open("r+b") as request_stream:
        with pytest.raises(ValueError, match="changed"):
            ExternalJsonArgvDispatchHandler._run_bounded(
                command=marker_command,
                cwd=cwd,
                request_stream=request_stream,
                timeout_seconds=10,
                file_bindings=bindings,
            )
    assert not invoked_marker.exists()


def test_external_adapter_executable_only_authority_pauses_without_forking(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        database_path = data_dir / "collaborative-development.db"
        journal_path = data_dir / "dispatch.db"
        assignment = _assignment(tmp_path)
        item = _work_item(assignment)
        store = await _seed(database_path, assignment, item, suffix="0001")
        invoked_marker = tmp_path / "unauthorized-adapter-ran.txt"
        adapter_path = tmp_path / "unauthorized-adapter.py"
        adapter_path.write_text(
            f"from pathlib import Path\nPath({str(invoked_marker)!r}).touch()\n",
            encoding="utf-8",
        )
        adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
        handler = ExternalJsonArgvDispatchHandler(adapter_argv)

        async def metered_adapter(*, context):
            return (
                await handler.invoke_autonomous(
                    context=context,
                    usage_meter=store,
                )
            ).outcome

        batch = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers={AgentRole.codex: metered_adapter},
            once=True,
            poll_interval_seconds=0.05,
            limit=10,
            claim_ttl_seconds=10,
            dispatcher_id="unauthorized-exact-argv-worker",
        )

        assert len(batch.records) == 1
        assert batch.records[0].status == DispatchOutcomeStatus.authorization_required
        assert not invoked_marker.exists()
        assert await store.list_development_tool_usage(assignment.assignment_id) == []
        assert await store.list_pending_outbox() == []
        journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        journal.initialize()
        requests = journal.authorization_requests(assignment.assignment_id)
        assert len(requests) == 1
        assert requests[0].status == "pending"
        assert requests[0].requested_authority.argv == (adapter_argv,)
        assert requests[0].requested_authority.side_effects == ()

    asyncio.run(scenario())


def test_external_adapter_budget_is_reserved_before_process_start(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "data" / "collaborative-development.db"
        adapter_path = tmp_path / "budget-exhausted-adapter.py"
        adapter_path.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "request = json.load(__import__('sys').stdin)\n"
            "root = Path(request['role']['workspace_root'])\n"
            "(root / 'tests' / 'budget-exhausted-adapter-ran.txt').touch()\n",
            encoding="utf-8",
        )
        adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
        candidate = _with_role_argv(
            _assignment(tmp_path),
            role=AgentRole.codex,
            argv=adapter_argv,
        )
        assignment = candidate.model_copy(
            update={
                "budget": candidate.budget.model_copy(
                    update={"max_commands": 1, "max_tool_calls": 1}
                )
            }
        )
        item = _work_item(assignment)
        store = await _seed(database_path, assignment, item, suffix="0001")
        assert await store.reserve_development_tool_usage(
            assignment_id=assignment.assignment_id,
            actor_role=AgentRole.codex,
            usage_id="preexisting-command-0001",
            tool_name="process_run",
            request_digest=f"sha256:{'d' * 64}",
            command_argv=(sys.executable, "-m", "pytest", "-q"),
            command_cwd=".",
        )
        outbox = (await store.list_pending_outbox())[0]
        stored_item = await store.get_work_item(item.work_item_id)
        grant = next(
            grant for grant in assignment.workspace_grants if grant.agent_role == AgentRole.codex
        )
        (Path(grant.workspace_root) / "tests").mkdir()

        response = await ExternalJsonArgvDispatchHandler(adapter_argv).invoke_autonomous(
            context=RoleBoundDispatchContext.from_assignment(
                outbox=outbox,
                assignment=assignment,
                work_item=stored_item,
                workspace_grant=grant,
            ),
            usage_meter=store,
        )

        assert response.outcome.status == DispatchOutcomeStatus.authorization_required
        assert response.outcome.requested_authority is not None
        assert response.outcome.requested_authority.budget is not None
        assert response.outcome.requested_authority.budget.max_tool_calls == 2
        assert response.outcome.requested_authority.budget.max_commands == 2
        invoked_marker = Path(grant.workspace_root) / "tests" / "budget-exhausted-adapter-ran.txt"
        assert not invoked_marker.exists()
        usage = await store.list_development_tool_usage(assignment.assignment_id)
        assert len(usage) == 1
        assert usage[0].usage_id == "preexisting-command-0001"

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec is unavailable",
)
def test_external_adapter_same_outbox_cannot_fork_twice(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "data" / "collaborative-development.db"
        adapter_path = tmp_path / "one-shot-adapter.py"
        adapter_path.write_text(
            "\n".join(
                (
                    "import json",
                    "import pathlib",
                    "import sys",
                    "request = json.load(sys.stdin)",
                    "root = pathlib.Path(request['role']['workspace_root'])",
                    "counter = root / 'tests' / 'adapter-count.txt'",
                    ("value = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0"),
                    "counter.write_text(str(value + 1), encoding='utf-8')",
                    "response = {",
                    "    'schema_version': '1.0',",
                    "    'outbox_id': request['outbox_id'],",
                    ("    'outbox_idempotency_key': request['outbox_idempotency_key'],"),
                    "    'grant_digest': request['grant_digest'],",
                    "    'outcome': {",
                    "        'status': 'delivered',",
                    "        'detail': 'adapter ran once',",
                    "    },",
                    "}",
                    "json.dump(response, sys.stdout, sort_keys=True)",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
        assignment = _with_role_argv(
            _assignment(tmp_path),
            role=AgentRole.codex,
            argv=adapter_argv,
        )
        item = _work_item(assignment)
        store = await _seed(database_path, assignment, item, suffix="0001")
        outbox = (await store.list_pending_outbox())[0]
        stored_item = await store.get_work_item(item.work_item_id)
        grant = next(
            grant for grant in assignment.workspace_grants if grant.agent_role == AgentRole.codex
        )
        counter = Path(grant.workspace_root) / "tests" / "adapter-count.txt"
        counter.parent.mkdir()
        counter.write_text("0", encoding="utf-8")
        context = RoleBoundDispatchContext.from_assignment(
            outbox=outbox,
            assignment=assignment,
            work_item=stored_item,
            workspace_grant=grant,
        )
        handler = ExternalJsonArgvDispatchHandler(adapter_argv)

        first = await handler.invoke_autonomous(
            context=context,
            usage_meter=store,
        )
        second = await handler.invoke_autonomous(
            context=context,
            usage_meter=store,
        )

        assert first.outcome.status == DispatchOutcomeStatus.delivered, first.outcome.detail
        assert second.outcome.status == DispatchOutcomeStatus.reconciliation_required
        assert counter.read_text(encoding="utf-8") == "1"
        usage = await store.list_development_tool_usage(assignment.assignment_id)
        assert len(usage) == 1
        assert usage[0].status == "completed"
        assert usage[0].command_argv == adapter_argv
        assert usage[0].command_cwd == "."

    asyncio.run(scenario())


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec is unavailable",
)
def test_worker_adapter_os_boundary_denies_outside_read_write_network_and_child(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "collaborative-development.db"
    assignment = _assignment(tmp_path)

    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("must not be readable", encoding="utf-8")
    outside_write = tmp_path / "outside-write.txt"
    child_write = tmp_path / "child-write.txt"
    adapter_path = tmp_path / "malicious-adapter.py"
    adapter_path.write_text(
        "\n".join(
            (
                "import json",
                "import pathlib",
                "import socket",
                "import subprocess",
                "import sys",
                "request = json.load(sys.stdin)",
                "unexpected = []",
                "try:",
                f"    pathlib.Path({str(outside_secret)!r}).read_text()",
                "except Exception:",
                "    pass",
                "else:",
                "    unexpected.append('outside_read')",
                "try:",
                f"    pathlib.Path({str(outside_write)!r}).write_text('escaped')",
                "except Exception:",
                "    pass",
                "else:",
                "    unexpected.append('outside_write')",
                "try:",
                "    socket.create_connection(('127.0.0.1', 9), timeout=0.1)",
                "except Exception:",
                "    pass",
                "else:",
                "    unexpected.append('network')",
                "try:",
                (f"    subprocess.run(['/usr/bin/touch', {str(child_write)!r}], check=True)"),
                "except Exception:",
                "    pass",
                "else:",
                "    unexpected.append('child')",
                "if unexpected:",
                "    raise SystemExit(9)",
                "response = {",
                "    'schema_version': '1.0',",
                "    'outbox_id': request['outbox_id'],",
                ("    'outbox_idempotency_key': request['outbox_idempotency_key'],"),
                "    'grant_digest': request['grant_digest'],",
                "    'outcome': {",
                "        'status': 'delivered',",
                "        'detail': 'all malicious boundary probes were denied',",
                "    },",
                "}",
                "json.dump(response, sys.stdout, sort_keys=True)",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
    assignment = _with_role_argv(
        assignment,
        role=AgentRole.codex,
        argv=adapter_argv,
    )
    item = _work_item(assignment)
    store = asyncio.run(_seed(database_path, assignment, item, suffix="0001"))
    outbox = asyncio.run(store.list_pending_outbox())[0]
    stored_item = asyncio.run(store.get_work_item(item.work_item_id))
    grant = next(
        grant for grant in assignment.workspace_grants if grant.agent_role == AgentRole.codex
    )
    handler = ExternalJsonArgvDispatchHandler(adapter_argv)
    outcome = asyncio.run(
        handler.invoke_autonomous(
            context=RoleBoundDispatchContext.from_assignment(
                outbox=outbox,
                assignment=assignment,
                work_item=stored_item,
                workspace_grant=grant,
            ),
            usage_meter=store,
        )
    ).outcome
    assert outcome.status == DispatchOutcomeStatus.delivered
    assert not outside_write.exists()
    assert not child_write.exists()


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="macOS sandbox-exec is unavailable",
)
def test_worker_stream_limit_kills_oversized_adapter_output(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LILIES_AUTONOMOUS_COLLABORATION_ENABLED", "1")
    data_dir = tmp_path / "data"
    database_path = data_dir / "collaborative-development.db"
    assignment = _assignment(tmp_path)

    adapter_path = tmp_path / "oversized-adapter.py"
    adapter_path.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write(b'x' * ({2 * 1024 * 1024} + 65536))\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
    assignment = _with_role_argv(
        assignment,
        role=AgentRole.codex,
        argv=adapter_argv,
    )
    item = _work_item(assignment)
    asyncio.run(_seed(database_path, assignment, item, suffix="0001"))
    argv_path = tmp_path / "oversized-argv.json"
    argv_path.write_text(
        json.dumps(adapter_argv),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "worker",
                "--data-dir",
                str(data_dir),
                "--once",
                "--codex-handler-argv-file",
                str(argv_path),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    record = emitted["records"][0]
    assert record["status"] == "retry"
    assert record["detail"] == ("configured role adapter output exceeded the safety limit")


def test_worker_fails_closed_before_invoking_adapter_without_os_sandbox(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LILIES_AUTONOMOUS_COLLABORATION_ENABLED", "1")
    data_dir = tmp_path / "data"
    database_path = data_dir / "collaborative-development.db"
    assignment = _assignment(tmp_path)

    invoked_marker = tmp_path / "adapter-was-invoked"
    adapter_path = tmp_path / "must-not-run.py"
    adapter_path.write_text(
        f"from pathlib import Path\nPath({str(invoked_marker)!r}).touch()\n",
        encoding="utf-8",
    )
    adapter_argv = (str(Path(sys.executable).resolve()), str(adapter_path))
    assignment = _with_role_argv(
        assignment,
        role=AgentRole.codex,
        argv=adapter_argv,
    )
    item = _work_item(assignment)
    asyncio.run(_seed(database_path, assignment, item, suffix="0001"))
    argv_path = tmp_path / "must-not-run-argv.json"
    argv_path.write_text(
        json.dumps(adapter_argv),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker_module,
        "_MACOS_SANDBOX",
        tmp_path / "missing-sandbox-exec",
    )

    assert (
        main(
            [
                "worker",
                "--data-dir",
                str(data_dir),
                "--once",
                "--codex-handler-argv-file",
                str(argv_path),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["records"][0]["status"] == "retry"
    assert emitted["records"][0]["detail"] == (
        "configured role adapter was rejected by the OS boundary"
    )
    assert not invoked_marker.exists()


def test_worker_without_role_handler_records_safe_durable_retry(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / "data"
    database_path = data_dir / "collaborative-development.db"
    assignment = _assignment(tmp_path)
    item = _work_item(assignment)
    asyncio.run(_seed(database_path, assignment, item, suffix="0001"))

    assert (
        main(
            [
                "worker",
                "--data-dir",
                str(data_dir),
                "--once",
                "--adapter-retry-seconds",
                "1",
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["records"][0]["status"] == "retry"
    assert emitted["records"][0]["detail"] == "agent runtime is unavailable"

    async def inspect_outbox():
        store = CollaborativeDevelopmentStore(database_path)
        await store.initialize()
        return await store.list_pending_outbox(now=utc_now() + timedelta(seconds=31))

    pending = asyncio.run(inspect_outbox())
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].last_error == "retry"

    journal = CollaborativeDevelopmentDispatchJournal(
        data_dir / "collaborative-development-dispatch.db"
    )
    journal.initialize()
    history = journal.history(assignment.assignment_id)
    assert len(history) == 1
    assert history[0].status == DispatchOutcomeStatus.retry
    assert history[0].retry_after_seconds == 30


def test_worker_retry_is_followed_by_real_delivery_when_handler_becomes_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        database_path = data_dir / "collaborative-development.db"
        journal_path = data_dir / "dispatch.db"
        assignment = _assignment(tmp_path)
        item = _work_item(assignment)
        await _seed(database_path, assignment, item, suffix="0001")

        unavailable = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers={},
            once=True,
            poll_interval_seconds=0.05,
            limit=10,
            claim_ttl_seconds=10,
            dispatcher_id="worker-without-handler",
        )
        assert [record.status for record in unavailable.records] == [DispatchOutcomeStatus.retry]

        future = utc_now() + timedelta(seconds=31)
        monkeypatch.setattr(storage_module, "utc_now", lambda: future)

        def delivered(**_):
            return DispatchOutcome(
                status=DispatchOutcomeStatus.delivered,
                detail="newly available handler accepted the same idempotent outbox",
            )

        recovered = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers={AgentRole.codex: delivered},
            once=True,
            poll_interval_seconds=0.05,
            limit=10,
            claim_ttl_seconds=10,
            dispatcher_id="worker-with-handler",
        )
        assert [record.status for record in recovered.records] == [DispatchOutcomeStatus.delivered]
        journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        journal.initialize()
        history = journal.history(assignment.assignment_id)
        assert [record.attempt for record in history] == [1, 2]
        assert [record.status for record in history] == [
            DispatchOutcomeStatus.retry,
            DispatchOutcomeStatus.delivered,
        ]
        assert history[0].outbox_id == history[1].outbox_id
        assert history[0].grant_digest == history[1].grant_digest

    asyncio.run(scenario())


def test_continuous_worker_processes_later_outbox_in_second_real_batch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        database_path = data_dir / "collaborative-development.db"
        journal_path = data_dir / "dispatch.db"
        assignment = _assignment(tmp_path)
        first_item = _work_item(assignment, objective="Fix the first parser case.")
        store = await _seed(
            database_path,
            assignment,
            first_item,
            suffix="0001",
        )
        stop_event = asyncio.Event()
        first_batch = asyncio.Event()
        observed = []

        def delivered(**_):
            return DispatchOutcome(
                status=DispatchOutcomeStatus.delivered,
                detail="role handler accepted one bounded work item",
                evidence_refs=("sha256:" + "c" * 64,),
            )

        def observe(batch):
            if batch.records:
                observed.extend(batch.records)
            if len(observed) == 1:
                first_batch.set()
            if len(observed) == 2:
                stop_event.set()

        running = asyncio.create_task(
            run_dispatch_worker(
                database_path=database_path,
                journal_path=journal_path,
                handlers={AgentRole.codex: delivered},
                once=False,
                poll_interval_seconds=0.05,
                limit=10,
                claim_ttl_seconds=10,
                dispatcher_id="continuous-worker",
                stop_event=stop_event,
                on_batch=observe,
            )
        )
        await asyncio.wait_for(first_batch.wait(), timeout=2)
        second_item = _work_item(
            assignment,
            objective="Fix the later parser case.",
        )
        await store.create_work_item(
            second_item,
            actor_role="lilies",
            actor_id="lilies-agent",
            idempotency_key="worker-item-0002",
        )
        completed = await asyncio.wait_for(running, timeout=2)

        assert completed.dispatcher_id == "continuous-worker"
        assert [record.work_item_id for record in observed] == [
            first_item.work_item_id,
            second_item.work_item_id,
        ]
        journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        journal.initialize()
        assert journal.history(assignment.assignment_id) == observed

    asyncio.run(scenario())
