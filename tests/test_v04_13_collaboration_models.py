from __future__ import annotations

import copy
import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

import agent_platform.collaboration_models as collaboration_models
from agent_platform.collaboration_models import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    ChannelSettingsRequest,
    CollaborationChannel,
    CollaborationMessageEnvelope,
    CollaborationReport,
    CollaborationReportPayload,
    DeveloperLease,
    DeveloperResponse,
    DeveloperResponseRequest,
    EnvironmentResponse,
    EvidenceRef,
    LeaseAcquireRequest,
    LiliesReprobeResult,
    ReaderCursor,
    ReportCategory,
    ReportSubmitRequest,
    TaskPackageAmendment,
    VerificationClaim,
    VerificationDifference,
    VerificationResult,
)


NOW = datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
IDEMPOTENCY_KEY = "collaboration-request-0001"


def evidence(
    *,
    evidence_id: str = "evidence:trace-0001",
    kind: str = "trace",
    digest: str = DIGEST_A,
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "digest": digest,
        "media_type": "application/json",
        "label": "Black-box trace evidence",
        "captured_at": NOW.isoformat(),
    }


def attempted_route() -> dict[str, object]:
    return {
        "attempt_id": str(uuid4()),
        "route": "workflow block catalog lookup",
        "input_digest": DIGEST_A,
        "outcome": "The required generic block was not present.",
        "evidence_refs": [evidence()],
        "attempted_at": NOW.isoformat(),
    }


def report_payload(
    *,
    category: str = "platform_capability_gap",
    complete: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "report_id": str(uuid4()),
        "category": category,
        "phase": "preflight",
        "severity": "blocking",
        "summary": "The public catalog lacks the required generic intake block.",
        "original_goal": "Build a verified enterprise intake and reconciliation workflow.",
        "requirement_digest": DIGEST_A,
        "manuals_checked": [],
        "attempted_routes": [],
        "blocking_scope": "Input normalization is blocked; artifact planning can continue.",
        "independent_work": ["Plan the artifact validation branch."],
        "workaround_considered": ["Use the closest documented catalog block."],
        "workaround_loss": "The substitute drops the required typed validation evidence.",
        "requested_outcome": "Add a generic typed intake contract and its evidence semantics.",
        "confidence": 0.94,
        "secret_redactions": ["provider_api_key"],
        "evidence_refs": [],
    }
    if complete:
        payload.update(
            {
                "platform_contract_digest": DIGEST_B,
                "manuals_checked": [
                    {
                        "manual_id": "manual:block-catalog",
                        "version": "2026-07-23",
                        "digest": DIGEST_A,
                    }
                ],
                "attempted_routes": [attempted_route()],
                "expected": "The catalog exposes a typed intake contract.",
                "actual": "No documented block satisfies the typed intake contract.",
                "evidence_refs": [evidence()],
            }
        )
        if category == "platform_capability_gap":
            payload["missing_contract"] = (
                "Inputs, outputs, validation errors, and immutable result evidence."
            )
        elif category == "platform_defect_suspected":
            payload["reproduction"] = [
                "Read the advertised contract digest.",
                "Invoke the documented operation with the public fixture.",
            ]
    return payload


def test_report_category_excludes_non_pipeline_and_claim_routes() -> None:
    assert {category.value for category in ReportCategory} == {
        "task_spec_gap",
        "environment_gap",
        "platform_capability_gap",
        "platform_defect_suspected",
    }
    for forbidden in (
        "workflow_design_error",
        "permission_request",
        "verification_claim",
    ):
        payload = report_payload()
        payload["category"] = forbidden
        with pytest.raises(ValidationError, match="Input should be"):
            CollaborationReportPayload.model_validate(payload)


def test_all_collaboration_models_and_requests_forbid_unknown_fields() -> None:
    models = [
        model
        for _, model in inspect.getmembers(collaboration_models, inspect.isclass)
        if model.__module__ == collaboration_models.__name__
        and issubclass(model, collaboration_models.StrictModel)
    ]
    assert len(models) >= 25
    assert all(model.model_config.get("extra") == "forbid" for model in models)

    valid = {
        "idempotency_key": IDEMPOTENCY_KEY,
        "expected_channel_revision": 1,
        "report": report_payload(),
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReportSubmitRequest.model_validate({**valid, "route": "developer"})


def test_evidence_refs_are_content_addressed_immutable_and_utc() -> None:
    item = EvidenceRef.model_validate(evidence())
    with pytest.raises(ValidationError, match="Instance is frozen"):
        item.label = "Changed label"

    invalid_digest = evidence()
    invalid_digest["digest"] = "a" * 64
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        EvidenceRef.model_validate(invalid_digest)

    non_utc = evidence()
    non_utc["captured_at"] = "2026-07-23T10:02:03+09:00"
    with pytest.raises(ValidationError, match="must use UTC"):
        EvidenceRef.model_validate(non_utc)


def test_channel_lifecycle_and_cas_fields_are_strict() -> None:
    channel_id = uuid4()
    base = {
        "channel_id": str(channel_id),
        "task_id": "EXP-LILIES-001",
        "task_revision": 3,
        "assignment_id": str(uuid4()),
        "lilies_session_id": str(uuid4()),
        "application_ids": [str(uuid4())],
        "approval_mode": "manual",
        "status": "active",
        "revision": 4,
        "next_seq": 18,
        "created_at": NOW.isoformat(),
        "retention_until": (NOW + timedelta(days=30)).isoformat(),
    }
    channel = CollaborationChannel.model_validate(base)
    assert channel.channel_id == channel_id
    assert channel.revision == 4
    assert channel.next_seq == 18

    with pytest.raises(ValidationError, match="require closed_at"):
        CollaborationChannel.model_validate({**base, "status": "closed"})
    with pytest.raises(ValidationError, match="must omit closed_at"):
        CollaborationChannel.model_validate(
            {**base, "closed_at": (NOW + timedelta(hours=1)).isoformat()}
        )


def test_incomplete_platform_report_is_persistable_but_not_approvable() -> None:
    report = CollaborationReportPayload.model_validate(report_payload(complete=False))
    assert report.completeness_issues() == (
        "attempted_routes",
        "expected",
        "actual",
        "evidence_refs",
        "platform_contract_digest",
        "manuals_checked",
        "missing_contract",
    )
    assert report.is_complete_for_routing() is False

    complete = CollaborationReportPayload.model_validate(report_payload())
    assert complete.completeness_issues() == ()
    assert complete.is_complete_for_routing() is True


def test_category_specific_completeness_is_deterministic() -> None:
    defect = report_payload(category="platform_defect_suspected")
    defect.pop("reproduction")
    parsed = CollaborationReportPayload.model_validate(defect)
    assert parsed.completeness_issues() == ("reproduction",)

    task_gap = report_payload(category="task_spec_gap")
    task_gap.pop("platform_contract_digest")
    task_gap["manuals_checked"] = []
    task_gap.pop("missing_contract", None)
    parsed_task = CollaborationReportPayload.model_validate(task_gap)
    assert parsed_task.completeness_issues() == ()

    incomplete_task = report_payload(category="task_spec_gap", complete=False)
    incomplete_task.pop("missing_contract", None)
    with pytest.raises(ValidationError, match="require common evidence fields"):
        CollaborationReportPayload.model_validate(incomplete_task)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actual", "Authorization: Bearer super-secret-value"),
        ("actual", "Cookie: session=customer-secret"),
        ("actual", "Provider returned sk-abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_report_payload_redacts_plaintext_credentials(
    field: str,
    value: str,
) -> None:
    payload = report_payload()
    payload[field] = value
    parsed = CollaborationReportPayload.model_validate(payload)
    assert parsed.actual == "[REDACTED]"

    redacted = report_payload()
    redacted["actual"] = "Authorization and cookie values were [REDACTED]."
    CollaborationReportPayload.model_validate(redacted)


def test_report_payload_rejects_protected_oracle_material() -> None:
    payload = report_payload()
    payload["actual"] = "Read oracle://private/expected.json"
    with pytest.raises(ValidationError, match="protected oracle reference"):
        CollaborationReportPayload.model_validate(payload)


def test_report_payload_rejects_sensitive_keys_at_any_depth() -> None:
    payload = report_payload()
    attempted = copy.deepcopy(payload["attempted_routes"])
    assert isinstance(attempted, list)
    assert isinstance(attempted[0], dict)
    attempted[0]["diagnostic"] = {"access_token": "smuggled"}
    payload["attempted_routes"] = attempted
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CollaborationReportPayload.model_validate(payload)


def test_persisted_report_enforces_category_route_and_status() -> None:
    payload = report_payload()
    record = CollaborationReport.model_validate(
        {
            **payload,
            "channel_id": str(uuid4()),
            "source_message_id": str(uuid4()),
            "route": "capability_approval",
            "status": "awaiting_user_review",
            "revision": 2,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )
    assert record.status.value == "awaiting_user_review"

    invalid = record.model_dump(mode="json")
    invalid["route"] = "task_author"
    with pytest.raises(ValidationError, match="capability state machine"):
        CollaborationReport.model_validate(invalid)

    preapproval_leak = record.model_dump(mode="json")
    preapproval_leak["route"] = "developer"
    with pytest.raises(ValidationError, match="capability state machine"):
        CollaborationReport.model_validate(preapproval_leak)

    approved_but_hidden = record.model_dump(mode="json")
    approved_but_hidden["status"] = "approved_for_codex"
    with pytest.raises(ValidationError, match="capability state machine"):
        CollaborationReport.model_validate(approved_but_hidden)


def test_envelope_validates_declared_payload_schema_and_type() -> None:
    channel_id = uuid4()
    report = report_payload()
    envelope_payload = {
        "message_id": str(uuid4()),
        "channel_id": str(channel_id),
        "seq": 9,
        "message_type": "report",
        "sender_role": "lilies",
        "sender_id": "lilies:session-01",
        "correlation_id": str(uuid4()),
        "idempotency_key": IDEMPOTENCY_KEY,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.report.v1",
        "payload": report,
        "evidence_refs": [evidence()],
        "created_at": NOW.isoformat(),
    }
    envelope = CollaborationMessageEnvelope.model_validate(envelope_payload)
    assert envelope.seq == 9
    assert envelope.payload["report_id"] == report["report_id"]
    assert envelope.model_dump(mode="json")["payload"]["category"] == (
        "platform_capability_gap"
    )

    wrong_type = {**envelope_payload, "message_type": "developer_response"}
    with pytest.raises(ValidationError, match="requires message_type report"):
        CollaborationMessageEnvelope.model_validate(wrong_type)

    extra_payload = copy.deepcopy(envelope_payload)
    extra_payload["payload"]["freeform_route"] = "codex"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CollaborationMessageEnvelope.model_validate(extra_payload)

    role_escalation = {**envelope_payload, "sender_role": "codex"}
    with pytest.raises(ValidationError, match="does not allow sender_role codex"):
        CollaborationMessageEnvelope.model_validate(role_escalation)

    visibility_escalation = {
        **envelope_payload,
        "visibility": "approved_developer",
    }
    with pytest.raises(ValidationError, match="visibility approved_developer"):
        CollaborationMessageEnvelope.model_validate(visibility_escalation)


def test_lilies_control_is_restricted_to_exact_report_withdrawal() -> None:
    channel_id = uuid4()
    report_id = uuid4()
    control = {
        "control_id": str(uuid4()),
        "channel_id": str(channel_id),
        "kind": "report_status_changed",
        "actor_id": "lilies:session-01",
        "reason": "The black-box evidence showed this report is no longer applicable.",
        "report_id": str(report_id),
        "previous_value": "awaiting_user_review",
        "new_value": "withdrawn",
        "created_at": NOW.isoformat(),
    }
    envelope = {
        "message_id": str(uuid4()),
        "channel_id": str(channel_id),
        "seq": 10,
        "message_type": "control",
        "sender_role": "lilies",
        "sender_id": "lilies:session-01",
        "correlation_id": str(report_id),
        "idempotency_key": IDEMPOTENCY_KEY,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.control.v1",
        "payload": control,
        "created_at": NOW.isoformat(),
    }
    CollaborationMessageEnvelope.model_validate(envelope)

    for forged in (
        {**control, "kind": "channel_closed"},
        {**control, "new_value": "approved_for_codex"},
        {**control, "report_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError, match="exact report withdrawal"):
            CollaborationMessageEnvelope.model_validate({**envelope, "payload": forged})


def test_approval_requires_reason_and_monotonic_revision() -> None:
    base = {
        "approval_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "expected_report_revision": 4,
        "resulting_report_revision": 5,
        "decision": "reject",
        "actor_id": "user:operator-1",
        "idempotency_key": IDEMPOTENCY_KEY,
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValidationError, match="require reason"):
        ApprovalDecision.model_validate(base)
    with pytest.raises(ValidationError, match="must increment"):
        ApprovalDecision.model_validate(
            {**base, "reason": "Evidence is not sufficient.", "resulting_report_revision": 7}
        )
    ApprovalDecision.model_validate({**base, "reason": "Evidence is not sufficient."})


def test_task_amendment_requires_new_immutable_revision_and_digest() -> None:
    base = {
        "amendment_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_revision": 3,
        "task_id": "EXP-LILIES-001",
        "outcome": "amended",
        "previous_task_revision": 2,
        "previous_requirement_digest": DIGEST_A,
        "new_task_revision": 3,
        "new_requirement_digest": DIGEST_B,
        "reason": "Supply the missing public fixture contract.",
        "changes": ["Added the immutable fixture digest."],
        "evidence_refs": [evidence(kind="task_package")],
        "created_at": NOW.isoformat(),
    }
    TaskPackageAmendment.model_validate(base)
    with pytest.raises(ValidationError, match="must change requirement digest"):
        TaskPackageAmendment.model_validate({**base, "new_requirement_digest": DIGEST_A})
    with pytest.raises(ValidationError, match="must not claim a new revision"):
        TaskPackageAmendment.model_validate({**base, "outcome": "rejected_with_evidence"})


def test_environment_restore_requires_real_passing_health_evidence() -> None:
    check = {
        "test_id": "health:host-0001",
        "command": "host-contract health --fixture public-fixture",
        "exit_code": 0,
        "summary": "The real host contract is reachable.",
        "evidence_ref": evidence(kind="health_check"),
    }
    base = {
        "response_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_revision": 2,
        "outcome": "restored",
        "environment_digest": DIGEST_A,
        "summary": "The declared host environment is restored.",
        "health_checks": [check],
        "known_limits": [],
        "created_at": NOW.isoformat(),
    }
    EnvironmentResponse.model_validate(base)
    failed_check = copy.deepcopy(check)
    failed_check["exit_code"] = 1
    with pytest.raises(ValidationError, match="all health checks to pass"):
        EnvironmentResponse.model_validate({**base, "health_checks": [failed_check]})


def developer_response_payload(*, changes: list[str] | None = None) -> dict[str, object]:
    return {
        "response_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_revision": 6,
        "outcome": "implemented",
        "commit_sha": "a" * 40,
        "generic_capability_changes": changes
        or ["Added the generic typed intake contract and black-box validation."],
        "new_contract_digest": DIGEST_B,
        "tests_run": [
            {
                "test_id": "test:typed-intake-0001",
                "command": "pytest -q tests/test_typed_intake.py",
                "exit_code": 0,
                "summary": "All typed intake contract tests passed.",
                "evidence_ref": evidence(kind="test_run"),
            }
        ],
        "browser_or_live_evidence": [],
        "known_limits": ["The contract does not infer customer-specific schemas."],
        "reprobe_steps": [
            {
                "order": 1,
                "action": "Refresh the public platform contract.",
                "expected": "The contract digest changes to the response digest.",
            },
            {
                "order": 2,
                "action": "Run the same public fixture through typed intake.",
                "expected": "The result includes immutable validation evidence.",
            },
        ],
        "created_at": NOW.isoformat(),
    }


def test_developer_response_cannot_be_bare_ok_or_unverified_commit() -> None:
    DeveloperResponse.model_validate(developer_response_payload())
    with pytest.raises(ValidationError, match="substantive generic changes"):
        DeveloperResponse.model_validate(developer_response_payload(changes=["OK"]))

    failing = developer_response_payload()
    tests_run = copy.deepcopy(failing["tests_run"])
    assert isinstance(tests_run, list)
    assert isinstance(tests_run[0], dict)
    tests_run[0]["exit_code"] = 1
    failing["tests_run"] = tests_run
    with pytest.raises(ValidationError, match="declared tests to pass"):
        DeveloperResponse.model_validate(failing)

    no_commit = developer_response_payload()
    no_commit["commit_sha"] = None
    with pytest.raises(ValidationError, match="requires commit_sha"):
        DeveloperResponse.model_validate(no_commit)


def claim_payload(*, status: str = "frozen") -> dict[str, object]:
    return {
        "claim_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "claim_revision": 1,
        "draft_revision": 8,
        "content_hash": DIGEST_A,
        "published_version": 2,
        "test_run_ids": ["test-run:acceptance-0001"],
        "business_run_ids": ["business-run:host-0001"],
        "artifact_refs": [evidence(kind="artifact")],
        "host_receipt_refs": [
            evidence(
                evidence_id="evidence:host-receipt-0001",
                kind="host_receipt",
                digest=DIGEST_B,
            )
        ],
        "resolved_report_ids": [str(uuid4())],
        "remaining_limits": [],
        "claim": "ready_for_independent_verification",
        "status": status,
        "created_at": NOW.isoformat(),
    }


def test_claim_is_frozen_and_explicitly_invalidated_after_draft_change() -> None:
    claim = VerificationClaim.model_validate(claim_payload())
    assert claim.status.value == "frozen"
    invalid = claim_payload(status="invalidated")
    with pytest.raises(ValidationError, match="requires timestamp and reason"):
        VerificationClaim.model_validate(invalid)
    invalid.update(
        {
            "invalidated_at": (NOW + timedelta(minutes=1)).isoformat(),
            "invalidation_reason": "Draft content hash changed after the claim was frozen.",
        }
    )
    VerificationClaim.model_validate(invalid)

    wrong_claim = claim_payload()
    wrong_claim["claim"] = "pass"
    with pytest.raises(ValidationError, match="ready_for_independent_verification"):
        VerificationClaim.model_validate(wrong_claim)


def test_verification_result_keeps_oracle_hidden_and_requires_differences() -> None:
    base = {
        "verification_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "claim_id": str(uuid4()),
        "claim_revision": 1,
        "verdict": "independently_verified",
        "oracle_digest": DIGEST_B,
        "differences": [],
        "evidence_refs": [evidence(kind="host_receipt")],
        "verifier_id": "verifier:independent-01",
        "created_at": NOW.isoformat(),
    }
    VerificationResult.model_validate(base)

    with pytest.raises(ValidationError, match="requires expected/actual differences"):
        VerificationResult.model_validate({**base, "verdict": "verification_failed"})
    difference = VerificationDifference.model_validate(
        {
            "check_id": "oracle-check:receipt-0001",
            "expected": "One scoped host receipt.",
            "actual": "No host receipt was recorded.",
            "evidence_refs": [evidence(kind="host_receipt")],
        }
    )
    failed = VerificationResult.model_validate(
        {
            **base,
            "verdict": "verification_failed",
            "differences": [difference.model_dump(mode="json")],
        }
    )
    assert failed.oracle_digest == DIGEST_B
    assert set(failed.model_dump(mode="json")) & {"oracle", "oracle_path"} == set()

    leaked = {**base, "oracle_path": "/hidden-oracle/answers.json"}
    with pytest.raises(ValidationError, match="forbidden sensitive field"):
        VerificationResult.model_validate(leaked)


def test_lease_and_cursor_encode_owner_revision_and_monotonic_bounds() -> None:
    lease = DeveloperLease.model_validate(
        {
            "lease_id": str(uuid4()),
            "report_id": str(uuid4()),
            "report_revision": 5,
            "owner_id": "codex:worker-01",
            "status": "active",
            "revision": 2,
            "acquired_at": NOW.isoformat(),
            "heartbeat_at": (NOW + timedelta(minutes=5)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        }
    )
    assert lease.report_revision == 5
    assert lease.owner_id == "codex:worker-01"

    with pytest.raises(ValidationError, match="later than heartbeat"):
        DeveloperLease.model_validate(
            {
                **lease.model_dump(mode="json"),
                "expires_at": lease.heartbeat_at.isoformat(),
            }
        )

    cursor = ReaderCursor.model_validate(
        {
            "channel_id": str(uuid4()),
            "reader_role": "lilies",
            "reader_id": "lilies:session-01",
            "ack_seq": 101,
            "revision": 7,
            "updated_at": NOW.isoformat(),
        }
    )
    assert cursor.ack_seq == 101


def test_mutation_requests_carry_idempotency_and_compare_and_set() -> None:
    report = CollaborationReportPayload.model_validate(report_payload())
    request = ReportSubmitRequest.model_validate(
        {
            "idempotency_key": IDEMPOTENCY_KEY,
            "expected_channel_revision": 2,
            "report": report.model_dump(mode="json"),
        }
    )
    assert request.expected_channel_revision == 2

    with pytest.raises(ValidationError, match="explicit confirmation"):
        ChannelSettingsRequest.model_validate(
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "expected_channel_revision": 2,
                "approval_mode": "auto_forward",
                "confirmed": False,
            }
        )
    ChannelSettingsRequest.model_validate(
        {
            "idempotency_key": IDEMPOTENCY_KEY,
            "expected_channel_revision": 2,
            "approval_mode": "auto_forward",
            "confirmed": True,
        }
    )

    response = developer_response_payload()
    sender_response = {
        key: value
        for key, value in response.items()
        if key not in {"channel_id", "report_id", "report_revision", "created_at"}
    }
    DeveloperResponseRequest.model_validate(
        {
            "idempotency_key": IDEMPOTENCY_KEY,
            "lease_id": str(uuid4()),
            "lease_owner_id": "codex:worker-01",
            "expected_report_revision": 5,
            "response": sender_response,
        }
    )
    for server_field in ("channel_id", "report_id", "report_revision", "created_at"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DeveloperResponseRequest.model_validate(
                {
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "lease_id": str(uuid4()),
                    "lease_owner_id": "codex:worker-01",
                    "expected_report_revision": 5,
                    "response": {
                        **sender_response,
                        server_field: response[server_field],
                    },
                }
            )

    with pytest.raises(ValidationError, match="greater than or equal to 60"):
        LeaseAcquireRequest.model_validate(
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "expected_report_revision": 5,
                "owner_id": "codex:worker-01",
                "ttl_seconds": 30,
            }
        )


def test_lilies_reprobe_requires_contiguous_black_box_steps() -> None:
    payload = {
        "reprobe_id": str(uuid4()),
        "channel_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_revision": 7,
        "outcome": "lilies_verified",
        "contract_digest": DIGEST_B,
        "steps": [
            {
                "order": 2,
                "action": "Run the public fixture through the refreshed contract.",
                "expected": "The generic capability returns immutable evidence.",
            }
        ],
        "expected": "The public black-box operation succeeds.",
        "actual": "The public black-box operation succeeded.",
        "evidence_refs": [evidence()],
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValidationError, match="contiguous from one"):
        LiliesReprobeResult.model_validate(payload)
    payload["steps"][0]["order"] = 1
    result = LiliesReprobeResult.model_validate(payload)
    assert result.report_revision == 7


def test_approval_request_rejects_stale_shape_and_missing_reason() -> None:
    with pytest.raises(ValidationError, match="require reason"):
        ApprovalDecisionRequest.model_validate(
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "expected_report_revision": 3,
                "decision": "needs_more_evidence",
            }
        )
