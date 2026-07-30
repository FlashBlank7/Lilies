from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from . import __version__
from .capability_contracts import CapabilityBuildContract
from .lilies_models import PlatformScope
from .models import AgentSpec
from .workflow_models import (
    ApplicationSnapshot,
    BlockDefinition,
    EdgeSpec,
    NodeSpec,
    TestFrameSpec,
    WorkflowTestCase,
)


PLATFORM_CONTRACT_SCHEMA_VERSION = "1.0"
PLATFORM_CONTRACT_VERSION = 2
MAX_ARTIFACT_CHUNK_BYTES = 64 * 1024
DEFAULT_ARTIFACT_CHUNK_BYTES = MAX_ARTIFACT_CHUNK_BYTES
MAX_REGISTERED_ARTIFACT_BYTES = 2_000_000


def canonical_json(value: Any) -> str:
    """Encode a public contract value deterministically."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def public_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _object_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: Iterable[str] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": additional_properties,
    }


def _string(*, minimum: int = 1, maximum: int = 1_000) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _uuid_schema() -> dict[str, Any]:
    return {"type": "string", "format": "uuid"}


def _idempotency_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 16,
        "maxLength": 128,
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _array_schema(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _datetime_schema() -> dict[str, Any]:
    return {"type": "string", "format": "date-time"}


def _raw_sha256_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": r"^[0-9a-f]{64}$"}


def _prefixed_sha256_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}


def _closed_schema(
    properties: dict[str, Any],
    *,
    optional: Iterable[str] = (),
) -> dict[str, Any]:
    optional_fields = set(optional)
    return _object_schema(
        properties,
        required=(field for field in properties if field not in optional_fields),
    )


def public_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local definitions and remove implementation-oriented schema titles."""

    definitions = schema.get("$defs", {})

    def project(
        value: Any,
        stack: tuple[str, ...] = (),
        *,
        property_map: bool = False,
    ) -> Any:
        if isinstance(value, list):
            return [project(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in stack or name not in definitions:
                return {}
            resolved = project(definitions[name], (*stack, name))
            siblings = {
                key: project(item, stack)
                for key, item in value.items()
                if key not in {"$ref", "$defs", "title"}
            }
            return {**resolved, **siblings} if isinstance(resolved, dict) else siblings
        return {
            key: project(item, stack, property_map=key == "properties")
            for key, item in value.items()
            if key != "$defs" and (key != "title" or property_map)
        }

    projected = project(schema)
    return projected if isinstance(projected, dict) else {}


def _public_model_schema(model: Any, *, strict_root: bool = False) -> dict[str, Any]:
    schema = public_json_schema(model.model_json_schema())
    if strict_root and schema.get("type") == "object":
        schema["additionalProperties"] = False
    return schema


def _model_dump_schema(model: Any) -> dict[str, Any]:
    """Describe the exact JSON shape produced by a Pydantic ``model_dump``.

    Pydantic marks fields with defaults as optional inputs.  Public response
    projections, however, serialize those fields too.  Closing model-shaped
    objects and requiring every serialized property keeps response discovery
    aligned with the actual wire representation while preserving deliberately
    open dictionary fields such as node config and workflow outputs.
    """

    schema = _public_model_schema(model)

    def close(value: Any) -> Any:
        if isinstance(value, list):
            return [close(item) for item in value]
        if not isinstance(value, dict):
            return value
        projected = {key: close(item) for key, item in value.items()}
        if projected.get("type") == "object" and "properties" in projected:
            projected.setdefault("additionalProperties", False)
            projected["required"] = list(projected["properties"])
        return projected

    result = close(schema)
    return result if isinstance(result, dict) else {}


def _port_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "name": _string(maximum=160),
            "value_type": {
                "enum": [
                    "any",
                    "string",
                    "number",
                    "boolean",
                    "object",
                    "array",
                    "file",
                    "file_list",
                ]
            },
            "required": {"type": "boolean"},
            "multiple": {"type": "boolean"},
            "description": {"type": "string", "maxLength": 2_000},
        }
    )


def _block_definition_data_schema() -> dict[str, Any]:
    # BlockDefinition.model_dump() is the source of public_block_catalog.
    return _model_dump_schema(BlockDefinition)


def _block_manual_data_schema() -> dict[str, Any]:
    port = _port_data_schema()
    return _closed_schema(
        {
            "type": _string(maximum=160),
            "title": _string(maximum=500),
            "description": {"type": "string", "maxLength": 4_000},
            "category": {
                "enum": [
                    "input",
                    "model",
                    "agent",
                    "logic",
                    "transform",
                    "integration",
                    "output",
                ]
            },
            "block_kind": {
                "enum": [
                    "business_workflow",
                    "agent_architecture",
                    "legacy_compatibility",
                ]
            },
            "summary": {"type": "string", "maxLength": 4_000},
            "when_to_use": _array_schema({"type": "string"}),
            "input_ports": _array_schema(copy.deepcopy(port)),
            "output_ports": _array_schema(copy.deepcopy(port)),
            # JSON Schema documents and examples intentionally contain
            # capability-specific keys.  Their containing public records remain
            # closed and discoverable.
            "config_schema": _object_schema(additional_properties=True),
            "examples": _array_schema(_object_schema(additional_properties=True)),
            "anti_patterns": _array_schema({"type": "string"}),
            "common_errors": _array_schema({"type": "string"}),
            "claude_architecture_mapping": _nullable({"type": "string"}),
            "composability_constraints": _array_schema({"type": "string"}),
        }
    )


def _runtime_tool_data_schema() -> dict[str, Any]:
    core_tool = _closed_schema(
        {
            "name": _string(maximum=200),
            "type": {"const": "core"},
            "published": {"const": True},
            "description": {"type": "string", "maxLength": 4_000},
            "input_schema": _object_schema(additional_properties=True),
            "output_schema": _object_schema(additional_properties=True),
        }
    )
    workflow_tool = _closed_schema(
        {
            "name": {
                "type": "string",
                "pattern": r"^workflow:[0-9a-f-]{36}$",
            },
            "type": {"const": "workflow"},
            "title": _string(maximum=500),
            "version": {"type": "integer", "minimum": 1},
            "published": {"const": True},
        }
    )
    return {"oneOf": [core_tool, workflow_tool]}


def _contract_operation_data_schema() -> dict[str, Any]:
    correlation_headers = _closed_schema(
        {
            "Authorization": {"type": "string"},
            "X-Lilies-Assignment-ID": {"type": "string"},
            "X-Lilies-Session-ID": {"type": "string"},
            "X-Lilies-Tool-Call-ID": {"type": "string"},
            "X-Lilies-Idempotency-Key": {"type": "string"},
            "X-Lilies-Contract-Digest": {"type": "string"},
        }
    )
    response_headers = _closed_schema(
        {"X-Lilies-Idempotent-Replay": {"type": "string"}}
    )
    return _closed_schema(
        {
            "name": _string(maximum=160),
            "method": {"enum": ["GET", "POST"]},
            "path": _string(maximum=500),
            "scope": _string(maximum=160),
            "correlation_headers": correlation_headers,
            "response_headers": response_headers,
            "request_schema": _object_schema(additional_properties=True),
            "response_schema": _object_schema(additional_properties=True),
            "error_codes": _array_schema(_string(maximum=160)),
        }
    )


def _contract_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "schema_version": {"const": PLATFORM_CONTRACT_SCHEMA_VERSION},
            "contract_version": {"type": "integer", "minimum": 1},
            "contract_schema_digest": _prefixed_sha256_schema(),
            "platform_version": _string(maximum=120),
            "block_catalog_digest": _prefixed_sha256_schema(),
            "manual_catalog_digest": _prefixed_sha256_schema(),
            "tool_catalog_digest": _prefixed_sha256_schema(),
            "operations": _array_schema(_contract_operation_data_schema()),
            "runtime_capabilities": _closed_schema(
                {
                    "workflow_sources": _array_schema({"type": "string"}),
                    "run_control": _array_schema({"type": "string"}),
                    "evidence": _array_schema({"type": "string"}),
                    "connector_contracts": _array_schema({"type": "string"}),
                    "artifact_transport": _array_schema({"type": "string"}),
                }
            ),
            "known_boundaries": _array_schema(
                _closed_schema(
                    {
                        "code": _string(maximum=160),
                        "description": _string(maximum=4_000),
                    }
                )
            ),
            "contract_digest": _prefixed_sha256_schema(),
            "generated_at": _datetime_schema(),
        }
    )


def _delivery_policy_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "mode": {"enum": ["quick", "guided", "governed"]},
            "title": _string(maximum=200),
            "summary": _string(maximum=2_000),
            "publication_behavior": {
                "enum": ["advisory", "advisory_confirmation", "hard_gate"]
            },
            "missing_evidence_action": {"enum": ["warn", "confirm", "block"]},
            "stale_evidence_action": {"enum": ["warn", "confirm", "block"]},
            "recommended_evidence": _array_schema({"type": "string"}),
            "visible_controls": _array_schema({"type": "string"}),
            "warning_ack_required": {"type": "boolean"},
            "hard_gate_enabled": {"type": "boolean"},
        }
    )


def _evidence_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "state": {"enum": ["missing", "current", "stale"]},
            "current_hash": _raw_sha256_schema(),
            "last_tested_hash": _nullable(_raw_sha256_schema()),
            "latest_validation_failed": {"type": "boolean"},
            "invalidated_at": _nullable(_datetime_schema()),
            "invalidated_revision": _nullable({"type": "integer", "minimum": 0}),
            "change_summary": _array_schema(_object_schema(additional_properties=True)),
            "revalidate_endpoint": {"type": "string", "minLength": 1},
            "last_validation_report": _object_schema(additional_properties=True),
        },
        optional=("last_validation_report",),
    )


def _application_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "id": _uuid_schema(),
            "name": {"type": "string", "maxLength": 100},
            "description": {"type": "string", "maxLength": 1_000},
            "mode": {"enum": ["workflow", "chat"]},
            "delivery_mode": {"enum": ["quick", "guided", "governed"]},
            "governed_hard_gate": {"type": "boolean"},
            "requirement": {"type": "string", "maxLength": 30_000},
            "active_version": _nullable({"type": "integer", "minimum": 1}),
            "created_at": _datetime_schema(),
            "updated_at": _datetime_schema(),
            "draft_revision": {"type": "integer", "minimum": 0},
            "tested_hash": _nullable(_raw_sha256_schema()),
            "content_hash": _raw_sha256_schema(),
            "evidence_invalidated_at": _nullable(_datetime_schema()),
            "evidence_invalidated_revision": _nullable(
                {"type": "integer", "minimum": 0}
            ),
            "evidence_change_summary_json": {"type": "string"},
            "display_description": {"type": "string", "maxLength": 500},
            "delivery_policy": _delivery_policy_data_schema(),
            "evidence": _evidence_data_schema(),
        }
    )


def _draft_inspect_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "application_id": _uuid_schema(),
            "revision": {"type": "integer", "minimum": 0},
            "content_hash": _raw_sha256_schema(),
            "tested_hash": _nullable(_raw_sha256_schema()),
            "validation_contract_digest": {
                "anyOf": [
                    {"const": ""},
                    _prefixed_sha256_schema(),
                ]
            },
            "evidence_invalidated_at": _nullable(_datetime_schema()),
            "evidence_invalidated_revision": _nullable(
                {"type": "integer", "minimum": 0}
            ),
            "evidence_change_summary_json": {"type": "string"},
            "updated_at": _datetime_schema(),
            "delivery_mode": {"enum": ["quick", "guided", "governed"]},
            "governed_hard_gate": {"type": "boolean"},
            "validation_report": _object_schema(additional_properties=True),
            "delivery_policy": _delivery_policy_data_schema(),
            "evidence": _evidence_data_schema(),
            "snapshot": _model_dump_schema(ApplicationSnapshot),
            "preflight": _validation_data_schema(),
        }
    )


def _draft_apply_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "application_id": _uuid_schema(),
            "revision": {"type": "integer", "minimum": 1},
            "content_hash": _raw_sha256_schema(),
            "evidence_state": {"enum": ["missing", "current", "stale"]},
            "operation": {
                "enum": [
                    "add_node",
                    "update_node",
                    "remove_node",
                    "add_edge",
                    "remove_edge",
                    "set_metadata",
                    "upsert_agent",
                    "add_test",
                    "remove_test",
                    "set_capability_build_contract",
                ]
            },
        }
    )


def _validation_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "valid": {"type": "boolean"},
            "errors": _array_schema({"type": "string"}),
            "warnings": _array_schema({"type": "string"}),
            "revision": {"type": "integer", "minimum": 0},
            "content_hash": _raw_sha256_schema(),
            "test_count": {"type": "integer", "minimum": 0},
        }
    )


def _test_frame_data_schema() -> dict[str, Any]:
    return _model_dump_schema(TestFrameSpec)


def _test_assertion_result_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "path": _array_schema({"type": "string"}),
            "operator": {
                "enum": [
                    "exists",
                    "type",
                    "min_length",
                    "max_length",
                    "equals",
                    "contains",
                    "not_contains",
                ]
            },
            "expected": {},
            "structural": {"type": "boolean"},
            "passed": {"type": "boolean"},
            "actual": {},
            "error": {"type": "string"},
            "semantic_unwrap": {"type": "boolean"},
        },
        optional=("actual", "error", "semantic_unwrap"),
    )


def _test_result_data_schema() -> dict[str, Any]:
    assertion = _test_assertion_result_schema()
    failed_node = _closed_schema(
        {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "type": {"type": "string"},
            "error": {"type": "string"},
            "output_preview": {"type": "string"},
        }
    )
    readable_report = _closed_schema(
        {
            "title": {"type": "string"},
            "category": {"type": "string"},
            "purpose": {"type": "string"},
            "status": {"enum": ["passed", "failed"]},
            "mandatory": {"type": "boolean"},
            "reviewer_guidance": {"type": "string"},
            "reference": {"type": "string"},
            "failure_target": {"type": "string"},
            "failed_checks": _array_schema({"type": "string"}),
            "failed_assertions": _array_schema(copy.deepcopy(assertion)),
            "failure_code": {"type": "string"},
            "feedback_hints": _array_schema({"type": "string"}),
        },
        optional=("failure_code",),
    )
    tool_evidence = _closed_schema(
        {
            "used_tools": _array_schema({"type": "string"}),
            "required_tools": _array_schema({"type": "string"}),
            "required_tools_passed": {"type": "boolean"},
            "required_node_types": _array_schema({"type": "string"}),
            "node_types": _array_schema({"type": "string"}),
            "required_node_types_passed": {"type": "boolean"},
            "required_tool_nodes": _array_schema({"type": "string"}),
            "tool_node_names": _array_schema({"type": "string"}),
            "required_tool_nodes_passed": {"type": "boolean"},
            "minimum_tool_calls": {"type": "integer", "minimum": 0},
            "minimum_calls_passed": {"type": "boolean"},
            "output_urls": _array_schema({"type": "string"}),
            "cited_tool_urls": _array_schema({"type": "string"}),
            "unverified_output_urls": _array_schema({"type": "string"}),
            "citation_passed": {"type": "boolean"},
        }
    )
    return _closed_schema(
        {
            "test_id": {"type": "string"},
            "name": {"type": "string"},
            "mandatory": {"type": "boolean"},
            "passed": {"type": "boolean"},
            "run_id": {"type": "string"},
            "run_status": {
                "enum": [
                    "not_run",
                    "queued",
                    "running",
                    "paused",
                    "succeeded",
                    "failed",
                    "cancelled",
                ]
            },
            "run_error": {"type": "string"},
            "failure_code": {"type": "string"},
            "failed_node": _nullable(failed_node),
            "outputs": _object_schema(additional_properties=True),
            "frame": _test_frame_data_schema(),
            "readable_report": readable_report,
            "assertions": _array_schema(assertion),
            "tool_evidence": tool_evidence,
        }
    )


def _tests_run_data_schema() -> dict[str, Any]:
    summary_frame = _closed_schema(
        {
            "test_id": {"type": "string"},
            "title": {"type": "string"},
            "category": {"type": "string"},
            "status": {"enum": ["passed", "failed"]},
        }
    )
    summary = _closed_schema(
        {
            "total": {"type": "integer", "minimum": 0},
            "passed": {"type": "integer", "minimum": 0},
            "failed": {"type": "integer", "minimum": 0},
            "mandatory_failed": {"type": "integer", "minimum": 0},
            "frames": _array_schema(summary_frame),
        }
    )
    return _closed_schema(
        {
            "passed": {"type": "boolean"},
            "validation": _validation_data_schema(),
            "summary": summary,
            "tests": _array_schema(_test_result_data_schema()),
        }
    )


def _run_start_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "run_id": _uuid_schema(),
            "status": {"const": "queued"},
            "version": _nullable({"type": "integer", "minimum": 1}),
            "draft_revision": _nullable({"type": "integer", "minimum": 0}),
            "published_execution_policy_digest": _nullable(
                _prefixed_sha256_schema()
            ),
            "execution_policy_digest": _nullable(_prefixed_sha256_schema()),
        }
    )


def _artifact_metadata_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "artifact_id": _uuid_schema(),
            "relative_path": _string(maximum=1_024),
            "media_type": _string(maximum=200),
            "size_bytes": {"type": "integer", "minimum": 0},
            "sha256": _prefixed_sha256_schema(),
        }
    )


def _run_get_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "id": _uuid_schema(),
            "application_id": _uuid_schema(),
            "version": _nullable({"type": "integer", "minimum": 1}),
            "draft_revision": _nullable({"type": "integer", "minimum": 0}),
            "status": {
                "enum": ["queued", "running", "paused", "succeeded", "failed", "cancelled"]
            },
            "outputs": _object_schema(additional_properties=True),
            "error": _nullable({"type": "string"}),
            "waiting_node_id": _nullable({"type": "string"}),
            "completed_node_ids": _array_schema({"type": "string"}),
            "skipped_node_ids": _array_schema({"type": "string"}),
            "published_execution_policy_digest": _nullable(
                _prefixed_sha256_schema()
            ),
            "execution_policy_digest": _nullable(_prefixed_sha256_schema()),
            "created_at": _datetime_schema(),
            "updated_at": _datetime_schema(),
            "artifacts": _array_schema(_artifact_metadata_data_schema()),
            "host_receipts": _array_schema(_artifact_metadata_data_schema()),
        }
    )


def _run_resume_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {"run_id": _uuid_schema(), "status": {"const": "queued"}}
    )


def _run_cancel_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {"run_id": _uuid_schema(), "status": {"const": "cancelling"}}
    )


def _trace_data_schema() -> dict[str, Any]:
    event_data = _object_schema(
        {
            "attempt": {},
            "behavior": {},
            "branch": {},
            "code": {},
            "duration_ms": {},
            "error_type": {},
            "iteration": {},
            "level": {},
            "max_iterations": {},
            "mode": {},
            "node_id": {},
            "status": {},
            "title": {},
            "tool": {},
            "tool_name": {},
            "type": {},
        }
    )
    event = _closed_schema(
        {
            "seq": {"type": "integer", "minimum": 1},
            "type": {"type": "string"},
            "data": event_data,
            "created_at": _datetime_schema(),
        }
    )
    return _closed_schema(
        {
            "run_id": _uuid_schema(),
            "events": _array_schema(event),
            "next_after": {"type": "integer", "minimum": 0},
            "redacted": {"const": True},
        }
    )


def _artifact_read_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "artifact_id": _uuid_schema(),
            "run_id": _uuid_schema(),
            "relative_path": _string(maximum=1_024),
            "media_type": _string(maximum=200),
            "size_bytes": {"type": "integer", "minimum": 0},
            "sha256": _prefixed_sha256_schema(),
            "offset_bytes": {"type": "integer", "minimum": 0},
            "chunk_size_bytes": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_ARTIFACT_CHUNK_BYTES,
            },
            "next_offset_bytes": _nullable({"type": "integer", "minimum": 0}),
            "complete": {"type": "boolean"},
            "encoding": {"enum": ["utf8", "base64"]},
            "content": {"type": "string"},
        }
    )


def _execution_policy_snapshot_data_schema() -> dict[str, Any]:
    string_policy = _array_schema(_string(maximum=1_000))
    return _closed_schema(
        {
            "schema_version": {"const": "1.0"},
            "policy_digest": _prefixed_sha256_schema(),
            "workspace_scope": _closed_schema(
                {
                    "kind": {"const": "assignment_session"},
                    "digest": _prefixed_sha256_schema(),
                }
            ),
            "assignment_id": _uuid_schema(),
            "session_id": _uuid_schema(),
            "allowed_nested_application_ids": _array_schema(_uuid_schema()),
            "allowed_runtime_tools": string_policy,
            "allowed_network_hosts": string_policy,
            "model_access": {"type": "boolean"},
            "allowed_connector_operations": string_policy,
            "writable_connector_operations": string_policy,
            "permission_required_connector_operations": string_policy,
            "compensation_connector_operations": string_policy,
            "max_connector_write_count": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1_000_000,
            },
            "max_connector_payload_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100 * 1024 * 1024,
            },
            "governed_host_actions": {"type": "boolean"},
        }
    )


def _connector_authorization_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "authorization_id": _string(maximum=200),
            "issuance_source": {"const": "task_policy"},
            "connector_id": _string(maximum=160),
            "connector_version": {"type": "integer", "minimum": 1},
            "tenant_id": _string(maximum=300),
            "actor_id": _string(maximum=300),
            "profile_id": _string(maximum=160),
            "operation_id": _string(maximum=120),
            "operation_kind": {"enum": ["write", "compensate"]},
            "payload_hash": _prefixed_sha256_schema(),
            "policy_revision": {"type": "integer", "minimum": 1},
            "descriptor_digest": _prefixed_sha256_schema(),
            "task_credential_ref_digest": _prefixed_sha256_schema(),
            "task_policy_digest": _prefixed_sha256_schema(),
            "allowed_actions_digest": _prefixed_sha256_schema(),
            "budget_digest": _prefixed_sha256_schema(),
            "assignment_budget_policy_digest": _prefixed_sha256_schema(),
            "assignment_id": _uuid_schema(),
            "session_id": _uuid_schema(),
            "application_id": _uuid_schema(),
            "assignment_max_write_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000,
            },
            "assignment_max_payload_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100 * 1024 * 1024,
            },
            "assignment_write_count_at_issue": {
                "type": "integer",
                "minimum": 0,
                "maximum": 999_999,
            },
            "max_uses": {"const": 1},
            "expires_at": _datetime_schema(),
            "task_deadline_at": _datetime_schema(),
            "created_at": _datetime_schema(),
            "receipt_digest": _prefixed_sha256_schema(),
        }
    )


def _publication_decision_data_schema() -> dict[str, Any]:
    warning = _closed_schema(
        {"code": _string(maximum=160), "message": _string(maximum=4_000)}
    )
    return _closed_schema(
        {
            "application_id": _uuid_schema(),
            "allowed": {"type": "boolean"},
            "requires_confirmation": {"type": "boolean"},
            "blocked": {"type": "boolean"},
            "warning_codes": _array_schema({"type": "string"}),
            "warnings": _array_schema(warning),
            "evidence_state": {"enum": ["missing", "current", "stale"]},
            "evidence": _evidence_data_schema(),
            "policy": _delivery_policy_data_schema(),
            "policy_source": {"type": "string"},
            "acknowledged_warnings": {"type": "boolean"},
            "decided_at": _datetime_schema(),
            "execution_policy_snapshot": _execution_policy_snapshot_data_schema(),
        }
    )


def _publish_data_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "application_id": _uuid_schema(),
            "version": {"type": "integer", "minimum": 1},
            "content_hash": _raw_sha256_schema(),
            "publication_decision": _publication_decision_data_schema(),
        }
    )


def _draft_operation_data_schemas() -> dict[str, dict[str, Any]]:
    """Return the complete public payload grammar for every DraftOperation."""

    node_schema = _public_model_schema(NodeSpec)
    node_properties = node_schema.get("properties", {})
    node_type = node_properties.get("type")
    if isinstance(node_type, dict):
        node_type["description"] = (
            "A public block type returned by platform_block_search; inspect it with "
            "platform_block_get before constructing config."
        )
    node_config = node_properties.get("config")
    if isinstance(node_config, dict):
        node_config["description"] = (
            "Must satisfy the config_schema returned by platform_block_get for this node's "
            "type. The DraftOperation contract intentionally does not duplicate dynamic block "
            "configuration schemas."
        )
        node_config["x-lilies-schema-source"] = "platform_block_get(node.type).data.config_schema"

    node_changes = _object_schema(copy.deepcopy(node_properties))
    node_changes["description"] = (
        "A partial NodeSpec. Only listed NodeSpec fields may change; config is merged unless "
        "merge_config is false."
    )

    metadata = _object_schema(
        {
            "name": _string(maximum=100),
            "description": {"type": "string", "maxLength": 1_000},
            "mode": {"enum": ["workflow", "chat"]},
            "delivery_mode": {"enum": ["quick", "guided", "governed"]},
            "governed_hard_gate": {"type": "boolean"},
            "requirement": {"type": "string", "maxLength": 30_000},
        }
    )
    metadata["minProperties"] = 1

    test_schema = _public_model_schema(WorkflowTestCase, strict_root=True)
    test_inputs = test_schema.get("properties", {}).get("inputs")
    if isinstance(test_inputs, dict):
        test_inputs["propertyNames"] = {"not": {"pattern": "^__"}}
    return {
        "add_node": _object_schema(
            {"node": node_schema},
            required=("node",),
        ),
        "update_node": _object_schema(
            {
                "node_id": _string(maximum=160),
                "changes": node_changes,
                "merge_config": {"type": "boolean", "default": True},
            },
            required=("node_id", "changes"),
        ),
        "remove_node": _object_schema(
            {"node_id": _string(maximum=160)},
            required=("node_id",),
        ),
        "add_edge": _object_schema(
            {"edge": _public_model_schema(EdgeSpec)},
            required=("edge",),
        ),
        "remove_edge": _object_schema(
            {"edge_id": _string(maximum=160)},
            required=("edge_id",),
        ),
        "set_metadata": metadata,
        "upsert_agent": _object_schema(
            {"agent": _public_model_schema(AgentSpec)},
            required=("agent",),
        ),
        "add_test": _object_schema(
            {"test": test_schema},
            required=("test",),
        ),
        "remove_test": _object_schema(
            {"test_id": _string(maximum=160)},
            required=("test_id",),
        ),
        "set_capability_build_contract": _object_schema(
            {"contract": _public_model_schema(CapabilityBuildContract)},
            required=("contract",),
        ),
    }


def _draft_apply_request_schema() -> dict[str, Any]:
    data_schemas = _draft_operation_data_schemas()
    operation_names = list(data_schemas)
    schema = _object_schema(
        {
            "application_id": _APPLICATION_ID,
            "expected_revision": {"type": "integer", "minimum": 0},
            "idempotency_key": _idempotency_schema(),
            "op": {
                "enum": operation_names,
                "description": "Selects exactly one data grammar from the conditional branches.",
            },
            "data": {"type": "object"},
        },
        required=(
            "application_id",
            "expected_revision",
            "idempotency_key",
            "op",
            "data",
        ),
    )
    schema["allOf"] = [
        {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "op": {"const": operation_name},
                        "data": data_schema,
                    },
                    "required": ["op", "data"],
                }
                for operation_name, data_schema in data_schemas.items()
            ]
        }
    ]
    schema["x-lilies-discriminator"] = "op"
    return schema


PUBLIC_FACADE_COMMON_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "authorization_denied",
        "correlation_conflict",
        "credential_expired",
        "credential_revoked",
        "idempotency_conflict",
        "internal_endpoint_denied",
        "invalid_contract_digest",
        "invalid_correlation",
        "invalid_request",
        "missing_correlation",
        "platform_auth_unavailable",
        "platform_operation_failed",
        "request_conflict",
        "request_in_progress",
    }
)
PUBLIC_CLIENT_COMMON_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "platform_client_failure",
        "platform_result_too_large",
        "platform_unavailable",
        "protocol_error",
    }
)
PUBLIC_CONTRACT_DEPENDENT_ERROR_CODES = frozenset(
    {"contract_drift", "contract_not_loaded", "operation_not_available"}
)
PUBLIC_FACADE_OPERATION_ERROR_CODES: dict[str, frozenset[str]] = {
    "platform_contract_get": frozenset(),
    "platform_block_search": frozenset(),
    "platform_block_get": frozenset({"not_found"}),
    "platform_tool_catalog": frozenset(),
    "platform_connector_authorization_issue": frozenset(
        {
            "application_not_assigned",
            "connector_authorization_budget_exhausted",
            "connector_authorization_denied",
            "connector_authorization_payload_too_large",
            "connector_descriptor_drift",
            "not_found",
        }
    ),
    "platform_application_create": frozenset({"not_found"}),
    "platform_application_get": frozenset({"application_not_assigned", "not_found"}),
    "platform_draft_inspect": frozenset({"application_not_assigned", "not_found"}),
    "platform_draft_apply": frozenset(
        {
            "application_not_assigned",
            "not_found",
            "revision_conflict",
            "runtime_tool_scope_denied",
        }
    ),
    "platform_tests_run": frozenset(
        {
            "application_not_assigned",
            "invalid_state",
            "nested_workflow_scope_denied",
            "not_found",
            "runtime_network_scope_denied",
            "runtime_secret_scope_denied",
            "runtime_tool_scope_denied",
            "workspace_boundary_violation",
        }
    ),
    "platform_run_start": frozenset(
        {
            "application_not_assigned",
            "execution_policy_expansion_denied",
            "invalid_state",
            "nested_workflow_scope_denied",
            "not_found",
            "runtime_network_scope_denied",
            "runtime_secret_scope_denied",
            "runtime_tool_scope_denied",
            "workspace_boundary_violation",
        }
    ),
    "platform_run_get": frozenset(
        {
            "application_not_assigned",
            "artifact_conflict",
            "artifact_error",
            "artifact_integrity_failed",
            "artifact_path_unsafe",
            "artifact_store_unavailable",
            "artifact_too_large",
            "not_found",
        }
    ),
    "platform_run_resume": frozenset(
        {
            "application_not_assigned",
            "invalid_state",
            "nested_workflow_scope_denied",
            "not_found",
            "runtime_network_scope_denied",
            "runtime_secret_scope_denied",
            "runtime_tool_scope_denied",
            "workspace_boundary_violation",
        }
    ),
    "platform_run_cancel": frozenset(
        {"application_not_assigned", "invalid_state", "not_found"}
    ),
    "platform_trace_get": frozenset({"application_not_assigned", "not_found"}),
    "platform_artifact_read": frozenset(
        {
            "application_not_assigned",
            "artifact_conflict",
            "artifact_error",
            "artifact_integrity_failed",
            "artifact_path_unsafe",
            "artifact_range_invalid",
            "artifact_store_unavailable",
            "artifact_too_large",
            "not_found",
        }
    ),
    "platform_publish": frozenset(
        {
            "application_not_assigned",
            "nested_workflow_scope_denied",
            "not_found",
            "publish_gate_failed",
            "runtime_network_scope_denied",
            "runtime_secret_scope_denied",
            "runtime_tool_scope_denied",
            "workspace_boundary_violation",
        }
    ),
}


def _platform_error_schema() -> dict[str, Any]:
    return _closed_schema(
        {
            "code": _string(maximum=120),
            "message": _string(maximum=4_000),
            "retryable": {"type": "boolean"},
            "failure_owner": {
                "enum": [
                    "lilies",
                    "user_permission",
                    "task_author",
                    "environment",
                    "platform",
                ]
            },
            "expected": {},
            "actual": {},
            "evidence_ref": _nullable({"type": "string"}),
        }
    )


def _operation_response_schema(name: str, data_schema: dict[str, Any]) -> dict[str, Any]:
    empty_data = _object_schema()
    error_schema = _platform_error_schema()
    envelope = _closed_schema(
        {
            "ok": {"type": "boolean"},
            "operation": {"const": name},
            "request_id": _uuid_schema(),
            "status_code": {"type": "integer", "minimum": 100, "maximum": 599},
            "contract_digest": _prefixed_sha256_schema(),
            "data": {
                "anyOf": [copy.deepcopy(data_schema), copy.deepcopy(empty_data)]
            },
            "error": {"anyOf": [{"type": "null"}, copy.deepcopy(error_schema)]},
            "evidence_refs": _array_schema({"type": "string"}),
        }
    )
    envelope["oneOf"] = [
        {
            "properties": {
                "ok": {"const": True},
                "status_code": {"type": "integer", "minimum": 200, "maximum": 299},
                "data": copy.deepcopy(data_schema),
                "error": {"type": "null"},
            },
            "required": ["ok", "status_code", "data", "error"],
        },
        {
            "properties": {
                "ok": {"const": False},
                "status_code": {"type": "integer", "minimum": 400, "maximum": 599},
                "data": copy.deepcopy(empty_data),
                "error": copy.deepcopy(error_schema),
            },
            "required": ["ok", "status_code", "data", "error"],
        },
    ]
    envelope["x-lilies-success-data-branch"] = 0
    envelope["x-lilies-error-branch"] = 1
    return envelope


def _operation(
    name: str,
    *,
    method: str,
    path: str,
    scope: PlatformScope,
    request_schema: dict[str, Any],
    data_schema: dict[str, Any],
    errors: Iterable[str] = (),
) -> dict[str, Any]:
    error_codes = set(PUBLIC_FACADE_COMMON_ERROR_CODES)
    error_codes.update(PUBLIC_CLIENT_COMMON_ERROR_CODES)
    error_codes.update(PUBLIC_FACADE_OPERATION_ERROR_CODES[name])
    if name != "platform_contract_get":
        error_codes.update(PUBLIC_CONTRACT_DEPENDENT_ERROR_CODES)
    error_codes.update(errors)
    return {
        "name": name,
        "method": method,
        "path": path,
        "scope": scope.value,
        "correlation_headers": {
            "Authorization": "Bearer <task credential>",
            "X-Lilies-Assignment-ID": "UUID",
            "X-Lilies-Session-ID": "UUID",
            "X-Lilies-Tool-Call-ID": "1-200 safe correlation characters",
            "X-Lilies-Idempotency-Key": "16-128 safe correlation characters",
            "X-Lilies-Contract-Digest": "sha256:<64 lowercase hex characters>",
        },
        "response_headers": {
            "X-Lilies-Idempotent-Replay": (
                "true only when the response body is the exact result of an earlier "
                "successful use of the same assignment idempotency key"
            )
        },
        "request_schema": request_schema,
        "response_schema": _operation_response_schema(name, data_schema),
        "error_codes": sorted(error_codes),
    }


_APPLICATION_ID = _uuid_schema()
_RUN_ID = _uuid_schema()
PUBLIC_OPERATION_SPECS: tuple[dict[str, Any], ...] = (
    _operation(
        "platform_contract_get",
        method="GET",
        path="/api/v1/lilies/platform-contract",
        scope=PlatformScope.catalog_read,
        request_schema=_object_schema(),
        data_schema=_contract_data_schema(),
    ),
    _operation(
        "platform_block_search",
        method="GET",
        path="/api/v1/lilies/blocks",
        scope=PlatformScope.catalog_read,
        request_schema=_object_schema(
            {
                "query": {"type": "string", "maxLength": 500},
                "block_kind": {"anyOf": [{"type": "string", "maxLength": 120}, {"type": "null"}]},
            }
        ),
        data_schema=_array_schema(_block_definition_data_schema()),
        errors=("invalid_request",),
    ),
    _operation(
        "platform_block_get",
        method="GET",
        path="/api/v1/lilies/blocks/{block_type}",
        scope=PlatformScope.catalog_read,
        request_schema=_object_schema(
            {"block_type": _string(maximum=160)},
            required=("block_type",),
        ),
        data_schema=_closed_schema(
            {
                "definition": _block_definition_data_schema(),
                "manual": _block_manual_data_schema(),
            }
        ),
        errors=("not_found",),
    ),
    _operation(
        "platform_tool_catalog",
        method="GET",
        path="/api/v1/lilies/tools",
        scope=PlatformScope.catalog_read,
        request_schema=_object_schema(),
        data_schema=_array_schema(_runtime_tool_data_schema()),
    ),
    _operation(
        "platform_connector_authorization_issue",
        method="POST",
        path=(
            "/api/v1/lilies/applications/{application_id}/"
            "connector-authorizations"
        ),
        scope=PlatformScope.run_execute,
        request_schema=_object_schema(
            {
                "application_id": _APPLICATION_ID,
                "connector_id": _string(maximum=160),
                "connector_version": {"type": "integer", "minimum": 1},
                "tenant_id": _string(maximum=300),
                "actor_id": _string(maximum=300),
                "profile_id": _string(maximum=160),
                "operation_id": _string(maximum=120),
                "operation_kind": {"enum": ["write", "compensate"]},
                "descriptor_digest": _prefixed_sha256_schema(),
                "payload": _object_schema(additional_properties=True),
                "expires_in_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 300,
                },
                "idempotency_key": _idempotency_schema(),
            },
            required=(
                "application_id",
                "connector_id",
                "connector_version",
                "tenant_id",
                "actor_id",
                "profile_id",
                "operation_id",
                "operation_kind",
                "descriptor_digest",
                "payload",
                "idempotency_key",
            ),
        ),
        data_schema=_connector_authorization_data_schema(),
        errors=(
            "application_not_assigned",
            "connector_authorization_budget_exhausted",
            "connector_authorization_denied",
            "connector_authorization_payload_too_large",
            "connector_descriptor_drift",
            "idempotency_conflict",
            "invalid_request",
            "not_found",
        ),
    ),
    _operation(
        "platform_application_create",
        method="POST",
        path="/api/v1/lilies/applications",
        scope=PlatformScope.application_write,
        request_schema=_object_schema(
            {
                "name": _string(maximum=100),
                "description": {"type": "string", "maxLength": 1_000},
                "requirement": {"type": "string", "maxLength": 30_000},
                "mode": {"enum": ["workflow", "chat"]},
                "delivery_mode": {"enum": ["quick", "guided", "governed"]},
                "governed_hard_gate": {"type": "boolean"},
                "idempotency_key": _idempotency_schema(),
            },
            required=("name", "idempotency_key"),
        ),
        data_schema=_application_data_schema(),
        errors=("idempotency_conflict", "invalid_request", "not_found"),
    ),
    _operation(
        "platform_application_get",
        method="GET",
        path="/api/v1/lilies/applications/{application_id}",
        scope=PlatformScope.application_write,
        request_schema=_object_schema(
            {"application_id": _APPLICATION_ID}, required=("application_id",)
        ),
        data_schema=_application_data_schema(),
        errors=("application_not_assigned", "not_found"),
    ),
    _operation(
        "platform_draft_inspect",
        method="GET",
        path="/api/v1/lilies/applications/{application_id}/draft",
        scope=PlatformScope.draft_write,
        request_schema=_object_schema(
            {"application_id": _APPLICATION_ID}, required=("application_id",)
        ),
        data_schema=_draft_inspect_data_schema(),
        errors=("application_not_assigned", "not_found"),
    ),
    _operation(
        "platform_draft_apply",
        method="POST",
        path="/api/v1/lilies/applications/{application_id}/draft",
        scope=PlatformScope.draft_write,
        request_schema=_draft_apply_request_schema(),
        data_schema=_draft_apply_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "invalid_request",
            "revision_conflict",
        ),
    ),
    _operation(
        "platform_tests_run",
        method="POST",
        path="/api/v1/lilies/applications/{application_id}/tests/run",
        scope=PlatformScope.test_execute,
        request_schema=_object_schema(
            {
                "application_id": _APPLICATION_ID,
                "idempotency_key": _idempotency_schema(),
            },
            required=("application_id", "idempotency_key"),
        ),
        data_schema=_tests_run_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "nested_workflow_scope_denied",
            "not_found",
            "workspace_boundary_violation",
        ),
    ),
    _operation(
        "platform_run_start",
        method="POST",
        path="/api/v1/lilies/applications/{application_id}/runs",
        scope=PlatformScope.run_execute,
        request_schema=_object_schema(
            {
                "application_id": _APPLICATION_ID,
                "inputs": {
                    **_object_schema(additional_properties=True),
                    "propertyNames": {"not": {"pattern": "^__"}},
                },
                "version": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
                "use_draft": {"type": "boolean"},
                "idempotency_key": _idempotency_schema(),
            },
            required=("application_id", "idempotency_key"),
        ),
        data_schema=_run_start_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "invalid_request",
            "nested_workflow_scope_denied",
            "not_found",
            "workspace_boundary_violation",
        ),
    ),
    _operation(
        "platform_run_get",
        method="GET",
        path="/api/v1/lilies/runs/{run_id}",
        scope=PlatformScope.run_execute,
        request_schema=_object_schema({"run_id": _RUN_ID}, required=("run_id",)),
        data_schema=_run_get_data_schema(),
        errors=("application_not_assigned", "not_found"),
    ),
    _operation(
        "platform_run_resume",
        method="POST",
        path="/api/v1/lilies/runs/{run_id}/resume",
        scope=PlatformScope.run_execute,
        request_schema=_object_schema(
            {
                "run_id": _RUN_ID,
                "values": _object_schema(additional_properties=True),
                "idempotency_key": _idempotency_schema(),
            },
            required=("run_id", "values", "idempotency_key"),
        ),
        data_schema=_run_resume_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "invalid_state",
            "nested_workflow_scope_denied",
            "not_found",
            "workspace_boundary_violation",
        ),
    ),
    _operation(
        "platform_run_cancel",
        method="POST",
        path="/api/v1/lilies/runs/{run_id}/cancel",
        scope=PlatformScope.run_execute,
        request_schema=_object_schema(
            {
                "run_id": _RUN_ID,
                "idempotency_key": _idempotency_schema(),
            },
            required=("run_id", "idempotency_key"),
        ),
        data_schema=_run_cancel_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "invalid_state",
            "not_found",
        ),
    ),
    _operation(
        "platform_trace_get",
        method="GET",
        path="/api/v1/lilies/runs/{run_id}/trace",
        scope=PlatformScope.trace_read,
        request_schema=_object_schema(
            {
                "run_id": _RUN_ID,
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2_000},
            },
            required=("run_id",),
        ),
        data_schema=_trace_data_schema(),
        errors=("application_not_assigned", "not_found"),
    ),
    _operation(
        "platform_artifact_read",
        method="GET",
        path="/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
        scope=PlatformScope.artifact_read,
        request_schema=_object_schema(
            {
                "run_id": _RUN_ID,
                "artifact_id": _uuid_schema(),
                "offset_bytes": {"type": "integer", "minimum": 0},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_ARTIFACT_CHUNK_BYTES,
                },
            },
            required=("run_id", "artifact_id"),
        ),
        data_schema=_artifact_read_data_schema(),
        errors=(
            "application_not_assigned",
            "artifact_path_unsafe",
            "artifact_range_invalid",
            "artifact_too_large",
            "not_found",
        ),
    ),
    _operation(
        "platform_publish",
        method="POST",
        path="/api/v1/lilies/applications/{application_id}/versions",
        scope=PlatformScope.application_publish,
        request_schema=_object_schema(
            {
                "application_id": _APPLICATION_ID,
                "acknowledge_warnings": {"type": "boolean"},
                "idempotency_key": _idempotency_schema(),
            },
            required=("application_id", "idempotency_key"),
        ),
        data_schema=_publish_data_schema(),
        errors=(
            "application_not_assigned",
            "idempotency_conflict",
            "publish_gate_failed",
        ),
    ),
)


def public_runtime_tool_catalog(
    tools: Any,
    *,
    allowed_runtime_tool_names: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    allowed = (
        None
        if allowed_runtime_tool_names is None
        else frozenset(str(name) for name in allowed_runtime_tool_names)
    )
    return [
        {
            "name": name,
            "type": "core",
            "published": True,
            "description": tools.get(name).description,
            "input_schema": public_json_schema(tools.get(name).input_model.model_json_schema()),
            "output_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "is_error": {"type": "boolean"},
                },
                "required": ["content", "is_error"],
                "additionalProperties": False,
            },
        }
        for name in tools.names()
        if allowed is None or name in allowed
    ]


def public_block_catalog(blocks: Any) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for definition in blocks.list():
        if definition.block_kind == "legacy_compatibility" or not definition.available:
            continue
        payload = definition.model_dump(mode="json")
        payload["config_schema"] = public_json_schema(payload["config_schema"])
        catalog.append(payload)
    return catalog


def public_block_manual(blocks: Any, block_type: str) -> dict[str, Any]:
    definition = blocks.get(block_type)
    if definition.block_kind == "legacy_compatibility" or not definition.available:
        raise KeyError(f"unknown public block type: {block_type}")
    manual = blocks.manual(block_type)
    manual["config_schema"] = public_json_schema(manual["config_schema"])
    return manual


def _catalog_payloads(
    blocks: Any,
    tools: Any,
    *,
    allowed_runtime_tool_names: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[Any], list[Any]]:
    block_catalog = public_block_catalog(blocks)
    manual_catalog = [public_block_manual(blocks, item["type"]) for item in block_catalog]
    tool_catalog = public_runtime_tool_catalog(
        tools,
        allowed_runtime_tool_names=allowed_runtime_tool_names,
    )
    return block_catalog, manual_catalog, tool_catalog


def _static_contract_semantics() -> dict[str, Any]:
    return {
        "runtime_capabilities": {
            "workflow_sources": ["draft", "published_version"],
            "run_control": ["start", "inspect", "resume", "cancel"],
            "evidence": ["acceptance_report", "redacted_trace", "scoped_artifact"],
            "connector_contracts": ["registered_tools", "published_workflows"],
            "artifact_transport": ["utf8", "base64"],
        },
        "known_boundaries": [
            {
                "code": "scope_filtered_contract",
                "description": "Operations absent from the bearer credential scopes are omitted.",
            },
            {
                "code": "incremental_draft_only",
                "description": "Draft edits accept exactly one public operation and never replace a graph.",
            },
            {
                "code": "no_internal_access",
                "description": "Database, source, private service, and arbitrary platform endpoints are unavailable.",
            },
            {
                "code": "trace_redaction",
                "description": "Trace projections omit secret-bearing fields and private model reasoning.",
            },
            {
                "code": "bounded_result_transport",
                "description": (
                    "Model-facing platform results are atomic JSON envelopes; oversized results "
                    "return platform_result_too_large, while artifacts are read in digest-verified "
                    "raw-byte chunks of at most 65536 bytes."
                ),
            },
            {
                "code": "registered_artifact_size_limit",
                "description": (
                    "A run artifact must be at most 2000000 bytes to enter the task-scoped "
                    "registry; chunked reads do not remove this whole-file registration limit."
                ),
            },
            {
                "code": "assigned_runtime_policy",
                "description": (
                    "Assigned black-box runs may use only the safe runtime tools returned by "
                    "this contract and connector operations/hosts projected by their credential; "
                    "platform secret references remain unavailable."
                ),
            },
            {
                "code": "immutable_execution_policy_snapshot",
                "description": (
                    "Task-scoped publication binds a digest-addressed assignment/session "
                    "workspace, model flag, tool/nested-application/connector allowlists and "
                    "connector budgets to the immutable version. Later callers may only narrow "
                    "that policy."
                ),
            },
            {
                "code": "exact_connector_authorization",
                "description": (
                    "A task credential can obtain a one-use connector mutation "
                    "receipt only for an exact descriptor and payload already "
                    "allowed by its immutable assignment/application policy, "
                    "budget and deadline; internal owner endpoints remain "
                    "unavailable."
                ),
            },
            {
                "code": "scheduled_publish_not_supported",
                "description": (
                    "Drafts containing schedule_trigger cannot be published through a task "
                    "credential because trigger authority is not represented by the immutable "
                    "execution-policy snapshot."
                ),
            },
            {
                "code": "raw_network_publish_not_supported",
                "description": (
                    "Raw HTTP and web-collection blocks cannot be published through a task "
                    "credential; network side effects must use governed connector operations."
                ),
            },
        ],
    }


def public_contract_schema_digest() -> str:
    """Digest the platform-wide schema generation, excluding contextual catalogs."""

    return public_digest(
        {
            "schema_version": PLATFORM_CONTRACT_SCHEMA_VERSION,
            "operations": PUBLIC_OPERATION_SPECS,
            **_static_contract_semantics(),
        }
    )


def build_platform_contract(
    blocks: Any,
    tools: Any,
    *,
    scopes: Iterable[PlatformScope | str],
    published_workflow_tools: Iterable[dict[str, Any]] = (),
    published_connector_tools: Iterable[dict[str, Any]] = (),
    generated_at: datetime | None = None,
    contract_version: int = PLATFORM_CONTRACT_VERSION,
    allowed_runtime_tool_names: Iterable[str] | None = None,
    allowed_operation_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a role/scope-filtered public contract without implementation identifiers."""

    allowed_scopes = {str(getattr(scope, "value", scope)) for scope in scopes}
    block_catalog, manual_catalog, tool_catalog = _catalog_payloads(
        blocks,
        tools,
        allowed_runtime_tool_names=allowed_runtime_tool_names,
    )
    tool_catalog.extend(dict(item) for item in published_workflow_tools)
    tool_catalog.extend(dict(item) for item in published_connector_tools)
    operation_allowlist = (
        None
        if allowed_operation_names is None
        else frozenset(str(name) for name in allowed_operation_names)
    )
    operations = [
        copy.deepcopy(operation)
        for operation in PUBLIC_OPERATION_SPECS
        if operation["scope"] in allowed_scopes
        and (
            operation_allowlist is None
            or operation["name"] in operation_allowlist
        )
    ]
    stable = {
        "schema_version": PLATFORM_CONTRACT_SCHEMA_VERSION,
        "contract_version": contract_version,
        "contract_schema_digest": public_contract_schema_digest(),
        "platform_version": __version__,
        "block_catalog_digest": public_digest(block_catalog),
        "manual_catalog_digest": public_digest(manual_catalog),
        "tool_catalog_digest": public_digest(tool_catalog),
        "operations": operations,
        **_static_contract_semantics(),
    }
    return {
        **stable,
        "contract_digest": public_digest(stable),
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
    }


def validate_contract_digest(contract: dict[str, Any]) -> bool:
    supplied = contract.get("contract_digest")
    stable = {
        key: value
        for key, value in contract.items()
        if key not in {"contract_digest", "generated_at"}
    }
    return isinstance(supplied, str) and supplied == public_digest(stable)


def operation_by_name(name: str) -> dict[str, Any]:
    try:
        return next(operation for operation in PUBLIC_OPERATION_SPECS if operation["name"] == name)
    except StopIteration as error:
        raise KeyError(f"unknown public platform operation: {name}") from error


def operation_request_schema(name: str) -> dict[str, Any]:
    """Return an isolated copy of the model-facing public request schema."""

    return copy.deepcopy(operation_by_name(name)["request_schema"])
