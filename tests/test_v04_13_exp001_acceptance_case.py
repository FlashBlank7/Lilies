from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.experiments.exp_lilies_001 import run_acceptance_case as adapter


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
APPLICATION_ID = "81eb87b2-8638-4399-a692-9c3da8d0d318"
ASSIGNMENT_ID = "5a4d5cb8-ef96-42b2-872a-99c4d444e18e"
SESSION_ID = "e6cde66d-0848-445c-86fc-646b8fdb14fb"
RUN_ID = "3a0d93c8-6755-41f9-bf91-91847ce1e3e8"
CHANNEL_ID = "af2603fd-6cb4-4168-af99-eeb978ce910f"
TOKEN = "owner-token-that-never-enters-the-receipt"
SIGNING_KEY = b"receipt-signing-key-with-at-least-thirty-two-bytes"
PUBLIC_RECORD = {
    "record_id": "PUBLIC-001",
    "source_id": "DOC-PUBLIC-001",
    "supplier": "Example Supplier",
    "purchase_order": "PO-PUBLIC-001",
    "part_number": "PART-001",
    "lot_number": "LOT-001",
    "quantity": 7,
    "document_date": "2026-01-01",
    "certificate_type": "QUALITY CERTIFICATE",
    "ocr_confidence": 0.98,
}
WORKFLOW_INPUTS = {"records": [PUBLIC_RECORD], "run_label": "formal"}


def _config(tmp_path: Path, **changes: Any) -> adapter.CaseConfig:
    values: dict[str, Any] = {
        "platform_url": "http://127.0.0.1:8765",
        "state_root": tmp_path / "environment",
        "application_id": APPLICATION_ID,
        "assignment_id": ASSIGNMENT_ID,
        "session_id": SESSION_ID,
        "version": 2,
        "content_hash": DIGEST,
        "seed": "101",
        "timeout_seconds": 2.0,
        "max_resume_count": 20,
    }
    values.update(changes)
    return adapter.CaseConfig(**values)


def _artifact(filename: str) -> dict[str, Any]:
    return {
        "relative_path": f".workflow-run-artifacts/{RUN_ID}/artifacts/{filename}",
        "filename": filename,
        "media_type": adapter.REQUIRED_ARTIFACTS[filename],
        "size_bytes": 321,
        "sha256": DIGEST,
        "replayed": False,
        "lineage": {"protected_value": "must-not-enter-projection"},
    }


def _artifact_event(filename: str, *, node_id: str = "artifact-node") -> dict[str, Any]:
    artifact = _artifact(filename)
    return {
        "type": "artifact.created",
        "data": {
            "node_id": node_id,
            **{
                key: artifact[key]
                for key in (
                    "relative_path",
                    "media_type",
                    "size_bytes",
                    "sha256",
                    "replayed",
                )
            },
        },
    }


def _run(
    *,
    status: str = "succeeded",
    fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "application_id": APPLICATION_ID,
        "version": 2,
        "status": status,
        "outputs": {
            "deliverables": [
                _artifact("enterprise-result.json"),
                _artifact("reconciliation.xlsx"),
            ],
            "protected_record": "HIDDEN-RECORD-MUST-NOT-LEAK",
        },
        "state": {
            "waiting_node_id": "iteration[0].review",
            "snapshot": {
                "workflow": {
                    "nodes": [
                        {
                            "id": "review",
                            "type": "human_input",
                            "config": {
                                "fields": fields
                                or [
                                    {
                                        "name": "decision",
                                        "type": "string",
                                        "required": True,
                                        "options": ["approve", "hold_for_review"],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        },
    }


def _guard() -> dict[str, Any]:
    return {
        "application_id": APPLICATION_ID,
        "active_version": 2,
        "published_content_hash": DIGEST,
        "draft_revision": 63,
        "draft_content_hash": DIGEST,
        "version_inventory_digest": OTHER_DIGEST,
        "version_count": 2,
    }


def _handoff() -> dict[str, Any]:
    return {
        "assignment_id": ASSIGNMENT_ID,
        "session_id": SESSION_ID,
        "phase": "completed",
        "status": "completed",
        "daemon_status": "completed",
        "lilies_discovery_status": "unavailable",
    }


def _formal_version(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "application_id": APPLICATION_ID,
        "version": 2,
        "content_hash": "a" * 64,
        "publication_decision": {
            "application_id": APPLICATION_ID,
            "execution_policy_snapshot": {
                "assignment_id": ASSIGNMENT_ID,
                "session_id": SESSION_ID,
                "allowed_nested_application_ids": [APPLICATION_ID],
                "policy_digest": OTHER_DIGEST,
            },
        },
    }
    value.update(changes)
    return value


def _raise_platform_not_found() -> None:
    try:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8765/redacted",
            404,
            "not found",
            {},
            None,
        )
    except urllib.error.HTTPError as error:
        raise adapter.AcceptanceCaseError("platform_request_failed") from error


def _host_verification(*, passed: bool = True) -> dict[str, Any]:
    return {
        "verdict": "independently_verified" if passed else "verification_failed",
        "check_count": 39,
        "passed_check_count": 39 if passed else 31,
        "difference_count": 0 if passed else 8,
        "record_binding_gate_count": 36,
        "fault_gate_count": 72,
        "snapshot_digest": DIGEST,
        "oracle_digest": OTHER_DIGEST,
        "result_digest": DIGEST,
        "duration_seconds": 0.1,
    }


def _private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _stub_successful_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter, "_application_guard", lambda *_: _guard())
    monkeypatch.setattr(adapter, "_builder_handoff_guard", lambda *_: _handoff())
    monkeypatch.setattr(
        adapter,
        "_environment_command",
        lambda _config, _private, command, *_extra: {
            "command": command,
            "duration_seconds": 0.01,
            "status": "passed",
        },
    )
    monkeypatch.setattr(adapter, "_install_host_secrets", lambda *_: 3)
    monkeypatch.setattr(
        adapter,
        "_workflow_inputs_command",
        lambda *_: (
            WORKFLOW_INPUTS,
            {
                "command": "workflow-input",
                "duration_seconds": 0.01,
                "status": "passed",
            },
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_execute_workflow",
        lambda *_: {
            "run": _run(),
            "receipt": {
                "run_id": RUN_ID,
                "last_status": "succeeded",
                "terminal_status": "succeeded",
                "resume_count": 12,
                "version": 2,
                "cancel_attempted": False,
                "cancel_result": "not_required",
            },
        },
    )
    monkeypatch.setattr(
        adapter,
        "_trace_projection",
        lambda *_: (
            {
                "event_count": 80,
                "identity_count": 2,
                "identities": [
                    {
                        "event_type": "node.started",
                        "node_id_digest": DIGEST,
                        "count": 12,
                    }
                ],
                "identity_digest": DIGEST,
            },
            [
                {
                    key: value
                    for key, value in _artifact(name).items()
                    if key
                    in {
                        "filename",
                        "media_type",
                        "size_bytes",
                        "sha256",
                        "replayed",
                    }
                }
                | {"node_id_digest": OTHER_DIGEST}
                for name in adapter.REQUIRED_ARTIFACTS
            ],
        ),
    )
    monkeypatch.setattr(
        adapter, "_run_host_verifier", lambda *_: _host_verification()
    )


def test_receipt_signature_detects_tampering() -> None:
    receipt = adapter.sign_receipt({"status": "passed", "count": 4}, SIGNING_KEY)
    assert adapter.verify_receipt(receipt, SIGNING_KEY)
    receipt["count"] = 5
    assert not adapter.verify_receipt(receipt, SIGNING_KEY)


def test_private_token_and_signing_key_use_0600_files(tmp_path: Path) -> None:
    secrets_file = tmp_path / "runner-secrets.json"
    _private_json(
        secrets_file,
        {
            "platform_api_token": TOKEN,
            "collaborative_development_signing_key": SIGNING_KEY.decode(),
        },
    )
    assert adapter._platform_token(secrets_file) == TOKEN
    assert adapter._receipt_signing_key(secrets_file) == SIGNING_KEY

    secrets_file.chmod(0o644)
    with pytest.raises(adapter.AcceptanceCaseError, match="private_input_rejected"):
        adapter._platform_token(secrets_file)


def test_private_secret_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text(TOKEN, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(adapter.AcceptanceCaseError, match="private_input_rejected"):
        adapter._platform_token(link)


def test_workflow_input_bridge_reads_only_allowlisted_business_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = {
        **PUBLIC_RECORD,
        "lot_number": None,
        "document_date": None,
        "certificate_type": None,
        "purchase_order": None,
        "quantity": None,
    }
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    observed: dict[str, Any] = {}

    def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> tuple[int, float]:
        observed["argv"] = argv
        output = Path(argv[argv.index("--output") + 1])
        _private_json(
            output,
            {"records": [record], "run_label": "formal"},
        )
        return 0, 0.25

    monkeypatch.setattr(adapter, "_protected_run", fake_run)
    inputs, command = adapter._workflow_inputs_command(
        _config(tmp_path),
        private_root,
    )
    assert "workflow-input" in observed["argv"]
    assert observed["argv"][observed["argv"].index("--seed") + 1] == "101"
    assert inputs == {"records": [record], "run_label": "formal"}
    assert set(inputs["records"][0]) == set(adapter.WORKFLOW_RECORD_FIELDS)
    assert command == {
        "command": "workflow-input",
        "duration_seconds": 0.25,
        "status": "passed",
    }


@pytest.mark.parametrize(
    "mutation",
    ["extra_record_hint", "extra_top_level_hint", "missing_business_field"],
)
def test_workflow_input_bridge_rejects_non_allowlisted_or_incomplete_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    record = dict(PUBLIC_RECORD)
    value: dict[str, Any] = {"records": [record], "run_label": "formal"}
    if mutation == "extra_record_hint":
        record["scenario"] = "PROTECTED-SCENARIO"
    elif mutation == "extra_top_level_hint":
        value["expected"] = "PROTECTED-EXPECTED"
    else:
        record.pop("supplier")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> tuple[int, float]:
        output = Path(argv[argv.index("--output") + 1])
        _private_json(output, value)
        return 0, 0.1

    monkeypatch.setattr(adapter, "_protected_run", fake_run)
    with pytest.raises(adapter.AcceptanceCaseError, match="workflow_input_rejected") as error:
        adapter._workflow_inputs_command(_config(tmp_path), private_root)
    assert "PROTECTED" not in str(error.value)


def test_conservative_resume_values_never_choose_approval() -> None:
    run = _run(
        fields=[
            {
                "name": "decision",
                "type": "string",
                "required": True,
                "options": ["approve", "manual_review"],
            },
            {"name": "approved", "type": "boolean", "required": True},
            {"name": "notes", "type": "string", "required": True},
            {"name": "optional", "type": "string", "required": False},
        ]
    )
    assert adapter.conservative_resume_values(run) == {
        "decision": "manual_review",
        "approved": False,
        "notes": "held_for_manual_review",
    }


@pytest.mark.parametrize(
    "field",
    [
        {
            "name": "decision",
            "type": "string",
            "required": True,
            "options": ["approve", "write"],
        },
        {"name": "upload", "type": "file", "required": True},
    ],
)
def test_unsafe_human_input_schema_fails_closed(field: dict[str, Any]) -> None:
    with pytest.raises(adapter.AcceptanceCaseError, match="human_input_schema_rejected"):
        adapter.conservative_resume_values(_run(fields=[field]))


def test_artifacts_require_trace_binding_and_expose_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events = [_artifact_event(name) for name in adapter.REQUIRED_ARTIFACTS]
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: events)
    trace, artifacts = adapter._trace_projection(
        _config(tmp_path), TOKEN, RUN_ID, _run()["outputs"]
    )
    encoded = json.dumps({"trace": trace, "artifacts": artifacts})
    assert [item["filename"] for item in artifacts] == [
        "enterprise-result.json",
        "reconciliation.xlsx",
    ]
    assert all("relative_path" not in item for item in artifacts)
    assert all("node_id" not in item for item in artifacts)
    assert "protected_value" not in encoded
    assert "HIDDEN-RECORD" not in encoded


def test_artifact_shaped_output_without_authoritative_trace_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: [])
    with pytest.raises(adapter.AcceptanceCaseError, match="artifact_trace_binding_failed"):
        adapter._trace_projection(
            _config(tmp_path), TOKEN, RUN_ID, _run()["outputs"]
        )


def test_artifact_trace_path_must_match_exact_run_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events = [_artifact_event(name) for name in adapter.REQUIRED_ARTIFACTS]
    events[0]["data"]["relative_path"] = "../enterprise-result.json"
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: events)
    with pytest.raises(adapter.AcceptanceCaseError, match="artifact_projection_rejected"):
        adapter._trace_projection(
            _config(tmp_path), TOKEN, RUN_ID, _run()["outputs"]
        )


def test_trace_projection_discards_payload_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_node_id = "protected-business-node-name"
    events = [
        {
            "type": "node.completed",
            "data": {
                "node_id": raw_node_id,
                "outputs": {"record": "PROTECTED-TRACE-VALUE"},
            },
        },
        {"type": "workflow.completed", "data": {"protected": "VALUE"}},
        *[_artifact_event(name) for name in adapter.REQUIRED_ARTIFACTS],
    ]
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: events)
    projection, _artifacts = adapter._trace_projection(
        _config(tmp_path), TOKEN, RUN_ID, _run()["outputs"]
    )
    encoded = json.dumps(projection)
    assert projection["event_count"] == 4
    assert "PROTECTED-TRACE-VALUE" not in encoded
    assert "outputs" not in encoded
    assert raw_node_id not in encoded
    assert adapter._digest(raw_node_id.encode()) in encoded


def test_application_guard_binds_active_version_and_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            {
                "id": APPLICATION_ID,
                "active_version": 2,
                "draft_revision": 63,
                "content_hash": "a" * 64,
            },
            [
                {"version": 2, "content_hash": "a" * 64},
                {"version": 1, "content_hash": OTHER_DIGEST},
            ],
            {
                "application_id": APPLICATION_ID,
                "source": "published",
                "version": 2,
                "content_hash": "a" * 64,
            },
        ]
    )
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: next(responses))
    guard = adapter._application_guard(_config(tmp_path), TOKEN)
    assert guard["published_content_hash"] == DIGEST
    assert guard["draft_content_hash"] == DIGEST
    assert guard["version_content_hash"] == DIGEST
    assert guard["runtime_content_hash"] == DIGEST
    assert guard["version_count"] == 2


@pytest.mark.parametrize("surface", ["application", "version", "runtime"])
def test_application_guard_rejects_invalid_public_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    application = {
        "id": APPLICATION_ID,
        "active_version": 2,
        "draft_revision": 63,
        "content_hash": DIGEST,
    }
    versions = [{"version": 2, "content_hash": DIGEST}]
    runtime = {
        "application_id": APPLICATION_ID,
        "source": "published",
        "version": 2,
        "content_hash": DIGEST,
    }
    if surface == "application":
        application["content_hash"] = "A" * 64
    elif surface == "version":
        versions[0]["content_hash"] = "sha256:short"
    else:
        runtime["content_hash"] = "sha512:" + "a" * 64
    responses = iter([application, versions, runtime])
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: next(responses))
    with pytest.raises(
        adapter.AcceptanceCaseError, match="published_version_guard_failed"
    ):
        adapter._application_guard(_config(tmp_path), TOKEN)


def test_builder_handoff_requires_completed_assignment_and_unavailable_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            {
                "assignment_id": ASSIGNMENT_ID,
                "application_id": APPLICATION_ID,
                "session_id": SESSION_ID,
                "phase": "completed",
                "status": "completed",
                "daemon_status": "completed",
            },
            {"discovery": {"status": "unavailable"}},
        ]
    )
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: next(responses))
    assert adapter._builder_handoff_guard(_config(tmp_path), TOKEN) == _handoff()


@pytest.mark.parametrize(
    "assignment_status,discovery_status",
    [("running", "unavailable"), ("completed", "available")],
)
def test_builder_handoff_rejects_live_or_incomplete_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    assignment_status: str,
    discovery_status: str,
) -> None:
    responses = iter(
        [
            {
                "assignment_id": ASSIGNMENT_ID,
                "application_id": APPLICATION_ID,
                "session_id": SESSION_ID,
                "phase": assignment_status,
                "status": assignment_status,
                "daemon_status": assignment_status,
            },
            {"discovery": {"status": discovery_status}},
        ]
    )
    monkeypatch.setattr(adapter, "_platform_json", lambda *_: next(responses))
    with pytest.raises(adapter.AcceptanceCaseError, match="builder_handoff_guard_failed"):
        adapter._builder_handoff_guard(_config(tmp_path), TOKEN)


def test_builder_handoff_accepts_one_closed_formal_collaboration_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        assert method == "GET"
        if path.endswith(f"/assignments/{ASSIGNMENT_ID}"):
            _raise_platform_not_found()
        if path == "/api/v1/local-lilies/status":
            return {"discovery": {"status": "unavailable"}}
        if path == "/api/v1/studio/collaboration/channels?limit=500":
            return {
                "channels": [
                    {
                        "channel_id": CHANNEL_ID,
                        "task_id": adapter.TASK_ID,
                        "task_revision": adapter.REVISION,
                        "assignment_id": ASSIGNMENT_ID,
                        "lilies_session_id": SESSION_ID,
                        "application_ids": [APPLICATION_ID],
                        "status": "closed",
                    }
                ]
            }
        if path == f"/api/v1/applications/{APPLICATION_ID}/versions":
            return [_formal_version()]
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    guard = adapter._builder_handoff_guard(_config(tmp_path), TOKEN)
    assert guard == {
        "assignment_id": ASSIGNMENT_ID,
        "session_id": SESSION_ID,
        "phase": "closed",
        "status": "closed",
        "channel_id": CHANNEL_ID,
        "channel_status": "closed",
        "handoff_route": "formal_collaboration",
        "execution_policy_digest": OTHER_DIGEST,
        "lilies_discovery_status": "unavailable",
    }


@pytest.mark.parametrize("mutation", ["active", "wrong_session", "wrong_app"])
def test_formal_builder_handoff_rejects_open_or_misbound_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    channel = {
        "channel_id": CHANNEL_ID,
        "task_id": adapter.TASK_ID,
        "task_revision": adapter.REVISION,
        "assignment_id": ASSIGNMENT_ID,
        "lilies_session_id": SESSION_ID,
        "application_ids": [APPLICATION_ID],
        "status": "closed",
    }
    if mutation == "active":
        channel["status"] = "active"
    elif mutation == "wrong_session":
        channel["lilies_session_id"] = RUN_ID
    else:
        channel["application_ids"] = [RUN_ID]

    def fake_platform(
        _method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        if path.endswith(f"/assignments/{ASSIGNMENT_ID}"):
            _raise_platform_not_found()
        if path == "/api/v1/local-lilies/status":
            return {"discovery": {"status": "unavailable"}}
        if path == "/api/v1/studio/collaboration/channels?limit=500":
            return {"channels": [channel]}
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(adapter.AcceptanceCaseError, match="builder_handoff_guard_failed"):
        adapter._builder_handoff_guard(_config(tmp_path), TOKEN)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_policy_assignment",
        "wrong_policy_session",
        "wrong_policy_app",
        "wrong_version_app",
        "wrong_decision_app",
    ],
)
def test_formal_builder_handoff_rejects_misbound_published_execution_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    version = _formal_version()
    decision = version["publication_decision"]
    policy = decision["execution_policy_snapshot"]
    if mutation == "wrong_policy_assignment":
        policy["assignment_id"] = RUN_ID
    elif mutation == "wrong_policy_session":
        policy["session_id"] = RUN_ID
    elif mutation == "wrong_policy_app":
        policy["allowed_nested_application_ids"] = [RUN_ID]
    elif mutation == "wrong_version_app":
        version["application_id"] = RUN_ID
    else:
        decision["application_id"] = RUN_ID

    def fake_platform(
        _method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        if path.endswith(f"/assignments/{ASSIGNMENT_ID}"):
            _raise_platform_not_found()
        if path == "/api/v1/local-lilies/status":
            return {"discovery": {"status": "unavailable"}}
        if path == "/api/v1/studio/collaboration/channels?limit=500":
            return {
                "channels": [
                    {
                        "channel_id": CHANNEL_ID,
                        "task_id": adapter.TASK_ID,
                        "task_revision": adapter.REVISION,
                        "assignment_id": ASSIGNMENT_ID,
                        "lilies_session_id": SESSION_ID,
                        "application_ids": [APPLICATION_ID],
                        "status": "closed",
                    }
                ]
            }
        if path == f"/api/v1/applications/{APPLICATION_ID}/versions":
            return [version]
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(adapter.AcceptanceCaseError, match="builder_handoff_guard_failed"):
        adapter._builder_handoff_guard(_config(tmp_path), TOKEN)


def test_builder_handoff_does_not_treat_non_404_assignment_error_as_formal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise adapter.AcceptanceCaseError("platform_request_failed")

    monkeypatch.setattr(adapter, "_platform_json", unavailable)
    with pytest.raises(adapter.AcceptanceCaseError, match="platform_request_failed"):
        adapter._builder_handoff_guard(_config(tmp_path), TOKEN)


def test_workflow_resumes_every_pause_with_schema_derived_hold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    responses = [_run(status="paused"), _run(status="paused"), _run()]
    resumes: list[dict[str, Any]] = []
    created_bodies: list[dict[str, Any]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        body: Any | None = None,
    ) -> Any:
        if method == "POST" and path.endswith("/runs"):
            created_bodies.append(body)
            return {"run_id": RUN_ID, "version": 2, "status": "queued"}
        if method == "GET":
            return responses.pop(0)
        if method == "POST" and path.endswith("/resume"):
            resumes.append(body)
            return {"status": "running"}
        raise AssertionError((method, path))

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    outcome = adapter._execute_workflow(_config(tmp_path), TOKEN, WORKFLOW_INPUTS)
    assert outcome["receipt"]["terminal_status"] == "succeeded"
    assert outcome["receipt"]["resume_count"] == 2
    assert created_bodies == [
        {"inputs": WORKFLOW_INPUTS, "version": 2, "workspace_path": "."}
    ]
    assert resumes == [
        {"values": {"decision": "hold_for_review"}},
        {"values": {"decision": "hold_for_review"}},
    ]


def test_abnormal_workflow_poll_attempts_public_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        calls.append((method, path))
        if method == "POST" and path.endswith("/runs"):
            return {"run_id": RUN_ID, "version": 2, "status": "queued"}
        if method == "GET":
            raise adapter.AcceptanceCaseError("platform_request_failed")
        return {"run_id": RUN_ID, "status": "cancelling"}

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(
        adapter.WorkflowExecutionError, match="platform_request_failed"
    ) as captured:
        adapter._execute_workflow(_config(tmp_path), TOKEN, WORKFLOW_INPUTS)
    assert ("POST", f"/api/v1/runs/{RUN_ID}/cancel") in calls
    assert captured.value.run_receipt == {
        "run_id": RUN_ID,
        "run_id_digest": adapter._opaque_value_digest(RUN_ID),
        "observed_version": 2,
        "observed_version_digest": adapter._opaque_value_digest(2),
        "created_status": "queued",
        "created_status_digest": adapter._opaque_value_digest("queued"),
        "last_status": "queued",
        "terminal_status": None,
        "resume_count": 0,
        "version": 2,
        "cancel_attempted": True,
        "cancel_result": "cancelling",
    }


def test_created_version_mismatch_is_cancelled_and_preserved_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        calls.append((method, path))
        if method == "POST" and path.endswith("/runs"):
            return {"run_id": RUN_ID, "version": 3, "status": "queued"}
        if method == "POST" and path.endswith("/cancel"):
            return {"run_id": RUN_ID, "status": "cancelling"}
        raise AssertionError((method, path))

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(
        adapter.WorkflowExecutionError, match="workflow_version_mismatch"
    ) as captured:
        adapter._execute_workflow(_config(tmp_path), TOKEN, WORKFLOW_INPUTS)

    assert calls == [
        ("POST", f"/api/v1/applications/{APPLICATION_ID}/runs"),
        ("POST", f"/api/v1/runs/{RUN_ID}/cancel"),
    ]
    assert captured.value.run_receipt["run_id"] == RUN_ID
    assert captured.value.run_receipt["observed_version"] == 3
    assert captured.value.run_receipt["created_status"] == "queued"
    assert captured.value.run_receipt["cancel_attempted"] is True
    assert captured.value.run_receipt["cancel_result"] == "cancelling"


def test_malformed_created_run_identity_is_hashed_without_unsafe_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    malformed = "Bearer PROTECTED malformed/run"
    calls: list[tuple[str, str]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        calls.append((method, path))
        return {"run_id": malformed, "version": 2, "status": "queued"}

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(
        adapter.WorkflowExecutionError, match="workflow_start_rejected"
    ) as captured:
        adapter._execute_workflow(_config(tmp_path), TOKEN, WORKFLOW_INPUTS)

    receipt = captured.value.run_receipt
    assert calls == [("POST", f"/api/v1/applications/{APPLICATION_ID}/runs")]
    assert receipt["run_id"] is None
    assert receipt["run_id_digest"] == adapter._opaque_value_digest(malformed)
    assert receipt["observed_version"] == 2
    assert receipt["created_status"] == "queued"
    assert receipt["cancel_attempted"] is False
    assert receipt["cancel_result"] == "unsafe_run_identity"
    assert malformed not in json.dumps(receipt)


def test_non_mapping_creation_response_emits_hashed_workflow_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    protected_response = ["Bearer PROTECTED creation payload"]
    calls: list[tuple[str, str]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        _body: Any | None = None,
    ) -> Any:
        calls.append((method, path))
        return protected_response

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(
        adapter.WorkflowExecutionError, match="workflow_start_rejected"
    ) as captured:
        adapter._execute_workflow(_config(tmp_path), TOKEN, WORKFLOW_INPUTS)

    receipt = captured.value.run_receipt
    encoded = json.dumps(receipt)
    assert calls == [("POST", f"/api/v1/applications/{APPLICATION_ID}/runs")]
    assert receipt["created_response_type"] == "array"
    assert receipt["created_response_digest"] == adapter._opaque_value_digest(
        protected_response
    )
    assert receipt["run_id"] is None
    assert receipt["observed_version"] is None
    assert receipt["created_status"] == "unknown"
    assert receipt["cancel_attempted"] is False
    assert receipt["cancel_result"] == "unsafe_run_identity"
    assert protected_response[0] not in encoded


def test_host_verifier_raw_differences_remain_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(mode=0o700)

    def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> tuple[int, float]:
        output = Path(argv[argv.index("--output") + 1])
        _private_json(
            output,
            {
                "task_id": adapter.TASK_ID,
                "revision": adapter.REVISION,
                "seed": config.seed,
                "verdict": "verification_failed",
                "check_count": 39,
                "passed_check_count": 31,
                "record_binding_gate_count": 36,
                "fault_gate_count": 72,
                "snapshot_digest": DIGEST,
                "oracle_digest": OTHER_DIGEST,
                "differences": [
                    {"expected": "PROTECTED-EXPECTED", "actual": "PROTECTED-ACTUAL"}
                    for _ in range(8)
                ],
            },
        )
        return 3, 0.2

    monkeypatch.setattr(adapter, "_protected_run", fake_run)
    with adapter.tempfile.TemporaryDirectory(dir=config.state_root) as private:
        result = adapter._run_host_verifier(config, Path(private))
    encoded = json.dumps(result)
    assert result["difference_count"] == 8
    assert "PROTECTED-EXPECTED" not in encoded
    assert "PROTECTED-ACTUAL" not in encoded


@pytest.mark.parametrize(
    "verdict,passed_count,differences,returncode",
    [
        ("independently_verified", 38, [], 0),
        ("independently_verified", 39, [{"mismatch": True}], 0),
        ("verification_failed", 39, [], 3),
    ],
)
def test_host_verifier_rejects_internally_inconsistent_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict: str,
    passed_count: int,
    differences: list[dict[str, Any]],
    returncode: int,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(mode=0o700)

    def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> tuple[int, float]:
        output = Path(argv[argv.index("--output") + 1])
        _private_json(
            output,
            {
                "task_id": adapter.TASK_ID,
                "revision": adapter.REVISION,
                "seed": config.seed,
                "verdict": verdict,
                "check_count": 39,
                "passed_check_count": passed_count,
                "record_binding_gate_count": 36,
                "fault_gate_count": 72,
                "snapshot_digest": DIGEST,
                "oracle_digest": OTHER_DIGEST,
                "differences": differences,
            },
        )
        return returncode, 0.1

    monkeypatch.setattr(adapter, "_protected_run", fake_run)
    with adapter.tempfile.TemporaryDirectory(dir=config.state_root) as private:
        with pytest.raises(
            adapter.AcceptanceCaseError, match="host_verifier_output_rejected"
        ):
            adapter._run_host_verifier(config, Path(private))


@pytest.mark.parametrize(
    "record_binding_gate_count,fault_gate_count",
    [(0, 72), (36, 0)],
)
def test_host_verifier_rejects_success_without_both_gate_families(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_binding_gate_count: int,
    fault_gate_count: int,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(mode=0o700)

    def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> tuple[int, float]:
        output = Path(argv[argv.index("--output") + 1])
        _private_json(
            output,
            {
                "task_id": adapter.TASK_ID,
                "revision": adapter.REVISION,
                "seed": config.seed,
                "verdict": "independently_verified",
                "check_count": 39,
                "passed_check_count": 39,
                "record_binding_gate_count": record_binding_gate_count,
                "fault_gate_count": fault_gate_count,
                "snapshot_digest": DIGEST,
                "oracle_digest": OTHER_DIGEST,
                "differences": [],
            },
        )
        return 0, 0.1

    monkeypatch.setattr(adapter, "_protected_run", fake_run)
    with adapter.tempfile.TemporaryDirectory(dir=config.state_root) as private:
        with pytest.raises(
            adapter.AcceptanceCaseError, match="host_verifier_output_rejected"
        ):
            adapter._run_host_verifier(config, Path(private))


def test_host_secret_rotation_uses_owner_api_without_reporting_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(mode=0o700)
    _private_json(config.state_root / "secrets.json", {"attestation_secret": "s" * 40})
    _private_json(
        config.state_root / "credentials.json",
        {
            "paperless_builder_token": "p" * 40,
            "inventree_builder_token": "i" * 40,
            "paperless_verifier_token": "v" * 40,
            "inventree_verifier_token": "w" * 40,
        },
    )
    requests: list[dict[str, Any]] = []

    def fake_platform(
        method: str,
        _config: adapter.CaseConfig,
        _token: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        assert method == "POST"
        assert path == "/api/v1/platform/secrets"
        requests.append(body)
        return {
            "owner_id": body["owner_id"],
            "name": body["name"],
            "encrypted": True,
        }

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    assert adapter._install_host_secrets(config, TOKEN) == 3
    assert len(requests) == 3
    assert all("verifier" not in request["name"] for request in requests)
    assert all(request["value"] not in json.dumps({"count": 3}) for request in requests)


def test_partial_host_secret_install_count_survives_in_signed_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)

    def partial(*_args: Any, **_kwargs: Any) -> int:
        raise adapter.HostSecretInstallError(1)

    monkeypatch.setattr(adapter, "_install_host_secrets", partial)
    receipt = adapter.execute_case(
        _config(tmp_path), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "host_secret_rotation_failed"
    assert receipt["host_secret_install_count"] == 1
    assert receipt["cleanup_status"] == "passed"
    assert adapter.verify_receipt(receipt, SIGNING_KEY)


def test_host_secret_installer_reports_exact_partial_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.state_root.mkdir(mode=0o700)
    _private_json(config.state_root / "secrets.json", {"attestation_secret": "s" * 40})
    _private_json(
        config.state_root / "credentials.json",
        {
            "paperless_builder_token": "p" * 40,
            "inventree_builder_token": "i" * 40,
        },
    )
    calls = 0

    def fake_platform(
        _method: str,
        _config: adapter.CaseConfig,
        _token: str,
        _path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise adapter.AcceptanceCaseError("platform_request_failed")
        return {
            "owner_id": body["owner_id"],
            "name": body["name"],
            "encrypted": True,
        }

    monkeypatch.setattr(adapter, "_platform_json", fake_platform)
    with pytest.raises(adapter.HostSecretInstallError) as captured:
        adapter._install_host_secrets(config, TOKEN)
    assert captured.value.installed_count == 1


def test_complete_case_receipt_is_signed_and_contains_no_protected_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)
    receipt = adapter.execute_case(
        _config(tmp_path), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    encoded = json.dumps(receipt)
    assert receipt["status"] == "passed"
    assert receipt["reason"] == "acceptance_case_passed"
    assert receipt["cleanup_status"] == "passed"
    assert receipt["builder_assignment_id"] == ASSIGNMENT_ID
    assert receipt["builder_session_id"] == SESSION_ID
    assert receipt["before_builder_handoff"] == _handoff()
    assert receipt["after_builder_handoff"] == _handoff()
    assert receipt["claim"] == "task_author_receipt_tamper_evidence_only"
    assert receipt["signature"]["assurance"] == "task_author_tamper_evidence_only"
    assert [item["command"] for item in receipt["environment_commands"]] == [
        "reset",
        "up",
        "initialize",
        "seed",
        "workflow-input",
        "snapshot",
        "snapshot",
        "down",
    ]
    assert adapter.verify_receipt(receipt, SIGNING_KEY)
    assert TOKEN not in encoded
    assert SIGNING_KEY.decode() not in encoded
    assert PUBLIC_RECORD["supplier"] not in encoded
    assert PUBLIC_RECORD["purchase_order"] not in encoded
    assert "HIDDEN-RECORD" not in encoded
    assert "protected/oracle" not in encoded
    assert "relative_path" not in encoded


def test_failed_verification_still_cleans_up_and_emits_signed_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)
    monkeypatch.setattr(
        adapter,
        "_run_host_verifier",
        lambda *_: _host_verification(passed=False),
    )
    receipt = adapter.execute_case(
        _config(tmp_path), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "host_verification_failed"
    assert receipt["host_verification"]["difference_count"] == 8
    assert receipt["environment_commands"][-1]["command"] == "down"
    assert adapter.verify_receipt(receipt, SIGNING_KEY)


def test_builder_or_platform_failure_is_not_repaired_or_bypassed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)

    def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        raise adapter.WorkflowExecutionError(
            "platform_request_failed",
            {
                "run_id": RUN_ID,
                "last_status": "running",
                "terminal_status": None,
                "resume_count": 4,
                "version": 2,
                "cancel_attempted": True,
                "cancel_result": "cancelling",
            },
        )

    monkeypatch.setattr(adapter, "_execute_workflow", interrupted)
    receipt = adapter.execute_case(
        _config(tmp_path), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "platform_request_failed"
    assert receipt["run"]["run_id"] == RUN_ID
    assert receipt["run"]["last_status"] == "running"
    assert receipt["run"]["resume_count"] == 4
    assert receipt["run"]["cancel_attempted"] is True
    assert receipt["run"]["cancel_result"] == "cancelling"
    assert receipt["environment_commands"][-1]["command"] == "down"
    assert adapter.verify_receipt(receipt, SIGNING_KEY)


def test_published_version_drift_fails_after_case_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)
    guards = [_guard(), {**_guard(), "active_version": 3}]
    monkeypatch.setattr(adapter, "_application_guard", lambda *_: guards.pop(0))
    receipt = adapter.execute_case(
        _config(tmp_path), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "published_version_changed"
    assert receipt["before_guard"] != receipt["after_guard"]
    assert receipt["environment_commands"][-1]["command"] == "down"


def test_protected_subprocess_output_is_redirected_to_private_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: Any) -> SimpleNamespace:
        observed["argv"] = argv
        observed["stdout"] = kwargs["stdout"].name
        observed["stderr"] = kwargs["stderr"].name
        kwargs["stdout"].write(b"PROTECTED-STDOUT")
        kwargs["stderr"].write(b"PROTECTED-STDERR")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    returncode, _duration = adapter._protected_run(
        ("safe-command", "--public-argument"), private_root=private
    )
    assert returncode == 0
    assert Path(observed["stdout"]).parent == private
    assert Path(observed["stderr"]).parent == private
    assert stat_mode(Path(observed["stdout"])) == 0o600
    assert TOKEN not in " ".join(observed["argv"])


def test_protected_subprocess_environment_is_a_strict_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/tmp/attacker")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/attacker.dylib")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("HOME", "/tmp/untrusted-home")
    environment = adapter._protected_environment()
    assert set(environment) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    assert environment["LANG"] == "C"
    assert environment["LC_ALL"] == "C"
    assert "PYTHONPATH" not in environment
    assert "DYLD_INSERT_LIBRARIES" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "HOME" not in environment


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_cli_exposes_token_files_not_bearer_values() -> None:
    help_text = adapter.build_parser().format_help()
    assert "--token-file" in help_text
    assert "--receipt-key-file" in help_text
    assert "--builder-assignment-id" in help_text
    assert "--builder-session-id" in help_text
    assert "--token " not in help_text
    assert "--oracle" not in help_text


def test_output_receipt_is_immutable_and_private(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    adapter._write_private_json(path, {"status": "passed"})
    assert stat_mode(path) == 0o600
    with pytest.raises(adapter.AcceptanceCaseError, match="receipt_write_rejected"):
        adapter._write_private_json(path, {"status": "changed"})


def test_invalid_seed_version_or_non_loopback_platform_is_rejected(tmp_path: Path) -> None:
    for config in (
        _config(tmp_path, seed="999"),
        _config(tmp_path, version=0),
        _config(tmp_path, platform_url="https://example.invalid"),
        _config(tmp_path, content_hash="a" * 64),
        _config(tmp_path, content_hash="sha256:short"),
    ):
        with pytest.raises(adapter.AcceptanceCaseError, match="case_binding_rejected"):
            adapter._validate_config(config)


def test_case_ids_are_fresh_across_signed_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_successful_case(monkeypatch)
    first = adapter.execute_case(
        _config(tmp_path / "first"), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    second = adapter.execute_case(
        _config(tmp_path / "second"), platform_token=TOKEN, signing_key=SIGNING_KEY
    )
    assert first["case_id"] != second["case_id"]
