from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_platform.lilies_config import LiliesSettings
from agent_platform.collaboration_models import CollaborationReportPayload
from agent_platform.lilies_service import LocalLiliesService
from tests.test_v04_13_lilies_service import ScriptedLocalProvider


def _message(value: object) -> dict[str, object]:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Durable collaboration updates for the current formal task:\n"
                    + json.dumps(value, sort_keys=True)
                ),
            }
        ],
    }


def _session() -> dict[str, object]:
    return {
        "platform_contract_digest": "sha256:" + "a" * 64,
        "last_platform_cursor": 9,
        "last_pipeline_cursor": 17,
        "assignment": {"constraints": {"no_substitute_validation": True}},
    }


def test_compaction_preserves_claim_verdict_revision_and_bound_differences() -> None:
    claim_id = str(uuid4())
    evidence_id = "evidence:oracle-difference-0001"
    messages = [
        _message(
            {
                "message_type": "verification_claim",
                "correlation_id": claim_id,
                "payload": {
                    "claim_id": claim_id,
                    "status": "frozen",
                    "claim_revision": 1,
                    "application_id": str(uuid4()),
                    "draft_revision": 12,
                    "content_hash": "sha256:" + "b" * 64,
                    "test_run_ids": ["test-run:acceptance-0001"],
                    "business_run_ids": ["business-run:seed-0001"],
                },
            }
        ),
        _message(
            {
                "message_type": "verification_result",
                "payload_schema": "collaboration.verification_result.v1",
                "correlation_id": claim_id,
                "payload": {
                    "claim_id": claim_id,
                    "claim_revision": 1,
                    "verification_id": str(uuid4()),
                    "verdict": "verification_failed",
                    "oracle_digest": "sha256:" + "c" * 64,
                    "differences": [
                        {
                            "check_id": "oracle-check:record-17",
                            "expected": "record should remain pending human review",
                            "actual": "record was written to the host system",
                            "evidence_refs": [
                                {
                                    "evidence_id": evidence_id,
                                    "kind": "run",
                                    "digest": "sha256:" + "d" * 64,
                                    "label": "hidden oracle mismatch",
                                }
                            ],
                        }
                    ],
                    "evidence_refs": [
                        {
                            "evidence_id": "evidence:oracle-summary-0001",
                            "kind": "run",
                            "digest": "sha256:" + "e" * 64,
                            "label": "oracle summary",
                        }
                    ],
                },
            }
        ),
    ]

    result = LocalLiliesService._compaction_invariants(_session(), messages)

    assert result["claim_count"] == 1
    claim = result["claims"][0]
    assert claim["claim_id"] == claim_id
    assert claim["status"] == "verification_failed"
    assert claim["claim_revision"] == 2
    assert claim["verdict"] == "verification_failed"
    assert claim["differences"]["count"] == 1
    assert claim["differences"]["sample"][0]["check_id"] == (
        "oracle-check:record-17"
    )
    assert evidence_id in claim["differences"]["sample"][0]["evidence_ids"]
    assert claim["differences"]["sample"][0]["evidence_digest"].startswith(
        "sha256:"
    )


def test_compaction_binds_claim_invalidation_reason_and_revision() -> None:
    claim_id = str(uuid4())
    result = LocalLiliesService._compaction_invariants(
        _session(),
        [
            _message(
                {
                    "message_type": "verification_claim",
                    "correlation_id": claim_id,
                    "payload": {
                        "claim_id": claim_id,
                        "status": "frozen",
                        "claim_revision": 1,
                        "application_id": str(uuid4()),
                        "draft_revision": 7,
                        "content_hash": "sha256:" + "7" * 64,
                        "test_run_ids": ["test-run:before-mutation"],
                        "business_run_ids": ["business-run:before-mutation"],
                    },
                }
            ),
            _message(
                {
                    "message_type": "control",
                    "correlation_id": claim_id,
                    "payload": {
                        "kind": "claim_invalidated",
                        "claim_id": claim_id,
                        "reason": "draft revision changed after claim freeze",
                    },
                }
            ),
        ],
    )

    claim = result["claims"][0]
    assert claim["status"] == "invalidated"
    assert claim["claim_revision"] == 2
    assert claim["invalidation_reason"] == (
        "draft revision changed after claim freeze"
    )


def test_compaction_reduces_typed_report_lifecycle_without_losing_evidence() -> None:
    report_id = str(uuid4())
    messages = [
        _message(
            {
                "message_type": "report",
                "correlation_id": report_id,
                "payload": {
                    "report_id": report_id,
                    "category": "platform_capability_gap",
                    "status": "awaiting_user_review",
                    "revision": 3,
                    "original_goal": "preserve the original enterprise business goal",
                    "requirement_digest": "sha256:" + "0" * 64,
                    "platform_contract_digest": "sha256:" + "9" * 64,
                    "summary": "generic connector contract lacks a required primitive",
                    "attempted_routes": [
                        {
                            "attempt_id": "attempt-initial",
                            "route": "public contract route",
                            "outcome": "unsupported",
                            "evidence_refs": [
                                {
                                    "evidence_id": "evidence:attempt-initial",
                                    "digest": "sha256:" + "f" * 64,
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        _message(
            {
                "message_type": "approval",
                "correlation_id": report_id,
                "payload": {
                    "report_id": report_id,
                    "report_revision": 3,
                    "decision": "approve",
                    "reason": "generic capability repair is authorized",
                },
            }
        ),
        _message(
            {
                "message_type": "developer_response",
                "correlation_id": report_id,
                "payload": {
                    "report_id": report_id,
                    "report_revision": 4,
                    "outcome": "implemented",
                    "commit_sha": "1" * 40,
                    "new_contract_digest": "sha256:" + "1" * 64,
                    "generic_capability_changes": ["added bounded public primitive"],
                    "tests_run": [
                        {
                            "test_id": "test:public-contract-repair",
                            "command": "pytest focused-contract-test",
                            "exit_code": 0,
                            "summary": "passed",
                            "evidence_refs": [
                                {
                                    "evidence_id": "evidence:test-contract-repair",
                                    "digest": "sha256:" + "2" * 64,
                                }
                            ],
                        }
                    ],
                    "reprobe_steps": ["fetch contract", "retry public operation"],
                },
            }
        ),
    ]

    result = LocalLiliesService._compaction_invariants(_session(), messages)
    report = result["reports"][0]

    assert report["report_id"] == report_id
    assert report["status"] == "ready_for_lilies_verification"
    assert report["revision"] == 5
    assert result["no_substitute_validation"] is True
    assert report["original_goal"] == "preserve the original enterprise business goal"
    assert report["platform_contract_digest"] == "sha256:" + "9" * 64
    assert report["commit_sha"] == "1" * 40
    assert report["new_contract_digest"] == "sha256:" + "1" * 64
    assert report["attempted_routes"]["count"] == 1
    assert report["attempted_routes"]["index"][0][0] == "attempt-initial"
    assert report["tests_run"]["sample"][0]["exit_code"] == 0


def test_compaction_indexes_all_one_hundred_attempted_routes_within_bound() -> None:
    report_id = str(uuid4())
    attempts = [
        {
            "attempt_id": f"attempt-{index:03d}",
            "route": f"public-route-{index:03d}",
            "input_digest": "sha256:" + f"{index:064x}"[-64:],
            "outcome": f"unsupported-result-{index:03d}",
            "attempted_at": "2026-07-22T00:00:00+00:00",
            "evidence_refs": [
                {
                    "evidence_id": f"evidence:route-{index:03d}",
                    "digest": "sha256:" + f"{index + 1:064x}"[-64:],
                }
            ],
        }
        for index in range(100)
    ]
    result = LocalLiliesService._compaction_invariants(
        _session(),
        [
            _message(
                {
                    "message_type": "report",
                    "correlation_id": report_id,
                    "payload": {
                        "report_id": report_id,
                        "category": "platform_capability_gap",
                        "status": "awaiting_user_review",
                        "revision": 3,
                        "summary": "one hundred bounded attempts",
                        "attempted_routes": attempts,
                    },
                }
            )
        ],
    )

    serialized = json.dumps(result, separators=(",", ":"), sort_keys=True)
    report = result["reports"][0]
    route_summary = report["attempted_routes"]
    assert len(serialized) <= 30_000
    assert route_summary["count"] == 100
    assert route_summary["index_schema"] == [
        "attempt_id",
        "route",
        "outcome",
        "evidence_count",
        "semantic_digest",
    ]
    assert len(route_summary["index"]) == 100
    assert [row[0] for row in route_summary["index"]] == [
        f"attempt-{index:03d}" for index in range(100)
    ]
    assert all(
        row[3] == 1 and str(row[4]).startswith("sha256-b64:")
        for row in route_summary["index"]
    )


def test_compaction_never_drops_an_active_report_when_detail_must_shrink() -> None:
    first_report_id = str(uuid4())
    second_report_id = str(uuid4())
    causal_parent_id = str(uuid4())
    attempts = [
        {
            "attempt_id": f"attempt-{index:03d}",
            "route": f"public-route-{index:03d}",
            "outcome": f"failed-with-evidence-{index:03d}",
            "evidence_refs": [
                {
                    "evidence_id": f"evidence:attempt-{index:03d}",
                    "digest": "sha256:" + f"{index + 1:064x}"[-64:],
                }
            ],
        }
        for index in range(100)
    ]
    verbose_tests = [
        {
            "test_id": f"test:detail-{index:03d}",
            "command": "verify " + ("x" * 490),
            "exit_code": 0,
            "summary": "passed " + ("y" * 490),
            "evidence_refs": [
                {
                    "evidence_id": f"evidence:test-{index:03d}",
                    "digest": "sha256:" + f"{index + 2:064x}"[-64:],
                }
            ],
        }
        for index in range(100)
    ]
    result = LocalLiliesService._compaction_invariants(
        _session(),
        [
            _message(
                {
                    "message_type": "report",
                    "correlation_id": first_report_id,
                    "causal_parent_id": causal_parent_id,
                    "payload": {
                        "report_id": first_report_id,
                        "category": "platform_capability_gap",
                        "status": "awaiting_user_review",
                        "revision": 3,
                        "original_goal": "retain the first unresolved business goal",
                        "requirement_digest": "sha256:" + "3" * 64,
                        "platform_contract_digest": "sha256:" + "4" * 64,
                        "attempted_routes": attempts,
                    },
                }
            ),
            _message(
                {
                    "message_type": "report",
                    "correlation_id": second_report_id,
                    "payload": {
                        "report_id": second_report_id,
                        "category": "environment_gap",
                        "status": "environment_failed",
                        "revision": 2,
                        "original_goal": "retain the second unresolved business goal",
                        "requirement_digest": "sha256:" + "5" * 64,
                        "tests_run": verbose_tests,
                    },
                }
            ),
        ],
    )

    serialized = json.dumps(result, separators=(",", ":"), sort_keys=True)
    reports = {report["report_id"]: report for report in result["reports"]}
    assert len(serialized) <= 30_000
    assert set(reports) == {first_report_id, second_report_id}
    assert reports[first_report_id]["original_goal"] == (
        "retain the first unresolved business goal"
    )
    assert reports[first_report_id]["platform_contract_digest"] == (
        "sha256:" + "4" * 64
    )
    assert len(reports[first_report_id]["attempted_routes"]["index"]) == 100
    assert reports[second_report_id]["tests_run"]["digest"].startswith("sha256:")
    if result["report_index"]:
        causal_index = result["report_index_schema"].index(
            "causal_parent_uuid_hex"
        )
        assert str(UUID(hex=result["report_index"][0][causal_index])) == (
            causal_parent_id
        )
    else:
        assert reports[first_report_id]["causal_parent_id"] == causal_parent_id


def test_compaction_preserves_identity_for_more_than_one_hundred_unresolved_reports() -> None:
    expected: dict[str, tuple[str, str, str]] = {}
    events: list[dict[str, object]] = []
    for index in range(121):
        report_id = str(uuid4())
        causal_parent_id = str(uuid4())
        category = (
            "platform_capability_gap" if index % 2 == 0 else "environment_gap"
        )
        expected[report_id] = (category, "awaiting_user_review", causal_parent_id)
        events.append(
            {
                "message_type": "report",
                "correlation_id": report_id,
                "causal_parent_id": causal_parent_id,
                "payload": {
                    "report_id": report_id,
                    "category": category,
                    "status": "awaiting_user_review",
                    "revision": 3,
                    "original_goal": f"unresolved business goal {index:03d}",
                },
            }
        )

    result = LocalLiliesService._compaction_invariants(_session(), [_message(events)])
    reports = {report["report_id"]: report for report in result["reports"]}
    observed = {
        report_id: (
            str(report["category"]),
            str(report["status"]),
            str(report["causal_parent_id"]),
        )
        for report_id, report in reports.items()
    }
    schema = result["report_index_schema"]
    for row in result["report_index"]:
        report_id = str(UUID(hex=str(row[schema.index("report_id_uuid_hex")])))
        causal_parent_id = str(
            UUID(hex=str(row[schema.index("causal_parent_uuid_hex")]))
        )
        observed.setdefault(
            report_id,
            (
                result["report_category_codes"][row[schema.index("category_code")]],
                result["report_status_codes"][row[schema.index("status_code")]],
                causal_parent_id,
            ),
        )

    assert len(json.dumps(result, separators=(",", ":"), sort_keys=True)) <= 30_000
    assert result["report_count"] == 121
    assert len(observed) == 121
    assert set(observed) == set(expected)
    for report_id, (category, status, causal_parent_id) in expected.items():
        assert observed[report_id] == (category, status, causal_parent_id)


def test_compaction_preserves_details_for_more_than_twenty_unresolved_reports() -> None:
    expected: dict[str, tuple[str, str, str, str, str]] = {}
    events: list[dict[str, object]] = []
    for index in range(121):
        report_id = str(uuid4())
        goal = f"business goal with distinct invariant {index:03d}"
        attempt_id = f"attempt-{index:03d}"
        route = f"public route unique {index:03d}"
        outcome = f"failure outcome unique {index:03d}"
        evidence_id = f"evidence:report-unique-{index:03d}"
        expected[report_id] = (goal, attempt_id, route, outcome, evidence_id)
        events.append(
            {
                "message_type": "report",
                "correlation_id": report_id,
                "causal_parent_id": str(uuid4()),
                "payload": {
                    "report_id": report_id,
                    "category": "platform_capability_gap",
                    "status": "awaiting_user_review",
                    "revision": 3,
                    "original_goal": goal,
                    "requirement_digest": "sha256:" + f"{index:064x}"[-64:],
                    "attempted_routes": [
                        {
                            "attempt_id": attempt_id,
                            "route": route,
                            "outcome": outcome,
                            "evidence_refs": [
                                {
                                    "evidence_id": evidence_id,
                                    "digest": "sha256:" + f"{index + 1:064x}"[-64:],
                                }
                            ],
                        }
                    ],
                },
            }
        )

    result = LocalLiliesService._compaction_invariants(_session(), [_message(events)])
    reports = {report["report_id"]: report for report in result["reports"]}
    schema = result["report_index_schema"]
    index_rows = {
        str(UUID(hex=str(row[schema.index("report_id_uuid_hex")]))): row
        for row in result["report_index"]
    }

    assert len(json.dumps(result, separators=(",", ":"), sort_keys=True)) <= 30_000
    assert set(reports) | set(index_rows) == set(expected)
    for report_id, (goal, attempt_id, route, outcome, evidence_id) in expected.items():
        if report_id in reports:
            report = reports[report_id]
            assert report["original_goal"] == goal
            attempted = report["attempted_routes"]
            assert attempted["count"] == 1
            assert attempted["index"][0][0] == attempt_id
            assert attempted["sample"][0]["route"] == route
            assert attempted["sample"][0]["outcome"] == outcome
            assert evidence_id in attempted["sample"][0]["evidence_ids"]
            continue
        row = index_rows[report_id]
        assert row[schema.index("original_goal_is_digest")] is False
        assert row[schema.index("original_goal_or_digest")] == goal
        attempts = row[schema.index("attempted_routes")]
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt[:3] == [attempt_id, route, outcome]
        assert evidence_id in attempt[3]
        assert attempt[4] == 1
        assert attempt[5] is None


def test_compaction_preserves_more_than_thirty_user_decisions() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"approve decision-{index:03d}"}
            ],
        }
        for index in range(31)
    ]

    result = LocalLiliesService._compaction_invariants(_session(), messages)

    assert len(result["user_decisions"]) == 31
    assert "decision-000" in result["user_decisions"][0]
    assert "decision-030" in result["user_decisions"][-1]
    assert len(json.dumps(result, separators=(",", ":"), sort_keys=True)) <= 30_000


def test_compaction_preserves_more_than_thirty_workflow_states() -> None:
    application_ids = [str(uuid4()) for _ in range(31)]
    messages = [
        _message(
            {
                "application_id": application_id,
                "draft_revision": index + 1,
                "content_hash": "sha256:" + f"{index + 1:064x}"[-64:],
                "test_run_ids": [f"test-run:{index:03d}"],
                "business_run_ids": [f"business-run:{index:03d}"],
            }
        )
        for index, application_id in enumerate(application_ids)
    ]

    result = LocalLiliesService._compaction_invariants(_session(), messages)

    assert len(result["workflow_state"]) == 31
    assert {state["application_id"] for state in result["workflow_state"]} == set(
        application_ids
    )
    assert len(json.dumps(result, separators=(",", ":"), sort_keys=True)) <= 30_000


def test_compaction_preserves_more_than_forty_claims() -> None:
    claim_ids = [str(uuid4()) for _ in range(41)]
    events = [
        {
            "message_type": "verification_claim",
            "correlation_id": claim_id,
            "payload": {
                "claim_id": claim_id,
                "status": "frozen",
                "claim_revision": 1,
                "application_id": str(uuid4()),
                "draft_revision": index + 1,
                "content_hash": "sha256:" + f"{index + 1:064x}"[-64:],
                "test_run_ids": [f"test-run:claim-{index:03d}"],
                "business_run_ids": [f"business-run:claim-{index:03d}"],
            },
        }
        for index, claim_id in enumerate(claim_ids)
    ]

    result = LocalLiliesService._compaction_invariants(_session(), [_message(events)])

    assert result["claim_count"] == 41
    assert {claim["claim_id"] for claim in result["claims"]} == set(claim_ids)
    assert len(json.dumps(result, separators=(",", ":"), sort_keys=True)) <= 30_000


@pytest.mark.asyncio
async def test_full_compaction_preserves_ten_thousand_character_business_goal(
    tmp_path: Path,
) -> None:
    decisive_clause = "DECISIVE-MIDDLE-CLAUSE-requires-human-review-before-writeback"
    business_goal = (
        ("A" * 4_970) + decisive_clause + ("B" * (5_030 - len(decisive_clause)))
    )
    assert len(business_goal) == 10_000
    report_id = str(uuid4())
    assignment = {
        "requirement": "Complete the frozen enterprise task without substitutes.",
        "business_context": {
            "business_goal": business_goal,
            "customer_roles": ["operations manager"],
            "inputs": ["approved records"],
            "outputs": ["host receipts"],
            "constraints": ["human review is binding"],
        },
        "constraints": {"no_substitute_validation": True},
    }
    service = LocalLiliesService(
        LiliesSettings(
            data_dir=tmp_path / "long-goal",
            context_window=8_000,
            max_output_tokens=256,
        ),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id = str(uuid4())
    await service.storage.create_session(
        session_id=session_id,
        assignment=assignment,
    )
    durable_report = {
        "message_type": "report",
        "correlation_id": report_id,
        "payload": {
            "report_id": report_id,
            "category": "platform_capability_gap",
            "status": "awaiting_user_review",
            "revision": 3,
            "original_goal": business_goal,
            "requirement_digest": "sha256:" + "8" * 64,
            "platform_contract_digest": "sha256:" + "9" * 64,
        },
    }
    await service.storage.add_message(
        session_id,
        "user",
        _message(durable_report)["content"],
    )
    for index in range(11):
        await service.storage.add_message(
            session_id,
            "user",
            [{"type": "text", "text": f"filler-{index}:" + ("z" * 3_000)}],
        )

    session = await service.storage.get_session(session_id)
    await service._compact_if_needed(session_id, session)
    compacted = await service.storage.get_session(session_id)
    summary = compacted["context_summary"]
    assignment_projection = json.loads(
        LocalLiliesService._compaction_assignment_projection(assignment)
    )
    invariants = LocalLiliesService._compaction_invariants(
        compacted,
        await service.storage.list_messages_for_compaction(session_id),
    )

    assert assignment_projection["business_context"]["business_goal"] == business_goal
    assert invariants["reports"][0]["original_goal"] == business_goal
    assert decisive_clause in summary
    assert business_goal in summary
    assert invariants["no_substitute_validation"] is True


def test_compaction_combines_max_goal_and_one_hundred_max_evidence_attempts() -> None:
    decisive_clause = "DECISIVE-COMBINED-CLAUSE-requires-human-review"
    original_goal = (
        ("G" * 4_980) + decisive_clause + ("H" * (5_020 - len(decisive_clause)))
    )
    now = datetime.now(timezone.utc).isoformat()

    def evidence(evidence_id: str, digest_digit: str) -> dict[str, object]:
        return {
            "evidence_id": evidence_id,
            "kind": "trace",
            "digest": "sha256:" + digest_digit * 64,
            "media_type": "application/json",
            "label": "bounded black-box observation",
            "captured_at": now,
        }

    attempts = [
        {
            "attempt_id": str(uuid4()),
            "route": f"public-operation-{index:03d}-" + ("r" * 470),
            "input_digest": "sha256:" + f"{index:064x}"[-64:],
            "outcome": f"unsupported-outcome-{index:03d}-" + ("o" * 4_960),
            "evidence_refs": [
                evidence(
                    f"evidence:attempt-{index:03d}-{evidence_index:03d}",
                    f"{(index + evidence_index) % 16:x}",
                )
                for evidence_index in range(100)
            ],
            "attempted_at": now,
        }
        for index in range(100)
    ]
    payload = CollaborationReportPayload.model_validate(
        {
            "report_id": str(uuid4()),
            "category": "platform_capability_gap",
            "phase": "planning",
            "severity": "blocking",
            "summary": "The public contract lacks the required generic operation.",
            "original_goal": original_goal,
            "requirement_digest": "sha256:" + "1" * 64,
            "platform_contract_digest": "sha256:" + "2" * 64,
            "manuals_checked": [
                {
                    "manual_id": "manual:generic-operation",
                    "version": "1.0",
                    "digest": "sha256:" + "3" * 64,
                }
            ],
            "attempted_routes": attempts,
            "expected": "A public generic operation returns a durable receipt.",
            "actual": "Every public route returned an unsupported result.",
            "missing_contract": "A bounded generic input, result, receipt, and error contract.",
            "blocking_scope": "Host writeback is blocked; independent parsing can continue.",
            "workaround_considered": ["substitute validation", "manual writeback"],
            "workaround_loss": "Either route would invalidate the business oracle.",
            "requested_outcome": "Expose the generic operation in the public contract.",
            "confidence": 1.0,
            "secret_redactions": ["sensitive values removed"],
            "evidence_refs": [
                evidence(f"evidence:report-{index:03d}", f"{index % 16:x}")
                for index in range(500)
            ],
        }
    ).model_dump(mode="json")
    report_id = payload["report_id"]
    result = LocalLiliesService._compaction_invariants(
        _session(),
        [
            _message(
                {
                    "message_type": "report",
                    "correlation_id": report_id,
                    "payload": {**payload, "status": "awaiting_user_review", "revision": 3},
                }
            )
        ],
    )

    serialized = json.dumps(result, separators=(",", ":"), sort_keys=True)
    report = result["reports"][0]
    attempted = report["attempted_routes"]
    assert len(serialized) <= 30_000
    assert report["original_goal"] == original_goal
    assert decisive_clause in report["original_goal"]
    assert attempted["count"] == 100
    assert len(attempted["index"]) == 100
    assert all(row[3] == 100 for row in attempted["index"])
    assert "index_omitted_for_global_bound" not in attempted


@pytest.mark.asyncio
async def test_compaction_accessor_reads_beyond_public_five_thousand_ceiling(
    tmp_path: Path,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id = str(uuid4())
    await service.storage.create_session(session_id=session_id)
    report_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    filler = json.dumps([{"type": "text", "text": "bounded filler"}])
    final = json.dumps(
        [
            {
                "type": "text",
                "text": (
                    "Durable collaboration updates for the current formal task:\n"
                    + json.dumps(
                        {
                            "message_type": "report",
                            "correlation_id": report_id,
                            "payload": {
                                "report_id": report_id,
                                "category": "environment_gap",
                                "status": "environment_failed",
                                "revision": 2,
                                "summary": "late durable collaboration state",
                            },
                        },
                        sort_keys=True,
                    )
                ),
            }
        ]
    )
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.executemany(
            "INSERT INTO messages(id,session_id,role,content_json,created_at) "
            "VALUES (?,?,?,?,?)",
            [
                (f"message-{index:05d}", session_id, "user", filler, created_at)
                for index in range(5_001)
            ]
            + [("zz-final-message", session_id, "user", final, created_at)],
        )

    recent = await service.storage.list_recent_messages(session_id, limit=5_000)
    complete = await service.storage.list_messages_for_compaction(session_id)
    result = LocalLiliesService._compaction_invariants(_session(), complete)

    assert len(recent) == 5_000
    assert len(complete) == 5_002
    assert all(message["id"] != "message-00000" for message in recent)
    assert result["report_count"] == 1
    assert result["reports"][0]["report_id"] == report_id
