from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import (
    AllowedAction,
    BuildAssignment,
    PlatformScope,
    ProhibitedAction,
    SessionMessageRequest,
)
from agent_platform.lilies_platform_client import LiliesPlatformClient
from agent_platform.lilies_service import LocalLiliesService
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class HttpBuildProvider(ModelProvider):
    name = "scripted-http-build"

    def __init__(self, initial_digest: str) -> None:
        self.phase = 0
        self.pending: str | None = None
        self.calls = 0
        self.application_id: str | None = None
        self.revision = 0
        self.failed_run_id: str | None = None
        self.run_id: str | None = None
        self.run_status: str | None = None
        self.outputs: dict[str, Any] = {}
        self.contract_digests = [initial_digest]
        self.operation_history: list[str] = []
        self.visible_platform_tools: set[str] = set()

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, False, False, False, 100_000, 10_000)

    @staticmethod
    def _last_envelope(messages: list[ChatMessage]) -> dict[str, Any]:
        for message in reversed(messages):
            for block in reversed(message.content):
                if block.type != "tool_result":
                    continue
                if not isinstance(block.content, str):
                    raise RuntimeError("tool result content is not text")
                return json.loads(block.content)
        raise RuntimeError("expected a prior tool result")

    def _absorb_result(self, messages: list[ChatMessage]) -> None:
        if self.pending is None:
            return
        envelope = self._last_envelope(messages)
        if envelope.get("operation") != self.pending:
            raise RuntimeError("tool result operation correlation failed")
        if not envelope.get("ok"):
            code = (envelope.get("error") or {}).get("code", "unknown")
            raise RuntimeError(f"platform operation failed: {self.pending}:{code}")
        data = envelope["data"]
        self.operation_history.append(self.pending)
        if self.pending == "platform_contract_get":
            digest = data["contract_digest"]
            if digest != self.contract_digests[-1]:
                self.contract_digests.append(digest)
        elif self.pending == "platform_application_create":
            self.application_id = data["id"]
        elif self.pending == "platform_draft_apply":
            self.revision = data["revision"]
        elif self.pending == "platform_draft_inspect":
            reference = data["snapshot"]["workflow"]["nodes"][1]["config"]["variables"][
                "name"
            ]["$ref"]
            if reference["path"] != ["name"]:
                raise RuntimeError("public draft projection destroyed the reference path")
        elif self.pending == "platform_tests_run":
            if self.phase == 14:
                if data["passed"] is not False:
                    raise RuntimeError("deliberately failing acceptance unexpectedly passed")
                self.failed_run_id = data["tests"][0]["run_id"]
            elif self.phase == 17 and data["passed"] is not True:
                raise RuntimeError("acceptance remained failed after one repair")
        elif self.pending == "platform_publish":
            if data["version"] != 1:
                raise RuntimeError("unexpected published version")
        elif self.pending == "platform_run_start":
            self.run_id = data["run_id"]
        elif self.pending == "platform_run_get":
            self.run_status = data["status"]
            self.outputs = data.get("outputs", {})
            if self.run_status in {"succeeded", "failed", "cancelled"}:
                self.phase = 22
        self.pending = None

    def _next_tool(self) -> tuple[str, dict[str, Any]] | None:
        application_id = self.application_id
        if self.phase == 0:
            result = ("platform_contract_get", {})
        elif self.phase == 1:
            result = ("platform_block_search", {"query": "template"})
        elif self.phase == 2:
            result = ("platform_block_get", {"block_type": "template_transform"})
        elif self.phase == 3:
            result = ("platform_tool_catalog", {})
        elif self.phase == 4:
            result = (
                "platform_application_create",
                {
                    "name": "Agent-loop HTTP repair",
                    "requirement": "Build, fail, repair, test, publish, and run a greeting.",
                    "idempotency_key": "agent-application-create-0001",
                },
            )
        elif self.phase == 5:
            result = ("platform_application_get", {"application_id": application_id})
        elif self.phase == 6:
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": "agent-draft-add-start-0001",
                    "op": "add_node",
                    "data": {
                        "node": {
                            "id": "start",
                            "type": "start",
                            "title": "Input",
                            "config": {"inputs": [{"name": "name", "type": "string"}]},
                        }
                    },
                },
            )
        elif self.phase == 7:
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": "agent-draft-add-template-0001",
                    "op": "add_node",
                    "data": {
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
                },
            )
        elif self.phase == 8:
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": "agent-draft-add-end-0001",
                    "op": "add_node",
                    "data": {
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
                },
            )
        elif self.phase in {9, 10}:
            source, target, source_port = (
                ("start", "template", "output")
                if self.phase == 9
                else ("template", "end", "text")
            )
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": f"agent-draft-edge-{self.phase:04d}",
                    "op": "add_edge",
                    "data": {
                        "edge": {
                            "id": f"{source}-{target}",
                            "source": source,
                            "target": target,
                            "source_port": source_port,
                            "target_port": "input",
                        }
                    },
                },
            )
        elif self.phase == 11:
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": "agent-draft-add-test-0001",
                    "op": "add_test",
                    "data": {
                        "test": {
                            "id": "greeting-case",
                            "name": "Greeting acceptance",
                            "requirement": "Return the exact greeting.",
                            "inputs": {"name": "Ada"},
                            "assertions": [
                                {
                                    "path": ["greeting"],
                                    "operator": "equals",
                                    "expected": "Hello Ada",
                                }
                            ],
                            "mandatory": True,
                        }
                    },
                },
            )
        elif self.phase == 12:
            result = ("platform_draft_inspect", {"application_id": application_id})
        elif self.phase == 13:
            result = (
                "platform_tests_run",
                {
                    "application_id": application_id,
                    "idempotency_key": "agent-tests-failing-0001",
                },
            )
        elif self.phase == 14:
            result = ("platform_trace_get", {"run_id": self.failed_run_id})
        elif self.phase == 15:
            result = (
                "platform_draft_apply",
                {
                    "application_id": application_id,
                    "expected_revision": self.revision,
                    "idempotency_key": "agent-draft-repair-0001",
                    "op": "update_node",
                    "data": {
                        "node_id": "template",
                        "changes": {"config": {"template": "Hello {{ name }}"}},
                    },
                },
            )
        elif self.phase == 16:
            result = (
                "platform_tests_run",
                {
                    "application_id": application_id,
                    "idempotency_key": "agent-tests-passing-0001",
                },
            )
        elif self.phase == 17:
            result = (
                "platform_publish",
                {
                    "application_id": application_id,
                    "acknowledge_warnings": True,
                    "idempotency_key": "agent-publish-version-0001",
                },
            )
        elif self.phase == 18:
            result = ("platform_contract_get", {})
        elif self.phase == 19:
            result = (
                "platform_run_start",
                {
                    "application_id": application_id,
                    "inputs": {"name": "Ada"},
                    "idempotency_key": "agent-business-run-0001",
                },
            )
        elif self.phase in {20, 21}:
            result = ("platform_run_get", {"run_id": self.run_id})
            self.phase = 21
            return result
        elif self.phase == 22:
            result = ("platform_trace_get", {"run_id": self.run_id})
        else:
            return None
        self.phase += 1
        return result

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.visible_platform_tools.update(
            definition.name for definition in tools if definition.name.startswith("platform_")
        )
        self._absorb_result(messages)
        action = self._next_tool()
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 10}}},
        )
        if action is None:
            yield StreamEvent(
                type="content_block_start",
                data={"index": 0, "content_block": {"type": "text", "text": ""}},
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": "HTTP-only workflow delivery completed.",
                    },
                },
            )
            stop_reason = "end_turn"
        else:
            name, payload = action
            self.pending = name
            tool_call_id = f"agent-tool-{self.calls:04d}"
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_call_id,
                        "name": name,
                        "input": {},
                    },
                },
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    },
                },
            )
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            stop_reason = "tool_use"
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
        )


async def main() -> dict[str, Any]:
    base_url = os.environ["LILIES_TEST_BASE_URL"]
    token = os.environ["LILIES_TEST_TOKEN"]
    assignment_id = UUID(os.environ["LILIES_TEST_ASSIGNMENT_ID"])
    session_id = UUID(os.environ["LILIES_TEST_SESSION_ID"])
    bootstrap = LiliesPlatformClient(
        base_url=base_url,
        access_token=token,
        assignment_id=assignment_id,
        session_id=session_id,
    )
    contract_result = await bootstrap.contract_get(
        tool_call_id="agent-bootstrap-contract",
        idempotency_key="agent-bootstrap-contract-0001",
    )
    if not contract_result.ok:
        raise RuntimeError("platform contract bootstrap failed")
    contract_digest = contract_result.data["contract_digest"]

    settings = LiliesSettings(
        data_dir=os.environ["LILIES_TEST_LOCAL_DATA"],
        workspace_root=os.environ["LILIES_TEST_LOCAL_WORKSPACE"],
        model="scripted-http-build",
        default_max_turns=80,
        default_max_tool_calls=120,
    )
    provider = HttpBuildProvider(contract_digest)
    service = LocalLiliesService(settings, provider=provider)
    await service.initialize()
    credential_ref = "credential:agent-process-platform"
    scopes = list(PlatformScope)
    await service.storage.provision_credential(
        "platform_assignment",
        token,
        scopes=[scope.value for scope in scopes],
        credential_ref=credential_ref,
        assignment_id=str(assignment_id),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    pairing = await service.storage.create_pairing_code(
        allowed_scopes=["lilies.session:read", "lilies.session:write"]
    )
    client = await service.storage.exchange_pairing_code(
        pairing["pairing_code"],
        "platform",
        ["lilies.session:read", "lilies.session:write"],
        f"agent-process-nonce-{uuid4().hex}",
        settings.daemon_fingerprint(),
    )
    created_at = datetime.now(timezone.utc)
    platform_host = urlsplit(base_url).hostname
    if not platform_host:
        raise RuntimeError("platform host is missing")
    assignment = BuildAssignment.model_validate(
        {
            "schema_version": "1.0",
            "assignment_id": str(assignment_id),
            "idempotency_key": "agent-process-assignment-0001",
            "mode": "customer",
            "requirement": "Build, test, repair, publish, and run the assigned greeting workflow.",
            "business_context": {
                "customer_roles": ["operations manager"],
                "business_goal": "Deliver an exact greeting from a reusable workflow.",
                "inputs": ["customer name"],
                "outputs": ["exact greeting"],
                "constraints": ["use only the public platform contract"],
            },
            "target": {"mode": "create_new"},
            "platform": {
                "base_url": base_url,
                "contract_url": "/api/v1/lilies/platform-contract",
                "contract_digest": contract_digest,
                "credential_ref": credential_ref,
                "scopes": [scope.value for scope in scopes],
                "application_ids": [],
            },
            "constraints": {
                "deadline_at": (created_at + timedelta(minutes=10)).isoformat(),
                "max_turns": 80,
                "max_budget_usd": 5,
                "max_tool_calls": 120,
                "network_policy": "allowlist",
                "allowed_hosts": [platform_host],
                "allowed_actions": [action.value for action in AllowedAction],
                "prohibited_actions": [action.value for action in ProhibitedAction],
                "no_substitute_validation": False,
            },
            "deliverables": [
                {
                    "name": "greeting workflow",
                    "description": "Published runnable workflow and verified output.",
                    "media_type": "application/json",
                }
            ],
            "created_at": created_at.isoformat(),
        }
    )
    await service.storage.create_session(
        session_id=str(session_id),
        agent_version=settings.agent_version,
        model_profile=settings.model,
        system_identity_version=settings.system_identity_version,
        config={
            "kind": "platform",
            "max_turns": 80,
            "max_model_calls": 80,
            "max_tool_calls": 120,
            "max_tokens": 1_000_000,
            "max_budget_usd": 5,
            "deadline_at": assignment.constraints.deadline_at.isoformat(),
        },
        profile={"model": settings.model},
        client_id=client["client_id"],
        assignment_id=str(assignment_id),
        assignment=assignment.model_dump(mode="json", exclude_none=True),
        platform_contract_digest=contract_digest,
    )
    turn = await service.submit_message(
        str(session_id),
        SessionMessageRequest(
            idempotency_key="agent-process-message-0001",
            message_id=uuid4(),
            content=(
                "Deliver the assignment now. Read the contract first, use incremental draft "
                "operations, execute the acceptance test, repair its first failure, publish, "
                "and prove the business output."
            ),
        ),
        client_id=client["client_id"],
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = await service.storage.get_turn(turn["id"])
        if current["status"] not in {
            "running",
            "waiting_permission",
            "waiting_collaboration",
        }:
            break
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("local Lilies turn timed out")
    current = await service.storage.get_turn(turn["id"])
    session = await service.storage.get_session(str(session_id))
    events = await service.storage.list_events(str(session_id), after=0, limit=5_000)
    result = {
        "client_pid": os.getpid(),
        "turn_status": current["status"],
        "session_status": session["status"],
        "application_id": provider.application_id,
        "draft_revision": provider.revision,
        "failed_run_id": provider.failed_run_id,
        "run_id": provider.run_id,
        "run_status": provider.run_status,
        "outputs": provider.outputs,
        "contract_changed_after_publish": len(provider.contract_digests) == 2,
        "persisted_contract_digest": session["platform_contract_digest"],
        "model_calls": provider.calls,
        "platform_tool_count": len(provider.visible_platform_tools),
        "platform_operations": provider.operation_history,
        "platform_tool_events": len(
            [
                event
                for event in events
                if event["event_type"] in {"tool.started", "tool.completed"}
                and str(event["data"].get("tool", "")).startswith("platform_")
            ]
        ),
    }
    await service.shutdown(reason="agent_process_test_complete")
    return result


print(json.dumps(asyncio.run(main()), ensure_ascii=False, sort_keys=True))
