#!/usr/bin/env python3
"""Freeze the public EXP-LILIES-004 revision-1 package metadata.

This generator inventories only public customer materials. Protected Seed data
and its oracle are produced by the separate sealed acceptance generator.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / "EXP-LILIES-004"
    / "1"
)
FIXTURES = PACKAGE / "fixtures" / "public-inputs"
COMPOSE = Path(__file__).with_name("compose.yaml")
ENVIRONMENT_CONTROL = Path(__file__).with_name("environment_control.py")
PUBLIC_FIXTURE_GENERATOR = Path(__file__).with_name(
    "generate_public_fixtures.py"
)
FAULT_PROXY = Path(__file__).with_name("fault_proxy.py")
PREPARE_FAULT_CONNECTOR = Path(__file__).with_name(
    "prepare_fault_connector.py"
)
SEALED_SEED_RUNNER = Path(__file__).with_name("run_sealed_seed.py")
TASK_ID = "EXP-LILIES-004"
REVISION = 1
SOURCE_PROJECT = {
    "name": "ThingsBoard Community Edition",
    "repository_url": "https://github.com/thingsboard/thingsboard",
    "release": "v4.3.1.3",
    "commit_sha": "105351615126682762caf849619f0ea02df1faf3",
    "image_digest": (
        "sha256:398ea445740dccc40f34a3618ba845f56c63ee7a594d3b850fce2149b40a6bb3"
    ),
    "license": "Apache-2.0",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def file_entry(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(FIXTURES).as_posix(),
        "digest": digest(payload),
        "size_bytes": len(payload),
    }


def write_json(path: Path, value: Any) -> bytes:
    payload = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def write_yaml(path: Path, value: Any) -> bytes:
    payload = yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    path.write_bytes(payload)
    return payload


def command_version(*args: str) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    fixture_entries = [
        file_entry(path)
        for path in sorted(FIXTURES.glob("*.json"))
    ]
    if len(fixture_entries) != 5:
        raise RuntimeError("expected exactly five public fixture files")

    manifest_payload = write_json(
        PACKAGE / "fixtures" / "manifest.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "files": [
                {
                    **entry,
                    "path": f"public-inputs/{entry['path']}",
                }
                for entry in fixture_entries
            ],
        },
    )

    environment_payload = write_yaml(
        PACKAGE / "environment.lock",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "source_projects": [SOURCE_PROJECT],
            "compose_digest": digest(COMPOSE.read_bytes()),
            "ports": [
                {
                    "service": "thingsboard-http",
                    "host": "127.0.0.1",
                    "port": 19090,
                },
                {
                    "service": "thingsboard-mqtt",
                    "host": "127.0.0.1",
                    "port": 18884,
                },
                {
                    "service": "thingsboard-fault-proxy",
                    "host": "127.0.0.1",
                    "port": 19091,
                },
            ],
            "network_name": "exp-lilies-004-r1_default",
            "volumes": ["exp-lilies-004-r1-postgres-data"],
            "initialization_commands": [
                {
                    "name": "ensure-thingsboard",
                    "digest": digest(ENVIRONMENT_CONTROL.read_bytes()),
                },
                {
                    "name": "prepare-fault-test-connector",
                    "digest": digest(PREPARE_FAULT_CONNECTOR.read_bytes()),
                },
                {
                    "name": "launch-fault-proxy",
                    "digest": digest(FAULT_PROXY.read_bytes()),
                },
            ],
            "seed_commands": [
                {
                    "name": "generate-public-fixtures",
                    "digest": digest(PUBLIC_FIXTURE_GENERATOR.read_bytes()),
                },
                {
                    "name": "run-sealed-seed",
                    "digest": digest(SEALED_SEED_RUNNER.read_bytes()),
                },
            ],
            "health_checks": [
                {
                    "check_id": "http:thingsboard-login",
                    "kind": "http",
                    "url": "http://127.0.0.1:19090/login",
                    "expected_status": 200,
                    "timeout_seconds": 5.0,
                    "mandatory": True,
                },
                {
                    "check_id": "tcp:thingsboard-mqtt",
                    "kind": "tcp",
                    "host": "127.0.0.1",
                    "port": 18884,
                    "timeout_seconds": 5.0,
                    "mandatory": True,
                },
                {
                    "check_id": "tcp:thingsboard-fault-proxy",
                    "kind": "tcp",
                    "host": "127.0.0.1",
                    "port": 19091,
                    "timeout_seconds": 5.0,
                    "mandatory": True,
                },
            ],
            "secret_refs": [
                "secret:exp-lilies-004-environment-attestation",
                "secret:exp-lilies-004-thingsboard-builder-jwt",
            ],
            "attestation_secret_ref": (
                "secret:exp-lilies-004-environment-attestation"
            ),
            "python_version": platform.python_version(),
            "node_version": command_version("node", "--version").lstrip("v"),
            "docker_version": command_version(
                "docker",
                "version",
                "--format",
                "{{.Client.Version}}",
            ),
            "fixture_files": [
                {
                    **entry,
                    "path": f"public-inputs/{entry['path']}",
                }
                for entry in fixture_entries
            ],
            "fault_injections": [
                {
                    "name": "thingsboard-save-alarm-transient-503",
                    "activation_command_digest": digest(
                        b"fault-proxy:transient_503:count=1"
                    ),
                    "recovery_command_digest": digest(
                        b"fault-proxy:pass"
                    ),
                },
                {
                    "name": "thingsboard-save-alarm-permission-403",
                    "activation_command_digest": digest(
                        b"fault-proxy:permission_403"
                    ),
                    "recovery_command_digest": digest(
                        b"fault-proxy:pass"
                    ),
                },
            ],
            "provenance": "real_host",
        },
    )

    write_json(
        PACKAGE / "allowed-actions.json",
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "revision": REVISION,
            "readable_host_objects": [
                "thingsboard.getLatestTimeseries",
                "thingsboard.alarm_list",
            ],
            "writable_host_operations": ["thingsboard.saveAlarm"],
            "platform_actions": [
                "platform_contract_get",
                "platform_block_search",
                "platform_block_get",
                "platform_tool_catalog",
                "platform_connector_authorization_issue",
                "platform_application_create",
                "platform_application_get",
                "platform_draft_inspect",
                "platform_draft_apply",
                "platform_tests_run",
                "platform_run_start",
                "platform_run_get",
                "platform_run_resume",
                "platform_run_cancel",
                "platform_trace_get",
                "platform_artifact_read",
                "platform_publish",
            ],
            "network_hosts": ["127.0.0.1"],
            "model_access": True,
            "file_access": True,
            "connector_access": True,
            "permission_required_actions": ["thingsboard.saveAlarm"],
            "max_write_count": 64,
            "max_payload_bytes": 4 * 1024 * 1024,
            "compensation_actions": [],
            "prohibited_actions": [
                "read_platform_source",
                "read_platform_database",
                "read_protected",
                "modify_task_package",
                "install_unknown_adapter",
                "thingsboard.device_rpc",
            ],
            "validation_mode": "real_host",
        },
    )

    write_json(
        PACKAGE / "BUILDER_API_MANUAL.json",
        {
            "schema_version": (
                "v0.4.13-t01h-external-builder-api-manual-1"
            ),
            "authority": {
                "token_source": (
                    "Use only the task-issued platform bearer from the launch "
                    "handoff; never print or persist it."
                ),
                "owner_continuation": (
                    "This user-authorized direct Codex run uses the public owner "
                    "API. A future isolated Lilies assignment must use its "
                    "task-scoped rendered contract instead."
                ),
            },
            "discovery": {
                "openapi": "GET /openapi.json",
                "blocks": [
                    "GET /api/v1/blocks",
                    "GET /api/v1/blocks/{block_type}",
                    "GET /api/v1/blocks/{block_type}/manual",
                ],
                "connectors": [
                    "GET /api/v1/connectors/manifests/{connector_id}/{version}",
                    "GET /api/v1/connectors/manifests/{connector_id}/{version}/contract",
                ],
            },
            "model_lifecycle": {
                "rule": (
                    "Model preparation is separate from production inference. "
                    "Only explicitly evaluated versions may be promoted."
                ),
                "operations": [
                    "POST /api/v1/tabular-models/train",
                    "POST /api/v1/tabular-models/import",
                    "POST /api/v1/tabular-models/{model_id}/versions/{version}/fine-tune",
                    "POST /api/v1/tabular-models/{model_id}/versions/{version}/evaluate",
                    "POST /api/v1/model-deployments/{deployment_name}/promote",
                    "POST /api/v1/model-deployments/{deployment_name}/rollback",
                    "GET /api/v1/model-deployments/{deployment_name}",
                    "POST /api/v1/model-deployments/{deployment_name}/predict",
                    "POST /api/v1/model-deployments/{deployment_name}/drift",
                ],
            },
            "workflow": {
                "operations": [
                    "POST /api/v1/applications",
                    "GET /api/v1/applications/{application_id}/draft",
                    "POST /api/v1/applications/{application_id}/draft",
                    "POST /api/v1/applications/{application_id}/tests/run",
                    "POST /api/v1/applications/{application_id}/runs",
                    "GET /api/v1/runs/{run_id}",
                    "POST /api/v1/runs/{run_id}/resume",
                    "GET /api/v1/runs/{run_id}/events",
                    "POST /api/v1/applications/{application_id}/versions",
                ],
                "rules": [
                    "Inspect current revision before every draft mutation.",
                    "Use stable event identities for Connector idempotency.",
                    "Use bounded NodeSpec retry only for retryable status codes.",
                    "A permission denial is terminal and must not be presented as low risk.",
                    "Production inference may not train, fine-tune, promote, or roll back.",
                ],
            },
            "budget": {
                "single_lilies_run_token_limit": 1000000,
                "note": (
                    "Direct deterministic Codex API work consumes no Lilies "
                    "model tokens."
                ),
            },
        },
    )

    materials = [
        {
            "description": "Customer business request and safety boundary.",
            "kind": "customer_brief",
            "path": "requirement.md",
            "provided_by": "customer",
        },
        *[
            {
                "description": (
                    "Customer-authorized public model or workflow fixture."
                ),
                "kind": "customer_data",
                "path": f"fixtures/public-inputs/{entry['path']}",
                "provided_by": "customer",
            }
            for entry in fixture_entries
        ],
        {
            "description": "Public platform and model-lifecycle API guide.",
            "kind": "platform_manual",
            "path": "BUILDER_API_MANUAL.json",
            "provided_by": "platform",
        },
        {
            "description": "Task-scoped host and platform action boundary.",
            "kind": "permission_contract",
            "path": "allowed-actions.json",
            "provided_by": "task_author",
        },
        {
            "description": "Pinned real-host environment and public fixtures.",
            "kind": "system_environment_contract",
            "path": "environment.lock",
            "provided_by": "task_author",
        },
        {
            "description": "Customer role, deliverables, and acceptance summary.",
            "kind": "delivery_contract",
            "path": "task.yaml",
            "provided_by": "task_author",
        },
    ]
    write_json(
        PACKAGE / "CUSTOMER_REQUIREMENT_PACKAGE.json",
        {
            "schema_version": (
                "v0.4.13-customer-requirement-package-1"
            ),
            "task_id": TASK_ID,
            "revision": REVISION,
            "material_completeness": "substantial",
            "materials": materials,
            "missing_materials": [
                "No real-factory labeled failure history is supplied; synthetic data limits the model-accuracy claim.",
                "No external model-registry credential is supplied; the completed route trains a new model.",
                "No production ThingsBoard identity or network is supplied; validation is controlled-local only.",
            ],
        },
    )

    task = yaml.safe_load((PACKAGE / "task.yaml").read_text(encoding="utf-8"))
    task["source_projects"] = [SOURCE_PROJECT]
    task["environment_lock_digest"] = digest(environment_payload)
    task["fixture_manifest_digest"] = digest(manifest_payload)
    write_yaml(PACKAGE / "task.yaml", task)

    print(
        json.dumps(
            {
                "task_id": TASK_ID,
                "revision": REVISION,
                "public_fixture_count": len(fixture_entries),
                "environment_lock_digest": digest(environment_payload),
                "fixture_manifest_digest": digest(manifest_payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
