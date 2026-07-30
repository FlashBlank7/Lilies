from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_external_workflow_scenario.py"
)
SPEC = importlib.util.spec_from_file_location(
    "run_external_workflow_scenario",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_json_pointer_and_secret_stripping() -> None:
    value = {
        "records": [
            {
                "record_id": "A",
                "expected_decision": "hidden",
                "nested": {"expected_value": 1, "safe": True},
            }
        ]
    }

    selected = MODULE._json_pointer(value, "/records")
    stripped = MODULE._strip_key_prefixes(selected, ("expected_",))

    assert stripped == [{"record_id": "A", "nested": {"safe": True}}]


def test_workspace_must_remain_inside_root(tmp_path: Path) -> None:
    workspace = MODULE._resolve_workspace(tmp_path, "runs/seed-101")

    assert workspace == (tmp_path / "runs" / "seed-101").resolve()
    assert workspace.is_dir()
    with pytest.raises(MODULE.ScenarioError, match="escapes"):
        MODULE._resolve_workspace(tmp_path, "../outside")


def test_binding_rotation_preserves_public_binding_contract() -> None:
    binding = {
        "connector_id": "example",
        "connector_version": 3,
        "tenant_id": "tenant",
        "external_tenant_id": "external",
        "profile_id": "contract",
        "secret_ref": "secret://tenant/old",
        "application_ids": ["app"],
        "allowed_operations": ["read", "write"],
        "subjects": [
            {
                "external_subject": "builder",
                "actor_id": "builder",
                "roles": ["operator"],
            }
        ],
        "enabled": True,
        "revision": 7,
        "created_at": "ignored",
        "updated_at": "ignored",
    }

    request = MODULE._binding_body(binding, "secret://tenant/new")

    assert request["expected_revision"] == 7
    assert request["binding"]["revision"] == 8
    assert request["binding"]["secret_ref"] == "secret://tenant/new"
    assert "created_at" not in request["binding"]


def test_artifact_discovery_is_output_shape_agnostic() -> None:
    outputs = {
        "summary": {"count": 3},
        "nested": [
            {
                "artifact": {
                    "filename": "result.json",
                    "media_type": "application/json",
                    "size_bytes": 12,
                    "sha256": "sha256:abc",
                }
            }
        ],
    }

    assert MODULE._artifact_metadata(outputs) == [
        {
            "filename": "result.json",
            "media_type": "application/json",
            "size_bytes": 12,
            "sha256": "sha256:abc",
            "relative_path": None,
        }
    ]


def test_command_can_declare_a_nonzero_result_status(
    tmp_path: Path,
) -> None:
    receipts = MODULE._run_commands(
        [
            {
                "argv": [
                    "python",
                    "-c",
                    "raise SystemExit(3)",
                ],
                "accepted_returncodes": [0, 3],
            }
        ],
        cwd=tmp_path,
        phase="verification",
    )

    assert receipts[0]["returncode"] == 3
    assert receipts[0]["accepted_returncodes"] == [0, 3]
