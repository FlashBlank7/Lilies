from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .blocks import IterationConfig, LoopConfig
from .capability_contracts import CapabilityBuildContract
from .lilies_platform_contract import (
    DEFAULT_ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_CHUNK_BYTES,
    build_platform_contract,
    public_block_catalog,
    public_block_manual,
    public_digest,
    public_runtime_tool_catalog,
)
from .platform_blackbox_auth import (
    BlackboxAuthorizationRequest,
    BlackboxRequestState,
    PlatformBlackboxApplicationDenied,
    PlatformBlackboxAuthError,
    PlatformBlackboxAuthenticationError,
    PlatformBlackboxAuthorizationError,
    PlatformBlackboxCredentialExpired,
    PlatformBlackboxCredentialRevoked,
    PlatformBlackboxIdempotencyConflict,
    PlatformBlackboxOperation,
    PlatformBlackboxRequestConflict,
    PlatformBlackboxScopeDenied,
    PlatformBlackboxStoreError,
    TaskCredentialRecord,
)
from .platform_blackbox_artifacts import (
    ArtifactBinding,
    ArtifactReadRequest,
    ArtifactRegistrationRequest,
    PlatformBlackboxArtifactConflict,
    PlatformBlackboxArtifactError,
    PlatformBlackboxArtifactIntegrityError,
    PlatformBlackboxArtifactNotFound,
    PlatformBlackboxArtifactPathUnsafe,
    PlatformBlackboxArtifactRangeInvalid,
    PlatformBlackboxArtifactScopeDenied,
    PlatformBlackboxArtifactStoreError,
    PlatformBlackboxArtifactTooLarge,
)
from .platform_contract_version import platform_contract_schema_digest
from .models import AgentSpec
from .sandbox import SandboxError
from .workflow_models import (
    ApplicationCreateRequest,
    DraftOperation,
    EdgeSpec,
    NodeSpec,
    PublishApplicationRequest,
    ResumeRunRequest,
    WorkflowTestCase,
    WorkflowRunRequest,
)
from .workflow_runtime import (
    BLACKBOX_RUNTIME_TOOL_ALLOWLIST,
    NestedWorkflowScopeDenied,
    WorkflowRuntimeNetworkScopeDenied,
    WorkflowRuntimeConnectorScopeDenied,
    WorkflowRuntimeModelScopeDenied,
    WorkflowRuntimePayloadLimitExceeded,
    WorkflowRuntimePermissionScopeDenied,
    WorkflowRuntimeSecretScopeDenied,
    WorkflowRuntimeToolScopeDenied,
    WorkflowRuntimeWriteLimitExceeded,
    WorkflowWorkspaceBoundaryViolation,
)
from .workflow_storage import PublishGateError, RevisionConflict


_ZERO_DIGEST = "sha256:" + "0" * 64
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?(?:thinking|reasoning)|"
    r"reasoning[_-]?content|secret|(?:^|[_-])token$|"
    r"(?:^|[_-])(?:access|auth|bearer|session|api|refresh|identity|csrf)[_-]?token(?:$|[_-])|"
    r"(?:^|_)(?:cwd|root_path|workspace_path|file_path|"
    r"database_path|source_path)(?:$|_))",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|\blpt_[0-9a-f]{32}_[A-Za-z0-9_-]+|"
    r"\bsk-[A-Za-z0-9_-]{12,})"
)
_PRIVATE_REASONING_KEY_RE = re.compile(
    r"^(?:thinking|signature|raw[_-]?blocks?|private[_-]?(?:thinking|reasoning)|"
    r"reasoning|reasoning[_-]?content)$",
    re.IGNORECASE,
)


def _runtime_tool_policy(
    credential: TaskCredentialRecord,
) -> frozenset[str]:
    return BLACKBOX_RUNTIME_TOOL_ALLOWLIST if credential.file_access else frozenset()


def _runtime_network_policy(
    credential: TaskCredentialRecord,
) -> frozenset[str]:
    if not credential.connector_access:
        return frozenset()
    return frozenset(host.casefold() for host in credential.allowed_network_hosts)


def _runtime_connector_policy(
    credential: TaskCredentialRecord,
) -> frozenset[str]:
    if not credential.connector_access:
        return frozenset()
    return frozenset(
        {
            *credential.readable_host_objects,
            *credential.writable_host_operations,
            *credential.compensation_actions,
        }
    )


def _governed_host_actions(credential: TaskCredentialRecord) -> bool:
    return (
        credential.allowed_actions_digest is not None
        and credential.budget_digest is not None
    )


_EMBEDDED_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s\"'<>\[\]{}(),;]+)"
)
_EMBEDDED_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9/:])/(?:[^\s\"'<>\[\]{}(),;]+)")
_TASK_CONTRACT_ROUTES = (
    ("GET", re.compile(r"^/api/v1/lilies/platform-contract$")),
    ("GET", re.compile(r"^/api/v1/lilies/blocks$")),
    ("GET", re.compile(r"^/api/v1/lilies/blocks/[^/]+$")),
    ("GET", re.compile(r"^/api/v1/lilies/tools$")),
    ("POST", re.compile(r"^/api/v1/lilies/applications$")),
    ("GET", re.compile(r"^/api/v1/lilies/applications/[^/]+$")),
    ("GET", re.compile(r"^/api/v1/lilies/applications/[^/]+/draft$")),
    ("POST", re.compile(r"^/api/v1/lilies/applications/[^/]+/draft$")),
    ("POST", re.compile(r"^/api/v1/lilies/applications/[^/]+/tests/run$")),
    ("POST", re.compile(r"^/api/v1/lilies/applications/[^/]+/runs$")),
    ("GET", re.compile(r"^/api/v1/lilies/runs/[^/]+$")),
    ("POST", re.compile(r"^/api/v1/lilies/runs/[^/]+/resume$")),
    ("POST", re.compile(r"^/api/v1/lilies/runs/[^/]+/cancel$")),
    ("GET", re.compile(r"^/api/v1/lilies/runs/[^/]+/trace$")),
    ("GET", re.compile(r"^/api/v1/lilies/runs/[^/]+/artifacts/[^/]+$")),
    ("POST", re.compile(r"^/api/v1/lilies/applications/[^/]+/versions$")),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _CorrelatedBody(_StrictModel):
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=128)


class _ApplicationPathBody(_CorrelatedBody):
    application_id: UUID | None = None


class _RunPathBody(_CorrelatedBody):
    run_id: UUID | None = None


class ApplicationCreateBody(_CorrelatedBody):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1_000)
    requirement: str = Field(default="", max_length=30_000)
    mode: Literal["workflow", "chat"] = "workflow"
    delivery_mode: Literal["quick", "guided", "governed"] = "guided"
    governed_hard_gate: bool = False


class _AddNodeData(_StrictModel):
    node: NodeSpec


class _UpdateNodeData(_StrictModel):
    node_id: str = Field(min_length=1, max_length=160)
    changes: dict[str, Any]
    merge_config: bool = True

    @field_validator("changes")
    @classmethod
    def validate_partial_node(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - set(NodeSpec.model_fields)
        if unknown:
            raise ValueError(f"unknown NodeSpec fields: {sorted(unknown)}")
        return {
            field: TypeAdapter(NodeSpec.model_fields[field].annotation).validate_python(
                item
            )
            for field, item in value.items()
        }


class _RemoveNodeData(_StrictModel):
    node_id: str = Field(min_length=1, max_length=160)


class _AddEdgeData(_StrictModel):
    edge: EdgeSpec


class _RemoveEdgeData(_StrictModel):
    edge_id: str = Field(min_length=1, max_length=160)


class _SetMetadataData(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1_000)
    mode: Literal["workflow", "chat"] | None = None
    delivery_mode: Literal["quick", "guided", "governed"] | None = None
    governed_hard_gate: bool | None = None
    requirement: str | None = Field(default=None, max_length=30_000)

    @model_validator(mode="before")
    @classmethod
    def require_non_null_field(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value:
            raise ValueError("set_metadata requires at least one field")
        if any(item is None for item in value.values()):
            raise ValueError("set_metadata fields cannot be null")
        return value


class _UpsertAgentData(_StrictModel):
    agent: AgentSpec


class _PublicWorkflowTestCase(WorkflowTestCase):
    model_config = ConfigDict(extra="forbid")

    @field_validator("inputs")
    @classmethod
    def reject_reserved_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _public_run_inputs(value)


class _AddTestData(_StrictModel):
    test: _PublicWorkflowTestCase


class _RemoveTestData(_StrictModel):
    test_id: str = Field(min_length=1, max_length=160)


class _SetCapabilityBuildContractData(_StrictModel):
    contract: CapabilityBuildContract


_DRAFT_DATA_MODELS: dict[str, type[_StrictModel]] = {
    "add_node": _AddNodeData,
    "update_node": _UpdateNodeData,
    "remove_node": _RemoveNodeData,
    "add_edge": _AddEdgeData,
    "remove_edge": _RemoveEdgeData,
    "set_metadata": _SetMetadataData,
    "upsert_agent": _UpsertAgentData,
    "add_test": _AddTestData,
    "remove_test": _RemoveTestData,
    "set_capability_build_contract": _SetCapabilityBuildContractData,
}


class DraftApplyBody(_ApplicationPathBody):
    expected_revision: int = Field(ge=0)
    op: Literal[
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
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_operation_data(self) -> DraftApplyBody:
        validated = _DRAFT_DATA_MODELS[self.op].model_validate(self.data)
        self.data = validated.model_dump(mode="json", exclude_none=True)
        return self


class TestsRunBody(_ApplicationPathBody):
    pass


class RunStartBody(_ApplicationPathBody):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)
    use_draft: bool = False

    @field_validator("inputs")
    @classmethod
    def reject_reserved_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _public_run_inputs(value)


class RunResumeBody(_RunPathBody):
    values: dict[str, Any]

    @field_validator("values")
    @classmethod
    def reject_reserved_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _public_run_inputs(value)


class RunCancelBody(_RunPathBody):
    pass


class PublishBody(_ApplicationPathBody):
    acknowledge_warnings: bool = False


@dataclass(frozen=True, slots=True)
class _Correlation:
    request_id: UUID
    assignment_id: UUID
    session_id: UUID
    tool_call_id: str
    idempotency_key: str
    contract_digest: str
    access_token: str


class _FacadeFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        failure_owner: str = "lilies",
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.failure_owner = failure_owner
        self.expected = expected
        self.actual = actual


def _public_run_inputs(value: dict[str, Any]) -> dict[str, Any]:
    reserved = sorted(str(key) for key in value if str(key).startswith("__"))
    if reserved:
        raise ValueError(f"reserved runtime input keys are not public: {reserved}")
    return value


def _path_body_payload(
    field: Literal["application_id", "run_id"],
    path_value: str,
    body: _ApplicationPathBody | _RunPathBody,
) -> dict[str, Any]:
    return {
        field: path_value,
        **body.model_dump(mode="json", exclude={field}),
    }


def _require_path_body_identity(
    body: _CorrelatedBody | None,
    *,
    field: Literal["application_id", "run_id"],
    path_value: UUID,
) -> None:
    supplied = getattr(body, field, None)
    if supplied is None or supplied == path_value:
        return
    raise _FacadeFailure(
        "invalid_request",
        f"{field} in the request body must match the path parameter",
        status_code=422,
        failure_owner="task_author",
        expected=str(path_value),
        actual=str(supplied),
    )


def _validate_public_node_tree(
    blocks: Any,
    node: NodeSpec,
    *,
    reject_schedule_trigger: bool = False,
) -> None:
    try:
        definition = blocks.get(node.type)
    except KeyError as error:
        raise _FacadeFailure(
            "invalid_request",
            "node type is not part of the public block catalog",
            status_code=422,
            failure_owner="task_author",
        ) from error
    if definition.block_kind == "legacy_compatibility" or not definition.available:
        raise _FacadeFailure(
            "runtime_tool_scope_denied",
            "workflow block is outside the public assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if reject_schedule_trigger and node.type == "schedule_trigger":
        raise _FacadeFailure(
            "runtime_tool_scope_denied",
            "scheduled execution cannot inherit the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    config = blocks.validate_node(node)
    if isinstance(config, (IterationConfig, LoopConfig)):
        for nested in config.workflow.nodes:
            _validate_public_node_tree(
                blocks,
                nested,
                reject_schedule_trigger=reject_schedule_trigger,
            )


def _require_blackbox_run(
    run: dict[str, Any],
    correlation: _Correlation,
    credential: TaskCredentialRecord,
) -> None:
    state = run.get("state")
    allowed_applications = {str(value) for value in credential.application_ids}
    if (
        str(run.get("application_id")) not in allowed_applications
        or state is None
        or getattr(state, "assignment_id", None) != str(correlation.assignment_id)
        or getattr(state, "session_id", None) != str(correlation.session_id)
    ):
        raise _FacadeFailure(
            "not_found",
            "the requested resource was not found",
            status_code=404,
            failure_owner="user_permission",
        )


def _operation(value: str | PlatformBlackboxOperation) -> PlatformBlackboxOperation:
    return (
        value
        if isinstance(value, PlatformBlackboxOperation)
        else PlatformBlackboxOperation(value)
    )


def _is_contract_route(method: str, path: str) -> bool:
    return any(
        method == allowed_method and pattern.fullmatch(path)
        for allowed_method, pattern in _TASK_CONTRACT_ROUTES
    )


def _error_payload(
    *,
    operation: str,
    request_id: UUID,
    status_code: int,
    contract_digest: str,
    code: str,
    message: str,
    retryable: bool = False,
    failure_owner: str = "lilies",
    expected: Any = None,
    actual: Any = None,
    evidence_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "request_id": str(request_id),
        "status_code": status_code,
        "contract_digest": contract_digest
        if _DIGEST_RE.fullmatch(contract_digest)
        else _ZERO_DIGEST,
        "data": {},
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "failure_owner": failure_owner,
            "expected": expected,
            "actual": actual,
            "evidence_ref": evidence_ref,
        },
        "evidence_refs": [evidence_ref] if evidence_ref else [],
    }


def _success_payload(
    *,
    operation: PlatformBlackboxOperation,
    correlation: _Correlation,
    status_code: int,
    contract_digest: str,
    data: Any,
    evidence_refs: list[str],
) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": operation.value,
        "request_id": str(correlation.request_id),
        "status_code": status_code,
        "contract_digest": contract_digest,
        "data": jsonable_encoder(data),
        "error": None,
        "evidence_refs": evidence_refs,
    }


def _json_response(
    payload: dict[str, Any],
    status_code: int,
    *,
    idempotent_replay: bool = False,
) -> JSONResponse:
    headers = {"X-Lilies-Idempotent-Replay": "true"} if idempotent_replay else None
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(payload),
        headers=headers,
    )


def _request_id(request: Request) -> UUID:
    supplied = request.headers.get("x-lilies-request-id")
    if not supplied:
        return uuid4()
    try:
        return UUID(supplied)
    except ValueError as error:
        raise _FacadeFailure(
            "invalid_request",
            "X-Lilies-Request-ID must be a UUID",
            status_code=422,
            expected="UUID",
            actual="invalid",
        ) from error


def _uuid_header(request: Request, name: str) -> UUID:
    supplied = request.headers.get(name)
    if not supplied:
        raise _FacadeFailure(
            "missing_correlation",
            f"{name} is required",
            status_code=422,
            failure_owner="task_author",
            expected="UUID header",
            actual="missing",
        )
    try:
        return UUID(supplied)
    except ValueError as error:
        raise _FacadeFailure(
            "invalid_correlation",
            f"{name} must be a UUID",
            status_code=422,
            failure_owner="task_author",
            expected="UUID header",
            actual="invalid",
        ) from error


def _bearer(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise _FacadeFailure(
            "authentication_failed",
            "a platform task bearer credential is required",
            status_code=401,
            failure_owner="user_permission",
            expected="Bearer task credential",
            actual="missing",
        )
    return token


def _correlation(
    request: Request,
    body: _CorrelatedBody | None,
    *,
    contract_get: bool = False,
) -> _Correlation:
    request_id = _request_id(request)
    assignment_id = _uuid_header(request, "x-lilies-assignment-id")
    session_id = _uuid_header(request, "x-lilies-session-id")
    header_tool_call = request.headers.get("x-lilies-tool-call-id")
    header_idempotency = request.headers.get("x-lilies-idempotency-key")
    body_idempotency = body.idempotency_key if body is not None else None
    if (
        header_idempotency
        and body_idempotency
        and header_idempotency != body_idempotency
    ):
        raise _FacadeFailure(
            "correlation_conflict",
            "idempotency_key body value does not match X-Lilies-Idempotency-Key",
            status_code=409,
            expected=header_idempotency,
            actual=body_idempotency,
        )
    tool_call_id = header_tool_call or ""
    idempotency_key = header_idempotency or body_idempotency or ""
    if not _CORRELATION_RE.fullmatch(tool_call_id):
        raise _FacadeFailure(
            "invalid_correlation",
            "a valid tool_call_id correlation value is required",
            status_code=422,
            failure_owner="task_author",
            expected="1-200 safe correlation characters",
            actual="missing_or_invalid",
        )
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise _FacadeFailure(
            "invalid_correlation",
            "a valid idempotency_key correlation value is required",
            status_code=422,
            failure_owner="task_author",
            expected="16-128 safe correlation characters",
            actual="missing_or_invalid",
        )
    supplied_digest = request.headers.get("x-lilies-contract-digest")
    contract_digest = supplied_digest or (_ZERO_DIGEST if contract_get else "")
    if not _DIGEST_RE.fullmatch(contract_digest):
        raise _FacadeFailure(
            "invalid_contract_digest",
            "X-Lilies-Contract-Digest must be a sha256 digest",
            status_code=422,
            failure_owner="task_author",
            expected="sha256:<64 lowercase hex characters>",
            actual="missing_or_invalid",
        )
    return _Correlation(
        request_id=request_id,
        assignment_id=assignment_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        idempotency_key=idempotency_key,
        contract_digest=contract_digest,
        access_token=_bearer(request),
    )


async def _credential_for_token(
    services: Any, access_token: str
) -> TaskCredentialRecord:
    return await services.platform_blackbox_auth.authenticate_credential(access_token)


async def _published_workflow_tools(
    services: Any,
    *,
    application_ids: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for application in await services.workflow_store.list_applications():
        if str(application["id"]) not in application_ids:
            continue
        version = application.get("active_version")
        if version is None:
            continue
        result.append(
            {
                "name": f"workflow:{application['id']}",
                "type": "workflow",
                "title": application["name"],
                "version": version,
                "published": True,
            }
        )
    return result


def _connector_policy_match(
    connector_id: str,
    operation_id: str,
    values: list[str],
) -> bool:
    candidates = {
        operation_id,
        f"{connector_id}.{operation_id}",
        f"{connector_id}:{operation_id}",
    }
    return len(candidates.intersection(values)) == 1


def _connector_object_json_schema(value: Any) -> dict[str, Any]:
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


async def _task_scoped_connector_tools(
    services: Any,
    credential: TaskCredentialRecord,
) -> list[dict[str, Any]]:
    """Project registered connectors through the exact task authority.

    The projection deliberately omits deployment URLs, secret references,
    external tenant identities, authentication material and source
    provenance.  Connector execution repeats every binding and policy check.
    """

    if not credential.connector_access:
        return []
    application_ids = {str(value) for value in credential.application_ids}
    allowed_hosts = {
        value.casefold().rstrip(".")
        for value in credential.allowed_network_hosts
        if value.strip()
    }
    bindings: dict[tuple[str, int, str], Any] = {}
    for application_id in sorted(application_ids):
        for binding in await services.connectors.list_bindings(
            application_id=application_id
        ):
            bindings[
                (
                    binding.connector_id,
                    binding.connector_version,
                    binding.tenant_id,
                )
            ] = binding

    result: list[dict[str, Any]] = []
    for key in sorted(bindings):
        binding = bindings[key]
        if not binding.enabled or len(binding.subjects) != 1:
            continue
        scoped_applications = sorted(
            application_ids.intersection(binding.application_ids)
        )
        if not scoped_applications:
            continue
        try:
            manifest = await services.connectors.get_manifest(
                binding.connector_id,
                binding.connector_version,
            )
            policy = await services.connectors.get_policy(
                binding.connector_id,
                binding.connector_version,
                binding.tenant_id,
            )
            profile = manifest.profile(binding.profile_id)
        except (KeyError, ValueError):
            continue
        endpoint_host = (
            (urlsplit(profile.base_url).hostname or "").casefold().rstrip(".")
        )
        if (
            not profile.available
            or endpoint_host not in allowed_hosts
            or profile.id not in policy.allowed_profiles
            or policy.domain != manifest.domain
        ):
            continue
        subject = binding.subjects[0]
        subject_roles = set(subject.roles)
        for operation in sorted(manifest.operations, key=lambda item: item.id):
            if (
                operation.id not in binding.allowed_operations
                or operation.id not in policy.allowed_operations
            ):
                continue
            if operation.kind == "read":
                lane = credential.readable_host_objects
            elif operation.kind == "write":
                lane = credential.writable_host_operations
            else:
                lane = credential.compensation_actions
            if not _connector_policy_match(
                manifest.connector_id,
                operation.id,
                lane,
            ):
                continue
            required_roles = set(policy.required_roles).union(operation.required_roles)
            if required_roles and not required_roles.intersection(subject_roles):
                continue
            if (
                policy.emergency_stop
                and operation.mutating
                and not (
                    operation.kind == "compensate"
                    and policy.allow_compensation_during_stop
                )
            ):
                continue
            execution_modes = ["execute"]
            if policy.allow_dry_run:
                execution_modes.insert(0, "dry_run")
            authorization_required = operation.mutating and (
                policy.mutation_preauthorization_required
                or _connector_policy_match(
                    manifest.connector_id,
                    operation.id,
                    credential.permission_required_actions,
                )
            )
            connector_contract: dict[str, Any] = {
                "schema_version": "1.0",
                "connector_id": manifest.connector_id,
                "connector_version": manifest.version,
                "operation_id": operation.id,
                "operation_kind": operation.kind,
                "execution_context": {
                    "tenant_id": binding.tenant_id,
                    "actor_id": subject.actor_id,
                    "actor_roles": sorted(subject.roles),
                    "profile_id": profile.id,
                    "application_ids": scoped_applications,
                },
                "execution_modes": execution_modes,
                "authorization_required": authorization_required,
                "available": True,
                "environment": profile.environment,
                "claim_ceiling": profile.claim_ceiling,
                "excluded_claims": sorted(profile.excluded_claims),
                "max_payload_bytes": min(
                    policy.max_payload_bytes,
                    credential.max_payload_bytes,
                ),
            }
            payload_schema = _connector_object_json_schema(operation.request_schema)
            output_schema = (
                json.loads(json.dumps(operation.response_json_schema))
                if operation.response_json_schema is not None
                else _connector_object_json_schema(operation.response_schema)
            )
            input_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "payload": payload_schema,
                    "idempotency_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                    },
                    "execution_mode": {
                        "type": "string",
                        "enum": execution_modes,
                    },
                    "authorization_id": {"type": "string"},
                },
                "required": [
                    "payload",
                    "idempotency_key",
                    "execution_mode",
                ],
                "additionalProperties": False,
                "x-lilies-connector": connector_contract,
            }
            connector_contract["descriptor_digest"] = public_digest(
                {
                    "connector_contract": connector_contract,
                    "payload_schema": payload_schema,
                    "output_schema": output_schema,
                }
            )
            descriptor: dict[str, Any] = {
                "name": (
                    f"connector:{manifest.connector_id}:{manifest.version}:"
                    f"{operation.id}"
                ),
                "type": "core",
                "published": True,
                "description": (
                    f"{manifest.title}: {operation.title}. Configure a "
                    "connector_action block with the constants in "
                    "input_schema.x-lilies-connector; raw host and secret "
                    "material remain platform-internal."
                ),
                "input_schema": input_schema,
                "output_schema": output_schema,
            }
            result.append(descriptor)
    return sorted(result, key=lambda item: item["name"])


async def _current_contract(
    services: Any, credential: TaskCredentialRecord
) -> dict[str, Any]:
    contract_version = getattr(services.settings, "lilies_platform_contract_version", 1)
    await services.platform_contract_versions.observe(
        contract_version=contract_version,
        schema_digest=platform_contract_schema_digest(),
    )
    return build_platform_contract(
        services.blocks,
        services.tools,
        scopes=credential.scopes,
        published_workflow_tools=await _published_workflow_tools(
            services,
            application_ids={str(value) for value in credential.application_ids},
        ),
        published_connector_tools=await _task_scoped_connector_tools(
            services,
            credential,
        ),
        contract_version=contract_version,
        allowed_runtime_tool_names=BLACKBOX_RUNTIME_TOOL_ALLOWLIST,
    )


def _auth_failure(error: PlatformBlackboxAuthError) -> _FacadeFailure:
    if isinstance(error, PlatformBlackboxCredentialExpired):
        return _FacadeFailure(
            "credential_expired",
            str(error),
            status_code=401,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxCredentialRevoked):
        return _FacadeFailure(
            "credential_revoked",
            str(error),
            status_code=401,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxAuthenticationError):
        return _FacadeFailure(
            "authentication_failed",
            "platform task credential is invalid",
            status_code=401,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxScopeDenied):
        return _FacadeFailure(
            "authorization_denied",
            str(error),
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxApplicationDenied):
        return _FacadeFailure(
            "not_found",
            "the requested resource was not found",
            status_code=404,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxAuthorizationError):
        return _FacadeFailure(
            "authorization_denied",
            str(error),
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxIdempotencyConflict):
        return _FacadeFailure("idempotency_conflict", str(error), status_code=409)
    if isinstance(error, PlatformBlackboxRequestConflict):
        return _FacadeFailure("request_conflict", str(error), status_code=409)
    if isinstance(error, PlatformBlackboxStoreError):
        return _FacadeFailure(
            "platform_auth_unavailable",
            "platform task authorization is unavailable",
            status_code=503,
            retryable=True,
            failure_owner="platform",
        )
    return _FacadeFailure("authorization_denied", str(error), status_code=403)


def _operation_failure(error: Exception) -> _FacadeFailure:
    if isinstance(error, WorkflowWorkspaceBoundaryViolation):
        return _FacadeFailure(
            "workspace_boundary_violation",
            _public_error_message(error),
            status_code=422,
            failure_owner="user_permission",
        )
    if isinstance(error, NestedWorkflowScopeDenied):
        return _FacadeFailure(
            "nested_workflow_scope_denied",
            "nested workflow target is outside the assigned application scope",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeToolScopeDenied):
        return _FacadeFailure(
            "runtime_tool_scope_denied",
            "runtime tool is outside the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeNetworkScopeDenied):
        return _FacadeFailure(
            "runtime_network_scope_denied",
            "network access is outside the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeSecretScopeDenied):
        return _FacadeFailure(
            "runtime_secret_scope_denied",
            "secret references are outside the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeModelScopeDenied):
        return _FacadeFailure(
            "runtime_model_scope_denied",
            "model access is outside the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeConnectorScopeDenied):
        return _FacadeFailure(
            "runtime_connector_scope_denied",
            "connector operation is outside the assigned run policy",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimePermissionScopeDenied):
        return _FacadeFailure(
            "runtime_permission_required",
            "connector operation requires an authorization receipt",
            status_code=403,
            failure_owner="user_permission",
        )
    if isinstance(error, WorkflowRuntimeWriteLimitExceeded):
        return _FacadeFailure(
            "runtime_write_limit_exceeded",
            "connector write limit is exhausted",
            status_code=429,
            failure_owner="lilies",
        )
    if isinstance(error, WorkflowRuntimePayloadLimitExceeded):
        return _FacadeFailure(
            "runtime_payload_limit_exceeded",
            "connector payload exceeds the assigned byte limit",
            status_code=413,
            failure_owner="lilies",
        )
    if isinstance(error, PlatformBlackboxArtifactNotFound):
        return _FacadeFailure(
            "not_found", _public_error_message(error), status_code=404
        )
    if isinstance(error, PlatformBlackboxArtifactPathUnsafe):
        return _FacadeFailure(
            "artifact_path_unsafe",
            _public_error_message(error),
            status_code=422,
            failure_owner="task_author",
        )
    if isinstance(error, PlatformBlackboxArtifactScopeDenied):
        return _FacadeFailure(
            "not_found",
            "the requested resource was not found",
            status_code=404,
            failure_owner="user_permission",
        )
    if isinstance(error, PlatformBlackboxArtifactRangeInvalid):
        return _FacadeFailure(
            "artifact_range_invalid",
            _public_error_message(error),
            status_code=416,
            failure_owner="task_author",
        )
    if isinstance(error, PlatformBlackboxArtifactTooLarge):
        return _FacadeFailure(
            "artifact_too_large", _public_error_message(error), status_code=413
        )
    if isinstance(error, PlatformBlackboxArtifactIntegrityError):
        return _FacadeFailure(
            "artifact_integrity_failed", _public_error_message(error), status_code=409
        )
    if isinstance(error, PlatformBlackboxArtifactConflict):
        return _FacadeFailure(
            "artifact_conflict", _public_error_message(error), status_code=409
        )
    if isinstance(error, PlatformBlackboxArtifactStoreError):
        return _FacadeFailure(
            "artifact_store_unavailable",
            "the artifact registry is unavailable",
            status_code=503,
            retryable=True,
            failure_owner="platform",
        )
    if isinstance(error, PlatformBlackboxArtifactError):
        return _FacadeFailure(
            "artifact_error", _public_error_message(error), status_code=422
        )
    if isinstance(error, RevisionConflict):
        return _FacadeFailure(
            "revision_conflict", _public_error_message(error), status_code=409
        )
    if isinstance(error, PublishGateError):
        return _FacadeFailure(
            "publish_gate_failed",
            _public_error_message(error),
            status_code=409,
            expected="publication gate pass or explicit warning acknowledgement",
            actual=_redact(jsonable_encoder(error.decision)),
        )
    if isinstance(error, KeyError):
        return _FacadeFailure(
            "not_found", _public_error_message(error), status_code=404
        )
    if isinstance(error, SandboxError):
        return _FacadeFailure(
            "artifact_path_unsafe",
            _public_error_message(error),
            status_code=422,
            failure_owner="task_author",
        )
    if isinstance(error, RuntimeError):
        return _FacadeFailure(
            "invalid_state", _public_error_message(error), status_code=409
        )
    if isinstance(error, ValueError):
        return _FacadeFailure(
            "invalid_request",
            _public_error_message(error),
            status_code=422,
            failure_owner="task_author",
        )
    return _FacadeFailure(
        "platform_operation_failed",
        "the platform operation failed",
        status_code=500,
        retryable=False,
        failure_owner="platform",
    )


def _redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if not _PRIVATE_REASONING_KEY_RE.fullmatch(str(item_key))
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        redacted = _SENSITIVE_VALUE_RE.sub("[REDACTED]", value)
        redacted = _EMBEDDED_WINDOWS_PATH_RE.sub("[REDACTED_PATH]", redacted)
        redacted = _EMBEDDED_POSIX_PATH_RE.sub("[REDACTED_PATH]", redacted)
        if len(redacted) > 4_000:
            return redacted[:4_000] + "...[TRUNCATED]"
        return redacted
    return value


def _public_error_message(error: Exception) -> str:
    value = _redact(str(error))
    return str(value)[:1_000] or type(error).__name__


def _run_projection(run: dict[str, Any]) -> dict[str, Any]:
    state = run.get("state")
    return {
        "id": run.get("id"),
        "application_id": run.get("application_id"),
        "version": run.get("version"),
        "draft_revision": run.get("draft_revision"),
        "status": run.get("status"),
        "outputs": _redact(run.get("outputs", {})),
        "error": _redact(run.get("error")),
        "waiting_node_id": getattr(state, "waiting_node_id", None),
        "completed_node_ids": list(getattr(state, "completed", [])),
        "skipped_node_ids": list(getattr(state, "skipped", [])),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def _trace_event_allowed(event_type: str) -> bool:
    exact = {
        "workflow.started",
        "workflow.completed",
        "workflow.paused",
        "workflow.resumed",
        "workflow.cancelled",
        "workflow.failed",
        "node.started",
        "node.completed",
        "node.skipped",
        "node.degraded",
        "node.retry",
        "node.failed",
        "human_input.required",
        "permission.requested",
        "permission.resolved",
        "checkpoint.saved",
        "context.compaction.started",
        "context.compaction.completed",
        "budget.exceeded",
        "round_limit.reached",
        "cancellation.checked",
    }
    if event_type in exact:
        return True
    return bool(
        re.fullmatch(
            r"(?:loop\.iteration\.(?:started|completed)|loop\.checkpoint\.saved|"
            r"node\.[A-Za-z0-9_.:-]+\.tool\.(?:started|completed|failed)|"
            r"contract\.(?:warning|error))",
            event_type,
        )
    )


def _trace_data_projection(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    # Trace is an operational projection, not a replay of model/tool payloads.
    allowed = {
        "attempt",
        "behavior",
        "branch",
        "code",
        "duration_ms",
        "error_type",
        "iteration",
        "level",
        "max_iterations",
        "mode",
        "node_id",
        "status",
        "title",
        "tool",
        "tool_name",
        "type",
    }
    if ".tool." in event_type:
        allowed &= {
            "duration_ms",
            "error_type",
            "node_id",
            "status",
            "tool",
            "tool_name",
        }
    return {
        key: _redact(value, key=key) for key, value in data.items() if key in allowed
    }


def _task_workspace(services: Any, correlation: _Correlation) -> Path:
    root = (
        services.settings.workspace_root.resolve()
        / ".lilies_tasks"
        / str(correlation.assignment_id)
        / str(correlation.session_id)
        / f"run-{hashlib.sha256(correlation.idempotency_key.encode()).hexdigest()[:24]}"
    )
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    services.sandboxes.resolve_workspace(str(resolved))
    return resolved


def _artifact_binding(
    correlation: _Correlation,
    run: dict[str, Any],
) -> ArtifactBinding:
    return ArtifactBinding(
        assignment_id=correlation.assignment_id,
        session_id=correlation.session_id,
        application_id=UUID(str(run["application_id"])),
        run_id=str(run["id"]),
    )


def _owned_artifact_root(
    services: Any,
    correlation: _Correlation,
    run: dict[str, Any],
) -> Path:
    state = run["state"]
    declared_workspace = Path(str(state.workspace_path))
    workspace = services.sandboxes.resolve_workspace(str(declared_workspace)).resolve()
    task_session_root = (
        services.settings.workspace_root.resolve()
        / ".lilies_tasks"
        / str(correlation.assignment_id)
        / str(correlation.session_id)
    ).resolve()
    try:
        relative = workspace.relative_to(task_session_root)
    except ValueError as error:
        raise _FacadeFailure(
            "artifact_path_unsafe",
            "run workspace is not owned by this task session",
            status_code=403,
            failure_owner="user_permission",
        ) from error
    if not relative.parts or not relative.parts[0].startswith("run-"):
        raise _FacadeFailure(
            "artifact_path_unsafe",
            "run workspace is not owned by this task session",
            status_code=403,
            failure_owner="user_permission",
        )
    # A test suite uses run-*/case-* roots.  Descendants remain readable only
    # while every directory component is a real task-owned directory, never a
    # symlink hop to another task or the global workspace.
    if any(
        (task_session_root / Path(*relative.parts[:index])).is_symlink()
        for index in range(1, len(relative.parts) + 1)
    ):
        raise _FacadeFailure(
            "artifact_path_unsafe",
            "run workspace contains an unsafe symbolic-link boundary",
            status_code=403,
            failure_owner="user_permission",
        )
    return workspace


async def _register_run_artifacts(
    services: Any,
    correlation: _Correlation,
    run: dict[str, Any],
) -> list[dict[str, Any]]:
    workspace = _owned_artifact_root(services, correlation, run)
    binding = _artifact_binding(correlation, run)
    entries: list[dict[str, Any]] = []
    for candidate in sorted(workspace.rglob("*")):
        relative = candidate.relative_to(workspace)
        if candidate.is_symlink() or any(
            (workspace / Path(*relative.parts[:index])).is_symlink()
            for index in range(1, len(relative.parts) + 1)
        ):
            continue
        if not candidate.is_file():
            continue
        media_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        registration = await services.platform_blackbox_artifacts.register_artifact(
            ArtifactRegistrationRequest(
                binding=binding,
                relative_path=relative.as_posix(),
                media_type=media_type,
            ),
            artifact_root=workspace,
        )
        record = registration.artifact
        entries.append(
            {
                "artifact_id": str(record.artifact_id),
                "relative_path": record.relative_path,
                "media_type": record.media_type,
                "size_bytes": record.size_bytes,
                "sha256": record.sha256,
            }
        )
    return entries


async def _invoke(
    services: Any,
    request: Request,
    operation: PlatformBlackboxOperation,
    *,
    payload: dict[str, Any],
    callback: Callable[[_Correlation, TaskCredentialRecord], Awaitable[Any]],
    body: _CorrelatedBody | None = None,
    application_id: str | None = None,
    resource_run_id: str | None = None,
    success_status: int = 200,
) -> JSONResponse:
    request_id = uuid4()
    contract_digest = request.headers.get("x-lilies-contract-digest", _ZERO_DIGEST)
    try:
        correlation = _correlation(
            request,
            body,
            contract_get=operation is PlatformBlackboxOperation.contract_get,
        )
        request_id = correlation.request_id
        if application_id is not None:
            try:
                scoped_application = UUID(application_id)
            except ValueError as error:
                raise _FacadeFailure(
                    "invalid_request",
                    "application_id must be a UUID",
                    status_code=422,
                    expected="UUID",
                    actual="invalid",
                ) from error
            _require_path_body_identity(
                body,
                field="application_id",
                path_value=scoped_application,
            )
        elif resource_run_id is not None:
            try:
                scoped_run_id = UUID(resource_run_id)
            except ValueError as error:
                raise _FacadeFailure(
                    "invalid_request",
                    "run_id must be a UUID",
                    status_code=422,
                    failure_owner="task_author",
                    expected="UUID",
                    actual="invalid",
                ) from error
            _require_path_body_identity(
                body,
                field="run_id",
                path_value=scoped_run_id,
            )
            try:
                resource_run = await services.workflow_store.get_run(resource_run_id)
                scoped_application = UUID(str(resource_run["application_id"]))
            except (KeyError, ValueError):
                # Authentication and its denied audit must not depend on a
                # successful credential pre-read.  The assignment UUID is a
                # non-resource fallback that cannot grant application access.
                scoped_application = correlation.assignment_id
        else:
            scoped_application = correlation.assignment_id
        authorization_request = BlackboxAuthorizationRequest(
            request_id=correlation.request_id,
            assignment_id=correlation.assignment_id,
            session_id=correlation.session_id,
            tool_call_id=correlation.tool_call_id,
            idempotency_key=correlation.idempotency_key,
            application_id=scoped_application,
            operation=operation,
            contract_digest=correlation.contract_digest,
            payload=payload,
        )
        operation_failure: _FacadeFailure | None = None
        try:
            decision = await services.platform_blackbox_auth.authorize_request(
                correlation.access_token,
                authorization_request,
            )
        except PlatformBlackboxAuthError as error:
            raise _auth_failure(error) from error
        credential = await _credential_for_token(services, correlation.access_token)
        contract = await _current_contract(services, credential)
        current_digest = str(contract["contract_digest"])
        replay_without_result = False
        if decision.replayed:
            if (
                decision.state is BlackboxRequestState.completed
                and decision.result is not None
            ):
                return _json_response(
                    decision.result,
                    int(decision.status_code or 200),
                    idempotent_replay=True,
                )
            if (
                decision.state is BlackboxRequestState.completed
                and operation is PlatformBlackboxOperation.artifact_read
            ):
                # Artifact bytes deliberately are not cached in the request
                # ledger.  A replay re-reads the immutable digest-bound artifact
                # and verifies the reconstructed response digest below.
                replay_without_result = True
            else:
                raise _FacadeFailure(
                    "request_in_progress",
                    "an identical request is already reserved",
                    status_code=409,
                    retryable=True,
                )
        evidence_ref = f"platform-blackbox-request:{decision.authorization_id}"
        if (
            operation is not PlatformBlackboxOperation.contract_get
            and not decision.replayed
            and correlation.contract_digest != current_digest
        ):
            failure = _FacadeFailure(
                "contract_drift",
                "the supplied platform contract digest is stale",
                status_code=409,
                failure_owner="task_author",
                expected=current_digest,
                actual=correlation.contract_digest,
            )
            error_payload = _error_payload(
                operation=operation.value,
                request_id=correlation.request_id,
                status_code=failure.status_code,
                contract_digest=current_digest,
                code=failure.code,
                message=str(failure),
                retryable=failure.retryable,
                failure_owner=failure.failure_owner,
                expected=failure.expected,
                actual=failure.actual,
                evidence_ref=evidence_ref,
            )
            await services.platform_blackbox_auth.complete_request(
                decision.authorization_id,
                status_code=failure.status_code,
                result=error_payload,
            )
            return _json_response(error_payload, failure.status_code)
        try:
            response_correlation = correlation
            response_contract_digest = current_digest
            if replay_without_result:
                response_correlation = _Correlation(
                    request_id=decision.request_id,
                    assignment_id=decision.assignment_id,
                    session_id=decision.session_id,
                    tool_call_id=decision.tool_call_id,
                    idempotency_key=decision.idempotency_key,
                    contract_digest=decision.contract_digest,
                    access_token=correlation.access_token,
                )
                response_contract_digest = decision.contract_digest
            data = await callback(response_correlation, credential)
            response = _success_payload(
                operation=operation,
                correlation=response_correlation,
                status_code=success_status,
                contract_digest=response_contract_digest,
                data=data,
                evidence_refs=[evidence_ref],
            )
            created_application_id = None
            if operation is PlatformBlackboxOperation.application_create:
                created_application_id = UUID(str(data["id"]))
            await services.platform_blackbox_auth.complete_request(
                decision.authorization_id,
                status_code=success_status,
                result=response,
                created_application_id=created_application_id,
                persist_result=operation is not PlatformBlackboxOperation.artifact_read,
            )
            return _json_response(
                response,
                success_status,
                idempotent_replay=replay_without_result,
            )
        except _FacadeFailure as error:
            operation_failure = error
        except (
            Exception
        ) as error:  # Keep implementation failures inside the public envelope.
            operation_failure = _operation_failure(error)
        if operation_failure is None:  # pragma: no cover - callback path returns above
            raise RuntimeError("public operation ended without a result")
        error_payload = _error_payload(
            operation=operation.value,
            request_id=correlation.request_id,
            status_code=operation_failure.status_code,
            contract_digest=current_digest,
            code=operation_failure.code,
            message=str(operation_failure),
            retryable=operation_failure.retryable,
            failure_owner=operation_failure.failure_owner,
            expected=operation_failure.expected,
            actual=operation_failure.actual,
            evidence_ref=evidence_ref,
        )
        if not replay_without_result:
            await services.platform_blackbox_auth.complete_request(
                decision.authorization_id,
                status_code=operation_failure.status_code,
                result=error_payload,
            )
        return _json_response(error_payload, operation_failure.status_code)
    except _FacadeFailure as failure:
        payload_out = _error_payload(
            operation=operation.value,
            request_id=request_id,
            status_code=failure.status_code,
            contract_digest=contract_digest,
            code=failure.code,
            message=str(failure),
            retryable=failure.retryable,
            failure_owner=failure.failure_owner,
            expected=failure.expected,
            actual=failure.actual,
        )
        return _json_response(payload_out, failure.status_code)
    except PlatformBlackboxAuthError as error:
        failure = _auth_failure(error)
        payload_out = _error_payload(
            operation=operation.value,
            request_id=request_id,
            status_code=failure.status_code,
            contract_digest=contract_digest,
            code=failure.code,
            message=str(failure),
            retryable=failure.retryable,
            failure_owner=failure.failure_owner,
            expected=failure.expected,
            actual=failure.actual,
        )
        return _json_response(payload_out, failure.status_code)


def install_lilies_platform_api(app: FastAPI, services: Any) -> None:
    """Install the task-token-only public platform boundary."""

    @app.middleware("http")
    async def deny_task_tokens_on_internal_endpoints(
        request: Request, call_next: Any
    ) -> Any:
        authorization = request.headers.get("authorization", "")
        token = (
            authorization.partition(" ")[2]
            if authorization.lower().startswith("bearer ")
            else ""
        )
        valid_task_token = False
        if token.startswith("lpt_"):
            try:
                await services.platform_blackbox_auth.authenticate_credential(token)
                valid_task_token = True
            except PlatformBlackboxAuthError:
                valid_task_token = False
        if valid_task_token and not _is_contract_route(
            request.method, request.url.path
        ):
            request_id = uuid4()
            try:
                request_id = _request_id(request)
            except _FacadeFailure:
                pass
            digest = request.headers.get("x-lilies-contract-digest", _ZERO_DIGEST)
            payload = _error_payload(
                operation="internal_endpoint_denied",
                request_id=request_id,
                status_code=403,
                contract_digest=digest,
                code="internal_endpoint_denied",
                message="platform task credentials are valid only on contract endpoints",
                failure_owner="user_permission",
                expected="one of the 16 versioned public contract operations",
                actual={"method": request.method, "path": request.url.path},
            )
            return _json_response(payload, 403)
        return await call_next(request)

    previous_validation_handler = app.exception_handlers.get(RequestValidationError)

    @app.exception_handler(RequestValidationError)
    async def lilies_validation_error(
        request: Request, error: RequestValidationError
    ) -> Any:
        if not request.url.path.startswith("/api/v1/lilies/"):
            if previous_validation_handler is not None:
                return await previous_validation_handler(request, error)
            return await request_validation_exception_handler(request, error)
        operation_name = getattr(request.scope.get("route"), "name", "invalid_request")
        request_id = uuid4()
        try:
            request_id = _request_id(request)
        except _FacadeFailure:
            pass
        actual = [
            {
                "location": list(item.get("loc", ())),
                "type": item.get("type", "validation_error"),
            }
            for item in error.errors()
        ]
        payload = _error_payload(
            operation=operation_name,
            request_id=request_id,
            status_code=422,
            contract_digest=request.headers.get(
                "x-lilies-contract-digest", _ZERO_DIGEST
            ),
            code="invalid_request",
            message="request payload did not match the public operation schema",
            failure_owner="task_author",
            expected="public operation request schema",
            actual=actual,
        )
        return _json_response(payload, 422)

    @app.get("/api/v1/lilies/platform-contract", name="platform_contract_get")
    async def platform_contract_get(request: Request) -> JSONResponse:
        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            return await _current_contract(services, credential)

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.contract_get,
            payload={},
            callback=execute,
        )

    @app.get("/api/v1/lilies/blocks", name="platform_block_search")
    async def platform_block_search(
        request: Request,
        query: str = Query(default="", max_length=500),
        block_kind: str | None = Query(default=None, max_length=120),
    ) -> JSONResponse:
        async def execute(
            _: _Correlation, __: TaskCredentialRecord
        ) -> list[dict[str, Any]]:
            needle = query.casefold().strip()
            result = []
            for item in public_block_catalog(services.blocks):
                if block_kind and item.get("block_kind") != block_kind:
                    continue
                if (
                    needle
                    and needle
                    not in " ".join(
                        str(item.get(key, ""))
                        for key in ("type", "title", "description", "category")
                    ).casefold()
                ):
                    continue
                result.append(item)
            return result

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.block_search,
            payload={"query": query, "block_kind": block_kind},
            callback=execute,
        )

    @app.get("/api/v1/lilies/blocks/{block_type}", name="platform_block_get")
    async def platform_block_get(request: Request, block_type: str) -> JSONResponse:
        async def execute(_: _Correlation, __: TaskCredentialRecord) -> dict[str, Any]:
            definition = next(
                (
                    item
                    for item in public_block_catalog(services.blocks)
                    if item["type"] == block_type
                ),
                None,
            )
            if definition is None:
                raise KeyError(f"public block not found: {block_type}")
            return {
                "definition": definition,
                "manual": public_block_manual(services.blocks, block_type),
            }

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.block_get,
            payload={"block_type": block_type},
            callback=execute,
        )

    @app.get("/api/v1/lilies/tools", name="platform_tool_catalog")
    async def platform_tool_catalog(request: Request) -> JSONResponse:
        async def execute(
            _: _Correlation, credential: TaskCredentialRecord
        ) -> list[dict[str, Any]]:
            core = public_runtime_tool_catalog(
                services.tools,
                allowed_runtime_tool_names=BLACKBOX_RUNTIME_TOOL_ALLOWLIST,
            )
            return [
                *core,
                *(
                    await _published_workflow_tools(
                        services,
                        application_ids={
                            str(value) for value in credential.application_ids
                        },
                    )
                ),
                *(await _task_scoped_connector_tools(services, credential)),
            ]

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.tool_catalog,
            payload={},
            callback=execute,
        )

    @app.post("/api/v1/lilies/applications", name="platform_application_create")
    async def platform_application_create(
        request: Request,
        body: ApplicationCreateBody,
    ) -> JSONResponse:
        async def execute(_: _Correlation, __: TaskCredentialRecord) -> dict[str, Any]:
            create_request = ApplicationCreateRequest.model_validate(
                body.model_dump(exclude={"idempotency_key"})
            )
            return await services.workflow_store.create_application(create_request)

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.application_create,
            payload=body.model_dump(mode="json"),
            callback=execute,
            body=body,
            success_status=201,
        )

    @app.get(
        "/api/v1/lilies/applications/{application_id}",
        name="platform_application_get",
    )
    async def platform_application_get(
        request: Request, application_id: str
    ) -> JSONResponse:
        async def execute(_: _Correlation, __: TaskCredentialRecord) -> dict[str, Any]:
            return await services.workflow_store.get_application(application_id)

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.application_get,
            payload={"application_id": application_id},
            callback=execute,
            application_id=application_id,
        )

    @app.get(
        "/api/v1/lilies/applications/{application_id}/draft",
        name="platform_draft_inspect",
    )
    async def platform_draft_inspect(
        request: Request, application_id: str
    ) -> JSONResponse:
        async def execute(_: _Correlation, __: TaskCredentialRecord) -> dict[str, Any]:
            draft = await services.workflow_store.get_draft(application_id)
            return {
                **{key: value for key, value in draft.items() if key != "snapshot"},
                "snapshot": _redact(draft["snapshot"].model_dump(mode="json")),
            }

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.draft_inspect,
            payload={"application_id": application_id},
            callback=execute,
            application_id=application_id,
        )

    @app.post(
        "/api/v1/lilies/applications/{application_id}/draft",
        name="platform_draft_apply",
    )
    async def platform_draft_apply(
        request: Request,
        application_id: str,
        body: DraftApplyBody,
    ) -> JSONResponse:
        payload = _path_body_payload("application_id", application_id, body)

        async def execute(
            correlation: _Correlation,
            credential: TaskCredentialRecord,
        ) -> dict[str, Any]:
            operation = DraftOperation(
                expected_revision=body.expected_revision,
                idempotency_key=correlation.idempotency_key,
                op=body.op,
                data=body.data,
            )
            if body.op in {"add_node", "update_node"}:
                draft = await services.workflow_store.get_draft(application_id)
                preview = services.applications.validate_preview_operations(
                    draft["snapshot"],
                    [operation],
                )
                node_id = (
                    str(body.data["node"]["id"])
                    if body.op == "add_node"
                    else str(body.data["node_id"])
                )
                updated_node = next(
                    node for node in preview.workflow.nodes if node.id == node_id
                )
                _validate_public_node_tree(services.blocks, updated_node)
            return await services.applications.apply_operation(
                application_id,
                operation,
                formal_mutation_context={
                    "assignment_id": str(credential.assignment_id),
                    "session_id": str(credential.session_id),
                    "application_id": application_id,
                    "request_id": str(correlation.request_id),
                    "tool_call_id": correlation.tool_call_id,
                    "operation": body.op,
                    "request_payload_digest": (
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()
                    ),
                },
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.draft_apply,
            payload=payload,
            callback=execute,
            body=body,
            application_id=application_id,
        )

    @app.post(
        "/api/v1/lilies/applications/{application_id}/tests/run",
        name="platform_tests_run",
    )
    async def platform_tests_run(
        request: Request,
        application_id: str,
        body: TestsRunBody,
    ) -> JSONResponse:
        payload = _path_body_payload("application_id", application_id, body)

        async def execute(
            correlation: _Correlation,
            credential: TaskCredentialRecord,
        ) -> dict[str, Any]:
            workspace = _task_workspace(services, correlation)
            return _redact(
                await services.workflow_runtime.run_test_suite(
                    application_id,
                    origin="lilies_platform_contract",
                    workspace_path=str(workspace),
                    workspace_boundary=str(workspace),
                    allowed_nested_application_ids={
                        str(value) for value in credential.application_ids
                    },
                    allowed_runtime_tools=_runtime_tool_policy(credential),
                    allowed_network_hosts=_runtime_network_policy(credential),
                    model_access=credential.model_access,
                    allowed_connector_operations=_runtime_connector_policy(credential),
                    writable_connector_operations=(credential.writable_host_operations),
                    permission_required_connector_operations=(
                        credential.permission_required_actions
                    ),
                    compensation_connector_operations=(credential.compensation_actions),
                    max_connector_write_count=credential.max_write_count,
                    max_connector_payload_bytes=credential.max_payload_bytes,
                    governed_host_actions=_governed_host_actions(credential),
                    assignment_id=str(correlation.assignment_id),
                    session_id=str(correlation.session_id),
                )
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.tests_run,
            payload=payload,
            callback=execute,
            body=body,
            application_id=application_id,
        )

    @app.post(
        "/api/v1/lilies/applications/{application_id}/runs",
        name="platform_run_start",
    )
    async def platform_run_start(
        request: Request,
        application_id: str,
        body: RunStartBody,
    ) -> JSONResponse:
        payload = _path_body_payload("application_id", application_id, body)

        async def execute(
            correlation: _Correlation,
            credential: TaskCredentialRecord,
        ) -> dict[str, Any]:
            workspace = _task_workspace(services, correlation)
            run_request = WorkflowRunRequest(
                inputs=body.inputs,
                version=body.version,
                use_draft=body.use_draft,
                workspace_path=str(workspace),
            )
            return await services.workflow_runtime.create_run(
                application_id,
                run_request,
                origin="lilies_platform_contract",
                workspace_boundary=str(workspace),
                allowed_nested_application_ids={
                    str(value) for value in credential.application_ids
                },
                allowed_runtime_tools=_runtime_tool_policy(credential),
                allowed_network_hosts=_runtime_network_policy(credential),
                model_access=credential.model_access,
                allowed_connector_operations=_runtime_connector_policy(credential),
                writable_connector_operations=(credential.writable_host_operations),
                permission_required_connector_operations=(
                    credential.permission_required_actions
                ),
                compensation_connector_operations=(credential.compensation_actions),
                max_connector_write_count=credential.max_write_count,
                max_connector_payload_bytes=credential.max_payload_bytes,
                governed_host_actions=_governed_host_actions(credential),
                assignment_id=str(correlation.assignment_id),
                session_id=str(correlation.session_id),
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.run_start,
            payload=payload,
            callback=execute,
            body=body,
            application_id=application_id,
            success_status=202,
        )

    @app.get("/api/v1/lilies/runs/{run_id}", name="platform_run_get")
    async def platform_run_get(request: Request, run_id: str) -> JSONResponse:
        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            run = await services.workflow_store.get_run(run_id)
            _require_blackbox_run(run, correlation, credential)
            # Snapshot only the task-owned workspace; artifact reads require this
            # server-side ownership and digest registry.
            artifacts = (
                await _register_run_artifacts(services, correlation, run)
                if run.get("status") in {"succeeded", "failed", "cancelled"}
                else []
            )
            return {**_run_projection(run), "artifacts": artifacts}

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.run_get,
            payload={"run_id": run_id},
            callback=execute,
            resource_run_id=run_id,
        )

    @app.post("/api/v1/lilies/runs/{run_id}/resume", name="platform_run_resume")
    async def platform_run_resume(
        request: Request,
        run_id: str,
        body: RunResumeBody,
    ) -> JSONResponse:
        payload = _path_body_payload("run_id", run_id, body)

        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            run = await services.workflow_store.get_run(run_id)
            _require_blackbox_run(run, correlation, credential)
            if services.harness.contains_secret_reference(body.values):
                raise WorkflowRuntimeSecretScopeDenied(
                    "secret references are outside the assigned run policy"
                )
            return await services.workflow_runtime.resume(
                run_id,
                ResumeRunRequest(values=body.values).values,
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.run_resume,
            payload=payload,
            callback=execute,
            body=body,
            resource_run_id=run_id,
        )

    @app.post("/api/v1/lilies/runs/{run_id}/cancel", name="platform_run_cancel")
    async def platform_run_cancel(
        request: Request,
        run_id: str,
        body: RunCancelBody,
    ) -> JSONResponse:
        payload = _path_body_payload("run_id", run_id, body)

        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            run = await services.workflow_store.get_run(run_id)
            _require_blackbox_run(run, correlation, credential)
            services.harness.enforce_cancellation_policy()
            services.workflow_runtime.cancel(run_id)
            return {"run_id": run_id, "status": "cancelling"}

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.run_cancel,
            payload=payload,
            callback=execute,
            body=body,
            resource_run_id=run_id,
        )

    @app.get("/api/v1/lilies/runs/{run_id}/trace", name="platform_trace_get")
    async def platform_trace_get(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2_000),
    ) -> JSONResponse:
        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            run = await services.workflow_store.get_run(run_id)
            _require_blackbox_run(run, correlation, credential)
            events = await services.storage.list_events(run_id, after=after)
            projected = []
            scanned = events[:limit]
            for event in scanned:
                if not _trace_event_allowed(event.type):
                    continue
                projected.append(
                    {
                        "seq": event.id,
                        "type": event.type,
                        "data": _trace_data_projection(event.type, event.data),
                        "created_at": event.created_at,
                    }
                )
            return {
                "run_id": run_id,
                "events": projected,
                "next_after": scanned[-1].id if scanned else after,
                "redacted": True,
            }

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.trace_get,
            payload={"run_id": run_id, "after": after, "limit": limit},
            callback=execute,
            resource_run_id=run_id,
        )

    @app.get(
        "/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
        name="platform_artifact_read",
    )
    async def platform_artifact_read(
        request: Request,
        run_id: str,
        artifact_id: UUID,
        offset_bytes: int = Query(default=0, ge=0),
        max_bytes: int = Query(
            default=DEFAULT_ARTIFACT_CHUNK_BYTES,
            ge=1,
            le=MAX_ARTIFACT_CHUNK_BYTES,
        ),
    ) -> JSONResponse:
        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            run = await services.workflow_store.get_run(run_id)
            _require_blackbox_run(run, correlation, credential)
            artifact_root = _owned_artifact_root(services, correlation, run)
            result = await services.platform_blackbox_artifacts.read_artifact(
                ArtifactReadRequest(
                    artifact_id=artifact_id,
                    binding=_artifact_binding(correlation, run),
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                ),
                artifact_root=artifact_root,
            )
            return result.model_dump(
                mode="json",
                exclude={"assignment_id", "session_id", "application_id"},
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.artifact_read,
            payload={
                "run_id": run_id,
                "artifact_id": str(artifact_id),
                "offset_bytes": offset_bytes,
                "max_bytes": max_bytes,
            },
            callback=execute,
            resource_run_id=run_id,
        )

    @app.post(
        "/api/v1/lilies/applications/{application_id}/versions",
        name="platform_publish",
    )
    async def platform_publish(
        request: Request,
        application_id: str,
        body: PublishBody,
    ) -> JSONResponse:
        payload = _path_body_payload("application_id", application_id, body)

        async def execute(
            correlation: _Correlation, credential: TaskCredentialRecord
        ) -> dict[str, Any]:
            workspace = _task_workspace(services, correlation)
            draft = await services.workflow_store.get_draft(application_id)
            mandatory_tests = [
                test for test in draft["snapshot"].tests if test.mandatory
            ]
            validation_report = draft.get("validation_report")
            current_acceptance = bool(
                mandatory_tests
                and draft.get("tested_hash") == draft.get("content_hash")
                and isinstance(validation_report, dict)
                and validation_report.get("passed") is True
            )
            if not current_acceptance:
                raise PublishGateError(
                    "black-box publication requires current passing mandatory acceptance evidence",
                    {
                        "blocked": True,
                        "reason": "current_mandatory_acceptance_required",
                        "mandatory_test_count": len(mandatory_tests),
                        "tested_hash_matches": draft.get("tested_hash")
                        == draft.get("content_hash"),
                        "latest_validation_passed": (
                            validation_report.get("passed")
                            if isinstance(validation_report, dict)
                            else False
                        ),
                    },
                )
            for node in draft["snapshot"].workflow.nodes:
                _validate_public_node_tree(
                    services.blocks,
                    node,
                    reject_schedule_trigger=True,
                )
            services.workflow_runtime.validate_restricted_snapshot(
                draft["snapshot"],
                workspace_boundary=str(workspace),
                allowed_nested_application_ids={
                    str(value) for value in credential.application_ids
                },
                allowed_runtime_tools=_runtime_tool_policy(credential),
                allowed_network_hosts=_runtime_network_policy(credential),
                model_access=credential.model_access,
                allowed_connector_operations=_runtime_connector_policy(credential),
                governed_host_actions=_governed_host_actions(credential),
                for_publication=True,
            )
            publish_request = PublishApplicationRequest(
                acknowledge_warnings=body.acknowledge_warnings
            )
            return await services.workflow_store.publish(
                application_id,
                acknowledge_warnings=publish_request.acknowledge_warnings,
            )

        return await _invoke(
            services,
            request,
            PlatformBlackboxOperation.publish,
            payload=payload,
            callback=execute,
            body=body,
            application_id=application_id,
        )

    @app.api_route(
        "/api/v1/lilies/{unmapped_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        name="platform_operation_not_found",
    )
    async def platform_operation_not_found(
        request: Request, unmapped_path: str
    ) -> JSONResponse:
        payload = _error_payload(
            operation="platform_operation_not_found",
            request_id=uuid4(),
            status_code=404,
            contract_digest=request.headers.get(
                "x-lilies-contract-digest", _ZERO_DIGEST
            ),
            code="operation_not_found",
            message="the requested operation is not part of the public platform contract",
            failure_owner="task_author",
            expected="a path advertised by platform_contract_get",
            actual=f"/api/v1/lilies/{unmapped_path}",
        )
        return _json_response(payload, 404)
