from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import utc_now
from .platform_harness import PlatformHarness
from .storage import Storage


ConnectorValueType = Literal["string", "number", "integer", "boolean", "object", "array"]
ConnectorOperationKind = Literal["read", "write", "compensate"]
ConnectorEnvironment = Literal["mock", "test", "live", "private"]
ConnectorParameterLocation = Literal["path", "query", "header", "cookie"]
ConnectorExecutionStatus = Literal[
    "executing",
    "dry_run",
    "succeeded",
    "failed",
    "compensated",
]
ConnectorFailureDisposition = Literal[
    "none",
    "retryable",
    "terminal",
    "ambiguous",
]
ConnectorRetrySafety = Literal[
    "none",
    "pre_dispatch",
    "read_only",
    "idempotency_key",
]
DEFAULT_CONNECTOR_RETRYABLE_STATUS_CODES = [408, 425, 429, 500, 502, 503, 504]
MAX_CONNECTOR_OPERATION_PARAMETERS = 1_000
MAX_CONNECTOR_SCHEMA_FIELDS = 1_000
MAX_CONNECTOR_MULTIPART_PARTS = 100
MAX_CONNECTOR_BLOB_BYTES = 20 * 1024 * 1024
MAX_CONNECTOR_MULTIPART_BYTES = 50 * 1024 * 1024
MAX_CONNECTOR_OPERATION_REQUEST_CONSTRAINTS = 100
MAX_CONNECTOR_REQUEST_CONSTRAINT_FIELDS = 100
MAX_CONNECTOR_REQUEST_CONSTRAINT_FIXED_VALUES = 100
MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_BYTES = 16 * 1024
MAX_CONNECTOR_POLICY_REQUEST_CONSTRAINT_BYTES = 64 * 1024
MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_DEPTH = 8
MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_NODES = 512
MAX_CONNECTOR_REQUEST_CONSTRAINT_STRING_BYTES = 4 * 1024


class ConnectorConflict(RuntimeError):
    pass


class ConnectorDenied(RuntimeError):
    pass


class ConnectorAdapterError(RuntimeError):
    """A structured adapter failure with an explicit replay safety decision."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        side_effect_state: Literal["none", "unknown"] = "unknown",
        adapter_called: bool = True,
        failure_disposition: ConnectorFailureDisposition | None = None,
        retry_safety: ConnectorRetrySafety = "none",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.side_effect_state = side_effect_state
        self.adapter_called = adapter_called
        self.failure_disposition: ConnectorFailureDisposition = (
            failure_disposition
            or ("retryable" if retryable else "ambiguous" if side_effect_state == "unknown" else "terminal")
        )
        self.retry_safety = retry_safety
        if self.retryable and self.retry_safety == "none":
            raise ValueError("retryable connector failure requires an explicit retry safety")
        if self.retryable and self.failure_disposition != "retryable":
            raise ValueError("retryable connector failure must use retryable disposition")
        if self.failure_disposition == "ambiguous" and self.retryable:
            raise ValueError("ambiguous connector failure cannot be retryable")

    @classmethod
    def from_execution(cls, execution: ConnectorExecution) -> ConnectorAdapterError:
        side_effect_state = (
            "unknown" if execution.side_effect_state == "unknown" else "none"
        )
        disposition = execution.failure_disposition
        if disposition == "none":
            disposition = (
                "retryable"
                if execution.retryable
                else "ambiguous"
                if side_effect_state == "unknown"
                else "terminal"
            )
        return cls(
            execution.error or "connector execution previously failed",
            retryable=execution.retryable,
            side_effect_state=side_effect_state,
            adapter_called=execution.adapter_called,
            failure_disposition=disposition,
            retry_safety=execution.retry_safety,
        )


class ConnectorSchemaField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_type: ConnectorValueType
    required: bool = True
    item_type: ConnectorValueType | None = None
    enum: list[Any] = Field(default_factory=list, max_length=100)
    max_length: int | None = Field(default=None, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def array_item_contract(self) -> ConnectorSchemaField:
        if self.value_type == "array" and self.item_type is None:
            raise ValueError("array connector fields require item_type")
        if self.value_type != "array" and self.item_type is not None:
            raise ValueError("item_type is only valid for array connector fields")
        return self


class ConnectorObjectSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    version: int = Field(default=1, ge=1)
    fields: list[ConnectorSchemaField] = Field(
        default_factory=list,
        max_length=MAX_CONNECTOR_SCHEMA_FIELDS,
    )
    additional_properties: bool = False
    json_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def unique_fields(self) -> ConnectorObjectSchema:
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("connector schema contains duplicate fields")
        return self

    def validate_payload(self, payload: Any, *, label: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"{label} must be an object")
        if self.json_schema:
            self._validate_json_schema(payload, self.json_schema, label=label)
            return dict(payload)
        declared = {item.name: item for item in self.fields}
        missing = [item.name for item in self.fields if item.required and item.name not in payload]
        if missing:
            raise ValueError(f"{label} is missing required fields: {missing}")
        unknown = sorted(set(payload) - set(declared))
        if unknown and not self.additional_properties:
            raise ValueError(f"{label} contains undeclared fields: {unknown}")
        for name, value in payload.items():
            field = declared.get(name)
            if field is None:
                continue
            self._validate_value(value, field, label=f"{label}.{name}")
        return dict(payload)

    @classmethod
    def _validate_json_schema(
        cls,
        value: Any,
        schema: dict[str, Any],
        *,
        label: str,
    ) -> None:
        if not isinstance(schema, dict):
            raise ValueError(f"{label} has an invalid JSON Schema declaration")
        raw_type = schema.get("type")
        nullable = bool(schema.get("nullable")) or (
            isinstance(raw_type, list) and "null" in raw_type
        ) or raw_type == "null"
        if value is None:
            inferred_type = (
                "object"
                if "properties" in schema
                else "array"
                if "items" in schema
                else None
            )
            if not nullable and (raw_type is not None or inferred_type is not None):
                raise ValueError(f"{label} must not be null")
            if nullable:
                return
        else:
            expected_types = (
                [item for item in raw_type if item != "null"]
                if isinstance(raw_type, list)
                else [raw_type]
                if raw_type is not None
                else ["object"]
                if "properties" in schema
                else ["array"]
                if "items" in schema
                else []
            )
            if expected_types and not any(
                cls._matches_json_type(value, expected) for expected in expected_types
            ):
                expected = " or ".join(str(item) for item in expected_types)
                raise ValueError(f"{label} must be {expected}")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{label} must be one of {schema['enum']}")
        if "const" in schema and value != schema["const"]:
            raise ValueError(f"{label} must equal the declared constant")
        if isinstance(value, str):
            min_length = schema.get("minLength")
            max_length = schema.get("maxLength")
            if isinstance(min_length, int) and len(value) < min_length:
                raise ValueError(f"{label} is shorter than minLength {min_length}")
            if isinstance(max_length, int) and len(value) > max_length:
                raise ValueError(f"{label} exceeds maxLength {max_length}")
        pattern = schema.get("pattern")
        if isinstance(value, str) and isinstance(pattern, str):
            try:
                matched = re.search(pattern, value)
            except re.error as error:
                raise ValueError(f"{label} has an invalid pattern declaration") from error
            if matched is None:
                raise ValueError(f"{label} does not match the declared pattern")
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            missing = sorted(required - set(value))
            if missing:
                raise ValueError(f"{label} is missing required fields: {missing}")
            if schema.get("additionalProperties") is False:
                unknown = sorted(set(value) - set(properties))
                if unknown:
                    raise ValueError(f"{label} contains undeclared fields: {unknown}")
            for key, item in value.items():
                item_schema = properties.get(key)
                if isinstance(item_schema, dict):
                    cls._validate_json_schema(item, item_schema, label=f"{label}.{key}")
        if isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    cls._validate_json_schema(item, item_schema, label=f"{label}[{index}]")
        all_of = schema.get("allOf")
        if all_of is not None:
            if not isinstance(all_of, list) or not all_of or not all(
                isinstance(candidate, dict) for candidate in all_of
            ):
                raise ValueError(f"{label} has an invalid allOf declaration")
            for candidate in all_of:
                cls._validate_json_schema(value, candidate, label=label)
        any_of = schema.get("anyOf")
        if any_of is not None:
            matches = cls._matching_json_schema_branches(
                value,
                any_of,
                label=label,
                keyword="anyOf",
            )
            if matches < 1:
                raise ValueError(f"{label} must match at least one declared schema branch")
        one_of = schema.get("oneOf")
        if one_of is not None:
            matches = cls._matching_json_schema_branches(
                value,
                one_of,
                label=label,
                keyword="oneOf",
            )
            if matches != 1:
                raise ValueError(f"{label} must match exactly one declared schema branch")

    @staticmethod
    def _matches_json_type(value: Any, expected: Any) -> bool:
        return {
            "null": value is None,
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }.get(expected, True)

    @classmethod
    def _matching_json_schema_branches(
        cls,
        value: Any,
        candidates: Any,
        *,
        label: str,
        keyword: str,
    ) -> int:
        if not isinstance(candidates, list) or not candidates or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise ValueError(f"{label} has an invalid {keyword} declaration")
        matches = 0
        for candidate in candidates:
            try:
                cls._validate_json_schema(value, candidate, label=label)
            except ValueError:
                continue
            matches += 1
        return matches

    @classmethod
    def _validate_value(
        cls,
        value: Any,
        field: ConnectorSchemaField,
        *,
        label: str,
    ) -> None:
        expected = field.value_type
        valid = {
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
        }[expected]
        if not valid:
            raise ValueError(f"{label} must be {expected}")
        if field.enum and value not in field.enum:
            raise ValueError(f"{label} must be one of {field.enum}")
        if field.max_length is not None and isinstance(value, (str, list, dict)):
            if len(value) > field.max_length:
                raise ValueError(f"{label} exceeds max_length {field.max_length}")
        if expected == "array" and field.item_type is not None:
            item_field = ConnectorSchemaField(
                name="item",
                value_type=field.item_type,
                required=True,
            )
            for index, item in enumerate(value):
                cls._validate_value(item, item_field, label=f"{label}[{index}]")


class ConnectorParameterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    wire_name: str = Field(min_length=1, max_length=300)
    location: ConnectorParameterLocation
    required: bool = False
    style: str = "form"
    explode: bool = True


class ConnectorMultipartPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_key: str = Field(min_length=1, max_length=300)
    wire_name: str = Field(min_length=1, max_length=300)
    kind: Literal["text", "blob"]
    required: bool = False
    content_types: list[str] = Field(default_factory=list, max_length=20)
    max_bytes: int = Field(default=MAX_CONNECTOR_BLOB_BYTES, ge=1, le=MAX_CONNECTOR_BLOB_BYTES)

    @model_validator(mode="after")
    def safe_wire_contract(self) -> ConnectorMultipartPart:
        if any(
            character in value
            for value in (self.input_key, self.wire_name)
            for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("multipart part names contain an unsafe character")
        for content_type in self.content_types:
            if re.fullmatch(
                r"[A-Za-z0-9!#$&^_.+-]+/(?:[A-Za-z0-9!#$&^_.+-]+|\*)",
                content_type,
            ) is None:
                raise ValueError("multipart part content_type is invalid")
            if self.kind == "text" and content_type.endswith("/*"):
                raise ValueError("multipart text parts require a concrete content_type")
        return self


class ConnectorRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_key: str = Field(default="body", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    required: bool = False
    content_type: str = Field(default="application/json", min_length=1, max_length=200)
    multipart_parts: list[ConnectorMultipartPart] = Field(
        default_factory=list,
        max_length=MAX_CONNECTOR_MULTIPART_PARTS,
    )
    max_total_bytes: int = Field(
        default=MAX_CONNECTOR_MULTIPART_BYTES,
        ge=1,
        le=MAX_CONNECTOR_MULTIPART_BYTES,
    )

    @model_validator(mode="after")
    def multipart_contract_is_consistent(self) -> ConnectorRequestBody:
        if self.multipart_parts and self.content_type != "multipart/form-data":
            raise ValueError("multipart parts require multipart/form-data content_type")
        if self.content_type == "multipart/form-data" and not self.multipart_parts:
            raise ValueError("multipart/form-data requires declared multipart parts")
        input_keys = [item.input_key for item in self.multipart_parts]
        wire_names = [item.wire_name for item in self.multipart_parts]
        if len(input_keys) != len(set(input_keys)) or len(wire_names) != len(set(wire_names)):
            raise ValueError("multipart request contains duplicate part bindings")
        return self


class ConnectorSecurityScheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    type: Literal["http", "apiKey"]
    scheme: str = ""
    location: Literal["header", "query", "cookie"] = "header"
    wire_name: str = "Authorization"


class ConnectorOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    title: str = Field(min_length=1, max_length=200)
    kind: ConnectorOperationKind
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=500)
    request_schema: ConnectorObjectSchema
    response_schema: ConnectorObjectSchema
    parameters: list[ConnectorParameterBinding] = Field(
        default_factory=list,
        max_length=MAX_CONNECTOR_OPERATION_PARAMETERS,
    )
    request_body: ConnectorRequestBody | None = None
    response_json_schema: dict[str, Any] | None = None
    response_root_type: ConnectorValueType = "object"
    success_status_codes: list[int] = Field(default_factory=lambda: [200], max_length=20)
    response_content_types: list[str] = Field(
        default_factory=lambda: ["application/json"],
        max_length=20,
    )
    security_requirements: list[list[str]] = Field(default_factory=list, max_length=20)
    error_responses: dict[str, str] = Field(default_factory=dict)
    required_roles: list[str] = Field(default_factory=list, max_length=40)
    compensation_operation_id: str | None = None
    retryable_status_codes: list[int] = Field(
        default_factory=lambda: list(DEFAULT_CONNECTOR_RETRYABLE_STATUS_CODES),
        max_length=20,
    )
    idempotency_semantics: Literal["none", "request_key"] = "none"
    max_attempts: int = Field(default=3, ge=1, le=20)

    @property
    def mutating(self) -> bool:
        return self.kind in {"write", "compensate"}

    @model_validator(mode="after")
    def bounded_retry_contract(self) -> ConnectorOperation:
        if len(self.retryable_status_codes) != len(set(self.retryable_status_codes)):
            raise ValueError("connector retryable status codes must be unique")
        if any(
            status < 400 or status > 599
            for status in self.retryable_status_codes
        ):
            raise ValueError("connector retryable status codes must be HTTP errors")
        overlap = sorted(
            set(self.retryable_status_codes).intersection(self.success_status_codes)
        )
        if overlap:
            raise ValueError(
                f"connector retryable status codes overlap success statuses: {overlap}"
            )
        return self


class ConnectorDeploymentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    environment: ConnectorEnvironment
    base_url: str = Field(min_length=1, max_length=1000)
    auth_type: Literal["none", "bearer", "basic", "api_key"] = "none"
    auth_location: Literal["header", "query", "cookie"] = "header"
    auth_wire_name: str = "Authorization"
    auth_prefix: str = ""
    allowed_hosts: list[str] = Field(min_length=1, max_length=100)
    available: bool = False
    timeout_seconds: float = Field(default=20, ge=1, le=300)
    claim_ceiling: Literal["H2", "H3", "H4", "H5"] = "H2"
    excluded_claims: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def endpoint_matches_allowlist(self) -> ConnectorDeploymentProfile:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("connector deployment base_url must be HTTP(S)")
        host = parsed.hostname.casefold()
        allowed = {item.casefold().rstrip(".") for item in self.allowed_hosts}
        if not any(host == item or host.endswith(f".{item}") for item in allowed):
            raise ValueError("connector deployment base_url host is outside allowed_hosts")
        if self.environment in {"live", "private"} and self.claim_ceiling in {"H2", "H3"}:
            return self
        if self.environment in {"mock", "test"} and self.claim_ceiling in {"H4", "H5"}:
            raise ValueError("mock/test deployment cannot claim H4 or H5")
        return self


class ConnectorPreDispatchAttestation(BaseModel):
    """Operator-owned contract for a trusted no-side-effect response."""

    model_config = ConfigDict(extra="forbid")

    header_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$",
    )
    header_value: str = Field(min_length=1, max_length=200)


class ConnectorManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    connector_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    domain: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    operations: list[ConnectorOperation] = Field(min_length=1, max_length=1000)
    deployment_profiles: list[ConnectorDeploymentProfile] = Field(
        min_length=1,
        max_length=20,
    )
    security_schemes: list[ConnectorSecurityScheme] = Field(default_factory=list, max_length=40)
    source_provenance: dict[str, Any] = Field(default_factory=dict)
    callback_schema: ConnectorObjectSchema | None = None
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def linked_contracts_exist(self) -> ConnectorManifest:
        operation_ids = [item.id for item in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("connector manifest contains duplicate operation ids")
        profile_ids = [item.id for item in self.deployment_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("connector manifest contains duplicate deployment profile ids")
        operations = {item.id: item for item in self.operations}
        for operation in self.operations:
            if operation.compensation_operation_id:
                compensation = operations.get(operation.compensation_operation_id)
                if compensation is None or compensation.kind != "compensate":
                    raise ValueError(
                        f"{operation.id} references an invalid compensation operation"
                    )
        return self

    def operation(self, operation_id: str) -> ConnectorOperation:
        operation = next((item for item in self.operations if item.id == operation_id), None)
        if operation is None:
            raise KeyError(f"unknown connector operation: {operation_id}")
        return operation

    def profile(self, profile_id: str) -> ConnectorDeploymentProfile:
        profile = next(
            (item for item in self.deployment_profiles if item.id == profile_id),
            None,
        )
        if profile is None:
            raise KeyError(f"unknown connector deployment profile: {profile_id}")
        return profile

    def contract_document(self) -> dict[str, Any]:
        paths: dict[str, dict[str, Any]] = {}
        for operation in self.operations:
            paths.setdefault(operation.path, {})[operation.method.casefold()] = {
                "operationId": operation.id,
                "x-kind": operation.kind,
                "x-required-roles": operation.required_roles,
                "x-compensation-operation": operation.compensation_operation_id,
                "x-idempotency-semantics": operation.idempotency_semantics,
                "x-retryable-status-codes": operation.retryable_status_codes,
                "x-max-attempts": operation.max_attempts,
                "requestSchema": operation.request_schema.model_dump(mode="json"),
                "responseSchema": operation.response_schema.model_dump(mode="json"),
                "parameters": [item.model_dump(mode="json") for item in operation.parameters],
                "requestBody": (
                    operation.request_body.model_dump(mode="json")
                    if operation.request_body
                    else None
                ),
                "successStatusCodes": operation.success_status_codes,
                "securityRequirements": operation.security_requirements,
            }
        return {
            "openapi": "3.1.0",
            "info": {"title": self.title, "version": str(self.version)},
            "x-lilies-connector": {
                "connector_id": self.connector_id,
                "schema_version": self.schema_version,
                "domain": self.domain,
                "deployment_profiles": [
                    {
                        "id": item.id,
                        "environment": item.environment,
                        "available": item.available,
                        "claim_ceiling": item.claim_ceiling,
                    }
                    for item in self.deployment_profiles
                ],
                "source_provenance": self.source_provenance,
            },
            "paths": paths,
            "callbackSchema": (
                self.callback_schema.model_dump(mode="json")
                if self.callback_schema
                else None
            ),
        }


class ConnectorIdentitySubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_subject: str = Field(min_length=1, max_length=300)
    actor_id: str = Field(min_length=1, max_length=300)
    roles: list[str] = Field(min_length=1, max_length=40)


class ConnectorTenantBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    connector_version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=200)
    external_tenant_id: str = Field(min_length=1, max_length=300)
    profile_id: str
    secret_ref: str = Field(pattern=r"^secret://[^/]+/.+$")
    application_ids: list[str] = Field(min_length=1, max_length=100)
    allowed_operations: list[str] = Field(min_length=1, max_length=1000)
    subjects: list[ConnectorIdentitySubject] = Field(min_length=1, max_length=500)
    enabled: bool = True
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def unique_subjects(self) -> ConnectorTenantBinding:
        subjects = [item.external_subject for item in self.subjects]
        if len(subjects) != len(set(subjects)):
            raise ValueError("tenant binding contains duplicate external subjects")
        return self


def _bounded_constraint_json_nodes(value: Any, *, depth: int = 0) -> int:
    if depth > MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_DEPTH:
        raise ValueError("connector request constraint JSON exceeds the depth limit")
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, int):
        if value.bit_length() > 4096:
            raise ValueError("connector request constraint integer is too large")
        return 1
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("connector request constraint JSON must contain finite numbers")
        return 1
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_CONNECTOR_REQUEST_CONSTRAINT_STRING_BYTES:
            raise ValueError("connector request constraint string exceeds the byte limit")
        return 1
    if isinstance(value, list):
        if len(value) > MAX_CONNECTOR_REQUEST_CONSTRAINT_FIELDS:
            raise ValueError("connector request constraint array exceeds the item limit")
        return 1 + sum(
            _bounded_constraint_json_nodes(item, depth=depth + 1)
            for item in value
        )
    if isinstance(value, dict):
        if len(value) > MAX_CONNECTOR_REQUEST_CONSTRAINT_FIELDS:
            raise ValueError("connector request constraint object exceeds the field limit")
        nodes = 1
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(
                    "connector request constraint JSON object keys must be non-empty strings"
                )
            if (
                len(key.encode("utf-8"))
                > MAX_CONNECTOR_REQUEST_CONSTRAINT_STRING_BYTES
                or any(ord(character) < 0x20 for character in key)
            ):
                raise ValueError("connector request constraint JSON object key is unsafe")
            nodes += _bounded_constraint_json_nodes(item, depth=depth + 1)
        return nodes
    raise ValueError("connector request constraint values must be JSON-compatible")


def _validate_constraint_field_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("connector request constraint field names must be non-empty strings")
    if (
        len(value) > 300
        or len(value.encode("utf-8")) > 1_000
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("connector request constraint field name is unsafe or too long")
    return value


class ConnectorOperationRequestConstraint(BaseModel):
    """Bounded per-operation request authority, not an arbitrary schema language.

    Path and query maps use ``ConnectorParameterBinding.input_key`` names. Body
    maps address only the first-level JSON object below ``request_body.input_key``
    (or the implicit body for legacy operations without an explicit body binding).
    """

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    allowed_body_fields: list[str] | None = Field(
        default=None,
        max_length=MAX_CONNECTOR_REQUEST_CONSTRAINT_FIELDS,
    )
    fixed_path_values: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_CONNECTOR_REQUEST_CONSTRAINT_FIXED_VALUES,
    )
    fixed_query_values: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_CONNECTOR_REQUEST_CONSTRAINT_FIXED_VALUES,
    )
    fixed_body_values: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_CONNECTOR_REQUEST_CONSTRAINT_FIXED_VALUES,
    )

    @model_validator(mode="after")
    def bounded_request_authority(self) -> ConnectorOperationRequestConstraint:
        if (
            self.allowed_body_fields is None
            and not self.fixed_path_values
            and not self.fixed_query_values
            and not self.fixed_body_values
        ):
            raise ValueError(
                "connector request constraint must narrow at least one request dimension"
            )
        if self.allowed_body_fields is not None:
            names = [
                _validate_constraint_field_name(item)
                for item in self.allowed_body_fields
            ]
            if len(names) != len(set(names)):
                raise ValueError(
                    "connector request constraint contains duplicate allowed body fields"
                )
        for values in (
            self.fixed_path_values,
            self.fixed_query_values,
            self.fixed_body_values,
        ):
            for key in values:
                _validate_constraint_field_name(key)
        fixed_count = sum(
            len(values)
            for values in (
                self.fixed_path_values,
                self.fixed_query_values,
                self.fixed_body_values,
            )
        )
        if fixed_count > MAX_CONNECTOR_REQUEST_CONSTRAINT_FIXED_VALUES:
            raise ValueError(
                "connector request constraint exceeds the total fixed-value limit"
            )
        for value in self.fixed_path_values.values():
            if value is None or isinstance(value, (list, dict)):
                raise ValueError(
                    "connector fixed path values must be non-null JSON scalars"
                )
        for value in self.fixed_query_values.values():
            if value is None or isinstance(value, dict) or (
                isinstance(value, list)
                and any(item is None or isinstance(item, (list, dict)) for item in value)
            ):
                raise ValueError(
                    "connector fixed query values must be JSON scalars or scalar arrays"
                )
        fixed_document = {
            "fixed_path_values": self.fixed_path_values,
            "fixed_query_values": self.fixed_query_values,
            "fixed_body_values": self.fixed_body_values,
        }
        nodes = _bounded_constraint_json_nodes(fixed_document)
        if nodes > MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_NODES:
            raise ValueError("connector request constraint JSON exceeds the node limit")
        try:
            encoded = json.dumps(
                fixed_document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError(
                "connector request constraint values must be finite canonical JSON"
            ) from error
        if len(encoded) > MAX_CONNECTOR_REQUEST_CONSTRAINT_JSON_BYTES:
            raise ValueError("connector request constraint JSON exceeds the byte limit")
        if (
            self.allowed_body_fields is not None
            and not set(self.fixed_body_values).issubset(self.allowed_body_fields)
        ):
            raise ValueError(
                "connector fixed body fields must be included in allowed_body_fields"
            )
        return self


class ConnectorDomainPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    connector_version: int = Field(ge=1)
    tenant_id: str
    domain: str
    allowed_profiles: list[str] = Field(min_length=1, max_length=20)
    allowed_operations: list[str] = Field(min_length=1, max_length=1000)
    required_roles: list[str] = Field(default_factory=list, max_length=40)
    max_payload_bytes: int = Field(default=100_000, ge=1, le=10_000_000)
    operation_request_constraints: list[ConnectorOperationRequestConstraint] = Field(
        default_factory=list,
        max_length=MAX_CONNECTOR_OPERATION_REQUEST_CONSTRAINTS,
    )
    mutation_preauthorization_required: bool = True
    allow_dry_run: bool = True
    allow_compensation_during_stop: bool = True
    emergency_stop: bool = False
    emergency_reason: str = Field(default="", max_length=1000)
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def bounded_operation_request_constraints(self) -> ConnectorDomainPolicy:
        operation_ids = [
            constraint.operation_id
            for constraint in self.operation_request_constraints
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError(
                "connector policy contains duplicate operation request constraints"
            )
        unknown = sorted(set(operation_ids) - set(self.allowed_operations))
        if unknown:
            raise ValueError(
                "connector policy request constraints reference disallowed operations: "
                f"{unknown}"
            )
        encoded = json.dumps(
            [
                constraint.model_dump(mode="json")
                for constraint in self.operation_request_constraints
            ],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_CONNECTOR_POLICY_REQUEST_CONSTRAINT_BYTES:
            raise ValueError(
                "connector policy request constraints exceed the aggregate byte limit"
            )
        return self

    def request_constraint(
        self,
        operation_id: str,
    ) -> ConnectorOperationRequestConstraint | None:
        return next(
            (
                constraint
                for constraint in self.operation_request_constraints
                if constraint.operation_id == operation_id
            ),
            None,
        )


def connector_object_json_schema(value: ConnectorObjectSchema) -> dict[str, Any]:
    if value.json_schema is not None:
        return json.loads(json.dumps(value.json_schema))
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in value.fields:
        field_schema: dict[str, Any] = {"type": field.value_type}
        if field.enum:
            field_schema["enum"] = list(field.enum)
        if field.item_type is not None:
            field_schema["items"] = {"type": field.item_type}
        if field.max_length is not None:
            if field.value_type == "string":
                field_schema["maxLength"] = field.max_length
            elif field.value_type == "array":
                field_schema["maxItems"] = field.max_length
            elif field.value_type == "object":
                field_schema["maxProperties"] = field.max_length
        properties[field.name] = field_schema
        if field.required:
            required.append(field.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": value.additional_properties,
    }


def _direct_object_schema(
    schema: dict[str, Any],
    *,
    label: str,
) -> tuple[dict[str, Any], list[str], bool | dict[str, Any]]:
    if not isinstance(schema, dict):
        raise ValueError(f"{label} must be a JSON object schema")
    if any(keyword in schema for keyword in ("allOf", "anyOf", "oneOf", "$ref")):
        raise ValueError(
            f"{label} must use a direct object schema for request constraints"
        )
    schema_type = schema.get("type")
    if schema_type not in {None, "object"}:
        raise ValueError(f"{label} must describe an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional = schema.get("additionalProperties", True)
    if not isinstance(properties, dict) or not all(
        isinstance(key, str) and isinstance(item, dict)
        for key, item in properties.items()
    ):
        raise ValueError(f"{label} has invalid direct properties")
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise ValueError(f"{label} has an invalid required field list")
    if not isinstance(additional, (bool, dict)):
        raise ValueError(f"{label} has an invalid additionalProperties declaration")
    return properties, list(required), additional


def _schema_for_allowed_property(
    properties: dict[str, Any],
    additional: bool | dict[str, Any],
    key: str,
    *,
    label: str,
) -> dict[str, Any]:
    existing = properties.get(key)
    if isinstance(existing, dict):
        return json.loads(json.dumps(existing))
    if additional is False:
        raise ValueError(f"{label} references undeclared field {key!r}")
    if isinstance(additional, dict):
        return json.loads(json.dumps(additional))
    return {}


def _set_schema_constant(
    properties: dict[str, Any],
    additional: bool | dict[str, Any],
    key: str,
    value: Any,
    *,
    label: str,
) -> None:
    property_schema = _schema_for_allowed_property(
        properties,
        additional,
        key,
        label=label,
    )
    ConnectorObjectSchema._validate_json_schema(
        value,
        property_schema,
        label=f"{label}.{key}",
    )
    property_schema["const"] = json.loads(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    )
    properties[key] = property_schema


def _operation_parameter_inputs(
    operation: ConnectorOperation,
    *,
    location: Literal["path", "query"],
    outer_properties: dict[str, Any],
) -> set[str]:
    if operation.parameters:
        return {
            parameter.input_key
            for parameter in operation.parameters
            if parameter.location == location
        }
    path_inputs = set(re.findall(r"\{([^{}]+)\}", operation.path))
    if location == "path":
        return path_inputs
    if operation.request_body is None and operation.method == "GET":
        return set(outer_properties) - path_inputs
    return set()


def project_connector_operation_request_schema(
    operation: ConnectorOperation,
    constraint: ConnectorOperationRequestConstraint | None,
) -> dict[str, Any]:
    """Return the public request schema after applying bounded policy authority.

    The same constraint is separately enforced by ``ConnectorService.execute``;
    this projection is descriptive and cannot grant authority.
    """

    schema = connector_object_json_schema(operation.request_schema)
    if constraint is None:
        return schema
    if constraint.operation_id != operation.id:
        raise ValueError("connector request constraint operation does not match")
    outer_properties, outer_required, outer_additional = _direct_object_schema(
        schema,
        label=f"{operation.id} request schema",
    )
    outer_properties = {
        key: json.loads(json.dumps(item))
        for key, item in outer_properties.items()
    }
    outer_required_set = set(outer_required)
    path_inputs = _operation_parameter_inputs(
        operation,
        location="path",
        outer_properties=outer_properties,
    )
    query_inputs = _operation_parameter_inputs(
        operation,
        location="query",
        outer_properties=outer_properties,
    )
    unknown_path = sorted(set(constraint.fixed_path_values) - path_inputs)
    unknown_query = sorted(set(constraint.fixed_query_values) - query_inputs)
    if unknown_path or unknown_query:
        raise ValueError(
            "connector request constraint references unknown parameter inputs: "
            f"path={unknown_path}, query={unknown_query}"
        )

    explicit_body = operation.request_body is not None
    implicit_body = (
        operation.request_body is None
        and not operation.parameters
        and operation.method != "GET"
    )
    body_constraint_present = (
        constraint.allowed_body_fields is not None
        or bool(constraint.fixed_body_values)
    )
    if body_constraint_present and not (explicit_body or implicit_body):
        raise ValueError(
            "connector request constraint declares body authority for an operation "
            "without a request body"
        )

    if explicit_body and body_constraint_present:
        body_key = operation.request_body.input_key
        body_schema = _schema_for_allowed_property(
            outer_properties,
            outer_additional,
            body_key,
            label=f"{operation.id} request body",
        )
        body_properties, body_required, body_additional = _direct_object_schema(
            body_schema,
            label=f"{operation.id} request body",
        )
        body_properties = {
            key: json.loads(json.dumps(item))
            for key, item in body_properties.items()
        }
        body_required_set = set(body_required)
        if operation.request_body.multipart_parts:
            part_names = {
                part.input_key for part in operation.request_body.multipart_parts
            }
            unknown_parts = sorted(
                set(body_properties).difference(part_names)
            )
            if unknown_parts:
                raise ValueError(
                    "connector multipart body schema contains undeclared parts: "
                    f"{unknown_parts}"
                )
            for part_name in part_names:
                body_properties.setdefault(part_name, {})
            body_additional = False
        if constraint.allowed_body_fields is not None:
            allowed_body = set(constraint.allowed_body_fields)
            missing_required = sorted(body_required_set - allowed_body)
            if missing_required:
                raise ValueError(
                    "connector allowed_body_fields excludes required request body "
                    f"fields: {missing_required}"
                )
            narrowed: dict[str, Any] = {}
            for key in constraint.allowed_body_fields:
                narrowed[key] = _schema_for_allowed_property(
                    body_properties,
                    body_additional,
                    key,
                    label=f"{operation.id} allowed request body",
                )
            body_properties = narrowed
            body_additional = False
        for key, value in constraint.fixed_body_values.items():
            _set_schema_constant(
                body_properties,
                body_additional,
                key,
                value,
                label=f"{operation.id} fixed request body",
            )
            body_required_set.add(key)
        body_schema = {
            **body_schema,
            "type": "object",
            "properties": body_properties,
            "required": sorted(body_required_set),
            "additionalProperties": body_additional,
        }
        outer_properties[body_key] = body_schema
        if constraint.fixed_body_values:
            outer_required_set.add(body_key)
    elif implicit_body and body_constraint_present:
        body_fields = set(outer_properties) - path_inputs
        body_required_set = outer_required_set - path_inputs
        if constraint.allowed_body_fields is not None:
            allowed_body = set(constraint.allowed_body_fields)
            missing_required = sorted(body_required_set - allowed_body)
            if missing_required:
                raise ValueError(
                    "connector allowed_body_fields excludes required request body "
                    f"fields: {missing_required}"
                )
            narrowed = {
                key: _schema_for_allowed_property(
                    outer_properties,
                    outer_additional,
                    key,
                    label=f"{operation.id} request path",
                )
                for key in sorted(path_inputs)
            }
            for key in constraint.allowed_body_fields:
                narrowed[key] = _schema_for_allowed_property(
                    {
                        field: outer_properties[field]
                        for field in body_fields
                    },
                    outer_additional,
                    key,
                    label=f"{operation.id} allowed request body",
                )
            outer_properties = narrowed
            outer_additional = False
            outer_required_set = (
                outer_required_set.intersection(path_inputs)
                | body_required_set
            )
        for key, value in constraint.fixed_body_values.items():
            _set_schema_constant(
                outer_properties,
                outer_additional,
                key,
                value,
                label=f"{operation.id} fixed request body",
            )
            outer_required_set.add(key)

    for values, label in (
        (constraint.fixed_path_values, "fixed path"),
        (constraint.fixed_query_values, "fixed query"),
    ):
        for key, value in values.items():
            _set_schema_constant(
                outer_properties,
                outer_additional,
                key,
                value,
                label=f"{operation.id} {label}",
            )
            outer_required_set.add(key)
    return {
        **schema,
        "type": "object",
        "properties": outer_properties,
        "required": sorted(outer_required_set),
        "additionalProperties": outer_additional,
    }


def _same_constraint_json_value(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if (
        isinstance(actual, (int, float))
        and isinstance(expected, (int, float))
    ):
        return (
            not isinstance(actual, bool)
            and not isinstance(expected, bool)
            and (not isinstance(actual, float) or math.isfinite(actual))
            and (not isinstance(expected, float) or math.isfinite(expected))
            and actual == expected
        )
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, str) or isinstance(expected, str):
        return isinstance(actual, str) and isinstance(expected, str) and actual == expected
    if isinstance(actual, list) or isinstance(expected, list):
        return (
            isinstance(actual, list)
            and isinstance(expected, list)
            and len(actual) == len(expected)
            and all(
                _same_constraint_json_value(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected, strict=True)
            )
        )
    if isinstance(actual, dict) or isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and isinstance(expected, dict)
            and set(actual) == set(expected)
            and all(
                _same_constraint_json_value(actual[key], expected[key])
                for key in actual
            )
        )
    return False


def enforce_connector_operation_request_constraint(
    operation: ConnectorOperation,
    constraint: ConnectorOperationRequestConstraint | None,
    payload: dict[str, Any],
) -> None:
    if constraint is None:
        return
    if constraint.operation_id != operation.id:
        raise ConnectorDenied("connector request constraint operation does not match")
    for key, expected in constraint.fixed_path_values.items():
        if key not in payload:
            raise ConnectorDenied(f"connector fixed path input is missing: {key}")
        if not _same_constraint_json_value(payload[key], expected):
            raise ConnectorDenied(f"connector fixed path input drifted: {key}")
    for key, expected in constraint.fixed_query_values.items():
        if key not in payload:
            raise ConnectorDenied(f"connector fixed query input is missing: {key}")
        if not _same_constraint_json_value(payload[key], expected):
            raise ConnectorDenied(f"connector fixed query input drifted: {key}")

    if operation.request_body is not None:
        body = payload.get(operation.request_body.input_key)
    elif not operation.parameters and operation.method != "GET":
        path_inputs = set(re.findall(r"\{([^{}]+)\}", operation.path))
        body = {
            key: value
            for key, value in payload.items()
            if key not in path_inputs
        }
    else:
        body = None
    if constraint.allowed_body_fields is not None:
        if body is not None and not isinstance(body, dict):
            raise ConnectorDenied("connector constrained request body must be an object")
        unknown = sorted(
            set(body or {}).difference(constraint.allowed_body_fields)
        )
        if unknown:
            raise ConnectorDenied(
                f"connector request body contains policy-denied fields: {unknown}"
            )
    if constraint.fixed_body_values:
        if not isinstance(body, dict):
            raise ConnectorDenied("connector fixed request body is missing")
        for key, expected in constraint.fixed_body_values.items():
            if key not in body:
                raise ConnectorDenied(
                    f"connector fixed request body field is missing: {key}"
                )
            if not _same_constraint_json_value(body[key], expected):
                raise ConnectorDenied(
                    f"connector fixed request body field drifted: {key}"
                )


class ConnectorAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    connector_version: int
    tenant_id: str
    actor_id: str
    profile_id: str
    operation_id: str
    operation_kind: ConnectorOperationKind | None = None
    payload_hash: str
    policy_revision: int
    issuance_source: Literal["owner", "task_policy"] = "owner"
    descriptor_digest: str = ""
    task_credential_ref_digest: str = ""
    task_policy_digest: str = ""
    allowed_actions_digest: str = ""
    budget_digest: str = ""
    assignment_budget_policy_digest: str = ""
    assignment_max_write_count: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    assignment_max_payload_bytes: int | None = Field(
        default=None,
        ge=1,
        le=100 * 1024 * 1024,
    )
    assignment_write_count_at_issue: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    task_deadline_at: str = ""
    assignment_id: str = ""
    session_id: str = ""
    application_id: str = ""
    run_id: str = ""
    max_uses: int = Field(default=1, ge=1, le=100)
    use_count: int = 0
    expires_at: str
    revoked: bool = False
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def task_policy_binding_is_complete(self) -> ConnectorAuthorization:
        if self.issuance_source != "task_policy":
            return self
        digest_fields = (
            self.descriptor_digest,
            self.task_credential_ref_digest,
            self.task_policy_digest,
            self.allowed_actions_digest,
            self.budget_digest,
            self.assignment_budget_policy_digest,
        )
        if (
            self.operation_kind not in {"write", "compensate"}
            or not all(
                re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                for value in digest_fields
            )
            or not self.assignment_id
            or not self.session_id
            or not self.application_id
            or self.assignment_max_write_count is None
            or self.assignment_max_payload_bytes is None
            or self.assignment_write_count_at_issue is None
            or not self.task_deadline_at
            or self.max_uses != 1
        ):
            raise ValueError(
                "task-policy connector authorization binding is incomplete"
            )
        expires_at = datetime.fromisoformat(
            self.expires_at.replace("Z", "+00:00")
        )
        task_deadline_at = datetime.fromisoformat(
            self.task_deadline_at.replace("Z", "+00:00")
        )
        if (
            expires_at.tzinfo is None
            or task_deadline_at.tzinfo is None
            or expires_at > task_deadline_at
            or self.assignment_write_count_at_issue
            >= self.assignment_max_write_count
        ):
            raise ValueError(
                "task-policy connector authorization exceeds its budget or deadline"
            )
        return self

    def public_task_receipt(self) -> dict[str, Any]:
        if self.issuance_source != "task_policy":
            raise ValueError("owner connector authorization has no task receipt")
        unsigned = {
            "authorization_id": self.id,
            "issuance_source": self.issuance_source,
            "connector_id": self.connector_id,
            "connector_version": self.connector_version,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "profile_id": self.profile_id,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "payload_hash": f"sha256:{self.payload_hash}",
            "policy_revision": self.policy_revision,
            "descriptor_digest": self.descriptor_digest,
            "task_credential_ref_digest": self.task_credential_ref_digest,
            "task_policy_digest": self.task_policy_digest,
            "allowed_actions_digest": self.allowed_actions_digest,
            "budget_digest": self.budget_digest,
            "assignment_budget_policy_digest": (
                self.assignment_budget_policy_digest
            ),
            "assignment_id": self.assignment_id,
            "session_id": self.session_id,
            "application_id": self.application_id,
            "assignment_max_write_count": self.assignment_max_write_count,
            "assignment_max_payload_bytes": self.assignment_max_payload_bytes,
            "assignment_write_count_at_issue": (
                self.assignment_write_count_at_issue
            ),
            "max_uses": self.max_uses,
            "expires_at": self.expires_at,
            "task_deadline_at": self.task_deadline_at,
            "created_at": self.created_at,
        }
        return {
            **unsigned,
            "receipt_digest": (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        unsigned,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
            ),
        }


class ConnectorExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    connector_version: int
    tenant_id: str
    actor_id: str
    actor_roles: list[str]
    application_id: str = ""
    run_id: str = ""
    assignment_id: str = ""
    session_id: str = ""
    assigned_allowed_network_hosts: list[str] | None = None
    assigned_compensation_operations: list[str] | None = None
    assignment_max_write_count: int | None = None
    assignment_max_payload_bytes: int | None = None
    profile_id: str
    operation_id: str
    operation_kind: ConnectorOperationKind
    idempotency_key: str
    payload_hash: str
    request_payload: dict[str, Any]
    status: ConnectorExecutionStatus
    side_effect_state: Literal["none", "applied", "unknown", "compensated"] = "none"
    policy_revision: int
    binding_revision: int = Field(default=0, ge=0)
    authorization_id: str = ""
    adapter_called: bool = False
    response: Any = Field(default_factory=dict)
    response_hash: str = ""
    external_reference: str = ""
    compensation_payload: dict[str, Any] = Field(default_factory=dict)
    compensation_execution_id: str = ""
    callback_sequence: int = 0
    callback_status: str = ""
    error: str = ""
    replayed: bool = False
    attempt_count: int = Field(default=0, ge=0, le=1_000_000)
    retryable: bool = False
    failure_disposition: ConnectorFailureDisposition = "none"
    retry_safety: ConnectorRetrySafety = "none"
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None

    @model_validator(mode="after")
    def retry_state_is_consistent(self) -> ConnectorExecution:
        if self.retryable:
            if (
                self.status != "failed"
                or self.failure_disposition != "retryable"
                or self.retry_safety == "none"
            ):
                raise ValueError(
                    "retryable connector receipt lacks a safe failed execution state"
                )
        elif self.failure_disposition == "retryable":
            raise ValueError(
                "connector retryable disposition requires retryable=true"
            )
        if self.failure_disposition == "ambiguous":
            if self.status != "failed" or self.side_effect_state != "unknown":
                raise ValueError(
                    "ambiguous connector receipt must be a failed unknown side effect"
                )
        if self.status != "failed" and (
            self.retryable
            or self.failure_disposition != "none"
            or self.retry_safety != "none"
        ):
            raise ValueError(
                "non-failed connector receipt cannot retain failure retry state"
            )
        return self

    def public_receipt(self) -> dict[str, Any]:
        return {
            "execution_id": self.id,
            "connector_id": self.connector_id,
            "connector_version": self.connector_version,
            "tenant_id": self.tenant_id,
            "profile_id": self.profile_id,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "status": self.status,
            "side_effect_state": self.side_effect_state,
            "external_reference": self.external_reference,
            "compensation_available": bool(self.compensation_payload),
            "compensation_execution_id": self.compensation_execution_id,
            "callback_status": self.callback_status,
            "replayed": self.replayed,
            "attempt_count": self.attempt_count,
            "retryable": self.retryable,
            "failure_disposition": self.failure_disposition,
            "retry_safety": self.retry_safety,
            "binding_revision": self.binding_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "claim_scope": (
                "Tenant-scoped Connector receipt from the configured deployment profile; "
                "not customer-production or SLO evidence."
            ),
        }


class ConnectorCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: str
    execution_id: str
    sequence: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)
    received_at: str = Field(default_factory=utc_now)


class ConnectorExercise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    connector_version: int
    tenant_id: str
    kind: Literal["emergency_stop", "compensation"]
    profile_id: str
    status: Literal["passed", "failed", "blocked_by_environment"]
    evidence_level: Literal["H0", "H3"]
    evidence: dict[str, Any] = Field(default_factory=dict)
    excluded_claims: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)


class ConnectorEmbeddingEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    connector_version: int = Field(ge=1)
    application_id: str
    external_tenant_id: str
    external_subject: str
    issued_at: str
    expires_at: str
    nonce: str = Field(min_length=8, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    authorization_id: str = ""
    write_mode: Literal["dry_run", "execute"] = "dry_run"
    request: dict[str, Any]


class ConnectorIdentityContext(BaseModel):
    connector_id: str
    connector_version: int
    tenant_id: str
    actor_id: str
    actor_roles: list[str]
    profile_id: str
    application_id: str


class ConnectorExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    connector_version: int = Field(ge=1)
    tenant_id: str
    actor_id: str
    actor_roles: list[str]
    profile_id: str
    operation_id: str
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=300)
    authorization_id: str = ""
    dry_run: bool = False
    application_id: str = ""
    run_id: str = ""
    assignment_id: str = ""
    session_id: str = ""
    allowed_network_hosts: list[str] | None = None
    allowed_compensation_operations: list[str] | None = None
    permission_required: bool = False
    assignment_max_write_count: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    assignment_max_payload_bytes: int | None = Field(
        default=None,
        ge=1,
        le=100 * 1024 * 1024,
    )


class ConnectorAssignmentWriteReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: str
    connector_id: str
    connector_version: int
    tenant_id: str
    profile_id: str
    operation_id: str
    operation_kind: ConnectorOperationKind
    idempotency_key: str
    payload_hash: str
    status: ConnectorExecutionStatus
    side_effect_state: Literal["none", "applied", "unknown", "compensated"]
    authorization_ref_digest: str | None
    adapter_called: bool
    attempt_count: int = Field(default=0, ge=0, le=1_000_000)
    retryable: bool = False
    failure_disposition: ConnectorFailureDisposition = "none"
    retry_safety: ConnectorRetrySafety = "none"
    created_at: str
    updated_at: str


class ConnectorAssignmentBudgetReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: str
    policy_digest: str
    allowed_network_hosts: list[str]
    allowed_compensation_operations: list[str]
    max_write_count: int
    max_payload_bytes: int
    write_count: int
    writes: list[ConnectorAssignmentWriteReceipt]
    receipt_digest: str

    @model_validator(mode="after")
    def verify_frozen_receipt(self) -> ConnectorAssignmentBudgetReceipt:
        identities = [
            (
                item.connector_id,
                item.connector_version,
                item.tenant_id,
                item.operation_id,
                item.idempotency_key,
                item.execution_id,
            )
            for item in self.writes
        ]
        if identities != sorted(identities):
            raise ValueError(
                "connector assignment writes are not in stable execution identity order"
            )
        if len(identities) != len(set(identities)):
            raise ValueError("connector assignment receipt contains duplicate writes")
        execution_ids = [item.execution_id for item in self.writes]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError(
                "connector assignment receipt contains duplicate execution ids"
            )
        # A budget reservation and its execution row are inserted in the same
        # BEGIN IMMEDIATE transaction. Failed/unknown adapter outcomes still
        # consume a reservation because a side effect may have occurred.
        if self.write_count != len(self.writes):
            raise ValueError(
                "connector assignment write count does not match durable reservations"
            )
        policy_document = {
            "allowed_network_hosts": self.allowed_network_hosts,
            "allowed_compensation_operations": (
                self.allowed_compensation_operations
            ),
            "max_write_count": self.max_write_count,
            "max_payload_bytes": self.max_payload_bytes,
        }
        policy_digest = hashlib.sha256(
            json.dumps(
                policy_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(self.policy_digest, policy_digest):
            raise ValueError("connector assignment policy digest does not match receipt")
        unsigned = self.model_dump(mode="json", exclude={"receipt_digest"})
        expected_receipt_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        if not hmac.compare_digest(self.receipt_digest, expected_receipt_digest):
            raise ValueError("connector assignment receipt digest does not match content")
        return self


class ConnectorService:
    def __init__(
        self,
        *,
        storage: Storage,
        harness: PlatformHarness,
        pre_dispatch_attestations: dict[str, dict[str, str]] | None = None,
        environment_epoch: str = "default",
    ) -> None:
        self.storage = storage
        self.harness = harness
        self._environment_epoch_digest = (
            ""
            if environment_epoch == "default"
            else hashlib.sha256(environment_epoch.encode()).hexdigest()[:24]
        )
        self._lock = asyncio.Lock()
        self._manifests: dict[tuple[str, int], ConnectorManifest] = {}
        self._bindings: dict[tuple[str, int, str], ConnectorTenantBinding] = {}
        self._pre_dispatch_attestations = {
            key: ConnectorPreDispatchAttestation.model_validate(value)
            for key, value in (pre_dispatch_attestations or {}).items()
        }

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        await asyncio.to_thread(self._load_cache_sync)

    def _initialize_sync(self) -> None:
        with self.storage._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS connector_manifests (
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(connector_id,version)
                );
                CREATE TABLE IF NOT EXISTS connector_tenant_bindings (
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  external_tenant_id TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(connector_id,version,tenant_id),
                  UNIQUE(connector_id,version,external_tenant_id)
                );
                CREATE TABLE IF NOT EXISTS connector_domain_policies (
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(connector_id,version,tenant_id)
                );
                CREATE TABLE IF NOT EXISTS connector_authorizations (
                  id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_executions (
                  id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  operation_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  status TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(connector_id,version,tenant_id,operation_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_connector_executions_tenant_created
                  ON connector_executions(connector_id,version,tenant_id,created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS connector_audit_events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  execution_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_connector_events_execution
                  ON connector_audit_events(execution_id,sequence);
                CREATE TABLE IF NOT EXISTS connector_embedding_nonces (
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  external_tenant_id TEXT NOT NULL,
                  nonce TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(connector_id,version,external_tenant_id,nonce)
                );
                CREATE TABLE IF NOT EXISTS connector_callbacks (
                  callback_id TEXT PRIMARY KEY,
                  execution_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  record_json TEXT NOT NULL,
                  received_at TEXT NOT NULL,
                  UNIQUE(execution_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS connector_exercises (
                  id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  tenant_id TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS connector_assignment_budgets (
                  assignment_id TEXT PRIMARY KEY,
                  policy_digest TEXT NOT NULL,
                  policy_json TEXT NOT NULL,
                  max_write_count INTEGER NOT NULL,
                  max_payload_bytes INTEGER NOT NULL,
                  write_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )

    def _load_cache_sync(self) -> None:
        with self.storage._connect() as conn:
            manifests = conn.execute("SELECT record_json FROM connector_manifests").fetchall()
            bindings = conn.execute(
                "SELECT record_json FROM connector_tenant_bindings"
            ).fetchall()
        self._manifests = {
            (item.connector_id, item.version): item
            for row in manifests
            if (item := ConnectorManifest.model_validate_json(row["record_json"]))
        }
        self._bindings = {
            (item.connector_id, item.connector_version, item.tenant_id): item
            for row in bindings
            if (item := ConnectorTenantBinding.model_validate_json(row["record_json"]))
        }

    async def freeze_assignment_budget(
        self,
        *,
        assignment_id: str,
        allowed_network_hosts: list[str],
        allowed_compensation_operations: list[str],
        max_write_count: int,
        max_payload_bytes: int,
    ) -> ConnectorAssignmentBudgetReceipt:
        if not assignment_id:
            raise ValueError("assignment_id is required")
        if max_write_count < 0 or max_write_count > 1_000_000:
            raise ValueError("max_write_count is outside the supported range")
        if max_payload_bytes < 1 or max_payload_bytes > 100 * 1024 * 1024:
            raise ValueError("max_payload_bytes is outside the supported range")
        async with self._lock:
            await asyncio.to_thread(
                self._freeze_assignment_budget_sync,
                assignment_id,
                allowed_network_hosts,
                allowed_compensation_operations,
                max_write_count,
                max_payload_bytes,
            )
        return await self.export_assignment_budget(assignment_id)

    def _freeze_assignment_budget_sync(
        self,
        assignment_id: str,
        allowed_network_hosts: list[str],
        allowed_compensation_operations: list[str],
        max_write_count: int,
        max_payload_bytes: int,
    ) -> None:
        policy_document = self._assignment_budget_policy(
            allowed_network_hosts=allowed_network_hosts,
            allowed_compensation_operations=allowed_compensation_operations,
            max_write_count=max_write_count,
            max_payload_bytes=max_payload_bytes,
        )
        policy_json = self.canonical_json(policy_document)
        policy_digest = self.payload_hash(policy_document)
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT policy_digest,policy_json,max_write_count,max_payload_bytes
                   FROM connector_assignment_budgets WHERE assignment_id=?""",
                (assignment_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO connector_assignment_budgets(
                         assignment_id,policy_digest,policy_json,max_write_count,
                         max_payload_bytes,write_count,created_at,updated_at
                       ) VALUES(?,?,?,?,?,0,?,?)""",
                    (
                        assignment_id,
                        policy_digest,
                        policy_json,
                        max_write_count,
                        max_payload_bytes,
                        now,
                        now,
                    ),
                )
                return
            if (
                str(row["policy_digest"]) != policy_digest
                or str(row["policy_json"]) != policy_json
                or int(row["max_write_count"]) != max_write_count
                or int(row["max_payload_bytes"]) != max_payload_bytes
            ):
                raise ConnectorDenied("assigned connector side-effect policy changed")

    async def export_assignment_budget(
        self,
        assignment_id: str,
    ) -> ConnectorAssignmentBudgetReceipt:
        return await asyncio.to_thread(
            self._export_assignment_budget_sync,
            assignment_id,
        )

    def _export_assignment_budget_sync(
        self,
        assignment_id: str,
    ) -> ConnectorAssignmentBudgetReceipt:
        with self.storage._connect() as conn:
            row = conn.execute(
                """SELECT policy_digest,policy_json,max_write_count,max_payload_bytes,
                          write_count
                   FROM connector_assignment_budgets WHERE assignment_id=?""",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown connector assignment budget: {assignment_id}")
            execution_rows = conn.execute(
                """SELECT record_json FROM connector_executions
                   ORDER BY created_at,id"""
            ).fetchall()
        writes: list[ConnectorAssignmentWriteReceipt] = []
        for execution_row in execution_rows:
            execution = ConnectorExecution.model_validate_json(
                execution_row["record_json"]
            )
            if (
                execution.assignment_id != assignment_id
                or execution.operation_kind not in {"write", "compensate"}
            ):
                continue
            writes.append(
                ConnectorAssignmentWriteReceipt(
                    execution_id=execution.id,
                    connector_id=execution.connector_id,
                    connector_version=execution.connector_version,
                    tenant_id=execution.tenant_id,
                    profile_id=execution.profile_id,
                    operation_id=execution.operation_id,
                    operation_kind=execution.operation_kind,
                    idempotency_key=execution.idempotency_key,
                    payload_hash=execution.payload_hash,
                    status=execution.status,
                    side_effect_state=execution.side_effect_state,
                    authorization_ref_digest=(
                        f"sha256:{self.payload_hash(execution.authorization_id)}"
                        if execution.authorization_id
                        else None
                    ),
                    adapter_called=execution.adapter_called,
                    attempt_count=execution.attempt_count,
                    retryable=execution.retryable,
                    failure_disposition=execution.failure_disposition,
                    retry_safety=execution.retry_safety,
                    created_at=execution.created_at,
                    updated_at=execution.updated_at,
                )
            )
        writes.sort(
            key=lambda item: (
                item.connector_id,
                item.connector_version,
                item.tenant_id,
                item.operation_id,
                item.idempotency_key,
                item.execution_id,
            )
        )
        policy = json.loads(str(row["policy_json"]))
        unsigned = {
            "schema_version": "1.0",
            "assignment_id": assignment_id,
            "policy_digest": str(row["policy_digest"]),
            "allowed_network_hosts": list(policy["allowed_network_hosts"]),
            "allowed_compensation_operations": list(
                policy["allowed_compensation_operations"]
            ),
            "max_write_count": int(row["max_write_count"]),
            "max_payload_bytes": int(row["max_payload_bytes"]),
            "write_count": int(row["write_count"]),
            "writes": [item.model_dump(mode="json") for item in writes],
        }
        return ConnectorAssignmentBudgetReceipt(
            **unsigned,
            receipt_digest=f"sha256:{self.payload_hash(unsigned)}",
        )

    @staticmethod
    def _assignment_budget_policy(
        *,
        allowed_network_hosts: list[str],
        allowed_compensation_operations: list[str],
        max_write_count: int,
        max_payload_bytes: int,
    ) -> dict[str, Any]:
        return {
            "allowed_network_hosts": sorted(
                {
                    str(item).casefold().rstrip(".")
                    for item in allowed_network_hosts
                    if str(item).strip()
                }
            ),
            "allowed_compensation_operations": sorted(
                set(allowed_compensation_operations)
            ),
            "max_write_count": max_write_count,
            "max_payload_bytes": max_payload_bytes,
        }

    async def register_manifest(self, manifest: ConnectorManifest) -> ConnectorManifest:
        async with self._lock:
            await asyncio.to_thread(self._register_manifest_sync, manifest)
            self._manifests[(manifest.connector_id, manifest.version)] = manifest
        return manifest

    def _register_manifest_sync(self, manifest: ConnectorManifest) -> None:
        with self.storage._connect() as conn:
            existing = conn.execute(
                "SELECT record_json FROM connector_manifests WHERE connector_id=? AND version=?",
                (manifest.connector_id, manifest.version),
            ).fetchone()
            if existing:
                current = ConnectorManifest.model_validate_json(existing["record_json"])
                if current.model_dump(mode="json") == manifest.model_dump(mode="json"):
                    return
                raise ConnectorConflict("connector manifest version is immutable")
            conn.execute(
                "INSERT INTO connector_manifests VALUES(?,?,?,?)",
                (
                    manifest.connector_id,
                    manifest.version,
                    manifest.model_dump_json(),
                    manifest.created_at,
                ),
            )

    async def list_manifests(self) -> list[ConnectorManifest]:
        return sorted(
            self._manifests.values(),
            key=lambda item: (item.connector_id, item.version),
        )

    async def get_manifest(self, connector_id: str, version: int) -> ConnectorManifest:
        try:
            return self._manifests[(connector_id, version)]
        except KeyError as error:
            raise KeyError(f"unknown connector manifest: {connector_id}@{version}") from error

    async def upsert_binding(
        self,
        binding: ConnectorTenantBinding,
        *,
        expected_revision: int = 0,
    ) -> ConnectorTenantBinding:
        manifest = await self.get_manifest(binding.connector_id, binding.connector_version)
        manifest.profile(binding.profile_id)
        unknown_operations = sorted(
            set(binding.allowed_operations) - {item.id for item in manifest.operations}
        )
        if unknown_operations:
            raise ValueError(f"binding references unknown operations: {unknown_operations}")
        async with self._lock:
            saved = await asyncio.to_thread(
                self._upsert_binding_sync,
                binding,
                expected_revision,
            )
            self._bindings[(saved.connector_id, saved.connector_version, saved.tenant_id)] = saved
            return saved

    def _upsert_binding_sync(
        self,
        binding: ConnectorTenantBinding,
        expected_revision: int,
    ) -> ConnectorTenantBinding:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT record_json FROM connector_tenant_bindings
                   WHERE connector_id=? AND version=? AND tenant_id=?""",
                (binding.connector_id, binding.connector_version, binding.tenant_id),
            ).fetchone()
            if row:
                current = ConnectorTenantBinding.model_validate_json(row["record_json"])
                if expected_revision != current.revision:
                    raise ConnectorConflict(
                        f"connector tenant revision conflict: expected {expected_revision}, "
                        f"current {current.revision}"
                    )
                saved = binding.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "created_at": current.created_at,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    """UPDATE connector_tenant_bindings SET external_tenant_id=?,record_json=?,
                       updated_at=? WHERE connector_id=? AND version=? AND tenant_id=?""",
                    (
                        saved.external_tenant_id,
                        saved.model_dump_json(),
                        now,
                        saved.connector_id,
                        saved.connector_version,
                        saved.tenant_id,
                    ),
                )
            else:
                if expected_revision not in {0, 1}:
                    raise ConnectorConflict("new connector tenant binding expects revision 0")
                saved = binding.model_copy(
                    update={"revision": 1, "created_at": now, "updated_at": now}
                )
                conn.execute(
                    """INSERT INTO connector_tenant_bindings
                       (connector_id,version,tenant_id,external_tenant_id,record_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        saved.connector_id,
                        saved.connector_version,
                        saved.tenant_id,
                        saved.external_tenant_id,
                        saved.model_dump_json(),
                        now,
                        now,
                    ),
                )
            self._append_event_sync(
                conn,
                saved.connector_id,
                saved.connector_version,
                saved.tenant_id,
                "",
                "connector.tenant_binding.saved",
                {"revision": saved.revision, "profile_id": saved.profile_id},
                now,
            )
            return saved

    async def list_bindings(
        self,
        connector_id: str | None = None,
        *,
        tenant_id: str | None = None,
        application_id: str | None = None,
    ) -> list[ConnectorTenantBinding]:
        items = list(self._bindings.values())
        if connector_id:
            items = [item for item in items if item.connector_id == connector_id]
        if tenant_id:
            items = [item for item in items if item.tenant_id == tenant_id]
        if application_id:
            items = [item for item in items if application_id in item.application_ids]
        return sorted(items, key=lambda item: (item.connector_id, item.tenant_id))

    async def set_policy(
        self,
        policy: ConnectorDomainPolicy,
        *,
        expected_revision: int = 0,
    ) -> ConnectorDomainPolicy:
        manifest = await self.get_manifest(policy.connector_id, policy.connector_version)
        if policy.domain != manifest.domain:
            raise ValueError("connector policy domain does not match manifest")
        unknown_profiles = sorted(
            set(policy.allowed_profiles) - {item.id for item in manifest.deployment_profiles}
        )
        unknown_operations = sorted(
            set(policy.allowed_operations) - {item.id for item in manifest.operations}
        )
        if unknown_profiles or unknown_operations:
            raise ValueError(
                f"connector policy has unknown profiles={unknown_profiles}, "
                f"operations={unknown_operations}"
            )
        for constraint in policy.operation_request_constraints:
            project_connector_operation_request_schema(
                manifest.operation(constraint.operation_id),
                constraint,
            )
        async with self._lock:
            return await asyncio.to_thread(
                self._set_policy_sync,
                policy,
                expected_revision,
            )

    def _set_policy_sync(
        self,
        policy: ConnectorDomainPolicy,
        expected_revision: int,
    ) -> ConnectorDomainPolicy:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT record_json FROM connector_domain_policies
                   WHERE connector_id=? AND version=? AND tenant_id=?""",
                (policy.connector_id, policy.connector_version, policy.tenant_id),
            ).fetchone()
            if row:
                current = ConnectorDomainPolicy.model_validate_json(row["record_json"])
                if current.revision != expected_revision:
                    raise ConnectorConflict(
                        f"connector policy revision conflict: expected {expected_revision}, "
                        f"current {current.revision}"
                    )
                saved = policy.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "created_at": current.created_at,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    """UPDATE connector_domain_policies SET record_json=?,updated_at=?
                       WHERE connector_id=? AND version=? AND tenant_id=?""",
                    (
                        saved.model_dump_json(),
                        now,
                        saved.connector_id,
                        saved.connector_version,
                        saved.tenant_id,
                    ),
                )
            else:
                if expected_revision not in {0, 1}:
                    raise ConnectorConflict("new connector policy expects revision 0")
                saved = policy.model_copy(
                    update={"revision": 1, "created_at": now, "updated_at": now}
                )
                conn.execute(
                    """INSERT INTO connector_domain_policies
                       (connector_id,version,tenant_id,record_json,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        saved.connector_id,
                        saved.connector_version,
                        saved.tenant_id,
                        saved.model_dump_json(),
                        now,
                    ),
                )
            self._append_event_sync(
                conn,
                saved.connector_id,
                saved.connector_version,
                saved.tenant_id,
                "",
                "connector.policy.updated",
                {
                    "revision": saved.revision,
                    "emergency_stop": saved.emergency_stop,
                    "reason": saved.emergency_reason,
                },
                now,
            )
            return saved

    async def get_policy(
        self,
        connector_id: str,
        version: int,
        tenant_id: str,
    ) -> ConnectorDomainPolicy:
        row = await asyncio.to_thread(
            self.storage._get_one,
            """SELECT record_json FROM connector_domain_policies
               WHERE connector_id=? AND version=? AND tenant_id=?""",
            (connector_id, version, tenant_id),
        )
        return ConnectorDomainPolicy.model_validate_json(row["record_json"])

    async def list_policies(
        self,
        *,
        application_id: str | None = None,
    ) -> list[ConnectorDomainPolicy]:
        rows = await asyncio.to_thread(
            self.storage._get_all,
            "SELECT record_json FROM connector_domain_policies ORDER BY connector_id,tenant_id",
            (),
        )
        items = [ConnectorDomainPolicy.model_validate_json(row["record_json"]) for row in rows]
        if application_id:
            bindings = await self.list_bindings(application_id=application_id)
            allowed = {
                (item.connector_id, item.connector_version, item.tenant_id)
                for item in bindings
            }
            items = [
                item for item in items
                if (item.connector_id, item.connector_version, item.tenant_id) in allowed
            ]
        return items

    async def create_authorization(
        self,
        *,
        connector_id: str,
        connector_version: int,
        tenant_id: str,
        actor_id: str,
        profile_id: str,
        operation_id: str,
        payload: dict[str, Any],
        assignment_id: str = "",
        session_id: str = "",
        application_id: str = "",
        run_id: str = "",
        expires_in_seconds: int = 300,
        max_uses: int = 1,
        issuance_source: Literal["owner", "task_policy"] = "owner",
        descriptor_digest: str = "",
        task_credential_ref_digest: str = "",
        task_policy_digest: str = "",
        allowed_actions_digest: str = "",
        budget_digest: str = "",
        assignment_budget_policy_digest: str = "",
        assignment_max_write_count: int | None = None,
        assignment_max_payload_bytes: int | None = None,
        assignment_write_count_at_issue: int | None = None,
        task_deadline_at: str = "",
    ) -> ConnectorAuthorization:
        manifest = await self.get_manifest(connector_id, connector_version)
        operation = manifest.operation(operation_id)
        policy = await self.get_policy(connector_id, connector_version, tenant_id)
        authorized_payload = operation.request_schema.validate_payload(
            payload,
            label="authorization payload",
        )
        if (
            profile_id not in policy.allowed_profiles
            or operation_id not in policy.allowed_operations
        ):
            raise ConnectorDenied("connector policy does not allow the authorization scope")
        enforce_connector_operation_request_constraint(
            operation,
            policy.request_constraint(operation_id),
            authorized_payload,
        )
        now = datetime.now(timezone.utc)
        authorization = ConnectorAuthorization(
            id=f"cauth-{uuid4()}",
            connector_id=connector_id,
            connector_version=connector_version,
            tenant_id=tenant_id,
            actor_id=actor_id,
            profile_id=profile_id,
            operation_id=operation_id,
            operation_kind=operation.kind,
            payload_hash=self.payload_hash(authorized_payload),
            policy_revision=policy.revision,
            issuance_source=issuance_source,
            descriptor_digest=descriptor_digest,
            task_credential_ref_digest=task_credential_ref_digest,
            task_policy_digest=task_policy_digest,
            allowed_actions_digest=allowed_actions_digest,
            budget_digest=budget_digest,
            assignment_budget_policy_digest=assignment_budget_policy_digest,
            assignment_max_write_count=assignment_max_write_count,
            assignment_max_payload_bytes=assignment_max_payload_bytes,
            assignment_write_count_at_issue=assignment_write_count_at_issue,
            task_deadline_at=task_deadline_at,
            assignment_id=assignment_id,
            session_id=session_id,
            application_id=application_id,
            run_id=run_id,
            max_uses=max_uses,
            expires_at=(now + timedelta(seconds=max(1, expires_in_seconds))).isoformat(),
        )
        await asyncio.to_thread(self._save_authorization_sync, authorization)
        return authorization

    def _save_authorization_sync(self, authorization: ConnectorAuthorization) -> None:
        with self.storage._connect() as conn:
            conn.execute(
                """INSERT INTO connector_authorizations
                   (id,connector_id,version,tenant_id,record_json,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    authorization.id,
                    authorization.connector_id,
                    authorization.connector_version,
                    authorization.tenant_id,
                    authorization.model_dump_json(),
                    authorization.updated_at,
                ),
            )
            self._append_event_sync(
                conn,
                authorization.connector_id,
                authorization.connector_version,
                authorization.tenant_id,
                "",
                "connector.authorization.created",
                {
                    "authorization_id": authorization.id,
                    "operation_id": authorization.operation_id,
                    "operation_kind": authorization.operation_kind,
                    "issuance_source": authorization.issuance_source,
                    "assignment_id": authorization.assignment_id,
                    "application_id": authorization.application_id,
                    "payload_hash": authorization.payload_hash,
                    "expires_at": authorization.expires_at,
                },
                authorization.created_at,
            )

    async def resolve_embedding_identity(
        self,
        envelope: ConnectorEmbeddingEnvelope,
        signature: str,
    ) -> ConnectorIdentityContext:
        binding = self._binding_by_external(
            envelope.connector_id,
            envelope.connector_version,
            envelope.external_tenant_id,
        )
        if not binding.enabled:
            raise ConnectorDenied("connector tenant binding is disabled")
        if envelope.application_id not in binding.application_ids:
            raise ConnectorDenied("application is outside the connector tenant binding")
        now = datetime.now(timezone.utc)
        issued_at = self._parse_time(envelope.issued_at)
        expires_at = self._parse_time(envelope.expires_at)
        if issued_at > now + timedelta(seconds=30) or expires_at <= now:
            raise ConnectorDenied("embedding signature window is invalid or expired")
        if expires_at - issued_at > timedelta(minutes=10):
            raise ConnectorDenied("embedding signature window exceeds ten minutes")
        secret = await self._tenant_secret(binding)
        expected = self.sign_payload(secret, envelope.model_dump(mode="json"))
        if not hmac.compare_digest(expected, signature):
            raise ConnectorDenied("embedding signature is invalid")
        subject = next(
            (
                item
                for item in binding.subjects
                if item.external_subject == envelope.external_subject
            ),
            None,
        )
        if subject is None:
            raise ConnectorDenied("embedding subject is not mapped for this tenant")
        await asyncio.to_thread(self._consume_nonce_sync, envelope)
        return ConnectorIdentityContext(
            connector_id=binding.connector_id,
            connector_version=binding.connector_version,
            tenant_id=binding.tenant_id,
            actor_id=subject.actor_id,
            actor_roles=list(subject.roles),
            profile_id=binding.profile_id,
            application_id=envelope.application_id,
        )

    def _consume_nonce_sync(self, envelope: ConnectorEmbeddingEnvelope) -> None:
        with self.storage._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO connector_embedding_nonces
                       (connector_id,version,external_tenant_id,nonce,expires_at,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        envelope.connector_id,
                        envelope.connector_version,
                        envelope.external_tenant_id,
                        envelope.nonce,
                        envelope.expires_at,
                        utc_now(),
                    ),
                )
            except Exception as error:
                if "UNIQUE constraint failed" in str(error):
                    raise ConnectorConflict("embedding nonce replay rejected") from error
                raise

    async def execute(self, request: ConnectorExecutionRequest) -> ConnectorExecution:
        manifest = await self.get_manifest(request.connector_id, request.connector_version)
        operation = manifest.operation(request.operation_id)
        profile = manifest.profile(request.profile_id)
        binding = self._binding(request.connector_id, request.connector_version, request.tenant_id)
        policy = await self.get_policy(
            request.connector_id,
            request.connector_version,
            request.tenant_id,
        )
        payload = operation.request_schema.validate_payload(
            request.payload,
            label=f"{request.operation_id} request",
        )
        try:
            self._preflight(
                request,
                manifest,
                operation,
                profile,
                binding,
                policy,
                payload,
            )
        except ConnectorDenied as error:
            await asyncio.to_thread(
                self._record_denial_sync,
                request,
                policy,
                str(error),
            )
            raise
        async with self._lock:
            reserved, replay = await asyncio.to_thread(
                self._reserve_execution_sync,
                request,
                operation,
                binding,
                policy,
                payload,
            )
        if replay:
            replayed = reserved.model_copy(update={"replayed": True})
            if replayed.status == "failed":
                raise ConnectorAdapterError.from_execution(replayed)
            return replayed
        if request.dry_run:
            return reserved
        try:
            response = await self._call_adapter(
                manifest=manifest,
                operation=operation,
                profile=profile,
                binding=binding,
                request=request,
                payload=payload,
            )
            self.validate_operation_response(operation, response)
            return await asyncio.to_thread(
                self._finish_execution_sync,
                reserved.id,
                response,
                "",
            )
        except ConnectorAdapterError as adapter_error:
            error = self._bounded_adapter_error(
                operation,
                reserved,
                adapter_error,
            )
            await asyncio.to_thread(
                self._finish_execution_sync,
                reserved.id,
                {},
                str(error),
                error.retryable,
                error.side_effect_state,
                error.adapter_called,
                error.failure_disposition,
                error.retry_safety,
            )
            if error is adapter_error:
                raise
            raise error from adapter_error
        except Exception as error:
            failure = ConnectorAdapterError(
                str(error),
                retryable=False,
                side_effect_state=(
                    "unknown" if operation.mutating else "none"
                ),
                adapter_called=True,
                failure_disposition=(
                    "ambiguous" if operation.mutating else "terminal"
                ),
            )
            await asyncio.to_thread(
                self._finish_execution_sync,
                reserved.id,
                {},
                str(failure),
                failure.retryable,
                failure.side_effect_state,
                failure.adapter_called,
                failure.failure_disposition,
                failure.retry_safety,
            )
            raise failure from error

    @staticmethod
    def _bounded_adapter_error(
        operation: ConnectorOperation,
        execution: ConnectorExecution,
        error: ConnectorAdapterError,
    ) -> ConnectorAdapterError:
        side_effect_state: Literal["none", "unknown"] = (
            "unknown"
            if execution.side_effect_state == "unknown"
            or error.side_effect_state == "unknown"
            else "none"
        )
        retryable = error.retryable
        retry_safety = error.retry_safety
        if retryable and operation.mutating and side_effect_state == "unknown":
            if operation.idempotency_semantics != "request_key":
                retryable = False
            else:
                retry_safety = "idempotency_key"
        if retryable and execution.attempt_count >= operation.max_attempts:
            retryable = False
        failure_disposition: ConnectorFailureDisposition = (
            "retryable"
            if retryable
            else "ambiguous"
            if side_effect_state == "unknown"
            else "terminal"
        )
        if not retryable:
            retry_safety = "none"
        if (
            retryable == error.retryable
            and side_effect_state == error.side_effect_state
            and failure_disposition == error.failure_disposition
            and retry_safety == error.retry_safety
        ):
            return error
        return ConnectorAdapterError(
            str(error),
            retryable=retryable,
            side_effect_state=side_effect_state,
            adapter_called=error.adapter_called,
            failure_disposition=failure_disposition,
            retry_safety=retry_safety,
        )

    def _preflight(
        self,
        request: ConnectorExecutionRequest,
        manifest: ConnectorManifest,
        operation: ConnectorOperation,
        profile: ConnectorDeploymentProfile,
        binding: ConnectorTenantBinding,
        policy: ConnectorDomainPolicy,
        payload: dict[str, Any],
    ) -> None:
        if not binding.enabled or binding.profile_id != request.profile_id:
            raise ConnectorDenied("connector tenant profile is disabled or mismatched")
        if request.operation_id not in binding.allowed_operations:
            raise ConnectorDenied("connector tenant binding denies this operation")
        mapped_subject = next(
            (item for item in binding.subjects if item.actor_id == request.actor_id),
            None,
        )
        if mapped_subject is None:
            raise ConnectorDenied("connector actor is not mapped for this tenant")
        if set(request.actor_roles) != set(mapped_subject.roles):
            raise ConnectorDenied("connector actor roles do not match the tenant mapping")
        if (
            request.application_id
            and request.application_id not in binding.application_ids
        ):
            raise ConnectorDenied(
                "application is outside the connector tenant binding"
            )
        if request.profile_id not in policy.allowed_profiles:
            raise ConnectorDenied("connector policy denies this deployment profile")
        if request.operation_id not in policy.allowed_operations:
            raise ConnectorDenied("connector policy denies this operation")
        if manifest.domain != policy.domain:
            raise ConnectorDenied("connector policy domain mismatch")
        reserved_headers = {
            "authorization",
            "content-length",
            "cookie",
            "host",
            "idempotency-key",
            "transfer-encoding",
            "x-lilies-actor",
            "x-lilies-tenant",
        }
        if profile.auth_type == "api_key" and profile.auth_location == "header":
            reserved_headers.add(profile.auth_wire_name.casefold())
        conflicting_headers = sorted(
            parameter.wire_name
            for parameter in operation.parameters
            if parameter.location == "header"
            and parameter.wire_name.casefold() in reserved_headers
        )
        if conflicting_headers:
            raise ConnectorDenied(
                "connector operation declares platform-controlled request headers: "
                f"{conflicting_headers}"
            )
        required_roles = set(policy.required_roles) | set(operation.required_roles)
        if required_roles and not required_roles.intersection(request.actor_roles):
            raise ConnectorDenied("connector actor lacks a required role")
        enforce_connector_operation_request_constraint(
            operation,
            policy.request_constraint(request.operation_id),
            payload,
        )
        payload_bytes = len(self.canonical_json(payload).encode())
        if payload_bytes > policy.max_payload_bytes:
            raise ConnectorDenied("connector payload exceeds policy limit")
        if request.assignment_id:
            if (
                not request.session_id
                or not request.application_id
                or request.allowed_compensation_operations is None
            ):
                raise ConnectorDenied(
                    "assigned connector identity or compensation policy is missing"
                )
            if request.allowed_network_hosts is None:
                raise ConnectorDenied(
                    "assigned connector host policy is missing"
                )
            endpoint_host = (urlsplit(profile.base_url).hostname or "").casefold().rstrip(".")
            assigned_hosts = {
                str(item).casefold().rstrip(".")
                for item in request.allowed_network_hosts
                if str(item).strip()
            }
            if endpoint_host not in assigned_hosts:
                raise ConnectorDenied(
                    "connector deployment host is outside the assigned host policy"
                )
            if request.assignment_max_payload_bytes is None:
                raise ConnectorDenied(
                    "assigned connector payload budget is missing"
                )
            if payload_bytes > request.assignment_max_payload_bytes:
                raise ConnectorDenied(
                    "connector payload exceeds the assigned byte limit"
                )
            if (
                operation.mutating
                and not request.dry_run
                and request.assignment_max_write_count is None
            ):
                raise ConnectorDenied(
                    "assigned connector write budget is missing"
                )
            if operation.kind == "compensate":
                self._require_assigned_operation(
                    connector_id=request.connector_id,
                    operation_id=request.operation_id,
                    allowed_operations=request.allowed_compensation_operations,
                    label="compensation",
                )
        if request.dry_run and not policy.allow_dry_run:
            raise ConnectorDenied("connector policy disables dry-run")
        if not profile.available:
            raise ConnectorDenied("connector deployment profile is unavailable")
        if operation.mutating and policy.emergency_stop:
            allowed_recovery = (
                operation.kind == "compensate" and policy.allow_compensation_during_stop
            )
            if not allowed_recovery:
                raise ConnectorDenied(
                    f"connector emergency stop is active: {policy.emergency_reason or 'no reason'}"
                )

    @staticmethod
    def _require_assigned_operation(
        *,
        connector_id: str,
        operation_id: str,
        allowed_operations: list[str],
        label: str,
    ) -> str:
        candidates = {
            operation_id,
            f"{connector_id}.{operation_id}",
            f"{connector_id}:{operation_id}",
        }
        matched = sorted(candidates.intersection(allowed_operations))
        if len(matched) != 1:
            raise ConnectorDenied(
                f"connector {label} is outside or ambiguous in the assigned policy"
            )
        return matched[0]

    def _record_denial_sync(
        self,
        request: ConnectorExecutionRequest,
        policy: ConnectorDomainPolicy,
        reason: str,
    ) -> None:
        now = utc_now()
        with self.storage._connect() as conn:
            self._append_event_sync(
                conn,
                request.connector_id,
                request.connector_version,
                request.tenant_id,
                "",
                "connector.execution.denied",
                {
                    "operation_id": request.operation_id,
                    "actor_id": request.actor_id,
                    "actor_roles": request.actor_roles,
                    "profile_id": request.profile_id,
                    "payload_hash": self.payload_hash(request.payload),
                    "policy_revision": policy.revision,
                    "reason": reason,
                    "adapter_called": False,
                    "stage": "preflight",
                },
                now,
            )

    def _reserve_execution_sync(
        self,
        request: ConnectorExecutionRequest,
        operation: ConnectorOperation,
        binding: ConnectorTenantBinding,
        policy: ConnectorDomainPolicy,
        payload: dict[str, Any],
    ) -> tuple[ConnectorExecution, bool]:
        now = utc_now()
        payload_hash = self.payload_hash(payload)
        persisted_payload = self._persistence_safe_payload(operation, payload)
        storage_idempotency_key = request.idempotency_key
        if self._environment_epoch_digest:
            storage_idempotency_key = (
                f"{storage_idempotency_key}:environment:"
                f"{self._environment_epoch_digest}"
            )
        if operation.kind == "read" and request.run_id:
            run_scope = hashlib.sha256(request.run_id.encode()).hexdigest()[:24]
            storage_idempotency_key = (
                f"{storage_idempotency_key}:read-run:{run_scope}"
            )
        execution_id = self.execution_id(
            request.connector_id,
            request.connector_version,
            request.tenant_id,
            request.operation_id,
            storage_idempotency_key,
        )
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT record_json FROM connector_executions WHERE connector_id=? AND version=?
                   AND tenant_id=? AND operation_id=? AND idempotency_key=?""",
                (
                    request.connector_id,
                    request.connector_version,
                    request.tenant_id,
                    request.operation_id,
                    storage_idempotency_key,
                ),
            ).fetchone()
            if existing:
                record = ConnectorExecution.model_validate_json(existing["record_json"])
                immutable = (
                    record.actor_id,
                    sorted(record.actor_roles),
                    record.profile_id,
                    record.payload_hash,
                    record.application_id,
                    record.assignment_id,
                    record.session_id,
                    record.assigned_allowed_network_hosts,
                    record.assigned_compensation_operations,
                    record.assignment_max_write_count,
                    record.assignment_max_payload_bytes,
                )
                if immutable != (
                    request.actor_id,
                    sorted(request.actor_roles),
                    request.profile_id,
                    payload_hash,
                    request.application_id,
                    request.assignment_id,
                    request.session_id,
                    (
                        sorted(
                            {
                                host.casefold().rstrip(".")
                                for host in request.allowed_network_hosts
                            }
                        )
                        if request.allowed_network_hosts is not None
                        else None
                    ),
                    (
                        sorted(set(request.allowed_compensation_operations))
                        if request.allowed_compensation_operations is not None
                        else None
                    ),
                    request.assignment_max_write_count,
                    request.assignment_max_payload_bytes,
                ):
                    raise ConnectorConflict(
                        "connector idempotency key is bound to different input"
                    )
                if record.status == "executing":
                    raise ConnectorConflict("connector execution is already in progress")
                if (
                    record.status == "failed"
                    and operation.kind == "read"
                    and record.binding_revision != binding.revision
                ):
                    refreshed = record.model_copy(
                        update={
                            "status": "executing",
                            "side_effect_state": "none",
                            "binding_revision": binding.revision,
                            "adapter_called": False,
                            "response": {},
                            "response_hash": "",
                            "external_reference": "",
                            "replayed": False,
                            "attempt_count": record.attempt_count + 1,
                            "retryable": False,
                            "failure_disposition": "none",
                            "retry_safety": "none",
                            "error": "",
                            "updated_at": now,
                            "finished_at": None,
                        }
                    )
                    conn.execute(
                        """UPDATE connector_executions SET status=?,record_json=?,updated_at=?
                           WHERE id=?""",
                        (
                            refreshed.status,
                            refreshed.model_dump_json(),
                            now,
                            refreshed.id,
                        ),
                    )
                    self._append_event_sync(
                        conn,
                        refreshed.connector_id,
                        refreshed.connector_version,
                        refreshed.tenant_id,
                        refreshed.id,
                        "connector.execution.binding_refresh_started",
                        {
                            "operation_id": refreshed.operation_id,
                            "previous_binding_revision": record.binding_revision,
                            "binding_revision": binding.revision,
                            "attempt": refreshed.attempt_count,
                            "side_effect_safe": True,
                        },
                        now,
                    )
                    return refreshed, False
                if record.status == "failed" and record.retryable:
                    if request.dry_run:
                        raise ConnectorConflict(
                            "a real connector execution cannot be replayed as dry-run"
                        )
                    if record.retry_safety == "none":
                        raise ConnectorConflict(
                            "connector retryable receipt has no replay safety contract"
                        )
                    if record.policy_revision != policy.revision:
                        raise ConnectorConflict(
                            "connector policy changed before a safe retry"
                        )
                    if (
                        record.authorization_id
                        and record.authorization_id != request.authorization_id
                    ):
                        raise ConnectorConflict(
                            "connector retry must preserve its original authorization"
                        )
                    if record.authorization_id:
                        self._authorization_for_request_sync(
                            conn,
                            request,
                            policy,
                            payload_hash,
                            operation.kind,
                        )
                    retried = record.model_copy(
                        update={
                            "status": "executing",
                            "replayed": False,
                            "attempt_count": record.attempt_count + 1,
                            "retryable": False,
                            "failure_disposition": "none",
                            "retry_safety": "none",
                            "error": "",
                            "updated_at": now,
                            "finished_at": None,
                        }
                    )
                    conn.execute(
                        """UPDATE connector_executions SET status=?,record_json=?,updated_at=?
                           WHERE id=?""",
                        (
                            retried.status,
                            retried.model_dump_json(),
                            now,
                            retried.id,
                        ),
                    )
                    self._append_event_sync(
                        conn,
                        retried.connector_id,
                        retried.connector_version,
                        retried.tenant_id,
                        retried.id,
                        "connector.execution.retry_started",
                        {
                            "attempt": retried.attempt_count,
                            "retry_safety": record.retry_safety,
                            "authorization_reused": bool(record.authorization_id),
                            "assignment_budget_reused": bool(record.assignment_id),
                        },
                        now,
                    )
                    return retried, False
                if record.status == "dry_run" and not request.dry_run:
                    current_policy_row = conn.execute(
                        """SELECT record_json FROM connector_domain_policies
                           WHERE connector_id=? AND version=? AND tenant_id=?""",
                        (
                            request.connector_id,
                            request.connector_version,
                            request.tenant_id,
                        ),
                    ).fetchone()
                    if not current_policy_row:
                        raise ConnectorDenied("connector policy is missing")
                    current_policy = ConnectorDomainPolicy.model_validate_json(
                        current_policy_row["record_json"]
                    )
                    if current_policy.revision != policy.revision:
                        raise ConnectorConflict("connector policy changed during execution")
                    enforce_connector_operation_request_constraint(
                        operation,
                        current_policy.request_constraint(request.operation_id),
                        payload,
                    )
                    if operation.mutating or request.permission_required:
                        self._consume_authorization_sync(
                            conn,
                            request,
                            current_policy,
                            payload_hash,
                            operation.kind,
                            force_required=request.permission_required,
                        )
                    if operation.mutating:
                        self._consume_assignment_budget_sync(
                            conn,
                            request,
                            payload,
                        )
                    promoted = record.model_copy(
                        update={
                            "status": "executing",
                            "policy_revision": current_policy.revision,
                            "authorization_id": request.authorization_id,
                            "run_id": request.run_id,
                            "replayed": False,
                            "attempt_count": 1,
                            "updated_at": now,
                            "finished_at": None,
                        }
                    )
                    conn.execute(
                        """UPDATE connector_executions SET status=?,record_json=?,updated_at=?
                           WHERE id=?""",
                        (
                            promoted.status,
                            promoted.model_dump_json(),
                            now,
                            promoted.id,
                        ),
                    )
                    self._append_event_sync(
                        conn,
                        promoted.connector_id,
                        promoted.connector_version,
                        promoted.tenant_id,
                        promoted.id,
                        "connector.execution.dry_run_promoted",
                        {
                            "policy_revision": promoted.policy_revision,
                            "authorization_id": promoted.authorization_id,
                            "adapter_called": False,
                        },
                        now,
                    )
                    return promoted, False
                return record, True
            current_policy_row = conn.execute(
                """SELECT record_json FROM connector_domain_policies
                   WHERE connector_id=? AND version=? AND tenant_id=?""",
                (request.connector_id, request.connector_version, request.tenant_id),
            ).fetchone()
            if not current_policy_row:
                raise ConnectorDenied("connector policy is missing")
            current_policy = ConnectorDomainPolicy.model_validate_json(
                current_policy_row["record_json"]
            )
            if current_policy.revision != policy.revision:
                raise ConnectorConflict("connector policy changed during execution")
            enforce_connector_operation_request_constraint(
                operation,
                current_policy.request_constraint(request.operation_id),
                payload,
            )
            if (
                (operation.mutating or request.permission_required)
                and not request.dry_run
            ):
                self._consume_authorization_sync(
                    conn,
                    request,
                    current_policy,
                    payload_hash,
                    operation.kind,
                    force_required=request.permission_required,
                )
            if operation.mutating and not request.dry_run:
                self._consume_assignment_budget_sync(
                    conn,
                    request,
                    payload,
                )
            status: ConnectorExecutionStatus = "dry_run" if request.dry_run else "executing"
            record = ConnectorExecution(
                id=execution_id,
                connector_id=request.connector_id,
                connector_version=request.connector_version,
                tenant_id=request.tenant_id,
                actor_id=request.actor_id,
                actor_roles=list(request.actor_roles),
                application_id=request.application_id,
                run_id=request.run_id,
                assignment_id=request.assignment_id,
                session_id=request.session_id,
                assigned_allowed_network_hosts=(
                    sorted(
                        {
                            host.casefold().rstrip(".")
                            for host in request.allowed_network_hosts
                        }
                    )
                    if request.allowed_network_hosts is not None
                    else None
                ),
                assigned_compensation_operations=(
                    sorted(set(request.allowed_compensation_operations))
                    if request.allowed_compensation_operations is not None
                    else None
                ),
                assignment_max_write_count=request.assignment_max_write_count,
                assignment_max_payload_bytes=request.assignment_max_payload_bytes,
                profile_id=request.profile_id,
                operation_id=request.operation_id,
                operation_kind=operation.kind,
                idempotency_key=request.idempotency_key,
                payload_hash=payload_hash,
                request_payload=persisted_payload,
                status=status,
                policy_revision=current_policy.revision,
                binding_revision=binding.revision,
                authorization_id=request.authorization_id,
                attempt_count=0 if request.dry_run else 1,
                finished_at=now if request.dry_run else None,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO connector_executions
                   (id,connector_id,version,tenant_id,operation_id,idempotency_key,status,
                    record_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.id,
                    record.connector_id,
                    record.connector_version,
                    record.tenant_id,
                    record.operation_id,
                    storage_idempotency_key,
                    record.status,
                    record.model_dump_json(),
                    now,
                    now,
                ),
            )
            self._append_event_sync(
                conn,
                record.connector_id,
                record.connector_version,
                record.tenant_id,
                record.id,
                "connector.execution.authorized",
                {
                    "operation_id": record.operation_id,
                    "policy_revision": record.policy_revision,
                    "authorization_id": record.authorization_id,
                    "dry_run": request.dry_run,
                },
                now,
            )
            if request.dry_run:
                self._append_event_sync(
                    conn,
                    record.connector_id,
                    record.connector_version,
                    record.tenant_id,
                    record.id,
                    "connector.execution.dry_run_completed",
                    {"adapter_called": False},
                    now,
                )
            return record, False

    def _consume_authorization_sync(
        self,
        conn: Any,
        request: ConnectorExecutionRequest,
        policy: ConnectorDomainPolicy,
        payload_hash: str,
        operation_kind: ConnectorOperationKind,
        *,
        force_required: bool = False,
    ) -> None:
        if not policy.mutation_preauthorization_required and not force_required:
            return
        authorization = self._authorization_for_request_sync(
            conn,
            request,
            policy,
            payload_hash,
            operation_kind,
        )
        if authorization.use_count >= authorization.max_uses:
            raise ConnectorDenied("connector authorization is revoked or exhausted")
        updated = authorization.model_copy(
            update={"use_count": authorization.use_count + 1, "updated_at": utc_now()}
        )
        conn.execute(
            "UPDATE connector_authorizations SET record_json=?,updated_at=? WHERE id=?",
            (updated.model_dump_json(), updated.updated_at, updated.id),
        )

    def _authorization_for_request_sync(
        self,
        conn: Any,
        request: ConnectorExecutionRequest,
        policy: ConnectorDomainPolicy,
        payload_hash: str,
        operation_kind: ConnectorOperationKind,
    ) -> ConnectorAuthorization:
        if not request.authorization_id:
            raise ConnectorDenied("connector mutation requires preauthorization")
        row = conn.execute(
            "SELECT record_json FROM connector_authorizations WHERE id=?",
            (request.authorization_id,),
        ).fetchone()
        if not row:
            raise ConnectorDenied("connector authorization does not exist")
        authorization = ConnectorAuthorization.model_validate_json(row["record_json"])
        expected = (
            request.connector_id,
            request.connector_version,
            request.tenant_id,
            request.actor_id,
            request.profile_id,
            request.operation_id,
            payload_hash,
            policy.revision,
        )
        actual = (
            authorization.connector_id,
            authorization.connector_version,
            authorization.tenant_id,
            authorization.actor_id,
            authorization.profile_id,
            authorization.operation_id,
            authorization.payload_hash,
            authorization.policy_revision,
        )
        if actual != expected:
            raise ConnectorDenied("connector authorization scope does not match execution")
        if request.assignment_id:
            assigned_expected = (
                request.assignment_id,
                request.session_id,
                request.application_id,
            )
            assigned_actual = (
                authorization.assignment_id,
                authorization.session_id,
                authorization.application_id,
            )
            if (
                not all(assigned_actual)
                or assigned_actual != assigned_expected
            ):
                raise ConnectorDenied(
                    "connector authorization assignment scope does not match execution"
                )
            if (
                authorization.run_id
                and authorization.run_id != request.run_id
            ):
                raise ConnectorDenied(
                    "connector authorization run scope does not match execution"
                )
        if authorization.issuance_source == "task_policy":
            if (
                authorization.operation_kind != operation_kind
                or request.assignment_max_write_count
                != authorization.assignment_max_write_count
                or request.assignment_max_payload_bytes
                != authorization.assignment_max_payload_bytes
                or request.allowed_network_hosts is None
                or request.allowed_compensation_operations is None
            ):
                raise ConnectorDenied(
                    "task-policy connector authorization budget scope does not "
                    "match execution"
                )
            budget_policy = self._assignment_budget_policy(
                allowed_network_hosts=request.allowed_network_hosts,
                allowed_compensation_operations=(
                    request.allowed_compensation_operations
                ),
                max_write_count=request.assignment_max_write_count,
                max_payload_bytes=request.assignment_max_payload_bytes,
            )
            budget_policy_digest = (
                f"sha256:{self.payload_hash(budget_policy)}"
            )
            if not hmac.compare_digest(
                budget_policy_digest,
                authorization.assignment_budget_policy_digest,
            ):
                raise ConnectorDenied(
                    "task-policy connector authorization budget changed"
                )
        if authorization.revoked:
            raise ConnectorDenied("connector authorization is revoked or exhausted")
        if self._parse_time(authorization.expires_at) <= datetime.now(timezone.utc):
            raise ConnectorDenied("connector authorization is expired")
        return authorization

    def _consume_assignment_budget_sync(
        self,
        conn: Any,
        request: ConnectorExecutionRequest,
        payload: dict[str, Any],
    ) -> None:
        """Atomically reserve one real host mutation for a frozen assignment.

        The Connector service, rather than the in-memory workflow runner, owns
        this counter so separate runs, processes, and restarts share one exact
        N-write ceiling. Idempotent replays return before this method is called.
        """

        if not request.assignment_id:
            return
        if (
            request.assignment_max_write_count is None
            or request.assignment_max_payload_bytes is None
            or request.allowed_network_hosts is None
        ):
            raise ConnectorDenied("assigned connector side-effect budget is incomplete")
        payload_bytes = len(self.canonical_json(payload).encode("utf-8"))
        if payload_bytes > request.assignment_max_payload_bytes:
            raise ConnectorDenied("connector payload exceeds the assigned byte limit")
        policy_document = self._assignment_budget_policy(
            allowed_network_hosts=request.allowed_network_hosts,
            allowed_compensation_operations=(
                request.allowed_compensation_operations
                if request.allowed_compensation_operations is not None
                else []
            ),
            max_write_count=request.assignment_max_write_count,
            max_payload_bytes=request.assignment_max_payload_bytes,
        )
        policy_json = self.canonical_json(policy_document)
        policy_digest = self.payload_hash(policy_document)
        now = utc_now()
        row = conn.execute(
            """SELECT policy_digest,policy_json,max_write_count,max_payload_bytes,write_count
               FROM connector_assignment_budgets WHERE assignment_id=?""",
            (request.assignment_id,),
        ).fetchone()
        if row is None:
            if request.assignment_max_write_count < 1:
                raise ConnectorDenied("assigned connector write limit is exhausted")
            conn.execute(
                """INSERT INTO connector_assignment_budgets(
                     assignment_id,policy_digest,policy_json,max_write_count,max_payload_bytes,
                     write_count,created_at,updated_at
                   ) VALUES(?,?,?,?,?,1,?,?)""",
                (
                    request.assignment_id,
                    policy_digest,
                    policy_json,
                    request.assignment_max_write_count,
                    request.assignment_max_payload_bytes,
                    now,
                    now,
                ),
            )
            return
        if (
            str(row["policy_digest"]) != policy_digest
            or str(row["policy_json"]) != policy_json
            or int(row["max_write_count"]) != request.assignment_max_write_count
            or int(row["max_payload_bytes"]) != request.assignment_max_payload_bytes
        ):
            raise ConnectorDenied("assigned connector side-effect policy changed")
        if int(row["write_count"]) >= request.assignment_max_write_count:
            raise ConnectorDenied("assigned connector write limit is exhausted")
        conn.execute(
            """UPDATE connector_assignment_budgets
               SET write_count=write_count+1,updated_at=?
               WHERE assignment_id=?""",
            (now, request.assignment_id),
        )

    async def _call_adapter(
        self,
        *,
        manifest: ConnectorManifest,
        operation: ConnectorOperation,
        profile: ConnectorDeploymentProfile,
        binding: ConnectorTenantBinding,
        request: ConnectorExecutionRequest,
        payload: dict[str, Any],
    ) -> Any:
        parsed = urlsplit(profile.base_url)
        host = (parsed.hostname or "").casefold()
        allowed_hosts = {
            item.casefold().rstrip(".") for item in profile.allowed_hosts
        }
        if not any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in allowed_hosts
        ):
            raise ConnectorDenied("connector deployment host is outside its allowlist")
        self.harness.enforce_network_egress_policy(
            surface=f"connector:{manifest.connector_id}:{operation.id}",
            hostname=host,
        )
        path_fields = set(re.findall(r"\{([^{}]+)\}", operation.path))
        rendered_path = operation.path
        query: dict[str, Any] = {}
        request_headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        consumed: set[str] = set()
        if operation.parameters:
            for parameter in operation.parameters:
                if parameter.input_key not in payload:
                    if parameter.required:
                        raise ValueError(
                            f"connector operation is missing {parameter.location} parameter: "
                            f"{parameter.wire_name}"
                        )
                    continue
                value = payload[parameter.input_key]
                consumed.add(parameter.input_key)
                if parameter.location == "path":
                    rendered_path = rendered_path.replace(
                        f"{{{parameter.wire_name}}}",
                        quote(str(value), safe=""),
                    )
                elif parameter.location == "query":
                    query[parameter.wire_name] = value
                elif parameter.location == "header":
                    request_headers[parameter.wire_name] = str(value)
                else:
                    cookies[parameter.wire_name] = str(value)
        else:
            missing_path = sorted(path_fields - set(payload))
            if missing_path:
                raise ValueError(f"connector operation path is missing fields: {missing_path}")
            for field in path_fields:
                rendered_path = rendered_path.replace(
                    f"{{{field}}}", quote(str(payload[field]), safe="")
                )
            consumed.update(path_fields)
        url = urljoin(profile.base_url.rstrip("/") + "/", rendered_path.lstrip("/"))
        headers = dict(request_headers)
        headers.update(
            {
                "X-Lilies-Tenant": binding.external_tenant_id,
                "X-Lilies-Actor": request.actor_id,
                "Idempotency-Key": request.idempotency_key,
            }
        )
        if profile.auth_type == "bearer":
            secret = await self._tenant_secret(binding)
            headers["Authorization"] = f"Bearer {secret}"
        elif profile.auth_type == "basic":
            secret = await self._tenant_secret(binding)
            encoded = base64.b64encode(secret.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        elif profile.auth_type == "api_key":
            secret = await self._tenant_secret(binding)
            auth_value = f"{profile.auth_prefix}{secret}"
            if profile.auth_location == "header":
                headers[profile.auth_wire_name] = auth_value
            elif profile.auth_location == "query":
                query[profile.auth_wire_name] = auth_value
            else:
                cookies[profile.auth_wire_name] = auth_value
        if cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={quote(str(value), safe='')}" for name, value in cookies.items()
            )
        body: Any = None
        multipart_files: list[tuple[str, tuple[str | None, bytes, str]]] | None = None
        if operation.request_body:
            consumed.add(operation.request_body.input_key)
            body = payload.get(operation.request_body.input_key)
            if body is None and operation.request_body.required:
                raise ValueError("connector operation is missing required request body")
            if body is not None and operation.request_body.multipart_parts:
                multipart_files = self._multipart_files(
                    body,
                    operation.request_body,
                )
                body = None
            elif not operation.request_body.multipart_parts:
                headers.setdefault("Content-Type", operation.request_body.content_type)
        remaining = {key: value for key, value in payload.items() if key not in consumed}
        if not operation.parameters and not operation.request_body:
            if operation.method == "GET":
                query.update(remaining)
            else:
                body = remaining
        try:
            async with httpx.AsyncClient(timeout=profile.timeout_seconds) as client:
                response = await client.request(
                    operation.method,
                    url,
                    headers=headers,
                    params=query or None,
                    json=body if multipart_files is None else None,
                    files=multipart_files,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ConnectorAdapterError(
                str(error) or type(error).__name__,
                retryable=True,
                side_effect_state="none",
                adapter_called=False,
                retry_safety="pre_dispatch",
            ) from error
        except httpx.TransportError as error:
            raise self._transport_adapter_error(operation, error) from error
        if response.status_code not in operation.success_status_codes:
            expected = ", ".join(str(item) for item in operation.success_status_codes)
            detail = response.text[:1000]
            message = (
                f"connector response status {response.status_code}; expected {expected}; "
                f"body={detail}"
            )
            if response.status_code in operation.retryable_status_codes:
                if self._is_attested_pre_dispatch_failure(
                    manifest=manifest,
                    profile=profile,
                    response=response,
                ):
                    raise ConnectorAdapterError(
                        message,
                        retryable=True,
                        side_effect_state="none",
                        adapter_called=True,
                        retry_safety="pre_dispatch",
                    )
                raise self._retryable_response_error(operation, message)
            raise ConnectorAdapterError(
                message,
                retryable=False,
                side_effect_state=(
                    "unknown" if operation.mutating else "none"
                ),
                adapter_called=True,
                failure_disposition=(
                    "ambiguous" if operation.mutating else "terminal"
                ),
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if operation.response_content_types and content_type:
            if content_type not in operation.response_content_types:
                raise ValueError(
                    f"connector response content-type {content_type!r} is outside contract "
                    f"{operation.response_content_types}"
                )
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        return data

    def _is_attested_pre_dispatch_failure(
        self,
        *,
        manifest: ConnectorManifest,
        profile: ConnectorDeploymentProfile,
        response: httpx.Response,
    ) -> bool:
        identity = f"{manifest.connector_id}@{manifest.version}/{profile.id}"
        attestation = self._pre_dispatch_attestations.get(identity)
        if attestation is None:
            return False
        actual = response.headers.get(attestation.header_name, "")
        return hmac.compare_digest(actual, attestation.header_value)

    @staticmethod
    def _retryable_response_error(
        operation: ConnectorOperation,
        message: str,
    ) -> ConnectorAdapterError:
        if not operation.mutating:
            return ConnectorAdapterError(
                message,
                retryable=True,
                side_effect_state="none",
                adapter_called=True,
                retry_safety="read_only",
            )
        if operation.idempotency_semantics == "request_key":
            return ConnectorAdapterError(
                message,
                retryable=True,
                side_effect_state="unknown",
                adapter_called=True,
                retry_safety="idempotency_key",
            )
        return ConnectorAdapterError(
            message,
            retryable=False,
            side_effect_state="unknown",
            adapter_called=True,
            failure_disposition="ambiguous",
        )

    @staticmethod
    def _transport_adapter_error(
        operation: ConnectorOperation,
        error: httpx.TransportError,
    ) -> ConnectorAdapterError:
        message = str(error) or type(error).__name__
        if not operation.mutating:
            return ConnectorAdapterError(
                message,
                retryable=True,
                side_effect_state="none",
                adapter_called=True,
                retry_safety="read_only",
            )
        if operation.idempotency_semantics == "request_key":
            return ConnectorAdapterError(
                message,
                retryable=True,
                side_effect_state="unknown",
                adapter_called=True,
                retry_safety="idempotency_key",
            )
        return ConnectorAdapterError(
            message,
            retryable=False,
            side_effect_state="unknown",
            adapter_called=True,
            failure_disposition="ambiguous",
        )

    @staticmethod
    def _multipart_files(
        body: Any,
        contract: ConnectorRequestBody,
    ) -> list[tuple[str, tuple[str | None, bytes, str]]]:
        if not isinstance(body, dict):
            raise ValueError("multipart connector request body must be an object")
        declared = {item.input_key: item for item in contract.multipart_parts}
        unknown = sorted(set(body) - set(declared))
        if unknown:
            raise ValueError(f"multipart connector request contains undeclared parts: {unknown}")
        missing = sorted(
            item.input_key
            for item in contract.multipart_parts
            if item.required and item.input_key not in body
        )
        if missing:
            raise ValueError(f"multipart connector request is missing required parts: {missing}")
        files: list[tuple[str, tuple[str | None, bytes, str]]] = []
        total_bytes = 0
        for part in contract.multipart_parts:
            if part.input_key not in body:
                continue
            value = body[part.input_key]
            if part.kind == "text":
                if isinstance(value, bool):
                    text = "true" if value else "false"
                elif isinstance(value, (str, int, float)) and not isinstance(value, complex):
                    text = str(value)
                else:
                    raise ValueError(
                        f"multipart text part {part.wire_name!r} must be a scalar value"
                    )
                raw = text.encode("utf-8")
                content_type = part.content_types[0] if part.content_types else "text/plain"
                files.append((part.wire_name, (None, raw, content_type)))
            else:
                filename, content_type, raw = ConnectorService._decode_blob_part(
                    value,
                    part,
                )
                files.append((part.wire_name, (filename, raw, content_type)))
            total_bytes += len(raw)
            if total_bytes > contract.max_total_bytes:
                raise ValueError(
                    "multipart connector request exceeds the bounded total byte limit "
                    f"{contract.max_total_bytes}"
                )
        return files

    @staticmethod
    def _persistence_safe_payload(
        operation: ConnectorOperation,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request_body = operation.request_body
        if request_body is None or not request_body.multipart_parts:
            return dict(payload)
        body = payload.get(request_body.input_key)
        if not isinstance(body, dict):
            return dict(payload)
        safe_body = dict(body)
        for part in request_body.multipart_parts:
            if part.kind != "blob" or part.input_key not in body:
                continue
            filename, content_type, raw = ConnectorService._decode_blob_part(
                body[part.input_key],
                part,
            )
            safe_body[part.input_key] = {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(raw),
                "sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                "content_redacted": True,
            }
        return {
            **payload,
            request_body.input_key: safe_body,
        }

    @staticmethod
    def _decode_blob_part(
        value: Any,
        part: ConnectorMultipartPart,
    ) -> tuple[str, str, bytes]:
        if not isinstance(value, dict):
            raise ValueError(
                f"multipart blob part {part.wire_name!r} must use the inline blob contract"
            )
        filename = value.get("filename")
        content_type = value.get("content_type")
        content_base64 = value.get("content_base64")
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255:
            raise ValueError("multipart blob filename must contain 1 to 255 characters")
        if any(character in filename for character in ("\x00", "\r", "\n", "/", "\\")):
            raise ValueError("multipart blob filename contains an unsafe character")
        if not isinstance(content_type, str) or not re.fullmatch(
            r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+",
            content_type,
        ):
            raise ValueError("multipart blob content_type is invalid")
        if part.content_types and not any(
            content_type == allowed
            or (
                allowed.endswith("/*")
                and content_type.startswith(f"{allowed[:-1]}")
            )
            for allowed in part.content_types
        ):
            raise ValueError(
                f"multipart blob content_type {content_type!r} is outside the contract"
            )
        if not isinstance(content_base64, str):
            raise ValueError("multipart blob content_base64 must be a string")
        max_encoded = 4 * ((part.max_bytes + 2) // 3)
        if len(content_base64) > max_encoded:
            raise ValueError(
                f"multipart blob exceeds the bounded part byte limit {part.max_bytes}"
            )
        try:
            raw = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("multipart blob content_base64 is invalid") from error
        if len(raw) > part.max_bytes:
            raise ValueError(
                f"multipart blob exceeds the bounded part byte limit {part.max_bytes}"
            )
        expected_digest = value.get("sha256")
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                expected_digest,
            ):
                raise ValueError("multipart blob sha256 is invalid")
            actual_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            if not hmac.compare_digest(expected_digest, actual_digest):
                raise ValueError("multipart blob content does not match sha256")
        return filename, content_type, raw

    @staticmethod
    def validate_operation_response(operation: ConnectorOperation, response: Any) -> None:
        if operation.response_json_schema:
            ConnectorObjectSchema._validate_json_schema(
                response,
                operation.response_json_schema,
                label=f"{operation.id} response",
            )
            return
        operation.response_schema.validate_payload(
            response,
            label=f"{operation.id} response",
        )

    def _finish_execution_sync(
        self,
        execution_id: str,
        response: Any,
        error: str,
        retryable: bool = False,
        side_effect_state: Literal["none", "unknown"] = "unknown",
        adapter_called: bool = True,
        failure_disposition: ConnectorFailureDisposition = "terminal",
        retry_safety: ConnectorRetrySafety = "none",
    ) -> ConnectorExecution:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_json FROM connector_executions WHERE id=?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise KeyError(execution_id)
            current = ConnectorExecution.model_validate_json(row["record_json"])
            if current.status != "executing":
                raise ConnectorConflict("connector execution is no longer running")
            if error:
                saved = current.model_copy(
                    update={
                        "status": "failed",
                        "side_effect_state": side_effect_state,
                        "adapter_called": current.adapter_called or adapter_called,
                        "error": error,
                        "retryable": retryable,
                        "failure_disposition": failure_disposition,
                        "retry_safety": retry_safety,
                        "updated_at": now,
                        "finished_at": now,
                    }
                )
                event_type = "connector.execution.failed"
                event_data = {
                    "error": error,
                    "side_effect_state": saved.side_effect_state,
                    "retryable": saved.retryable,
                    "failure_disposition": saved.failure_disposition,
                    "retry_safety": saved.retry_safety,
                    "attempt": saved.attempt_count,
                }
            else:
                response_object = response if isinstance(response, dict) else {}
                compensation = response_object.get("compensation_payload", {})
                if not isinstance(compensation, dict):
                    compensation = {}
                external_reference = str(
                    response_object.get("external_id")
                    or response_object.get("id")
                    or response_object.get("case_id")
                    or ""
                )
                saved = current.model_copy(
                    update={
                        "status": "succeeded",
                        "side_effect_state": (
                            "applied" if current.operation_kind != "read" else "none"
                        ),
                        "adapter_called": True,
                        "response": response,
                        "response_hash": self.payload_hash(response),
                        "external_reference": external_reference,
                        "compensation_payload": compensation,
                        "retryable": False,
                        "failure_disposition": "none",
                        "retry_safety": "none",
                        "error": "",
                        "updated_at": now,
                        "finished_at": now,
                    }
                )
                event_type = "connector.execution.succeeded"
                event_data = {
                    "response_hash": saved.response_hash,
                    "external_reference": saved.external_reference,
                    "compensation_available": bool(saved.compensation_payload),
                }
            conn.execute(
                """UPDATE connector_executions SET status=?,record_json=?,updated_at=?
                   WHERE id=?""",
                (saved.status, saved.model_dump_json(), now, saved.id),
            )
            self._append_event_sync(
                conn,
                saved.connector_id,
                saved.connector_version,
                saved.tenant_id,
                saved.id,
                event_type,
                event_data,
                now,
            )
            return saved

    async def compensate(
        self,
        execution_id: str,
        *,
        actor_id: str,
        actor_roles: list[str],
        authorization_id: str,
        idempotency_key: str,
    ) -> ConnectorExecution:
        original = await self.get_execution(execution_id)
        manifest = await self.get_manifest(original.connector_id, original.connector_version)
        operation = manifest.operation(original.operation_id)
        if not operation.compensation_operation_id:
            raise ConnectorConflict("connector operation has no compensation contract")
        if original.assignment_id:
            if (
                not original.session_id
                or not original.application_id
                or original.assigned_allowed_network_hosts is None
                or original.assigned_compensation_operations is None
                or original.assignment_max_write_count is None
                or original.assignment_max_payload_bytes is None
            ):
                raise ConnectorDenied(
                    "assigned connector compensation policy is missing"
                )
            self._require_assigned_operation(
                connector_id=original.connector_id,
                operation_id=operation.compensation_operation_id,
                allowed_operations=original.assigned_compensation_operations,
                label="compensation",
            )
        if original.compensation_execution_id:
            compensation = await self.get_execution(
                original.compensation_execution_id
            )
            if (
                original.assignment_id
                and (
                    compensation.assignment_id != original.assignment_id
                    or compensation.session_id != original.session_id
                    or compensation.application_id != original.application_id
                )
            ):
                raise ConnectorDenied(
                    "assigned connector compensation receipt is outside the original scope"
                )
            return compensation
        if original.status != "succeeded" or not original.compensation_payload:
            raise ConnectorConflict("connector execution is not compensation-eligible")
        compensation = await self.execute(
            ConnectorExecutionRequest(
                connector_id=original.connector_id,
                connector_version=original.connector_version,
                tenant_id=original.tenant_id,
                actor_id=actor_id,
                actor_roles=actor_roles,
                profile_id=original.profile_id,
                operation_id=operation.compensation_operation_id,
                payload=original.compensation_payload,
                idempotency_key=idempotency_key,
                authorization_id=authorization_id,
                application_id=original.application_id,
                run_id=original.run_id,
                assignment_id=original.assignment_id,
                session_id=original.session_id,
                allowed_network_hosts=original.assigned_allowed_network_hosts,
                allowed_compensation_operations=(
                    original.assigned_compensation_operations
                ),
                permission_required=bool(original.assignment_id),
                assignment_max_write_count=original.assignment_max_write_count,
                assignment_max_payload_bytes=(
                    original.assignment_max_payload_bytes
                ),
            )
        )
        await asyncio.to_thread(
            self._link_compensation_sync,
            original.id,
            compensation.id,
        )
        return compensation

    def _link_compensation_sync(self, original_id: str, compensation_id: str) -> None:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_json FROM connector_executions WHERE id=?",
                (original_id,),
            ).fetchone()
            if not row:
                raise KeyError(original_id)
            original = ConnectorExecution.model_validate_json(row["record_json"])
            if original.compensation_execution_id and original.compensation_execution_id != compensation_id:
                raise ConnectorConflict("connector execution already has another compensation")
            saved = original.model_copy(
                update={
                    "status": "compensated",
                    "side_effect_state": "compensated",
                    "compensation_execution_id": compensation_id,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE connector_executions SET status=?,record_json=?,updated_at=? WHERE id=?",
                (saved.status, saved.model_dump_json(), now, saved.id),
            )
            self._append_event_sync(
                conn,
                saved.connector_id,
                saved.connector_version,
                saved.tenant_id,
                saved.id,
                "connector.execution.compensated",
                {"compensation_execution_id": compensation_id},
                now,
            )

    async def record_callback(
        self,
        callback: ConnectorCallback,
        *,
        signature: str,
    ) -> ConnectorExecution:
        execution = await self.get_execution(callback.execution_id)
        binding = self._binding(
            execution.connector_id,
            execution.connector_version,
            execution.tenant_id,
        )
        manifest = await self.get_manifest(
            execution.connector_id,
            execution.connector_version,
        )
        if manifest.callback_schema:
            manifest.callback_schema.validate_payload(
                callback.data,
                label="connector callback data",
            )
        secret = await self._tenant_secret(binding)
        expected = self.sign_payload(secret, callback.model_dump(mode="json"))
        if not hmac.compare_digest(expected, signature):
            raise ConnectorDenied("connector callback signature is invalid")
        return await asyncio.to_thread(self._record_callback_sync, callback, execution)

    def _record_callback_sync(
        self,
        callback: ConnectorCallback,
        execution: ConnectorExecution,
    ) -> ConnectorExecution:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT record_json FROM connector_executions WHERE id=?",
                (execution.id,),
            ).fetchone()
            if not row:
                raise KeyError(execution.id)
            current = ConnectorExecution.model_validate_json(row["record_json"])
            if callback.sequence <= current.callback_sequence:
                raise ConnectorConflict("connector callback sequence is stale or replayed")
            try:
                conn.execute(
                    """INSERT INTO connector_callbacks
                       (callback_id,execution_id,sequence,record_json,received_at)
                       VALUES(?,?,?,?,?)""",
                    (
                        callback.callback_id,
                        callback.execution_id,
                        callback.sequence,
                        callback.model_dump_json(),
                        callback.received_at,
                    ),
                )
            except Exception as error:
                if "UNIQUE constraint failed" in str(error):
                    raise ConnectorConflict("connector callback replay rejected") from error
                raise
            saved = current.model_copy(
                update={
                    "callback_sequence": callback.sequence,
                    "callback_status": callback.status,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE connector_executions SET record_json=?,updated_at=? WHERE id=?",
                (saved.model_dump_json(), now, saved.id),
            )
            self._append_event_sync(
                conn,
                saved.connector_id,
                saved.connector_version,
                saved.tenant_id,
                saved.id,
                "connector.callback.accepted",
                {
                    "callback_id": callback.callback_id,
                    "sequence": callback.sequence,
                    "status": callback.status,
                },
                now,
            )
            return saved

    async def run_exercise(
        self,
        *,
        connector_id: str,
        connector_version: int,
        tenant_id: str,
        kind: Literal["emergency_stop", "compensation"],
        execution_id: str = "",
    ) -> ConnectorExercise:
        binding = self._binding(connector_id, connector_version, tenant_id)
        manifest = await self.get_manifest(connector_id, connector_version)
        profile = manifest.profile(binding.profile_id)
        excluded = [
            "customer production readiness",
            "production SLO or incident response",
            "unobserved customer tenants",
        ]
        if profile.environment in {"live", "private"}:
            status: Literal["passed", "failed", "blocked_by_environment"] = (
                "blocked_by_environment"
            )
            evidence_level: Literal["H0", "H3"] = "H0"
            evidence = {"reason": "production exercise telemetry is not configured"}
        elif kind == "emergency_stop":
            policy = await self.get_policy(connector_id, connector_version, tenant_id)
            denial_events = await self.list_events(
                connector_id=connector_id,
                tenant_id=tenant_id,
                limit=1000,
            )
            stop_denials = [
                item
                for item in denial_events
                if item["event_type"] == "connector.execution.denied"
                and "emergency stop" in str(item["data"].get("reason", "")).casefold()
                and item["data"].get("adapter_called") is False
            ]
            status = "passed" if policy.emergency_stop and stop_denials else "failed"
            evidence_level = "H3"
            evidence = {
                "policy_revision": policy.revision,
                "emergency_stop": policy.emergency_stop,
                "denied_attempt_count": len(stop_denials),
                "latest_denied_attempt": stop_denials[-1] if stop_denials else None,
                "adapter_called": False if stop_denials else None,
            }
        else:
            execution = await self.get_execution(execution_id)
            status = (
                "passed"
                if execution.tenant_id == tenant_id
                and execution.status == "compensated"
                and bool(execution.compensation_execution_id)
                else "failed"
            )
            evidence_level = "H3"
            evidence = {
                "execution_id": execution.id,
                "status": execution.status,
                "compensation_execution_id": execution.compensation_execution_id,
            }
        exercise = ConnectorExercise(
            id=f"cexercise-{uuid4()}",
            connector_id=connector_id,
            connector_version=connector_version,
            tenant_id=tenant_id,
            kind=kind,
            profile_id=binding.profile_id,
            status=status,
            evidence_level=evidence_level,
            evidence=evidence,
            excluded_claims=excluded,
        )
        await asyncio.to_thread(self._save_exercise_sync, exercise)
        return exercise

    def _save_exercise_sync(self, exercise: ConnectorExercise) -> None:
        with self.storage._connect() as conn:
            conn.execute(
                """INSERT INTO connector_exercises
                   (id,connector_id,version,tenant_id,record_json,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    exercise.id,
                    exercise.connector_id,
                    exercise.connector_version,
                    exercise.tenant_id,
                    exercise.model_dump_json(),
                    exercise.created_at,
                ),
            )
            self._append_event_sync(
                conn,
                exercise.connector_id,
                exercise.connector_version,
                exercise.tenant_id,
                "",
                "connector.exercise.recorded",
                {
                    "exercise_id": exercise.id,
                    "kind": exercise.kind,
                    "status": exercise.status,
                    "evidence_level": exercise.evidence_level,
                },
                exercise.created_at,
            )

    async def get_execution(self, execution_id: str) -> ConnectorExecution:
        row = await asyncio.to_thread(
            self.storage._get_one,
            "SELECT record_json FROM connector_executions WHERE id=?",
            (execution_id,),
        )
        return ConnectorExecution.model_validate_json(row["record_json"])

    async def list_executions(
        self,
        *,
        connector_id: str | None = None,
        tenant_id: str | None = None,
        application_id: str | None = None,
        run_id: str | None = None,
        operation_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConnectorExecution]:
        clauses: list[str] = []
        values: list[Any] = []
        if connector_id:
            clauses.append("connector_id=?")
            values.append(connector_id)
        if tenant_id:
            clauses.append("tenant_id=?")
            values.append(tenant_id)
        if application_id:
            clauses.append("json_extract(record_json, '$.application_id')=?")
            values.append(application_id)
        if run_id:
            clauses.append("json_extract(record_json, '$.run_id')=?")
            values.append(run_id)
        if operation_id:
            clauses.append("operation_id=?")
            values.append(operation_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((max(1, min(limit, 200)), max(0, offset)))
        rows = await asyncio.to_thread(
            self.storage._get_all,
            f"""SELECT record_json FROM connector_executions {where}
                ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
            tuple(values),
        )
        return [ConnectorExecution.model_validate_json(row["record_json"]) for row in rows]

    async def list_events(
        self,
        *,
        execution_id: str = "",
        connector_id: str = "",
        tenant_id: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if execution_id:
            clauses.append("execution_id=?")
            values.append(execution_id)
        if connector_id:
            clauses.append("connector_id=?")
            values.append(connector_id)
        if tenant_id:
            clauses.append("tenant_id=?")
            values.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((max(1, min(limit, 1000)), max(0, offset)))
        rows = await asyncio.to_thread(
            self.storage._get_all,
            f"""SELECT * FROM connector_audit_events {where}
                ORDER BY sequence ASC LIMIT ? OFFSET ?""",
            tuple(values),
        )
        return [
            {
                "sequence": int(row["sequence"]),
                "connector_id": row["connector_id"],
                "connector_version": int(row["version"]),
                "tenant_id": row["tenant_id"],
                "execution_id": row["execution_id"],
                "event_type": row["event_type"],
                "data": json.loads(row["data_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_exercises(
        self,
        *,
        connector_id: str | None = None,
        tenant_id: str | None = None,
        application_id: str | None = None,
    ) -> list[ConnectorExercise]:
        clauses: list[str] = []
        values: list[Any] = []
        if connector_id:
            clauses.append("connector_id=?")
            values.append(connector_id)
        if tenant_id:
            clauses.append("tenant_id=?")
            values.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await asyncio.to_thread(
            self.storage._get_all,
            f"SELECT record_json FROM connector_exercises {where} ORDER BY created_at DESC,id DESC",
            tuple(values),
        )
        items = [ConnectorExercise.model_validate_json(row["record_json"]) for row in rows]
        if application_id:
            bindings = await self.list_bindings(application_id=application_id)
            allowed = {
                (item.connector_id, item.connector_version, item.tenant_id)
                for item in bindings
            }
            items = [
                item for item in items
                if (item.connector_id, item.connector_version, item.tenant_id) in allowed
            ]
        return items

    def contract_availability(self, connector_id: str, version: int) -> dict[str, Any]:
        manifest = self._manifests.get((connector_id, version))
        bindings = [
            item
            for item in self._bindings.values()
            if item.connector_id == connector_id
            and item.connector_version == version
            and item.enabled
        ]
        profiles = (
            [item for item in manifest.deployment_profiles if item.available]
            if manifest
            else []
        )
        available = bool(manifest and bindings and profiles)
        return {
            "available": available,
            "manifest": bool(manifest),
            "tenant_bindings": len(bindings),
            "available_profiles": [item.id for item in profiles],
            "claim_ceiling": max(
                (item.claim_ceiling for item in profiles),
                default="H0",
            ),
        }

    def controlled_test_identity(self, application_id: str) -> ConnectorIdentityContext:
        candidates: list[tuple[ConnectorTenantBinding, ConnectorDeploymentProfile]] = []
        for binding in self._bindings.values():
            if not binding.enabled or application_id not in binding.application_ids:
                continue
            manifest = self._manifests.get(
                (binding.connector_id, binding.connector_version)
            )
            if manifest is None:
                continue
            profile = manifest.profile(binding.profile_id)
            if profile.available and profile.environment in {"mock", "test"}:
                candidates.append((binding, profile))
        if not candidates:
            raise ConnectorDenied(
                "application has no eligible controlled mock/test Connector tenant"
            )
        binding, _ = sorted(
            candidates,
            key=lambda item: (
                item[0].connector_id,
                item[0].connector_version,
                item[0].tenant_id,
            ),
        )[0]
        subject = binding.subjects[0]
        return ConnectorIdentityContext(
            connector_id=binding.connector_id,
            connector_version=binding.connector_version,
            tenant_id=binding.tenant_id,
            actor_id=subject.actor_id,
            actor_roles=list(subject.roles),
            profile_id=binding.profile_id,
            application_id=application_id,
        )

    def _binding(
        self,
        connector_id: str,
        connector_version: int,
        tenant_id: str,
    ) -> ConnectorTenantBinding:
        try:
            return self._bindings[(connector_id, connector_version, tenant_id)]
        except KeyError as error:
            raise KeyError(
                f"unknown connector tenant binding: {connector_id}@{connector_version}:{tenant_id}"
            ) from error

    def _binding_by_external(
        self,
        connector_id: str,
        connector_version: int,
        external_tenant_id: str,
    ) -> ConnectorTenantBinding:
        binding = next(
            (
                item
                for item in self._bindings.values()
                if item.connector_id == connector_id
                and item.connector_version == connector_version
                and item.external_tenant_id == external_tenant_id
            ),
            None,
        )
        if binding is None:
            raise ConnectorDenied("external tenant is not registered")
        return binding

    async def _tenant_secret(self, binding: ConnectorTenantBinding) -> str:
        resolved = await self.harness.inject_secret_references(
            owner_id=binding.tenant_id,
            payload={"secret_ref": binding.secret_ref},
        )
        if not isinstance(resolved, str):
            raise ConnectorDenied("connector tenant secret reference did not resolve")
        return resolved

    @staticmethod
    def canonical_json(payload: Any) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def payload_hash(cls, payload: Any) -> str:
        return hashlib.sha256(cls.canonical_json(payload).encode()).hexdigest()

    @classmethod
    def sign_payload(cls, secret: str, payload: Any) -> str:
        return hmac.new(
            secret.encode(),
            cls.canonical_json(payload).encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def execution_id(
        connector_id: str,
        connector_version: int,
        tenant_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> str:
        identity = (
            f"{connector_id}:{connector_version}:{tenant_id}:{operation_id}:{idempotency_key}"
        )
        return "cexec-" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _append_event_sync(
        conn: Any,
        connector_id: str,
        version: int,
        tenant_id: str,
        execution_id: str,
        event_type: str,
        data: dict[str, Any],
        created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO connector_audit_events
               (connector_id,version,tenant_id,execution_id,event_type,data_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                connector_id,
                version,
                tenant_id,
                execution_id,
                event_type,
                json.dumps(data),
                created_at,
            ),
        )
