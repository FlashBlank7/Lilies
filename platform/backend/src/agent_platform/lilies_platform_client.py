from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .lilies_models import Digest
from .lilies_platform_contract import operation_by_name, validate_contract_digest


ZERO_CONTRACT_DIGEST = "sha256:" + "0" * 64
SUPPORTED_PLATFORM_CONTRACT_SCHEMA_VERSIONS = frozenset({"1.0"})
MIN_PLATFORM_CONTRACT_VERSION = 1
MAX_PLATFORM_CONTRACT_VERSION = 2**63 - 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class StrictPlatformModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlatformToolError(StrictPlatformModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4_000)
    retryable: bool
    failure_owner: str = Field(
        pattern=r"^(lilies|user_permission|task_author|environment|platform)$"
    )
    expected: Any
    actual: Any
    evidence_ref: str | None


class PlatformToolEnvelope(StrictPlatformModel):
    ok: bool
    operation: str = Field(min_length=1, max_length=120)
    request_id: UUID
    status_code: int = Field(ge=100, le=599)
    contract_digest: Digest
    data: Any
    error: PlatformToolError | None
    evidence_refs: list[str] = Field(default_factory=list, max_length=1_000)

    @model_validator(mode="after")
    def result_and_error_are_consistent(self) -> PlatformToolEnvelope:
        if self.ok and self.error is not None:
            raise ValueError("successful platform result cannot carry an error")
        if not self.ok and self.error is None:
            raise ValueError("failed platform result must carry an error")
        if self.ok != (self.status_code < 400):
            raise ValueError("ok must agree with status_code")
        return self


class LiliesPlatformProtocolError(RuntimeError):
    """The black-box endpoint did not honor its public wire contract."""


class LiliesPlatformContractNotLoaded(LiliesPlatformProtocolError):
    """A scoped operation was attempted before the public contract was fetched."""


class LiliesPlatformOperationUnavailable(LiliesPlatformProtocolError):
    """An operation is absent from the credential-filtered public contract."""


class LiliesPlatformClient:
    """HTTP-only client used by the standalone Lilies process.

    This module intentionally imports no workflow storage, runtime, application,
    block, or platform service implementation.
    """

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str | SecretStr,
        assignment_id: UUID | str,
        session_id: UUID | str,
        contract_digest: str | None = None,
        require_contract_fetch: bool = False,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = httpx.URL(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("platform base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("platform base_url cannot contain credentials, query, or fragment")
        self.base_url = str(parsed).rstrip("/")
        self.access_token = (
            access_token if isinstance(access_token, SecretStr) else SecretStr(access_token)
        )
        self.assignment_id = UUID(str(assignment_id))
        self.session_id = UUID(str(session_id))
        self.contract_digest = contract_digest
        self.require_contract_fetch = require_contract_fetch
        self._fetched_operations: frozenset[str] | None = None
        self._contract_schema_version: str | None = None
        self._highest_contract_version: int | None = None
        self._highest_contract_schema_digest: str | None = None
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def __repr__(self) -> str:
        return (
            f"LiliesPlatformClient(base_url={self.base_url!r}, "
            f"assignment_id={self.assignment_id!r}, session_id={self.session_id!r}, "
            f"contract_digest={self.contract_digest!r})"
        )

    @property
    def contract_schema_version(self) -> str | None:
        return self._contract_schema_version

    @property
    def highest_contract_version(self) -> int | None:
        return self._highest_contract_version

    @property
    def contract_schema_digest(self) -> str | None:
        return self._highest_contract_schema_digest

    async def contract_get(
        self,
        *,
        tool_call_id: str,
        idempotency_key: str | None = None,
    ) -> PlatformToolEnvelope:
        result = await self.invoke(
            "platform_contract_get",
            {},
            tool_call_id=tool_call_id,
            idempotency_key=idempotency_key,
            allow_missing_contract=True,
        )
        if not result.ok:
            return result
        if not isinstance(result.data, dict) or not validate_contract_digest(result.data):
            raise LiliesPlatformProtocolError("platform contract digest validation failed")
        if result.contract_digest != result.data.get("contract_digest"):
            raise LiliesPlatformProtocolError(
                "result envelope and platform contract use different digests"
            )
        schema_version = result.data.get("schema_version")
        if (
            not isinstance(schema_version, str)
            or schema_version not in SUPPORTED_PLATFORM_CONTRACT_SCHEMA_VERSIONS
        ):
            raise LiliesPlatformProtocolError(
                "platform contract schema_version is unsupported"
            )
        contract_version = result.data.get("contract_version")
        if (
            not isinstance(contract_version, int)
            or isinstance(contract_version, bool)
            or not MIN_PLATFORM_CONTRACT_VERSION
            <= contract_version
            <= MAX_PLATFORM_CONTRACT_VERSION
        ):
            raise LiliesPlatformProtocolError(
                "platform contract contract_version is outside the supported range"
            )
        contract_schema_digest = result.data.get("contract_schema_digest")
        if (
            not isinstance(contract_schema_digest, str)
            or not _DIGEST_RE.fullmatch(contract_schema_digest)
        ):
            raise LiliesPlatformProtocolError(
                "platform contract contract_schema_digest is invalid"
            )
        if (
            self._highest_contract_version is not None
            and contract_version < self._highest_contract_version
        ):
            raise LiliesPlatformProtocolError(
                "platform contract version rolled back below the highest observed version"
            )
        if (
            self._highest_contract_version == contract_version
            and self._highest_contract_schema_digest is not None
            and contract_schema_digest != self._highest_contract_schema_digest
        ):
            raise LiliesPlatformProtocolError(
                "platform contract schema changed without a contract version increment"
            )
        operations = result.data.get("operations")
        if not isinstance(operations, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in operations
        ):
            raise LiliesPlatformProtocolError("platform contract operations are invalid")
        fetched_operations = frozenset(str(item["name"]) for item in operations)
        self._fetched_operations = fetched_operations
        self._contract_schema_version = schema_version
        self._highest_contract_version = contract_version
        self._highest_contract_schema_digest = contract_schema_digest
        self.contract_digest = result.contract_digest
        return result

    async def invoke(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        tool_call_id: str,
        idempotency_key: str | None = None,
        allow_missing_contract: bool = False,
    ) -> PlatformToolEnvelope:
        spec = operation_by_name(operation)
        if operation != "platform_contract_get":
            if self.require_contract_fetch and self._fetched_operations is None:
                raise LiliesPlatformContractNotLoaded(
                    "platform contract must be fetched before invoking an operation"
                )
            if (
                self._fetched_operations is not None
                and operation not in self._fetched_operations
            ):
                raise LiliesPlatformOperationUnavailable(
                    f"{operation} is not present in the fetched scoped contract"
                )
        request_payload = dict(payload)
        self._validate_reserved_inputs(operation, request_payload)
        if idempotency_key is None:
            raw_key = request_payload.get("idempotency_key")
            idempotency_key = str(raw_key) if raw_key else f"read-{uuid4().hex}"
        else:
            raw_key = request_payload.get("idempotency_key")
            if raw_key is not None and str(raw_key) != idempotency_key:
                raise ValueError("payload idempotency_key does not match request correlation")
        path = self._render_path(spec["path"], request_payload)
        digest = self.contract_digest
        if digest is None and not allow_missing_contract:
            raise LiliesPlatformContractNotLoaded(
                "platform contract must be fetched before invoking an operation"
            )
        headers = {
            "Authorization": f"Bearer {self.access_token.get_secret_value()}",
            "X-Lilies-Assignment-ID": str(self.assignment_id),
            "X-Lilies-Session-ID": str(self.session_id),
            "X-Lilies-Tool-Call-ID": tool_call_id,
            "X-Lilies-Idempotency-Key": idempotency_key,
            "X-Lilies-Contract-Digest": digest or ZERO_CONTRACT_DIGEST,
            "Accept": "application/json",
        }
        method = spec["method"]
        kwargs: dict[str, Any] = {"headers": headers}
        if method == "GET":
            kwargs["params"] = request_payload
        else:
            kwargs["json"] = request_payload
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, **kwargs)
        try:
            raw = response.json()
        except ValueError as error:
            raise LiliesPlatformProtocolError(
                f"{operation} returned non-JSON HTTP {response.status_code}"
            ) from error
        try:
            envelope = PlatformToolEnvelope.model_validate(raw)
        except Exception as error:
            raise LiliesPlatformProtocolError(
                f"{operation} returned an invalid result envelope"
            ) from error
        if envelope.operation != operation:
            raise LiliesPlatformProtocolError(
                f"operation mismatch: expected {operation}, received {envelope.operation}"
            )
        if envelope.status_code != response.status_code:
            raise LiliesPlatformProtocolError(
                "result envelope status_code does not match HTTP status"
            )
        if operation != "platform_contract_get" and digest != envelope.contract_digest:
            if not (
                envelope.error is not None
                and envelope.error.code == "contract_drift"
                or response.headers.get("X-Lilies-Idempotent-Replay", "").casefold()
                == "true"
            ):
                raise LiliesPlatformProtocolError(
                    "result envelope contract digest differs from the fetched contract"
                )
        return envelope

    @staticmethod
    def _validate_reserved_inputs(operation: str, payload: dict[str, Any]) -> None:
        inputs: Any = None
        if operation == "platform_run_start":
            inputs = payload.get("inputs")
        elif operation == "platform_draft_apply" and payload.get("op") == "add_test":
            data = payload.get("data")
            test = data.get("test") if isinstance(data, dict) else None
            inputs = test.get("inputs") if isinstance(test, dict) else None
        if isinstance(inputs, dict) and any(str(key).startswith("__") for key in inputs):
            raise ValueError("reserved runtime input keys are not public")

    @staticmethod
    def _render_path(template: str, payload: dict[str, Any]) -> str:
        path = template
        for field in ("application_id", "run_id", "block_type", "artifact_id"):
            marker = "{" + field + "}"
            if marker not in path:
                continue
            if field not in payload:
                raise ValueError(f"{field} is required for this platform operation")
            raw = str(payload.pop(field))
            segments = raw.split("/")
            if (
                not raw
                or raw.startswith("/")
                or "\\" in raw
                or any(segment in {"", ".", ".."} for segment in segments)
                or len(segments) != 1
            ):
                raise ValueError(f"{field} must be a normalized path value")
            encoded = quote(raw, safe="")
            path = path.replace(marker, encoded)
        if "{" in path or "}" in path:
            raise ValueError("platform operation path is missing a required parameter")
        return path
