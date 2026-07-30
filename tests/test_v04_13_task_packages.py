from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from agent_platform.collaboration_models import (
    VerificationClaim,
    VerificationClaimPayload,
    frozen_claim_context_digest,
)
from agent_platform.forbidden_assistance_scanner import (
    _source_semantic_policy,
    derive_source_semantic_input,
    scan_forbidden_assistance,
)
from agent_platform.formal_verification_contracts import ArchivedEvidenceIndex
from agent_platform.formal_source_provenance import (
    DEVELOPER_TRUST_ROOT_PATHS,
    LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1,
)
from agent_platform.lilies_tools import (
    LiliesToolContext,
    LiliesToolError,
    build_lilies_core_registry,
)
from agent_platform.lilies_models import (
    ApplicationTarget,
    ApplicationTargetMode,
    CollaborationAccess,
    CollaborationScope,
    PlatformAccess,
)
from agent_platform.task_packages import (
    WORKSPACE_MANIFEST_FILE,
    WORKSPACE_POLICY_FILE,
    _VERIFICATION_BUNDLE_SOURCE_PATHS_V1,
    ArchiveClaimBinding,
    ArchiveStatus,
    RunArchiveManifest,
    TaskPackageConflict,
    TaskPackageError,
    TaskPackageManager,
    TaskPackageNotReady,
    TaskPackageSecurityError,
    TaskPackageSpec,
    VerificationPolicyBundleManifest,
    VerificationRuntimeDependency,
    ValidationMode,
    WorkspaceRole,
    formal_platform_scopes,
)
from agent_platform.workflow_models import ApplicationSnapshot


TASK_ID = "EXP-LILIES-TEST-001"
ORACLE_MARKER = "HIDDEN-ORACLE-ANSWER-7f9d3b20"
CAPTURED_AT = "2026-07-24T00:00:00Z"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
FORMAL_ARTIFACT_PAYLOAD = b'{"artifact":"formal-result"}'
FORMAL_RECEIPT_PAYLOAD = b'{"receipt":"host-write-001"}'
FORMAL_ARTIFACT_DIGEST = "sha256:" + hashlib.sha256(FORMAL_ARTIFACT_PAYLOAD).hexdigest()
FORMAL_RECEIPT_DIGEST = "sha256:" + hashlib.sha256(FORMAL_RECEIPT_PAYLOAD).hexdigest()
ATTESTATION_SECRET_REF = "secret:formal-environment-attestation"
ATTESTATION_SECRET = b"lilies-t01f-environment-attestation-key-v1"
ATTESTATION_BODY = b'{"identity":"lilies-controlled-local-host-v1"}'


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> bytes:
    payload = _json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _write_yaml(path: Path, value: Any) -> bytes:
    payload = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _source_project() -> dict[str, Any]:
    return {
        "name": "paperless-ngx",
        "repository_url": "https://github.com/paperless-ngx/paperless-ngx",
        "release": "v2.20.15",
        "commit_sha": "1" * 40,
        "image_digest": "sha256:" + "2" * 64,
        "license": "GPL-3.0",
    }


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _make_task_source(
    root: Path,
    *,
    task_id: str = TASK_ID,
    revision: int = 1,
    health_port: int | None = None,
) -> Path:
    health_port = health_port or _unused_tcp_port()
    attestation_port = _unused_tcp_port()
    while attestation_port == health_port:
        attestation_port = _unused_tcp_port()
    root.mkdir(parents=True)
    requirement = (
        "Reconcile public invoice inputs against the frozen real host, preserve "
        "receipts, and produce an independently verifiable result.\n"
    ).encode()
    (root / "requirement.md").write_bytes(requirement)

    fixture = b"invoice_id,supplier,amount\nINV-001,Acme,42\n"
    fixture_path = root / "fixtures" / "public-inputs" / "invoice.csv"
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_bytes(fixture)
    fixture_entry = {
        "path": "public-inputs/invoice.csv",
        "digest": _sha256(fixture),
        "size_bytes": len(fixture),
    }
    fixture_manifest = {
        "schema_version": "1.0",
        "task_id": task_id,
        "revision": revision,
        "files": [fixture_entry],
    }
    fixture_manifest_payload = _write_json(
        root / "fixtures" / "manifest.json",
        fixture_manifest,
    )

    protected = root / "protected"
    (protected / "oracle").mkdir(parents=True)
    (protected / "expected-state").mkdir()
    _write_json(
        protected / "oracle" / "checks.json",
        {"checks": [{"id": "record-001", "expected": ORACLE_MARKER}]},
    )
    _write_json(
        protected / "oracle" / "oracle.json",
        {
            "schema_version": "1.0",
            "oracle_id": "invoice-oracle-v1",
            "task_id": task_id,
            "revision": revision,
            "validation_mode": "real_host",
            "checks": [
                {
                    "check_id": "result-status",
                    "kind": "json_equals",
                    "evidence_selector": {
                        "kind": "artifact",
                        "label": "formal-result.json",
                    },
                    "json_pointer": "/artifact",
                    "expected": "formal-result",
                }
            ],
        },
    )
    _write_json(
        protected / "expected-state" / "records.json",
        {"INV-001": {"matched": True}},
    )
    _write_json(protected / "leak-markers.json", {"markers": [ORACLE_MARKER]})

    source_project = _source_project()
    environment = {
        "schema_version": "1.0",
        "task_id": task_id,
        "revision": revision,
        "source_projects": [source_project],
        "compose_digest": "sha256:" + "3" * 64,
        "ports": [
            {
                "service": "paperless",
                "host": "127.0.0.1",
                "port": health_port,
            },
            {
                "service": "environment-attestation",
                "host": "127.0.0.1",
                "port": attestation_port,
            },
        ],
        "network_name": "lilies-formal-test",
        "volumes": ["paperless-data"],
        "initialization_commands": [{"name": "initialize", "digest": "sha256:" + "4" * 64}],
        "seed_commands": [{"name": "seed-public-records", "digest": "sha256:" + "5" * 64}],
        "health_checks": [
            {
                "check_id": "health:paperless",
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": health_port,
                "timeout_seconds": 1,
                "mandatory": True,
            },
            {
                "check_id": "identity:environment",
                "kind": "http",
                "url": f"http://127.0.0.1:{attestation_port}/identity",
                "expected_status": 200,
                "expected_body_digest": _sha256(ATTESTATION_BODY),
                "timeout_seconds": 1,
                "mandatory": True,
            },
        ],
        "secret_refs": [
            "secret:paperless-test-token",
            ATTESTATION_SECRET_REF,
        ],
        "attestation_secret_ref": ATTESTATION_SECRET_REF,
        "python_version": "3.12.10",
        "node_version": "22.17.0",
        "docker_version": "28.3.2",
        "fixture_files": [fixture_entry],
        "fault_injections": [
            {
                "name": "paperless-temporary-unavailable",
                "activation_command_digest": "sha256:" + "6" * 64,
                "recovery_command_digest": "sha256:" + "7" * 64,
            }
        ],
        "provenance": "real_host",
    }
    environment_payload = _write_yaml(root / "environment.lock", environment)

    allowed_actions = {
        "schema_version": "1.0",
        "task_id": task_id,
        "revision": revision,
        "readable_host_objects": ["paperless.documents"],
        "writable_host_operations": ["paperless.metadata.update"],
        "platform_actions": [
            "platform_contract_get",
            "platform_application_get",
            "platform_draft_apply",
            "platform_tests_run",
            "platform_run_start",
            "platform_run_get",
            "platform_trace_get",
            "platform_artifact_read",
        ],
        "network_hosts": ["paperless.local"],
        "model_access": True,
        "file_access": True,
        "connector_access": True,
        "permission_required_actions": ["paperless.metadata.update"],
        "max_write_count": 36,
        "max_payload_bytes": 1_048_576,
        "compensation_actions": ["paperless.metadata.restore"],
        "prohibited_actions": [
            "read_platform_source",
            "read_platform_database",
            "read_protected",
            "modify_task_package",
            "install_unknown_adapter",
        ],
        "validation_mode": "real_host",
    }
    _write_json(root / "allowed-actions.json", allowed_actions)
    budget = {
        "schema_version": "1.0",
        "task_id": task_id,
        "revision": revision,
        "max_build_repair_turns": 20,
        "max_model_cost_usd": 25,
        "assignment_wall_clock_seconds": 3600,
        "max_platform_tool_calls": 500,
        "max_report_evidence_rounds": 5,
        "stable_hidden_runs": 3,
    }
    _write_json(root / "budget.json", budget)

    task = {
        "schema_version": "1.0",
        "task_id": task_id,
        "revision": revision,
        "title": "Enterprise invoice reconciliation",
        "cohort": "enterprise",
        "customer_role": "Procurement and quality specialist",
        "business_goal": "Reconcile documents without unsafe or duplicate writes.",
        "source_projects": [source_project],
        "requirement_file": "requirement.md",
        "environment_lock_digest": _sha256(environment_payload),
        "fixture_manifest_digest": _sha256(fixture_manifest_payload),
        "allowed_actions_file": "allowed-actions.json",
        "budget_file": "budget.json",
        "deliverables": [
            {
                "name": "reconciliation result",
                "description": "A traceable reconciliation artifact.",
                "media_type": "application/json",
            }
        ],
        "acceptance_summary": "Every public input has a traceable safe outcome.",
        "no_substitute_validation": True,
        "collaboration_enabled": True,
        "author": "codex-task-author",
        "created_at": CAPTURED_AT,
        "parent_revision": revision - 1 if revision > 1 else None,
        "amendment_reason": (
            "Repair a documented task specification gap." if revision > 1 else None
        ),
    }
    _write_yaml(root / "task.yaml", task)
    return root


def _manager_and_package(tmp_path: Path) -> tuple[TaskPackageManager, Any]:
    manager = TaskPackageManager(
        tmp_path / "state",
        environment_secret_resolver=_environment_secret_resolver,
    )
    package = manager.freeze_revision(_make_task_source(tmp_path / "source"))
    return manager, package


def _environment_secret_resolver(secret_ref: str) -> bytes:
    if secret_ref != ATTESTATION_SECRET_REF:
        raise KeyError("unknown controlled environment secret")
    return ATTESTATION_SECRET


@contextmanager
def _real_health_endpoints(
    package: Any,
    *,
    attestation_secret: bytes = ATTESTATION_SECRET,
) -> Any:
    listeners: list[socket.socket] = []
    servers: list[ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []
    try:
        for spec in package.environment.health_checks:
            if spec.kind == "tcp":
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((spec.host, spec.port))
                listener.listen()
                listeners.append(listener)
                continue
            assert spec.url is not None
            parsed = urlsplit(str(spec.url))

            class AttestationHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    challenge = self.headers.get(
                        "X-Lilies-Attestation-Challenge",
                        "",
                    )
                    signature = (
                        "sha256:"
                        + hmac.new(
                            attestation_secret,
                            challenge.encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest()
                    )
                    self.send_response(200)
                    self.send_header(
                        "X-Lilies-Environment-Attestation",
                        signature,
                    )
                    self.send_header(
                        "Content-Length",
                        str(len(ATTESTATION_BODY)),
                    )
                    self.end_headers()
                    self.wfile.write(ATTESTATION_BODY)

                def log_message(self, format: str, *args: Any) -> None:
                    return

            server = ThreadingHTTPServer(
                (str(parsed.hostname), int(parsed.port or 80)),
                AttestationHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        yield
    finally:
        for listener in listeners:
            listener.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def _run_real_preflight(
    manager: TaskPackageManager,
    package: Any,
    **kwargs: Any,
) -> tuple[Path, Any]:
    with _real_health_endpoints(package):
        return manager.run_environment_preflight(package, **kwargs)


def _snapshot() -> ApplicationSnapshot:
    return ApplicationSnapshot(
        name="Formal reconciliation",
        description="A minimal frozen application snapshot.",
        requirement="Reconcile the public invoice input against the real host.",
    )


def _claim_binding(
    snapshot: ApplicationSnapshot,
    *,
    claim_id: UUID | None = None,
    assignment_id: UUID | None = None,
    application_id: UUID | None = None,
) -> ArchiveClaimBinding:
    return ArchiveClaimBinding(
        claim_id=claim_id or uuid4(),
        assignment_id=assignment_id or uuid4(),
        application_id=application_id or uuid4(),
        draft_revision=7,
        content_hash=f"sha256:{snapshot.content_hash()}",
        published_version=1,
        test_run_ids=["test-run:formal-001"],
        business_run_ids=["business-run:formal-001"],
        artifact_digests=[FORMAL_ARTIFACT_DIGEST],
        host_receipt_digests=[FORMAL_RECEIPT_DIGEST],
    )


def _connector_budget_receipt(assignment: Any) -> bytes:
    constraints = assignment.constraints
    policy = {
        "allowed_network_hosts": sorted(
            {
                str(host).casefold().rstrip(".")
                for host in constraints.allowed_hosts
            }
        ),
        "allowed_compensation_operations": sorted(
            set(constraints.compensation_actions)
        ),
        "max_write_count": constraints.max_write_count,
        "max_payload_bytes": constraints.max_payload_bytes,
    }
    document = {
        "schema_version": "1.0",
        "assignment_id": str(assignment.assignment_id),
        "policy_digest": hashlib.sha256(_json_bytes(policy)).hexdigest(),
        **policy,
        "write_count": 0,
        "writes": [],
    }
    document["receipt_digest"] = _sha256(_json_bytes(document))
    return _json_bytes(document)


def _archive_files(
    snapshot: ApplicationSnapshot,
    binding: ArchiveClaimBinding,
    *,
    package: Any | None = None,
    run_id: str | None = None,
    assignment: Any | None = None,
    business_status: str = "succeeded",
    archive_status: ArchiveStatus = ArchiveStatus.succeeded,
    validation_mode: ValidationMode = ValidationMode.real_host,
    marker: str | None = None,
    artifact_payload: bytes = FORMAL_ARTIFACT_PAYLOAD,
    artifact_label: str = "formal-result.json",
    receipt_payload: bytes = FORMAL_RECEIPT_PAYLOAD,
    receipt_label: str = "write-receipt.json",
    receipt_operation: str = "paperless:create-document",
    artifact_archive_path: str = (
        "artifacts/00000000-0000-4000-8000-000000000101.bin"
    ),
    receipt_archive_path: str = (
        "host-receipts/00000000-0000-4000-8000-000000000102.bin"
    ),
) -> dict[str, bytes]:
    if package is None or run_id is None or assignment is None:
        return {
            "assignment.json": _json_bytes(
                {
                    "assignment_id": str(binding.assignment_id),
                    "mode": "formal_experiment",
                }
            ),
            "draft.json": _json_bytes(
                {
                    "revision": binding.draft_revision,
                    "content_hash": binding.content_hash,
                    "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
                }
            ),
            "messages.jsonl": b'{"seq":1,"kind":"assignment"}\n',
            "platform-events.jsonl": b'{"seq":1,"kind":"run.started"}\n',
            "collaboration.jsonl": b'{"seq":1,"kind":"claim.frozen"}\n',
            "result.json": _json_bytes({"status": business_status, "summary": "formal result"}),
            "artifacts/formal-result.json": FORMAL_ARTIFACT_PAYLOAD,
            "host-receipts/write-receipt.json": FORMAL_RECEIPT_PAYLOAD,
        }
    message_payload = {"mode": "formal_experiment"}
    platform_started_payload = {"status": "started"}
    platform_snapshot_payload = {"source": "platform_event_store"}
    collaboration_payload = {"source": "collaboration_store"}
    outcome = {
        "application_id": str(binding.application_id),
        "draft_revision": binding.draft_revision,
        "content_hash": binding.content_hash,
        "published_version": binding.published_version,
        "test_run_ids": binding.test_run_ids,
        "business_run_ids": binding.business_run_ids,
        "artifact_digests": binding.artifact_digests,
        "host_receipt_digests": binding.host_receipt_digests,
    }
    session_id = uuid4()
    messages = [
        {
            "schema_version": "1.0",
            "seq": 1,
            "message_id": f"message:assignment:{uuid4().hex}",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "session_id": str(session_id),
            "kind": "assignment.accepted",
            "payload": message_payload,
            "payload_digest": _sha256(_json_bytes(message_payload)),
        }
    ]
    platform_events = [
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": f"platform-event:started:{uuid4().hex}",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "application_id": str(binding.application_id),
            "kind": "run.started",
            "payload": platform_started_payload,
            "payload_digest": _sha256(_json_bytes(platform_started_payload)),
        },
        {
            "schema_version": "1.0",
            "seq": 2,
            "event_id": f"platform-event:snapshot:{uuid4().hex}",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "application_id": str(binding.application_id),
            "kind": "formal_run.snapshot",
            "payload": platform_snapshot_payload,
            "payload_digest": _sha256(_json_bytes(platform_snapshot_payload)),
            "outcome": outcome,
        },
    ]
    collaboration_events: list[dict[str, Any]] = []
    for report_id in binding.resolved_report_ids:
        report_payload = {"source": "collaboration_store"}
        collaboration_events.append(
            {
                "schema_version": "1.0",
                "seq": len(collaboration_events) + 1,
                "event_id": f"collaboration:report:{uuid4().hex}",
                "task_id": package.task.task_id,
                "revision": package.task.revision,
                "run_id": run_id,
                "assignment_id": str(binding.assignment_id),
                "channel_id": str(assignment.collaboration.channel_id),
                "kind": "report.resolved",
                "report_id": str(report_id),
                "payload": report_payload,
                "payload_digest": _sha256(_json_bytes(report_payload)),
            }
        )
    collaboration_events.append(
        {
            "schema_version": "1.0",
            "seq": len(collaboration_events) + 1,
            "event_id": f"collaboration:claim:{uuid4().hex}",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "channel_id": str(assignment.collaboration.channel_id),
            "kind": "claim.prepared",
            "claim_binding": binding.model_dump(mode="json"),
            "payload": collaboration_payload,
            "payload_digest": _sha256(_json_bytes(collaboration_payload)),
        }
    )
    result = {
        "schema_version": "1.0",
        "task_id": package.task.task_id,
        "revision": package.task.revision,
        "run_id": run_id,
        "assignment_id": str(binding.assignment_id),
        "application_id": str(binding.application_id),
        "archive_status": archive_status.value,
        "validation_mode": validation_mode.value,
        "business_status": business_status,
        "business_run_ids": binding.business_run_ids,
        "artifact_digests": binding.artifact_digests,
        "host_receipt_digests": binding.host_receipt_digests,
        "remaining_limits": binding.remaining_limits,
        "summary": "formal result",
    }
    if marker is not None:
        messages[0]["payload"]["debug"] = marker
        messages[0]["payload_digest"] = _sha256(_json_bytes(messages[0]["payload"]))
    files = {
        "assignment.json": _json_bytes(assignment.model_dump(mode="json", exclude_none=True)),
        "draft.json": _json_bytes(
            {
                "revision": binding.draft_revision,
                "content_hash": binding.content_hash,
                "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
            }
        ),
        "messages.jsonl": b"".join(_json_bytes(item) + b"\n" for item in messages),
        "platform-events.jsonl": b"".join(_json_bytes(item) + b"\n" for item in platform_events),
        "collaboration.jsonl": b"".join(_json_bytes(item) + b"\n" for item in collaboration_events),
        "result.json": _json_bytes(result),
    }
    connector_budget = _connector_budget_receipt(assignment)
    files["connector-budget.json"] = connector_budget
    files["scanner-inputs/connector-budget.json"] = connector_budget
    if binding.artifact_digests:
        assert binding.artifact_digests == [_sha256(artifact_payload)]
        files[artifact_archive_path] = artifact_payload
    if binding.host_receipt_digests:
        assert binding.host_receipt_digests == [_sha256(receipt_payload)]
        files[receipt_archive_path] = receipt_payload
    evidence_entries: list[dict[str, Any]] = []
    business_run_id = binding.business_run_ids[0]
    if binding.artifact_digests:
        evidence_entries.append(
            {
                "schema_version": "1.0",
                "evidence_key": f"artifact:{artifact_label}",
                "kind": "artifact",
                "label": artifact_label,
                "provenance_source": "platform_artifact_scan",
                "run_id": business_run_id,
                "archive_path": artifact_archive_path,
                "digest": _sha256(artifact_payload),
                "size_bytes": len(artifact_payload),
                "media_type": "application/json",
            }
        )
    if binding.host_receipt_digests:
        evidence_entries.append(
            {
                "schema_version": "1.0",
                "evidence_key": (f"host_receipt:{receipt_operation}:{receipt_label}"),
                "kind": "host_receipt",
                "label": receipt_label,
                "operation": receipt_operation,
                "provenance_source": "platform_host_write",
                "run_id": business_run_id,
                "archive_path": receipt_archive_path,
                "digest": _sha256(receipt_payload),
                "size_bytes": len(receipt_payload),
                "media_type": "application/json",
            }
        )
    evidence_index = ArchivedEvidenceIndex.model_validate(
        {
            "schema_version": "1.0",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "application_id": str(binding.application_id),
            "entry_count": len(evidence_entries),
            "entries": evidence_entries,
        }
    )
    evidence_index_payload = evidence_index.model_dump(
        mode="json",
        exclude_none=True,
    )
    files["evidence-index.json"] = _json_bytes(evidence_index_payload)
    assert assignment.collaboration is not None
    scanner_session_id = session_id
    scanner_tool_call_id = "tool-call:formal-draft-apply"
    scanner_request_id = uuid4()
    scanner_idempotency_key = "fixture-formal-draft-mutation-0001"
    scanner_request_payload = {
        "application_id": str(binding.application_id),
        "expected_revision": binding.draft_revision - 1,
        "op": "update_node",
        "data": {
            "node_id": "fixture-source",
            "patch": {"label": "Lilies-authored draft mutation"},
        },
    }
    scanner_request_payload_digest = _sha256(
        _json_bytes(scanner_request_payload)
    )
    scanner_operation_digest = _sha256(
        _json_bytes(scanner_request_payload)
    )
    scanner_tests_tool_call_id = "tool-call:platform-tests-run"
    scanner_tests_request_payload = {
        "application_id": str(binding.application_id),
        "idempotency_key": "fixture-platform-tests-run-0001",
    }
    scanner_tests_request = {
        "request_id": "00000000-0000-4000-8000-000000000202",
        "assignment_id": str(binding.assignment_id),
        "session_id": str(scanner_session_id),
        "application_id": str(binding.application_id),
        "tool_call_id": scanner_tests_tool_call_id,
        "operation": "platform_tests_run",
        "idempotency_key": scanner_tests_request_payload["idempotency_key"],
        "payload": scanner_tests_request_payload,
        "payload_digest": _sha256(_json_bytes(scanner_tests_request_payload)),
        "state": "completed",
        "status_code": 200,
        "response": {
            "ok": True,
            "operation": "platform_tests_run",
            "data": {
                "passed": True,
                "tests": [
                    {
                        "test_id": f"fixture-acceptance-{index + 1}",
                        "run_id": platform_run_id,
                        "run_status": "succeeded",
                        "passed": True,
                    }
                    for index, platform_run_id in enumerate(binding.test_run_ids)
                ],
            },
        },
    }
    scanner_business_requests = []
    for index, platform_run_id in enumerate(binding.business_run_ids):
        idempotency_key = f"fixture-platform-run-start-{index + 1:04d}"
        payload = {
            "application_id": str(binding.application_id),
            "idempotency_key": idempotency_key,
            "inputs": {},
            "use_draft": True,
        }
        scanner_business_requests.append(
            {
                "request_id": (
                    f"00000000-0000-4000-8000-{index + 203:012d}"
                ),
                "assignment_id": str(binding.assignment_id),
                "session_id": str(scanner_session_id),
                "application_id": str(binding.application_id),
                "tool_call_id": f"tool-call:platform-run-start:{index + 1}",
                "operation": "platform_run_start",
                "idempotency_key": idempotency_key,
                "payload": payload,
                "payload_digest": _sha256(_json_bytes(payload)),
                "state": "completed",
                "status_code": 202,
                "response": {
                    "ok": True,
                    "operation": "platform_run_start",
                    "data": {
                        "run_id": platform_run_id,
                        "status": "running",
                        "version": binding.published_version,
                        "draft_revision": binding.draft_revision,
                    },
                },
            }
        )
    scanner_tool_requests = [
        (
            scanner_tests_tool_call_id,
            "platform_tests_run",
        ),
        *[
            (
                str(request["tool_call_id"]),
                "platform_run_start",
            )
            for request in scanner_business_requests
        ],
    ]
    bridge_events = [
        {
            "daemon_seq": 1,
            "event_type": "tool.started",
            "data_json": json.dumps(
                {
                    "tool_call_id": scanner_tool_call_id,
                    "tool": "platform_draft_apply",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
        {
            "daemon_seq": 2,
            "event_type": "tool.completed",
            "data_json": json.dumps(
                {
                    "tool_call_id": scanner_tool_call_id,
                    "tool": "platform_draft_apply",
                    "is_error": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    ]
    for tool_call_id, operation in scanner_tool_requests:
        next_seq = len(bridge_events) + 1
        bridge_events.extend(
            [
                {
                    "daemon_seq": next_seq,
                    "event_type": "tool.started",
                    "data_json": json.dumps(
                        {
                            "tool_call_id": tool_call_id,
                            "tool": operation,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
                {
                    "daemon_seq": next_seq + 1,
                    "event_type": "tool.completed",
                    "data_json": json.dumps(
                        {
                            "tool_call_id": tool_call_id,
                            "tool": operation,
                            "is_error": False,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            ]
        )
    baseline_content_hash = "sha256:" + "6" * 64
    bridge_export = {
        "schema_version": "1.0",
        "complete": True,
        "assignment": {
            "assignment_id": str(binding.assignment_id),
            "session_id": str(scanner_session_id),
            "application_id": str(binding.application_id),
            "terminal_events_drained_at": CAPTURED_AT,
        },
        "events": bridge_events,
        "counts": {"events": len(bridge_events)},
        "watermark": {
            "min_daemon_seq": 1,
            "max_daemon_seq": len(bridge_events),
            "relay_cursor": len(bridge_events),
            "ack_cursor": len(bridge_events),
        },
    }
    collaboration_table_names = (
        "credentials",
        "messages",
        "reports",
        "report_revisions",
        "approvals",
        "reader_cursors",
        "reader_ack_receipts",
        "developer_leases",
        "lease_operations",
        "developer_responses",
        "task_amendments",
        "environment_responses",
        "reprobes",
        "claims",
        "verifications",
        "audit",
        "outbox",
        "channel_operations",
        "operation_receipts",
    )
    collaboration_export = {
        "schema_version": "1.0",
        "complete": True,
        "counts": {name: 0 for name in collaboration_table_names},
        "watermark": {
            "min_message_seq": None,
            "max_message_seq": None,
            "next_seq": 1,
        },
        "channel": {
            "channel_id": str(assignment.collaboration.channel_id),
            "assignment_id": str(binding.assignment_id),
            "lilies_session_id": str(scanner_session_id),
            "next_seq": 1,
        },
        **{name: [] for name in collaboration_table_names},
    }
    workflow_runs = [
        {
            "id": platform_run_id,
            "status": "succeeded",
            "state": {
                "assignment_id": str(binding.assignment_id),
                "session_id": str(scanner_session_id),
            },
            "outputs": {"status": "succeeded"},
            "error": None,
            "events": [
                {
                    "seq": 1,
                    "type": "workflow.completed",
                    "created_at": CAPTURED_AT,
                    "data": {"status": "succeeded"},
                }
            ],
        }
        for platform_run_id in [
            *binding.test_run_ids,
            *binding.business_run_ids,
        ]
    ]
    workflow_export = {
        "schema_version": "1.0",
        "complete": True,
        "counts": {
            "runs": len(workflow_runs),
            "run_events": len(workflow_runs),
            "formal_draft_baselines": 1,
            "formal_draft_mutations": 1,
        },
        "run_event_counts": {
            str(item["id"]): len(item["events"])
            for item in workflow_runs
        },
        "application": {"id": str(binding.application_id)},
        "draft": {
            "revision": binding.draft_revision,
            "content_hash": binding.content_hash,
            "snapshot": snapshot.model_dump(mode="json", exclude_none=True),
        },
        "runs": workflow_runs,
        "formal_draft_provenance": {
            "baselines": [
                {
                    "assignment_id": str(binding.assignment_id),
                    "session_id": str(scanner_session_id),
                    "application_id": str(binding.application_id),
                    "baseline_revision": binding.draft_revision - 1,
                    "baseline_content_hash": baseline_content_hash,
                    "started_at": "2026-07-23T23:59:58Z",
                }
            ],
            "mutations": [
                {
                    "mutation_id": "fixture-formal-draft-mutation",
                    "assignment_id": str(binding.assignment_id),
                    "session_id": str(scanner_session_id),
                    "application_id": str(binding.application_id),
                    "actor_kind": "lilies_blackbox",
                    "request_id": str(scanner_request_id),
                    "tool_call_id": scanner_tool_call_id,
                    "operation": "update_node",
                    "operation_digest": scanner_operation_digest,
                    "request_payload_digest": scanner_request_payload_digest,
                    "idempotency_key": scanner_idempotency_key,
                    "before_revision": binding.draft_revision - 1,
                    "before_content_hash": baseline_content_hash,
                    "after_revision": binding.draft_revision,
                    "after_content_hash": binding.content_hash,
                    "content_changed": 1,
                    "created_at": "2026-07-23T23:59:59Z",
                }
            ],
        },
    }
    blackbox_auth_export = {
        "schema_version": "1.0",
        "assignment_id": str(binding.assignment_id),
        "session_id": str(scanner_session_id),
        "complete": True,
        "credentials": [],
        "credential_applications": [],
        "requests": [
            {
                "request_id": str(scanner_request_id),
                "assignment_id": str(binding.assignment_id),
                "session_id": str(scanner_session_id),
                "application_id": str(binding.application_id),
                "tool_call_id": scanner_tool_call_id,
                "operation": "platform_draft_apply",
                "idempotency_key": scanner_idempotency_key,
                "payload": scanner_request_payload,
                "payload_digest": scanner_request_payload_digest,
                "state": "completed",
                "status_code": 200,
                "response": {
                    "revision": binding.draft_revision,
                    "content_hash": binding.content_hash,
                },
            },
            scanner_tests_request,
            *scanner_business_requests,
        ],
        "audit": [],
        "security_events": [],
        "audit_min_seq": None,
        "audit_max_seq": None,
        "security_min_seq": None,
        "security_max_seq": None,
        "counts": {
            "credentials": 0,
            "credential_applications": 0,
            "requests": 2 + len(scanner_business_requests),
            "audit": 0,
            "security_events": 0,
        },
    }
    inventory_records = [
        {
            "artifact_id": entry["archive_path"]
            .rsplit("/", 1)[-1]
            .removesuffix(".bin"),
            "assignment_id": str(binding.assignment_id),
            "session_id": str(scanner_session_id),
            "application_id": str(binding.application_id),
            "run_id": entry["run_id"],
            "evidence_kind": entry["kind"],
        }
        for entry in evidence_entries
    ]
    artifact_inventory_export = {
        "schema_version": "1.0",
        "assignment_id": str(binding.assignment_id),
        "session_id": str(scanner_session_id),
        "application_id": str(binding.application_id),
        "complete": True,
        "count": len(inventory_records),
        "records": inventory_records,
    }
    empty_tree_payload = b""
    empty_tree_oid = hashlib.sha1(b"tree 0\0").hexdigest()  # noqa: S324
    baseline_commit_payload = (
        f"tree {empty_tree_oid}\n"
        "author Lilies Fixture <lilies@example.invalid> 0 +0000\n"
        "committer Lilies Fixture <lilies@example.invalid> 0 +0000\n"
        "\n"
        "formal source fixture\n"
    ).encode()
    baseline_commit_oid = hashlib.sha1(  # noqa: S324
        b"commit "
        + str(len(baseline_commit_payload)).encode()
        + b"\0"
        + baseline_commit_payload
    ).hexdigest()
    baseline_commit_path = (
        "source-provenance/commits/"
        f"baseline-{baseline_commit_oid}.commit"
    )
    tree_path = f"source-provenance/trees/{empty_tree_oid}.tree"
    baseline_commit_object = {
        "schema_version": "1.0",
        "object_type": "commit",
        "oid": baseline_commit_oid,
        "archive_path": baseline_commit_path,
        "payload_digest": _sha256(baseline_commit_payload),
        "size_bytes": len(baseline_commit_payload),
    }
    tree_object = {
        "schema_version": "1.0",
        "object_type": "tree",
        "oid": empty_tree_oid,
        "archive_path": tree_path,
        "payload_digest": _sha256(empty_tree_payload),
        "size_bytes": len(empty_tree_payload),
    }
    source_state = {
        "schema_version": "1.0",
        "object_format": "sha1",
        "head_commit_sha": baseline_commit_oid,
        "head_tree_sha": empty_tree_oid,
        "status_digest": _sha256(b""),
        "tracked_change_count": 0,
        "untracked_file_count": 0,
        "conflicted_path_count": 0,
        "clean": True,
    }
    source_baseline_payload = {
        "schema_version": "1.0",
        "task_id": package.task.task_id,
        "task_revision": package.task.revision,
        "run_id": run_id,
        "assignment_id": str(binding.assignment_id),
        "channel_id": str(assignment.collaboration.channel_id),
        "source_state": source_state,
        "captured_at": "2026-07-23T23:59:57Z",
    }
    source_baseline = {
        **source_baseline_payload,
        "baseline_digest": _sha256(_json_bytes(source_baseline_payload)),
    }
    source_manifest_payload = {
        "schema_version": "1.0",
        "task_id": package.task.task_id,
        "task_revision": package.task.revision,
        "run_id": run_id,
        "assignment_id": str(binding.assignment_id),
        "channel_id": str(assignment.collaboration.channel_id),
        "baseline": source_baseline,
        "baseline_commit_object": baseline_commit_object,
        "tree_objects": [tree_object],
        "approved_commits": [],
        "final_source_state": source_state,
        "finalized_at": CAPTURED_AT,
    }
    source_provenance_export = {
        **source_manifest_payload,
        "manifest_digest": _sha256(_json_bytes(source_manifest_payload)),
    }
    files["source-provenance/manifest.json"] = _json_bytes(
        source_provenance_export
    )
    files[baseline_commit_path] = baseline_commit_payload
    files[tree_path] = empty_tree_payload
    source_files = {
        path: payload
        for path, payload in files.items()
        if path.startswith("source-provenance/")
    }
    source_semantic_input = derive_source_semantic_input(
        task_package=package,
        source_manifest=source_provenance_export,
        source_files=source_files,
    )
    assert source_semantic_input.task_policy.fixture_identifiers
    source_semantic_export = source_semantic_input.model_dump(
        mode="json",
        exclude_none=True,
    )
    assistance_scan = scan_forbidden_assistance(
        assignment=assignment,
        session_id=scanner_session_id,
        channel_id=assignment.collaboration.channel_id,
        bridge_export=bridge_export,
        collaboration_export=collaboration_export,
        workflow_export=workflow_export,
        blackbox_auth_export=blackbox_auth_export,
        artifact_inventory_export=artifact_inventory_export,
        source_provenance_export=source_provenance_export,
        source_semantic_export=source_semantic_export,
        source_semantic_task_package=package,
        source_semantic_files=source_files,
        evidence_index=evidence_index,
        business_run_ids=binding.business_run_ids,
        validation_mode=validation_mode.value,
        created_at=datetime.fromisoformat(
            CAPTURED_AT.replace("Z", "+00:00")
        ),
    )
    files.update(
        {
            "forbidden-assistance-scan.json": _json_bytes(
                assistance_scan.model_dump(
                    mode="json",
                    exclude_none=True,
                )
            ),
            "scanner-inputs/bridge.json": _json_bytes(bridge_export),
            "scanner-inputs/collaboration.json": _json_bytes(
                collaboration_export
            ),
            "scanner-inputs/workflow.json": _json_bytes(workflow_export),
            "scanner-inputs/blackbox-auth.json": _json_bytes(
                blackbox_auth_export
            ),
            "scanner-inputs/artifact-inventory.json": _json_bytes(
                artifact_inventory_export
            ),
            "scanner-inputs/source-semantic.json": _json_bytes(
                source_semantic_export
            ),
        }
    )
    return files


def _build_formal_assignment(
    manager: TaskPackageManager,
    package: Any,
    *,
    ready_path: Path,
    workspace: Path,
    run_id: str,
    assignment_id: UUID,
) -> Any:
    created_at = datetime.now(timezone.utc)
    return manager.build_formal_assignment(
        package,
        ready_path=ready_path,
        workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
        run_id=run_id,
        assignment_id=assignment_id,
        idempotency_key=f"formal-archive:{uuid4().hex}",
        target=ApplicationTarget(mode=ApplicationTargetMode.create_new),
        platform=PlatformAccess(
            base_url="http://127.0.0.1:8001",
            contract_url="/api/v1/lilies/platform-contract",
            contract_digest=DIGEST_A,
            credential_ref=f"credential:platform:{uuid4().hex}",
            scopes=formal_platform_scopes(package.allowed_actions.platform_actions),
            application_ids=[],
        ),
        collaboration=CollaborationAccess(
            channel_id=uuid4(),
            credential_ref=f"credential:collaboration:{uuid4().hex}",
            scopes=list(CollaborationScope),
            expires_at=created_at + timedelta(seconds=package.budget.assignment_wall_clock_seconds),
        ),
        created_at=created_at,
    )


def _successful_archive(
    tmp_path: Path,
    *,
    run_id: str = "run-formal-001",
    archive_now: datetime | None = None,
) -> dict[str, Any]:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id="environment:paperless-001",
    )
    workspace = tmp_path / "lilies-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=binding.assignment_id,
    )

    def archive() -> tuple[Path, RunArchiveManifest, str]:
        return manager.archive_run(
            package,
            run_id=run_id,
            status=ArchiveStatus.succeeded,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=ready_path,
            workspace_manifest_path=workspace / WORKSPACE_MANIFEST_FILE,
            files=_archive_files(
                snapshot,
                binding,
                package=package,
                run_id=run_id,
                assignment=assignment,
            ),
            claim_binding=binding,
        )

    if archive_now is None:
        run_root, manifest, manifest_digest = archive()
    else:

        class ArchiveDateTime(datetime):
            @classmethod
            def now(cls, tz: timezone | None = None) -> datetime:
                return archive_now if tz is not None else archive_now.replace(tzinfo=None)

        with patch("agent_platform.task_packages.datetime", ArchiveDateTime):
            run_root, manifest, manifest_digest = archive()
    return {
        "manager": manager,
        "package": package,
        "snapshot": snapshot,
        "binding": binding,
        "ready_path": ready_path,
        "workspace": workspace,
        "assignment": assignment,
        "run_root": run_root,
        "manifest": manifest,
        "manifest_digest": manifest_digest,
    }


def _terminal_archive_inputs(
    tmp_path: Path,
    *,
    run_id: str,
) -> dict[str, Any]:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_instance_id=f"environment:{run_id}",
    )
    workspace = tmp_path / "terminal-workspace"
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id=run_id,
        assignment_id=binding.assignment_id,
        environment_ready_path=ready_path,
    )
    assignment = _build_formal_assignment(
        manager,
        package,
        ready_path=ready_path,
        workspace=workspace,
        run_id=run_id,
        assignment_id=binding.assignment_id,
    )
    files = _archive_files(
        snapshot,
        binding,
        package=package,
        run_id=run_id,
        assignment=assignment,
        business_status="assignment_failed",
        archive_status=ArchiveStatus.failed,
    )
    workflow_export = json.loads(
        files["scanner-inputs/workflow.json"]
    )
    collaboration_export = json.loads(
        files["scanner-inputs/collaboration.json"]
    )
    bridge_export = json.loads(files["scanner-inputs/bridge.json"])
    blackbox_auth_export = json.loads(
        files["scanner-inputs/blackbox-auth.json"]
    )
    artifact_inventory_export = json.loads(
        files["scanner-inputs/artifact-inventory.json"]
    )
    source_provenance_export = json.loads(
        files["source-provenance/manifest.json"]
    )
    source_semantic_export = json.loads(
        files["scanner-inputs/source-semantic.json"]
    )
    source_files = {
        path: payload
        for path, payload in files.items()
        if path.startswith("source-provenance/")
    }
    evidence_index = ArchivedEvidenceIndex.model_validate(
        json.loads(files["evidence-index.json"])
    )
    business_run_ids = [str(item["id"]) for item in workflow_export["runs"]]
    scan = scan_forbidden_assistance(
        assignment=assignment,
        session_id=UUID(str(bridge_export["assignment"]["session_id"])),
        channel_id=assignment.collaboration.channel_id,
        bridge_export=bridge_export,
        collaboration_export=collaboration_export,
        workflow_export=workflow_export,
        blackbox_auth_export=blackbox_auth_export,
        artifact_inventory_export=artifact_inventory_export,
        source_provenance_export=source_provenance_export,
        source_semantic_export=source_semantic_export,
        source_semantic_task_package=package,
        source_semantic_files=source_files,
        evidence_index=evidence_index,
        business_run_ids=business_run_ids,
        validation_mode=ValidationMode.real_host.value,
        created_at=datetime.fromisoformat(CAPTURED_AT.replace("Z", "+00:00")),
    )
    platform_records: list[dict[str, Any]] = []
    for run in workflow_export["runs"]:
        durable_events = [
            {
                "seq": int(event["seq"]),
                "type": str(event["type"]),
                "created_at": str(event["created_at"]),
                "data": event["data"],
                "data_digest": _sha256(_json_bytes(event["data"])),
            }
            for event in run["events"]
        ]
        payload = {
            "platform_run_id": str(run["id"]),
            "status": str(run["status"]),
            "version": run.get("version"),
            "draft_revision": run.get("draft_revision"),
            "created_at": str(run.get("created_at") or ""),
            "updated_at": str(run.get("updated_at") or ""),
            "outputs": run.get("outputs"),
            "error": run.get("error"),
            "durable_events": durable_events,
        }
        platform_records.append(
            {
                "schema_version": "1.0",
                "seq": len(platform_records) + 1,
                "event_id": f"terminal-platform:{len(platform_records) + 1}",
                "task_id": package.task.task_id,
                "revision": package.task.revision,
                "run_id": run_id,
                "assignment_id": str(binding.assignment_id),
                "application_id": str(binding.application_id),
                "kind": "run.started",
                "payload": payload,
                "payload_digest": _sha256(_json_bytes(payload)),
            }
        )
    files["platform-events.jsonl"] = b"".join(
        _json_bytes(item) + b"\n" for item in platform_records
    )
    files["collaboration.jsonl"] = b""
    files["forbidden-assistance-scan.json"] = _json_bytes(
        scan.model_dump(mode="json", exclude_none=True)
    )
    files["result.json"] = _json_bytes(
        {
            "schema_version": "1.0",
            "task_id": package.task.task_id,
            "revision": package.task.revision,
            "run_id": run_id,
            "assignment_id": str(binding.assignment_id),
            "application_id": str(binding.application_id),
            "archive_status": "failed",
            "validation_mode": "real_host",
            "business_status": "assignment_failed",
            "business_run_ids": business_run_ids,
            "artifact_digests": binding.artifact_digests,
            "host_receipt_digests": binding.host_receipt_digests,
            "remaining_limits": binding.remaining_limits,
            "summary": "Formal assignment failed after preserving every attempt.",
        }
    )
    return {
        "manager": manager,
        "package": package,
        "files": files,
        "run_id": run_id,
        "ready_path": ready_path,
        "workspace_manifest_path": workspace / WORKSPACE_MANIFEST_FILE,
        "findings": [
            f"{item.rule_id}:{item.source_ref}" for item in scan.findings
        ],
    }


def _claim_for_archive(
    archive: dict[str, Any],
    *,
    content_hash: str | None = None,
    artifact_digest: str = FORMAL_ARTIFACT_DIGEST,
    receipt_digest: str = FORMAL_RECEIPT_DIGEST,
) -> VerificationClaim:
    package = archive["package"]
    binding: ArchiveClaimBinding = archive["binding"]
    manifest: RunArchiveManifest = archive["manifest"]
    payload: dict[str, Any] = {
        "schema_version": "1.1",
        "claim_id": str(binding.claim_id),
        "application_id": str(binding.application_id),
        "draft_revision": binding.draft_revision,
        "content_hash": content_hash or binding.content_hash,
        "published_version": binding.published_version,
        "test_run_ids": binding.test_run_ids,
        "business_run_ids": binding.business_run_ids,
        "artifact_refs": [
            {
                "evidence_id": "evidence:formal-artifact-001",
                "kind": "artifact",
                "digest": artifact_digest,
                "media_type": "application/json",
                "label": "Formal result artifact",
                "captured_at": CAPTURED_AT,
            }
        ],
        "host_receipt_refs": [
            {
                "evidence_id": "evidence:formal-host-receipt-001",
                "kind": "host_receipt",
                "digest": receipt_digest,
                "media_type": "application/json",
                "label": "Scoped host write receipt",
                "captured_at": CAPTURED_AT,
            }
        ],
        "resolved_report_ids": [],
        "remaining_limits": [],
        "task_package_digest": package.record.public_summary_digest,
        "environment_ready_digest": manifest.environment_ready_digest,
        "archive_manifest_digest": archive["manifest_digest"],
        "verification_process_digest": (
            package.record.verification_process_digest
        ),
        "validation_mode": "real_host",
        "claim": "ready_for_independent_verification",
    }
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    return VerificationClaim.model_validate(
        {
            **payload,
            "channel_id": str(uuid4()),
            "assignment_id": str(binding.assignment_id),
            "created_at": CAPTURED_AT,
        }
    )


def test_task_package_schema_is_strict_frozen_and_revision_bound(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    task = TaskPackageSpec.model_validate(
        yaml.safe_load((source / "task.yaml").read_text(encoding="utf-8"))
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskPackageSpec.model_validate(
            {
                **task.model_dump(mode="json"),
                "oracle_path": "protected/oracle/checks.json",
            }
        )
    with pytest.raises(ValidationError, match="Instance is frozen"):
        task.title = "mutated"  # type: ignore[misc]

    invalid = task.model_dump(mode="json")
    invalid.update(
        {
            "revision": 2,
            "parent_revision": None,
            "amendment_reason": None,
        }
    )
    with pytest.raises(ValidationError, match="immediately preceding parent"):
        TaskPackageSpec.model_validate(invalid)


def test_allowed_actions_accepts_explicitly_disabled_connector_and_network(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    allowed_path = source / "allowed-actions.json"
    allowed = json.loads(allowed_path.read_bytes())
    allowed.update(
        {
            "readable_host_objects": [],
            "writable_host_operations": [],
            "network_hosts": [],
            "connector_access": False,
            "permission_required_actions": [],
            "compensation_actions": [],
        }
    )
    _write_json(allowed_path, allowed)

    package = TaskPackageManager(tmp_path / "state").freeze_revision(source)

    assert package.allowed_actions.connector_access is False
    assert package.allowed_actions.network_hosts == []
    assert package.allowed_actions.readable_host_objects == []


@pytest.mark.parametrize(
    "changes",
    [
        {
            "readable_host_objects": [],
            "writable_host_operations": [],
            "network_hosts": ["paperless.local"],
            "connector_access": False,
            "permission_required_actions": [],
            "compensation_actions": [],
        },
        {
            "readable_host_objects": [],
            "writable_host_operations": [],
            "network_hosts": [],
            "connector_access": True,
            "permission_required_actions": [],
            "compensation_actions": [],
        },
    ],
)
def test_allowed_actions_empty_connector_policy_fails_closed_when_incoherent(
    tmp_path: Path,
    changes: dict[str, Any],
) -> None:
    source = _make_task_source(tmp_path / "source")
    allowed_path = source / "allowed-actions.json"
    allowed = json.loads(allowed_path.read_bytes())
    allowed.update(changes)
    _write_json(allowed_path, allowed)

    with pytest.raises(ValueError):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_source_semantic_policy_reads_frozen_public_fixture_headers(
    tmp_path: Path,
) -> None:
    _manager, package = _manager_and_package(tmp_path)

    policy = _source_semantic_policy(package)

    assert {
        "amount",
        "invoice_id",
        "supplier",
    }.issubset(policy.fixture_identifiers)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda original: original + b"\nrevision: 1\n",
        lambda original: b"defaults: &defaults {}\n" + original,
        lambda original: original + b"\nunknown_control: true\n",
    ],
    ids=["duplicate-key", "anchor", "unknown-field"],
)
def test_task_yaml_rejects_ambiguous_or_unknown_content(
    tmp_path: Path,
    mutate: Any,
) -> None:
    source = _make_task_source(tmp_path / "source")
    task_yaml = source / "task.yaml"
    task_yaml.write_bytes(mutate(task_yaml.read_bytes()))

    with pytest.raises((TaskPackageError, ValidationError)):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_freeze_is_idempotent_but_rejects_source_and_frozen_drift(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)
    replay = manager.freeze_revision(source)
    assert replay.record.sealed_package_digest == package.record.sealed_package_digest
    assert replay.record.public_summary_digest == package.record.public_summary_digest
    assert package.record.sealed_package_digest != package.record.public_summary_digest

    (source / "requirement.md").write_text("changed old revision", encoding="utf-8")
    with pytest.raises(TaskPackageConflict, match="immutable task revision"):
        manager.freeze_revision(source)

    frozen_requirement = package.root / "requirement.md"
    frozen_requirement.chmod(0o600)
    frozen_requirement.write_text("tampered frozen revision", encoding="utf-8")
    with pytest.raises(TaskPackageConflict, match="frozen task file drift"):
        manager.load_frozen(TASK_ID, 1)


def test_freeze_retains_content_addressed_verification_policy_bundle(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)

    policy_source, policy = manager.load_verification_policy_bundle(
        package.record.verification_process_digest
    )

    assert policy.verification_process_digest == (
        package.record.verification_process_digest
    )
    assert policy.entrypoint == "agent_platform.independent_verifier"
    assert policy.schema_version == "1.1"
    assert (
        "platform/backend/src/agent_platform/kernel_boot_identity.py"
        in policy.protected_source_paths
    )
    assert {
        "agent_platform/capability_generality_gate.py",
        "agent_platform/kernel_boot_identity.py",
    } <= {item.path for item in policy.sources}
    assert policy.python_executable_digest.startswith("sha256:")
    assert policy.python_executable_size_bytes > 0
    assert [item.name for item in policy.runtime_dependencies] == [
        "PyYAML",
        "annotated-types",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    ]
    assert all(
        item.installed_file_count > 0
        and item.installed_files_digest.startswith("sha256:")
        for item in policy.runtime_dependencies
    )
    assert {
        "platform/backend/src/agent_platform/independent_verifier.py",
        "platform/backend/src/agent_platform/independent_verifier_broker.py",
        "platform/backend/src/agent_platform/forbidden_assistance_scanner.py",
        "platform/backend/src/agent_platform/formal_source_provenance.py",
        "platform/backend/src/agent_platform/task_packages.py",
        "platform/backend/src/agent_platform/stable_verification.py",
        "platform/backend/src/agent_platform/stable_verification_cli.py",
        "platform/backend/src/agent_platform/stable_verification_coordinator.py",
    } <= set(policy.protected_source_paths)
    assert {
        "agent_platform/independent_verifier.py",
        "agent_platform/independent_verifier_broker.py",
        "agent_platform/forbidden_assistance_scanner.py",
        "agent_platform/formal_source_provenance.py",
        "agent_platform/task_packages.py",
        "agent_platform/stable_verification.py",
        "agent_platform/stable_verification_cli.py",
        "agent_platform/stable_verification_coordinator.py",
    } <= {entry.path for entry in policy.sources}
    assert policy_source.parent.name == (
        package.record.verification_process_digest.removeprefix("sha256:")
    )
    assert stat.S_IMODE(policy_source.parent.stat().st_mode) == 0o500
    assert all(
        stat.S_IMODE((policy_source / entry.path).stat().st_mode) == 0o400
        for entry in policy.sources
    )
    isolated_import = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys;"
                f"sys.path.insert(0,{str(policy_source)!r});"
                "import agent_platform.independent_verifier"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolated_import.returncode == 0, isolated_import.stderr


def test_verification_policy_trust_root_evolution_is_versioned(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)
    _policy_source, current = manager.load_verification_policy_bundle(
        package.record.verification_process_digest
    )

    legacy_payload = current.model_dump(mode="json")
    legacy_payload["schema_version"] = "1.0"
    legacy_payload["protected_source_paths"] = sorted(
        LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1
    )
    legacy_payload["sources"] = [
        {
            "digest": digest,
            "path": path,
            "size_bytes": size_bytes,
        }
        for path, digest, size_bytes in (
            (
                "agent_platform/__init__.py",
                "sha256:6fa5d1b3841b0f5e447de0741c9e11619c3521520be2d3b56e18a62d7afece69",
                87,
            ),
            (
                "agent_platform/capability_contracts.py",
                "sha256:f48e2c81b3535f9f512fe56632e6df9094cde563310220c2d6112d0da6ab2ac9",
                50258,
            ),
            (
                "agent_platform/collaboration_models.py",
                "sha256:13d414b1e6cdbd3d99d7b9447a72ac4eff226c20acb2d675fc2559e48bc5ffc5",
                58305,
            ),
            (
                "agent_platform/forbidden_assistance_scanner.py",
                "sha256:e5f77c428cae253c6b1f1b66543923ff6b3347a82aa6a1b2f6102d0c2eb4975e",
                94043,
            ),
            (
                "agent_platform/formal_source_provenance.py",
                "sha256:cdb395e4b683e10dbcb36d09a65f5596a38ace15b4fcf42b0851f48e2698c6d2",
                270665,
            ),
            (
                "agent_platform/formal_verification_contracts.py",
                "sha256:b70086bff7aaa35e190b4ebb42d501ac2dafb4b1c72660039ea54c329a521c2d",
                10567,
            ),
            (
                "agent_platform/independent_verifier.py",
                "sha256:623a6d43a8c19065302fbb006b7822d20737ec481495a3bc8aa1f85313b5e47d",
                35885,
            ),
            (
                "agent_platform/independent_verifier_broker.py",
                "sha256:58492dc19a764ba0d645d778819773f1d09e81568eaecc58e143c047fdbfd23a",
                13839,
            ),
            (
                "agent_platform/lilies_models.py",
                "sha256:b53720e977a14acdeac7cf1ee3e1440b024ae24a4ffd8279dac9cf5c60b5f313",
                44551,
            ),
            (
                "agent_platform/models.py",
                "sha256:ca22d2026e98d7eb8a913d072889487c9599453048e12af6d27e0c35edad7b4a",
                5984,
            ),
            (
                "agent_platform/stable_verification.py",
                "sha256:9c914aa397fc59eb8e72283592e3451e024eecc26f17cf8d8df69007f7da791a",
                55904,
            ),
            (
                "agent_platform/stable_verification_cli.py",
                "sha256:64957431b38d394f3026caa653969979b2dff4b4dc751167bb71632f6fac41c7",
                5956,
            ),
            (
                "agent_platform/stable_verification_coordinator.py",
                "sha256:56965000a74de92c8e254598e51387faff064004bec19cfec5f8d38b5326f7ef",
                21062,
            ),
            (
                "agent_platform/task_packages.py",
                "sha256:884ecc42eb66e28045f2a194f74e88a89e55e3abc6bbf353de3d481d7b2c12f2",
                258356,
            ),
            (
                "agent_platform/workflow_models.py",
                "sha256:3556272401798946922f69109a3d60507116c500b0fd368b8af14e39d066712e",
                15974,
            ),
        )
    ]
    legacy_payload["verification_process_digest"] = _sha256(
        _json_bytes(
            {
                key: value
                for key, value in legacy_payload.items()
                if key != "verification_process_digest"
            }
        )
    )
    legacy = VerificationPolicyBundleManifest.model_validate(legacy_payload)
    assert legacy.protected_source_paths == sorted(
        LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1
    )

    downgraded_current = current.model_dump(mode="json")
    downgraded_current["schema_version"] = "1.0"
    downgraded_current["protected_source_paths"] = sorted(
        LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1
    )
    downgraded_current["sources"] = [
        item
        for item in downgraded_current["sources"]
        if item["path"] in _VERIFICATION_BUNDLE_SOURCE_PATHS_V1
    ]
    downgraded_current["verification_process_digest"] = _sha256(
        _json_bytes(
            {
                key: value
                for key, value in downgraded_current.items()
                if key != "verification_process_digest"
            }
        )
    )
    with pytest.raises(
        ValidationError,
        match="approved legacy source closure",
    ):
        VerificationPolicyBundleManifest.model_validate(downgraded_current)

    for schema_version, protected_paths in (
        ("1.0", sorted(DEVELOPER_TRUST_ROOT_PATHS)),
        ("1.1", sorted(LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1)),
    ):
        forged = {
            **legacy_payload,
            "schema_version": schema_version,
            "protected_source_paths": protected_paths,
        }
        forged["verification_process_digest"] = _sha256(
            _json_bytes(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "verification_process_digest"
                }
            )
        )
        with pytest.raises(
            ValidationError,
            match="complete developer trust root",
        ):
            VerificationPolicyBundleManifest.model_validate(forged)

    for schema_version, protected_paths, sources in (
        (
            "1.0",
            sorted(LEGACY_DEVELOPER_TRUST_ROOT_PATHS_V1),
            current.model_dump(mode="json")["sources"],
        ),
        (
            "1.1",
            sorted(DEVELOPER_TRUST_ROOT_PATHS),
            legacy_payload["sources"],
        ),
    ):
        forged = {
            **legacy_payload,
            "schema_version": schema_version,
            "protected_source_paths": protected_paths,
            "sources": sources,
        }
        forged["verification_process_digest"] = _sha256(
            _json_bytes(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "verification_process_digest"
                }
            )
        )
        with pytest.raises(
            ValidationError,
            match="exact executable source closure",
        ):
            VerificationPolicyBundleManifest.model_validate(forged)


def test_verification_policy_rejects_same_version_dependency_byte_drift(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)
    dependencies = manager._verification_runtime_dependencies()
    forged = [
        VerificationRuntimeDependency(
            **{
                **item.model_dump(mode="json"),
                "installed_files_digest": (
                    "sha256:" + "f" * 64
                    if index == 0
                    else item.installed_files_digest
                ),
            }
        )
        for index, item in enumerate(dependencies)
    ]

    with (
        patch.object(
            TaskPackageManager,
            "_verification_runtime_dependencies",
            return_value=forged,
        ),
        pytest.raises(TaskPackageConflict, match="runtime differs"),
    ):
        manager.load_verification_policy_bundle(
            package.record.verification_process_digest
        )


def test_verification_policy_rejects_python_executable_byte_drift(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    manager = TaskPackageManager(tmp_path / "state")
    package = manager.freeze_revision(source)
    executable = manager._verification_python_executable()
    forged = executable.model_copy(
        update={"digest": "sha256:" + "e" * 64}
    )

    with (
        patch.object(
            TaskPackageManager,
            "_verification_python_executable",
            return_value=forged,
        ),
        pytest.raises(TaskPackageConflict, match="runtime differs"),
    ):
        manager.load_verification_policy_bundle(
            package.record.verification_process_digest
        )


def test_verification_policy_rejects_dependency_without_record_inventory(
    tmp_path: Path,
) -> None:
    missing_record = SimpleNamespace(
        files=[],
        version="1.0",
    )
    with (
        patch(
            "agent_platform.task_packages.importlib.metadata.distribution",
            return_value=missing_record,
        ),
        pytest.raises(
            TaskPackageSecurityError,
            match="RECORD inventory",
        ),
    ):
        TaskPackageManager(
            tmp_path / "state"
        )._verification_runtime_dependencies()


@pytest.mark.parametrize("kind", ["fixture-drift", "symlink", "hardlink"])
def test_freeze_rejects_manifest_drift_and_unsafe_file_aliases(
    tmp_path: Path,
    kind: str,
) -> None:
    source = _make_task_source(tmp_path / "source")
    fixture = source / "fixtures" / "public-inputs" / "invoice.csv"
    if kind == "fixture-drift":
        fixture.write_bytes(b"different bytes")
    else:
        outside = tmp_path / "outside.csv"
        outside.write_bytes(b"outside")
        fixture.unlink()
        if kind == "symlink":
            fixture.symlink_to(outside)
        else:
            os.link(outside, fixture)

    with pytest.raises((TaskPackageError, TaskPackageSecurityError)):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_freeze_rejects_oracle_larger_than_verification_result_capacity(
    tmp_path: Path,
) -> None:
    source = _make_task_source(tmp_path / "source")
    oracle_path = source / "protected" / "oracle" / "oracle.json"
    oracle = json.loads(oracle_path.read_bytes())
    oracle["checks"] = [
        {
            "check_id": f"result-status-{index:03d}",
            "kind": "json_equals",
            "archive_path": "result.json",
            "json_pointer": "/business_status",
            "expected": "succeeded",
        }
        for index in range(501)
    ]
    _write_json(oracle_path, oracle)

    with pytest.raises(TaskPackageError, match="oracle identity or checks"):
        TaskPackageManager(tmp_path / "state").freeze_revision(source)


def test_environment_preflight_success_is_exact_bound_and_stale_safe(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    assignment_id = uuid4()
    ready_path, ready = _run_real_preflight(
        manager,
        package,
        run_id="run-preflight-001",
        assignment_id=assignment_id,
        environment_instance_id="environment:paperless-001",
    )
    assert stat.S_IMODE(ready_path.stat().st_mode) == 0o400
    assert ready.ready is True
    assert ready.provenance == "real_host"
    assert ready.sealed_package_digest == package.record.sealed_package_digest
    http_check = next(check for check in ready.checks if check.kind == "http")
    tcp_check = next(check for check in ready.checks if check.kind == "tcp")
    assert http_check.attestation_challenge_digest is not None
    assert tcp_check.attestation_challenge_digest is None
    loaded, ready_digest = manager.require_environment_ready(
        package,
        ready_path,
        run_id="run-preflight-001",
        assignment_id=assignment_id,
        at=ready.finished_at,
    )
    assert loaded == ready
    assert ready_digest == _sha256(ready_path.read_bytes())

    with pytest.raises(TaskPackageNotReady, match="exact run"):
        manager.require_environment_ready(
            package,
            ready_path,
            run_id="run-other",
            assignment_id=assignment_id,
            at=ready.finished_at,
        )
    with pytest.raises(TaskPackageNotReady, match="stale"):
        manager.require_environment_ready(
            package,
            ready_path,
            run_id="run-preflight-001",
            assignment_id=assignment_id,
            at=ready.expires_at,
        )


def test_failed_or_missing_health_evidence_never_issues_ready(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    assignment_id = uuid4()
    with pytest.raises(TaskPackageNotReady, match="preflight failed"):
        manager.run_environment_preflight(
            package,
            run_id="run-preflight-failed",
            assignment_id=assignment_id,
            environment_instance_id="environment:paperless-failed",
        )
    preflight_dir = manager.preflight_root / TASK_ID / "1" / "run-preflight-failed"
    failure_path = preflight_dir / "environment-preflight.json"
    assert failure_path.is_file()
    failure = json.loads(failure_path.read_bytes())
    assert failure["attempt"] == 1
    assert failure["environment_instance_id"] == "environment:paperless-failed"
    assert stat.S_IMODE(failure_path.stat().st_mode) == 0o400
    assert not (preflight_dir / "environment-ready.json").exists()

    with pytest.raises(TaskPackageError):
        manager.require_environment_ready(
            package,
            preflight_dir / "environment-ready.json",
            run_id="run-preflight-failed",
            assignment_id=assignment_id,
        )


def test_failed_environment_preflight_can_reprobe_without_erasing_failure_evidence(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    assignment_id = uuid4()
    run_id = "run-preflight-reprobe"
    environment_instance_id = "environment:paperless-reprobe"

    with pytest.raises(TaskPackageNotReady, match="preflight failed"):
        manager.run_environment_preflight(
            package,
            run_id=run_id,
            assignment_id=assignment_id,
            environment_instance_id=environment_instance_id,
        )
    failure_path = manager.preflight_root / TASK_ID / "1" / run_id / ("environment-preflight.json")
    failure_payload = failure_path.read_bytes()

    ready_path, ready = _run_real_preflight(
        manager,
        package,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_instance_id=environment_instance_id,
    )

    assert ready.ready is True
    assert ready_path.is_file()
    assert failure_path.read_bytes() == failure_payload
    assert not list(failure_path.parent.glob("environment-preflight-attempt-*.json"))


def test_unauthenticated_health_substitute_never_issues_ready(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    assignment_id = uuid4()

    with _real_health_endpoints(
        package,
        attestation_secret=b"x" * 32,
    ):
        for _ in range(2):
            with pytest.raises(TaskPackageNotReady, match="preflight failed"):
                manager.run_environment_preflight(
                    package,
                    run_id="run-preflight-substitute",
                    assignment_id=assignment_id,
                    environment_instance_id="environment:substitute",
                )

    preflight_dir = manager.preflight_root / TASK_ID / "1" / "run-preflight-substitute"
    assert not (preflight_dir / "environment-ready.json").exists()
    failure = json.loads((preflight_dir / "environment-preflight.json").read_bytes())
    retry = json.loads((preflight_dir / "environment-preflight-attempt-0002.json").read_bytes())
    identity = next(
        item for item in failure["checks"] if item["check_id"] == "identity:environment"
    )
    retry_identity = next(
        item for item in retry["checks"] if item["check_id"] == "identity:environment"
    )
    assert identity["passed"] is False
    assert identity["identity_authenticated"] is False
    assert (
        identity["attestation_challenge_digest"] != retry_identity["attestation_challenge_digest"]
    )


@pytest.mark.parametrize("role", [WorkspaceRole.lilies, WorkspaceRole.developer])
def test_workspace_materialization_exposes_public_inputs_and_filters_protected(
    tmp_path: Path,
    role: WorkspaceRole,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    workspace = tmp_path / f"workspace-{role.value}"
    run_id = f"run-workspace-{role.value}"
    assignment_id = uuid4()
    ready_path: Path | None = None
    if role is WorkspaceRole.lilies:
        ready_path, _ = _run_real_preflight(
            manager,
            package,
            run_id=run_id,
            assignment_id=assignment_id,
            environment_instance_id="environment:workspace-test",
        )
    manifest = manager.materialize_task_workspace(
        package,
        workspace,
        role=role,
        run_id=run_id,
        assignment_id=assignment_id,
        environment_ready_path=ready_path,
    )
    prefix = Path("task") if role is WorkspaceRole.developer else Path()
    assert (workspace / prefix / "requirement.md").is_file()
    assert (workspace / prefix / "fixtures/public-inputs/invoice.csv").is_file()
    assert not (workspace / prefix / "protected").exists()
    assert not any(".git" in path.parts for path in workspace.rglob("*"))
    assert all("protected" not in Path(item.target_path).parts for item in manifest.entries)
    assert {".git", "protected", "oracle", "platform-data"} <= set(manifest.denied_segments)
    assert all(item.read_only for item in manifest.entries)
    for item in manifest.entries:
        assert stat.S_IMODE((workspace / item.target_path).stat().st_mode) == 0o400

    policy = json.loads((workspace / WORKSPACE_POLICY_FILE).read_text(encoding="utf-8"))
    assert policy["denied_segments"] == sorted(policy["denied_segments"])
    assert policy["writable_prefixes"] == manifest.writable_prefixes
    assert stat.S_IMODE((workspace / WORKSPACE_POLICY_FILE).stat().st_mode) == 0o400
    assert stat.S_IMODE((workspace / WORKSPACE_MANIFEST_FILE).stat().st_mode) == 0o400


@pytest.mark.asyncio
async def test_workspace_policy_denies_reserved_and_read_only_paths(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    workspace = tmp_path / "workspace-lilies"
    assignment_id = uuid4()
    ready_path, _ = _run_real_preflight(
        manager,
        package,
        run_id="run-workspace-policy",
        assignment_id=assignment_id,
        environment_instance_id="environment:workspace-policy",
    )
    manager.materialize_task_workspace(
        package,
        workspace,
        role=WorkspaceRole.lilies,
        run_id="run-workspace-policy",
        assignment_id=assignment_id,
        environment_ready_path=ready_path,
    )
    registry = build_lilies_core_registry()
    context = LiliesToolContext(session_id="formal-session", workspace=workspace)

    with pytest.raises(LiliesToolError, match="reserved"):
        await registry.get("workspace_read").execute(
            {"path": "protected/oracle/checks.json"},
            context,
        )
    with pytest.raises(LiliesToolError, match="read-only"):
        await registry.get("workspace_write").execute(
            {"path": "requirement.md", "content": "mutate old revision"},
            context,
        )
    result = await registry.get("workspace_write").execute(
        {"path": "work/result.txt", "content": "allowed scratch output"},
        context,
    )
    assert result.content == "wrote work/result.txt"


def test_successful_archive_seals_replays_and_validates_exact_claim(
    tmp_path: Path,
) -> None:
    archive = _successful_archive(tmp_path)
    manager: TaskPackageManager = archive["manager"]
    manifest: RunArchiveManifest = archive["manifest"]
    run_root: Path = archive["run_root"]
    package = archive["package"]

    assert manifest.status is ArchiveStatus.succeeded
    assert manifest.validation_mode is ValidationMode.real_host
    assert manifest.environment_ready_digest
    assert manifest.workspace_mount_digest
    assert manifest.verification_process_digest == (
        package.record.verification_process_digest
    )
    assert manifest.claim_binding == archive["binding"]
    assert not any(item.path.startswith("task/protected/") for item in manifest.files)
    assert stat.S_IMODE((run_root / "archive-manifest.json").stat().st_mode) == 0o400
    assert (
        manager.replay_archive(
            run_root,
            expected_manifest_digest=archive["manifest_digest"],
        )
        == manifest
    )

    claim = _claim_for_archive(archive)
    assert claim.verification_process_digest == (
        package.record.verification_process_digest
    )
    assert (
        manager.validate_claim_binding(
            task_id=package.task.task_id,
            revision=package.task.revision,
            claim=claim,
        )
        == manifest
    )


def test_successful_archive_replays_assignment_time_health_after_ready_ttl(
    tmp_path: Path,
) -> None:
    archive_now = datetime.now(timezone.utc) + timedelta(hours=2)
    archive = _successful_archive(
        tmp_path,
        run_id="run-formal-after-ready-ttl",
        archive_now=archive_now,
    )
    ready_payload = json.loads(archive["ready_path"].read_text(encoding="utf-8"))
    ready_expires_at = datetime.fromisoformat(ready_payload["expires_at"])

    assert ready_expires_at < archive["manifest"].created_at
    assert (
        archive["manager"].replay_archive(
            archive["run_root"],
            expected_manifest_digest=archive["manifest_digest"],
        )
        == archive["manifest"]
    )


def test_terminal_formal_archive_rejects_scanner_input_tamper(
    tmp_path: Path,
) -> None:
    archive = _terminal_archive_inputs(
        tmp_path,
        run_id="run-terminal-input-tamper",
    )
    workflow = json.loads(
        archive["files"]["scanner-inputs/workflow.json"]
    )
    workflow["runs"][0]["outputs"] = {
        "status": "tampered-after-scan",
    }
    archive["files"]["scanner-inputs/workflow.json"] = _json_bytes(
        workflow
    )

    with pytest.raises(
        TaskPackageConflict,
        match="scan input digest changed",
    ):
        archive["manager"].archive_run(
            archive["package"],
            run_id=archive["run_id"],
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=archive["ready_path"],
            workspace_manifest_path=archive[
                "workspace_manifest_path"
            ],
            files=archive["files"],
            claim_binding=None,
            forbidden_assistance_findings=archive["findings"],
        )


def test_terminal_formal_archive_rejects_scan_record_tamper(
    tmp_path: Path,
) -> None:
    archive = _terminal_archive_inputs(
        tmp_path,
        run_id="run-terminal-scan-tamper",
    )
    scan = json.loads(
        archive["files"]["forbidden-assistance-scan.json"]
    )
    scan["input_bindings"][0]["count"] += 1
    scan_without_digest = dict(scan)
    scan_without_digest.pop("scan_digest")
    scan["scan_digest"] = _sha256(_json_bytes(scan_without_digest))
    archive["files"]["forbidden-assistance-scan.json"] = _json_bytes(scan)

    with pytest.raises(
        TaskPackageConflict,
        match="scan replay differs",
    ):
        archive["manager"].archive_run(
            archive["package"],
            run_id=archive["run_id"],
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=archive["ready_path"],
            workspace_manifest_path=archive[
                "workspace_manifest_path"
            ],
            files=archive["files"],
            claim_binding=None,
            forbidden_assistance_findings=archive["findings"],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing typed evidence"),
        ("digest", "receipt digest or writes are invalid"),
        ("policy", "changed its frozen policy"),
    ],
)
def test_terminal_formal_archive_rejects_connector_budget_evidence_drift(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    archive = _terminal_archive_inputs(
        tmp_path,
        run_id=f"run-terminal-connector-{mutation}",
    )
    files = archive["files"]
    if mutation == "missing":
        files.pop("connector-budget.json")
        files.pop("scanner-inputs/connector-budget.json")
    else:
        receipt = json.loads(files["connector-budget.json"])
        if mutation == "digest":
            receipt["receipt_digest"] = DIGEST_B
        else:
            receipt["allowed_network_hosts"] = ["attacker.example"]
            policy = {
                "allowed_network_hosts": receipt[
                    "allowed_network_hosts"
                ],
                "allowed_compensation_operations": receipt[
                    "allowed_compensation_operations"
                ],
                "max_write_count": receipt["max_write_count"],
                "max_payload_bytes": receipt["max_payload_bytes"],
            }
            receipt["policy_digest"] = hashlib.sha256(
                _json_bytes(policy)
            ).hexdigest()
            unsigned = dict(receipt)
            unsigned.pop("receipt_digest")
            receipt["receipt_digest"] = _sha256(_json_bytes(unsigned))
        payload = _json_bytes(receipt)
        files["connector-budget.json"] = payload
        files["scanner-inputs/connector-budget.json"] = payload

    with pytest.raises(TaskPackageConflict, match=message):
        archive["manager"].archive_run(
            archive["package"],
            run_id=archive["run_id"],
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=archive["ready_path"],
            workspace_manifest_path=archive[
                "workspace_manifest_path"
            ],
            files=files,
            claim_binding=None,
            forbidden_assistance_findings=archive["findings"],
        )


def test_successful_archive_rejects_missing_health_mount_claim_and_substitute(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    snapshot = _snapshot()
    binding = _claim_binding(snapshot)
    files = _archive_files(snapshot, binding)

    with pytest.raises(TaskPackageError, match="real health"):
        manager.archive_run(
            package,
            run_id="run-no-health",
            status=ArchiveStatus.succeeded,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files=files,
            claim_binding=binding,
        )
    with pytest.raises(TaskPackageError, match="real health"):
        manager.archive_run(
            package,
            run_id="run-substitute",
            status=ArchiveStatus.succeeded,
            validation_mode=ValidationMode.protocol_mock,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files=files,
            claim_binding=binding,
        )


def test_failed_archives_are_append_only_preserved_and_replayable(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    created: list[tuple[Path, RunArchiveManifest, str]] = []
    for run_id, status in (
        ("run-failed", ArchiveStatus.failed),
        ("run-environment-failed", ArchiveStatus.environment_failed),
    ):
        created.append(
            manager.archive_run(
                package,
                run_id=run_id,
                status=status,
                validation_mode=ValidationMode.real_host,
                environment_ready_path=None,
                workspace_manifest_path=None,
                files={"result.json": _json_bytes({"status": status.value})},
                claim_binding=None,
            )
        )

    assert [item[1].status for item in created] == [
        ArchiveStatus.failed,
        ArchiveStatus.environment_failed,
    ]
    for root, manifest, digest in created:
        assert root.is_dir()
        assert (
            manager.replay_archive(
                root,
                expected_manifest_digest=digest,
            )
            == manifest
        )
    index = json.loads((package.root / "archive-manifest.json").read_text())
    assert [(item["run_id"], item["status"]) for item in index["runs"]] == [
        ("run-failed", "failed"),
        ("run-environment-failed", "environment_failed"),
    ]


def test_archive_replay_rejects_byte_tampering(tmp_path: Path) -> None:
    manager, package = _manager_and_package(tmp_path)
    run_root, _, manifest_digest = manager.archive_run(
        package,
        run_id="run-tamper",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": b'{"status":"failed"}'},
        claim_binding=None,
    )
    result = run_root / "result.json"
    result.chmod(0o600)
    result.write_bytes(b'{"status":"succeeded"}')
    result.chmod(0o400)

    with pytest.raises(TaskPackageConflict, match="archive byte drift"):
        manager.replay_archive(
            run_root,
            expected_manifest_digest=manifest_digest,
        )


def test_archive_replay_rejects_permission_only_drift(
    tmp_path: Path,
) -> None:
    archive = _successful_archive(tmp_path)
    result = archive["run_root"] / "result.json"
    result.chmod(0o600)

    with pytest.raises(
        TaskPackageConflict,
        match="archive file permissions changed",
    ):
        archive["manager"].replay_archive(
            archive["run_root"],
            expected_manifest_digest=archive["manifest_digest"],
        )


def test_archive_identity_rejects_conflicting_replay(tmp_path: Path) -> None:
    manager, package = _manager_and_package(tmp_path)
    manager.archive_run(
        package,
        run_id="run-conflict",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": b'{"attempt":1}'},
        claim_binding=None,
    )

    with pytest.raises(TaskPackageConflict, match="identity"):
        manager.archive_run(
            package,
            run_id="run-conflict",
            status=ArchiveStatus.failed,
            validation_mode=ValidationMode.real_host,
            environment_ready_path=None,
            workspace_manifest_path=None,
            files={"result.json": b'{"attempt":2}'},
            claim_binding=None,
        )


def test_archive_oracle_marker_and_forbidden_assistance_force_invalid_status(
    tmp_path: Path,
) -> None:
    manager, package = _manager_and_package(tmp_path)
    _, leaked, _ = manager.archive_run(
        package,
        run_id="run-oracle-leak",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": _json_bytes({"status": "failed", "debug": ORACLE_MARKER})},
        claim_binding=None,
    )
    assert leaked.status is ArchiveStatus.invalid
    assert leaked.security_findings == ["protected_oracle_marker:result.json"]

    _, assisted, _ = manager.archive_run(
        package,
        run_id="run-forbidden-assistance",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={"result.json": b'{"status":"failed"}'},
        claim_binding=None,
        forbidden_assistance_findings=["codex_authored_final_workflow"],
    )
    assert assisted.status is ArchiveStatus.invalid
    assert assisted.forbidden_assistance_findings == ["codex_authored_final_workflow"]


def test_claim_binding_rejects_draft_run_and_frozen_context_drift(
    tmp_path: Path,
) -> None:
    archive = _successful_archive(tmp_path)
    manager: TaskPackageManager = archive["manager"]
    package = archive["package"]
    claim = _claim_for_archive(archive)
    manager.validate_claim_binding(
        task_id=package.task.task_id,
        revision=package.task.revision,
        claim=claim,
    )

    payload = claim.model_dump(mode="json")
    payload["business_run_ids"] = ["business-run:different"]
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    drifted = VerificationClaim.model_validate(payload)
    with pytest.raises(TaskPackageConflict, match="binding mismatch"):
        manager.validate_claim_binding(
            task_id=package.task.task_id,
            revision=package.task.revision,
            claim=drifted,
        )

    incomplete = claim.model_dump(mode="json")
    for field in (
        "channel_id",
        "assignment_id",
        "claim_revision",
        "status",
        "created_at",
        "invalidated_at",
        "invalidation_reason",
    ):
        incomplete.pop(field)
    incomplete["schema_version"] = "1.0"
    for field in (
        "task_package_digest",
        "environment_ready_digest",
        "archive_manifest_digest",
        "verification_process_digest",
        "validation_mode",
        "frozen_context_digest",
    ):
        incomplete[field] = None
    legacy_claim = VerificationClaimPayload.model_validate(incomplete)
    with pytest.raises(TaskPackageConflict, match="schema 1.1"):
        manager.validate_claim_binding(
            task_id=package.task.task_id,
            revision=package.task.revision,
            claim=legacy_claim,
        )


def test_claim_binding_rejects_artifact_and_receipt_digest_drift(
    tmp_path: Path,
) -> None:
    archive = _successful_archive(tmp_path)
    manager: TaskPackageManager = archive["manager"]
    package = archive["package"]
    drifted = _claim_for_archive(
        archive,
        artifact_digest="sha256:" + "c" * 64,
        receipt_digest="sha256:" + "d" * 64,
    )

    with pytest.raises(TaskPackageConflict, match="binding mismatch"):
        manager.validate_claim_binding(
            task_id=package.task.task_id,
            revision=package.task.revision,
            claim=drifted,
        )


def test_success_manifest_model_rejects_protocol_mock_substitution() -> None:
    with pytest.raises(ValidationError, match="successful archives require real"):
        RunArchiveManifest.model_validate(
            {
                "schema_version": "1.0",
                "task_id": TASK_ID,
                "revision": 1,
                "run_id": "run-mock",
                "source_status": "succeeded",
                "status": "succeeded",
                "validation_mode": "protocol_mock",
                "public_summary_digest": DIGEST_A,
                "sealed_package_digest": DIGEST_B,
                "verification_process_digest": DIGEST_A,
                "request_digest": DIGEST_A,
                "files": [
                    {
                        "path": "result.json",
                        "digest": DIGEST_A,
                        "size_bytes": 2,
                    }
                ],
                "created_at": CAPTURED_AT,
            }
        )


def test_archive_replay_rejects_invalid_jsonl_sequence(tmp_path: Path) -> None:
    manager, package = _manager_and_package(tmp_path)
    run_root, _, _ = manager.archive_run(
        package,
        run_id="run-bad-jsonl",
        status=ArchiveStatus.failed,
        validation_mode=ValidationMode.real_host,
        environment_ready_path=None,
        workspace_manifest_path=None,
        files={
            "messages.jsonl": b'{"seq":1}\n{"seq":3}\n',
            "result.json": b'{"status":"failed"}',
        },
        claim_binding=None,
    )
    with pytest.raises(TaskPackageConflict, match="invalid strict record"):
        manager.replay_archive(run_root)
