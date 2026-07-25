from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_platform import lilies_cli
from agent_platform.collaborative_development_cli import (
    CollaborativeDevelopmentCliError,
    _Client,
    build_parser as build_collaboration_parser,
)
from agent_platform.collaborative_development_api import (
    create_standalone_collaborative_development_app,
)
from agent_platform.collaborative_development_auth import DevelopmentCredentialIssuer
from agent_platform.collaborative_development_authority import (
    CollaborativeDevelopmentAuthorityStore,
)
from agent_platform.collaborative_development_cli import (
    _dispatch as dispatch_collaboration_cli,
)
from agent_platform.collaborative_development_cli import (
    build_parser as build_collaboration_cli_parser,
)
from agent_platform.collaborative_development_dispatcher import (
    CollaborativeDevelopmentDispatchJournal,
    CollaborativeDevelopmentDispatcher,
    DevelopmentAuthorizationRequest,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RequestedAuthority,
    RoleBoundDispatchContext,
    canonical_digest,
)
from agent_platform.collaborative_development_models import (
    AgentRole,
    DevelopmentWorkItem,
    ExecutionMode,
    WorkItemKind,
    utc_now,
)
from agent_platform.collaborative_development_service import (
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_storage import (
    CollaborativeDevelopmentStore,
)
from agent_platform.config import Settings
from agent_platform.development_workspace_broker import (
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceError,
)
from tests.test_v04_13_collaborative_development_api import (
    OWNER_TOKEN,
    SIGNING_KEY,
    _assignment,
    _headers,
)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8780",
        "http://[::1]:8780",
        "http://localhost:8780",
        "https://collaboration.example.invalid",
    ],
)
def test_standalone_collaboration_client_accepts_only_secure_or_loopback_origins(
    base_url: str,
) -> None:
    client = _Client(base_url=base_url, access_token=OWNER_TOKEN)

    assert client.base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.0.2.10:8780",
        "http://collaboration.example.invalid",
        "http://127.0.0.1:8780/nested",
    ],
)
def test_standalone_collaboration_client_rejects_remote_plaintext_or_paths(
    base_url: str,
) -> None:
    with pytest.raises(
        CollaborativeDevelopmentCliError,
        match="plaintext collaborative development HTTP|plain http\\(s\\) origin",
    ):
        _Client(base_url=base_url, access_token=OWNER_TOKEN)


def test_standalone_cli_exposes_explicit_result_read_and_review_prepare() -> None:
    parser = build_collaboration_parser()

    shown = parser.parse_args(["result-show", str(uuid4())])
    prepared = parser.parse_args(
        [
            "review-prepare",
            str(uuid4()),
            "--idempotency-key",
            "review-prepare-parser-0001",
        ]
    )

    assert shown.command == "result-show"
    assert prepared.command == "review-prepare"
    assert prepared.idempotency_key == "review-prepare-parser-0001"


def _service(
    tmp_path: Path,
    *,
    enabled: bool,
    autonomous_enabled: bool,
) -> CollaborativeDevelopmentService:
    return CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(
            tmp_path / "collaborative-development.db"
        ),
        enabled=enabled,
        autonomous_enabled=autonomous_enabled,
    )


def _app(
    tmp_path: Path,
    *,
    enabled: bool,
    autonomous_enabled: bool,
):
    return create_standalone_collaborative_development_app(
        service=_service(
            tmp_path,
            enabled=enabled,
            autonomous_enabled=autonomous_enabled,
        ),
        credential_issuer=DevelopmentCredentialIssuer(SIGNING_KEY),
        owner_token=OWNER_TOKEN,
    )


@pytest.mark.parametrize(
    ("development_enabled", "autonomous_enabled", "expected_status"),
    [
        (False, False, 404),
        (False, True, 404),
        (True, False, 409),
        (True, True, 201),
    ],
)
def test_autonomous_assignment_creation_requires_both_feature_flags(
    tmp_path: Path,
    development_enabled: bool,
    autonomous_enabled: bool,
    expected_status: int,
) -> None:
    assignment = _assignment(tmp_path).model_copy(
        update={"execution_mode": ExecutionMode.autonomous}
    )
    with TestClient(
        _app(
            tmp_path,
            enabled=development_enabled,
            autonomous_enabled=autonomous_enabled,
        )
    ) as client:
        response = client.post(
            "/api/v1/collaborative-development/assignments",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "autonomous-create-matrix-0001",
                "assignment": assignment.model_dump(mode="json"),
            },
        )

    assert response.status_code == expected_status
    if expected_status == 409:
        assert response.json()["detail"]["code"] == "autonomous_collaboration_disabled"


@pytest.mark.parametrize(
    ("autonomous_enabled", "expected_status"),
    [(False, 409), (True, 200)],
)
def test_switch_to_autonomous_requires_the_independent_feature_flag(
    tmp_path: Path,
    autonomous_enabled: bool,
    expected_status: int,
) -> None:
    assignment = _assignment(tmp_path)
    with TestClient(
        _app(
            tmp_path,
            enabled=True,
            autonomous_enabled=autonomous_enabled,
        )
    ) as client:
        created = client.post(
            "/api/v1/collaborative-development/assignments",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "manual-create-matrix-0001",
                "assignment": assignment.model_dump(mode="json"),
            },
        )
        assert created.status_code == 201, created.text
        original_grants = created.json()["assignment"]["workspace_grants"]
        switched = client.post(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/execution-mode",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "autonomous-switch-matrix-0001",
                "expected_revision": 1,
                "mode": "autonomous",
            },
        )
        fetched = client.get(
            f"/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}",
            headers=_headers(OWNER_TOKEN),
        )

    assert switched.status_code == expected_status
    assert fetched.status_code == 200
    assert fetched.json()["workspace_grants"] == original_grants
    assert fetched.json()["execution_mode"] == (
        "autonomous" if autonomous_enabled else "manual_dispatch"
    )


def test_autonomous_environment_flag_is_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LILIES_COLLABORATIVE_DEVELOPMENT_ENABLED", "false")
    monkeypatch.setenv("LILIES_AUTONOMOUS_COLLABORATION_ENABLED", "true")

    settings = Settings(_env_file=None)

    assert settings.lilies_collaborative_development_enabled is False
    assert settings.lilies_autonomous_collaboration_enabled is True


def _insert_authority_request(
    database_path: Path,
    *,
    assignment_id,
    work_item_id,
    grant,
) -> DevelopmentAuthorizationRequest:
    request = DevelopmentAuthorizationRequest(
        request_id=uuid4(),
        assignment_id=assignment_id,
        work_item_id=work_item_id,
        outbox_id=uuid4(),
        destination_role=AgentRole.codex,
        existing_grant_digest=canonical_digest(grant),
        requested_authority=RequestedAuthority(
            paths=("outside-frozen-grant",),
            reason="The worker requested a path outside the frozen grant.",
        ),
        created_at=utc_now(),
    )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            INSERT INTO development_authorization_requests(
              request_id,outbox_id,assignment_id,work_item_id,status,
              payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                str(request.request_id),
                str(request.outbox_id),
                str(request.assignment_id),
                str(request.work_item_id),
                request.status,
                request.model_dump_json(),
                request.created_at.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return request


@contextmanager
def _running_api(app) -> Iterator[str]:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    port = int(server_socket.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        server_socket.close()
        raise RuntimeError("test collaboration API did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        server_socket.close()
        if thread.is_alive():
            raise RuntimeError("test collaboration API did not stop")


def test_main_lilies_develop_cli_runs_real_api_and_applies_only_requested_grant_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _service(tmp_path, enabled=True, autonomous_enabled=True)
    app = create_standalone_collaborative_development_app(
        service=service,
        credential_issuer=DevelopmentCredentialIssuer(SIGNING_KEY),
        owner_token=OWNER_TOKEN,
    )
    assignment = _assignment(tmp_path).model_copy(
        update={"execution_mode": ExecutionMode.autonomous}
    )
    assignment_file = tmp_path / "assignment.json"
    assignment_file.write_text(
        assignment.model_dump_json(indent=2),
        encoding="utf-8",
    )
    authority_database = tmp_path / "collaborative-development-dispatch.db"

    with _running_api(app) as base_url:
        monkeypatch.setenv("LILIES_COLLABORATIVE_DEVELOPMENT_TOKEN", OWNER_TOKEN)
        monkeypatch.setenv(
            "LILIES_COLLABORATIVE_DEVELOPMENT_BASE_URL",
            base_url,
        )

        assert (
            lilies_cli.main(
                [
                    "develop",
                    "--assignment-file",
                    str(assignment_file),
                    "--idempotency-key",
                    "main-cli-create-0001",
                ]
            )
            == 0
        )
        created_output = json.loads(capsys.readouterr().out)
        assert created_output["assignment"]["assignment_id"] == str(
            assignment.assignment_id
        )
        assert created_output["lilies_access_token"] == "[redacted]"
        original_grants = {
            grant.agent_role: grant for grant in assignment.workspace_grants
        }
        work_item = DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment.assignment_id,
            kind=WorkItemKind.bug,
            objective="Update the authorized fixture documentation.",
            acceptance=("The authorized documentation path is updated.",),
            assigned_role=AgentRole.codex,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        created_work = httpx.post(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/work-items",
            headers=_headers(OWNER_TOKEN),
            json={
                "idempotency_key": "main-cli-work-item-0001",
                "work_item": work_item.model_dump(mode="json"),
            },
        )
        assert created_work.status_code == 201, created_work.text

        def request_authority(**_):
            return DispatchOutcome(
                status=DispatchOutcomeStatus.authorization_required,
                detail="A new documentation path is required.",
                requested_authority=RequestedAuthority(
                    paths=("docs",),
                    reason="The approved task requires documentation output.",
                ),
            )

        dispatch_store = CollaborativeDevelopmentStore(
            tmp_path / "collaborative-development.db"
        )
        journal = CollaborativeDevelopmentDispatchJournal(authority_database)
        dispatcher = CollaborativeDevelopmentDispatcher(
            store=dispatch_store,
            journal=journal,
            handlers={AgentRole.codex: request_authority},
        )
        asyncio.run(dispatcher.initialize())
        first_dispatch = asyncio.run(dispatcher.dispatch_once())
        assert [record.status for record in first_dispatch] == [
            DispatchOutcomeStatus.authorization_required
        ]
        first_request = journal.authorization_requests(
            assignment.assignment_id
        )[0]
        assert (
            lilies_cli.main(
                ["develop", "status", str(assignment.assignment_id)]
            )
            == 0
        )
        status_output = json.loads(capsys.readouterr().out)
        assert [
            item["request_id"]
            for item in status_output["pending_authority_requests"]
        ] == [str(first_request.request_id)]

        original_codex = original_grants[AgentRole.codex]
        replacement_grant = original_codex.model_copy(
            update={
                "grant_revision": original_codex.grant_revision + 1,
                "allowed_paths": (*original_codex.allowed_paths, "docs"),
            }
        )
        replacement_file = tmp_path / "replacement-grant.json"
        replacement_file.write_text(
            replacement_grant.model_dump_json(indent=2),
            encoding="utf-8",
        )
        overbroad_grant = replacement_grant.model_copy(
            update={"secret_refs": ("unrequested-secret",)}
        )
        overbroad_file = tmp_path / "overbroad-grant.json"
        overbroad_file.write_text(
            overbroad_grant.model_dump_json(indent=2),
            encoding="utf-8",
        )
        assert (
            lilies_cli.main(
                [
                    "develop",
                    "approve",
                    str(assignment.assignment_id),
                    "--authority-request",
                    str(first_request.request_id),
                    "--decision",
                    "approve",
                    "--reason",
                    "This must be rejected because it adds an unrequested secret.",
                    "--expected-revision",
                    "1",
                    "--grant-file",
                    str(overbroad_file),
                    "--idempotency-key",
                    "main-cli-overbroad-authority-0001",
                ]
            )
            == 1
        )
        overbroad_error = capsys.readouterr()
        assert "replacement grant must add exactly" in overbroad_error.err
        unchanged = httpx.get(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}",
            headers=_headers(OWNER_TOKEN),
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["revision"] == 1
        assert {
            item["agent_role"]: item
            for item in unchanged.json()["workspace_grants"]
        }["codex"] == original_codex.model_dump(mode="json")

        approval_arguments = [
            "develop",
            "approve",
            str(assignment.assignment_id),
            "--authority-request",
            str(first_request.request_id),
            "--decision",
            "approve",
            "--reason",
            "Approved exactly the requested documentation path.",
            "--expected-revision",
            "1",
            "--grant-file",
            str(replacement_file),
            "--idempotency-key",
            "main-cli-authority-approve-0001",
        ]
        revise_prepared_grant = DevelopmentWorkspaceBroker.revise_prepared_grant

        def fail_first_broker_revision(*_args, **_kwargs):
            raise DevelopmentWorkspaceError("injected manifest write failure")

        monkeypatch.setattr(
            DevelopmentWorkspaceBroker,
            "revise_prepared_grant",
            fail_first_broker_revision,
        )
        assert lilies_cli.main(approval_arguments) == 1
        interrupted = capsys.readouterr()
        assert "broker attestation" in interrupted.err
        partially_applied = httpx.get(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}/authority-requests?status=approved",
            headers=_headers(OWNER_TOKEN),
        )
        assert partially_applied.status_code == 200
        partial_request = partially_applied.json()["requests"][0]
        assert partial_request["status"] == "approved"
        assert partial_request["grant_changed"] is False
        assert partial_request["execution_resumed"] is False
        assert partial_request["next_action"] == "retry_approved_grant_revision"
        assert asyncio.run(dispatch_store.list_pending_outbox()) == []

        monkeypatch.setattr(
            DevelopmentWorkspaceBroker,
            "revise_prepared_grant",
            revise_prepared_grant,
        )
        assert lilies_cli.main(approval_arguments) == 0
        approved = json.loads(capsys.readouterr().out)
        assert approved["status"] == "approved"
        assert approved["grant_changed"] is True
        assert approved["execution_resumed"] is True
        assert approved["next_action"] == "continue_dispatch"
        assert lilies_cli.main(approval_arguments) == 0
        replayed = json.loads(capsys.readouterr().out)
        assert replayed["status"] == "approved"
        assert replayed["applied_at"] == approved["applied_at"]

        def deliver_with_revised_grant(*, context: RoleBoundDispatchContext):
            assert context.workspace_grant == replacement_grant
            return DispatchOutcome(
                status=DispatchOutcomeStatus.delivered,
                detail="The revised grant was applied and dispatch resumed.",
            )

        dispatcher.handlers[AgentRole.codex] = deliver_with_revised_grant
        resumed_dispatch = asyncio.run(dispatcher.dispatch_once())
        assert [record.status for record in resumed_dispatch] == [
            DispatchOutcomeStatus.delivered
        ]

        second_request = _insert_authority_request(
            authority_database,
            assignment_id=assignment.assignment_id,
            work_item_id=uuid4(),
            grant=original_codex,
        )
        assert (
            lilies_cli.main(
                [
                    "develop",
                    "approve",
                    str(assignment.assignment_id),
                    "--authority-request",
                    str(second_request.request_id),
                    "--decision",
                    "reject",
                    "--reason",
                    "The requested path is outside this task.",
                    "--idempotency-key",
                    "main-cli-authority-reject-0001",
                ]
            )
            == 0
        )
        rejected = json.loads(capsys.readouterr().out)
        assert rejected["status"] == "rejected"
        assert rejected["grant_changed"] is False

        fetched = httpx.get(
            f"{base_url}/api/v1/collaborative-development/assignments/"
            f"{assignment.assignment_id}",
            headers=_headers(OWNER_TOKEN),
        )
        assert fetched.status_code == 200
        fetched_grants = {
            item["agent_role"]: item
            for item in fetched.json()["workspace_grants"]
        }
        assert fetched_grants["lilies"] == original_grants[
            AgentRole.lilies
        ].model_dump(mode="json")
        assert fetched_grants["codex"] == replacement_grant.model_dump(mode="json")

        assert (
            lilies_cli.main(
                [
                    "develop",
                    "stop",
                    str(assignment.assignment_id),
                    "--expected-revision",
                    "2",
                    "--idempotency-key",
                    "main-cli-stop-0001",
                ]
            )
            == 0
        )
        stopped = json.loads(capsys.readouterr().out)
        assert stopped["status"] == "stopped"

    restarted = CollaborativeDevelopmentAuthorityStore(authority_database)
    asyncio.run(restarted.initialize())
    decisions = asyncio.run(
        restarted.list_requests(assignment.assignment_id, status="all")
    )
    assert [item.status for item in decisions] == ["approved", "rejected"]
    assert [item.grant_changed for item in decisions] == [True, False]


def test_standalone_cli_requires_explicit_unknown_review_confirmation_and_fences_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assignment_id = uuid4()
    outbox_id = uuid4()
    parser = build_collaboration_cli_parser()
    arguments = [
        "review-requeue",
        str(assignment_id),
        str(outbox_id),
        "--expected-work-item-revision",
        "7",
        "--expected-failed-attempt",
        "2",
        "--reason",
        "The owner inspected the uncertain attempt.",
        "--idempotency-key",
        "cli-review-requeue-0001",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(arguments)
    assert "--confirm-unknown-outcome" in capsys.readouterr().err

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        def request(
            self,
            method: str,
            path: str,
            payload: dict | None = None,
        ) -> dict:
            self.calls.append((method, path, payload))
            return {"ok": True}

    client = RecordingClient()
    parsed = parser.parse_args([*arguments, "--confirm-unknown-outcome"])
    assert dispatch_collaboration_cli(parsed, client) == {"ok": True}
    assert client.calls == [
        (
            "POST",
            (
                "/api/v1/collaborative-development/assignments/"
                f"{assignment_id}/review-reconciliations/{outbox_id}/requeue"
            ),
            {
                "idempotency_key": "cli-review-requeue-0001",
                "expected_work_item_revision": 7,
                "expected_failed_attempt": 2,
                "confirmation": "requeue_unknown_review_attempt",
                "reason": "The owner inspected the uncertain attempt.",
            },
        )
    ]

    listed = parser.parse_args(
        ["review-reconciliations", str(assignment_id)]
    )
    assert dispatch_collaboration_cli(listed, client) == {"ok": True}
    assert client.calls[-1] == (
        "GET",
        (
            "/api/v1/collaborative-development/assignments/"
            f"{assignment_id}/review-reconciliations"
        ),
        None,
    )
