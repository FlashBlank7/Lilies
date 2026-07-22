from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .collaboration_models import (
    CollaborationReportPayload,
    LiliesReprobeResultPayload,
    VerificationClaimPayload,
)
from .lilies_collaboration_client import (
    CollaborationHttpResult,
    LiliesCollaborationClient,
)
from .lilies_models import IdempotencyKey
from .lilies_tools import (
    LiliesTool,
    LiliesToolContext,
    LiliesToolRegistry,
    LiliesToolResult,
    StrictToolInput,
)


class CollaborationReportSubmitInput(StrictToolInput):
    operation: Literal["submit", "revise", "reprobe", "withdraw"] = "submit"
    idempotency_key: IdempotencyKey
    report_id: UUID | None = None
    expected_report_revision: int | None = Field(default=None, ge=1)
    report: CollaborationReportPayload | None = None
    result: LiliesReprobeResultPayload | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def operation_fields_are_exact(self) -> CollaborationReportSubmitInput:
        if self.operation == "submit":
            if (
                self.report is None
                or self.report_id is not None
                or self.expected_report_revision is not None
                or self.result is not None
                or self.reason is not None
            ):
                raise ValueError("submit requires report only")
        elif self.operation == "revise":
            if (
                self.report_id is None
                or self.expected_report_revision is None
                or self.report is None
                or self.result is not None
                or self.reason is not None
            ):
                raise ValueError("revise requires report binding, revision, and report")
        elif self.operation == "reprobe":
            if (
                self.report_id is None
                or self.expected_report_revision is None
                or self.result is None
                or self.report is not None
                or self.reason is not None
            ):
                raise ValueError("reprobe requires report binding, revision, and result")
        elif (
            self.report_id is None
            or self.expected_report_revision is None
            or self.reason is None
            or self.report is not None
            or self.result is not None
        ):
            raise ValueError("withdraw requires report binding, revision, and reason")
        return self


class CollaborationUpdatesReadInput(StrictToolInput):
    after: int | None = Field(default=None, ge=0)
    acknowledge_through: int | None = Field(default=None, ge=0)
    limit: int = Field(default=200, ge=1, le=500)

    @model_validator(mode="after")
    def cursor_order_is_monotonic(self) -> CollaborationUpdatesReadInput:
        if (
            self.after is not None
            and self.acknowledge_through is not None
            and self.after < self.acknowledge_through
        ):
            raise ValueError("after cannot precede acknowledge_through")
        return self


class CollaborationVerificationClaimInput(StrictToolInput):
    idempotency_key: IdempotencyKey
    claim: VerificationClaimPayload


_DESCRIPTIONS = {
    "collaboration_report_submit": (
        "Submit a new evidence-backed report, revise that same report after an evidence "
        "request, or submit its black-box reprobe result. Select only the enumerated "
        "operation; routing remains platform-determined and cannot bypass approval."
    ),
    "collaboration_updates_read": (
        "Read persisted approvals, task amendments, environment responses, developer "
        "responses, verification results, and controls from this task's durable cursor."
    ),
    "collaboration_verification_claim": (
        "Submit a frozen ready-for-independent-verification claim for this task. "
        "The claim is not a pass and remains subject to an independent verifier."
    ),
}


class _CollaborationHttpTool(LiliesTool):
    requires_permission = False
    handles_input_validation = True
    preserve_result_integrity = True
    max_result_chars = 500_000

    def __init__(
        self,
        client: LiliesCollaborationClient,
        *,
        name: str,
        input_model: type[StrictToolInput],
        mutating: bool,
    ) -> None:
        self.client = client
        self.name = name
        self.description = _DESCRIPTIONS[name]
        self.input_model = input_model
        self.mutating = mutating
        self.side_effecting = mutating
        self.dangerous = False

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        try:
            args = self.input_model.model_validate(data)
            result = await self._invoke(
                args.model_dump(mode="json", exclude_none=True), context=context
            )
        except (TypeError, ValueError) as error:
            result = CollaborationHttpResult(
                ok=False,
                status_code=422,
                error={
                    "code": "invalid_collaboration_request",
                    "message": "collaboration tool input did not match its public schema",
                    "retryable": False,
                    "error_type": type(error).__name__,
                },
            )
        serialized = json.dumps(
            result.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(serialized) > self.max_result_chars:
            result = CollaborationHttpResult(
                ok=False,
                status_code=502,
                error={
                    "code": "collaboration_result_too_large",
                    "message": "the collaboration result exceeded its bounded model wire",
                    "retryable": True,
                },
            )
            serialized = result.model_dump_json(exclude_none=True)
        return LiliesToolResult(serialized, is_error=not result.ok)

    async def _invoke(
        self,
        payload: dict[str, Any],
        *,
        context: LiliesToolContext,
    ) -> CollaborationHttpResult:
        if self.name == "collaboration_report_submit":
            operation = str(payload.pop("operation", "submit"))
            report_id = payload.pop("report_id", None)
            if operation == "submit":
                revision = await self._current_channel_revision()
                if isinstance(revision, CollaborationHttpResult):
                    return revision
                payload["expected_channel_revision"] = revision
                result = await self.client.submit_report(payload)
            elif operation == "revise":
                result = await self.client.revise_report(UUID(str(report_id)), payload)
            elif operation == "reprobe":
                result = await self.client.submit_reprobe(UUID(str(report_id)), payload)
            else:
                result = await self.client.withdraw_report(UUID(str(report_id)), payload)
            return await self._attach_channel_state(result)
        if self.name == "collaboration_verification_claim":
            revision = await self._current_channel_revision()
            if isinstance(revision, CollaborationHttpResult):
                return revision
            payload["expected_channel_revision"] = revision
            return await self._attach_channel_state(
                await self.client.submit_verification_claim(payload)
            )
        acknowledged = payload.get("acknowledge_through")
        acknowledgement: dict[str, Any] | None = None
        if acknowledged is not None:
            cursor = await self._current_reader_cursor()
            if isinstance(cursor, CollaborationHttpResult):
                return cursor
            if acknowledged > cursor["ack_seq"]:
                ack = await self.client.acknowledge(
                    {
                        "idempotency_key": (
                            f"collaboration.ack.{self.client.channel_id.hex}.{acknowledged}"
                        ),
                        "expected_cursor_revision": cursor["revision"],
                        "reader_role": "lilies",
                        "reader_id": cursor["reader_id"],
                        "ack_seq": acknowledged,
                    }
                )
                if not ack.ok:
                    return ack
                acknowledgement = ack.data
            else:
                acknowledgement = cursor
        updates = await self.client.read_updates(
            after=payload.get("after"),
            limit=int(payload.get("limit", 200)),
        )
        if acknowledgement is not None and updates.ok:
            updates.data["acknowledgement"] = acknowledgement
        return await self._attach_channel_state(updates)

    async def _current_reader_cursor(
        self,
    ) -> dict[str, Any] | CollaborationHttpResult:
        state = await self.client.channel_state()
        if not state.ok:
            return state
        cursor = state.data.get("reader_cursor")
        if not isinstance(cursor, dict):
            return self._invalid_channel_state(
                "channel state omitted its durable reader cursor"
            )
        ack_seq = cursor.get("ack_seq")
        revision = cursor.get("revision")
        reader_id = cursor.get("reader_id")
        if (
            isinstance(ack_seq, bool)
            or not isinstance(ack_seq, int)
            or ack_seq < 0
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(reader_id, str)
            or not reader_id
            or cursor.get("reader_role") != "lilies"
            or cursor.get("channel_id") != str(self.client.channel_id)
        ):
            return self._invalid_channel_state(
                "channel state returned an invalid durable reader cursor"
            )
        return {
            "channel_id": cursor["channel_id"],
            "reader_role": "lilies",
            "reader_id": reader_id,
            "ack_seq": ack_seq,
            "revision": revision,
        }

    async def _current_channel_revision(
        self,
    ) -> int | CollaborationHttpResult:
        state = await self.client.channel_state()
        if not state.ok:
            return state
        revision = state.data.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return self._invalid_channel_state(
                "channel state omitted its valid integer revision"
            )
        return revision

    @staticmethod
    def _invalid_channel_state(message: str) -> CollaborationHttpResult:
        return CollaborationHttpResult(
            ok=False,
            status_code=502,
            error={
                "code": "collaboration_state_invalid",
                "message": message,
                "retryable": True,
            },
        )

    async def _attach_channel_state(
        self,
        result: CollaborationHttpResult,
    ) -> CollaborationHttpResult:
        if not result.ok:
            return result
        state = await self.client.channel_state()
        if state.ok:
            result.data["channel_state"] = state.data
        else:
            result.data["channel_state_error"] = state.error or {
                "code": "collaboration_state_unavailable",
                "message": "channel revision could not be refreshed",
                "retryable": True,
            }
        return result


def register_lilies_collaboration_tools(
    registry: LiliesToolRegistry,
    client: LiliesCollaborationClient,
) -> LiliesToolRegistry:
    """Add the three temporary tools to an already scoped assignment registry."""

    definitions: tuple[tuple[str, type[StrictToolInput], bool], ...] = (
        ("collaboration_report_submit", CollaborationReportSubmitInput, True),
        ("collaboration_updates_read", CollaborationUpdatesReadInput, False),
        (
            "collaboration_verification_claim",
            CollaborationVerificationClaimInput,
            True,
        ),
    )
    for name, input_model, mutating in definitions:
        registry.register(
            _CollaborationHttpTool(
                client,
                name=name,
                input_model=input_model,
                mutating=mutating,
            )
        )
    return registry
