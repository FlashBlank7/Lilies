from __future__ import annotations

import ast
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_platform.lilies_models import PlatformScope
from agent_platform.lilies_platform_client import (
    MAX_PLATFORM_CONTRACT_VERSION,
    ZERO_CONTRACT_DIGEST,
    LiliesPlatformClient,
    LiliesPlatformProtocolError,
    PlatformToolEnvelope,
)
from agent_platform.lilies_platform_contract import (
    PLATFORM_CONTRACT_VERSION,
    PUBLIC_CLIENT_COMMON_ERROR_CODES,
    PUBLIC_CONTRACT_DEPENDENT_ERROR_CODES,
    PUBLIC_FACADE_COMMON_ERROR_CODES,
    PUBLIC_FACADE_OPERATION_ERROR_CODES,
    PUBLIC_OPERATION_DESCRIPTIONS,
    PUBLIC_OPERATION_SPECS,
    build_platform_contract,
    public_digest,
    public_runtime_tool_catalog,
    validate_contract_digest,
)
from agent_platform.lilies_platform_tools import build_lilies_platform_registry
from agent_platform.lilies_tools import LiliesToolContext


CONTRACT_DIGEST = "sha256:" + "a" * 64
NEW_CONTRACT_DIGEST = "sha256:" + "b" * 64
PLATFORM_TOOL_NAMES = {
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
}


class _CatalogRecord:
    def __init__(self, **value: Any) -> None:
        self.value = value
        self.block_kind = str(value["block_kind"])
        self.available = True

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return dict(self.value)


class _Blocks:
    def __init__(self) -> None:
        self.record = _CatalogRecord(
            type="start",
            block_kind="business_workflow",
            category="control",
            title="Start",
            config_schema={
                "title": "Private Python title",
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    def list(self) -> list[_CatalogRecord]:
        return [self.record]

    def get(self, block_type: str) -> _CatalogRecord:
        assert block_type == "start"
        return self.record

    def manual(self, block_type: str) -> dict[str, Any]:
        assert block_type == "start"
        return {
            "type": block_type,
            "summary": "Public start block.",
            "config_schema": dict(self.record.value["config_schema"]),
        }


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RuntimeTool:
    description = "A public runtime tool."
    input_model = _ToolInput


class _Tools:
    def names(self) -> list[str]:
        return ["http_request", "web_search"]

    def get(self, name: str) -> _RuntimeTool:
        assert name in self.names()
        return _RuntimeTool()


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    digest: str | None = CONTRACT_DIGEST,
    token: str = "platform-secret-token-value",
) -> LiliesPlatformClient:
    return LiliesPlatformClient(
        base_url="https://platform.test/root",
        access_token=token,
        assignment_id=uuid4(),
        session_id=uuid4(),
        contract_digest=digest,
        transport=transport,
    )


def _envelope(
    operation: str,
    *,
    status_code: int = 200,
    digest: str = CONTRACT_DIGEST,
    data: Any = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": status_code < 400,
        "operation": operation,
        "request_id": str(uuid4()),
        "status_code": status_code,
        "contract_digest": digest,
        "data": {} if data is None else data,
        "error": error,
        "evidence_refs": [],
    }


def _contract_error(*, expected: str, actual: str) -> dict[str, Any]:
    return {
        "code": "contract_drift",
        "message": "The supplied contract digest is no longer current.",
        "retryable": True,
        "failure_owner": "platform",
        "expected": expected,
        "actual": actual,
        "evidence_ref": None,
    }


def _redigest_contract(contract: dict[str, Any]) -> dict[str, Any]:
    contract["contract_digest"] = public_digest(
        {
            key: value
            for key, value in contract.items()
            if key not in {"contract_digest", "generated_at"}
        }
    )
    return contract


def _minimal_schema_value(schema: dict[str, Any]) -> Any:
    """Build a minimal value without relying on application implementation models."""

    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("anyOf"):
        options = [item for item in schema["anyOf"] if item.get("type") != "null"]
        return _minimal_schema_value(options[0])
    value_type = schema.get("type")
    if value_type == "object":
        properties = schema.get("properties", {})
        required = list(schema.get("required", []))
        if not required and schema.get("minProperties", 0) > 0:
            required = [next(iter(properties))]
        return {field: _minimal_schema_value(properties[field]) for field in required}
    if value_type == "array":
        count = int(schema.get("minItems", 0))
        return [_minimal_schema_value(schema.get("items", {})) for _ in range(count)]
    if value_type == "integer":
        return int(schema.get("minimum", 0))
    if value_type == "number":
        return float(schema.get("minimum", 0))
    if value_type == "boolean":
        return False
    if value_type == "string":
        if schema.get("format") == "uuid":
            return "00000000-0000-0000-0000-000000000001"
        return "x" * max(1, int(schema.get("minLength", 0)))
    return None


def _draft_payload_from_tool_schema(schema: dict[str, Any], operation: str) -> dict[str, Any]:
    properties = schema["properties"]
    common = {
        field: _minimal_schema_value(properties[field])
        for field in schema["required"]
        if field not in {"op", "data"}
    }
    branches = schema["allOf"][0]["oneOf"]
    selected = next(
        branch for branch in branches if branch["properties"]["op"]["const"] == operation
    )
    return {**common, **_minimal_schema_value(selected)}


def test_public_contract_and_local_registry_expose_exactly_the_seventeen_http_tools() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    platform_only = build_lilies_platform_registry(client, include_core_tools=False)
    combined = build_lilies_platform_registry(client)

    assert len(PUBLIC_OPERATION_SPECS) == 17
    assert {operation["name"] for operation in PUBLIC_OPERATION_SPECS} == PLATFORM_TOOL_NAMES
    assert PLATFORM_CONTRACT_VERSION == 4
    assert set(PUBLIC_OPERATION_DESCRIPTIONS) == PLATFORM_TOOL_NAMES
    assert set(platform_only.names()) == PLATFORM_TOOL_NAMES
    assert {
        name for name in combined.names() if name.startswith("platform_")
    } == PLATFORM_TOOL_NAMES
    assert all(
        definition.input_schema.get("additionalProperties") is False
        for definition in platform_only.definitions()
    )
    contract_operations = {item["name"]: item for item in PUBLIC_OPERATION_SPECS}
    registry_definitions = {item.name: item for item in platform_only.definitions()}
    assert all(
        contract_operations[name]["description"]
        == PUBLIC_OPERATION_DESCRIPTIONS[name]
        == registry_definitions[name].description
        for name in PLATFORM_TOOL_NAMES
    )
    block_search_schema = contract_operations["platform_block_search"]["request_schema"]
    assert "block_kind" not in block_search_schema["required"]
    assert block_search_schema["properties"]["block_kind"] == {
        "enum": [
            "business_workflow",
            "agent_architecture",
            "legacy_compatibility",
        ]
    }
    assert block_search_schema["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
        "default": 12,
    }
    block_search_tool = platform_only.get("platform_block_search")
    assert (
        block_search_tool.input_model.model_validate(
            {"block_kind": "business_workflow"}
        ).block_kind
        == "business_workflow"
    )
    with pytest.raises(ValidationError):
        block_search_tool.input_model.model_validate({"block_kind": "workflow"})
    with pytest.raises(ValidationError):
        block_search_tool.input_model.model_validate({"limit": 51})
    draft_schema = platform_only.get("platform_draft_apply").definition().input_schema
    assert "replace_workflow" not in draft_schema["properties"]["op"]["enum"]
    assert "replace_tests" not in draft_schema["properties"]["op"]["enum"]

    artifact_contract_schema = next(
        operation["request_schema"]
        for operation in PUBLIC_OPERATION_SPECS
        if operation["name"] == "platform_artifact_read"
    )
    artifact_tool_schema = platform_only.get("platform_artifact_read").definition().input_schema
    assert artifact_tool_schema == artifact_contract_schema
    assert artifact_tool_schema["properties"]["artifact_id"] == {
        "type": "string",
        "format": "uuid",
    }
    assert artifact_tool_schema["properties"]["offset_bytes"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert artifact_tool_schema["properties"]["max_bytes"]["maximum"] == 65_536


def test_all_seventeen_response_schemas_are_strict_discoverable_success_or_error_envelopes() -> None:
    expected_success_fields = {
        "platform_contract_get": {
            "schema_version",
            "contract_version",
            "platform_version",
            "block_catalog_digest",
            "manual_catalog_digest",
            "tool_catalog_digest",
            "operations",
            "runtime_capabilities",
            "known_boundaries",
            "contract_digest",
            "generated_at",
        },
        "platform_block_search": set(),
        "platform_block_get": {"definition", "manual"},
        "platform_tool_catalog": set(),
        "platform_connector_authorization_issue": {
            "authorization_id",
            "connector_id",
            "operation_id",
            "operation_kind",
            "payload_hash",
            "descriptor_digest",
            "assignment_id",
            "session_id",
            "application_id",
            "receipt_digest",
        },
        "platform_application_create": {"id", "draft_revision", "content_hash", "evidence"},
        "platform_application_get": {"id", "draft_revision", "content_hash", "evidence"},
        "platform_draft_inspect": {
            "application_id",
            "revision",
            "content_hash",
            "snapshot",
            "validation_report",
        },
        "platform_draft_apply": {
            "application_id",
            "revision",
            "content_hash",
            "evidence_state",
            "operation",
        },
        "platform_tests_run": {"passed", "validation", "summary", "tests"},
        "platform_run_start": {"run_id", "status", "version", "draft_revision"},
        "platform_run_get": {
            "id",
            "application_id",
            "status",
            "outputs",
            "artifacts",
        },
        "platform_run_resume": {"run_id", "status"},
        "platform_run_cancel": {"run_id", "status"},
        "platform_trace_get": {"run_id", "events", "next_after", "redacted"},
        "platform_artifact_read": {
            "artifact_id",
            "run_id",
            "relative_path",
            "media_type",
            "size_bytes",
            "sha256",
            "offset_bytes",
            "chunk_size_bytes",
            "next_offset_bytes",
            "complete",
            "encoding",
            "content",
        },
        "platform_publish": {
            "application_id",
            "version",
            "content_hash",
            "publication_decision",
        },
    }

    assert set(expected_success_fields) == PLATFORM_TOOL_NAMES
    for operation in PUBLIC_OPERATION_SPECS:
        name = operation["name"]
        response = operation["response_schema"]
        assert response["type"] == "object"
        assert response["additionalProperties"] is False
        assert set(response["required"]) == {
            "ok",
            "operation",
            "request_id",
            "status_code",
            "contract_digest",
            "data",
            "error",
            "evidence_refs",
        }
        success, failure = response["oneOf"]
        assert success["properties"]["ok"] == {"const": True}
        assert success["properties"]["error"] == {"type": "null"}
        assert failure["properties"]["ok"] == {"const": False}
        assert failure["properties"]["data"]["additionalProperties"] is False
        assert failure["properties"]["data"]["properties"] == {}
        error = failure["properties"]["error"]
        assert error["additionalProperties"] is False
        assert set(error["required"]) == {
            "code",
            "message",
            "retryable",
            "failure_owner",
            "expected",
            "actual",
            "evidence_ref",
        }

        data = success["properties"]["data"]
        if data.get("type") == "object":
            assert data["additionalProperties"] is False
            assert data["properties"]
            assert expected_success_fields[name] <= set(data["required"])
        else:
            assert data["type"] == "array"
            item = data["items"]
            branches = item.get("oneOf", [item])
            assert all(branch["additionalProperties"] is False for branch in branches)
            assert all(branch["properties"] for branch in branches)


def test_declared_error_codes_cover_every_central_facade_and_client_mapping() -> None:
    operations = {item["name"]: set(item["error_codes"]) for item in PUBLIC_OPERATION_SPECS}

    for name, operation_codes in PUBLIC_FACADE_OPERATION_ERROR_CODES.items():
        expected = {
            *PUBLIC_FACADE_COMMON_ERROR_CODES,
            *PUBLIC_CLIENT_COMMON_ERROR_CODES,
            *operation_codes,
        }
        if name != "platform_contract_get":
            expected.update(PUBLIC_CONTRACT_DEPENDENT_ERROR_CODES)
        assert expected <= operations[name]

    for name in ("platform_tests_run", "platform_run_start", "platform_run_resume"):
        assert {
            "invalid_state",
            "runtime_network_scope_denied",
            "runtime_tool_scope_denied",
        } <= operations[name]
    assert {
        "artifact_conflict",
        "artifact_error",
        "artifact_integrity_failed",
        "artifact_range_invalid",
        "artifact_store_unavailable",
    } <= operations["platform_artifact_read"]
    assert "platform_result_too_large" in PUBLIC_CLIENT_COMMON_ERROR_CODES
    assert all(
        "platform_result_too_large" in operation["error_codes"]
        for operation in PUBLIC_OPERATION_SPECS
    )


def test_runtime_tool_catalog_can_be_filtered_without_changing_legacy_default() -> None:
    tools = _Tools()
    legacy = public_runtime_tool_catalog(tools)
    filtered = public_runtime_tool_catalog(
        tools,
        allowed_runtime_tool_names=["web_search", "not-registered"],
    )

    assert [item["name"] for item in legacy] == ["http_request", "web_search"]
    assert [item["name"] for item in filtered] == ["web_search"]
    contract = build_platform_contract(
        _Blocks(),
        tools,
        scopes=list(PlatformScope),
        allowed_runtime_tool_names=["web_search"],
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    assert contract["tool_catalog_digest"] == public_digest(filtered)


def test_draft_tool_schema_is_contract_owned_strict_and_complete_for_all_operations() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    tool = build_lilies_platform_registry(client, include_core_tools=False).get(
        "platform_draft_apply"
    )
    tool_schema = tool.definition().input_schema
    contract_schema = next(
        operation["request_schema"]
        for operation in PUBLIC_OPERATION_SPECS
        if operation["name"] == "platform_draft_apply"
    )

    assert tool_schema == contract_schema
    assert tool_schema is not contract_schema
    assert tool_schema["additionalProperties"] is False
    assert set(tool_schema["required"]) == {
        "application_id",
        "expected_revision",
        "idempotency_key",
        "op",
        "data",
    }

    branches = tool_schema["allOf"][0]["oneOf"]
    by_operation = {
        branch["properties"]["op"]["const"]: branch["properties"]["data"] for branch in branches
    }
    assert set(by_operation) == set(tool_schema["properties"]["op"]["enum"])
    assert len(by_operation) == 10
    assert all(schema["additionalProperties"] is False for schema in by_operation.values())
    assert {operation: set(schema["required"]) for operation, schema in by_operation.items()} == {
        "add_node": {"node"},
        "update_node": {"node_id", "changes"},
        "remove_node": {"node_id"},
        "add_edge": {"edge"},
        "remove_edge": {"edge_id"},
        "set_metadata": set(),
        "upsert_agent": {"agent"},
        "add_test": {"test"},
        "remove_test": {"test_id"},
        "set_capability_build_contract": {"contract"},
    }
    assert by_operation["set_metadata"]["minProperties"] == 1
    node_config = by_operation["add_node"]["properties"]["node"]["properties"]["config"]
    assert node_config["additionalProperties"] is True
    assert node_config["x-lilies-schema-source"] == (
        "platform_block_get(node.type).data.config_schema"
    )
    assert by_operation["add_test"]["properties"]["test"]["properties"]["inputs"][
        "propertyNames"
    ] == {"not": {"pattern": "^__"}}


@pytest.mark.asyncio
async def test_reserved_runtime_inputs_are_rejected_by_contract_client_and_tools(
    tmp_path: Path,
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"reserved input reached HTTP: {request.url}")

    client = _client(httpx.MockTransport(unreachable))
    run_schema = next(
        operation["request_schema"]
        for operation in PUBLIC_OPERATION_SPECS
        if operation["name"] == "platform_run_start"
    )
    assert run_schema["properties"]["inputs"]["propertyNames"] == {
        "not": {"pattern": "^__"}
    }
    with pytest.raises(ValueError, match="reserved runtime input"):
        await client.invoke(
            "platform_run_start",
            {
                "application_id": str(uuid4()),
                "inputs": {"__governed_memory__": {}},
                "idempotency_key": "reserved-client-run-0001",
            },
            tool_call_id="reserved-client-run",
        )
    with pytest.raises(ValueError, match="reserved runtime input"):
        await client.invoke(
            "platform_draft_apply",
            {
                "application_id": str(uuid4()),
                "expected_revision": 0,
                "op": "add_test",
                "data": {
                    "test": {
                        "name": "Human bypass",
                        "requirement": "Must not self-sign trusted runtime context.",
                        "inputs": {"__human__": {"approved": True}},
                    }
                },
                "idempotency_key": "reserved-client-test-0001",
            },
            tool_call_id="reserved-client-test",
        )

    registry = build_lilies_platform_registry(client, include_core_tools=False)
    context = LiliesToolContext(
        session_id=str(client.session_id),
        workspace=tmp_path,
        tool_call_id="reserved-tool-call",
    )
    run_result = json.loads(
        (
            await registry.get("platform_run_start").execute(
                {
                    "application_id": str(uuid4()),
                    "inputs": {"__job__": {"trusted": True}},
                    "idempotency_key": "reserved-tool-run-0001",
                },
                context,
            )
        ).content
    )
    assert run_result["status_code"] == 422
    assert run_result["error"]["code"] == "invalid_request"
    draft_result = json.loads(
        (
            await registry.get("platform_draft_apply").execute(
                {
                    "application_id": str(uuid4()),
                    "expected_revision": 0,
                    "op": "add_test",
                    "data": {
                        "test": {
                            "name": "Reserved test",
                            "requirement": "Must fail local validation.",
                            "inputs": {"__human__": {}},
                        }
                    },
                    "idempotency_key": "reserved-tool-test-0001",
                },
                context,
            )
        ).content
    )
    assert draft_result["status_code"] == 422
    assert draft_result["error"]["code"] == "invalid_request"


def test_held_out_minimal_draft_payloads_are_constructible_from_tool_schema_alone() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(500, request=request)))
    tool = build_lilies_platform_registry(client, include_core_tools=False).get(
        "platform_draft_apply"
    )
    schema = tool.definition().input_schema

    payloads = {
        operation: _draft_payload_from_tool_schema(schema, operation)
        for operation in ("add_node", "update_node", "add_edge", "add_test")
    }

    assert set(payloads["add_node"]["data"]["node"]) == {"type", "title"}
    assert payloads["update_node"]["data"] == {"node_id": "x", "changes": {}}
    assert set(payloads["add_edge"]["data"]["edge"]) == {"source", "target"}
    assert set(payloads["add_test"]["data"]["test"]) == {"name", "requirement"}
    for operation, payload in payloads.items():
        assert payload["op"] == operation
        assert set(payload) == {
            "application_id",
            "expected_revision",
            "idempotency_key",
            "op",
            "data",
        }
        validated = tool.input_model.model_validate(payload)
        assert validated.op == operation


def test_runtime_isolation_failures_are_declared_by_the_actual_scoped_contract() -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    operations = {item["name"]: item for item in contract["operations"]}
    boundaries = {item["code"]: item for item in contract["known_boundaries"]}

    assert "bounded_result_transport" in boundaries
    assert "platform_result_too_large" in boundaries["bounded_result_transport"][
        "description"
    ]
    assert "65536" in boundaries["bounded_result_transport"]["description"]

    for operation in ("platform_tests_run", "platform_run_start", "platform_run_resume"):
        assert {
            "nested_workflow_scope_denied",
            "workspace_boundary_violation",
        } <= set(operations[operation]["error_codes"])


def test_client_and_tool_adapter_have_an_http_only_import_boundary() -> None:
    source_root = Path(__file__).parents[1] / "platform/backend/src/agent_platform"
    imported_modules: set[str] = set()
    for filename in ("lilies_platform_client.py", "lilies_platform_tools.py"):
        tree = ast.parse((source_root / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    forbidden = {
        "api",
        "applications",
        "blocks",
        "database",
        "workflow_runtime",
        "workflow_storage",
    }
    assert not {name.rsplit(".", 1)[-1] for name in imported_modules} & forbidden
    assert "sqlite3" not in imported_modules
    assert "httpx" in imported_modules


def test_contract_digest_is_canonical_and_excludes_only_observation_time() -> None:
    scopes = list(PlatformScope)
    first = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=scopes,
        generated_at=datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc),
    )
    second = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=reversed(scopes),
        generated_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    assert first["generated_at"] != second["generated_at"]
    assert first["contract_digest"] == second["contract_digest"]
    assert first["contract_schema_digest"] == second["contract_schema_digest"]
    assert validate_contract_digest(first)
    assert validate_contract_digest(second)
    assert public_digest({"b": 2, "a": 1}) == public_digest({"a": 1, "b": 2})

    first["operations"][0]["path"] = "/tampered"
    rebuilt = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=scopes,
        generated_at=datetime(2040, 1, 1, tzinfo=timezone.utc),
    )
    assert rebuilt["operations"][0]["path"] != "/tampered"
    assert rebuilt["contract_digest"] == second["contract_digest"]
    assert not validate_contract_digest(first)

    second["platform_version"] = "tampered"
    assert not validate_contract_digest(second)

    contextual = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=[PlatformScope.catalog_read],
        published_workflow_tools=[
            {
                "name": "workflow:00000000-0000-0000-0000-000000000001",
                "type": "workflow",
                "title": "Assigned workflow",
                "version": 1,
                "published": True,
            }
        ],
    )
    assert contextual["contract_digest"] != rebuilt["contract_digest"]
    assert contextual["contract_schema_digest"] == rebuilt["contract_schema_digest"]


def test_contract_filters_operations_by_exact_credential_scope() -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=[PlatformScope.catalog_read, PlatformScope.trace_read],
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )

    operations = contract["operations"]
    assert {operation["name"] for operation in operations} == {
        "platform_contract_get",
        "platform_block_search",
        "platform_block_get",
        "platform_tool_catalog",
        "platform_trace_get",
    }
    assert {operation["scope"] for operation in operations} == {
        PlatformScope.catalog_read.value,
        PlatformScope.trace_read.value,
    }
    serialized = json.dumps(contract, sort_keys=True)
    assert "workflow.draft:write" not in serialized
    assert "platform_publish" not in serialized


def test_result_envelope_is_strict_and_enforces_result_error_consistency() -> None:
    valid = PlatformToolEnvelope.model_validate(_envelope("platform_run_get"))
    assert valid.ok is True
    assert isinstance(valid.request_id, UUID)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlatformToolEnvelope.model_validate(
            {**_envelope("platform_run_get"), "private_debug": "do not expose"}
        )
    with pytest.raises(ValidationError, match="failed platform result must carry an error"):
        PlatformToolEnvelope.model_validate(
            _envelope("platform_run_get", status_code=403, error=None)
        )
    with pytest.raises(ValidationError, match="successful platform result cannot carry an error"):
        PlatformToolEnvelope.model_validate(
            {
                **_envelope("platform_run_get"),
                "error": _contract_error(expected=CONTRACT_DIGEST, actual=NEW_CONTRACT_DIGEST),
            }
        )


@pytest.mark.asyncio
async def test_contract_fetch_uses_zero_digest_then_binds_verified_digest() -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        digest = contract["contract_digest"]
        return httpx.Response(
            200,
            request=request,
            json=_envelope("platform_contract_get", digest=digest, data=contract),
        )

    client = _client(httpx.MockTransport(handler), digest=None)
    result = await client.contract_get(tool_call_id="tool-call-contract-1")

    assert result.ok
    assert client.contract_digest == contract["contract_digest"]
    assert seen[0].headers["X-Lilies-Contract-Digest"] == ZERO_CONTRACT_DIGEST
    assert seen[0].headers["X-Lilies-Tool-Call-ID"] == "tool-call-contract-1"


@pytest.mark.asyncio
async def test_contract_fetch_rejects_tampered_contract_even_in_a_valid_envelope() -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    contract["runtime_capabilities"]["private_service"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                "platform_contract_get",
                digest=contract["contract_digest"],
                data=contract,
            ),
        )

    client = _client(httpx.MockTransport(handler), digest=None)
    with pytest.raises(LiliesPlatformProtocolError, match="digest validation failed"):
        await client.contract_get(tool_call_id="tool-call-contract-2")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 1, "schema_version is unsupported"),
        ("schema_version", "2.0", "schema_version is unsupported"),
        ("contract_version", True, "outside the supported range"),
        ("contract_version", 0, "outside the supported range"),
        (
            "contract_version",
            MAX_PLATFORM_CONTRACT_VERSION + 1,
            "outside the supported range",
        ),
        ("contract_version", "1", "outside the supported range"),
        ("contract_schema_digest", None, "contract_schema_digest is invalid"),
        ("contract_schema_digest", "sha256:ABC", "contract_schema_digest is invalid"),
    ],
)
@pytest.mark.asyncio
async def test_contract_fetch_rejects_unsupported_schema_and_contract_versions(
    field: str,
    value: Any,
    message: str,
) -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    contract[field] = value
    _redigest_contract(contract)

    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json=_envelope(
                    "platform_contract_get",
                    digest=contract["contract_digest"],
                    data=contract,
                ),
            )
        ),
        digest=None,
    )
    with pytest.raises(LiliesPlatformProtocolError, match=message):
        await client.contract_get(tool_call_id="tool-call-contract-version-invalid")
    assert client.contract_schema_version is None
    assert client.highest_contract_version is None
    assert client.contract_schema_digest is None
    assert client.contract_digest is None


@pytest.mark.asyncio
async def test_contract_client_rejects_version_rollback_and_same_version_digest_drift() -> None:
    first = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        contract_version=2,
    )
    rollback = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        contract_version=1,
    )
    same_version_drift = copy.deepcopy(first)
    same_version_drift["contract_schema_digest"] = "sha256:" + "f" * 64
    _redigest_contract(same_version_drift)
    responses = iter((first, rollback, same_version_drift))

    def handler(request: httpx.Request) -> httpx.Response:
        contract = next(responses)
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                "platform_contract_get",
                digest=contract["contract_digest"],
                data=contract,
            ),
        )

    client = _client(httpx.MockTransport(handler), digest=None)
    accepted = await client.contract_get(tool_call_id="tool-contract-version-two")
    assert accepted.ok
    assert client.contract_schema_version == "1.0"
    assert client.highest_contract_version == 2
    assert client.contract_schema_digest == first["contract_schema_digest"]
    assert client.contract_digest == first["contract_digest"]

    with pytest.raises(LiliesPlatformProtocolError, match="rolled back"):
        await client.contract_get(tool_call_id="tool-contract-version-rollback")
    with pytest.raises(LiliesPlatformProtocolError, match="schema changed without"):
        await client.contract_get(tool_call_id="tool-contract-same-version-drift")

    assert client.highest_contract_version == 2
    assert client.contract_schema_digest == first["contract_schema_digest"]
    assert client.contract_digest == first["contract_digest"]


@pytest.mark.asyncio
async def test_explicit_contract_refresh_allows_dynamic_digest_at_the_same_schema_version() -> None:
    first = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        contract_version=1,
    )
    refreshed = copy.deepcopy(first)
    refreshed["tool_catalog_digest"] = "sha256:" + "d" * 64
    _redigest_contract(refreshed)
    responses = iter((first, refreshed))

    def handler(request: httpx.Request) -> httpx.Response:
        contract = next(responses)
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                "platform_contract_get",
                digest=contract["contract_digest"],
                data=contract,
            ),
        )

    client = _client(httpx.MockTransport(handler), digest=None)
    await client.contract_get(tool_call_id="tool-contract-dynamic-one")
    await client.contract_get(tool_call_id="tool-contract-dynamic-two")

    assert client.highest_contract_version == 1
    assert client.contract_schema_digest == first["contract_schema_digest"]
    assert client.contract_digest == refreshed["contract_digest"]


@pytest.mark.asyncio
async def test_explicit_contract_refresh_accepts_changed_digest_at_a_new_version() -> None:
    first = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=list(PlatformScope),
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        contract_version=1,
    )
    refreshed = copy.deepcopy(first)
    refreshed["contract_version"] = 2
    refreshed["known_boundaries"].append(
        {"code": "versioned_change", "description": "A declared versioned capability change."}
    )
    _redigest_contract(refreshed)
    responses = iter((first, refreshed))

    def handler(request: httpx.Request) -> httpx.Response:
        contract = next(responses)
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                "platform_contract_get",
                digest=contract["contract_digest"],
                data=contract,
            ),
        )

    client = _client(httpx.MockTransport(handler), digest=None)
    await client.contract_get(tool_call_id="tool-contract-version-one")
    await client.contract_get(tool_call_id="tool-contract-version-two")

    assert client.highest_contract_version == 2
    assert client.contract_digest == refreshed["contract_digest"]


@pytest.mark.asyncio
async def test_assignment_client_requires_fetch_and_enforces_scoped_contract_operations() -> None:
    contract = build_platform_contract(
        _Blocks(),
        _Tools(),
        scopes=[PlatformScope.catalog_read, PlatformScope.run_execute],
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    digest = contract["contract_digest"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        operation = (
            "platform_contract_get"
            if request.url.path.endswith("/platform-contract")
            else "platform_run_get"
        )
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                operation,
                digest=digest,
                data=contract if operation == "platform_contract_get" else {"status": "queued"},
            ),
        )

    client = LiliesPlatformClient(
        base_url="https://platform.test",
        access_token="platform-secret-token-value",
        assignment_id=uuid4(),
        session_id=uuid4(),
        contract_digest=CONTRACT_DIGEST,
        require_contract_fetch=True,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LiliesPlatformProtocolError, match="must be fetched"):
        await client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-before-contract",
        )
    assert seen == []

    await client.contract_get(tool_call_id="tool-contract-scoped")
    assert client.contract_digest == digest
    assert {item["name"] for item in contract["operations"]} == {
        "platform_contract_get",
        "platform_block_search",
        "platform_block_get",
        "platform_tool_catalog",
        "platform_connector_authorization_issue",
        "platform_run_start",
        "platform_run_get",
        "platform_run_resume",
        "platform_run_cancel",
    }
    with pytest.raises(LiliesPlatformProtocolError, match="not present"):
        await client.invoke(
            "platform_publish",
            {"application_id": str(uuid4()), "idempotency_key": "publish-denied-0001"},
            tool_call_id="tool-publish-denied",
        )
    await client.invoke(
        "platform_run_get",
        {"run_id": str(uuid4())},
        tool_call_id="tool-run-scoped",
    )
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_old_digest_contract_drift_error_is_returned_without_protocol_masking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Lilies-Contract-Digest"] == CONTRACT_DIGEST
        return httpx.Response(
            409,
            request=request,
            json=_envelope(
                "platform_run_get",
                status_code=409,
                digest=NEW_CONTRACT_DIGEST,
                error=_contract_error(
                    expected=NEW_CONTRACT_DIGEST,
                    actual=CONTRACT_DIGEST,
                ),
            ),
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.invoke(
        "platform_run_get",
        {"run_id": str(uuid4())},
        tool_call_id="tool-call-drift-1",
    )

    assert not result.ok
    assert result.contract_digest == NEW_CONTRACT_DIGEST
    assert result.error is not None
    assert result.error.code == "contract_drift"
    assert result.error.expected == NEW_CONTRACT_DIGEST
    assert result.error.actual == CONTRACT_DIGEST
    assert client.contract_digest == CONTRACT_DIGEST


@pytest.mark.asyncio
async def test_successful_exact_replay_may_carry_its_original_contract_digest() -> None:
    def replay_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"X-Lilies-Idempotent-Replay": "true"},
            json=_envelope(
                "platform_run_get",
                digest=CONTRACT_DIGEST,
                data={"status": "succeeded"},
            ),
        )

    replay_client = _client(httpx.MockTransport(replay_handler), digest=NEW_CONTRACT_DIGEST)
    replay = await replay_client.invoke(
        "platform_run_get",
        {"run_id": str(uuid4())},
        tool_call_id="tool-call-replay-new-contract",
    )
    assert replay.ok
    assert replay.contract_digest == CONTRACT_DIGEST
    assert replay_client.contract_digest == NEW_CONTRACT_DIGEST

    strict_client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json=_envelope("platform_run_get", digest=CONTRACT_DIGEST),
            )
        ),
        digest=NEW_CONTRACT_DIGEST,
    )
    with pytest.raises(LiliesPlatformProtocolError, match="differs"):
        await strict_client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-call-unmarked-stale-success",
        )


@pytest.mark.asyncio
async def test_tool_context_and_mutation_idempotency_are_correlated_in_http_headers(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json=_envelope(
                "platform_application_create",
                data={"application_id": str(uuid4())},
            ),
        )

    client = _client(httpx.MockTransport(handler))
    tool = build_lilies_platform_registry(client, include_core_tools=False).get(
        "platform_application_create"
    )
    context = LiliesToolContext(
        session_id=str(client.session_id),
        workspace=tmp_path,
        turn_id="turn-17",
        tool_call_id="real-model-tool-call-17",
    )
    payload = {
        "name": "Assigned app",
        "idempotency_key": "same-mutation-key-0001",
    }

    first = await tool.execute(payload, context)
    second = await tool.execute(payload, context)

    assert not first.is_error
    assert not second.is_error
    assert len(requests) == 2
    for request in requests:
        assert request.headers["Authorization"] == "Bearer platform-secret-token-value"
        assert request.headers["X-Lilies-Assignment-ID"] == str(client.assignment_id)
        assert request.headers["X-Lilies-Session-ID"] == str(client.session_id)
        assert request.headers["X-Lilies-Tool-Call-ID"] == "real-model-tool-call-17"
        assert request.headers["X-Lilies-Idempotency-Key"] == "same-mutation-key-0001"
        assert request.headers["X-Lilies-Contract-Digest"] == CONTRACT_DIGEST
        assert json.loads(request.content)["idempotency_key"] == "same-mutation-key-0001"


@pytest.mark.asyncio
async def test_operation_and_path_are_contract_bounded_and_cannot_redirect_or_change_host() -> None:
    requests: list[httpx.Request] = []

    def artifact_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json=_envelope("platform_artifact_read", data={"encoding": "utf8"}),
        )

    client = _client(httpx.MockTransport(artifact_handler))
    with pytest.raises(KeyError, match="unknown public platform operation"):
        await client.invoke(
            "https://attacker.invalid/steal",
            {},
            tool_call_id="tool-call-url-1",
        )
    for operation, payload in (
        ("platform_block_get", {"block_type": ".."}),
        (
            "platform_artifact_read",
            {
                "run_id": str(uuid4()),
                "artifact_id": "../../../../internal/admin",
            },
        ),
    ):
        with pytest.raises(ValueError, match="must be a normalized path value"):
            await client.invoke(
                operation,
                payload,
                tool_call_id="tool-call-traversal-1",
            )
    assert requests == []

    artifact_id = str(uuid4())
    await client.invoke(
        "platform_artifact_read",
        {
            "run_id": str(uuid4()),
            "artifact_id": artifact_id,
            "offset_bytes": 8,
            "max_bytes": 20,
        },
        tool_call_id="tool-call-url-2",
    )
    assert len(requests) == 1
    assert requests[0].url.host == "platform.test"
    assert requests[0].url.scheme == "https"
    assert requests[0].url.path.endswith(f"/artifacts/{artifact_id}")
    assert requests[0].url.params["offset_bytes"] == "8"
    assert requests[0].url.params["max_bytes"] == "20"

    redirect_count = 0

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        nonlocal redirect_count
        redirect_count += 1
        return httpx.Response(
            307,
            request=request,
            headers={"Location": "https://attacker.invalid/redirect"},
        )

    redirect_client = _client(httpx.MockTransport(redirect_handler))
    with pytest.raises(LiliesPlatformProtocolError, match="non-JSON HTTP 307"):
        await redirect_client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-call-redirect-1",
        )
    assert redirect_count == 1


@pytest.mark.asyncio
async def test_invalid_remote_errors_and_transport_failures_do_not_leak_tokens(
    tmp_path: Path,
) -> None:
    token = "super-secret-platform-token-never-return"

    def invalid_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            request=request,
            text=f"private upstream body includes {token}",
        )

    client = _client(httpx.MockTransport(invalid_handler), token=token)
    assert token not in repr(client)
    with pytest.raises(LiliesPlatformProtocolError) as captured:
        await client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-call-secret-1",
        )
    assert token not in str(captured.value)

    def failed_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"transport accidentally mentioned {token}",
            request=request,
        )

    failed_client = _client(httpx.MockTransport(failed_handler), token=token)
    tool = build_lilies_platform_registry(failed_client, include_core_tools=False).get(
        "platform_run_get"
    )
    result = await tool.execute(
        {"run_id": str(uuid4())},
        LiliesToolContext(
            session_id=str(failed_client.session_id),
            workspace=tmp_path,
            tool_call_id="tool-call-secret-2",
        ),
    )
    decoded = json.loads(result.content)
    assert result.is_error
    assert decoded["error"]["code"] == "platform_unavailable"
    assert decoded["error"]["message"] == "the public platform endpoint is unavailable"
    assert token not in result.content


@pytest.mark.asyncio
async def test_platform_adapter_classifies_local_validation_and_protocol_failures(
    tmp_path: Path,
) -> None:
    context = LiliesToolContext(
        session_id=str(uuid4()),
        workspace=tmp_path,
        tool_call_id="tool-call-classification-1",
    )
    unreachable = _client(
        httpx.MockTransport(
            lambda request: pytest.fail(f"invalid input reached HTTP: {request.url}")
        )
    )
    run_tool = build_lilies_platform_registry(unreachable, include_core_tools=False).get(
        "platform_run_get"
    )
    invalid = json.loads((await run_tool.execute({"run_id": "not-a-uuid"}, context)).content)
    assert invalid["status_code"] == 422
    assert invalid["error"] == {
        "code": "invalid_request",
        "message": "platform tool input did not match the public request schema",
        "retryable": False,
        "failure_owner": "task_author",
        "expected": "the published operation request schema",
        "actual": "invalid tool input",
        "evidence_ref": None,
    }

    artifact_tool = build_lilies_platform_registry(
        unreachable,
        include_core_tools=False,
    ).get("platform_artifact_read")
    invalid_artifact = json.loads(
        (
            await artifact_tool.execute(
                {
                    "run_id": str(uuid4()),
                    "artifact_id": "not-a-uuid",
                },
                context,
            )
        ).content
    )
    assert invalid_artifact["status_code"] == 422
    assert invalid_artifact["error"] == invalid["error"]

    not_loaded_client = LiliesPlatformClient(
        base_url="https://platform.test",
        access_token="platform-secret-token-value",
        assignment_id=uuid4(),
        session_id=uuid4(),
        contract_digest=CONTRACT_DIGEST,
        require_contract_fetch=True,
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"unfetched operation reached HTTP: {request.url}")
        ),
    )
    not_loaded_tool = build_lilies_platform_registry(
        not_loaded_client,
        include_core_tools=False,
    ).get("platform_run_get")
    not_loaded = json.loads(
        (
            await not_loaded_tool.execute(
                {"run_id": str(uuid4())},
                context,
            )
        ).content
    )
    assert not_loaded["status_code"] == 409
    assert not_loaded["error"]["code"] == "contract_not_loaded"
    assert not_loaded["error"]["failure_owner"] == "lilies"

    protocol_client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, text="not-json")
        )
    )
    protocol_tool = build_lilies_platform_registry(
        protocol_client,
        include_core_tools=False,
    ).get("platform_run_get")
    protocol = json.loads(
        (await protocol_tool.execute({"run_id": str(uuid4())}, context)).content
    )
    assert protocol["status_code"] == 502
    assert protocol["error"]["code"] == "protocol_error"
    assert protocol["error"]["failure_owner"] == "platform"


@pytest.mark.asyncio
async def test_client_rejects_non_http_base_urls_and_mismatched_envelopes() -> None:
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        LiliesPlatformClient(
            base_url="file:///tmp/platform.sock",
            access_token="secret-token",
            assignment_id=uuid4(),
            session_id=uuid4(),
        )

    client = LiliesPlatformClient(
        base_url="https://platform.test",
        access_token="secret-token",
        assignment_id=uuid4(),
        session_id=uuid4(),
        contract_digest=CONTRACT_DIGEST,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json=_envelope("platform_run_cancel"),
            )
        ),
    )
    with pytest.raises(LiliesPlatformProtocolError, match="operation mismatch"):
        await client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-call-envelope-1",
        )

    wrong_status_client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                202,
                request=request,
                json=_envelope("platform_run_get", status_code=200),
            )
        )
    )
    with pytest.raises(LiliesPlatformProtocolError, match="does not match HTTP status"):
        await wrong_status_client.invoke(
            "platform_run_get",
            {"run_id": str(uuid4())},
            tool_call_id="tool-call-envelope-2",
        )
