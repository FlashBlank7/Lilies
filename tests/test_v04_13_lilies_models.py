from __future__ import annotations

import copy
import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

import agent_platform.lilies_models as lilies_models
from agent_platform.lilies_models import (
    AssignmentConstraints,
    AssignmentMode,
    AssignmentNetworkPolicy,
    BuildAssignment,
    CredentialKind,
    CredentialProvisionRequest,
    CredentialRevokeRequest,
    DaemonStatus,
    DaemonStopRequest,
    LocalScope,
    PairingCodeCreateRequest,
    PairingExchangeRequest,
    PermissionDecisionRequest,
    PlatformScope,
    ProhibitedAction,
    SessionCreateRequest,
    SessionMessageRequest,
    SessionStatus,
)


DIGEST = "sha256:" + "a" * 64
IDEMPOTENCY_KEY = "request-1234567890"
CREATED_AT = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)
DEADLINE_AT = CREATED_AT + timedelta(hours=2)


def test_daemon_status_provider_credential_state_is_backward_compatible() -> None:
    payload = {
        "schema_version": "1.0",
        "pid": 42,
        "address": "http://127.0.0.1:8765",
        "started_at": CREATED_AT.isoformat(),
        "daemon_fingerprint": DIGEST,
        "client_id": str(uuid4()),
        "client_scopes": [LocalScope.session_read.value],
        "client_expires_at": DEADLINE_AT.isoformat(),
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "model_egress_enabled": False,
        "paired_client_count": 1,
        "platform_paired": True,
        "active_session_count": 0,
        "active_assignment_count": 0,
        "stopping": False,
    }

    assert (
        DaemonStatus.model_validate(payload).provider_credential_loaded is False
    )
    assert (
        DaemonStatus.model_validate(
            {**payload, "provider_credential_loaded": True}
        ).provider_credential_loaded
        is True
    )


def assignment_payload(*, mode: str = "customer") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "assignment_id": str(uuid4()),
        "idempotency_key": "assignment-1234567890",
        "mode": mode,
        "requirement": "Build the requested enterprise intake and reconciliation workflow.",
        "business_context": {
            "customer_roles": ["operations manager"],
            "business_goal": "Reconcile approved documents with inventory records.",
            "inputs": ["approved document metadata"],
            "outputs": ["reconciliation workbook"],
            "constraints": ["human approval before ambiguous writes"],
        },
        "target": {"mode": "create_new"},
        "platform": {
            "base_url": "http://127.0.0.1:8000",
            "contract_url": "/api/v1/lilies/platform-contract",
            "contract_digest": DIGEST,
            "credential_ref": "credential:assignment-1",
            "scopes": [
                "workflow.catalog:read",
                "workflow.application:write",
                "workflow.draft:write",
            ],
            "application_ids": [],
        },
        "constraints": {
            "deadline_at": DEADLINE_AT.isoformat(),
            "max_turns": 40,
            "max_tool_calls": 200,
            "network_policy": "none",
            "allowed_hosts": [],
            "allowed_actions": [
                "platform_contract_get",
                "platform_application_create",
                "platform_draft_apply",
            ],
            "prohibited_actions": [action.value for action in ProhibitedAction],
            "no_substitute_validation": False,
        },
        "deliverables": [
            {
                "name": "reconciliation workbook",
                "description": "A customer-visible Excel workbook.",
                "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
        ],
        "created_at": CREATED_AT.isoformat(),
    }
    if mode == "formal_experiment":
        payload["task_package"] = {
            "task_id": "EXP-LILIES-001",
            "revision": 1,
            "public_summary_digest": DIGEST,
        }
        payload["fixture_refs"] = [
            {
                "artifact_id": "fixture:documents-1",
                "digest": DIGEST,
                "media_type": "application/zip",
                "display_name": "public-documents.zip",
            }
        ]
        constraints = payload["constraints"]
        assert isinstance(constraints, dict)
        constraints["max_budget_usd"] = 20
        constraints["no_substitute_validation"] = True
    return payload


def test_all_external_request_models_forbid_unknown_fields() -> None:
    request_models = [
        model
        for name, model in inspect.getmembers(lilies_models, inspect.isclass)
        if name.endswith("Request") and model.__module__ == lilies_models.__name__
    ]
    assert request_models
    assert all(model.model_config.get("extra") == "forbid" for model in request_models)

    valid_requests = [
        (
            SessionCreateRequest,
            {"idempotency_key": IDEMPOTENCY_KEY},
        ),
        (
            SessionMessageRequest,
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "message_id": str(uuid4()),
                "content": "Continue the workflow build.",
            },
        ),
        (
            PairingExchangeRequest,
            {
                "pairing_code": "ABCD-EFGH",
                "client_name": "cli:test-host",
                "requested_scopes": ["lilies.session:read"],
                "client_nonce": "A" * 22,
            },
        ),
        (
            PermissionDecisionRequest,
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "behavior": "deny",
                "expected_input_digest": DIGEST,
            },
        ),
        (
            CredentialRevokeRequest,
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "credential_ref": "credential:assignment-1",
                "reason": "assignment cancelled",
            },
        ),
        (
            DaemonStopRequest,
            {"idempotency_key": IDEMPOTENCY_KEY},
        ),
    ]
    for model, payload in valid_requests:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            model.model_validate({**payload, "unexpected": True})


def test_customer_assignment_projection_cannot_discover_collaboration() -> None:
    assignment = BuildAssignment.model_validate(assignment_payload())
    projection = assignment.model_dump(mode="json", exclude_none=True)

    assert assignment.mode == AssignmentMode.customer
    assert "collaboration" not in projection
    assert "task_package" not in projection
    assert "fixture_refs" not in projection

    explicit_null = assignment_payload()
    explicit_null["collaboration"] = None
    with pytest.raises(ValidationError, match="must completely omit collaboration"):
        BuildAssignment.model_validate(explicit_null)

    with_collaboration = assignment_payload()
    with_collaboration["collaboration"] = {
        "channel_id": str(uuid4()),
        "credential_ref": "credential:channel-1",
        "scopes": ["collaboration.report:write", "collaboration.response:read"],
        "expires_at": DEADLINE_AT.isoformat(),
    }
    with pytest.raises(ValidationError, match="must completely omit collaboration"):
        BuildAssignment.model_validate(with_collaboration)


def test_formal_assignment_requires_frozen_package_fixtures_and_budget() -> None:
    valid = BuildAssignment.model_validate(assignment_payload(mode="formal_experiment"))
    assert valid.task_package is not None
    assert valid.fixture_refs
    assert valid.constraints.max_budget_usd == 20
    assert valid.constraints.no_substitute_validation is True

    cases = [
        ("task_package", None, "requires task_package"),
        ("fixture_refs", None, "requires non-empty fixture_refs"),
    ]
    for field, value, message in cases:
        invalid = assignment_payload(mode="formal_experiment")
        invalid[field] = value
        with pytest.raises(ValidationError, match=message):
            BuildAssignment.model_validate(invalid)

    without_budget = assignment_payload(mode="formal_experiment")
    constraints = without_budget["constraints"]
    assert isinstance(constraints, dict)
    constraints.pop("max_budget_usd")
    with pytest.raises(ValidationError, match="requires max_budget_usd"):
        BuildAssignment.model_validate(without_budget)

    substitution_allowed = assignment_payload(mode="formal_experiment")
    constraints = substitution_allowed["constraints"]
    assert isinstance(constraints, dict)
    constraints["no_substitute_validation"] = False
    with pytest.raises(ValidationError, match="no_substitute_validation=true"):
        BuildAssignment.model_validate(substitution_allowed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("secret",), "plaintext-secret"),
        (("oracle_path",), "/protected/oracle.json"),
        (("business_context", "expected_answer"), {"records": 36}),
        (("fixture_refs", 0, "source_path"), "/repo/platform/backend"),
        (("platform", "access_token"), "plaintext-token"),
    ],
)
def test_assignment_rejects_secret_oracle_and_source_fields(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    payload = assignment_payload(mode="formal_experiment")
    parent: object = payload
    for segment in path[:-1]:
        assert isinstance(parent, (dict, list))
        parent = parent[segment]  # type: ignore[index]
    assert isinstance(parent, (dict, list))
    parent[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValidationError, match="forbidden sensitive field"):
        BuildAssignment.model_validate(payload)


def test_assignment_contract_url_uuid_and_idempotency_are_strict() -> None:
    wrong_contract = assignment_payload()
    platform = wrong_contract["platform"]
    assert isinstance(platform, dict)
    platform["contract_url"] = "/internal/platform-contract"
    with pytest.raises(ValidationError):
        BuildAssignment.model_validate(wrong_contract)

    short_key = assignment_payload()
    short_key["idempotency_key"] = "too-short"
    with pytest.raises(ValidationError):
        BuildAssignment.model_validate(short_key)

    invalid_uuid = assignment_payload()
    invalid_uuid["assignment_id"] = "assignment-1"
    with pytest.raises(ValidationError):
        BuildAssignment.model_validate(invalid_uuid)


def test_assignment_network_and_prohibited_action_constraints() -> None:
    allowlist_without_hosts = assignment_payload(mode="formal_experiment")
    constraints = allowlist_without_hosts["constraints"]
    assert isinstance(constraints, dict)
    constraints["network_policy"] = "allowlist"
    with pytest.raises(ValidationError, match="requires allowed_hosts"):
        BuildAssignment.model_validate(allowlist_without_hosts)

    formal_full_network = assignment_payload(mode="formal_experiment")
    constraints = formal_full_network["constraints"]
    assert isinstance(constraints, dict)
    constraints["network_policy"] = "full"
    with pytest.raises(ValidationError, match="cannot use full network"):
        BuildAssignment.model_validate(formal_full_network)

    missing_oracle_prohibition = assignment_payload()
    constraints = missing_oracle_prohibition["constraints"]
    assert isinstance(constraints, dict)
    constraints["prohibited_actions"] = [
        ProhibitedAction.read_platform_source.value,
        ProhibitedAction.write_task_package.value,
        ProhibitedAction.write_task_package.value,
    ]
    with pytest.raises(ValidationError, match="prohibited_actions"):
        BuildAssignment.model_validate(missing_oracle_prohibition)


def test_assignment_requires_utc_timestamps_and_forward_deadline() -> None:
    non_utc = assignment_payload()
    non_utc["created_at"] = "2026-07-22T10:00:00+09:00"
    with pytest.raises(ValidationError, match="must use UTC"):
        BuildAssignment.model_validate(non_utc)

    naive = assignment_payload()
    naive["created_at"] = "2026-07-22T01:00:00"
    with pytest.raises(ValidationError, match="must include a UTC timezone"):
        BuildAssignment.model_validate(naive)

    reversed_deadline = assignment_payload()
    constraints = reversed_deadline["constraints"]
    assert isinstance(constraints, dict)
    constraints["deadline_at"] = (CREATED_AT - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="deadline_at must be later"):
        BuildAssignment.model_validate(reversed_deadline)


def test_pairing_default_scopes_include_control_and_private_credentials() -> None:
    request = PairingCodeCreateRequest()
    assert request.ttl_seconds == 600
    assert set(request.allowed_scopes) == {
        LocalScope.session_read,
        LocalScope.session_write,
        LocalScope.permission_resolve,
        LocalScope.daemon_control,
        LocalScope.credential_write,
    }
    assert LocalScope.observability_read not in request.allowed_scopes
    assert LocalScope.daemon_control in request.allowed_scopes
    assert LocalScope.credential_write in request.allowed_scopes

    with pytest.raises(ValidationError, match="must not contain duplicates"):
        PairingExchangeRequest(
            pairing_code="ABCD-EFGH",
            client_name="platform",
            requested_scopes=[LocalScope.session_read, LocalScope.session_read],
            client_nonce="A" * 22,
        )

    previous_client_id = uuid4()
    previous_token = f"{previous_client_id}." + "x" * 40
    rotation = PairingExchangeRequest(
        pairing_code="ABCD-EFGH",
        client_name="cli:test",
        requested_scopes=[LocalScope.session_read],
        client_nonce="B" * 22,
        previous_client_id=previous_client_id,
        previous_access_token=previous_token,
    )
    assert rotation.previous_access_token is not None
    assert rotation.previous_access_token.get_secret_value() == previous_token
    assert previous_token not in str(rotation.model_dump(mode="json"))

    with pytest.raises(ValidationError, match="must be provided together"):
        PairingExchangeRequest(
            pairing_code="ABCD-EFGH",
            client_name="cli:test",
            requested_scopes=[LocalScope.session_read],
            client_nonce="C" * 22,
            previous_client_id=previous_client_id,
        )


def test_pairing_prepared_bearer_is_strictly_bound_to_requested_client() -> None:
    requested_client_id = uuid4()
    prepared_token = f"{requested_client_id}." + "p" * 48
    previous_client_id = uuid4()
    previous_token = f"{previous_client_id}." + "x" * 48
    request = PairingExchangeRequest(
        pairing_code="ABCD-EFGH",
        client_name="platform",
        requested_scopes=[LocalScope.session_read],
        client_nonce="D" * 22,
        requested_client_id=requested_client_id,
        prepared_access_token=prepared_token,
    )
    assert request.requested_client_id == requested_client_id
    assert request.prepared_access_token is not None
    assert request.prepared_access_token.get_secret_value() == prepared_token
    assert prepared_token not in str(request.model_dump(mode="json"))

    with pytest.raises(ValidationError, match="must be provided together"):
        PairingExchangeRequest(
            pairing_code="ABCD-EFGH",
            client_name="platform",
            requested_scopes=[LocalScope.session_read],
            client_nonce="E" * 22,
            requested_client_id=requested_client_id,
        )

    different_client_id = uuid4()
    with pytest.raises(ValidationError, match="must be bound"):
        PairingExchangeRequest(
            pairing_code="ABCD-EFGH",
            client_name="platform",
            requested_scopes=[LocalScope.session_read],
            client_nonce="F" * 22,
            requested_client_id=requested_client_id,
            prepared_access_token=f"{different_client_id}." + "q" * 48,
        )

    with pytest.raises(ValidationError, match="must equal previous_client_id"):
        PairingExchangeRequest(
            pairing_code="ABCD-EFGH",
            client_name="platform",
            requested_scopes=[LocalScope.session_read],
            client_nonce="G" * 22,
            previous_client_id=previous_client_id,
            previous_access_token=previous_token,
            requested_client_id=requested_client_id,
            prepared_access_token=prepared_token,
        )


def test_permission_decision_is_bound_to_the_exact_input_digest() -> None:
    decision = PermissionDecisionRequest(
        idempotency_key=IDEMPOTENCY_KEY,
        behavior="allow",
        expected_input_digest=DIGEST,
        updated_input={"path": "public/output.xlsx"},
    )
    assert decision.expected_input_digest == DIGEST

    with pytest.raises(ValidationError, match="only valid for an allow"):
        PermissionDecisionRequest(
            idempotency_key=IDEMPOTENCY_KEY,
            behavior="deny",
            expected_input_digest=DIGEST,
            updated_input={"path": "different.xlsx"},
        )


def test_credential_provision_secret_is_not_serialized_in_plaintext() -> None:
    request = CredentialProvisionRequest(
        idempotency_key=IDEMPOTENCY_KEY,
        credential_ref="credential:assignment-1",
        assignment_id=uuid4(),
        kind=CredentialKind.platform_assignment,
        secret="this-is-a-private-assignment-token",
        scopes=[PlatformScope.catalog_read],
        expires_at=DEADLINE_AT,
    )
    serialized = request.model_dump_json()
    assert "this-is-a-private-assignment-token" not in serialized
    assert "**********" in serialized

    with pytest.raises(ValidationError, match="cannot grant collaboration scopes"):
        CredentialProvisionRequest(
            idempotency_key=IDEMPOTENCY_KEY,
            credential_ref="credential:assignment-1",
            assignment_id=uuid4(),
            kind=CredentialKind.platform_assignment,
            secret="this-is-a-private-assignment-token",
            scopes=["collaboration.report:write"],
            expires_at=DEADLINE_AT,
        )


def test_session_status_contract_has_only_the_locked_states() -> None:
    assert {status.value for status in SessionStatus} == {
        "ready",
        "running",
        "waiting_permission",
        "waiting_collaboration",
        "interrupted",
        "error",
        "cancelled",
        "completed",
        "closed",
    }


def test_constraint_model_rejects_hosts_outside_allowlist_mode() -> None:
    payload = assignment_payload()["constraints"]
    assert isinstance(payload, dict)
    payload = copy.deepcopy(payload)
    payload["allowed_hosts"] = ["paperless.local"]
    with pytest.raises(ValidationError, match="only valid with allowlist"):
        AssignmentConstraints.model_validate(payload)

    assert AssignmentNetworkPolicy.none.value == "none"
