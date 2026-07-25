from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from agent_platform.lilies_collaboration_client import LiliesCollaborationClient
from agent_platform.lilies_collaboration_tools import (
    register_lilies_collaboration_tools,
)
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_identity import build_lilies_system_prompt
from agent_platform.lilies_models import BuildAssignment, ProhibitedAction
from agent_platform.lilies_service import LiliesServiceError, LocalLiliesService
from agent_platform.lilies_storage import (
    LiliesAccessDeniedError,
    LiliesAuthenticationError,
    LiliesConflictError,
)
from agent_platform.lilies_tools import (
    LiliesToolContext,
    LiliesToolRegistry,
    LiliesToolResult,
)
from tests.test_v04_13_lilies_service import ScriptedLocalProvider


CONTRACT_DIGEST = "sha256:" + "a" * 64
FIXTURE_DIGEST = "sha256:" + "b" * 64
PLATFORM_TOKEN = "private-platform-assignment-token-value"
COLLABORATION_TOKEN = "private-collaboration-channel-token-value"
COLLABORATION_TOOL_NAMES = {
    "collaboration_report_submit",
    "collaboration_updates_read",
    "collaboration_formal_run_archive",
    "collaboration_verification_claim",
}
COLLABORATION_SCOPES = [
    "collaboration.report:write",
    "collaboration.response:read",
]


def assignment_payload(
    *,
    formal: bool,
    assignment_id: str | None = None,
    platform_credential_ref: str = "credential:platform-daemon-tools",
    collaboration_credential_ref: str = "credential:collaboration-daemon-tools",
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc)
    deadline_at = created_at + timedelta(hours=1)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "assignment_id": assignment_id or str(uuid4()),
        "idempotency_key": f"assignment:{uuid4().hex}",
        "mode": "formal_experiment" if formal else "customer",
        "requirement": "Build a verified enterprise intake and reconciliation workflow.",
        "business_context": {
            "customer_roles": ["operations manager"],
            "business_goal": "Reconcile approved records and produce a reviewable artifact.",
            "inputs": ["approved records"],
            "outputs": ["reconciliation report"],
            "constraints": ["platform remains a black box"],
        },
        "target": {"mode": "create_new"},
        "platform": {
            "base_url": "http://127.0.0.1:18999",
            "contract_url": "/api/v1/lilies/platform-contract",
            "contract_digest": CONTRACT_DIGEST,
            "credential_ref": platform_credential_ref,
            "scopes": ["workflow.catalog:read"],
            "application_ids": [],
        },
        "constraints": {
            "deadline_at": deadline_at.isoformat(),
            "max_turns": 30,
            "max_tool_calls": 100,
            "network_policy": "allowlist",
            "allowed_hosts": ["127.0.0.1"],
            "allowed_actions": ["platform_contract_get"],
            "prohibited_actions": [action.value for action in ProhibitedAction],
            "no_substitute_validation": formal,
        },
        "deliverables": [
            {
                "name": "reconciliation report",
                "description": "A customer-visible structured reconciliation report.",
                "media_type": "application/json",
            }
        ],
        "created_at": created_at.isoformat(),
    }
    if formal:
        collaboration_expiry = deadline_at + timedelta(minutes=5)
        payload.update(
            {
                "task_package": {
                    "task_id": "EXP-LILIES-COLLABORATION-001",
                    "revision": 1,
                    "public_summary_digest": CONTRACT_DIGEST,
                },
                "fixture_refs": [
                    {
                        "artifact_id": "fixture:collaboration-public-0001",
                        "digest": FIXTURE_DIGEST,
                        "media_type": "application/json",
                        "display_name": "public-input.json",
                    }
                ],
                "collaboration": {
                    "channel_id": str(uuid4()),
                    "credential_ref": collaboration_credential_ref,
                    "scopes": COLLABORATION_SCOPES,
                    "expires_at": collaboration_expiry.isoformat(),
                },
            }
        )
        payload["constraints"]["max_budget_usd"] = 5.0
    return payload


def service_at(tmp_path: Path) -> LocalLiliesService:
    return LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
        ),
        provider=ScriptedLocalProvider(),
    )


async def provision_platform_access(
    service: LocalLiliesService,
    assignment: BuildAssignment,
) -> None:
    await service.storage.provision_credential(
        "platform_assignment",
        PLATFORM_TOKEN,
        scopes=[scope.value for scope in assignment.platform.scopes],
        credential_ref=assignment.platform.credential_ref,
        assignment_id=str(assignment.assignment_id),
        expires_at=assignment.constraints.deadline_at + timedelta(minutes=5),
    )


async def create_assigned_session(
    service: LocalLiliesService,
    assignment: BuildAssignment,
) -> str:
    session_id = str(uuid4())
    await service.storage.create_session(
        session_id=session_id,
        assignment_id=str(assignment.assignment_id),
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=assignment.platform.contract_digest,
    )
    return session_id


async def create_and_run_turn(
    service: LocalLiliesService,
    session_id: str,
) -> str:
    message_id = str(uuid4())
    message = await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "Exercise the formal collaboration path."}],
        message_id=message_id,
    )
    turn = await service.storage.create_turn(
        session_id,
        request_id=message_id,
        idempotency_key=f"turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )
    await service._run_turn(session_id, turn["id"])
    return str(turn["id"])


async def durable_session_projection(
    service: LocalLiliesService,
    session_id: str,
) -> str:
    projection = {
        "session": await service.storage.get_session(session_id),
        "messages": await service.storage.list_messages(session_id, limit=5_000),
        "turns": await service.storage.list_turns(session_id),
        "events": await service.storage.list_events(session_id, after=0, limit=5_000),
        "permissions": await service.storage.list_pending_permission_requests(
            session_id=session_id
        ),
    }
    return json.dumps(projection, ensure_ascii=False, sort_keys=True)


def collaboration_report_payload(
    *,
    summary: str = "The public catalog lacks a generic intake contract.",
    requested_outcome: str = "Add a generic typed intake contract.",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "report_id": str(uuid4()),
        "category": "platform_capability_gap",
        "phase": "preflight",
        "severity": "blocking",
        "summary": summary,
        "original_goal": "Build a verified enterprise intake workflow.",
        "requirement_digest": CONTRACT_DIGEST,
        "manuals_checked": [],
        "attempted_routes": [],
        "blocking_scope": "Typed intake is blocked; artifact planning can continue.",
        "independent_work": ["Plan the artifact validation branch."],
        "workaround_considered": ["Use the closest documented catalog block."],
        "workaround_loss": "The substitute loses typed validation evidence.",
        "requested_outcome": requested_outcome,
        "confidence": 0.96,
        "secret_redactions": ["model_supplied_credentials"],
        "evidence_refs": [],
        "missing_contract": "Typed inputs, outputs, errors, and immutable evidence.",
    }


async def provision_collaboration_access(
    service: LocalLiliesService,
    assignment: BuildAssignment,
    *,
    kind: str = "collaboration_channel",
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    assignment_id: str | None = None,
) -> None:
    access = assignment.collaboration
    assert access is not None
    await service.storage.provision_credential(
        kind,
        COLLABORATION_TOKEN,
        scopes=COLLABORATION_SCOPES if scopes is None else scopes,
        credential_ref=access.credential_ref,
        assignment_id=assignment_id or str(assignment.assignment_id),
        expires_at=expires_at or access.expires_at,
    )


@pytest.mark.asyncio
async def test_customer_assignment_has_no_collaboration_projection_tools_or_prompt(
    tmp_path: Path,
) -> None:
    service = service_at(tmp_path)
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=False))
    projection = assignment.model_dump(mode="json", exclude_none=True)
    serialized_projection = json.dumps(projection, sort_keys=True)

    assert "collaboration" not in projection
    assert "collaboration" not in serialized_projection.casefold()
    assert "合作管道" not in serialized_projection

    await provision_platform_access(service, assignment)
    session_id = await create_assigned_session(service, assignment)
    registry = await service.tool_registry_for_session(session_id)
    assert registry.names() == ["platform_contract_get"]
    assert COLLABORATION_TOOL_NAMES.isdisjoint(registry.names())
    assert "collaboration" not in json.dumps(
        [definition.model_dump(mode="json") for definition in registry.definitions()],
        sort_keys=True,
    ).casefold()

    prompt = build_lilies_system_prompt(
        workspace=str(tmp_path / "workspaces" / session_id),
        tool_names=registry.names(),
        collaboration_active=False,
    )
    assert "collaboration" not in prompt.casefold()
    assert "合作管道" not in prompt
    assert "Codex" not in prompt


@pytest.mark.asyncio
async def test_formal_assignment_gets_exactly_four_tools_with_context_recall_mode(
    tmp_path: Path,
) -> None:
    service = service_at(tmp_path)
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    access = assignment.collaboration
    assert access is not None
    await provision_platform_access(service, assignment)
    await provision_collaboration_access(service, assignment)
    session_id = await create_assigned_session(service, assignment)

    registry = await service.tool_registry_for_session(session_id)
    assert set(registry.names()) == {
        "platform_contract_get",
        *COLLABORATION_TOOL_NAMES,
    }
    assert len(COLLABORATION_TOOL_NAMES & set(registry.names())) == 4
    for name in COLLABORATION_TOOL_NAMES:
        tool = registry.get(name)
        assert tool.client.channel_id == access.channel_id
        assert COLLABORATION_TOKEN not in repr(tool.client)
        assert PLATFORM_TOKEN not in repr(tool.client)

    test_run_ids = [f"test-run:formal-recall-{index:03d}" for index in range(101)]
    application_id = str(uuid4())
    workflow_tool_use_id = "formal-workflow-recall-tests"
    await service.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": workflow_tool_use_id,
                "name": "platform_tests_run",
                "input": {"application_id": application_id},
            }
        ],
    )
    await service.storage.add_message(
        session_id,
        "tool",
        [
            {
                "type": "tool_result",
                "tool_use_id": workflow_tool_use_id,
                "content": json.dumps(
                    {
                        "ok": True,
                        "operation": "platform_tests_run",
                        "request_id": str(uuid4()),
                        "status_code": 200,
                        "contract_digest": str(assignment.platform.contract_digest),
                        "data": {
                            "passed": True,
                            "validation": {
                                "revision": 7,
                                "content_hash": "sha256:" + "c" * 64,
                            },
                            "tests": [
                                {"run_id": run_id} for run_id in test_run_ids
                            ],
                        },
                        "error": None,
                        "evidence_refs": [],
                    },
                    sort_keys=True,
                ),
            }
        ],
    )
    archive_index_result = await registry.get("collaboration_updates_read").execute(
        {
            "archive_collection": "current_workflow",
            "archive_field": "index",
            "archive_offset": 0,
            "archive_limit": 100,
        },
        LiliesToolContext(session_id=session_id, workspace=tmp_path),
    )
    archive_index_payload = json.loads(archive_index_result.content)
    assert archive_index_payload["ok"] is True
    archive_index = archive_index_payload["data"]["archive_recall"]
    state_digest_b64 = archive_index["values"][0]["state_digest_b64"]

    archive_result = await registry.get("collaboration_updates_read").execute(
        {
            "archive_collection": "current_workflow",
            "archive_field": "test_run_ids",
            "archive_state_digest_b64": state_digest_b64,
            "archive_offset": 0,
            "archive_limit": 100,
        },
        LiliesToolContext(session_id=session_id, workspace=tmp_path),
    )
    archive_payload = json.loads(archive_result.content)
    assert archive_payload["ok"] is True
    recall = archive_payload["data"]["archive_recall"]
    assert recall["values"] == test_run_ids[:100]
    assert recall["next_offset"] == 100
    assert recall["complete"] is False

    prompt = build_lilies_system_prompt(
        workspace=str(tmp_path / "workspaces" / session_id),
        tool_names=registry.names(),
        collaboration_active=True,
    )
    assert "临时合作管道" in prompt
    assert COLLABORATION_TOOL_NAMES <= set(prompt.split()) or all(
        name in prompt for name in COLLABORATION_TOOL_NAMES
    )
    assert COLLABORATION_TOKEN not in prompt
    assert PLATFORM_TOKEN not in prompt
    projection = assignment.model_dump(mode="json", exclude_none=True)
    assert COLLABORATION_TOKEN not in json.dumps(projection, sort_keys=True)
    assert projection["platform"]["credential_ref"] != (
        projection["collaboration"]["credential_ref"]
    )


@pytest.mark.asyncio
async def test_equal_platform_and_collaboration_bearers_fail_before_assignment_receipt(
    tmp_path: Path,
) -> None:
    service = service_at(tmp_path)
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    access = assignment.collaboration
    assert access is not None
    shared_bearer = "same-platform-and-collaboration-bearer-value"
    await service.storage.provision_credential(
        "platform_assignment",
        shared_bearer,
        scopes=[scope.value for scope in assignment.platform.scopes],
        credential_ref=assignment.platform.credential_ref,
        assignment_id=str(assignment.assignment_id),
        expires_at=assignment.constraints.deadline_at + timedelta(minutes=5),
    )
    await service.storage.provision_credential(
        "collaboration_channel",
        shared_bearer,
        scopes=COLLABORATION_SCOPES,
        credential_ref=access.credential_ref,
        assignment_id=str(assignment.assignment_id),
        expires_at=access.expires_at,
    )
    session_id = str(uuid4())
    await service.storage.create_session(
        session_id=session_id,
        config={"kind": "platform"},
    )

    with pytest.raises(LiliesAccessDeniedError, match="must be distinct"):
        await service.submit_assignment(  # type: ignore[arg-type]
            session_id,
            assignment,
            client_id=None,
        )
    session = await service.storage.get_session(session_id)
    assert session["status"] == "ready"
    assert session["assignment_id"] is None
    assert await service.storage.list_turns(session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch", "error_type", "expected_error"),
    [
        ("kind", LiliesServiceError, "wrong kind"),
        ("scope", LiliesServiceError, "scopes do not exactly match"),
        ("expiry", LiliesAuthenticationError, "expiry does not match"),
        ("assignment", LiliesServiceError, "bound to another assignment"),
    ],
)
async def test_formal_collaboration_tools_fail_closed_on_credential_mismatch(
    tmp_path: Path,
    mismatch: str,
    error_type: type[Exception],
    expected_error: str,
) -> None:
    service = service_at(tmp_path / mismatch)
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    access = assignment.collaboration
    assert access is not None
    await provision_platform_access(service, assignment)
    await provision_collaboration_access(
        service,
        assignment,
        kind="platform_assignment" if mismatch == "kind" else "collaboration_channel",
        scopes=["collaboration.report:write"] if mismatch == "scope" else None,
        expires_at=(access.expires_at + timedelta(seconds=1))
        if mismatch == "expiry"
        else None,
        assignment_id=str(uuid4()) if mismatch == "assignment" else None,
    )
    session_id = await create_assigned_session(service, assignment)

    with pytest.raises(error_type, match=expected_error):
        await service.tool_registry_for_session(session_id)
    assert session_id not in service.assignment_tool_bindings


@pytest.mark.asyncio
async def test_collaboration_tool_input_is_redacted_before_every_daemon_projection(
    tmp_path: Path,
) -> None:
    authorization_secret = "model-authorization-secret-0123456789"
    cookie_secret = "model-cookie-secret-0123456789"
    provider_pattern_secret = "sk-ABCDEFGHIJKLMNOPQRSTUV123456"
    report = collaboration_report_payload(
        summary=f"Authorization: Bearer {authorization_secret}"
    )
    report.update(
        {
            "cookie": f"session={cookie_secret}",
            "access_token": "explicit-token-field-secret",
            "provider_note": provider_pattern_secret,
            # The provisioned collaboration bearer is not pattern-shaped.  It
            # must still be removed when copied into a generically named field.
            "innocent_label": f"copied value {COLLABORATION_TOKEN}",
        }
    )
    tool_input = {
        "operation": "submit",
        "idempotency_key": "collaboration-report-sensitive-0001",
        "report": report,
    }
    provider = ScriptedLocalProvider(
        tool="collaboration_report_submit",
        tool_input=tool_input,
    )
    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
        ),
        provider=provider,
    )
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    await provision_platform_access(service, assignment)
    await provision_collaboration_access(service, assignment)
    session_id = await create_assigned_session(service, assignment)

    await create_and_run_turn(service, session_id)

    durable = await durable_session_projection(service, session_id)
    forbidden = {
        authorization_secret,
        cookie_secret,
        "explicit-token-field-secret",
        provider_pattern_secret,
        COLLABORATION_TOKEN,
    }
    assert "[REDACTED]" in durable
    assert all(secret not in durable for secret in forbidden)
    assistant = next(
        message
        for message in await service.storage.list_messages(session_id, limit=5_000)
        if message["role"] == "assistant"
        and any(block.get("type") == "tool_use" for block in message["content"])
    )
    persisted_input = next(
        block["input"]
        for block in assistant["content"]
        if block.get("type") == "tool_use"
    )
    assert persisted_input["report"]["summary"] == "[REDACTED]"
    assert persisted_input["report"]["access_token"] == "[REDACTED]"
    assert persisted_input["report"]["cookie"] == "[REDACTED]"
    assert persisted_input["report"]["provider_note"] == "[REDACTED]"
    assert persisted_input["report"]["innocent_label"] == "copied value [REDACTED]"
    model_replay = json.dumps(
        [message.model_dump(mode="json") for message in provider.seen_messages[-1]],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert all(secret not in model_replay for secret in forbidden)


@pytest.mark.asyncio
async def test_collaboration_tool_protected_oracle_fails_closed_without_raw_persistence(
    tmp_path: Path,
) -> None:
    protected_value = "protected://hidden-answer/super-secret-oracle-answer-9173"
    provider = ScriptedLocalProvider(
        tool="collaboration_report_submit",
        tool_input={
            "operation": "submit",
            "idempotency_key": "collaboration-report-protected-0001",
            "report": collaboration_report_payload(summary=protected_value),
        },
    )
    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "lilies",
            workspace_root=tmp_path / "workspaces",
        ),
        provider=provider,
    )
    await service.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    await provision_platform_access(service, assignment)
    await provision_collaboration_access(service, assignment)
    session_id = await create_assigned_session(service, assignment)

    await create_and_run_turn(service, session_id)

    durable = await durable_session_projection(service, session_id)
    assert protected_value not in durable
    assert "super-secret-oracle-answer-9173" not in durable
    assert "collaboration tool input was rejected by daemon safety policy" in durable
    assistant = next(
        message
        for message in await service.storage.list_messages(session_id, limit=5_000)
        if message["role"] == "assistant"
        and any(block.get("type") == "tool_use" for block in message["content"])
    )
    persisted_input = next(
        block["input"]
        for block in assistant["content"]
        if block.get("type") == "tool_use"
    )
    assert persisted_input == {"sensitive_payload": "[REDACTED]"}
    for path in service.settings.data_dir.iterdir():
        if path.is_file():
            assert protected_value.encode() not in path.read_bytes()


@pytest.mark.asyncio
async def test_collaboration_permission_checkpoint_and_restart_use_only_redacted_input(
    tmp_path: Path,
) -> None:
    raw_secret = "permission-cookie-secret-0123456789"
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "workspaces",
    )
    first = LocalLiliesService(
        settings,
        provider=ScriptedLocalProvider(
            tool="collaboration_report_submit",
            tool_input={
                "operation": "submit",
                "idempotency_key": "collaboration-permission-sensitive-0001",
                "report": collaboration_report_payload(
                    summary=f"Cookie: session={raw_secret}",
                    requested_outcome=COLLABORATION_TOKEN,
                ),
            },
        ),
    )
    await first.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    await provision_platform_access(first, assignment)
    await provision_collaboration_access(first, assignment)
    session_id = await create_assigned_session(first, assignment)
    registry = await first.tool_registry_for_session(session_id)
    registry.get("collaboration_report_submit").requires_permission = True
    message_id = str(uuid4())
    message = await first.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "Request a permission-gated collaboration report."}],
        message_id=message_id,
    )
    turn = await first.storage.create_turn(
        session_id,
        request_id=message_id,
        idempotency_key=f"turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )
    first._start_turn_task(session_id, turn["id"])
    pending: list[dict[str, Any]] = []
    for _ in range(100):
        pending = await first.storage.list_pending_permission_requests(
            session_id=session_id
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    assert len(pending) == 1
    permission = pending[0]
    assert permission["tool_input"]["report"]["summary"] == "[REDACTED]"
    assert permission["tool_input"]["report"]["requested_outcome"] == "[REDACTED]"
    checkpoint = (await first.storage.get_turn(turn["id"]))["checkpoint"]
    assert checkpoint["pending"]["tool_input"] == permission["tool_input"]
    before_restart = await durable_session_projection(first, session_id)
    assert raw_secret not in before_restart
    assert COLLABORATION_TOKEN not in before_restart
    await first.shutdown(reason="permission_redaction_restart")

    resumed = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await resumed.initialize()
    resolved = await resumed.storage.resolve_permission_request(
        session_id,
        permission["id"],
        "allowed",
        expected_input_digest=permission["input_digest"],
        idempotency_key="allow-collaboration-permission-redacted-0001",
    )
    assert resolved["decision_input"] == permission["tool_input"]
    resumed_turn = await resumed.storage.get_turn(turn["id"])
    await resumed._run_turn(
        session_id,
        resumed_turn["id"],
        resume_permission=resolved,
    )

    after_restart = await durable_session_projection(resumed, session_id)
    assert raw_secret not in after_restart
    assert COLLABORATION_TOKEN not in after_restart
    assert '"summary": "[REDACTED]"' in after_restart
    assert "resumed_after_permission" in after_restart


@pytest.mark.asyncio
async def test_formal_archive_tool_calls_the_intent_endpoint_with_current_channel_revision(
    tmp_path: Path,
) -> None:
    channel_id = uuid4()
    claim_id = uuid4()
    assignment_id = uuid4()
    seen: list[httpx.Request] = []
    revisions = iter((9, 10))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        channel_path = f"/api/v1/collaboration/channels/{channel_id}"
        if request.method == "GET" and request.url.path == channel_path:
            return httpx.Response(
                200,
                request=request,
                json={"channel_id": str(channel_id), "revision": next(revisions)},
            )
        assert request.method == "POST"
        assert request.url.path == f"{channel_path}/formal-run-archives"
        return httpx.Response(
            201,
            request=request,
            json={
                "schema_version": "1.0",
                "task_id": "EXP-LILIES-TOOLS-001",
                "revision": 1,
                "run_id": "run:formal-tools-0001",
                "assignment_id": str(assignment_id),
                "channel_id": str(channel_id),
                "claim_id": str(claim_id),
                "intent_digest": CONTRACT_DIGEST,
                "state": "awaiting_daemon_completion",
                "accepted_at": "2026-07-24T00:00:00Z",
                "replayed": False,
            },
        )

    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:8001",
        access_token=COLLABORATION_TOKEN,
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)
    result = await registry.get("collaboration_formal_run_archive").execute(
        {
            "idempotency_key": "formal-archive-tool-intent-0001",
            "claim_id": str(claim_id),
            "test_run_ids": ["test-run:formal-tools-0001"],
            "business_run_ids": ["business-run:formal-tools-0001"],
            "remaining_limits": ["controlled local evidence only"],
            "summary": "Freeze the complete formal evidence selection.",
        },
        LiliesToolContext(
            session_id=str(uuid4()),
            workspace=tmp_path,
            turn_id=str(uuid4()),
            tool_call_id=str(uuid4()),
        ),
    )

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["data"]["claim_id"] == str(claim_id)
    assert payload["data"]["channel_state"]["revision"] == 10
    assert [(item.method, item.url.path) for item in seen] == [
        ("GET", f"/api/v1/collaboration/channels/{channel_id}"),
        (
            "POST",
            f"/api/v1/collaboration/channels/{channel_id}/formal-run-archives",
        ),
        ("GET", f"/api/v1/collaboration/channels/{channel_id}"),
    ]
    assert json.loads(seen[1].content) == {
        "idempotency_key": "formal-archive-tool-intent-0001",
        "claim_id": str(claim_id),
        "test_run_ids": ["test-run:formal-tools-0001"],
        "business_run_ids": ["business-run:formal-tools-0001"],
        "artifact_ids": [],
        "host_receipt_ids": [],
        "remaining_limits": ["controlled local evidence only"],
        "summary": "Freeze the complete formal evidence selection.",
        "expected_channel_revision": 9,
    }
    assert seen[1].headers["Authorization"] == f"Bearer {COLLABORATION_TOKEN}"
    assert COLLABORATION_TOKEN not in str(seen[1].url)
    assert COLLABORATION_TOKEN.encode() not in seen[1].content


@pytest.mark.asyncio
async def test_collaboration_http_tools_use_header_only_credentials_and_never_leak(
    tmp_path: Path,
) -> None:
    channel_id = uuid4()
    seen: list[httpx.Request] = []
    state_revisions = iter((7, 8, 9, 10, 11, 12, 13))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        channel_path = f"/api/v1/collaboration/channels/{channel_id}"
        if request.method == "GET" and request.url.path == channel_path:
            return httpx.Response(
                200,
                request=request,
                json={
                    "channel_id": str(channel_id),
                    "revision": next(state_revisions),
                    "reader_cursor": {
                        "channel_id": str(channel_id),
                        "reader_role": "lilies",
                        "reader_id": context.session_id,
                        "ack_seq": 0,
                        "revision": 0,
                    },
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                request=request,
                json={"events": [{"seq": 5, "type": "developer_response"}]},
            )
        return httpx.Response(200, request=request, json={"accepted": True})

    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:8001",
        access_token=COLLABORATION_TOKEN,
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)
    context = LiliesToolContext(
        session_id=str(uuid4()),
        workspace=tmp_path,
        turn_id=str(uuid4()),
        tool_call_id=str(uuid4()),
    )
    report = collaboration_report_payload()
    claim = {
        "schema_version": "1.0",
        "claim_id": str(uuid4()),
        "application_id": str(uuid4()),
        "draft_revision": 4,
        "content_hash": CONTRACT_DIGEST,
        "test_run_ids": ["test-run:http-contract-0001"],
        "business_run_ids": ["business-run:http-contract-0001"],
        "artifact_refs": [],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": [],
        "claim": "ready_for_independent_verification",
    }

    results = [
        await registry.get("collaboration_report_submit").execute(
            {
                "operation": "submit",
                "idempotency_key": "collaboration-report-0001",
                "report": report,
            },
            context,
        ),
        await registry.get("collaboration_updates_read").execute(
            {
                "after": 4,
                "acknowledge_through": 4,
                "limit": 50,
            },
            context,
        ),
        await registry.get("collaboration_verification_claim").execute(
            {
                "idempotency_key": "collaboration-claim-0001",
                "claim": claim,
            },
            context,
        ),
    ]
    results.append(
        await registry.get("collaboration_updates_read").execute(
            {"after": 0, "limit": 500, "history_replay": True},
            context,
        )
    )

    assert all(result.is_error is False for result in results)
    # Every successful tool also refreshes channel CAS state.  The updates tool
    # performs its explicit acknowledgement before reading the durable cursor.
    channel_path = f"/api/v1/collaboration/channels/{channel_id}"
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", channel_path),
        ("POST", f"{channel_path}/reports"),
        ("GET", channel_path),
        ("GET", channel_path),
        ("POST", f"{channel_path}/acks"),
        ("GET", f"{channel_path}/events"),
        ("GET", channel_path),
        ("GET", channel_path),
        ("POST", f"{channel_path}/verification-claims"),
        ("GET", channel_path),
        ("GET", f"{channel_path}/events"),
        ("GET", channel_path),
    ]
    assert all(not request.url.path.endswith("/") for request in seen)
    report_request = seen[1]
    assert json.loads(report_request.content) == {
        "idempotency_key": "collaboration-report-0001",
        "expected_channel_revision": 7,
        "report": report,
    }
    assert json.loads(seen[4].content) == {
        "idempotency_key": f"collaboration.ack.{channel_id.hex}.4",
        "expected_cursor_revision": 0,
        "reader_role": "lilies",
        "reader_id": context.session_id,
        "ack_seq": 4,
    }
    assert dict(seen[5].url.params) == {
        "limit": "50",
        "format": "json",
        "after": "4",
    }
    assert dict(seen[10].url.params) == {
        "limit": "500",
        "format": "json",
        "after": "0",
        "history_replay": "true",
    }
    assert json.loads(seen[8].content) == {
        "idempotency_key": "collaboration-claim-0001",
        "expected_channel_revision": 11,
        "claim": claim,
    }
    rejected_replay_ack = await registry.get("collaboration_updates_read").execute(
        {
            "after": 0,
            "acknowledge_through": 4,
            "history_replay": True,
        },
        context,
    )
    assert rejected_replay_ack.is_error is True
    for request in seen:
        assert request.headers["Authorization"] == f"Bearer {COLLABORATION_TOKEN}"
        assert request.headers["Accept"] == "application/json"
        assert "Cookie" not in request.headers
        assert COLLABORATION_TOKEN not in str(request.url)
        assert COLLABORATION_TOKEN.encode() not in request.content
        assert all(
            key.casefold() not in {"token", "access_token", "authorization", "cookie"}
            for key in request.url.params.keys()
        )
        if request.method == "POST":
            assert request.headers["Content-Type"] == "application/json"

    report_result = json.loads(results[0].content)
    updates_result = json.loads(results[1].content)
    claim_result = json.loads(results[2].content)
    assert report_result["data"]["channel_state"]["revision"] == 8
    assert updates_result["data"]["channel_state"]["revision"] == 10
    assert claim_result["data"]["channel_state"]["revision"] == 12

    definitions = {
        definition.name: definition.input_schema for definition in registry.definitions()
    }
    report_schema = definitions["collaboration_report_submit"]
    report_required = set(report_schema["$defs"]["CollaborationReportPayload"]["required"])
    reprobe_required = set(
        report_schema["$defs"]["LiliesReprobeResultPayload"]["required"]
    )
    claim_schema = definitions["collaboration_verification_claim"]
    claim_required = set(claim_schema["$defs"]["VerificationClaimPayload"]["required"])
    assert {
        "report_id",
        "category",
        "phase",
        "severity",
        "summary",
        "original_goal",
        "requirement_digest",
        "blocking_scope",
        "workaround_considered",
        "workaround_loss",
        "requested_outcome",
        "confidence",
        "secret_redactions",
    } <= report_required
    assert {
        "reprobe_id",
        "outcome",
        "contract_digest",
        "steps",
        "expected",
        "actual",
        "evidence_refs",
    } <= reprobe_required
    assert {
        "claim_id",
        "application_id",
        "draft_revision",
        "content_hash",
        "test_run_ids",
        "business_run_ids",
        "claim",
    } <= claim_required
    assert report_schema["$defs"]["CollaborationReportPayload"]["additionalProperties"] is False
    assert report_schema["$defs"]["LiliesReprobeResultPayload"]["additionalProperties"] is False
    assert claim_schema["$defs"]["VerificationClaimPayload"]["additionalProperties"] is False
    assert "expected_channel_revision" not in report_schema["properties"]
    assert "expected_channel_revision" not in claim_schema["properties"]
    assert (
        "expected_cursor_revision"
        not in definitions["collaboration_updates_read"]["properties"]
    )
    assert definitions["collaboration_updates_read"]["properties"]["limit"]["maximum"] == 500

    model_surface = json.dumps(
        {
            "definitions": [
                definition.model_dump(mode="json") for definition in registry.definitions()
            ],
            "results": [result.content for result in results],
            "client_repr": repr(client),
        },
        sort_keys=True,
    )
    assert COLLABORATION_TOKEN not in model_surface
    assert PLATFORM_TOKEN not in model_surface
    assert "authorization" not in model_surface.casefold()
    assert "cookie" not in model_surface.casefold()
    assert UUID(str(channel_id)) == client.channel_id


@pytest.mark.asyncio
async def test_mutating_tool_fails_closed_when_channel_cas_state_is_unavailable(
    tmp_path: Path,
) -> None:
    channel_id = uuid4()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            503,
            request=request,
            json={"detail": {"code": "state_unavailable", "message": "retry later"}},
        )

    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:8001/",
        access_token=COLLABORATION_TOKEN,
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)
    result = await registry.get("collaboration_report_submit").execute(
        {
            "operation": "submit",
            "idempotency_key": "collaboration-report-state-failure-0001",
            "report": collaboration_report_payload(),
        },
        LiliesToolContext(session_id=str(uuid4()), workspace=tmp_path),
    )
    assert result.is_error is True
    assert json.loads(result.content)["error"]["code"] == "state_unavailable"
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", f"/api/v1/collaboration/channels/{channel_id}")
    ]


@pytest.mark.asyncio
async def test_invalid_collaboration_tool_input_returns_bounded_public_paths(
    tmp_path: Path,
) -> None:
    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:8001/",
        access_token=COLLABORATION_TOKEN,
        channel_id=uuid4(),
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"validation must precede HTTP: {request.url}")
        ),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)

    result = await registry.get("collaboration_report_submit").execute(
        {
            "operation": "submit",
            "idempotency_key": "collaboration-report-invalid-diagnostic-0001",
            "report": {
                "category": "task_spec_gap",
                "phase": "acceptance",
                "severity": "minor",
                "summary": "Output binding did not resolve.",
            },
        },
        LiliesToolContext(session_id=str(uuid4()), workspace=tmp_path),
    )

    assert result.is_error is True
    body = json.loads(result.content)
    assert body["error"]["code"] == "invalid_collaboration_request"
    assert body["error"]["validation_error_count"] > 0
    paths = {item["path"] for item in body["error"]["validation_errors"]}
    assert {
        "report.report_id",
        "report.original_goal",
        "report.requirement_digest",
        "report.blocking_scope",
        "report.workaround_considered",
        "report.workaround_loss",
        "report.requested_outcome",
        "report.confidence",
        "report.secret_redactions",
    } <= paths
    assert all(
        set(item) == {"path", "type", "message"}
        for item in body["error"]["validation_errors"]
    )
    assert "collaboration-report-invalid-diagnostic-0001" not in result.content


@pytest.mark.asyncio
async def test_invalid_direct_report_explains_missing_common_evidence_without_http(
    tmp_path: Path,
) -> None:
    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:8001/",
        access_token=COLLABORATION_TOKEN,
        channel_id=uuid4(),
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"validation must precede HTTP: {request.url}")
        ),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)

    result = await registry.get("collaboration_report_submit").execute(
        {
            "operation": "submit",
            "idempotency_key": "collaboration-report-common-evidence-0001",
            "report": {
                "report_id": str(uuid4()),
                "category": "task_spec_gap",
                "phase": "acceptance",
                "severity": "minor",
                "summary": "Output binding did not resolve.",
                "original_goal": "Produce a verified reconciliation workflow.",
                "requirement_digest": CONTRACT_DIGEST,
                "blocking_scope": "The formal run cannot be archived as complete.",
                "workaround_considered": ["Retain the failed run without a claim."],
                "workaround_loss": "The enterprise outcome remains unverified.",
                "requested_outcome": "Clarify the required report evidence.",
                "confidence": 0.9,
                "secret_redactions": [],
            },
        },
        LiliesToolContext(session_id=str(uuid4()), workspace=tmp_path),
    )

    assert result.is_error is True
    body = json.loads(result.content)
    issues = body["error"]["validation_errors"]
    assert any(
        issue["path"] == "report"
        and issue["type"] == "value_error"
        and all(
            field in issue["message"]
            for field in (
                "attempted_routes",
                "expected",
                "actual",
                "evidence_refs",
            )
        )
        for issue in issues
    )
    assert "collaboration-report-common-evidence-0001" not in result.content


@pytest.mark.asyncio
async def test_reprobe_requires_contract_fetch_after_delivered_response_cursor(
    tmp_path: Path,
) -> None:
    service = service_at(tmp_path)
    await service.initialize()
    session = await service.storage.create_session(
        platform_contract_digest=CONTRACT_DIGEST
    )
    session_id = str(session["id"])
    reprobe_input = {
        "operation": "reprobe",
        "result": {"contract_digest": CONTRACT_DIGEST},
    }

    with pytest.raises(LiliesConflictError, match="after the developer response"):
        await service._require_refreshed_contract_for_reprobe(
            session_id,
            tool_name="collaboration_report_submit",
            tool_input=reprobe_input,
        )

    contract_result = LiliesToolResult(
        json.dumps(
            {
                "ok": True,
                "status_code": 200,
                "data": {"contract_digest": CONTRACT_DIGEST},
            }
        )
    )
    await service._persist_platform_contract_digest(
        session_id,
        tool_name="platform_contract_get",
        outcome=contract_result,
    )
    await service._require_refreshed_contract_for_reprobe(
        session_id,
        tool_name="collaboration_report_submit",
        tool_input=reprobe_input,
    )

    await service.storage.update_session_context(session_id, last_pipeline_cursor=5)
    with pytest.raises(LiliesConflictError, match="after the developer response"):
        await service._require_refreshed_contract_for_reprobe(
            session_id,
            tool_name="collaboration_report_submit",
            tool_input=reprobe_input,
        )
    await service._persist_platform_contract_digest(
        session_id,
        tool_name="platform_contract_get",
        outcome=contract_result,
    )
    await service._require_refreshed_contract_for_reprobe(
        session_id,
        tool_name="collaboration_report_submit",
        tool_input=reprobe_input,
    )
