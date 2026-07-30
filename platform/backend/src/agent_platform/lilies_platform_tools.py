from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from .lilies_models import IdempotencyKey
from .lilies_platform_contract import (
    DEFAULT_ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_CHUNK_BYTES,
    operation_request_schema,
)
from .lilies_platform_client import (
    ZERO_CONTRACT_DIGEST,
    LiliesPlatformClient,
    LiliesPlatformContractNotLoaded,
    LiliesPlatformOperationUnavailable,
    LiliesPlatformProtocolError,
    PlatformToolEnvelope,
    PlatformToolError,
)
from .models import ToolDefinition
from .lilies_tools import (
    LiliesTool,
    LiliesToolContext,
    LiliesToolRegistry,
    LiliesToolResult,
    StrictToolInput,
    build_lilies_core_registry,
)
PLATFORM_MODEL_RESULT_SAFE_CHARS = 400_000


class ContractGetInput(StrictToolInput):
    pass


class BlockSearchInput(StrictToolInput):
    query: str = Field(default="", max_length=500)
    block_kind: str | None = Field(default=None, max_length=120)


class BlockGetInput(StrictToolInput):
    block_type: str = Field(min_length=1, max_length=160)


class ToolCatalogInput(StrictToolInput):
    pass


class ConnectorAuthorizationIssueInput(StrictToolInput):
    application_id: UUID
    connector_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    connector_version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=300)
    actor_id: str = Field(min_length=1, max_length=300)
    profile_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    operation_id: str = Field(
        min_length=2,
        max_length=120,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]+$",
    )
    operation_kind: Literal["write", "compensate"]
    descriptor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload: dict[str, Any]
    expires_in_seconds: int = Field(default=300, ge=1, le=300)
    idempotency_key: IdempotencyKey


class ApplicationCreateInput(StrictToolInput):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1_000)
    requirement: str = Field(default="", max_length=30_000)
    mode: Literal["workflow", "chat"] = "workflow"
    delivery_mode: Literal["quick", "guided", "governed"] = "guided"
    governed_hard_gate: bool = False
    idempotency_key: IdempotencyKey


class ApplicationGetInput(StrictToolInput):
    application_id: UUID


class DraftInspectInput(StrictToolInput):
    application_id: UUID


class DraftApplyInput(StrictToolInput):
    application_id: UUID
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey
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
    def reject_reserved_test_inputs(self) -> DraftApplyInput:
        if self.op != "add_test":
            return self
        test = self.data.get("test")
        inputs = test.get("inputs") if isinstance(test, dict) else None
        if isinstance(inputs, dict) and any(str(key).startswith("__") for key in inputs):
            raise ValueError("reserved runtime input keys are not public")
        return self


class TestsRunInput(StrictToolInput):
    application_id: UUID
    idempotency_key: IdempotencyKey


class RunStartInput(StrictToolInput):
    application_id: UUID
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)
    use_draft: bool = False
    idempotency_key: IdempotencyKey

    @field_validator("inputs")
    @classmethod
    def reject_reserved_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(str(key).startswith("__") for key in value):
            raise ValueError("reserved runtime input keys are not public")
        return value


class RunGetInput(StrictToolInput):
    run_id: UUID


class RunResumeInput(StrictToolInput):
    run_id: UUID
    values: dict[str, Any]
    idempotency_key: IdempotencyKey


class RunCancelInput(StrictToolInput):
    run_id: UUID
    idempotency_key: IdempotencyKey


class TraceGetInput(StrictToolInput):
    run_id: UUID
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1, le=2_000)


class ArtifactReadInput(StrictToolInput):
    run_id: UUID
    artifact_id: UUID
    offset_bytes: int = Field(default=0, ge=0)
    max_bytes: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_BYTES,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_BYTES,
    )


class PublishInput(StrictToolInput):
    application_id: UUID
    acknowledge_warnings: bool = False
    idempotency_key: IdempotencyKey


_TOOL_DESCRIPTIONS = {
    "platform_contract_get": "Read and verify the scoped public platform capability contract.",
    "platform_block_search": "Search public workflow blocks and their manuals.",
    "platform_block_get": "Read one public block schema, ports, examples, and anti-patterns.",
    "platform_tool_catalog": "List public workflow-runtime tool contracts.",
    "platform_connector_authorization_issue": (
        "Issue one task-policy-bound, exact-payload, single-use connector authorization."
    ),
    "platform_application_create": "Create one application owned by this assignment.",
    "platform_application_get": "Read an assigned application's version summary.",
    "platform_draft_inspect": "Inspect an assigned draft, revision, graph, tests, and validation.",
    "platform_draft_apply": "Apply exactly one incremental public DraftOperation.",
    "platform_tests_run": "Run the assigned draft's complete acceptance suite.",
    "platform_run_start": "Start an assigned draft or published workflow run.",
    "platform_run_get": "Inspect an assigned workflow run and its outputs or wait state.",
    "platform_run_resume": "Resume an assigned run waiting for human or permission input.",
    "platform_run_cancel": "Cancel an assigned workflow run.",
    "platform_trace_get": "Read a bounded, secret-redacted structured run trace.",
    "platform_artifact_read": (
        "Read one digest-verified, bounded chunk of a run artifact through "
        "assignment-safe path containment."
    ),
    "platform_publish": (
        "Publish an immutable assigned application version after the Builder's explicit "
        "decision; platform structural, permission, and execution-safety checks still apply."
    ),
}


class PlatformHttpTool(LiliesTool):
    requires_permission = False
    handles_input_validation = True
    # The versioned contract intentionally includes exact request/response
    # schemas for every operation.  Keep the JSON envelope whole: arbitrary
    # character truncation would turn a valid contract into invalid JSON.
    max_result_chars = 500_000
    preserve_result_integrity = True

    def __init__(
        self,
        client: LiliesPlatformClient,
        *,
        name: str,
        input_model: type[StrictToolInput],
        mutating: bool = False,
        side_effecting: bool = False,
    ) -> None:
        self.client = client
        self.name = name
        self.description = _TOOL_DESCRIPTIONS[name]
        self.input_model = input_model
        self.mutating = mutating
        self.side_effecting = side_effecting
        self.dangerous = False

    def definition(self) -> ToolDefinition:
        """Expose the exact public HTTP grammar to the model.

        Runtime Pydantic models remain a local validation layer, while the versioned
        platform contract is the sole source of discoverable request shapes.
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=operation_request_schema(self.name),
        )

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        try:
            args = self.input_model.model_validate(data)
            payload = args.model_dump(mode="json", exclude_none=True)
            idempotency_key = payload.get("idempotency_key")
            tool_call_id = context.tool_call_id or f"local-tool-{uuid4().hex}"
            if self.name == "platform_contract_get":
                result = await self.client.contract_get(
                    tool_call_id=tool_call_id,
                    idempotency_key=idempotency_key,
                )
            else:
                result = await self.client.invoke(
                    self.name,
                    payload,
                    tool_call_id=tool_call_id,
                    idempotency_key=idempotency_key,
                )
        except Exception as error:
            result = self._client_failure(error)
        serialized = self._serialize(result)
        if len(serialized) > PLATFORM_MODEL_RESULT_SAFE_CHARS:
            result = self._oversized_result(result, serialized_chars=len(serialized))
            serialized = self._serialize(result)
        # The replacement envelope is intentionally far below the service's
        # atomic-result ceiling.  This assertion prevents a future schema change
        # from reintroducing arbitrary JSON slicing at the model boundary.
        if len(serialized) > self.max_result_chars:  # pragma: no cover - invariant guard
            raise RuntimeError("bounded platform result envelope exceeds its atomic limit")
        return LiliesToolResult(serialized, is_error=not result.ok)

    @staticmethod
    def _serialize(result: PlatformToolEnvelope) -> str:
        return json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _oversized_result(
        self,
        result: PlatformToolEnvelope,
        *,
        serialized_chars: int,
    ) -> PlatformToolEnvelope:
        return PlatformToolEnvelope(
            ok=False,
            operation=self.name,
            request_id=result.request_id,
            status_code=502,
            contract_digest=result.contract_digest,
            data={},
            error=PlatformToolError(
                code="platform_result_too_large",
                message=(
                    "the platform result exceeded the bounded model-result wire; "
                    "use a scoped artifact chunk or a narrower public read"
                ),
                retryable=False,
                failure_owner="platform",
                expected={"maximum_serialized_chars": PLATFORM_MODEL_RESULT_SAFE_CHARS},
                actual={"serialized_chars": serialized_chars},
                evidence_ref=None,
            ),
            evidence_refs=[],
        )

    def _client_failure(self, error: Exception) -> PlatformToolEnvelope:
        digest = self.client.contract_digest or ZERO_CONTRACT_DIGEST
        if isinstance(error, ValidationError | ValueError):
            code = "invalid_request"
            message = "platform tool input did not match the public request schema"
            status_code = 422
            retryable = False
            failure_owner = "task_author"
            expected = "the published operation request schema"
            actual = "invalid tool input"
        elif isinstance(error, LiliesPlatformContractNotLoaded):
            code = "contract_not_loaded"
            message = "fetch and validate the scoped platform contract before this operation"
            status_code = 409
            retryable = True
            failure_owner = "lilies"
            expected = "a verified platform_contract_get result"
            actual = "no fetched contract is bound to this client"
        elif isinstance(error, LiliesPlatformOperationUnavailable):
            code = "operation_not_available"
            message = "the operation is absent from the scoped platform contract"
            status_code = 403
            retryable = False
            failure_owner = "user_permission"
            expected = "an operation published for this task credential"
            actual = "operation omitted by the scoped contract"
        elif isinstance(error, LiliesPlatformProtocolError):
            code = "protocol_error"
            message = "the platform response did not match the public wire contract"
            status_code = 502
            retryable = True
            failure_owner = "platform"
            expected = "a valid public platform result envelope"
            actual = "invalid HTTP response or contract digest"
        elif isinstance(error, httpx.HTTPError):
            code = "platform_unavailable"
            message = "the public platform endpoint is unavailable"
            status_code = 503
            retryable = True
            failure_owner = "environment"
            expected = "a reachable public platform endpoint"
            actual = "HTTP transport failure"
        else:
            code = "platform_client_failure"
            message = "the platform HTTP adapter failed locally"
            status_code = 500
            retryable = False
            failure_owner = "lilies"
            expected = "a valid public platform invocation"
            actual = f"local adapter exception ({type(error).__name__})"
        return PlatformToolEnvelope(
            ok=False,
            operation=self.name,
            request_id=uuid4(),
            status_code=status_code,
            contract_digest=digest,
            data={},
            error=PlatformToolError(
                code=code,
                message=message,
                retryable=retryable,
                failure_owner=failure_owner,
                expected=expected,
                actual=actual,
                evidence_ref=None,
            ),
            evidence_refs=[],
        )


def build_lilies_platform_registry(
    client: LiliesPlatformClient,
    *,
    include_core_tools: bool = True,
    allowed_operations: set[str] | frozenset[str] | None = None,
) -> LiliesToolRegistry:
    registry = build_lilies_core_registry() if include_core_tools else LiliesToolRegistry()
    definitions: tuple[tuple[str, type[StrictToolInput], bool], ...] = (
        ("platform_contract_get", ContractGetInput, False),
        ("platform_block_search", BlockSearchInput, False),
        ("platform_block_get", BlockGetInput, False),
        ("platform_tool_catalog", ToolCatalogInput, False),
        (
            "platform_connector_authorization_issue",
            ConnectorAuthorizationIssueInput,
            True,
        ),
        ("platform_application_create", ApplicationCreateInput, True),
        ("platform_application_get", ApplicationGetInput, False),
        ("platform_draft_inspect", DraftInspectInput, False),
        ("platform_draft_apply", DraftApplyInput, True),
        ("platform_tests_run", TestsRunInput, True),
        ("platform_run_start", RunStartInput, True),
        ("platform_run_get", RunGetInput, False),
        ("platform_run_resume", RunResumeInput, True),
        ("platform_run_cancel", RunCancelInput, True),
        ("platform_trace_get", TraceGetInput, False),
        ("platform_artifact_read", ArtifactReadInput, False),
        ("platform_publish", PublishInput, True),
    )
    for name, input_model, mutating in definitions:
        if allowed_operations is not None and name not in allowed_operations:
            continue
        registry.register(
            PlatformHttpTool(
                client,
                name=name,
                input_model=input_model,
                mutating=mutating,
                side_effecting=mutating,
            )
        )
    return registry
