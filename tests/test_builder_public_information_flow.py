from __future__ import annotations

import json
from uuid import UUID

from agent_platform.builder_public_information_flow import (
    project_builder_public_api_information_flow,
)
from agent_platform.platform_blackbox_auth import (
    BlackboxAuditEventType,
    BlackboxAuditRecord,
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
)


APPLICATION_ID = "514040c8-8a02-419a-ad49-25ccfdc5aadc"
ASSIGNMENT_ID = UUID("011b4844-08ab-5046-a8f3-ea89794da11b")
SESSION_ID = UUID("40353198-0de3-5e9e-831a-9430d88f6cec")
DIGEST = "sha256:" + "a" * 64


def _audit() -> BlackboxAuditRecord:
    return BlackboxAuditRecord.model_validate(
        {
            "seq": 7,
            "event_id": "68ae0c2c-8e85-4f99-9622-b464e14f83e0",
            "event_type": BlackboxAuditEventType.completed,
            "outcome": "completed",
            "credential_id": "7266e8f6-81b4-4bc8-a856-8ad3c0bd79c8",
            "authorization_id": "57f0ec75-ea60-460d-9790-69de718b0dcb",
            "assignment_id": ASSIGNMENT_ID,
            "session_id": SESSION_ID,
            "tool_call_id": "tool-call-0001",
            "request_id": "25305912-2f8a-4ec2-927b-c6389ebcaf22",
            "idempotency_key": "draft-inspect-0001",
            "application_id": APPLICATION_ID,
            "operation": PlatformBlackboxOperation.draft_inspect,
            "required_scope": PlatformBlackboxScope.draft_write,
            "contract_digest": DIGEST,
            "payload_digest": DIGEST,
            "details": {
                "status_code": 200,
                "response_digest": DIGEST,
                "api_key": "must-not-be-projected",
                "reasoning": "must-not-be-projected",
            },
            "created_at": "2026-07-26T01:02:03+00:00",
        }
    )


def test_public_information_flow_projects_only_bounded_summaries() -> None:
    platform_bearer = "lpt_" + "b" * 32 + "_" + "C" * 43
    projection = project_builder_public_api_information_flow(
        application_id=APPLICATION_ID,
        audit_records=[_audit()],
        application_response={
            "operation": "platform_application_get",
            "request_id": "application-read-0001",
            "data": {
                "id": APPLICATION_ID,
                "name": platform_bearer,
                "description": "authorization=Bearer must-not-leak",
                "status": "active",
                "published_version": 2,
                "evidence": {"state": "tested", "private_reasoning": "drop"},
                "updated_at": "2026-07-26T01:02:04+00:00",
            },
        },
        draft_response={
            "operation": "platform_draft_inspect",
            "request_id": "draft-read-0001",
            "data": {
                "application_id": APPLICATION_ID,
                "revision": 4,
                "content_hash": DIGEST,
                "tested_hash": DIGEST,
                "updated_at": "2026-07-26T01:02:05+00:00",
                "snapshot": {
                    "workflow": {
                        "nodes": [
                            {
                                "id": "model",
                                "type": "llm",
                                "config": {
                                    "system_prompt": "must-not-be-projected",
                                    "api_key": "must-not-be-projected",
                                },
                            }
                        ],
                        "edges": [],
                    },
                    "tests": [
                        {
                            "id": "private-test",
                            "expected": "must-not-be-projected",
                        }
                    ],
                    "raw_model_delta": "must-not-be-projected",
                },
            },
        },
        test_responses=[
            {
                "operation": "platform_tests_run",
                "request_id": "tests-run-0001",
                "data": {
                    "application_id": APPLICATION_ID,
                    "passed": False,
                    "summary": {"total": 2, "passed": 1, "failed": 1},
                    "results": [
                        {"actual": platform_bearer},
                        {"error": "must-not-be-projected"},
                    ],
                },
            }
        ],
        run_responses=[
            {
                "operation": "platform_run_get",
                "data": {
                    "id": "run-0001",
                    "application_id": APPLICATION_ID,
                    "status": "failed",
                    "draft_revision": 4,
                    "outputs": {"answer": platform_bearer},
                    "error": "authorization=Bearer must-not-leak",
                    "completed_node_ids": ["model"],
                    "skipped_node_ids": [],
                    "artifacts": [{"artifact_id": "artifact-1", "secret": "drop"}],
                    "updated_at": "2026-07-26T01:02:07+00:00",
                },
            }
        ],
        trace_responses=[
            {
                "operation": "platform_trace_get",
                "data": {
                    "run_id": "run-0001",
                    "redacted": True,
                    "next_after": 13,
                    "events": [
                        {
                            "seq": 10,
                            "type": "node.started",
                            "data": {
                                "node_id": "model",
                                "type": "llm",
                                "status": "running",
                                "raw_blocks": platform_bearer,
                                "reasoning": "must-not-be-projected",
                            },
                            "created_at": "2026-07-26T01:02:08+00:00",
                        },
                        {
                            "seq": 11,
                            "type": "model.text.delta",
                            "data": {"text": platform_bearer},
                            "created_at": "2026-07-26T01:02:09+00:00",
                        },
                        {
                            "seq": 12,
                            "type": "model.thinking.delta",
                            "data": {"thinking": "must-not-be-projected"},
                            "created_at": "2026-07-26T01:02:10+00:00",
                        },
                        {
                            "seq": 13,
                            "type": "workflow.failed",
                            "data": {
                                "status": "failed",
                                "error": platform_bearer,
                            },
                            "created_at": "2026-07-26T01:02:11+00:00",
                        },
                    ],
                },
            }
        ],
    )

    assert projection.redacted is True
    assert projection.assignment_id == str(ASSIGNMENT_ID)
    assert projection.session_id == str(SESSION_ID)
    assert projection.cursor.audit_after == 7
    assert projection.cursor.trace_after_by_run == {"run-0001": 13}
    by_type = {event.event_type: event for event in projection.events}
    assert {
        "request.completed",
        "application.summary",
        "application.draft.summary",
        "application.tests.summary",
        "application.run.summary",
        "node.started",
        "workflow.failed",
    } <= set(by_type)
    assert "model.text.delta" not in by_type
    assert "model.thinking.delta" not in by_type
    assert by_type["application.draft.summary"].details == {
        "revision": 4,
        "node_count": 1,
        "edge_count": 0,
        "test_count": 1,
        "content_hash": DIGEST,
        "tested_hash": DIGEST,
    }
    assert by_type["application.tests.summary"].details == {
        "total": 2,
        "passed": 1,
        "failed": 1,
    }
    assert by_type["application.run.summary"].details["artifact_count"] == 1
    assert by_type["node.started"].details == {
        "node_id": "model",
        "status": "running",
        "type": "llm",
    }
    encoded = projection.model_dump_json()
    assert platform_bearer not in encoded
    assert "must-not-be-projected" not in encoded
    assert "raw_blocks" not in encoded
    assert "reasoning" not in encoded
    assert "system_prompt" not in encoded
    assert "api_key" not in encoded


def test_projection_is_read_only_deduplicated_and_cross_application_scoped() -> None:
    audit = _audit().model_dump(mode="json", exclude_none=True)
    audit_copy = json.loads(json.dumps(audit))
    run = {
        "data": {
            "id": "run-0002",
            "application_id": APPLICATION_ID,
            "status": "succeeded",
        }
    }
    trace = {
        "data": {
            "run_id": "run-0002",
            "next_after": 2,
            "events": [
                {
                    "seq": 2,
                    "type": "node.completed",
                    "data": {"node_id": "end", "status": "completed"},
                },
                {
                    "seq": 2,
                    "type": "node.completed",
                    "data": {"node_id": "end", "status": "completed"},
                },
            ],
        }
    }
    projection = project_builder_public_api_information_flow(
        application_id=APPLICATION_ID,
        audit_records=[
            audit,
            audit,
            {
                **audit,
                "seq": 8,
                "event_id": "58db61f4-494e-43ba-ae15-26678622d552",
                "application_id": "other-application",
            },
        ],
        run_responses=[run],
        trace_responses=[trace],
    )

    assert audit == audit_copy
    event_ids = [event.event_id for event in projection.events]
    assert len(event_ids) == len(set(event_ids)) == 3
    assert all(event.application_id == APPLICATION_ID for event in projection.events)
    assert {event.kind for event in projection.events} == {
        "authorization",
        "run",
        "trace",
    }
    assert not hasattr(projection, "messages")
