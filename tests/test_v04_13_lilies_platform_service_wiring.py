from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import BuildAssignment, ProhibitedAction
from agent_platform.lilies_platform_client import (
    LiliesPlatformClient,
    PlatformToolEnvelope,
)
from agent_platform.lilies_platform_tools import (
    PLATFORM_MODEL_RESULT_SAFE_CHARS,
    PlatformHttpTool,
)
from agent_platform.lilies_service import LiliesServiceError, LocalLiliesService
from agent_platform.lilies_storage import LiliesAuthenticationError
from agent_platform.lilies_tools import LiliesToolResult
from tests.test_v04_13_lilies_service import (
    ScriptedLocalProvider,
    create_session,
    paired_service,
    send,
    wait_for_status,
)


CONTRACT_DIGEST = "sha256:" + "a" * 64
PLATFORM_TOKEN = "private-platform-task-token-value"


def _assignment(*, assignment_id: str, credential_ref: str) -> BuildAssignment:
    created_at = datetime.now(timezone.utc)
    return BuildAssignment.model_validate(
        {
            "schema_version": "1.0",
            "assignment_id": assignment_id,
            "idempotency_key": "assignment-wiring-0001",
            "mode": "customer",
            "requirement": "Build and inspect the assigned enterprise workflow through HTTP.",
            "business_context": {
                "customer_roles": ["operations manager"],
                "business_goal": "Create a bounded workflow from the public contract.",
                "inputs": ["customer requirement"],
                "outputs": ["working application"],
                "constraints": ["platform remains a black box"],
            },
            "target": {"mode": "create_new"},
            "platform": {
                "base_url": "http://127.0.0.1:18999",
                "contract_url": "/api/v1/lilies/platform-contract",
                "contract_digest": CONTRACT_DIGEST,
                "credential_ref": credential_ref,
                "scopes": [
                    "workflow.catalog:read",
                    "workflow.application:write",
                ],
                "application_ids": [],
            },
            "constraints": {
                "deadline_at": (created_at + timedelta(hours=1)).isoformat(),
                "max_turns": 30,
                "max_tool_calls": 100,
                "network_policy": "allowlist",
                "allowed_hosts": ["127.0.0.1"],
                "allowed_actions": [
                    "platform_contract_get",
                    "platform_application_create",
                ],
                "prohibited_actions": [action.value for action in ProhibitedAction],
                "no_substitute_validation": False,
            },
            "deliverables": [
                {
                    "name": "workflow application",
                    "description": "The assigned runnable application.",
                    "media_type": "application/json",
                }
            ],
            "created_at": created_at.isoformat(),
        }
    )


@pytest.mark.asyncio
async def test_platform_tools_are_resolved_per_assignment_from_private_credential(
    tmp_path: Path,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", workspace_root=tmp_path / "workspaces"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    ordinary_session = await service.storage.create_session()
    ordinary_tools = await service.tool_registry_for_session(ordinary_session["id"])
    assert all(not name.startswith("platform_") for name in ordinary_tools.names())

    assignment_id = str(uuid4())
    session_id = str(uuid4())
    credential_ref = "credential:platform-wiring-1"
    assignment = _assignment(
        assignment_id=assignment_id,
        credential_ref=credential_ref,
    )
    await service.storage.provision_credential(
        "platform_assignment",
        PLATFORM_TOKEN,
        scopes=[scope.value for scope in assignment.platform.scopes],
        credential_ref=credential_ref,
        assignment_id=assignment_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await service.storage.create_session(
        session_id=session_id,
        assignment_id=assignment_id,
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=CONTRACT_DIGEST,
    )

    assigned_tools = await service.tool_registry_for_session(session_id)
    assert set(assigned_tools.names()) == {
        "platform_contract_get",
        "platform_application_create",
    }
    assert "platform_publish" not in assigned_tools.names()
    contract_tool = assigned_tools.get("platform_contract_get")
    assert contract_tool.client.require_contract_fetch is True
    assert contract_tool.client.assignment_id.hex == assignment_id.replace("-", "")
    assert str(contract_tool.client.session_id) == session_id
    assert PLATFORM_TOKEN not in repr(contract_tool.client)

    refreshed_digest = "sha256:" + "b" * 64
    await service._persist_platform_contract_digest(  # noqa: SLF001 - restart invariant
        session_id,
        tool_name="platform_contract_get",
        outcome=LiliesToolResult(
            json.dumps({"data": {"contract_digest": refreshed_digest}})
        ),
    )
    restarted = LocalLiliesService(service.settings, provider=ScriptedLocalProvider())
    await restarted.initialize()
    restarted_tools = await restarted.tool_registry_for_session(session_id)
    restarted_contract_tool = restarted_tools.get("platform_contract_get")
    assert restarted_contract_tool.client.contract_digest == refreshed_digest
    persisted = await restarted.storage.get_session(session_id)
    assert persisted["platform_contract_digest"] == refreshed_digest
    assert persisted["assignment"]["platform"]["contract_digest"] == CONTRACT_DIGEST

    await service.storage.revoke_credential(
        credential_ref,
        idempotency_key="revoke-platform-wiring-0001",
        reason="test revocation",
    )
    with pytest.raises(LiliesAuthenticationError, match="revoked"):
        await service.tool_registry_for_session(session_id)


@pytest.mark.asyncio
async def test_assignment_tool_resolution_rejects_scope_and_network_mismatch(
    tmp_path: Path,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", workspace_root=tmp_path / "workspaces"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    assignment_id = str(uuid4())
    session_id = str(uuid4())
    credential_ref = "credential:platform-wiring-2"
    assignment = _assignment(
        assignment_id=assignment_id,
        credential_ref=credential_ref,
    )
    await service.storage.provision_credential(
        "platform_assignment",
        PLATFORM_TOKEN,
        scopes=["workflow.catalog:read"],
        credential_ref=credential_ref,
        assignment_id=assignment_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await service.storage.create_session(
        session_id=session_id,
        assignment_id=assignment_id,
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=CONTRACT_DIGEST,
    )
    with pytest.raises(LiliesServiceError, match="credential scopes do not match"):
        await service.tool_registry_for_session(session_id)

    denied_payload = assignment.model_dump(mode="json", exclude_none=True)
    denied_constraints = denied_payload["constraints"]
    assert isinstance(denied_constraints, dict)
    denied_constraints["network_policy"] = "none"
    denied_constraints["allowed_hosts"] = []
    denied = BuildAssignment.model_validate(denied_payload)
    await service.storage.update_session_context(
        session_id,
        assignment=denied.model_dump(mode="json", exclude_none=True),
    )
    with pytest.raises(LiliesServiceError, match="network policy denies"):
        await service.tool_registry_for_session(session_id)


@pytest.mark.asyncio
async def test_agent_loop_receives_platform_envelope_for_invalid_model_tool_input(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider(
        tool="platform_application_create",
        tool_input={"name": "Invalid model call", "unexpected": True},
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    assignment_id = str(uuid4())
    credential_ref = "credential:platform-agent-loop-invalid"
    assignment = _assignment(
        assignment_id=assignment_id,
        credential_ref=credential_ref,
    )
    await service.storage.provision_credential(
        "platform_assignment",
        PLATFORM_TOKEN,
        scopes=[scope.value for scope in assignment.platform.scopes],
        credential_ref=credential_ref,
        assignment_id=assignment_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await service.storage.update_session_context(
        session_id,
        assignment_id=assignment_id,
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=CONTRACT_DIGEST,
    )

    await send(service, client_id, session_id, "Make an invalid platform call.")
    await wait_for_status(service, session_id, "ready")

    messages = await service.storage.list_messages(session_id, client_id=client_id)
    tool_message = next(item for item in messages if item["role"] == "tool")
    envelope = json.loads(tool_message["content"][0]["content"])
    assert envelope["ok"] is False
    assert envelope["operation"] == "platform_application_create"
    assert envelope["status_code"] == 422
    assert envelope["error"]["code"] == "invalid_request"
    assert envelope["error"]["failure_owner"] == "task_author"
    assert envelope["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_agent_loop_receives_complete_local_envelope_for_oversized_platform_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversized_invoke(
        client: LiliesPlatformClient,
        operation: str,
        payload: dict[str, object],
        *,
        tool_call_id: str,
        idempotency_key: str | None = None,
        allow_missing_contract: bool = False,
    ) -> PlatformToolEnvelope:
        assert client.contract_digest == CONTRACT_DIGEST
        assert operation == "platform_application_create"
        assert payload["name"] == "Oversized result"
        assert tool_call_id == "tool-call-1"
        assert idempotency_key == "oversized-platform-result-0001"
        assert allow_missing_contract is False
        return PlatformToolEnvelope(
            ok=True,
            operation=operation,
            request_id=uuid4(),
            status_code=201,
            contract_digest=CONTRACT_DIGEST,
            data={"synthetic": "x" * (PLATFORM_MODEL_RESULT_SAFE_CHARS + 10_000)},
            error=None,
            evidence_refs=[],
        )

    monkeypatch.setattr(LiliesPlatformClient, "invoke", oversized_invoke)
    provider = ScriptedLocalProvider(
        tool="platform_application_create",
        tool_input={
            "name": "Oversized result",
            "idempotency_key": "oversized-platform-result-0001",
        },
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    assignment_id = str(uuid4())
    credential_ref = "credential:platform-agent-loop-oversized"
    assignment = _assignment(
        assignment_id=assignment_id,
        credential_ref=credential_ref,
    )
    await service.storage.provision_credential(
        "platform_assignment",
        PLATFORM_TOKEN,
        scopes=[scope.value for scope in assignment.platform.scopes],
        credential_ref=credential_ref,
        assignment_id=assignment_id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await service.storage.update_session_context(
        session_id,
        assignment_id=assignment_id,
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=CONTRACT_DIGEST,
    )

    await send(service, client_id, session_id, "Request an oversized platform result.")
    await wait_for_status(service, session_id, "ready")

    messages = await service.storage.list_messages(session_id, client_id=client_id)
    stored_content = next(
        item["content"][0]["content"] for item in messages if item["role"] == "tool"
    )
    assert len(stored_content) < PlatformHttpTool.max_result_chars
    envelope = json.loads(stored_content)
    assert envelope["ok"] is False
    assert envelope["operation"] == "platform_application_create"
    assert envelope["status_code"] == 502
    assert envelope["error"]["code"] == "platform_result_too_large"
    assert envelope["error"]["failure_owner"] == "platform"
    assert envelope["error"]["actual"]["serialized_chars"] > (
        PLATFORM_MODEL_RESULT_SAFE_CHARS
    )

    assert provider.calls == 2
    model_content = next(
        block.content
        for message in provider.seen_messages[1]
        for block in message.content
        if block.type == "tool_result"
    )
    assert model_content == stored_content
    assert json.loads(model_content)["error"]["code"] == "platform_result_too_large"
