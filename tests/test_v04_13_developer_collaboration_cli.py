from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_platform import developer_collaboration_cli
from agent_platform.developer_collaboration_client import (
    DeveloperCollaborationClient,
    DeveloperCollaborationClientError,
    read_developer_token,
)


BASE_URL = "http://platform.test"
DEVELOPER_TOKEN = "developer-cli-token-" + "x" * 32
REPORT_ID = UUID("11111111-1111-4111-8111-111111111111")
LEASE_ID = UUID("22222222-2222-4222-8222-222222222222")
RESPONSE_ID = UUID("33333333-3333-4333-8333-333333333333")
CHANNEL_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = "2026-07-24T00:00:00Z"
DIGEST = "sha256:" + "a" * 64
SOURCE_DIGEST = "sha256:" + "b" * 64


def _install_http_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    transport = httpx.MockTransport(handler)

    def client_factory(
        *,
        base_url: str,
        access_token: str,
    ) -> DeveloperCollaborationClient:
        return DeveloperCollaborationClient(
            base_url=base_url,
            access_token=access_token,
            transport=transport,
        )

    monkeypatch.setattr(
        developer_collaboration_cli,
        "DeveloperCollaborationClient",
        client_factory,
    )


def _lease_payload(
    *,
    status: str = "active",
    revision: int = 1,
    report_revision: int = 5,
    include_workspace: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "lease_id": str(LEASE_ID),
        "report_id": str(REPORT_ID),
        "report_revision": report_revision,
        "owner_id": "codex-developer",
        "status": status,
        "revision": revision,
        "acquired_at": NOW,
        "heartbeat_at": "2026-07-24T00:01:00+00:00",
        "expires_at": "2026-07-24T00:16:00+00:00",
    }
    if status == "released":
        payload["released_at"] = "2026-07-24T00:02:00+00:00"
    if include_workspace:
        payload["developer_workspace"] = {
            "schema_version": "1.0",
            "task_id": "EXP-LILIES-TEST-001",
            "task_revision": 1,
            "run_id": "formal-run:developer-cli-001",
            "assignment_id": "55555555-5555-4555-8555-555555555555",
            "path": "/private/lilies/formal-developer-assignments/assignment-001",
            "manifest_digest": DIGEST,
            "policy_digest": "sha256:" + "b" * 64,
            "source_manifest_digest": SOURCE_DIGEST,
            "baseline_commit_sha": "a" * 40,
            "baseline_tree_sha": "b" * 40,
            "branch_ref": "refs/heads/main",
            "allowed_new_prefixes": ["platform", "scripts", "tests"],
            "allowed_new_files": ["pyproject.toml", "uv.lock"],
        }
    return payload


def _developer_response_payload(*, include_bindings: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "response_id": str(RESPONSE_ID),
        "outcome": "implemented",
        "commit_sha": "b" * 40,
        "generic_capability_changes": [
            "Added a typed developer collaboration command client."
        ],
        "new_contract_digest": DIGEST,
        "tests_run": [
            {
                "test_id": "developer-cli-test-0001",
                "command": "pytest tests/test_v04_13_developer_collaboration_cli.py",
                "exit_code": 0,
                "summary": "Developer CLI regression passed.",
                "evidence_ref": {
                    "evidence_id": "developer-cli-evidence-0001",
                    "kind": "test_run",
                    "digest": DIGEST,
                    "media_type": "application/json",
                    "label": "Developer CLI deterministic regression",
                    "captured_at": NOW,
                },
            }
        ],
        "browser_or_live_evidence": [],
        "known_limits": ["Local static developer identity only."],
        "reprobe_steps": [
            {
                "order": 1,
                "action": "Read the refreshed public workflow contract.",
                "expected": "The new generic capability is present.",
            }
        ],
    }
    if include_bindings:
        payload.update(
            {
                "channel_id": str(CHANNEL_ID),
                "report_id": str(REPORT_ID),
                "report_revision": 5,
                "created_at": "2026-07-24T00:03:00+00:00",
            }
        )
    return payload


def _source_promotion_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "assignment_id": "55555555-5555-4555-8555-555555555555",
        "channel_id": str(CHANNEL_ID),
        "report_id": str(REPORT_ID),
        "report_revision": 5,
        "lease_id": str(LEASE_ID),
        "response_id": str(RESPONSE_ID),
        "workspace_manifest_digest": DIGEST,
        "source_manifest_digest": SOURCE_DIGEST,
        "intent_digest": "sha256:" + "c" * 64,
        "branch_ref": "refs/heads/main",
        "parent_commit_sha": "a" * 40,
        "parent_tree_sha": "b" * 40,
        "commit_sha": "c" * 40,
        "tree_sha": "d" * 40,
        "changed_paths": ["tests/test_generic_intake.py"],
        "object_state": "object_created",
        "activation_state": "activated",
        "reload_status": "not_required",
        "effective": True,
        "reload_confirmed": False,
        "object_created_at": "2026-07-24T00:02:00Z",
        "activated_at": "2026-07-24T00:02:00Z",
        "process_instance_id": "66666666-6666-4666-8666-666666666666",
        "receipt_digest": "sha256:" + "d" * 64,
    }


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> tuple[int, dict[str, Any], str]:
    monkeypatch.setenv("LILIES_PLATFORM_BASE_URL", BASE_URL)
    monkeypatch.setenv("LILIES_COLLABORATION_DEVELOPER_TOKEN", DEVELOPER_TOKEN)
    exit_code = developer_collaboration_cli.main(arguments)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out else {}
    return exit_code, payload, captured.err


def test_parser_has_no_argv_token_surface() -> None:
    parser = developer_collaboration_cli.build_parser()
    help_text = parser.format_help()

    assert "--token-file" in help_text
    assert "--token " not in help_text
    assert "--access-token" not in help_text
    option_strings = {
        option
        for action in parser._actions  # noqa: SLF001 - assert the public CLI surface
        for option in action.option_strings
    }
    assert "--token" not in option_strings
    assert "--access-token" not in option_strings


def test_developer_token_is_read_only_from_environment_or_exact_0600_file(
    tmp_path: Path,
) -> None:
    assert (
        read_developer_token(environment_value=DEVELOPER_TOKEN, token_file=None)
        == DEVELOPER_TOKEN
    )

    token_file = tmp_path / "developer-token"
    token_file.write_text(DEVELOPER_TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    assert (
        read_developer_token(
            environment_value="ignored-environment-token-" + "z" * 32,
            token_file=token_file,
        )
        == DEVELOPER_TOKEN
    )

    for unsafe_mode in (0o400, 0o700, 0o640, 0o644):
        token_file.chmod(unsafe_mode)
        with pytest.raises(
            DeveloperCollaborationClientError,
            match="must have mode 0600",
        ):
            read_developer_token(
                environment_value=DEVELOPER_TOKEN,
                token_file=token_file,
            )

    token_file.chmod(0o600)
    symlink = tmp_path / "developer-token-link"
    symlink.symlink_to(token_file)
    with pytest.raises(DeveloperCollaborationClientError, match="must not be a symlink"):
        read_developer_token(
            environment_value=DEVELOPER_TOKEN,
            token_file=symlink,
        )


def test_preapproval_inbox_json_has_only_global_pending_signal_and_no_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hidden_markers = {
        "formal-task-super-secret",
        str(REPORT_ID),
        str(CHANNEL_ID),
        "hidden-evidence-digest",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/developer/collaboration/inbox"
        assert dict(request.url.params) == {
            "after": "0",
            "limit": "50",
            "route": "developer",
        }
        assert request.headers["authorization"] == f"Bearer {DEVELOPER_TOKEN}"
        assert DEVELOPER_TOKEN not in str(request.url)
        assert not request.content
        return httpx.Response(
            200,
            request=request,
            json={
                "reports": [],
                "claims": [],
                "pending_user_action": True,
                "next_cursor": 0,
            },
        )

    _install_http_client(monkeypatch, handler)
    exit_code, payload, stderr = _run_cli(
        monkeypatch,
        capsys,
        ["inbox", "--limit", "50", "--route", "developer"],
    )

    assert exit_code == 0
    assert payload == {
        "claims": [],
        "next_cursor": 0,
        "pending_user_action": True,
        "reports": [],
    }
    assert stderr == ""
    projection = json.dumps(payload, ensure_ascii=False, sort_keys=True) + stderr
    assert DEVELOPER_TOKEN not in projection
    assert all(marker not in projection for marker in hidden_markers)


def test_cli_lease_renew_release_and_respond_use_strict_json_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == f"Bearer {DEVELOPER_TOKEN}"
        assert DEVELOPER_TOKEN not in str(request.url)
        assert DEVELOPER_TOKEN not in request.content.decode("utf-8")
        if request.url.path.endswith("/lease/renew"):
            return httpx.Response(
                200,
                request=request,
                json=_lease_payload(revision=2),
            )
        if request.url.path.endswith("/lease/release"):
            return httpx.Response(
                200,
                request=request,
                json=_lease_payload(status="released", revision=3),
            )
        if request.url.path.endswith("/lease"):
            return httpx.Response(
                200,
                request=request,
                json=_lease_payload(include_workspace=True),
            )
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                request=request,
                json=_developer_response_payload(include_bindings=True),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    _install_http_client(monkeypatch, handler)
    commands = [
        [
            "lease",
            str(REPORT_ID),
            "--expected-report-revision",
            "4",
            "--idempotency-key",
            "developer-cli-lease-0001",
        ],
        [
            "renew",
            str(REPORT_ID),
            "--expected-lease-revision",
            "1",
            "--idempotency-key",
            "developer-cli-renew-0001",
        ],
        [
            "release",
            str(REPORT_ID),
            "--expected-lease-revision",
            "2",
            "--idempotency-key",
            "developer-cli-release-0001",
            "--reason",
            "Return the report to the durable inbox.",
        ],
    ]

    for command in commands:
        exit_code, payload, stderr = _run_cli(
            monkeypatch,
            capsys,
            command,
        )
        assert exit_code == 0
        assert payload["lease_id"] == str(LEASE_ID)
        assert payload["owner_id"] == "codex-developer"
        if command[0] == "lease":
            assert payload["developer_workspace"]["path"].startswith("/private/")
            assert payload["developer_workspace"]["manifest_digest"] == DIGEST
            assert (
                payload["developer_workspace"]["source_manifest_digest"]
                == SOURCE_DIGEST
            )
        assert stderr == ""

    response_file = tmp_path / "developer-response.json"
    response_file.write_text(
        json.dumps(_developer_response_payload(include_bindings=False)),
        encoding="utf-8",
    )
    exit_code, payload, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "respond",
            str(REPORT_ID),
            "--lease-id",
            str(LEASE_ID),
            "--expected-report-revision",
            "5",
            "--idempotency-key",
            "developer-cli-respond-0001",
            "--response-file",
            str(response_file),
        ],
    )
    assert exit_code == 0
    assert payload["response_id"] == str(RESPONSE_ID)
    assert payload["report_revision"] == 5
    assert stderr == ""

    assert observed[0][2] == {
        "expected_report_revision": 4,
        "idempotency_key": "developer-cli-lease-0001",
        "owner_id": "codex-developer",
        "ttl_seconds": 900,
    }
    assert observed[1][2] == {
        "expected_lease_revision": 1,
        "idempotency_key": "developer-cli-renew-0001",
        "owner_id": "codex-developer",
        "ttl_seconds": 900,
    }
    assert observed[2][2] == {
        "expected_lease_revision": 2,
        "idempotency_key": "developer-cli-release-0001",
        "owner_id": "codex-developer",
        "reason": "Return the report to the durable inbox.",
    }
    assert observed[3][2]["lease_id"] == str(LEASE_ID)
    assert observed[3][2]["lease_owner_id"] == "codex-developer"
    assert observed[3][2]["expected_report_revision"] == 5
    assert observed[3][2]["idempotency_key"] == "developer-cli-respond-0001"
    assert observed[3][2]["response"] == _developer_response_payload(
        include_bindings=False
    )


def test_cli_promote_uses_exact_source_promotion_route_and_strict_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == (
            f"/api/v1/developer/collaboration/reports/{REPORT_ID}/source-promotions"
        )
        assert request.headers["authorization"] == f"Bearer {DEVELOPER_TOKEN}"
        body = json.loads(request.content)
        observed.append(body)
        return httpx.Response(
            200,
            request=request,
            json=_source_promotion_payload(),
        )

    _install_http_client(monkeypatch, handler)
    exit_code, payload, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "promote",
            str(REPORT_ID),
            "--lease-id",
            str(LEASE_ID),
            "--expected-report-revision",
            "5",
            "--response-id",
            str(RESPONSE_ID),
            "--idempotency-key",
            "developer-cli-promote-0001",
            "--workspace-manifest-digest",
            DIGEST,
            "--source-manifest-digest",
            SOURCE_DIGEST,
        ],
    )
    assert exit_code == 0
    assert stderr == ""
    assert payload == _source_promotion_payload()
    assert observed == [
        {
            "idempotency_key": "developer-cli-promote-0001",
            "lease_id": str(LEASE_ID),
            "lease_owner_id": "codex-developer",
            "expected_report_revision": 5,
            "response_id": str(RESPONSE_ID),
            "workspace_manifest_digest": DIGEST,
            "source_manifest_digest": SOURCE_DIGEST,
        }
    ]


def test_http_error_json_redacts_bearer_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            request=request,
            json={
                "detail": {
                    "code": "report_revision_conflict",
                    "message": (
                        f"Authorization: Bearer {DEVELOPER_TOKEN}; "
                        f"token={DEVELOPER_TOKEN}"
                    ),
                }
            },
        )

    _install_http_client(monkeypatch, handler)
    exit_code, stdout, stderr = _run_cli(
        monkeypatch,
        capsys,
        [
            "lease",
            str(REPORT_ID),
            "--expected-report-revision",
            "4",
            "--idempotency-key",
            "developer-cli-error-0001",
        ],
    )

    assert exit_code == 1
    assert stdout == {}
    error = json.loads(stderr)
    assert error["error"]["code"] == "developer_collaboration_failed"
    assert error["error"]["status"] == 409
    assert DEVELOPER_TOKEN not in stderr
    assert "Authorization" not in stderr
    assert "token=" not in stderr
    assert "[redacted]" in stderr
