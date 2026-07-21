from __future__ import annotations

import asyncio
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
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .connector_sdk import (
    ConnectorDeploymentProfile,
    ConnectorExecutionRequest,
    ConnectorManifest,
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


class OpenAPICapabilityGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^IF-(0[1-9]|1[0-4])$")
    capability: str
    location: str
    message: str
    fatal: bool = False


class OpenAPISourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["inline", "url"]
    source_url: str = ""
    source_digest: str
    openapi_version: str
    title: str
    document_version: str
    size_bytes: int
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

    @model_validator(mode="after")
    def exactly_one_source(self) -> OpenAPIConnectorGenerationRequest:
        if bool(self.document) == bool(self.document_url):
            raise ValueError("provide exactly one of document or document_url")
        return self


class OpenAPIConnectorGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    connector_id: str
    version: int
    status: Literal["generated", "verified", "registered"] = "generated"
    provenance: OpenAPISourceProvenance
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


class OpenAPIMaterialLoader:
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
        gaps: list[OpenAPICapabilityGap] = []
        resolved = self._resolve_refs(document, document, "$", (), gaps)
        self._inspect_unsupported(resolved, gaps)
        info = resolved.get("info", {}) if isinstance(resolved.get("info"), dict) else {}
        provenance = OpenAPISourceProvenance(
            source_kind=source_kind,
            source_url=source_url,
            source_digest=hashlib.sha256(raw).hexdigest(),
            openapi_version=version,
            title=str(info.get("title") or request.connector_id),
            document_version=str(info.get("version") or "unknown"),
            size_bytes=len(raw),
        )
        return resolved, provenance, self._unique_gaps(gaps)

    async def _fetch(self, request: OpenAPIConnectorGenerationRequest) -> bytes:
        parsed = urlsplit(request.document_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        allowed = {item.casefold().rstrip(".") for item in request.allowed_document_hosts}
        if parsed.scheme not in {"https", "http"} or not host:
            raise self._error("IF-01", "document_url", "$", "document URL must be HTTP(S)")
        if parsed.scheme == "http" and not request.allow_insecure_document_http:
            raise self._error("IF-01", "document_url", "$", "insecure HTTP document URL is disabled")
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
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as client:
            async with client.stream(
                "GET",
                request.document_url,
                headers={"Accept": "application/json, application/yaml, text/yaml"},
            ) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > MAX_OPENAPI_BYTES:
                    raise self._error(
                        "IF-01", "document_size", "$", "OpenAPI document exceeds 5 MB"
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_OPENAPI_BYTES:
                        raise self._error(
                            "IF-01", "document_size", "$", "OpenAPI document exceeds 5 MB"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)

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
            raise self._error("IF-01", "document_root", "$", "OpenAPI document root must be an object")
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

    def _inspect_unsupported(self, document: dict[str, Any], gaps: list[OpenAPICapabilityGap]) -> None:
        if document.get("webhooks"):
            gaps.append(self._gap("IF-03", "webhooks", "$.webhooks", "webhooks require a later transport stage"))
        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(operation, dict):
                    continue
                location = f"$.paths.{path}.{method}"
                if operation.get("callbacks"):
                    gaps.append(self._gap("IF-03", "callbacks", location, "callbacks are recorded but not generated"))

    @staticmethod
    def _gap(code: str, capability: str, location: str, message: str, *, fatal: bool = False) -> OpenAPICapabilityGap:
        return OpenAPICapabilityGap(code=code, capability=capability, location=location, message=message, fatal=fatal)

    def _error(self, code: str, capability: str, location: str, message: str) -> OpenAPIMaterialError:
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
        operations: list[ConnectorOperation] = []
        mapped_fields = 0
        total_fields = 0
        discovered = 0
        used_ids: set[str] = set()
        global_security = document.get("security", [])
        for path, path_item in document.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue
            path_parameters = path_item.get("parameters", [])
            for method, raw_operation in path_item.items():
                if method not in HTTP_METHODS or not isinstance(raw_operation, dict):
                    continue
                discovered += 1
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
                mapped_fields += mapped
                total_fields += total
        if not operations:
            raise OpenAPIMaterialError(
                OpenAPICapabilityGap(
                    code="IF-01",
                    capability="operations",
                    location="$.paths",
                    message="OpenAPI document contains no supported REST operations",
                    fatal=True,
                )
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
            source_provenance=provenance.model_dump(mode="json"),
        )
        return OpenAPIConnectorGeneration(
            id=str(uuid4()),
            connector_id=request.connector_id,
            version=request.version,
            provenance=provenance,
            manifest=manifest,
            gaps=OpenAPIMaterialLoader._unique_gaps(gaps),
            discovered_operation_count=discovered,
            generated_operation_count=len(operations),
            mapped_field_count=mapped_fields,
            total_field_count=total_fields,
            parse_ms=parse_ms,
            generate_ms=(time.perf_counter() - started) * 1000,
        )

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
        for index, parameter in enumerate(raw_parameters):
            if not isinstance(parameter, dict):
                continue
            total += 1
            wire_name = str(parameter.get("name") or "")
            parameter_location = str(parameter.get("in") or "")
            schema = parameter.get("schema", {})
            if not wire_name or parameter_location not in {"path", "query", "header", "cookie"} or not isinstance(schema, dict):
                gaps.append(OpenAPIMaterialLoader._gap("IF-04", "parameter", f"{location}.parameters[{index}]", "unsupported parameter declaration"))
                continue
            if not self._schema_supported(schema, f"{location}.parameters[{index}].schema", gaps):
                continue
            style = str(parameter.get("style") or ("simple" if parameter_location in {"path", "header"} else "form"))
            explode = bool(parameter.get("explode", style == "form"))
            if style not in {"simple", "form"}:
                gaps.append(OpenAPIMaterialLoader._gap("IF-04", "parameter_serialization", f"{location}.parameters[{index}]", f"parameter style {style!r} is unsupported"))
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
            media = content.get("application/json") if isinstance(content, dict) else None
            if not isinstance(media, dict) or not isinstance(media.get("schema", {}), dict):
                gaps.append(OpenAPIMaterialLoader._gap("IF-05", "request_media_type", f"{location}.requestBody", "only application/json request bodies are generated"))
                return None
            body_schema = self._schema_for_direction(media.get("schema", {}), request=True)
            body_field_count = self._schema_field_count(body_schema)
            total += body_field_count
            if not self._schema_supported(body_schema, f"{location}.requestBody.schema", gaps):
                return None
            request_properties["body"] = body_schema
            if body.get("required"):
                required_inputs.append("body")
            request_body = ConnectorRequestBody(required=bool(body.get("required")))
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
        security_requirements = [
            [self._identifier(str(key), prefix="security") for key in item]
            for item in security
            if isinstance(item, dict)
        ] if isinstance(security, list) else []
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

    def _responses(
        self,
        responses: Any,
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> tuple[dict[str, Any] | None, str, list[int], list[str], dict[str, str]]:
        if not isinstance(responses, dict):
            gaps.append(OpenAPIMaterialLoader._gap("IF-14", "responses", f"{location}.responses", "responses must be an object"))
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
            gaps.append(OpenAPIMaterialLoader._gap("IF-14", "success_response", f"{location}.responses", "operation has no explicit 2xx response"))
            return None, "object", [], [], errors
        success.sort(key=lambda item: item[0])
        status, selected = success[0]
        content = selected.get("content", {})
        if status == 204 or not content:
            return {"type": "object", "properties": {}, "additionalProperties": True}, "object", [item[0] for item in success], [], errors
        if not isinstance(content, dict) or "application/json" not in content:
            gaps.append(OpenAPIMaterialLoader._gap("IF-05", "response_media_type", f"{location}.responses.{status}", "only application/json responses are generated"))
            return None, "object", [], [], errors
        media = content["application/json"]
        schema = media.get("schema", {}) if isinstance(media, dict) else {}
        if not isinstance(schema, dict):
            gaps.append(OpenAPIMaterialLoader._gap("IF-14", "response_schema", f"{location}.responses.{status}", "response schema must be an object"))
            return None, "object", [], [], errors
        root_type = self._schema_type(schema)
        return schema, root_type, [item[0] for item in success], ["application/json"], errors

    def _security_schemes(self, document: dict[str, Any], gaps: list[OpenAPICapabilityGap]) -> list[ConnectorSecurityScheme]:
        raw = document.get("components", {}).get("securitySchemes", {}) if isinstance(document.get("components"), dict) else {}
        result: list[ConnectorSecurityScheme] = []
        if not isinstance(raw, dict):
            return result
        for scheme_id, scheme in raw.items():
            if not isinstance(scheme, dict):
                continue
            normalized_id = self._identifier(str(scheme_id), prefix="security")
            if scheme.get("type") == "http" and str(scheme.get("scheme", "")).casefold() in {"bearer", "basic"}:
                result.append(
                    ConnectorSecurityScheme(
                        id=normalized_id,
                        type="http",
                        scheme=str(scheme.get("scheme", "")).casefold(),
                    )
                )
            elif scheme.get("type") == "apiKey" and scheme.get("in") in {"header", "query", "cookie"}:
                result.append(ConnectorSecurityScheme(id=normalized_id, type="apiKey", location=scheme["in"], wire_name=str(scheme.get("name") or "Authorization")))
            else:
                gaps.append(OpenAPIMaterialLoader._gap("IF-07", "security_scheme", f"$.components.securitySchemes.{scheme_id}", f"security scheme {scheme.get('type')!r}/{scheme.get('scheme')!r} is unsupported"))
        return result

    def _schema_supported(
        self,
        schema: dict[str, Any],
        location: str,
        gaps: list[OpenAPICapabilityGap],
    ) -> bool:
        for keyword in ("oneOf", "anyOf", "allOf", "not"):
            if keyword in schema:
                gaps.append(
                    OpenAPIMaterialLoader._gap(
                        "IF-06",
                        "schema_composition",
                        location,
                        f"JSON Schema keyword {keyword} is not generated",
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
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and properties:
            return sum(
                max(1, self._schema_field_count(child)) if isinstance(child, dict) else 1
                for child in properties.values()
            )
        items = schema.get("items")
        if isinstance(items, dict):
            return max(1, self._schema_field_count(items))
        return 1

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
        properties = schema.get("properties")
        if isinstance(properties, dict):
            filtered: dict[str, Any] = {}
            for name, child in properties.items():
                child_schema = child if isinstance(child, dict) else {}
                excluded = bool(child_schema.get("readOnly" if request else "writeOnly"))
                if excluded:
                    continue
                filtered[name] = self._schema_for_direction(child_schema, request=request)
            normalized["properties"] = filtered
            required = schema.get("required")
            if isinstance(required, list):
                normalized["required"] = [name for name in required if name in filtered]
        items = schema.get("items")
        if isinstance(items, dict):
            normalized["items"] = self._schema_for_direction(items, request=request)
        return normalized

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
            for name, field_schema in list(properties.items())[:100]:
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
            additional_properties=schema.get("additionalProperties", True) is not False if isinstance(schema, dict) else True,
            json_schema=schema or None,
        )

    @staticmethod
    def _schema_type(schema: dict[str, Any]) -> Literal["string", "number", "integer", "boolean", "object", "array"]:
        raw = schema.get("type")
        if isinstance(raw, list):
            raw = next((item for item in raw if item != "null"), None)
        if raw in {"string", "number", "integer", "boolean", "object", "array"}:
            return raw
        if "properties" in schema:
            return "object"
        if "items" in schema:
            return "array"
        return "string"

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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS openapi_connector_generations (
                  id TEXT PRIMARY KEY,
                  connector_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  source_digest TEXT NOT NULL,
                  record_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(connector_id,version,source_digest)
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

    async def generate(self, request: OpenAPIConnectorGenerationRequest) -> OpenAPIConnectorGeneration:
        started = time.perf_counter()
        document, provenance, gaps = await self.loader.load(request)
        parse_ms = (time.perf_counter() - started) * 1000
        generated = self.generator.generate(document, request, provenance, gaps, parse_ms=parse_ms)
        async with self._lock:
            return await asyncio.to_thread(self._save_generation_sync, generated)

    def _save_generation_sync(self, generation: OpenAPIConnectorGeneration) -> OpenAPIConnectorGeneration:
        with self.storage._connect() as conn:
            existing = conn.execute(
                "SELECT record_json FROM openapi_connector_generations WHERE connector_id=? AND version=? AND source_digest=?",
                (generation.connector_id, generation.version, generation.provenance.source_digest),
            ).fetchone()
            if existing:
                return OpenAPIConnectorGeneration.model_validate_json(existing["record_json"])
            conn.execute(
                "INSERT INTO openapi_connector_generations VALUES(?,?,?,?,?,?)",
                (
                    generation.id,
                    generation.connector_id,
                    generation.version,
                    generation.provenance.source_digest,
                    generation.model_dump_json(),
                    generation.created_at,
                ),
            )
        return generation

    async def list_generations(self) -> list[OpenAPIConnectorGeneration]:
        return await asyncio.to_thread(self._list_generations_sync)

    def _list_generations_sync(self) -> list[OpenAPIConnectorGeneration]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM openapi_connector_generations ORDER BY created_at DESC"
            ).fetchall()
        items = [OpenAPIConnectorGeneration.model_validate_json(row["record_json"]) for row in rows]
        latest_digest: dict[str, str] = {}
        for item in items:
            latest_digest.setdefault(item.connector_id, item.provenance.source_digest)
        return [
            item.model_copy(update={"evidence_stale": latest_digest[item.connector_id] != item.provenance.source_digest})
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
                "SELECT source_digest FROM openapi_connector_generations WHERE connector_id=? ORDER BY created_at DESC LIMIT 1",
                (item.connector_id,),
            ).fetchone()
        stale = bool(newest and newest["source_digest"] != item.provenance.source_digest)
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
            subjects=[{"external_subject": "contract-test", "actor_id": "contract-test", "roles": ["contract-test"]}],
        )
        for case in cases:
            operation = manifest.operation(case.operation_id)
            payload = (
                request.sample_inputs.get(case.operation_id, case.generated_input)
                if case.kind == "positive"
                else case.generated_input
            )
            case_started = time.perf_counter()
            if case.kind == "negative":
                try:
                    operation.request_schema.validate_payload(payload, label=f"{operation.id} request")
                except ValueError as error:
                    results.append(ConnectorContractCaseResult(case=case, status="passed", actual=str(error), duration_ms=(time.perf_counter() - case_started) * 1000))
                else:
                    results.append(ConnectorContractCaseResult(case=case, status="failed", actual="invalid input was accepted", duration_ms=(time.perf_counter() - case_started) * 1000))
                continue
            if operation.mutating and not request.allow_mutating_operations:
                results.append(ConnectorContractCaseResult(case=case, status="skipped", actual="mutating contract requires explicit allow_mutating_operations", duration_ms=0))
                continue
            if operation.mutating and profile.environment in {"live", "private"}:
                results.append(ConnectorContractCaseResult(case=case, status="unsupported", actual="automatic mutation contracts are restricted to mock/test deployments", duration_ms=0))
                continue
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
                status: Literal["failed", "blocked_by_environment"] = "blocked_by_environment" if self._environment_error(error) else "failed"
                results.append(ConnectorContractCaseResult(case=case, status=status, actual=str(error), duration_ms=(time.perf_counter() - case_started) * 1000))
            else:
                duration = (time.perf_counter() - case_started) * 1000
                results.append(ConnectorContractCaseResult(case=case, status="passed", actual="status, content type, and response schema matched", duration_ms=duration))
                if first_valid is None:
                    first_valid = (time.perf_counter() - started) * 1000
        counts = {status: sum(item.status == status for item in results) for status in ["passed", "failed", "skipped", "unsupported", "blocked_by_environment"]}
        positive_results = [item for item in results if item.case.kind == "positive"]
        if any(item.status == "failed" for item in results):
            status: Literal["passed", "failed", "partial", "blocked_by_environment"] = "failed"
        elif results and all(item.status == "passed" for item in results):
            status = "passed"
        elif positive_results and all(item.status == "blocked_by_environment" for item in positive_results):
            status = "blocked_by_environment"
        else:
            status = "partial"
        run = ConnectorContractRun(
            id=str(uuid4()),
            generation_id=generation_id,
            source_digest=generation.provenance.source_digest,
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
                (run.id, run.generation_id, run.source_digest, run.model_dump_json(), run.created_at),
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
        saved = await self.connectors.register_manifest(generation.manifest)
        await asyncio.to_thread(self._mark_generation_status_sync, generation_id, "registered")
        return saved

    def _mark_generation_status_sync(self, generation_id: str, status: str) -> None:
        with self.storage._connect() as conn:
            row = conn.execute("SELECT record_json FROM openapi_connector_generations WHERE id=?", (generation_id,)).fetchone()
            if not row:
                raise KeyError(generation_id)
            current = OpenAPIConnectorGeneration.model_validate_json(row["record_json"])
            updated = current.model_copy(update={"status": status})
            conn.execute("UPDATE openapi_connector_generations SET record_json=? WHERE id=?", (updated.model_dump_json(), generation_id))

    def _sample_payload(self, schema: dict[str, Any]) -> dict[str, Any]:
        value = self._sample_value(schema)
        return value if isinstance(value, dict) else {}

    def _sample_value(self, schema: dict[str, Any]) -> Any:
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        schema_type = OpenAPIConnectorGenerator._schema_type(schema)
        if schema_type == "object":
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            return {
                name: self._sample_value(item)
                for name, item in properties.items()
                if isinstance(item, dict) and (name in required or "example" in item or "default" in item)
            }
        if schema_type == "array":
            items = schema.get("items", {})
            return [self._sample_value(items)] if isinstance(items, dict) else []
        return {"string": "example", "integer": 1, "number": 1.0, "boolean": True}[schema_type]

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
