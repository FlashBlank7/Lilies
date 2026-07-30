from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import socket
import time
from typing import Any, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
import httpcore
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .connector_sdk import (
    MAX_CONNECTOR_BLOB_BYTES,
    MAX_CONNECTOR_MULTIPART_PARTS,
    MAX_CONNECTOR_OPERATION_PARAMETERS,
    MAX_CONNECTOR_SCHEMA_FIELDS,
    ConnectorDeploymentProfile,
    ConnectorExecutionRequest,
    ConnectorManifest,
    ConnectorMultipartPart,
    ConnectorObjectSchema,
    ConnectorOperation,
    ConnectorParameterBinding,
    ConnectorRequestBody,
    ConnectorSchemaField,
    ConnectorSecurityScheme,
    ConnectorService,
    ConnectorTenantBinding,
)
from .models import utc_now
from .platform_harness import PlatformHarness
from .storage import Storage


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
MAX_OPENAPI_BYTES = 5_000_000
MAX_OPENAPI_SCHEMA_OVERLAY_ACTIONS = 100
MAX_OPENAPI_SCHEMA_OVERLAY_BYTES = 64_000
MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_BYTES = 4_000
MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_SEGMENTS = 128
MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS = 100
MAX_OPENAPI_OPERATION_CONTRACT_OVERLAY_BYTES = 64_000
MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAYS = 100
MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAY_BYTES = 64_000


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect an HTTP origin only through its prevalidated DNS answers."""

    def __init__(
        self,
        host: str,
        port: int,
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
    ) -> None:
        self.host = host.casefold().rstrip(".")
        self.port = port
        self.addresses = tuple(sorted(addresses, key=lambda item: (item.version, str(item))))
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        normalized = host.casefold().rstrip(".")
        if normalized != self.host or port != self.port:
            raise httpcore.ConnectError("pinned OpenAPI origin changed during connection")
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                return await self.backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("pinned OpenAPI origin has no validated address")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("OpenAPI document fetch does not allow Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class OpenAPICapabilityGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^IF-(0[1-9]|1[0-4])$")
    capability: str
    location: str
    message: str
    fatal: bool = False


class OpenAPISchemaOverlayAction(BaseModel):
    """One explicit JSON-Patch-like mutation inside an OpenAPI schema subtree."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "replace", "remove"]
    path: str = Field(min_length=1, max_length=MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_BYTES)
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value_presence(self) -> OpenAPISchemaOverlayAction:
        has_value = "value" in self.model_fields_set
        if len(self.path.encode("utf-8")) > MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_BYTES:
            raise ValueError(
                "schema overlay JSON Pointer exceeds "
                f"{MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_BYTES} bytes"
            )
        if self.op == "remove" and has_value:
            raise ValueError("remove schema overlay actions must not include value")
        if self.op != "remove" and not has_value:
            raise ValueError(f"{self.op} schema overlay actions require value")
        return self


class OpenAPISchemaOverlayProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    action_count: int = Field(default=0, ge=0, le=MAX_OPENAPI_SCHEMA_OVERLAY_ACTIONS)
    effective_contract_digest: str = Field(
        default="",
        pattern=r"^(|sha256:[0-9a-f]{64})$",
    )


class OpenAPIOperationContractOverlay(BaseModel):
    """Bounded contract corrections addressed by an existing live-spec operationId."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=300)
    request_body_schema: dict[str, Any] | None = None
    request_body_required: Literal[True] | None = None
    response_schemas: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        max_length=100,
    )

    @field_validator("request_body_required", mode="before")
    @classmethod
    def request_required_must_be_explicit_true(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("request_body_required may only be set to true")
        return value

    @model_validator(mode="after")
    def validate_contract_schemas(self) -> OpenAPIOperationContractOverlay:
        request_schema_present = "request_body_schema" in self.model_fields_set
        if request_schema_present and self.request_body_schema is None:
            raise ValueError("request_body_schema must be a JSON Schema object")
        if self.request_body_required is True and not request_schema_present:
            raise ValueError(
                "request_body_required=true requires request_body_schema"
            )
        if not request_schema_present and not self.response_schemas:
            raise ValueError("operation contract overlay requires a request or response schema")
        for status in self.response_schemas:
            if re.fullmatch(r"2[0-9]{2}", status) is None:
                raise ValueError(
                    "operation contract response schema keys must be explicit 2xx statuses"
                )
        return self


class OpenAPIOperationContractOverlayProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    operation_count: int = Field(
        default=0,
        ge=0,
        le=MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS,
    )
    request_schema_count: int = Field(default=0, ge=0)
    request_body_required_count: int = Field(
        default=0,
        ge=0,
        le=MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS,
    )
    response_schema_count: int = Field(default=0, ge=0)
    effective_contract_digest: str = Field(
        default="",
        pattern=r"^(|sha256:[0-9a-f]{64})$",
    )


class OpenAPIOperationSemanticsOverlay(BaseModel):
    """Governed operation semantics for existing official operationIds."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=300)
    kind: Literal["compensate"] | None = None
    compensation_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
    )

    @model_validator(mode="after")
    def exactly_one_semantic_declaration(
        self,
    ) -> OpenAPIOperationSemanticsOverlay:
        kind_present = "kind" in self.model_fields_set and self.kind is not None
        compensation_present = (
            "compensation_operation_id" in self.model_fields_set
            and self.compensation_operation_id is not None
        )
        if kind_present == compensation_present:
            raise ValueError(
                "operation semantics overlay requires exactly one of "
                "kind or compensation_operation_id"
            )
        return self


class OpenAPIOperationSemanticsOverlayProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    operation_count: int = Field(
        default=0,
        ge=0,
        le=MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAYS,
    )
    compensate_count: int = Field(default=0, ge=0)
    compensation_binding_count: int = Field(default=0, ge=0)
    effective_manifest_digest: str = Field(
        default="",
        pattern=r"^(|sha256:[0-9a-f]{64})$",
    )


class OpenAPISourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["inline", "url"]
    source_url: str = ""
    source_digest: str
    openapi_version: str
    title: str
    document_version: str
    size_bytes: int
    schema_overlay: OpenAPISchemaOverlayProvenance = Field(
        default_factory=OpenAPISchemaOverlayProvenance
    )
    operation_contract_overlay: OpenAPIOperationContractOverlayProvenance = Field(
        default_factory=OpenAPIOperationContractOverlayProvenance
    )
    operation_semantics_overlay: OpenAPIOperationSemanticsOverlayProvenance = Field(
        default_factory=OpenAPIOperationSemanticsOverlayProvenance
    )
    fetched_at: str = Field(default_factory=utc_now)


class OpenAPIDeploymentChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(default="generated-test", pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    environment: Literal["mock", "test", "live", "private"] = "test"
    base_url: str = Field(min_length=1, max_length=1000)
    allowed_hosts: list[str] = Field(min_length=1, max_length=100)
    available: bool = True
    timeout_seconds: float = Field(default=20, ge=1, le=300)
    claim_ceiling: Literal["H2", "H3", "H4", "H5"] = "H3"
    auth_scheme_id: str = ""
    auth_prefix: str = ""


class OpenAPIConnectorGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    version: int = Field(default=1, ge=1)
    domain: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    deployment: OpenAPIDeploymentChoice
    document: str = ""
    document_url: str = Field(default="", max_length=2000)
    allowed_document_hosts: list[str] = Field(default_factory=list, max_length=30)
    allow_insecure_document_http: bool = False
    include_operation_ids: list[str] = Field(default_factory=list, max_length=1_000)
    exclude_operation_ids: list[str] = Field(default_factory=list, max_length=1_000)
    schema_overlay: list[OpenAPISchemaOverlayAction] = Field(
        default_factory=list,
        max_length=MAX_OPENAPI_SCHEMA_OVERLAY_ACTIONS,
    )
    operation_contract_overlays: list[OpenAPIOperationContractOverlay] = Field(
        default_factory=list,
        max_length=MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS,
    )
    operation_semantics_overlays: list[OpenAPIOperationSemanticsOverlay] = Field(
        default_factory=list,
        max_length=MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAYS,
    )

    @model_validator(mode="after")
    def exactly_one_source(self) -> OpenAPIConnectorGenerationRequest:
        if bool(self.document) == bool(self.document_url):
            raise ValueError("provide exactly one of document or document_url")
        if self.include_operation_ids and self.exclude_operation_ids:
            raise ValueError(
                "include_operation_ids and exclude_operation_ids are mutually exclusive"
            )
        for label, values in (
            ("include_operation_ids", self.include_operation_ids),
            ("exclude_operation_ids", self.exclude_operation_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} contains duplicate operationId values")
            if any(not value or len(value) > 300 for value in values):
                raise ValueError(f"{label} values must contain 1 to 300 characters")
        overlay_paths = [action.path for action in self.schema_overlay]
        if len(overlay_paths) != len(set(overlay_paths)):
            raise ValueError("schema_overlay contains duplicate JSON Pointer paths")
        try:
            overlay_bytes = json.dumps(
                [
                    action.model_dump(mode="json", exclude_unset=True)
                    for action in self.schema_overlay
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("schema_overlay values must be finite JSON values") from error
        if len(overlay_bytes) > MAX_OPENAPI_SCHEMA_OVERLAY_BYTES:
            raise ValueError(f"schema_overlay exceeds {MAX_OPENAPI_SCHEMA_OVERLAY_BYTES} bytes")
        operation_ids = [overlay.operation_id for overlay in self.operation_contract_overlays]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_contract_overlays contains duplicate operationId values")
        try:
            operation_overlay_bytes = json.dumps(
                [
                    overlay.model_dump(mode="json", exclude_unset=True)
                    for overlay in sorted(
                        self.operation_contract_overlays,
                        key=lambda item: item.operation_id,
                    )
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError(
                "operation_contract_overlays must contain finite JSON values"
            ) from error
        if len(operation_overlay_bytes) > MAX_OPENAPI_OPERATION_CONTRACT_OVERLAY_BYTES:
            raise ValueError(
                "operation_contract_overlays exceeds "
                f"{MAX_OPENAPI_OPERATION_CONTRACT_OVERLAY_BYTES} bytes"
            )
        semantics_operation_ids = [
            overlay.operation_id for overlay in self.operation_semantics_overlays
        ]
        if len(semantics_operation_ids) != len(set(semantics_operation_ids)):
            raise ValueError(
                "operation_semantics_overlays contains duplicate operationId values"
            )
        semantics_overlay_bytes = json.dumps(
            [
                overlay.model_dump(mode="json", exclude_unset=True)
                for overlay in sorted(
                    self.operation_semantics_overlays,
                    key=lambda item: item.operation_id,
                )
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            len(semantics_overlay_bytes)
            > MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAY_BYTES
        ):
            raise ValueError(
                "operation_semantics_overlays exceeds "
                f"{MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAY_BYTES} bytes"
            )
        return self


class OpenAPIOperationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["all", "include", "exclude"] = "all"
    requested_operation_ids: list[str] = Field(default_factory=list, max_length=1_000)
    generated_operation_ids: list[str] = Field(default_factory=list, max_length=1_000)


class OpenAPIConnectorGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    version: int
    status: Literal["generated", "verified", "registered"] = "generated"
    provenance: OpenAPISourceProvenance
    request_fingerprint: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    operation_selection: OpenAPIOperationSelection = Field(
        default_factory=OpenAPIOperationSelection
    )
    manifest: ConnectorManifest
    gaps: list[OpenAPICapabilityGap] = Field(default_factory=list)
    discovered_operation_count: int
    generated_operation_count: int
    mapped_field_count: int
    total_field_count: int
    parse_ms: float
    generate_ms: float
    created_at: str = Field(default_factory=utc_now)
    evidence_stale: bool = False


class ConnectorContractCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    operation_id: str
    kind: Literal["positive", "negative"]
    expected: str
    generated_input: dict[str, Any]


class ConnectorContractCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case: ConnectorContractCase
    status: Literal["passed", "failed", "skipped", "unsupported", "blocked_by_environment"]
    actual: str
    executed_input_evidence: dict[str, Any] = Field(default_factory=dict)
    response_evidence: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0


class ConnectorContractRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_ids: list[str] = Field(default_factory=list, max_length=100)
    sample_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    owner_id: str = "contract-test"
    secret_ref: str = ""
    external_tenant_id: str = "contract-test"
    allow_mutating_operations: bool = False


class ConnectorContractRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    generation_id: str
    source_digest: str
    overlay_digest: str = Field(default="", pattern=r"^(|sha256:[0-9a-f]{64})$")
    effective_contract_digest: str = Field(
        default="",
        pattern=r"^(|sha256:[0-9a-f]{64})$",
    )
    operation_contract_overlay: OpenAPIOperationContractOverlayProvenance = Field(
        default_factory=OpenAPIOperationContractOverlayProvenance
    )
    operation_semantics_overlay: OpenAPIOperationSemanticsOverlayProvenance = Field(
        default_factory=OpenAPIOperationSemanticsOverlayProvenance
    )
    status: Literal["passed", "failed", "partial", "blocked_by_environment"]
    results: list[ConnectorContractCaseResult]
    passed: int
    failed: int
    skipped: int
    unsupported: int
    blocked_by_environment: int
    attempts: int
    test_ms: float
    time_to_first_valid_contract_ms: float | None = None
    created_at: str = Field(default_factory=utc_now)


class OpenAPIMaterialError(ValueError):
    def __init__(self, gap: OpenAPICapabilityGap) -> None:
        super().__init__(gap.message)
        self.gap = gap


class OpenAPIOperationSemanticsOverlayReconciler:
    """Validate semantics without changing the live OpenAPI document."""

    def reconcile(
        self,
        document: dict[str, Any],
        overlays: list[OpenAPIOperationSemanticsOverlay],
    ) -> OpenAPIOperationSemanticsOverlayProvenance:
        ordered = sorted(overlays, key=lambda item: item.operation_id)
        serialized = self._canonical_json(
            [item.model_dump(mode="json", exclude_unset=True) for item in ordered]
        )
        if len(serialized) > MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAY_BYTES:
            raise self._error(
                "operation semantics overlays exceed "
                f"{MAX_OPENAPI_OPERATION_SEMANTICS_OVERLAY_BYTES} bytes"
            )
        operations = self._operation_index(document)
        by_overlay_id = {item.operation_id: item for item in ordered}
        primary_writes_by_compensation: dict[str, list[str]] = {}
        compensate_count = 0
        compensation_binding_count = 0
        for index, overlay in enumerate(ordered):
            location = f"$.operation_semantics_overlays[{index}]"
            method = self._unique_method(
                operations,
                overlay.operation_id,
                f"{location}.operation_id",
            )
            if overlay.kind == "compensate":
                if method != "delete":
                    raise self._error(
                        "kind=compensate may only be declared for an existing "
                        "DELETE operation",
                        location=f"{location}.kind",
                    )
                compensate_count += 1
                continue
            target_id = overlay.compensation_operation_id
            if target_id is None:
                raise self._error(
                    "compensation_operation_id must be present",
                    location=f"{location}.compensation_operation_id",
                )
            if method == "get":
                raise self._error(
                    "compensation_operation_id may only be bound to a write operation",
                    location=f"{location}.compensation_operation_id",
                )
            if target_id == overlay.operation_id:
                raise self._error(
                    "a write operation cannot compensate itself",
                    location=f"{location}.compensation_operation_id",
                )
            target_method = self._unique_method(
                operations,
                target_id,
                f"{location}.compensation_operation_id",
            )
            if target_method != "delete":
                raise self._error(
                    "compensation_operation_id must reference an existing DELETE operation",
                    location=f"{location}.compensation_operation_id",
                )
            target_overlay = by_overlay_id.get(target_id)
            if target_overlay is None or target_overlay.kind != "compensate":
                raise self._error(
                    "compensation_operation_id must reference an operation declared "
                    "kind=compensate in the same overlay",
                    location=f"{location}.compensation_operation_id",
                )
            primary_writes_by_compensation.setdefault(target_id, []).append(
                overlay.operation_id
            )
            compensation_binding_count += 1
        conflicting_targets = sorted(
            target_id
            for target_id, primary_ids in primary_writes_by_compensation.items()
            if len(primary_ids) > 1
        )
        if conflicting_targets:
            raise self._error(
                "one compensation operation cannot be bound to multiple primary "
                f"write operations: {conflicting_targets}"
            )
        return OpenAPIOperationSemanticsOverlayProvenance(
            overlay_digest=f"sha256:{hashlib.sha256(serialized).hexdigest()}",
            operation_count=len(ordered),
            compensate_count=compensate_count,
            compensation_binding_count=compensation_binding_count,
        )

    @staticmethod
    def _operation_index(
        document: dict[str, Any],
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        paths = document.get("paths", {})
        if not isinstance(paths, dict):
            return result
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(operation, dict):
                    continue
                operation_id = operation.get("operationId")
                if isinstance(operation_id, str) and operation_id:
                    result.setdefault(operation_id, []).append(method)
        return result

    def _unique_method(
        self,
        operations: dict[str, list[str]],
        operation_id: str,
        location: str,
    ) -> str:
        methods = operations.get(operation_id, [])
        if not methods:
            raise self._error(
                "operation semantics overlay must reference an operationId that "
                "exists in the live OpenAPI document",
                location=location,
            )
        if len(methods) != 1:
            raise self._error(
                "operation semantics overlay operationId is ambiguous in the "
                "live OpenAPI document",
                location=location,
            )
        return methods[0]

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _error(
        message: str,
        *,
        location: str = "$.operation_semantics_overlays",
    ) -> OpenAPIMaterialError:
        return OpenAPIMaterialError(
            OpenAPICapabilityGap(
                code="IF-04",
                capability="operation_semantics_overlay",
                location=location,
                message=message,
                fatal=True,
            )
        )


class OpenAPIOperationContractOverlayReconciler:
    """Apply operation-addressed schemas without changing the live API surface."""

    def reconcile(
        self,
        document: dict[str, Any],
        overlays: list[OpenAPIOperationContractOverlay],
    ) -> tuple[dict[str, Any], OpenAPIOperationContractOverlayProvenance]:
        ordered = sorted(overlays, key=lambda item: item.operation_id)
        serialized_overlays = self._canonical_json(
            [overlay.model_dump(mode="json", exclude_unset=True) for overlay in ordered],
            "$.operation_contract_overlays",
        )
        if len(serialized_overlays) > MAX_OPENAPI_OPERATION_CONTRACT_OVERLAY_BYTES:
            raise self._error(
                "$.operation_contract_overlays",
                "operation contract overlays exceed "
                f"{MAX_OPENAPI_OPERATION_CONTRACT_OVERLAY_BYTES} bytes",
            )
        effective = copy.deepcopy(document)
        immutable_surface = self._immutable_surface(effective)
        operations = self._operation_index(effective)
        request_schema_count = 0
        request_body_required_count = 0
        response_schema_count = 0
        for index, overlay in enumerate(ordered):
            location = f"$.operation_contract_overlays[{index}]"
            candidates = operations.get(overlay.operation_id, [])
            if not candidates:
                raise self._error(
                    f"{location}.operation_id",
                    "operation contract overlay must reference an operationId "
                    "that exists in the live OpenAPI document",
                )
            if len(candidates) != 1:
                raise self._error(
                    f"{location}.operation_id",
                    "operation contract overlay operationId is ambiguous in the "
                    "live OpenAPI document",
                )
            operation = candidates[0]
            if overlay.request_body_schema is not None:
                self._set_request_schema(
                    operation,
                    overlay.request_body_schema,
                    f"{location}.request_body_schema",
                    required=overlay.request_body_required is True,
                )
                request_schema_count += 1
                request_body_required_count += int(
                    overlay.request_body_required is True
                )
            for status, schema in sorted(overlay.response_schemas.items()):
                self._set_response_schema(
                    operation,
                    status,
                    schema,
                    f"{location}.response_schemas.{status}",
                )
                response_schema_count += 1
        if self._immutable_surface(effective) != immutable_surface:
            raise self._error(
                "$.operation_contract_overlays",
                "operation contract overlays must not change paths, methods, "
                "operationIds, security, servers, authentication, or response statuses",
            )
        effective_bytes = self._canonical_json(
            effective,
            "$.operation_contract_overlays",
        )
        return effective, OpenAPIOperationContractOverlayProvenance(
            overlay_digest=(f"sha256:{hashlib.sha256(serialized_overlays).hexdigest()}"),
            operation_count=len(ordered),
            request_schema_count=request_schema_count,
            request_body_required_count=request_body_required_count,
            response_schema_count=response_schema_count,
            effective_contract_digest=(f"sha256:{hashlib.sha256(effective_bytes).hexdigest()}"),
        )

    @staticmethod
    def _operation_index(
        document: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        paths = document.get("paths", {})
        if not isinstance(paths, dict):
            return result
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(operation, dict):
                    continue
                operation_id = operation.get("operationId")
                if isinstance(operation_id, str) and operation_id:
                    result.setdefault(operation_id, []).append(operation)
        return result

    def _set_request_schema(
        self,
        operation: dict[str, Any],
        schema: dict[str, Any],
        location: str,
        *,
        required: bool,
    ) -> None:
        if "requestBody" not in operation:
            operation["requestBody"] = {
                "content": {"application/json": {"schema": copy.deepcopy(schema)}}
            }
            if required:
                operation["requestBody"]["required"] = True
            return
        request_body = operation["requestBody"]
        if not isinstance(request_body, dict) or "$ref" in request_body:
            raise self._error(
                location,
                "requestBody must be an inline object before its JSON schema can be overlaid",
            )
        content = request_body.get("content")
        if content is None:
            content = {}
            request_body["content"] = content
        if not isinstance(content, dict):
            raise self._error(location, "requestBody content must be an object")
        media = content.get("application/json")
        if media is None:
            media = {}
            content["application/json"] = media
        if not isinstance(media, dict):
            raise self._error(
                location,
                "requestBody application/json media contract must be an object",
            )
        media["schema"] = copy.deepcopy(schema)
        if required:
            request_body["required"] = True

    def _set_response_schema(
        self,
        operation: dict[str, Any],
        status: str,
        schema: dict[str, Any],
        location: str,
    ) -> None:
        if status in {"204", "205"}:
            raise self._error(
                location,
                f"HTTP {status} cannot carry a JSON response schema",
            )
        responses = operation.get("responses")
        if not isinstance(responses, dict):
            raise self._error(location, "operation responses must be an object")
        matching_keys = [key for key in responses if str(key) == status]
        if len(matching_keys) != 1:
            raise self._error(
                location,
                "response schema overlay status must already exist exactly once "
                "in the live operation",
            )
        response = responses[matching_keys[0]]
        if not isinstance(response, dict) or "$ref" in response:
            raise self._error(
                location,
                "response must be an inline object before its JSON schema can be overlaid",
            )
        content = response.get("content")
        if content is None:
            content = {}
            response["content"] = content
        if not isinstance(content, dict):
            raise self._error(location, "response content must be an object")
        media = content.get("application/json")
        if media is None:
            media = {}
            content["application/json"] = media
        if not isinstance(media, dict):
            raise self._error(
                location,
                "response application/json media contract must be an object",
            )
        media["schema"] = copy.deepcopy(schema)

    def _immutable_surface(self, document: dict[str, Any]) -> bytes:
        paths = document.get("paths", {})
        path_controls: list[dict[str, Any]] = []
        if isinstance(paths, dict):
            for path in sorted(paths, key=str):
                path_item = paths[path]
                if not isinstance(path_item, dict):
                    path_controls.append(
                        {"path": str(path), "path_item_type": type(path_item).__name__}
                    )
                    continue
                methods: list[dict[str, Any]] = []
                for method in sorted(
                    (key for key in path_item if key in HTTP_METHODS),
                    key=str,
                ):
                    operation = path_item[method]
                    if not isinstance(operation, dict):
                        methods.append(
                            {
                                "method": method,
                                "operation_type": type(operation).__name__,
                            }
                        )
                        continue
                    responses = operation.get("responses")
                    statuses = (
                        sorted(str(status) for status in responses)
                        if isinstance(responses, dict)
                        else None
                    )
                    methods.append(
                        {
                            "method": method,
                            "operation_id": operation.get("operationId"),
                            "security": operation.get("security"),
                            "servers": operation.get("servers"),
                            "response_statuses": statuses,
                        }
                    )
                path_controls.append(
                    {
                        "path": str(path),
                        "servers": path_item.get("servers"),
                        "methods": methods,
                    }
                )
        components = document.get("components")
        security_schemes = (
            components.get("securitySchemes") if isinstance(components, dict) else None
        )
        return self._canonical_json(
            {
                "servers": document.get("servers"),
                "security": document.get("security"),
                "security_schemes": security_schemes,
                "paths": path_controls,
            },
            "$.operation_contract_overlays",
        )

    def _canonical_json(self, value: Any, location: str) -> bytes:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise self._error(
                location,
                "operation contract overlays and the effective OpenAPI contract "
                "must contain only finite JSON values",
            ) from error

    @staticmethod
    def _error(location: str, message: str) -> OpenAPIMaterialError:
        return OpenAPIMaterialError(
            OpenAPICapabilityGap(
                code="IF-01",
                capability="operation_contract_overlay",
                location=location,
                message=message,
                fatal=True,
            )
        )


class OpenAPISchemaOverlayReconciler:
    """Apply bounded schema-only corrections without mutating the source material."""

    def reconcile(
        self,
        document: dict[str, Any],
        actions: list[OpenAPISchemaOverlayAction],
    ) -> tuple[dict[str, Any], OpenAPISchemaOverlayProvenance]:
        serialized_actions = json.dumps(
            [
                action.model_dump(mode="json", exclude_unset=True)
                for action in actions
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(serialized_actions) > MAX_OPENAPI_SCHEMA_OVERLAY_BYTES:
            raise self._error(
                "$.schema_overlay",
                f"schema overlay exceeds {MAX_OPENAPI_SCHEMA_OVERLAY_BYTES} bytes",
            )
        effective = copy.deepcopy(document)
        for index, action in enumerate(actions):
            location = f"$.schema_overlay[{index}].path"
            segments = self._pointer_segments(action.path, location)
            if self._schema_boundary(segments) is None:
                raise self._error(
                    location,
                    "schema overlay target must be inside a request or response schema "
                    "or an OpenAPI components schema contract",
                )
            self._apply_action(effective, action, segments, location)
        effective_bytes = self._canonical_document(effective)
        return effective, OpenAPISchemaOverlayProvenance(
            overlay_digest=f"sha256:{hashlib.sha256(serialized_actions).hexdigest()}",
            action_count=len(actions),
            effective_contract_digest=(
                f"sha256:{hashlib.sha256(effective_bytes).hexdigest()}"
            ),
        )

    def _pointer_segments(self, pointer: str, location: str) -> list[str]:
        if not pointer.startswith("/") or pointer == "/":
            raise self._error(location, "schema overlay path must be an absolute JSON Pointer")
        raw_segments = pointer[1:].split("/")
        if len(raw_segments) > MAX_OPENAPI_SCHEMA_OVERLAY_POINTER_SEGMENTS:
            raise self._error(
                location,
                "schema overlay JSON Pointer has too many segments",
            )
        segments: list[str] = []
        for raw in raw_segments:
            cursor = 0
            while cursor < len(raw):
                if raw[cursor] != "~":
                    cursor += 1
                    continue
                if cursor + 1 >= len(raw) or raw[cursor + 1] not in {"0", "1"}:
                    raise self._error(
                        location,
                        "schema overlay path contains an invalid JSON Pointer escape",
                    )
                cursor += 2
            segments.append(raw.replace("~1", "/").replace("~0", "~"))
        return segments

    def _schema_boundary(self, segments: list[str]) -> int | None:
        if len(segments) >= 3 and segments[:2] == ["components", "schemas"]:
            return 3
        if len(segments) >= 4 and segments[0] == "components":
            if segments[1] in {"headers", "parameters"} and segments[3] == "schema":
                return 4
            if (
                segments[1] == "requestBodies"
                and len(segments) >= 6
                and segments[3] == "content"
                and segments[5] == "schema"
            ):
                return 6
            if segments[1] == "responses" and len(segments) >= 6:
                if segments[3] == "content" and segments[5] == "schema":
                    return 6
                if segments[3] == "headers" and segments[5] == "schema":
                    return 6
            return None
        if len(segments) < 4 or segments[0] != "paths":
            return None
        if (
            segments[2] == "parameters"
            and len(segments) >= 5
            and segments[4] == "schema"
        ):
            return 5
        if segments[2].casefold() not in HTTP_METHODS:
            return None
        if (
            segments[3] == "parameters"
            and len(segments) >= 6
            and segments[5] == "schema"
        ):
            return 6
        if (
            segments[3] == "requestBody"
            and len(segments) >= 7
            and segments[4] == "content"
            and segments[6] == "schema"
        ):
            return 7
        if segments[3] == "responses" and len(segments) >= 8:
            if segments[5] == "content" and segments[7] == "schema":
                return 8
            if segments[5] == "headers" and segments[7] == "schema":
                return 8
        return None

    def _apply_action(
        self,
        document: dict[str, Any],
        action: OpenAPISchemaOverlayAction,
        segments: list[str],
        location: str,
    ) -> None:
        parent: Any = document
        for segment in segments[:-1]:
            parent = self._descend(parent, segment, location)
        target = segments[-1]
        if isinstance(parent, dict):
            exists = target in parent
            if action.op == "add":
                if exists:
                    raise self._error(
                        location,
                        "schema overlay add target already exists; use replace explicitly",
                    )
                parent[target] = copy.deepcopy(action.value)
                return
            if not exists:
                raise self._error(location, "schema overlay target does not exist")
            if action.op == "replace":
                parent[target] = copy.deepcopy(action.value)
            else:
                del parent[target]
            return
        if isinstance(parent, list):
            if action.op == "add" and target == "-":
                parent.append(copy.deepcopy(action.value))
                return
            index = self._list_index(target, location)
            upper_bound = len(parent) if action.op == "add" else len(parent) - 1
            if index < 0 or index > upper_bound:
                raise self._error(location, "schema overlay list index is out of range")
            if action.op == "add":
                parent.insert(index, copy.deepcopy(action.value))
            elif action.op == "replace":
                parent[index] = copy.deepcopy(action.value)
            else:
                del parent[index]
            return
        raise self._error(location, "schema overlay parent is not a JSON object or array")

    def _descend(self, value: Any, segment: str, location: str) -> Any:
        if isinstance(value, dict):
            if segment not in value:
                raise self._error(location, "schema overlay parent path does not exist")
            return value[segment]
        if isinstance(value, list):
            index = self._list_index(segment, location)
            if index >= len(value):
                raise self._error(location, "schema overlay list index is out of range")
            return value[index]
        raise self._error(location, "schema overlay parent path is not traversable")

    def _list_index(self, segment: str, location: str) -> int:
        if not re.fullmatch(r"(0|[1-9][0-9]*)", segment):
            raise self._error(
                location,
                "schema overlay list indices must be canonical non-negative integers",
            )
        return int(segment)

    def _canonical_document(self, document: dict[str, Any]) -> bytes:
        try:
            return json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise self._error(
                "$.schema_overlay",
                "effective OpenAPI contract must contain only finite JSON values",
            ) from error

    @staticmethod
    def _error(location: str, message: str) -> OpenAPIMaterialError:
        return OpenAPIMaterialError(
            OpenAPICapabilityGap(
                code="IF-01",
                capability="schema_overlay",
                location=location,
                message=message,
                fatal=True,
            )
        )


class OpenAPIMaterialLoader:
    def __init__(self) -> None:
        self.operation_overlay_reconciler = OpenAPIOperationContractOverlayReconciler()
        self.operation_semantics_reconciler = (
            OpenAPIOperationSemanticsOverlayReconciler()
        )
        self.overlay_reconciler = OpenAPISchemaOverlayReconciler()

    async def load(
        self,
        request: OpenAPIConnectorGenerationRequest,
    ) -> tuple[dict[str, Any], OpenAPISourceProvenance, list[OpenAPICapabilityGap]]:
        if request.document:
            raw = request.document.encode()
            source_kind: Literal["inline", "url"] = "inline"
            source_url = ""
        else:
            raw = await self._fetch(request)
            source_kind = "url"
            source_url = request.document_url
        if len(raw) > MAX_OPENAPI_BYTES:
            raise self._error("IF-01", "document_size", "$", "OpenAPI document exceeds 5 MB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise self._error(
                "IF-01", "document_encoding", "$", "OpenAPI document must be UTF-8"
            ) from error
        document = self._parse(text)
        version = str(document.get("openapi", ""))
        if not (version.startswith("3.0.") or version.startswith("3.1.")):
            raise self._error(
                "IF-01",
                "openapi_version",
                "$.openapi",
                "only OpenAPI 3.0 and 3.1 documents are supported",
            )
        if not isinstance(document.get("paths"), dict):
            raise self._error("IF-01", "paths", "$.paths", "OpenAPI paths must be an object")
        source_gaps: list[OpenAPICapabilityGap] = []
        source_resolved = self._resolve_refs(document, document, "$", (), source_gaps)
        self._inspect_unsupported(source_resolved, source_gaps)
        operation_semantics_provenance = (
            self.operation_semantics_reconciler.reconcile(
                source_resolved,
                request.operation_semantics_overlays,
            )
        )
        operation_effective_document, operation_overlay_provenance = (
            self.operation_overlay_reconciler.reconcile(
                document,
                request.operation_contract_overlays,
            )
        )
        effective_document, overlay_provenance = self.overlay_reconciler.reconcile(
            operation_effective_document,
            request.schema_overlay,
        )
        if request.operation_contract_overlays or request.schema_overlay:
            effective_gaps: list[OpenAPICapabilityGap] = []
            resolved = self._resolve_refs(
                effective_document,
                effective_document,
                "$",
                (),
                effective_gaps,
            )
            self._inspect_unsupported(resolved, effective_gaps)
            gaps = [*source_gaps, *effective_gaps]
        else:
            resolved = source_resolved
            gaps = source_gaps
        info = resolved.get("info", {}) if isinstance(resolved.get("info"), dict) else {}
        provenance = OpenAPISourceProvenance(
            source_kind=source_kind,
            source_url=source_url,
            source_digest=hashlib.sha256(raw).hexdigest(),
            openapi_version=version,
            title=str(info.get("title") or request.connector_id),
            document_version=str(info.get("version") or "unknown"),
            size_bytes=len(raw),
            schema_overlay=overlay_provenance,
            operation_contract_overlay=operation_overlay_provenance,
            operation_semantics_overlay=operation_semantics_provenance,
        )
        return resolved, provenance, self._unique_gaps(gaps)

    async def _fetch(self, request: OpenAPIConnectorGenerationRequest) -> bytes:
        parsed = urlsplit(request.document_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        allowed = {item.casefold().rstrip(".") for item in request.allowed_document_hosts}
        if parsed.scheme not in {"https", "http"} or not host:
            raise self._error("IF-01", "document_url", "$", "document URL must be HTTP(S)")
        if parsed.scheme == "http" and not request.allow_insecure_document_http:
            raise self._error(
                "IF-01", "document_url", "$", "insecure HTTP document URL is disabled"
            )
        if not any(host == item or host.endswith(f".{item}") for item in allowed):
            raise self._error(
                "IF-01", "document_host", "$", "document URL host is outside explicit allowlist"
            )
        addresses = await self._resolved_addresses(host, parsed.port)
        if any(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
            for address in addresses
        ):
            raise self._error(
                "IF-01", "document_host", "$", "private and loopback document URLs are disabled"
            )
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        backend = _PinnedNetworkBackend(host, port, addresses)
        timeout = {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}
        try:
            async with httpcore.AsyncConnectionPool(network_backend=backend) as pool:
                async with pool.stream(
                    "GET",
                    request.document_url,
                    headers=[
                        (b"Accept", b"application/json, application/yaml, text/yaml"),
                        (b"Accept-Encoding", b"identity"),
                    ],
                    extensions={"timeout": timeout},
                ) as response:
                    headers = {
                        key.decode("latin-1").casefold(): value.decode("latin-1")
                        for key, value in response.headers
                    }
                    if not 200 <= response.status < 300:
                        raise self._error(
                            "IF-01",
                            "document_http",
                            "$",
                            f"OpenAPI document returned HTTP {response.status}",
                        )
                    encoding = headers.get("content-encoding", "identity").casefold()
                    if encoding not in {"", "identity"}:
                        raise self._error(
                            "IF-01",
                            "document_encoding",
                            "$",
                            "compressed OpenAPI document responses are disabled",
                        )
                    length = headers.get("content-length")
                    if length:
                        try:
                            declared_length = int(length)
                        except ValueError as error:
                            raise self._error(
                                "IF-01",
                                "document_http",
                                "$",
                                "OpenAPI document returned an invalid Content-Length",
                            ) from error
                        if declared_length > MAX_OPENAPI_BYTES:
                            raise self._error(
                                "IF-01", "document_size", "$", "OpenAPI document exceeds 5 MB"
                            )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_stream():
                        size += len(chunk)
                        if size > MAX_OPENAPI_BYTES:
                            raise self._error(
                                "IF-01", "document_size", "$", "OpenAPI document exceeds 5 MB"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except OpenAPIMaterialError:
            raise
        except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException) as error:
            raise self._error(
                "IF-01",
                "document_network",
                "$",
                f"OpenAPI document fetch failed: {error}",
            ) from error

    async def _resolved_addresses(
        self,
        host: str,
        port: int | None,
    ) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                records = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port or 443,
                    type=socket.SOCK_STREAM,
                )
            except socket.gaierror as error:
                raise self._error(
                    "IF-01", "document_dns", "$", f"document host DNS lookup failed: {error}"
                ) from error
            addresses = {ipaddress.ip_address(record[4][0]) for record in records}
            if not addresses:
                raise self._error(
                    "IF-01", "document_dns", "$", "document host resolved to no addresses"
                )
            return addresses
        return {literal}

    def _parse(self, text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            try:
                value = yaml.safe_load(text)
            except yaml.YAMLError as error:
                raise self._error("IF-01", "syntax", "$", f"invalid JSON/YAML: {error}") from error
        if not isinstance(value, dict):
            raise self._error(
                "IF-01", "document_root", "$", "OpenAPI document root must be an object"
            )
        return value

    def _resolve_refs(
        self,
        value: Any,
        root: dict[str, Any],
        location: str,
        stack: tuple[str, ...],
        gaps: list[OpenAPICapabilityGap],
    ) -> Any:
        if isinstance(value, list):
            return [
                self._resolve_refs(item, root, f"{location}[{index}]", stack, gaps)
                for index, item in enumerate(value)
            ]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str):
            if not reference.startswith("#/"):
                gap = OpenAPICapabilityGap(
                    code="IF-02",
                    capability="remote_reference",
                    location=location,
                    message=f"remote OpenAPI reference is unsupported: {reference}",
                    fatal=True,
                )
                gaps.append(gap)
                raise OpenAPIMaterialError(gap)
            if reference in stack:
                gap = OpenAPICapabilityGap(
                    code="IF-08",
                    capability="recursive_schema",
                    location=location,
                    message=f"recursive OpenAPI reference is unsupported: {reference}",
                    fatal=True,
                )
                gaps.append(gap)
                raise OpenAPIMaterialError(gap)
            target: Any = root
            try:
                for part in reference[2:].split("/"):
                    target = target[unquote(part).replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as error:
                raise self._error(
                    "IF-01", "local_reference", location, f"unresolved local reference: {reference}"
                ) from error
            merged = dict(target) if isinstance(target, dict) else target
            if isinstance(merged, dict):
                merged.update({key: item for key, item in value.items() if key != "$ref"})
            return self._resolve_refs(merged, root, location, (*stack, reference), gaps)
        return {
            key: self._resolve_refs(item, root, f"{location}.{key}", stack, gaps)
            for key, item in value.items()
        }

    def _inspect_unsupported(
        self, document: dict[str, Any], gaps: list[OpenAPICapabilityGap]
    ) -> None:
        if document.get("webhooks"):
            gaps.append(
                self._gap(
                    "IF-03", "webhooks", "$.webhooks", "webhooks require a later transport stage"
                )
            )
        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(operation, dict):
                    continue
                location = f"$.paths.{path}.{method}"
                if operation.get("callbacks"):
                    gaps.append(
                        self._gap(
                            "IF-03",
                            "callbacks",
                            location,
                            "callbacks are recorded but not generated",
                        )
                    )

    @staticmethod
    def _gap(
        code: str, capability: str, location: str, message: str, *, fatal: bool = False
    ) -> OpenAPICapabilityGap:
        return OpenAPICapabilityGap(
            code=code, capability=capability, location=location, message=message, fatal=fatal
        )

    def _error(
        self, code: str, capability: str, location: str, message: str
    ) -> OpenAPIMaterialError:
        return OpenAPIMaterialError(self._gap(code, capability, location, message, fatal=True))

    @staticmethod
    def _unique_gaps(gaps: list[OpenAPICapabilityGap]) -> list[OpenAPICapabilityGap]:
        found: dict[tuple[str, str, str], OpenAPICapabilityGap] = {}
        for gap in gaps:
            found[(gap.code, gap.capability, gap.location)] = gap
        return list(found.values())


class OpenAPIConnectorGenerator:
    def generate(
        self,
        document: dict[str, Any],
        request: OpenAPIConnectorGenerationRequest,
        provenance: OpenAPISourceProvenance,
        initial_gaps: list[OpenAPICapabilityGap],
        *,
        parse_ms: float,
    ) -> OpenAPIConnectorGeneration:
        started = time.perf_counter()
        gaps = list(initial_gaps)
        security_schemes = self._security_schemes(document, gaps)
        selection_mode: Literal["all", "include", "exclude"] = (
            "include"
            if request.include_operation_ids
            else "exclude"
            if request.exclude_operation_ids
            else "all"
        )
        requested_operation_ids = sorted(
            request.include_operation_ids or request.exclude_operation_ids
        )
        selected_locations = self._selected_operation_locations(document, request)
        operations: list[ConnectorOperation] = []
        mapped_fields = 0
        total_fields = 0
        discovered = 0
        used_ids: set[str] = set()
        official_to_generated: dict[str, str] = {}
        global_security = document.get("security", [])
        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            path_parameters = path_item.get("parameters", [])
            for method, raw_operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(raw_operation, dict):
                    continue
                discovered += 1
                if (str(path), method) not in selected_locations:
                    continue
                generated = self._operation(
                    path,
                    method,
                    raw_operation,
                    path_parameters,
                    global_security,
                    used_ids,
                    gaps,
                )
                if generated is None:
                    continue
                operation, mapped, total = generated
                operations.append(operation)
                used_ids.add(operation.id)
                official_id = raw_operation.get("operationId")
                if isinstance(official_id, str) and official_id:
                    official_to_generated[official_id] = operation.id
                mapped_fields += mapped
                total_fields += total
        if not operations:
            operation_gap = next(
                (
                    gap
                    for gap in gaps
                    if gap.location.startswith("$.paths.")
                    and gap.code in {"IF-04", "IF-05", "IF-06", "IF-07", "IF-09", "IF-11", "IF-14"}
                ),
                None,
            )
            if discovered and operation_gap is not None:
                raise OpenAPIMaterialError(
                    operation_gap.model_copy(update={"fatal": True})
                )
            raise OpenAPIMaterialError(
                OpenAPICapabilityGap(
                    code="IF-01",
                    capability="operations",
                    location="$.paths",
                    message="OpenAPI document contains no supported REST operations",
                    fatal=True,
                )
            )
        operations = self._apply_operation_semantics(
            operations,
            request.operation_semantics_overlays,
            official_to_generated,
        )
        provenance = self._bind_operation_semantics_provenance(
            provenance,
            operations,
        )
        selected_scheme = request.deployment.auth_scheme_id
        if not selected_scheme:
            selected_scheme = self._first_required_scheme(operations)
        auth = next((item for item in security_schemes if item.id == selected_scheme), None)
        auth_type: Literal["none", "bearer", "basic", "api_key"] = "none"
        auth_location: Literal["header", "query", "cookie"] = "header"
        auth_wire_name = "Authorization"
        auth_prefix = request.deployment.auth_prefix
        if auth:
            if auth.type == "http" and auth.scheme.casefold() == "bearer":
                auth_type = "bearer"
            elif auth.type == "http" and auth.scheme.casefold() == "basic":
                auth_type = "basic"
            elif auth.type == "apiKey":
                auth_type = "api_key"
                auth_location = auth.location
                auth_wire_name = auth.wire_name
        profile = ConnectorDeploymentProfile(
            id=request.deployment.profile_id,
            environment=request.deployment.environment,
            base_url=request.deployment.base_url,
            auth_type=auth_type,
            auth_location=auth_location,
            auth_wire_name=auth_wire_name,
            auth_prefix=auth_prefix,
            allowed_hosts=request.deployment.allowed_hosts,
            available=request.deployment.available,
            timeout_seconds=request.deployment.timeout_seconds,
            claim_ceiling=request.deployment.claim_ceiling,
            excluded_claims=["customer production readiness", "non-REST transport support"],
        )
        operation_selection = OpenAPIOperationSelection(
            mode=selection_mode,
            requested_operation_ids=requested_operation_ids,
            generated_operation_ids=[item.id for item in operations],
        )
        manifest = ConnectorManifest(
            connector_id=request.connector_id,
            version=request.version,
            title=provenance.title,
            description=(
                f"Automatically generated from OpenAPI {provenance.openapi_version}; "
                f"source digest {provenance.source_digest[:12]}."
            ),
            domain=request.domain,
            operations=operations,
            deployment_profiles=[profile],
            security_schemes=security_schemes,
            source_provenance={
                **provenance.model_dump(mode="json"),
                "operation_selection": operation_selection.model_dump(mode="json"),
            },
        )
        return OpenAPIConnectorGeneration(
            id=str(uuid4()),
            connector_id=request.connector_id,
            version=request.version,
            provenance=provenance,
            operation_selection=operation_selection,
            manifest=manifest,
            gaps=OpenAPIMaterialLoader._unique_gaps(gaps),
            discovered_operation_count=discovered,
            generated_operation_count=len(operations),
            mapped_field_count=mapped_fields,
            total_field_count=total_fields,
            parse_ms=parse_ms,
            generate_ms=(time.perf_counter() - started) * 1000,
        )

    def _apply_operation_semantics(
        self,
        operations: list[ConnectorOperation],
        overlays: list[OpenAPIOperationSemanticsOverlay],
        official_to_generated: dict[str, str],
    ) -> list[ConnectorOperation]:
        if not overlays:
            return operations
        by_generated_id = {item.id: item for item in operations}
        updates: dict[str, dict[str, Any]] = {}
        ordered = sorted(overlays, key=lambda item: item.operation_id)
        for index, overlay in enumerate(ordered):
            location = f"$.operation_semantics_overlays[{index}]"
            generated_id = official_to_generated.get(overlay.operation_id)
            if generated_id is None or generated_id not in by_generated_id:
                raise OpenAPIMaterialError(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "operation_semantics_overlay",
                        f"{location}.operation_id",
                        "operation semantics overlay source must be present in the "
                        "same generated manifest",
                        fatal=True,
                    )
                )
            operation = by_generated_id[generated_id]
            if overlay.kind == "compensate":
                if operation.method != "DELETE":
                    raise OpenAPIMaterialError(
                        OpenAPIMaterialLoader._gap(
                            "IF-04",
                            "operation_semantics_overlay",
                            f"{location}.kind",
                            "kind=compensate may only be declared for a generated "
                            "DELETE operation",
                            fatal=True,
                        )
                    )
                updates.setdefault(generated_id, {})["kind"] = "compensate"
                continue
            target_official_id = overlay.compensation_operation_id
            target_generated_id = (
                official_to_generated.get(target_official_id)
                if target_official_id is not None
                else None
            )
            if (
                target_generated_id is None
                or target_generated_id not in by_generated_id
            ):
                raise OpenAPIMaterialError(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "operation_semantics_overlay",
                        f"{location}.compensation_operation_id",
                        "compensation operation must be present in the same generated "
                        "manifest",
                        fatal=True,
                    )
                )
            target = by_generated_id[target_generated_id]
            if operation.kind != "write" or target.method != "DELETE":
                raise OpenAPIMaterialError(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "operation_semantics_overlay",
                        f"{location}.compensation_operation_id",
                        "compensation binding requires a generated write operation "
                        "and a generated DELETE target",
                        fatal=True,
                    )
                )
            updates.setdefault(generated_id, {})[
                "compensation_operation_id"
            ] = target_generated_id
        updated = [
            operation.model_copy(update=updates.get(operation.id, {}))
            for operation in operations
        ]
        updated_by_id = {item.id: item for item in updated}
        for overlay in ordered:
            if overlay.compensation_operation_id is None:
                continue
            target_generated_id = official_to_generated[overlay.compensation_operation_id]
            if updated_by_id[target_generated_id].kind != "compensate":
                raise OpenAPIMaterialError(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "operation_semantics_overlay",
                        "$.operation_semantics_overlays",
                        "compensation target must be generated as kind=compensate",
                        fatal=True,
                    )
                )
        return updated

    @staticmethod
    def _bind_operation_semantics_provenance(
        provenance: OpenAPISourceProvenance,
        operations: list[ConnectorOperation],
    ) -> OpenAPISourceProvenance:
        semantics = provenance.operation_semantics_overlay
        if not semantics.operation_count:
            return provenance
        effective = json.dumps(
            [
                operation.model_dump(mode="json")
                for operation in sorted(operations, key=lambda item: item.id)
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return provenance.model_copy(
            update={
                "operation_semantics_overlay": semantics.model_copy(
                    update={
                        "effective_manifest_digest": (
                            f"sha256:{hashlib.sha256(effective).hexdigest()}"
                        )
                    }
                )
            }
        )

    def _selected_operation_locations(
        self,
        document: dict[str, Any],
        request: OpenAPIConnectorGenerationRequest,
    ) -> set[tuple[str, str]]:
        operations: list[tuple[str, str, str | None]] = []
        by_id: dict[str, list[tuple[str, str]]] = {}
        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, raw_operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(raw_operation, dict):
                    continue
                raw_id = raw_operation.get("operationId")
                operation_id = str(raw_id) if isinstance(raw_id, str) and raw_id else None
                location = (str(path), method)
                operations.append((*location, operation_id))
                if operation_id is not None:
                    by_id.setdefault(operation_id, []).append(location)
        requested = set(request.include_operation_ids or request.exclude_operation_ids)
        unknown = sorted(requested - set(by_id))
        selection_location = (
            "$.include_operation_ids"
            if request.include_operation_ids
            else "$.exclude_operation_ids"
        )
        if unknown:
            raise OpenAPIMaterialError(
                OpenAPIMaterialLoader._gap(
                    "IF-04",
                    "operation_selection",
                    selection_location,
                    f"unknown official operationId values: {unknown}",
                    fatal=True,
                )
            )
        ambiguous = sorted(
            operation_id
            for operation_id in requested
            if len(by_id.get(operation_id, [])) != 1
        )
        if ambiguous:
            raise OpenAPIMaterialError(
                OpenAPIMaterialLoader._gap(
                    "IF-04",
                    "operation_selection",
                    "$.paths",
                    f"selected operationId values are not unique in the document: {ambiguous}",
                    fatal=True,
                )
            )
        if not requested:
            return {(path, method) for path, method, _ in operations}
        selected: set[tuple[str, str]] = set()
        for path, method, operation_id in operations:
            if operation_id is None:
                continue
            if request.include_operation_ids and operation_id in requested:
                selected.add((path, method))
            elif request.exclude_operation_ids and operation_id not in requested:
                selected.add((path, method))
        if not selected:
            raise OpenAPIMaterialError(
                OpenAPIMaterialLoader._gap(
                    "IF-04",
                    "operation_selection",
                    "$.paths",
                    "operation selection produced an empty manifest scope",
                    fatal=True,
                )
            )
        return selected

    def _operation(
        self,
        path: str,
        method: str,
        raw: dict[str, Any],
        path_parameters: Any,
        global_security: Any,
        used_ids: set[str],
        gaps: list[OpenAPICapabilityGap],
    ) -> tuple[ConnectorOperation, int, int] | None:
        location = f"$.paths.{path}.{method}"
        raw_id = str(raw.get("operationId") or f"{method}_{path}")
        operation_id = self._identifier(raw_id, prefix="operation")
        suffix = 2
        base_id = operation_id
        while operation_id in used_ids:
            operation_id = f"{base_id}_{suffix}"
            suffix += 1
        parameters: list[ConnectorParameterBinding] = []
        request_properties: dict[str, Any] = {}
        required_inputs: list[str] = []
        mapped = 0
        total = 0
        raw_parameters = []
        if isinstance(path_parameters, list):
            raw_parameters.extend(path_parameters)
        if isinstance(raw.get("parameters"), list):
            raw_parameters.extend(raw["parameters"])
        if len(raw_parameters) > MAX_CONNECTOR_OPERATION_PARAMETERS:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-04",
                    "parameter_count",
                    f"{location}.parameters",
                    "operation declares "
                    f"{len(raw_parameters)} parameters; the bounded connector limit is "
                    f"{MAX_CONNECTOR_OPERATION_PARAMETERS}",
                )
            )
            return None
        for index, parameter in enumerate(raw_parameters):
            if not isinstance(parameter, dict):
                continue
            total += 1
            wire_name = str(parameter.get("name") or "")
            parameter_location = str(parameter.get("in") or "")
            schema = parameter.get("schema", {})
            if (
                not wire_name
                or parameter_location not in {"path", "query", "header", "cookie"}
                or not isinstance(schema, dict)
            ):
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "parameter",
                        f"{location}.parameters[{index}]",
                        "unsupported parameter declaration",
                    )
                )
                continue
            if not self._schema_supported(schema, f"{location}.parameters[{index}].schema", gaps):
                continue
            style = str(
                parameter.get("style")
                or ("simple" if parameter_location in {"path", "header"} else "form")
            )
            explode = bool(parameter.get("explode", style == "form"))
            if style not in {"simple", "form"}:
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-04",
                        "parameter_serialization",
                        f"{location}.parameters[{index}]",
                        f"parameter style {style!r} is unsupported",
                    )
                )
                continue
            input_key = self._unique_input_key(wire_name, parameter_location, request_properties)
            request_properties[input_key] = schema
            required = bool(parameter.get("required")) or parameter_location == "path"
            if required:
                required_inputs.append(input_key)
            parameters.append(
                ConnectorParameterBinding(
                    input_key=input_key,
                    wire_name=wire_name,
                    location=parameter_location,
                    required=required,
                    style=style,
                    explode=explode,
                )
            )
            mapped += 1
        request_body: ConnectorRequestBody | None = None
        body = raw.get("requestBody")
        if isinstance(body, dict):
            content = body.get("content", {})
            json_media = content.get("application/json") if isinstance(content, dict) else None
            multipart_media = (
                content.get("multipart/form-data") if isinstance(content, dict) else None
            )
            media = json_media if isinstance(json_media, dict) else multipart_media
            if not isinstance(media, dict) or not isinstance(media.get("schema", {}), dict):
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-05",
                        "request_media_type",
                        f"{location}.requestBody",
                        "only application/json and multipart/form-data request bodies are generated",
                    )
                )
                return None
            body_schema = self._schema_for_direction(media.get("schema", {}), request=True)
            body_field_count = self._schema_field_count(body_schema)
            total += body_field_count
            multipart_parts: list[ConnectorMultipartPart] = []
            content_type = "application/json"
            if media is multipart_media:
                multipart_contract = self._multipart_request_contract(
                    body_schema,
                    media,
                    f"{location}.requestBody",
                    gaps,
                )
                if multipart_contract is None:
                    return None
                body_schema, multipart_parts = multipart_contract
                content_type = "multipart/form-data"
            if not self._schema_supported(body_schema, f"{location}.requestBody.schema", gaps):
                return None
            request_properties["body"] = body_schema
            if body.get("required"):
                required_inputs.append("body")
            request_body = ConnectorRequestBody(
                required=bool(body.get("required")),
                content_type=content_type,
                multipart_parts=multipart_parts,
            )
            mapped += body_field_count
        responses = raw.get("responses", {})
        response_schema, response_type, statuses, content_types, errors = self._responses(
            responses, location, gaps
        )
        if response_schema is None:
            return None
        response_schema = self._schema_for_direction(response_schema, request=False)
        response_field_count = self._schema_field_count(response_schema)
        total += response_field_count
        if not self._schema_supported(response_schema, f"{location}.responses", gaps):
            return None
        mapped += response_field_count
        response_object_schema = self._object_schema(
            f"{operation_id}.response",
            response_schema if response_type == "object" else {},
        )
        request_json_schema = {
            "type": "object",
            "properties": request_properties,
            "required": required_inputs,
            "additionalProperties": False,
        }
        request_schema = self._object_schema(f"{operation_id}.request", request_json_schema)
        security = raw.get("security", global_security)
        security_requirements = (
            [
                [self._identifier(str(key), prefix="security") for key in item]
                for item in security
                if isinstance(item, dict)
            ]
            if isinstance(security, list)
            else []
        )
        title = str(raw.get("summary") or raw.get("description") or operation_id)
        return (
            ConnectorOperation(
                id=operation_id,
                title=title[:200],
                kind="read" if method == "get" else "write",
                method=method.upper(),
                path=path,
                request_schema=request_schema,
                response_schema=response_object_schema,
                parameters=parameters,
                request_body=request_body,
                response_json_schema=response_schema,
                response_root_type=response_type,
                success_status_codes=statuses,
                response_content_types=content_types,
                security_requirements=security_requirements,
                error_responses=errors,
            ),
            mapped,
            total,
        )

    def _multipart_request_contract(
        self,
        schema: dict[str, Any],
        media: dict[str, Any],
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> tuple[dict[str, Any], list[ConnectorMultipartPart]] | None:
        flattened = self._multipart_properties(schema, location, gaps)
        if flattened is None:
            return None
        properties, required = flattened
        if not properties:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "multipart_schema",
                    f"{location}.schema",
                    "multipart/form-data requires declared object properties",
                )
            )
            return None
        if len(properties) > MAX_CONNECTOR_MULTIPART_PARTS:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "multipart_part_count",
                    f"{location}.schema.properties",
                    f"multipart request declares {len(properties)} parts; the bounded limit is "
                    f"{MAX_CONNECTOR_MULTIPART_PARTS}",
                )
            )
            return None
        encoding = media.get("encoding", {})
        if not isinstance(encoding, dict):
            encoding = {}
        normalized_properties: dict[str, Any] = {}
        parts: list[ConnectorMultipartPart] = []
        for name, part_schema in properties.items():
            part_location = f"{location}.schema.properties.{name}"
            if not isinstance(name, str) or not 1 <= len(name) <= 300:
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-05",
                        "multipart_part_name",
                        part_location,
                        "multipart part names must contain 1 to 300 characters",
                    )
                )
                return None
            if not isinstance(part_schema, dict):
                part_schema = {}
            raw_encoding = encoding.get(name, {})
            if not isinstance(raw_encoding, dict):
                raw_encoding = {}
            content_types = self._multipart_content_types(
                raw_encoding.get("contentType")
                or part_schema.get("contentMediaType"),
                binary=self._is_binary_schema(part_schema),
                location=part_location,
                gaps=gaps,
            )
            if content_types is None:
                return None
            if self._is_binary_schema(part_schema):
                max_encoded_length = 4 * ((MAX_CONNECTOR_BLOB_BYTES + 2) // 3)
                normalized_properties[name] = {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 255,
                            "example": "upload.bin",
                        },
                        "content_type": {
                            "type": "string",
                            "default": content_types[0],
                            "maxLength": 200,
                        },
                        "content_base64": {
                            "type": "string",
                            "maxLength": max_encoded_length,
                            "example": "Zml4dHVyZQ==",
                        },
                        "sha256": {
                            "type": "string",
                            "pattern": r"^sha256:[0-9a-f]{64}$",
                        },
                    },
                    "required": ["filename", "content_type", "content_base64"],
                    "additionalProperties": False,
                    "x-lilies-blob-contract": "inline-base64-v1",
                    "x-lilies-max-decoded-bytes": MAX_CONNECTOR_BLOB_BYTES,
                }
                parts.append(
                    ConnectorMultipartPart(
                        input_key=name,
                        wire_name=name,
                        kind="blob",
                        required=name in required,
                        content_types=content_types,
                        max_bytes=MAX_CONNECTOR_BLOB_BYTES,
                    )
                )
                continue
            part_type = self._schema_type(part_schema)
            if part_type not in {"string", "number", "integer", "boolean"}:
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-05",
                        "multipart_part_schema",
                        part_location,
                        "multipart text parts must use scalar schemas; nested objects and "
                        "arrays require a later serialization contract",
                    )
                )
                return None
            normalized_properties[name] = part_schema
            parts.append(
                ConnectorMultipartPart(
                    input_key=name,
                    wire_name=name,
                    kind="text",
                    required=name in required,
                    content_types=content_types,
                )
            )
        return (
            {
                "type": "object",
                "properties": normalized_properties,
                "required": sorted(required & set(normalized_properties)),
                "additionalProperties": False,
            },
            parts,
        )

    def _multipart_properties(
        self,
        schema: dict[str, Any],
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> tuple[dict[str, dict[str, Any]], set[str]] | None:
        if "oneOf" in schema or "anyOf" in schema:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "multipart_schema_composition",
                    f"{location}.schema",
                    "multipart root oneOf/anyOf part sets are not generated",
                )
            )
            return None
        raw_type = schema.get("type")
        if raw_type not in {None, "object"}:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "multipart_schema",
                    f"{location}.schema",
                    "multipart/form-data schema root must be an object",
                )
            )
            return None
        properties: dict[str, dict[str, Any]] = {}
        required = {
            str(item)
            for item in schema.get("required", [])
            if isinstance(item, str)
        }
        raw_properties = schema.get("properties", {})
        if isinstance(raw_properties, dict):
            for name, part_schema in raw_properties.items():
                properties[str(name)] = part_schema if isinstance(part_schema, dict) else {}
        candidates = schema.get("allOf", [])
        if not isinstance(candidates, list):
            candidates = []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            nested = self._multipart_properties(
                candidate,
                f"{location}.schema.allOf[{index}]",
                gaps,
            )
            if nested is None:
                return None
            nested_properties, nested_required = nested
            required.update(nested_required)
            for name, part_schema in nested_properties.items():
                existing = properties.get(name)
                if existing is not None and json.dumps(
                    existing,
                    sort_keys=True,
                    separators=(",", ":"),
                ) != json.dumps(part_schema, sort_keys=True, separators=(",", ":")):
                    gaps.append(
                        OpenAPIMaterialLoader._gap(
                            "IF-05",
                            "multipart_schema_conflict",
                            f"{location}.schema.properties.{name}",
                            "multipart allOf declares incompatible schemas for one part",
                        )
                    )
                    return None
                properties[name] = part_schema
        return properties, required

    @staticmethod
    def _is_binary_schema(schema: dict[str, Any]) -> bool:
        if schema.get("format") in {"binary", "byte"}:
            return True
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if isinstance(candidates, list) and any(
                OpenAPIConnectorGenerator._is_binary_schema(candidate)
                for candidate in candidates
                if isinstance(candidate, dict)
            ):
                return True
        return False

    @staticmethod
    def _multipart_content_types(
        raw: Any,
        *,
        binary: bool,
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> list[str] | None:
        values = (
            [item.strip() for item in str(raw).split(",") if item.strip()]
            if raw
            else ["application/octet-stream" if binary else "text/plain"]
        )
        if len(values) > 20 or any(
            re.fullmatch(
                r"[A-Za-z0-9!#$&^_.+-]+/(?:[A-Za-z0-9!#$&^_.+-]+|\*)",
                item,
            )
            is None
            or (not binary and item.endswith("/*"))
            for item in values
        ):
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "multipart_content_type",
                    location,
                    "multipart contentType declaration is invalid or exceeds 20 alternatives",
                )
            )
            return None
        return values

    def _responses(
        self,
        responses: Any,
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> tuple[dict[str, Any] | None, str, list[int], list[str], dict[str, str]]:
        if not isinstance(responses, dict):
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-14", "responses", f"{location}.responses", "responses must be an object"
                )
            )
            return None, "object", [], [], {}
        success: list[tuple[int, dict[str, Any]]] = []
        errors: dict[str, str] = {}
        for status, response in responses.items():
            if not isinstance(response, dict):
                continue
            if str(status).isdigit() and 200 <= int(status) < 300:
                success.append((int(status), response))
            else:
                errors[str(status)] = str(response.get("description") or "documented error")
        if not success:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-14",
                    "success_response",
                    f"{location}.responses",
                    "operation has no explicit 2xx response",
                )
            )
            return None, "object", [], [], errors
        success.sort(key=lambda item: item[0])
        status, selected = success[0]
        content = selected.get("content", {})
        if status == 204 or not content:
            return (
                {"type": "object", "properties": {}, "additionalProperties": True},
                "object",
                [item[0] for item in success],
                [],
                errors,
            )
        if not isinstance(content, dict) or "application/json" not in content:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-05",
                    "response_media_type",
                    f"{location}.responses.{status}",
                    "only application/json responses are generated",
                )
            )
            return None, "object", [], [], errors
        media = content["application/json"]
        schema = media.get("schema", {}) if isinstance(media, dict) else {}
        if not isinstance(schema, dict):
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-14",
                    "response_schema",
                    f"{location}.responses.{status}",
                    "response schema must be an object",
                )
            )
            return None, "object", [], [], errors
        root_type = self._schema_type(schema)
        return schema, root_type, [item[0] for item in success], ["application/json"], errors

    def _security_schemes(
        self, document: dict[str, Any], gaps: list[OpenAPICapabilityGap]
    ) -> list[ConnectorSecurityScheme]:
        raw = (
            document.get("components", {}).get("securitySchemes", {})
            if isinstance(document.get("components"), dict)
            else {}
        )
        result: list[ConnectorSecurityScheme] = []
        if not isinstance(raw, dict):
            return result
        for scheme_id, scheme in raw.items():
            if not isinstance(scheme, dict):
                continue
            normalized_id = self._identifier(str(scheme_id), prefix="security")
            if scheme.get("type") == "http" and str(scheme.get("scheme", "")).casefold() in {
                "bearer",
                "basic",
            }:
                result.append(
                    ConnectorSecurityScheme(
                        id=normalized_id,
                        type="http",
                        scheme=str(scheme.get("scheme", "")).casefold(),
                    )
                )
            elif scheme.get("type") == "apiKey" and scheme.get("in") in {
                "header",
                "query",
                "cookie",
            }:
                result.append(
                    ConnectorSecurityScheme(
                        id=normalized_id,
                        type="apiKey",
                        location=scheme["in"],
                        wire_name=str(scheme.get("name") or "Authorization"),
                    )
                )
            else:
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-07",
                        "security_scheme",
                        f"$.components.securitySchemes.{scheme_id}",
                        f"security scheme {scheme.get('type')!r}/{scheme.get('scheme')!r} is unsupported",
                    )
                )
        return result

    def _schema_supported(
        self,
        schema: dict[str, Any],
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> bool:
        if "not" in schema:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-06",
                    "schema_composition",
                    location,
                    "JSON Schema keyword not is not generated",
                )
            )
            return False
        for keyword in ("oneOf", "anyOf", "allOf"):
            if keyword not in schema:
                continue
            candidates = schema[keyword]
            if (
                not isinstance(candidates, list)
                or not candidates
                or not all(isinstance(candidate, dict) for candidate in candidates)
            ):
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-06",
                        "schema_composition",
                        location,
                        f"JSON Schema keyword {keyword} must contain non-empty schema objects",
                    )
                )
                return False
            for index, candidate in enumerate(candidates):
                if not self._schema_supported(
                    candidate,
                    f"{location}.{keyword}[{index}]",
                    gaps,
                ):
                    return False
        if any(keyword in schema for keyword in ("allOf", "oneOf", "anyOf")) and not (
            self._schema_type_domain(schema)
        ):
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-06",
                    "schema_composition_conflict",
                    location,
                    "schema composition contains mutually incompatible value types",
                )
            )
            return False
        if "discriminator" in schema:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-09",
                    "schema_discriminator",
                    location,
                    "polymorphic discriminator mapping is not generated",
                )
            )
            return False
        if schema.get("format") in {"binary", "byte"}:
            gaps.append(
                OpenAPIMaterialLoader._gap(
                    "IF-11",
                    "binary_payload",
                    location,
                    "binary payload mapping is not generated",
                )
            )
            return False
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict) and not self._schema_supported(
                    child,
                    f"{location}.properties.{name}",
                    gaps,
                ):
                    return False
        items = schema.get("items")
        if isinstance(items, dict) and not self._schema_supported(items, f"{location}.items", gaps):
            return False
        return True

    def _schema_field_count(self, schema: dict[str, Any]) -> int:
        return max(1, len(self._schema_field_paths(schema)))

    def _schema_field_paths(
        self,
        schema: dict[str, Any],
        *,
        prefix: str = "",
    ) -> set[str]:
        paths: set[str] = set()
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child in properties.items():
                path = f"{prefix}.{name}" if prefix else str(name)
                if isinstance(child, dict):
                    nested = self._schema_field_paths(child, prefix=path)
                    paths.update(nested or {path})
                else:
                    paths.add(path)
        items = schema.get("items")
        if isinstance(items, dict):
            item_prefix = f"{prefix}[]" if prefix else "[]"
            nested = self._schema_field_paths(items, prefix=item_prefix)
            paths.update(nested or {item_prefix})
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if isinstance(candidate, dict):
                    paths.update(self._schema_field_paths(candidate, prefix=prefix))
        return paths

    def _schema_for_direction(
        self,
        schema: dict[str, Any],
        *,
        request: bool,
    ) -> dict[str, Any]:
        """Remove response-only fields from requests and request-only fields from responses."""
        if not isinstance(schema, dict):
            return {}
        normalized = dict(schema)
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if isinstance(candidates, list):
                normalized[keyword] = [
                    self._schema_for_direction(candidate, request=request)
                    if isinstance(candidate, dict)
                    else candidate
                    for candidate in candidates
                ]
        properties = schema.get("properties")
        excluded_names: set[str] = set()
        if isinstance(properties, dict):
            filtered: dict[str, Any] = {}
            for name, child in properties.items():
                child_schema = child if isinstance(child, dict) else {}
                excluded = bool(child_schema.get("readOnly" if request else "writeOnly"))
                if excluded:
                    excluded_names.add(str(name))
                    continue
                filtered[name] = self._schema_for_direction(child_schema, request=request)
            normalized["properties"] = filtered
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if isinstance(candidates, list):
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        excluded_names.update(
                            self._direction_excluded_property_names(
                                candidate,
                                request=request,
                            )
                        )
        required = schema.get("required")
        if isinstance(required, list) and excluded_names:
            normalized["required"] = [name for name in required if name not in excluded_names]
        items = schema.get("items")
        if isinstance(items, dict):
            normalized["items"] = self._schema_for_direction(items, request=request)
        return normalized

    def _direction_excluded_property_names(
        self,
        schema: dict[str, Any],
        *,
        request: bool,
    ) -> set[str]:
        excluded: set[str] = set()
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict) and child.get(
                    "readOnly" if request else "writeOnly"
                ):
                    excluded.add(str(name))
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if isinstance(candidates, list):
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        excluded.update(
                            self._direction_excluded_property_names(
                                candidate,
                                request=request,
                            )
                        )
        return excluded

    @staticmethod
    def _first_required_scheme(operations: list[ConnectorOperation]) -> str:
        for operation in operations:
            for alternative in operation.security_requirements:
                if alternative:
                    return alternative[0]
        return ""

    def _object_schema(self, schema_id: str, schema: dict[str, Any]) -> ConnectorObjectSchema:
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        fields: list[ConnectorSchemaField] = []
        if isinstance(properties, dict):
            for name, field_schema in list(properties.items())[:MAX_CONNECTOR_SCHEMA_FIELDS]:
                if not isinstance(field_schema, dict):
                    field_schema = {}
                field_name = self._identifier(str(name), prefix="field")
                value_type = self._schema_type(field_schema)
                item_type = None
                if value_type == "array":
                    items = field_schema.get("items", {})
                    item_type = self._schema_type(items if isinstance(items, dict) else {})
                enum = field_schema.get("enum", [])
                fields.append(
                    ConnectorSchemaField(
                        name=field_name,
                        value_type=value_type,
                        required=name in required,
                        item_type=item_type,
                        enum=enum[:100] if isinstance(enum, list) else [],
                        max_length=field_schema.get("maxLength"),
                    )
                )
        return ConnectorObjectSchema(
            schema_id=self._identifier(schema_id, prefix="schema")[:120],
            fields=fields,
            additional_properties=schema.get("additionalProperties", True) is not False
            if isinstance(schema, dict)
            else True,
            json_schema=schema or None,
        )

    @staticmethod
    def _schema_type(
        schema: dict[str, Any],
    ) -> Literal["string", "number", "integer", "boolean", "object", "array"]:
        raw = schema.get("type")
        if isinstance(raw, list):
            raw = next((item for item in raw if item != "null"), None)
        if raw in {"string", "number", "integer", "boolean", "object", "array"}:
            return raw
        if "properties" in schema:
            return "object"
        if "items" in schema:
            return "array"
        for keyword in ("allOf", "oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if not isinstance(candidates, list):
                continue
            branch_types = [
                OpenAPIConnectorGenerator._schema_type(candidate)
                for candidate in candidates
                if isinstance(candidate, dict) and candidate
            ]
            if branch_types:
                return branch_types[0]
        return "string"

    @classmethod
    def _schema_type_domain(cls, schema: dict[str, Any]) -> set[str]:
        all_types = {"null", "string", "number", "integer", "boolean", "object", "array"}
        raw_type = schema.get("type")
        if isinstance(raw_type, str) and raw_type in all_types:
            domain = {raw_type}
        elif isinstance(raw_type, list):
            domain = {item for item in raw_type if item in all_types}
            if not domain:
                domain = set(all_types)
        elif "properties" in schema:
            domain = {"object"}
        elif "items" in schema:
            domain = {"array"}
        else:
            domain = set(all_types)
        if "nullable" in schema and schema.get("nullable"):
            domain.add("null")
        if "number" in domain:
            domain.add("integer")
        for keyword in ("oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if isinstance(candidates, list) and candidates:
                alternatives: set[str] = set()
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        alternatives.update(cls._schema_type_domain(candidate))
                domain.intersection_update(alternatives)
        candidates = schema.get("allOf")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    domain.intersection_update(cls._schema_type_domain(candidate))
        return domain

    @staticmethod
    def _identifier(value: str, *, prefix: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")
        if not normalized or not normalized[0].isalpha():
            normalized = f"{prefix}_{normalized}" if normalized else prefix
        if len(normalized) < 2:
            normalized = f"{normalized}_"
        return normalized[:119]

    def _unique_input_key(self, wire_name: str, location: str, properties: dict[str, Any]) -> str:
        base = self._identifier(wire_name, prefix="parameter").replace(".", "_").replace("-", "_")
        candidate = base
        if candidate in properties:
            candidate = f"{location}_{base}"
        suffix = 2
        while candidate in properties:
            candidate = f"{location}_{base}_{suffix}"
            suffix += 1
        return candidate


class OpenAPIConnectorService:
    def __init__(
        self,
        *,
        storage: Storage,
        harness: PlatformHarness,
        connectors: ConnectorService,
    ) -> None:
        self.storage = storage
        self.harness = harness
        self.connectors = connectors
        self.loader = OpenAPIMaterialLoader()
        self.generator = OpenAPIConnectorGenerator()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self.storage._connect() as conn:
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(openapi_connector_generations)"
                ).fetchall()
            }
            if columns and "request_fingerprint" not in columns:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE openapi_connector_generations_v2 (
                      id TEXT PRIMARY KEY,
                      connector_id TEXT NOT NULL,
                      version INTEGER NOT NULL,
                      source_digest TEXT NOT NULL,
                      request_fingerprint TEXT NOT NULL,
                      record_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(
                        connector_id,
                        version,
                        source_digest,
                        request_fingerprint
                      )
                    )
                    """
                )
                rows = conn.execute(
                    """
                    SELECT id,connector_id,version,source_digest,record_json,created_at
                    FROM openapi_connector_generations
                    """
                ).fetchall()
                for row in rows:
                    generation = OpenAPIConnectorGeneration.model_validate_json(
                        row["record_json"]
                    )
                    generation = self._normalize_generation_selection(generation)
                    fingerprint = self._request_fingerprint(generation)
                    generation = generation.model_copy(
                        update={"request_fingerprint": fingerprint}
                    )
                    conn.execute(
                        """
                        INSERT INTO openapi_connector_generations_v2
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            row["id"],
                            row["connector_id"],
                            row["version"],
                            row["source_digest"],
                            fingerprint,
                            generation.model_dump_json(),
                            row["created_at"],
                        ),
                    )
                conn.execute("DROP TABLE openapi_connector_generations")
                conn.execute(
                    """
                    ALTER TABLE openapi_connector_generations_v2
                    RENAME TO openapi_connector_generations
                    """
                )
                conn.commit()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS openapi_connector_generations (
                  id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  source_digest TEXT NOT NULL,
                  request_fingerprint TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(
                    connector_id,
                    version,
                    source_digest,
                    request_fingerprint
                  )
                );
                CREATE TABLE IF NOT EXISTS openapi_connector_contract_runs (
                  id TEXT PRIMARY KEY,
                  generation_id TEXT NOT NULL,
                  source_digest TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_openapi_contract_generation
                  ON openapi_connector_contract_runs(generation_id,created_at);
                """
            )
            rows = conn.execute(
                "SELECT id,record_json FROM openapi_connector_generations"
            ).fetchall()
            for row in rows:
                generation = self._normalize_generation_selection(
                    OpenAPIConnectorGeneration.model_validate_json(row["record_json"])
                )
                fingerprint = self._request_fingerprint(generation)
                generation = generation.model_copy(
                    update={"request_fingerprint": fingerprint}
                )
                conn.execute(
                    """
                    UPDATE openapi_connector_generations
                    SET request_fingerprint=?,record_json=?
                    WHERE id=?
                    """,
                    (fingerprint, generation.model_dump_json(), row["id"]),
                )

    async def generate(
        self, request: OpenAPIConnectorGenerationRequest
    ) -> OpenAPIConnectorGeneration:
        started = time.perf_counter()
        document, provenance, gaps = await self.loader.load(request)
        parse_ms = (time.perf_counter() - started) * 1000
        generated = self.generator.generate(document, request, provenance, gaps, parse_ms=parse_ms)
        generated = generated.model_copy(
            update={"request_fingerprint": self._request_fingerprint(generated)}
        )
        async with self._lock:
            return await asyncio.to_thread(self._save_generation_sync, generated)

    def _save_generation_sync(
        self, generation: OpenAPIConnectorGeneration
    ) -> OpenAPIConnectorGeneration:
        with self.storage._connect() as conn:
            existing = conn.execute(
                """
                SELECT record_json
                FROM openapi_connector_generations
                WHERE connector_id=? AND version=? AND source_digest=?
                  AND request_fingerprint=?
                """,
                (
                    generation.connector_id,
                    generation.version,
                    generation.provenance.source_digest,
                    generation.request_fingerprint,
                ),
            ).fetchone()
            if existing:
                return OpenAPIConnectorGeneration.model_validate_json(existing["record_json"])
            conn.execute(
                "INSERT INTO openapi_connector_generations VALUES(?,?,?,?,?,?,?)",
                (
                    generation.id,
                    generation.connector_id,
                    generation.version,
                    generation.provenance.source_digest,
                    generation.request_fingerprint,
                    generation.model_dump_json(),
                    generation.created_at,
                ),
            )
        return generation

    @staticmethod
    def _request_fingerprint(generation: OpenAPIConnectorGeneration) -> str:
        material = {
            "connector_id": generation.connector_id,
            "version": generation.version,
            "domain": generation.manifest.domain,
            "deployment_profiles": [
                item.model_dump(mode="json")
                for item in generation.manifest.deployment_profiles
            ],
            "security_schemes": [
                item.model_dump(mode="json")
                for item in generation.manifest.security_schemes
            ],
            "operation_selection": generation.operation_selection.model_dump(mode="json"),
            "schema_overlay": generation.provenance.schema_overlay.model_dump(mode="json"),
            "operation_contract_overlay": (
                generation.provenance.operation_contract_overlay.model_dump(mode="json")
            ),
        }
        semantics_overlay = generation.provenance.operation_semantics_overlay
        if semantics_overlay.operation_count:
            material["operation_semantics_overlay"] = semantics_overlay.model_dump(
                mode="json"
            )
        canonical = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _normalize_generation_selection(
        generation: OpenAPIConnectorGeneration,
    ) -> OpenAPIConnectorGeneration:
        selection = generation.operation_selection
        if not selection.generated_operation_ids:
            selection = selection.model_copy(
                update={
                    "generated_operation_ids": [
                        item.id for item in generation.manifest.operations
                    ]
                }
            )
        return generation.model_copy(
            update={"operation_selection": selection}
        )

    @staticmethod
    def _contract_identity(generation: OpenAPIConnectorGeneration) -> str:
        parts = [generation.provenance.source_digest]
        operation_overlay = generation.provenance.operation_contract_overlay
        if operation_overlay.operation_count:
            parts.append(f"operation={operation_overlay.overlay_digest}")
        schema_overlay = generation.provenance.schema_overlay
        if schema_overlay.action_count:
            parts.append(f"schema={schema_overlay.overlay_digest}")
        semantics_overlay = generation.provenance.operation_semantics_overlay
        if semantics_overlay.operation_count:
            parts.append(
                "semantics="
                f"{semantics_overlay.overlay_digest}:"
                f"{semantics_overlay.effective_manifest_digest}"
            )
        return ":".join(parts)

    async def list_generations(self) -> list[OpenAPIConnectorGeneration]:
        return await asyncio.to_thread(self._list_generations_sync)

    def _list_generations_sync(self) -> list[OpenAPIConnectorGeneration]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM openapi_connector_generations ORDER BY created_at DESC"
            ).fetchall()
        items = [OpenAPIConnectorGeneration.model_validate_json(row["record_json"]) for row in rows]
        latest_identity: dict[str, str] = {}
        for item in items:
            latest_identity.setdefault(item.connector_id, self._contract_identity(item))
        return [
            item.model_copy(
                update={
                    "evidence_stale": latest_identity[item.connector_id]
                    != self._contract_identity(item)
                }
            )
            for item in items
        ]

    async def get_generation(self, generation_id: str) -> OpenAPIConnectorGeneration:
        return await asyncio.to_thread(self._get_generation_sync, generation_id)

    def _get_generation_sync(self, generation_id: str) -> OpenAPIConnectorGeneration:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM openapi_connector_generations WHERE id=?", (generation_id,)
            ).fetchone()
            if not row:
                raise KeyError(generation_id)
            item = OpenAPIConnectorGeneration.model_validate_json(row["record_json"])
            newest = conn.execute(
                "SELECT record_json FROM openapi_connector_generations "
                "WHERE connector_id=? ORDER BY created_at DESC LIMIT 1",
                (item.connector_id,),
            ).fetchone()
        newest_item = (
            OpenAPIConnectorGeneration.model_validate_json(newest["record_json"])
            if newest
            else None
        )
        stale = bool(
            newest_item
            and self._contract_identity(newest_item) != self._contract_identity(item)
        )
        return item.model_copy(update={"evidence_stale": stale})

    async def generate_contract_cases(self, generation_id: str) -> list[ConnectorContractCase]:
        generation = await self.get_generation(generation_id)
        cases: list[ConnectorContractCase] = []
        for operation in generation.manifest.operations:
            sample = self._sample_payload(operation.request_schema.json_schema or {})
            cases.append(
                ConnectorContractCase(
                    id=f"{operation.id}.positive",
                    operation_id=operation.id,
                    kind="positive",
                    expected=f"HTTP status in {operation.success_status_codes} and response matches generated schema",
                    generated_input=sample,
                )
            )
            required = list((operation.request_schema.json_schema or {}).get("required", []))
            if required:
                invalid = dict(sample)
                invalid.pop(required[0], None)
                negative_id = f"{operation.id}.negative.missing_{required[0]}"
                expected = f"local schema rejects missing required input {required[0]}"
            else:
                invalid = {**sample, "__unexpected_contract_field__": True}
                negative_id = f"{operation.id}.negative.unexpected_field"
                expected = "local schema rejects an undeclared input field"
            cases.append(
                ConnectorContractCase(
                    id=negative_id,
                    operation_id=operation.id,
                    kind="negative",
                    expected=expected,
                    generated_input=invalid,
                )
            )
        return cases

    async def run_contracts(
        self,
        generation_id: str,
        request: ConnectorContractRunRequest,
    ) -> ConnectorContractRun:
        generation = await self.get_generation(generation_id)
        if generation.evidence_stale:
            raise ValueError("source document changed; regenerate before running contracts")
        all_cases = await self.generate_contract_cases(generation_id)
        selected = set(request.operation_ids)
        cases = [item for item in all_cases if not selected or item.operation_id in selected]
        results: list[ConnectorContractCaseResult] = []
        started = time.perf_counter()
        first_valid: float | None = None
        manifest = generation.manifest
        profile = manifest.deployment_profiles[0]
        binding = ConnectorTenantBinding(
            connector_id=manifest.connector_id,
            connector_version=manifest.version,
            tenant_id=request.owner_id,
            external_tenant_id=request.external_tenant_id,
            profile_id=profile.id,
            secret_ref=request.secret_ref or f"secret://{request.owner_id}/missing-contract-secret",
            application_ids=["openapi-contract-test"],
            allowed_operations=[item.id for item in manifest.operations],
            subjects=[
                {
                    "external_subject": "contract-test",
                    "actor_id": "contract-test",
                    "roles": ["contract-test"],
                }
            ],
        )
        for case in cases:
            operation = manifest.operation(case.operation_id)
            payload = (
                request.sample_inputs.get(case.operation_id, case.generated_input)
                if case.kind == "positive"
                else case.generated_input
            )
            case_started = time.perf_counter()
            executed_input_evidence = self._payload_evidence(payload)
            if case.kind == "negative":
                try:
                    operation.request_schema.validate_payload(
                        payload, label=f"{operation.id} request"
                    )
                except ValueError as error:
                    results.append(
                        ConnectorContractCaseResult(
                            case=case,
                            status="passed",
                            actual=str(error),
                            executed_input_evidence=executed_input_evidence,
                            duration_ms=(time.perf_counter() - case_started) * 1000,
                        )
                    )
                else:
                    results.append(
                        ConnectorContractCaseResult(
                            case=case,
                            status="failed",
                            actual="invalid input was accepted",
                            executed_input_evidence=executed_input_evidence,
                            duration_ms=(time.perf_counter() - case_started) * 1000,
                        )
                    )
                continue
            if operation.mutating and not request.allow_mutating_operations:
                results.append(
                    ConnectorContractCaseResult(
                        case=case,
                        status="skipped",
                        actual="mutating contract requires explicit allow_mutating_operations",
                        executed_input_evidence=executed_input_evidence,
                        duration_ms=0,
                    )
                )
                continue
            if operation.mutating and profile.environment in {"live", "private"}:
                results.append(
                    ConnectorContractCaseResult(
                        case=case,
                        status="unsupported",
                        actual="automatic mutation contracts are restricted to mock/test deployments",
                        executed_input_evidence=executed_input_evidence,
                        duration_ms=0,
                    )
                )
                continue
            response: Any | None = None
            try:
                operation.request_schema.validate_payload(payload, label=f"{operation.id} request")
                response = await self.connectors._call_adapter(
                    manifest=manifest,
                    operation=operation,
                    profile=profile,
                    binding=binding,
                    request=ConnectorExecutionRequest(
                        connector_id=manifest.connector_id,
                        connector_version=manifest.version,
                        tenant_id=request.owner_id,
                        actor_id="contract-test",
                        actor_roles=["contract-test"],
                        profile_id=profile.id,
                        operation_id=operation.id,
                        payload=payload,
                        idempotency_key=f"contract-{uuid4()}",
                    ),
                    payload=payload,
                )
                self.connectors.validate_operation_response(operation, response)
            except Exception as error:
                status: Literal["failed", "blocked_by_environment"] = (
                    "blocked_by_environment" if self._environment_error(error) else "failed"
                )
                results.append(
                    ConnectorContractCaseResult(
                        case=case,
                        status=status,
                        actual=str(error),
                        executed_input_evidence=executed_input_evidence,
                        response_evidence=self._response_evidence(response)
                        if response is not None
                        else {},
                        duration_ms=(time.perf_counter() - case_started) * 1000,
                    )
                )
            else:
                duration = (time.perf_counter() - case_started) * 1000
                results.append(
                    ConnectorContractCaseResult(
                        case=case,
                        status="passed",
                        actual="status, content type, and response schema matched",
                        executed_input_evidence=executed_input_evidence,
                        response_evidence=self._response_evidence(response),
                        duration_ms=duration,
                    )
                )
                if first_valid is None:
                    first_valid = (time.perf_counter() - started) * 1000
        counts = {
            status: sum(item.status == status for item in results)
            for status in ["passed", "failed", "skipped", "unsupported", "blocked_by_environment"]
        }
        positive_results = [item for item in results if item.case.kind == "positive"]
        if any(item.status == "failed" for item in results):
            status: Literal["passed", "failed", "partial", "blocked_by_environment"] = "failed"
        elif results and all(item.status == "passed" for item in results):
            status = "passed"
        elif positive_results and all(
            item.status == "blocked_by_environment" for item in positive_results
        ):
            status = "blocked_by_environment"
        else:
            status = "partial"
        run = ConnectorContractRun(
            id=str(uuid4()),
            generation_id=generation_id,
            source_digest=generation.provenance.source_digest,
            overlay_digest=generation.provenance.schema_overlay.overlay_digest,
            effective_contract_digest=(
                generation.provenance.schema_overlay.effective_contract_digest
            ),
            operation_contract_overlay=(
                generation.provenance.operation_contract_overlay
            ),
            operation_semantics_overlay=(
                generation.provenance.operation_semantics_overlay
            ),
            status=status,
            results=results,
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            unsupported=counts["unsupported"],
            blocked_by_environment=counts["blocked_by_environment"],
            attempts=len(positive_results),
            test_ms=(time.perf_counter() - started) * 1000,
            time_to_first_valid_contract_ms=first_valid,
        )
        await asyncio.to_thread(self._save_contract_run_sync, run)
        return run

    def _save_contract_run_sync(self, run: ConnectorContractRun) -> None:
        with self.storage._connect() as conn:
            conn.execute(
                "INSERT INTO openapi_connector_contract_runs VALUES(?,?,?,?,?)",
                (
                    run.id,
                    run.generation_id,
                    run.source_digest,
                    run.model_dump_json(),
                    run.created_at,
                ),
            )

    async def list_contract_runs(self, generation_id: str) -> list[ConnectorContractRun]:
        return await asyncio.to_thread(self._list_contract_runs_sync, generation_id)

    def _list_contract_runs_sync(self, generation_id: str) -> list[ConnectorContractRun]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM openapi_connector_contract_runs WHERE generation_id=? ORDER BY created_at DESC",
                (generation_id,),
            ).fetchall()
        return [ConnectorContractRun.model_validate_json(row["record_json"]) for row in rows]

    async def register_verified(self, generation_id: str) -> ConnectorManifest:
        generation = await self.get_generation(generation_id)
        if generation.evidence_stale:
            raise ValueError("source document changed; contract evidence is stale")
        runs = await self.list_contract_runs(generation_id)
        if not runs or runs[0].status != "passed":
            raise ValueError("latest contract run must pass before registration")
        overlay = generation.provenance.schema_overlay
        if overlay.action_count and (
            runs[0].source_digest != generation.provenance.source_digest
            or runs[0].overlay_digest != overlay.overlay_digest
            or runs[0].effective_contract_digest
            != overlay.effective_contract_digest
        ):
            raise ValueError(
                "latest contract run does not match the effective overlaid contract"
            )
        operation_overlay = generation.provenance.operation_contract_overlay
        if operation_overlay.operation_count and (
            runs[0].source_digest != generation.provenance.source_digest
            or runs[0].operation_contract_overlay != operation_overlay
        ):
            raise ValueError(
                "latest contract run does not match the effective operation contract overlay"
            )
        semantics_overlay = generation.provenance.operation_semantics_overlay
        if semantics_overlay.operation_count and (
            runs[0].source_digest != generation.provenance.source_digest
            or runs[0].operation_semantics_overlay != semantics_overlay
        ):
            raise ValueError(
                "latest contract run does not match the effective operation "
                "semantics overlay"
            )
        expected_case_ids = {
            item.id for item in await self.generate_contract_cases(generation_id)
        }
        passed_case_ids = {
            item.case.id for item in runs[0].results if item.status == "passed"
        }
        missing_case_ids = sorted(expected_case_ids - passed_case_ids)
        if missing_case_ids:
            raise ValueError(
                "latest contract run must pass every generated positive and negative "
                f"case before registration; missing={missing_case_ids}"
            )
        saved = await self.connectors.register_manifest(generation.manifest)
        await asyncio.to_thread(self._mark_generation_status_sync, generation_id, "registered")
        return saved

    def _mark_generation_status_sync(self, generation_id: str, status: str) -> None:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM openapi_connector_generations WHERE id=?", (generation_id,)
            ).fetchone()
            if not row:
                raise KeyError(generation_id)
            current = OpenAPIConnectorGeneration.model_validate_json(row["record_json"])
            updated = current.model_copy(update={"status": status})
            conn.execute(
                "UPDATE openapi_connector_generations SET record_json=? WHERE id=?",
                (updated.model_dump_json(), generation_id),
            )

    def _sample_payload(self, schema: dict[str, Any]) -> dict[str, Any]:
        value = self._sample_value(schema)
        return value if isinstance(value, dict) else {}

    def _sample_value(
        self,
        schema: dict[str, Any],
        *,
        include_optional: bool = False,
    ) -> Any:
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        if "const" in schema:
            return schema["const"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        sample: Any | None = None
        raw_type = schema.get("type")
        if raw_type == "object" or "properties" in schema:
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            sample = {
                name: self._sample_value(item, include_optional=include_optional)
                for name, item in properties.items()
                if isinstance(item, dict)
                and (
                    include_optional
                    or name in required
                    or "example" in item
                    or "default" in item
                    or "const" in item
                )
            }
        elif raw_type == "array" or "items" in schema:
            items = schema.get("items", {})
            sample = (
                [self._sample_value(items, include_optional=include_optional)]
                if isinstance(items, dict)
                else []
            )
        for candidate in schema.get("allOf", []):
            if isinstance(candidate, dict):
                sample = self._merge_sample_values(
                    sample,
                    self._sample_value(candidate, include_optional=include_optional),
                )
        for keyword in ("oneOf", "anyOf"):
            candidates = schema.get(keyword)
            if not isinstance(candidates, list) or not candidates:
                continue
            generated: list[Any] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                value = self._merge_sample_values(
                    sample,
                    self._sample_value(candidate, include_optional=True),
                )
                generated.append(value)
                try:
                    ConnectorObjectSchema._validate_json_schema(
                        value,
                        schema,
                        label="generated sample",
                    )
                except ValueError:
                    continue
                sample = value
                break
            else:
                if generated:
                    sample = generated[0]
        if sample is not None:
            return sample
        schema_type = OpenAPIConnectorGenerator._schema_type(schema)
        return {
            "string": "example",
            "integer": 1,
            "number": 1.0,
            "boolean": True,
            "object": {},
            "array": [],
        }[schema_type]

    def _merge_sample_values(self, left: Any, right: Any) -> Any:
        if left is None:
            return right
        if right is None:
            return left
        if isinstance(left, dict) and isinstance(right, dict):
            merged = dict(left)
            for key, value in right.items():
                merged[key] = (
                    self._merge_sample_values(merged[key], value)
                    if key in merged
                    else value
                )
            return merged
        if left == right:
            return left
        return right

    def _payload_evidence(self, payload: Any) -> dict[str, Any]:
        redacted_fields: list[str] = []
        preview = self._redacted_preview(payload, path="$", redacted_fields=redacted_fields)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return {
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "canonical_bytes": len(canonical),
            "redaction_policy_version": "openapi-contract-evidence-v1",
            "redacted_fields": redacted_fields,
            "body_preview": preview,
        }

    def _response_evidence(self, response: Any) -> dict[str, Any]:
        payload_evidence = self._payload_evidence(response)
        evidence: dict[str, Any] = {
            **payload_evidence,
            "root_type": type(response).__name__,
        }
        if isinstance(response, dict):
            evidence["top_level_keys"] = sorted(str(key) for key in response)[:100]
            identity = {
                key: response[key]
                for key in ("id", "pk", "uuid", "external_id")
                if key in response and isinstance(response[key], (str, int))
            }
            if identity:
                evidence["identity"] = identity
        elif isinstance(response, list):
            evidence["item_count"] = len(response)
        return evidence

    def _redacted_preview(
        self,
        value: Any,
        *,
        path: str,
        redacted_fields: list[str],
        depth: int = 0,
    ) -> Any:
        if depth >= 8:
            return "<depth-limit>"
        if isinstance(value, dict):
            preview: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100:
                    preview["<truncated-fields>"] = len(value) - 100
                    break
                item_path = f"{path}.{key}"
                if any(
                    token in str(key).casefold()
                    for token in (
                        "key",
                        "secret",
                        "token",
                        "password",
                        "authorization",
                        "cookie",
                        "credential",
                        "content_base64",
                    )
                ):
                    redacted_fields.append(item_path)
                    preview[str(key)] = "***"
                else:
                    preview[str(key)] = self._redacted_preview(
                        item,
                        path=item_path,
                        redacted_fields=redacted_fields,
                        depth=depth + 1,
                    )
            return preview
        if isinstance(value, list):
            return [
                self._redacted_preview(
                    item,
                    path=f"{path}[{index}]",
                    redacted_fields=redacted_fields,
                    depth=depth + 1,
                )
                for index, item in enumerate(value[:20])
            ] + ([f"<truncated-items:{len(value) - 20}>"] if len(value) > 20 else [])
        if isinstance(value, str) and len(value) > 512:
            return value[:512] + "<truncated>"
        return value

    @staticmethod
    def _environment_error(error: Exception) -> bool:
        if isinstance(error, httpx.RequestError):
            return True
        text = str(error).casefold()
        markers = [
            "secret reference",
            "secret does not exist",
            "missing-contract-secret",
            "outside its allowlist",
            "network egress",
            "name or service",
            "nodename",
        ]
        return any(marker in text for marker in markers)
