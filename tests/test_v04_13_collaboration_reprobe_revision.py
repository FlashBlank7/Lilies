from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from agent_platform.lilies_collaboration_client import LiliesCollaborationClient
from agent_platform.lilies_collaboration_tools import CollaborationReportSubmitInput
from scripts.run_v04_13_codex_builder_child import _public_api_manual


def _developer_response_event(
    *,
    channel_id: UUID,
    report_id: UUID,
    report_revision: int,
    seq: int = 7,
    message_id: UUID | None = None,
    sender_role: str = "codex",
    message_type: str = "developer_response",
    payload_schema: str = "collaboration.developer_response.v1",
    correlation_id: UUID | None = None,
    payload_report_id: UUID | None = None,
    payload_channel_id: UUID | None = None,
) -> dict[str, Any]:
    """Return the strict public event wire shape emitted by collaboration events."""

    return {
        "schema_version": "1.0",
        "message_id": str(message_id or uuid4()),
        "channel_id": str(channel_id),
        "seq": seq,
        "message_type": message_type,
        "sender_role": sender_role,
        "sender_id": "codex-developer",
        "correlation_id": str(correlation_id or report_id),
        "idempotency_key": f"developer.response.{seq:04d}",
        "visibility": "user_and_lilies",
        "payload_schema": payload_schema,
        "payload": {
            "schema_version": "1.0",
            "response_id": str(uuid4()),
            "outcome": "implemented",
            "commit_sha": "a" * 40,
            "generic_capability_changes": ["Added a generic collaboration repair."],
            "new_contract_digest": "sha256:" + "b" * 64,
            "tests_run": [
                {
                    "run_id": "focused-reprobe-revision",
                    "command": "pytest focused",
                    "exit_code": 0,
                    "summary": "focused regression passed",
                    "evidence_ref": {
                        "evidence_id": "focused-reprobe-revision",
                        "kind": "test_run",
                        "digest": "sha256:" + "c" * 64,
                        "media_type": "application/json",
                        "label": "focused reprobe revision regression",
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ],
            "reprobe_steps": [
                {
                    "order": 1,
                    "action": "Rerun the same public collaboration reprobe.",
                    "expected": "The reprobe is accepted at the resulting revision.",
                }
            ],
            "channel_id": str(payload_channel_id or channel_id),
            "report_id": str(payload_report_id or report_id),
            "report_revision": report_revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "evidence_refs": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_reprobe_uses_resulting_revision_from_exact_developer_response() -> None:
    channel_id = uuid4()
    report_id = uuid4()
    event = _developer_response_event(
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
    )
    posted: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "channel_id": str(channel_id),
                    "after": 0,
                    "next_cursor": 7,
                    "events": [event],
                },
            )
        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "reprobe_id": str(uuid4()),
                "report_id": str(report_id),
                "report_revision": 6,
            },
        )

    client = LiliesCollaborationClient(
        base_url="http://collaboration.test",
        access_token="private-test-token",
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    updates = await client.read_updates(after=0)
    assert updates.ok is True
    assert updates.data["client_report_revision_transitions"] == [
        {
            "schema_version": "1.0",
            "report_id": str(report_id),
            "source_message_id": event["message_id"],
            "source_event_seq": 7,
            "consumed_report_revision": 5,
            "resulting_report_revision": 6,
            "reprobe_expected_report_revision": 6,
            "derivation": "developer_response_v1_atomic_increment",
        }
    ]

    caller_payload = {
        "idempotency_key": "reprobe-revision-regression-0001",
        "expected_report_revision": 5,
        "result": {"schema_version": "1.0"},
    }
    result = await client.submit_reprobe(report_id, caller_payload)

    assert caller_payload["expected_report_revision"] == 5
    assert posted == [
        {
            **caller_payload,
            "expected_report_revision": 6,
        }
    ]
    assert result.data["client_report_revision_resolution"] == {
        **updates.data["client_report_revision_transitions"][0],
        "requested_report_revision": 5,
        "effective_report_revision": 6,
    }


@pytest.mark.parametrize("failure_mode", ["conflict", "transport_unavailable"])
@pytest.mark.asyncio
async def test_failed_reprobe_does_not_report_revision_mapping_as_accepted(
    failure_mode: str,
) -> None:
    channel_id = uuid4()
    report_id = uuid4()
    event = _developer_response_event(
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
    )
    posted: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "channel_id": str(channel_id),
                    "after": 0,
                    "next_cursor": 7,
                    "events": [event],
                },
            )
        posted.append(json.loads(request.content))
        if failure_mode == "transport_unavailable":
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "report_revision_conflict",
                    "message": "the report revision changed",
                    "retryable": False,
                }
            },
        )

    client = LiliesCollaborationClient(
        base_url="http://collaboration.test",
        access_token="private-test-token",
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    updates = await client.read_updates(after=0)
    assert updates.ok is True

    result = await client.submit_reprobe(
        report_id,
        {
            "idempotency_key": f"reprobe-failed-{failure_mode}-0001",
            "expected_report_revision": 5,
            "result": {"schema_version": "1.0"},
        },
    )

    assert result.ok is False
    assert result.data == {}
    assert "client_report_revision_resolution" not in result.data
    assert posted[0]["expected_report_revision"] == 6


@pytest.mark.asyncio
async def test_reprobe_never_increments_unbound_or_untrusted_revisions() -> None:
    channel_id = uuid4()
    report_id = uuid4()
    other_report_id = uuid4()
    events = [
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=5,
            seq=7,
        ),
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=40,
            seq=8,
            sender_role="lilies",
        ),
        _developer_response_event(
            channel_id=uuid4(),
            report_id=report_id,
            report_revision=50,
            seq=9,
        ),
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=55,
            seq=10,
            payload_channel_id=uuid4(),
        ),
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=60,
            seq=11,
            message_type="control",
        ),
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=70,
            seq=12,
            payload_schema="collaboration.control.v1",
        ),
        _developer_response_event(
            channel_id=channel_id,
            report_id=report_id,
            report_revision=80,
            seq=13,
            correlation_id=other_report_id,
        ),
    ]
    posted: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "channel_id": str(channel_id),
                    "after": 0,
                    "next_cursor": 13,
                    "events": events,
                },
            )
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    client = LiliesCollaborationClient(
        base_url="http://collaboration.test",
        access_token="private-test-token",
        channel_id=channel_id,
        transport=httpx.MockTransport(handler),
    )
    updates = await client.read_updates(after=0)
    transitions = updates.data["client_report_revision_transitions"]
    assert len(transitions) == 1
    assert transitions[0]["report_id"] == str(report_id)
    assert transitions[0]["resulting_report_revision"] == 6

    await client.submit_reprobe(
        other_report_id,
        {
            "idempotency_key": "reprobe-other-report-0001",
            "expected_report_revision": 5,
        },
    )
    await client.submit_reprobe(
        report_id,
        {
            "idempotency_key": "reprobe-arbitrary-revision-0001",
            "expected_report_revision": 4,
        },
    )
    await client.submit_reprobe(
        report_id,
        {
            "idempotency_key": "reprobe-already-resulting-0001",
            "expected_report_revision": 6,
        },
    )

    assert [item["expected_report_revision"] for item in posted] == [5, 4, 6]


def test_tool_schema_and_public_manual_define_the_atomic_revision_transition() -> None:
    schema = CollaborationReportSubmitInput.model_json_schema()
    description = schema["properties"]["expected_report_revision"]["description"]
    assert "developer_response.v1" in description
    assert "never increments arbitrary revisions" in description

    collaboration = _public_api_manual()["collaboration"]
    transition = collaboration["developer_response_revision_transition"]
    assert transition == {
        "applies_only_when": (
            "the latest same-report event has payload_schema="
            "collaboration.developer_response.v1, message_type="
            "developer_response, and sender_role=codex"
        ),
        "consumed_report_revision": "event.payload.report_revision",
        "resulting_report_revision": (
            "event.payload.report_revision + 1; developer response persistence "
            "atomically performs exactly this one report transition"
        ),
        "reprobe_expected_report_revision": "resulting_report_revision",
        "guard": (
            "Do not increment an arbitrary report revision, do not subtract from "
            "the developer response revision, and require event.correlation_id="
            "event.payload.report_id for the same reprobed report."
        ),
    }
    expected_revision = collaboration["reprobe_request_schema"]["properties"][
        "expected_report_revision"
    ]
    assert "resulting/current revision" in expected_revision
    assert "do not reuse its consumed" in expected_revision
