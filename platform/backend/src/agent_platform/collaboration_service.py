from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from pydantic import BaseModel, SecretStr, ValidationError

from .collaboration_models import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    ApprovalMode,
    ChannelCloseRequest,
    ChannelSettingsRequest,
    ChannelStatus,
    ClaimStatus,
    CollaborationChannel,
    CollaborationMessageEnvelope,
    CollaborationReport,
    ControlKind,
    ControlMessage,
    DeveloperLease,
    DeveloperResponse,
    DeveloperResponseRequest,
    DeveloperSourcePromotionRequest,
    DeveloperWorkerReceiptReference,
    DeveloperWorkspaceBinding,
    EvidenceRef,
    EnvironmentOutcome,
    EnvironmentResponse,
    EnvironmentResponseRequest,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    LiliesReprobeResult,
    LiliesReprobeResultRequest,
    MessageType,
    MessageVisibility,
    PayloadSchema,
    ReaderAckRequest,
    ReaderCursor,
    ReportCategory,
    ReportDecision,
    ReportRevisionRequest,
    ReportRoute,
    ReportStatus,
    ReportSubmitRequest,
    ReportWithdrawalRequest,
    ReprobeOutcome,
    SenderRole,
    TaskAmendmentOutcome,
    TaskPackageAmendment,
    TaskPackageAmendmentRequest,
    VerificationClaim,
    VerificationClaimRequest,
    VerificationResult,
    VerificationResultRequest,
    VerificationVerdict,
    sanitize_collaboration_payload,
    validate_collaboration_payload_safety,
)
from .formal_developer_worker_broker import (
    DeveloperWorkerReceipt,
    DeveloperWorkerRunRequest,
)
from .lilies_models import AssignmentMode, CollaborationScope


_CAPABILITY_CATEGORIES = frozenset(
    {
        ReportCategory.platform_capability_gap,
        ReportCategory.platform_defect_suspected,
    }
)
_OPEN_CHANNEL_STATUSES = frozenset({ChannelStatus.active, ChannelStatus.disconnected})
_DEVELOPER_VISIBLE_REPORT_STATUSES = frozenset(
    {
        ReportStatus.approved_for_codex,
        ReportStatus.implementing,
        ReportStatus.ready_for_lilies_verification,
        ReportStatus.routed_to_task_author,
        ReportStatus.task_package_amended,
        ReportStatus.environment_failed,
        ReportStatus.environment_restored,
        ReportStatus.unresolved,
    }
)


def _studio_derived_status(
    *,
    channel: CollaborationChannel,
    reports: Sequence[CollaborationReport],
    active_leases: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None,
    unread_count: int,
) -> dict[str, Any]:
    lease_by_report = {
        str(item.get("report_id")): item for item in active_leases if item.get("report_id")
    }
    open_reports = [
        report
        for report in reports
        if report.status
        not in {
            ReportStatus.rejected,
            ReportStatus.withdrawn,
            ReportStatus.independently_verified,
            ReportStatus.task_package_amended,
            ReportStatus.rejected_with_evidence,
            ReportStatus.environment_restored,
        }
    ]
    report = max(open_reports, key=lambda item: item.updated_at) if open_reports else None

    def result(
        block_code: str,
        block_label: str,
        owner_role: str,
        owner_id: str,
        owner_label: str,
        why_waiting: str,
        next_code: str,
        next_label: str,
    ) -> dict[str, Any]:
        return {
            "current_block": {"code": block_code, "label": block_label},
            "owner": {
                "role": owner_role,
                "id": owner_id,
                "label": owner_label,
            },
            "why_waiting": why_waiting,
            "next_action": {"code": next_code, "label": next_label},
            "unread_count": unread_count,
        }

    if report is not None:
        status = report.status
        if status is ReportStatus.awaiting_user_review:
            return result(
                "capability_approval",
                "平台能力报告等待你的审查",
                "user",
                "studio-user",
                "你",
                "报告在批准前不会向 Codex 暴露正文或证据。",
                "review_report",
                "阅读 expected、actual、尝试和证据后批准、补证据或拒绝。",
            )
        if status in {
            ReportStatus.observed,
            ReportStatus.evidence_collecting,
            ReportStatus.needs_more_evidence,
            ReportStatus.verification_failed,
        }:
            return result(
                "lilies_evidence",
                "莉莉丝正在补齐或复验报告证据",
                "lilies",
                "lilies-local",
                "莉莉丝",
                "当前证据尚不足以继续路由，或原修复复验未通过。",
                "await_lilies_evidence",
                "等待同一莉莉丝会话提交新证据或新的复验结果。",
            )
        if status in {ReportStatus.approved_for_codex, ReportStatus.implementing}:
            lease = lease_by_report.get(str(report.report_id))
            owner_id = str(lease.get("owner_id")) if lease else "codex-developer"
            return result(
                "developer_implementation",
                "Codex 正在处理已批准的平台报告" if lease else "已批准报告等待 Codex 领取",
                "codex",
                owner_id,
                owner_id,
                "报告已获用户批准；开发端必须持有有效租约才能回传结果。",
                "developer_response" if lease else "acquire_developer_lease",
                "提交包含 commit、测试、已知限制和复验步骤的 DeveloperResponse。"
                if lease
                else "由 Codex 开发端领取报告租约。",
            )
        if status is ReportStatus.ready_for_lilies_verification:
            return result(
                "lilies_reprobe",
                "实现已返回，等待莉莉丝用原失败输入复验",
                "lilies",
                "lilies-local",
                "莉莉丝",
                "DeveloperResponse 不能自行关闭报告，必须由莉莉丝黑箱复验。",
                "run_reprobe",
                "按返回的复验步骤重跑并提交通过或失败证据。",
            )
        if status is ReportStatus.lilies_verified:
            return result(
                "independent_verification",
                "莉莉丝复验通过，等待独立验证",
                "verifier",
                "independent-verifier",
                "独立验证器",
                "正式完成需要独立检查冻结 claim 和真实宿主证据。",
                "verify_claim",
                "独立验证器提交 expected/actual 和证据结论。",
            )
        if status in {
            ReportStatus.reported,
            ReportStatus.routed_to_task_author,
            ReportStatus.environment_failed,
            ReportStatus.unresolved,
        }:
            return result(
                "task_or_environment_owner",
                "题包或环境问题等待负责人处理",
                "task_author",
                "codex-task-author",
                "题包/环境负责人",
                "该类问题不能作为平台能力缺口绕过，也不能用 mock 替代。",
                "amend_or_restore",
                "修订题包或恢复真实环境，并附可复查证据。",
            )
        if status in {ReportStatus.lilies_rechecks, ReportStatus.lilies_health_checks}:
            return result(
                "lilies_recheck",
                "题包或环境已更新，等待莉莉丝重新检查",
                "lilies",
                "lilies-local",
                "莉莉丝",
                "负责人已返回处理结果，但仍需原会话自行确认。",
                "lilies_recheck",
                "莉莉丝重跑同一检查并继续任务或提交差异。",
            )

    frozen_claims = [item for item in claims if str(item.get("status")) == ClaimStatus.frozen.value]
    if frozen_claims:
        return result(
            "independent_verification",
            "完成 claim 等待独立验证",
            "verifier",
            "independent-verifier",
            "独立验证器",
            "莉莉丝的完成声明已冻结，但尚未成为正式成功。",
            "verify_claim",
            "独立验证器检查真实数据、副作用、工件与隐藏验收。",
        )

    raw_assignment = (context or {}).get("assignment")
    assignment = _as_dict(raw_assignment) if raw_assignment is not None else {}
    connection_status = str(assignment.get("connection_status") or "unknown")
    daemon_status = str(assignment.get("daemon_status") or "")
    assignment_status = str(assignment.get("status") or "")
    if connection_status not in {"connected", "unknown"}:
        return result(
            "daemon_connection",
            "本地莉莉丝连接不可用",
            "user",
            "studio-user",
            "你",
            "平台保留了完整时间线，但当前无法联系已配对 daemon。",
            "reconnect_daemon",
            "恢复或重新配对同一 daemon 后继续，不会改用旧 Builder。",
        )
    if daemon_status == "waiting_permission":
        return result(
            "runtime_permission",
            "运行权限等待你的决定",
            "user",
            "studio-user",
            "你",
            "高风险工具调用必须单独批准，不会被能力报告批准替代。",
            "resolve_permission",
            "查看脱敏输入后选择“允许一次”或“拒绝”。",
        )
    if assignment_status in {"completed", "cancelled"}:
        return result(
            "none",
            "当前没有阻塞",
            "platform",
            "platform",
            "平台",
            "任务已结束，历史和因果链仍可读取与导出。",
            "inspect_history",
            "查看结果、证据或导出完整因果链。",
        )
    return result(
        "lilies_execution",
        "莉莉丝正在执行当前任务",
        "lilies",
        "lilies-local",
        "莉莉丝",
        "当前没有需要用户或 Codex 处理的阻塞。",
        "await_progress",
        "等待新的可观测消息、工具结果、报告或完成 claim。",
    )


class CollaborationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        identifiers: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.identifiers = dict(identifiers or {})

    def public_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.identifiers:
            detail["identifiers"] = self.identifiers
        return detail


class CollaborationNotFound(CollaborationError):
    def __init__(self) -> None:
        super().__init__(
            "collaboration_not_found",
            "collaboration resource was not found",
            status_code=404,
        )


class CollaborationConflict(CollaborationError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=409)


class CollaborationClosed(CollaborationError):
    def __init__(self) -> None:
        super().__init__(
            "collaboration_channel_closed",
            "the collaboration channel is closed for new writes",
            status_code=410,
        )


class CollaborationBudgetExhausted(CollaborationError):
    def __init__(self, *, report_id: UUID, reason: str) -> None:
        super().__init__(
            "report_evidence_budget_exhausted",
            "the formal report evidence budget is exhausted and the assignment was stopped",
            status_code=409,
            identifiers={"report_id": str(report_id), "reason": reason},
        )


class CollaborationSubscriberOverflow(CollaborationError):
    def __init__(self) -> None:
        super().__init__(
            "collaboration_subscriber_overflow",
            "the transient subscriber queue overflowed; reconnect from the durable cursor",
            status_code=409,
        )


@dataclass(frozen=True, slots=True)
class CollaborationPrincipal:
    role: SenderRole
    sender_id: str
    scopes: frozenset[str]
    channel_id: UUID | None = None
    assignment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class IssuedCollaborationChannel:
    channel: CollaborationChannel
    credential_ref: str
    access_token: SecretStr


@dataclass(eq=False, slots=True)
class CollaborationEventSubscription:
    channel_id: UUID
    queue: asyncio.Queue[int]
    overflowed: asyncio.Event
    closed: bool = False


def _replay_receipt_after_conflict(
    *,
    receipt_operation: str,
    digest_operation: str,
    scope_parameter: str,
    binding_parameters: tuple[str, ...],
    receipt_actor_role: SenderRole | None = None,
) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[dict[str, Any]]]]:
    """Recover an exact concurrent retry after its optimistic precheck loses.

    A second process can miss the first receipt while the winning transaction is
    still uncommitted, then observe the winner's advanced CAS revision.  Every
    receipt-backed mutation therefore gets one final exact-digest lookup before
    exposing that otherwise-correct conflict to the caller.
    """

    def decorate(
        method: Callable[..., Awaitable[dict[str, Any]]],
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        @wraps(method)
        async def wrapped(self: CollaborationService, *args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await method(self, *args, **kwargs)
            except (CollaborationConflict, CollaborationClosed):
                request = kwargs.get("request")
                principal = kwargs.get("principal")
                scope_value = kwargs.get(scope_parameter)
                if request is None or principal is None or scope_value is None:
                    raise
                actor = self._principal(principal)
                if receipt_actor_role is not None and actor.role is not receipt_actor_role:
                    actor = CollaborationPrincipal(
                        role=receipt_actor_role,
                        sender_id=actor.sender_id,
                        scopes=actor.scopes,
                        channel_id=actor.channel_id,
                        assignment_id=actor.assignment_id,
                    )
                bindings = {name: kwargs[name] for name in binding_parameters if name in kwargs}
                client_request_digest = self._client_request_digest(
                    digest_operation,
                    request,
                    **bindings,
                )
                replay = await self._operation_replay(
                    operation=receipt_operation,
                    scope_id=UUID(str(scope_value)),
                    actor=actor,
                    idempotency_key=request.idempotency_key,
                    client_request_digest=client_request_digest,
                )
                if replay is not None:
                    return replay
                raise

        return wrapped

    return decorate


def _replay_lease_after_conflict(
    operation: str,
) -> Callable[[Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[dict[str, Any]]]]:
    """Apply the same late-receipt rule to developer lease mutations."""

    def decorate(
        method: Callable[..., Awaitable[dict[str, Any]]],
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        @wraps(method)
        async def wrapped(self: CollaborationService, *args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                return await method(self, *args, **kwargs)
            except (CollaborationConflict, CollaborationClosed):
                request = kwargs.get("request")
                report_id = kwargs.get("report_id")
                principal = kwargs.get("principal")
                if request is None or report_id is None or principal is None:
                    raise
                actor = self._principal(principal)
                expected_revision = (
                    request.expected_report_revision
                    if operation == "acquire"
                    else request.expected_lease_revision
                )
                replay = await self._developer_lease_replay(
                    report_id=UUID(str(report_id)),
                    operation=operation,
                    actor=actor,
                    idempotency_key=request.idempotency_key,
                    expected_revision=expected_revision,
                    ttl_seconds=getattr(request, "ttl_seconds", None),
                    reason=getattr(request, "reason", None),
                )
                if replay is not None:
                    if operation == "acquire":
                        report = CollaborationReport.model_validate(
                            await self.store.get_report(UUID(str(report_id)))
                        )
                        self._require_developer_visible(report)
                        channel = await self._channel(report.channel_id)
                        self._require_open(channel)
                        return await self._lease_with_developer_workspace(
                            replay,
                            channel,
                        )
                    return replay
                raise

        return wrapped

    return decorate


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise TypeError("collaboration storage returned a non-object record")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError("collaboration storage returned a non-list record")


def _canonical_value(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: Any) -> str:
    digest = str(value)
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"


def _without_fields(value: Any, fields: set[str]) -> dict[str, Any]:
    projected = _as_dict(value) if not isinstance(value, Mapping) else dict(value)
    return {key: item for key, item in projected.items() if key not in fields}


class CollaborationService:
    """Role-separated orchestration over the dedicated collaboration tables."""

    def __init__(
        self,
        *,
        store: Any,
        enabled: bool,
        developer_token: str = "",
        verifier_token: str = "",
        reserved_role_tokens: Sequence[str] = (),
        now: Callable[[], datetime] = _utc_now,
        draft_state_provider: Callable[[str], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]
        | None = None,
        developer_commit_resolver: Callable[[str], Awaitable[bool] | bool] | None = None,
        developer_promotion_resolver: Callable[
            [CollaborationChannel, DeveloperResponse],
            Awaitable[bool] | bool,
        ]
        | None = None,
        developer_evidence_resolver: Callable[[str, EvidenceRef], Awaitable[bool] | bool]
        | None = None,
        studio_context_provider: Callable[
            [CollaborationChannel],
            Awaitable[Mapping[str, Any] | BaseModel] | Mapping[str, Any] | BaseModel,
        ]
        | None = None,
        developer_workspace_provider: Callable[
            [CollaborationChannel],
            Awaitable[Mapping[str, Any] | BaseModel] | Mapping[str, Any] | BaseModel,
        ]
        | None = None,
        developer_source_promotion_provider: Callable[
            [
                CollaborationChannel,
                CollaborationReport,
                DeveloperLease,
                DeveloperSourcePromotionRequest,
            ],
            Awaitable[Mapping[str, Any] | BaseModel]
            | Mapping[str, Any]
            | BaseModel,
        ]
        | None = None,
        developer_worker_provider: Callable[
            [
                CollaborationChannel,
                CollaborationReport,
                DeveloperLease,
                DeveloperWorkerRunRequest,
            ],
            Awaitable[Mapping[str, Any] | BaseModel]
            | Mapping[str, Any]
            | BaseModel,
        ]
        | None = None,
        developer_worker_receipt_resolver: Callable[
            [
                CollaborationChannel,
                CollaborationReport,
                DeveloperLease,
                UUID,
                DeveloperWorkerReceiptReference,
                bool,
            ],
            Awaitable[bool] | bool,
        ]
        | None = None,
        formal_archive_provider: Callable[
            [CollaborationChannel, Any, str],
            Awaitable[Mapping[str, Any] | BaseModel] | Mapping[str, Any] | BaseModel,
        ]
        | None = None,
        formal_source_response_recorder: Callable[
            [UUID, UUID],
            Awaitable[Any] | Any,
        ]
        | None = None,
        assignment_cancel_handler: Callable[
            [UUID, str, str],
            Awaitable[Any] | Any,
        ]
        | None = None,
        verification_claim_resolver: Callable[
            [CollaborationChannel, VerificationClaim],
            Awaitable[bool] | bool,
        ]
        | None = None,
        verification_result_resolver: Callable[
            [VerificationClaim, Any],
            Awaitable[bool] | bool,
        ]
        | None = None,
        require_frozen_verification_evidence: (
            bool
            | Callable[
                [CollaborationChannel],
                Awaitable[bool] | bool,
            ]
        ) = False,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self._developer_token = developer_token
        self._verifier_token = verifier_token
        self._reserved_role_tokens = tuple(
            token
            for token in dict.fromkeys(
                (developer_token, verifier_token, *(str(item) for item in reserved_role_tokens))
            )
            if token
        )
        self._now = now
        self._draft_state_provider = draft_state_provider
        self._developer_commit_resolver = developer_commit_resolver
        self._developer_promotion_resolver = developer_promotion_resolver
        self._developer_evidence_resolver = developer_evidence_resolver
        self._studio_context_provider = studio_context_provider
        self._developer_workspace_provider = developer_workspace_provider
        self._developer_source_promotion_provider = (
            developer_source_promotion_provider
        )
        self._developer_worker_provider = developer_worker_provider
        self._developer_worker_receipt_resolver = (
            developer_worker_receipt_resolver
        )
        self._formal_archive_provider = formal_archive_provider
        self._formal_source_response_recorder = formal_source_response_recorder
        self._assignment_cancel_handler = assignment_cancel_handler
        self._verification_claim_resolver = verification_claim_resolver
        self._verification_result_resolver = verification_result_resolver
        self._require_frozen_verification_evidence = require_frozen_verification_evidence
        self._event_subscribers: dict[UUID, set[CollaborationEventSubscription]] = {}

    async def _record_formal_source_response(
        self,
        *,
        channel_id: UUID,
        response_id: UUID,
    ) -> None:
        provider = self._formal_source_response_recorder
        if provider is None:
            return
        recorded = provider(channel_id, response_id)
        if inspect.isawaitable(recorded):
            await recorded

    async def _raise_report_evidence_budget_exhausted(
        self,
        record: Mapping[str, Any],
        *,
        report_id: UUID,
    ) -> None:
        reason = str(record.get("reason") or "max_report_evidence_rounds")
        assignment_id = UUID(str(record["assignment_id"]))
        if self._assignment_cancel_handler is not None:
            cancellation = self._assignment_cancel_handler(
                assignment_id,
                f"collaboration.report-budget.{report_id.hex}",
                f"report evidence budget exhausted: {reason}",
            )
            if inspect.isawaitable(cancellation):
                await cancellation
        channel_id = record.get("channel_id")
        if channel_id is not None:
            self._notify_events(UUID(str(channel_id)))
        raise CollaborationBudgetExhausted(report_id=report_id, reason=reason)

    async def initialize(self) -> None:
        await self.store.initialize()

    async def _current_draft(self, application_id: UUID) -> Mapping[str, Any] | None:
        if self._draft_state_provider is None:
            return None
        result = self._draft_state_provider(str(application_id))
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise CollaborationConflict(
                "draft_state_invalid",
                "current draft state is unavailable for claim verification",
            )
        return result

    async def _developer_workspace(
        self,
        channel: CollaborationChannel,
    ) -> DeveloperWorkspaceBinding | None:
        if self._developer_workspace_provider is None:
            return None
        try:
            result = self._developer_workspace_provider(channel)
            if inspect.isawaitable(result):
                result = await result
            binding = DeveloperWorkspaceBinding.model_validate(result)
        except Exception as error:
            raise CollaborationConflict(
                "developer_workspace_unavailable",
                "the formal developer workspace could not be verified",
            ) from error
        if (
            binding.task_id != channel.task_id
            or binding.task_revision != channel.task_revision
            or binding.assignment_id != channel.assignment_id
        ):
            raise CollaborationConflict(
                "developer_workspace_mismatch",
                "the developer workspace is not bound to this formal channel",
            )
        return binding

    async def _lease_with_developer_workspace(
        self,
        lease: Any,
        channel: CollaborationChannel,
    ) -> dict[str, Any]:
        result = _as_dict(lease)
        binding = await self._developer_workspace(channel)
        if binding is not None:
            result["developer_workspace"] = binding.model_dump(
                mode="json",
                exclude_none=True,
            )
        return result

    async def _require_trusted_verification_claim(
        self,
        *,
        channel: CollaborationChannel,
        claim: VerificationClaim,
    ) -> None:
        if not await self._frozen_verification_required(channel):
            return
        if claim.schema_version != "1.1":
            raise CollaborationConflict(
                "frozen_verification_claim_required",
                "formal verification requires a frozen v1.1 claim",
            )
        resolver = self._verification_claim_resolver
        if resolver is None:
            raise CollaborationConflict(
                "verification_claim_resolver_unavailable",
                "the trusted frozen-archive resolver is unavailable",
            )
        try:
            resolved = resolver(channel, claim)
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except Exception as error:
            raise CollaborationConflict(
                "verification_claim_resolution_failed",
                "the frozen claim could not be resolved against trusted evidence",
            ) from error
        if resolved is not True:
            raise CollaborationConflict(
                "verification_claim_untrusted",
                "the claim does not match a trusted frozen run archive",
            )

    async def _require_trusted_verification_result(
        self,
        *,
        channel: CollaborationChannel,
        claim: VerificationClaim,
        result: Any,
    ) -> None:
        if not await self._frozen_verification_required(channel):
            return
        if claim.schema_version != "1.1" or result.schema_version != "1.1":
            raise CollaborationConflict(
                "frozen_verification_result_required",
                "formal verification requires matching v1.1 claim and result evidence",
            )
        frozen_fields = (
            "task_package_digest",
            "environment_ready_digest",
            "archive_manifest_digest",
            "verification_process_digest",
            "frozen_context_digest",
            "validation_mode",
        )
        if any(
            str(getattr(result, field, None)) != str(getattr(claim, field, None))
            for field in frozen_fields
        ):
            raise CollaborationConflict(
                "verification_result_binding_mismatch",
                "verification result does not bind the exact frozen claim context",
            )
        resolver = self._verification_result_resolver
        if resolver is None:
            raise CollaborationConflict(
                "verification_result_resolver_unavailable",
                "the trusted verifier-result resolver is unavailable",
            )
        try:
            resolved = resolver(claim, result)
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except Exception as error:
            raise CollaborationConflict(
                "verification_result_resolution_failed",
                "the verification result could not be resolved against trusted evidence",
            ) from error
        if resolved is not True:
            raise CollaborationConflict(
                "verification_result_untrusted",
                "the verification result is not backed by the trusted verifier process",
            )

    async def _frozen_verification_required(
        self,
        channel: CollaborationChannel,
    ) -> bool:
        requirement = self._require_frozen_verification_evidence
        if isinstance(requirement, bool):
            return requirement
        try:
            required = requirement(channel)
            if inspect.isawaitable(required):
                required = await required
        except Exception as error:
            raise CollaborationConflict(
                "verification_evidence_requirement_failed",
                "the formal verification policy could not resolve the task package",
            ) from error
        if not isinstance(required, bool):
            raise CollaborationConflict(
                "verification_evidence_requirement_invalid",
                "the formal verification policy returned an invalid decision",
            )
        return required

    async def _require_developer_response_evidence(
        self,
        channel: CollaborationChannel,
        response: DeveloperResponse,
    ) -> None:
        if response.outcome.value != "implemented":
            return
        promotion_required = await self._frozen_verification_required(channel)
        if promotion_required and self._developer_promotion_resolver is None:
            raise CollaborationConflict(
                "developer_promotion_resolver_unavailable",
                "formal implemented responses require the trusted promotion resolver",
            )
        if (
            self._developer_promotion_resolver is None
            and self._developer_commit_resolver is None
        ) or self._developer_evidence_resolver is None:
            raise CollaborationConflict(
                "developer_evidence_resolver_unavailable",
                "implemented developer responses require trusted commit and evidence resolvers",
            )
        commit_sha = response.commit_sha
        if commit_sha is None:  # pragma: no cover - model invariant
            raise CollaborationConflict(
                "developer_commit_missing",
                "implemented developer response has no commit",
            )
        if self._developer_promotion_resolver is not None:
            try:
                promoted = self._developer_promotion_resolver(channel, response)
                if inspect.isawaitable(promoted):
                    promoted = await promoted
            except Exception as error:
                raise CollaborationConflict(
                    "developer_promotion_resolution_failed",
                    "the declared implementation promotion could not be resolved",
                ) from error
            if promoted is not True:
                raise CollaborationConflict(
                    "developer_commit_not_promoted",
                    "the declared commit is not the effective promotion for this response",
                )
        else:
            assert self._developer_commit_resolver is not None
            try:
                commit_exists = self._developer_commit_resolver(commit_sha)
                if inspect.isawaitable(commit_exists):
                    commit_exists = await commit_exists
            except Exception as error:
                raise CollaborationConflict(
                    "developer_commit_resolution_failed",
                    "the declared implementation commit could not be resolved",
                ) from error
            if commit_exists is not True:
                raise CollaborationConflict(
                    "developer_commit_not_found",
                    "the declared implementation commit does not exist in the trusted source",
                )

        evidence_refs = [test.evidence_ref for test in response.tests_run]
        evidence_refs.extend(response.browser_or_live_evidence)
        resolved: set[tuple[str, str]] = set()
        for evidence in evidence_refs:
            identity = (evidence.evidence_id, evidence.digest)
            if identity in resolved:
                continue
            resolved.add(identity)
            try:
                evidence_exists = self._developer_evidence_resolver(
                    commit_sha,
                    evidence,
                )
                if inspect.isawaitable(evidence_exists):
                    evidence_exists = await evidence_exists
            except Exception as error:
                raise CollaborationConflict(
                    "developer_evidence_resolution_failed",
                    "declared developer evidence could not be resolved",
                ) from error
            if evidence_exists is not True:
                raise CollaborationConflict(
                    "developer_evidence_not_found",
                    "declared developer evidence is absent or has another digest",
                )

    async def _require_developer_worker_receipt(
        self,
        *,
        channel: CollaborationChannel,
        report: CollaborationReport,
        lease: DeveloperLease,
        response_id: UUID,
        reference: DeveloperWorkerReceiptReference | None,
        require_success: bool,
    ) -> None:
        if not await self._frozen_verification_required(channel):
            return
        if reference is None:
            raise CollaborationConflict(
                "developer_worker_receipt_required",
                "formal developer work requires a trusted OS worker receipt",
            )
        resolver = self._developer_worker_receipt_resolver
        if resolver is None:
            raise CollaborationConflict(
                "developer_worker_receipt_resolver_unavailable",
                "the trusted developer worker receipt resolver is unavailable",
            )
        try:
            resolved = resolver(
                channel,
                report,
                lease,
                response_id,
                reference,
                require_success,
            )
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except Exception as error:
            raise CollaborationConflict(
                "developer_worker_receipt_resolution_failed",
                "the developer worker receipt could not be resolved",
            ) from error
        if resolved is not True:
            raise CollaborationConflict(
                "developer_worker_receipt_untrusted",
                "the developer worker receipt is forged, stale, or differently bound",
            )

    def require_enabled(self) -> None:
        if not self.enabled:
            # Feature-off and an unknown path intentionally share one projection.
            raise CollaborationNotFound()

    def subscribe_events(
        self, channel_id: UUID, *, max_queue: int = 64
    ) -> CollaborationEventSubscription:
        self.require_enabled()
        if not 1 <= max_queue <= 10_000:
            raise ValueError("subscriber max_queue must be between 1 and 10000")
        subscription = CollaborationEventSubscription(
            channel_id=channel_id,
            queue=asyncio.Queue(maxsize=max_queue),
            overflowed=asyncio.Event(),
        )
        self._event_subscribers.setdefault(channel_id, set()).add(subscription)
        return subscription

    def unsubscribe_events(self, subscription: CollaborationEventSubscription) -> None:
        subscription.closed = True
        subscribers = self._event_subscribers.get(subscription.channel_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._event_subscribers.pop(subscription.channel_id, None)

    async def wait_for_event(
        self,
        subscription: CollaborationEventSubscription,
        *,
        timeout: float,
    ) -> bool:
        if subscription.closed:
            return False
        if subscription.overflowed.is_set():
            raise CollaborationSubscriberOverflow()
        get_task = asyncio.create_task(subscription.queue.get())
        overflow_task = asyncio.create_task(subscription.overflowed.wait())
        try:
            done, _ = await asyncio.wait(
                {get_task, overflow_task},
                timeout=max(0.01, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if overflow_task in done and overflow_task.result():
                raise CollaborationSubscriberOverflow()
            return get_task in done
        finally:
            for task in (get_task, overflow_task):
                if not task.done():
                    task.cancel()

    def _notify_events(self, channel_id: UUID) -> None:
        for subscription in tuple(self._event_subscribers.get(channel_id, ())):
            if subscription.closed or subscription.overflowed.is_set():
                continue
            try:
                subscription.queue.put_nowait(1)
            except asyncio.QueueFull:
                # Notifications are expendable; collaboration messages are not.
                # The stream disconnects and resumes from the durable ack/cursor.
                subscription.overflowed.set()

    async def _message_replay(
        self,
        *,
        channel_id: UUID,
        actor: CollaborationPrincipal,
        idempotency_key: str,
        payload_schema: PayloadSchema,
        correlation_id: UUID,
        client_request_digest: str,
    ) -> CollaborationMessageEnvelope | None:
        lookup = getattr(self.store, "get_message_by_idempotency", None)
        if not callable(lookup):
            return None
        raw = await lookup(
            channel_id,
            actor.role.value,
            actor.sender_id,
            idempotency_key,
            client_request_digest=client_request_digest,
        )
        if raw is None:
            return None
        envelope = CollaborationMessageEnvelope.model_validate(raw)
        if (
            envelope.payload_schema is not payload_schema
            or envelope.correlation_id != correlation_id
        ):
            raise CollaborationConflict(
                "idempotency_payload_conflict",
                "idempotency key was reused for another collaboration operation",
            )
        return envelope

    async def _operation_replay(
        self,
        *,
        operation: str,
        scope_id: UUID,
        actor: CollaborationPrincipal,
        idempotency_key: str,
        client_request_digest: str,
    ) -> dict[str, Any] | None:
        lookup = getattr(self.store, "get_operation_receipt", None)
        if not callable(lookup):
            return None
        replay = await lookup(
            operation,
            scope_id,
            actor.role.value,
            actor.sender_id,
            idempotency_key,
            request_digest=client_request_digest,
        )
        return _as_dict(replay) if replay is not None else None

    @staticmethod
    def _client_request_digest(
        operation: str,
        request: Any,
        **bindings: UUID | str | int,
    ) -> str:
        request_payload = (
            request.model_dump(mode="json", exclude_none=False)
            if hasattr(request, "model_dump")
            else request
        )
        canonical = _canonical_value(
            {
                "operation": operation,
                "bindings": {key: str(value) for key, value in bindings.items()},
                "request": request_payload,
            }
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _require_replay_payload(
        actual: Mapping[str, Any],
        expected: Mapping[str, Any],
        *,
        ignored_fields: set[str] | None = None,
    ) -> None:
        ignored = ignored_fields or set()
        if _canonical_value(_without_fields(actual, ignored)) != _canonical_value(
            _without_fields(expected, ignored)
        ):
            raise CollaborationConflict(
                "idempotency_payload_conflict",
                "idempotency key was reused with another payload",
            )

    async def _developer_lease_replay(
        self,
        *,
        report_id: UUID,
        operation: str,
        actor: CollaborationPrincipal,
        idempotency_key: str,
        expected_revision: int,
        ttl_seconds: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        lookup = getattr(self.store, "get_developer_lease_receipt", None)
        if not callable(lookup):
            return None
        replay = await lookup(
            report_id,
            operation,
            actor.sender_id,
            idempotency_key,
            expected_revision=expected_revision,
            ttl_seconds=ttl_seconds,
            reason=reason,
        )
        return _as_dict(replay) if replay is not None else None

    async def _persisted_replay(
        self,
        getter_name: str,
        record_id: UUID,
        fallback: Mapping[str, Any],
    ) -> dict[str, Any]:
        getter = getattr(self.store, getter_name, None)
        if callable(getter):
            return _as_dict(await getter(record_id))
        return dict(fallback)

    async def _latest_report_chain_message(
        self,
        report: CollaborationReport,
    ) -> CollaborationMessageEnvelope | None:
        lookup = getattr(self.store, "get_latest_report_chain_message", None)
        if callable(lookup):
            raw = await lookup(report.report_id)
            if raw is not None:
                return CollaborationMessageEnvelope.model_validate(raw)
        return None

    async def _latest_report_chain_parent(
        self,
        report: CollaborationReport,
    ) -> UUID:
        latest = await self._latest_report_chain_message(report)
        return latest.message_id if latest is not None else report.source_message_id

    async def create_formal_channel(
        self,
        *,
        assignment_mode: AssignmentMode | str,
        task_id: str,
        task_revision: int,
        assignment_id: UUID,
        lilies_session_id: UUID,
        application_ids: Sequence[UUID],
        collaboration_enabled: bool,
        user_notified: bool,
        expires_at: datetime,
        retention_until: datetime | None,
        idempotency_key: str,
        max_report_evidence_rounds: int,
        prepared_access_token: SecretStr | None = None,
    ) -> IssuedCollaborationChannel:
        self.require_enabled()
        mode = (
            assignment_mode
            if isinstance(assignment_mode, AssignmentMode)
            else AssignmentMode(assignment_mode)
        )
        if mode is not AssignmentMode.formal_experiment:
            raise CollaborationConflict(
                "collaboration_not_formal",
                "temporary collaboration is restricted to formal experiments",
            )
        if not collaboration_enabled or not user_notified:
            raise CollaborationConflict(
                "collaboration_activation_incomplete",
                "formal collaboration requires task enablement and prior user notice",
            )
        normalized_application_ids = list(dict.fromkeys(application_ids))
        if not normalized_application_ids:
            raise CollaborationConflict(
                "collaboration_application_binding_missing",
                "formal collaboration requires at least one assigned application",
            )
        now = self._now()
        if expires_at.tzinfo is None or expires_at.utcoffset() != timedelta(0):
            raise ValueError("expires_at must use UTC")
        if expires_at <= now:
            raise CollaborationConflict(
                "collaboration_expired",
                "collaboration credential expiry must be in the future",
            )
        channel_id = uuid5(
            NAMESPACE_URL,
            f"lilies:collaboration:{task_id}:{task_revision}:{assignment_id}",
        )
        credential_id = uuid5(
            NAMESPACE_URL,
            f"lilies:collaboration-credential:{channel_id}:{idempotency_key}",
        )
        credential_ref = f"collaboration_{credential_id.hex}"
        bearer = (
            prepared_access_token.get_secret_value()
            if prepared_access_token is not None
            else f"lcc_{channel_id.hex}_{secrets.token_urlsafe(48)}"
        )
        if len(bearer) < 32:
            raise ValueError("prepared collaboration token is too short")
        if any(hmac.compare_digest(bearer, reserved) for reserved in self._reserved_role_tokens):
            raise CollaborationConflict(
                "collaboration_credential_role_collision",
                "collaboration credential must not reuse another role credential",
            )
        register_secret = getattr(self.store, "register_secret_value", None)
        if callable(register_secret):
            register_secret(bearer)
        channel = CollaborationChannel(
            channel_id=channel_id,
            task_id=task_id,
            task_revision=task_revision,
            assignment_id=assignment_id,
            lilies_session_id=lilies_session_id,
            application_ids=normalized_application_ids,
            approval_mode=ApprovalMode.manual,
            max_report_evidence_rounds=max_report_evidence_rounds,
            status=ChannelStatus.active,
            revision=1,
            next_seq=1,
            created_at=now,
            retention_until=retention_until,
        )
        control = ControlMessage(
            control_id=uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-activation-control:{channel_id}:{idempotency_key}",
            ),
            channel_id=channel_id,
            kind=ControlKind.channel_activated,
            actor_id="platform",
            reason="formal task collaboration activated after user notice",
            new_value=ChannelStatus.active.value,
            created_at=now,
        )
        activation_message = self._message(
            channel_id=channel_id,
            message_type=MessageType.control,
            sender_role=SenderRole.platform,
            sender_id="platform",
            correlation_id=channel_id,
            causal_parent_id=None,
            idempotency_key=f"{idempotency_key}.activated",
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.control_v1,
            payload=control,
            message_id=uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-activation-message:{channel_id}:{idempotency_key}",
            ),
        )
        activated = await self.store.activate_channel(
            {
                **channel.model_dump(mode="json", exclude_none=True),
                "idempotency_key": idempotency_key,
            },
            {
                "credential_id": str(credential_id),
                "credential_ref": credential_ref,
                "role": SenderRole.lilies.value,
                "actor_id": str(lilies_session_id),
                "lilies_session_id": str(lilies_session_id),
                "channel_id": str(channel_id),
                "assignment_id": str(assignment_id),
                "scopes": [scope.value for scope in CollaborationScope],
                "expires_at": expires_at.isoformat(),
                "created_at": now.isoformat(),
                "idempotency_key": idempotency_key,
            },
            bearer,
            activation_message,
        )
        activated_record = _as_dict(activated)
        stored = CollaborationChannel.model_validate(activated_record["channel"])
        return IssuedCollaborationChannel(
            channel=stored,
            credential_ref=credential_ref,
            access_token=SecretStr(bearer),
        )

    async def close_formal_assignment_channel(
        self,
        *,
        assignment_mode: AssignmentMode | str,
        task_id: str,
        task_revision: int,
        assignment_id: UUID,
        lilies_session_id: UUID,
        application_ids: Sequence[UUID],
    ) -> dict[str, Any]:
        """Revoke one exact formal channel, including pre-activation races."""

        self.require_enabled()
        mode = (
            assignment_mode
            if isinstance(assignment_mode, AssignmentMode)
            else AssignmentMode(assignment_mode)
        )
        if mode is not AssignmentMode.formal_experiment:
            raise CollaborationConflict(
                "collaboration_not_formal",
                "only a formal assignment can close formal collaboration authority",
            )
        normalized_application_ids = list(dict.fromkeys(application_ids))
        if not normalized_application_ids:
            raise CollaborationConflict(
                "collaboration_application_binding_missing",
                "formal channel close requires its exact application binding",
            )
        channel_id = uuid5(
            NAMESPACE_URL,
            f"lilies:collaboration:{task_id}:{task_revision}:{assignment_id}",
        )
        raw = await self.store.close_formal_channel_boundary(
            channel_id=channel_id,
            task_id=task_id,
            task_revision=task_revision,
            assignment_id=assignment_id,
            lilies_session_id=lilies_session_id,
            application_ids=normalized_application_ids,
            idempotency_key=f"formal.channel.close.{assignment_id.hex}",
        )
        channel = CollaborationChannel.model_validate(raw)
        if (
            channel.channel_id != channel_id
            or channel.task_id != task_id
            or channel.task_revision != task_revision
            or channel.assignment_id != assignment_id
            or channel.lilies_session_id != lilies_session_id
            or channel.application_ids != normalized_application_ids
            or channel.status is not ChannelStatus.closed
            or channel.closed_at is None
        ):
            raise CollaborationConflict(
                "collaboration_formal_close_mismatch",
                "formal channel close returned another frozen identity",
            )
        self._notify_events(channel_id)
        return channel.model_dump(mode="json", exclude_none=True)

    async def authenticate_lilies(
        self,
        access_token: str,
        *,
        channel_id: UUID,
        required_scope: str,
    ) -> CollaborationPrincipal:
        self.require_enabled()
        try:
            raw = await self.store.authenticate_credential(
                access_token,
                required_scope=required_scope,
                channel_id=channel_id,
            )
        except Exception as error:
            # Missing, expired, revoked and cross-channel credentials are
            # intentionally indistinguishable to an untrusted caller.
            raise CollaborationNotFound() from error
        record = _as_dict(raw)
        if record.get("role") != SenderRole.lilies.value:
            raise CollaborationNotFound()
        return CollaborationPrincipal(
            role=SenderRole.lilies,
            sender_id=str(record.get("actor_id") or record["lilies_session_id"]),
            scopes=frozenset(str(scope) for scope in record.get("scopes", [])),
            channel_id=UUID(str(record["channel_id"])),
            assignment_id=UUID(str(record["assignment_id"])),
        )

    def authenticate_developer(
        self,
        access_token: str,
        *,
        required_scope: str,
    ) -> CollaborationPrincipal:
        self.require_enabled()
        if (
            required_scope != "collaboration.developer"
            or not self._developer_token
            or not hmac.compare_digest(access_token, self._developer_token)
        ):
            raise CollaborationNotFound()
        return CollaborationPrincipal(
            role=SenderRole.codex,
            sender_id="codex-developer",
            scopes=frozenset({"collaboration.developer"}),
        )

    async def authenticate_verifier(
        self,
        access_token: str,
        *,
        claim_id: UUID,
        required_scope: str,
    ) -> CollaborationPrincipal:
        self.require_enabled()
        if (
            required_scope != "collaboration.verify"
            or not self._verifier_token
            or not hmac.compare_digest(access_token, self._verifier_token)
        ):
            raise CollaborationNotFound()
        try:
            claim = VerificationClaim.model_validate(await self.store.get_claim(claim_id))
        except Exception as error:
            raise CollaborationNotFound() from error
        return CollaborationPrincipal(
            role=SenderRole.verifier,
            sender_id="independent-verifier",
            scopes=frozenset({"collaboration.verify"}),
            channel_id=claim.channel_id,
            assignment_id=claim.assignment_id,
        )

    @staticmethod
    def _principal(value: Any) -> CollaborationPrincipal:
        if isinstance(value, CollaborationPrincipal):
            return value
        if isinstance(value, Mapping):
            role = SenderRole(str(value.get("role")))
            sender_id = str(value.get("sender_id") or value.get("actor_id"))
            scopes = frozenset(str(item) for item in value.get("scopes", []))
            channel = value.get("channel_id")
            assignment = value.get("assignment_id")
            return CollaborationPrincipal(
                role=role,
                sender_id=sender_id,
                scopes=scopes,
                channel_id=UUID(str(channel)) if channel else None,
                assignment_id=UUID(str(assignment)) if assignment else None,
            )
        raise CollaborationNotFound()

    async def _channel(
        self,
        channel_id: UUID,
        *,
        principal: CollaborationPrincipal | None = None,
    ) -> CollaborationChannel:
        try:
            channel = CollaborationChannel.model_validate(await self.store.get_channel(channel_id))
        except (KeyError, ValueError, ValidationError) as error:
            raise CollaborationNotFound() from error
        if principal is not None and principal.channel_id not in {None, channel_id}:
            raise CollaborationNotFound()
        return channel

    @staticmethod
    def _require_open(channel: CollaborationChannel) -> None:
        if channel.status not in _OPEN_CHANNEL_STATUSES:
            raise CollaborationClosed()

    @staticmethod
    def _require_developer_visible(report: CollaborationReport) -> None:
        if report.status not in _DEVELOPER_VISIBLE_REPORT_STATUSES:
            # Deliberately hide the existence, category, revision, and state of
            # every report that has not crossed the approval/direct-route gate.
            raise CollaborationNotFound()

    def _message(
        self,
        *,
        channel_id: UUID,
        message_type: MessageType,
        sender_role: SenderRole,
        sender_id: str,
        correlation_id: UUID,
        causal_parent_id: UUID | None,
        idempotency_key: str,
        visibility: MessageVisibility,
        payload_schema: PayloadSchema,
        payload: Any,
        message_id: UUID | None = None,
        client_request_digest: str | None = None,
    ) -> dict[str, Any]:
        raw_payload = (
            payload.model_dump(mode="json", exclude_none=True)
            if hasattr(payload, "model_dump")
            else _as_dict(payload)
        )
        raw_payload = sanitize_collaboration_payload(raw_payload)
        validate_collaboration_payload_safety(raw_payload)
        evidence_refs = raw_payload.get("evidence_refs", [])
        message = {
            "schema_version": "1.0",
            "message_id": str(message_id or uuid4()),
            "channel_id": str(channel_id),
            # seq is assigned by CollaborationStore in the same transaction.
            "message_type": message_type.value,
            "sender_role": sender_role.value,
            "sender_id": sender_id,
            "correlation_id": str(correlation_id),
            "causal_parent_id": (str(causal_parent_id) if causal_parent_id is not None else None),
            "idempotency_key": idempotency_key,
            "visibility": visibility.value,
            "payload_schema": payload_schema.value,
            "payload": raw_payload,
            "evidence_refs": evidence_refs,
            # created_at is overwritten by storage's transaction clock.
            "created_at": self._now().isoformat(),
        }
        if client_request_digest is not None:
            message["client_request_digest"] = client_request_digest
        return message

    def _audit_record(
        self,
        *,
        channel_id: UUID,
        entity_kind: str,
        entity_id: UUID,
        event_type: str,
        actor_role: SenderRole,
        actor_id: str,
        idempotency_key: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        safe_details = sanitize_collaboration_payload(details)
        validate_collaboration_payload_safety(safe_details)
        return {
            "audit_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"lilies:collaboration-audit:{entity_kind}:{entity_id}:"
                    f"{actor_role.value}:{actor_id}:{idempotency_key}",
                )
            ),
            "channel_id": str(channel_id),
            "entity_kind": entity_kind,
            "entity_id": str(entity_id),
            "event_type": event_type,
            "actor_role": actor_role.value,
            "actor_id": actor_id,
            "idempotency_key": idempotency_key,
            "details": dict(safe_details),
            "created_at": self._now().isoformat(),
        }

    def _outbox_record(
        self,
        *,
        channel_id: UUID,
        message_id: UUID | None,
        destination: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        safe_payload = sanitize_collaboration_payload(payload)
        validate_collaboration_payload_safety(safe_payload)
        return {
            "outbox_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"lilies:collaboration-outbox:{channel_id}:{destination}:{idempotency_key}",
                )
            ),
            "channel_id": str(channel_id),
            "message_id": str(message_id) if message_id is not None else None,
            "destination": destination,
            "idempotency_key": idempotency_key,
            "payload": dict(safe_payload),
            "status": "pending",
            "available_at": self._now().isoformat(),
            "created_at": self._now().isoformat(),
        }

    @staticmethod
    def _initial_report_state(
        *,
        category: ReportCategory,
        complete: bool,
        approval_mode: ApprovalMode,
    ) -> tuple[ReportRoute, ReportStatus]:
        if category is ReportCategory.task_spec_gap:
            return ReportRoute.task_author, ReportStatus.routed_to_task_author
        if category is ReportCategory.environment_gap:
            return ReportRoute.environment, ReportStatus.environment_failed
        if not complete:
            return ReportRoute.capability_approval, ReportStatus.needs_more_evidence
        if approval_mode is ApprovalMode.auto_forward:
            return ReportRoute.developer, ReportStatus.approved_for_codex
        return ReportRoute.capability_approval, ReportStatus.awaiting_user_review

    @_replay_receipt_after_conflict(
        receipt_operation="report.create",
        digest_operation="report.submit",
        scope_parameter="channel_id",
        binding_parameters=("channel_id",),
    )
    async def submit_report(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: ReportSubmitRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies or (
            CollaborationScope.report_write.value not in actor.scopes
        ):
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "report.submit", request, channel_id=channel_id
        )
        operation_replay = await self._operation_replay(
            operation="report.create",
            scope_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.report_v1,
            correlation_id=request.report.report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_report(request.report.report_id))
        channel = await self._channel(channel_id, principal=actor)
        self._require_open(channel)
        if request.expected_channel_revision != channel.revision:
            raise CollaborationConflict(
                "channel_revision_conflict",
                "channel revision changed before report submission",
            )
        payload = request.report
        route, report_status = self._initial_report_state(
            category=payload.category,
            complete=payload.is_complete_for_routing(),
            approval_mode=channel.approval_mode,
        )
        auto_forward = (
            payload.category in _CAPABILITY_CATEGORIES
            and payload.is_complete_for_routing()
            and channel.approval_mode is ApprovalMode.auto_forward
        )
        capability_intake = payload.category in _CAPABILITY_CATEGORIES
        initial_route = ReportRoute.capability_approval if capability_intake else route
        initial_status = ReportStatus.observed if capability_intake else report_status
        now = self._now()
        message_id = uuid4()
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.report,
            sender_role=SenderRole.lilies,
            sender_id=actor.sender_id,
            correlation_id=payload.report_id,
            causal_parent_id=None,
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.report_v1,
            payload=payload,
            message_id=message_id,
            client_request_digest=client_request_digest,
        )
        report = CollaborationReport(
            **payload.model_dump(mode="python"),
            channel_id=channel_id,
            source_message_id=message_id,
            route=initial_route,
            status=initial_status,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        storage_record: dict[str, Any] = {
            "report_id": str(report.report_id),
            "channel_id": str(report.channel_id),
            "category": report.category.value,
            "phase": report.phase.value,
            "severity": report.severity.value,
            "route": report.route.value,
            "status": report.status.value,
            "visibility": MessageVisibility.user_and_lilies.value,
            "payload": payload.model_dump(mode="json", exclude_none=True),
            "expected_channel_revision": request.expected_channel_revision,
        }
        if payload.category in {
            ReportCategory.task_spec_gap,
            ReportCategory.environment_gap,
        }:
            storage_record["initial_outbox"] = self._outbox_record(
                channel_id=channel_id,
                message_id=message_id,
                destination="developer_inbox",
                idempotency_key=request.idempotency_key,
                payload={"report_id": str(report.report_id)},
            )
        validated_revision = 1
        if capability_intake:
            intake_key_digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            validation_status = (
                ReportStatus.awaiting_user_review
                if payload.is_complete_for_routing()
                else ReportStatus.needs_more_evidence
            )
            storage_record["intake_transitions"] = [
                {
                    "status": ReportStatus.evidence_collecting.value,
                    "route": ReportRoute.capability_approval.value,
                    "visibility": MessageVisibility.user_and_lilies.value,
                    "actor_role": SenderRole.lilies.value,
                    "actor_id": actor.sender_id,
                    "idempotency_key": f"intake-lilies:{intake_key_digest}",
                },
                {
                    "status": validation_status.value,
                    "route": ReportRoute.capability_approval.value,
                    "visibility": MessageVisibility.user_and_lilies.value,
                    "actor_role": SenderRole.platform.value,
                    "actor_id": "platform-schema-validator",
                    "idempotency_key": f"intake-platform:{intake_key_digest}",
                },
            ]
            validated_revision = 3
        if auto_forward:
            auto_key = (
                "auto-forward:" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            )
            approval = ApprovalDecision(
                approval_id=uuid5(
                    NAMESPACE_URL,
                    f"lilies:auto-forward-approval:{report.report_id}:{auto_key}",
                ),
                channel_id=channel_id,
                report_id=report.report_id,
                expected_report_revision=validated_revision,
                resulting_report_revision=validated_revision + 1,
                decision=ReportDecision.approve,
                actor_id="platform-auto-forward",
                reason="user-confirmed task-local auto-forward",
                idempotency_key=auto_key,
                created_at=now,
            )
            approval_message = self._message(
                channel_id=channel_id,
                message_type=MessageType.approval,
                sender_role=SenderRole.platform,
                sender_id="platform-auto-forward",
                correlation_id=report.report_id,
                causal_parent_id=message_id,
                idempotency_key=auto_key,
                visibility=MessageVisibility.user_and_lilies,
                payload_schema=PayloadSchema.approval_v1,
                payload=approval,
            )
            storage_record["auto_forward"] = {
                "approval": approval.model_dump(mode="json", exclude_none=True),
                "message": approval_message,
                "audit": self._audit_record(
                    channel_id=channel_id,
                    entity_kind="report",
                    entity_id=report.report_id,
                    event_type="collaboration.report_auto_forwarded",
                    actor_role=SenderRole.platform,
                    actor_id="platform-auto-forward",
                    idempotency_key=auto_key,
                    details={"task_revision": channel.task_revision},
                ),
                "outbox": self._outbox_record(
                    channel_id=channel_id,
                    message_id=UUID(str(approval_message["message_id"])),
                    destination="developer_inbox",
                    idempotency_key=auto_key,
                    payload={"report_id": str(report.report_id)},
                ),
                "next_report_status": ReportStatus.approved_for_codex.value,
                "next_report_route": ReportRoute.developer.value,
                "next_visibility": MessageVisibility.approved_developer.value,
            }
        stored = await self.store.create_report(storage_record, message)
        self._notify_events(channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="report.revise",
        digest_operation="report.revise",
        scope_parameter="report_id",
        binding_parameters=("channel_id", "report_id"),
    )
    async def revise_report(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        report_id: UUID,
        request: ReportRevisionRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies or (
            CollaborationScope.report_write.value not in actor.scopes
        ):
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "report.revise",
            request,
            channel_id=channel_id,
            report_id=report_id,
        )
        operation_replay = await self._operation_replay(
            operation="report.revise",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            if operation_replay.get("budget_exhausted") is True:
                await self._raise_report_evidence_budget_exhausted(
                    operation_replay,
                    report_id=report_id,
                )
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.report_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_report(report_id))
        channel = await self._channel(channel_id, principal=actor)
        self._require_open(channel)
        current = CollaborationReport.model_validate(await self.store.get_report(report_id))
        if current.channel_id != channel_id or request.report.report_id != report_id:
            raise CollaborationNotFound()
        if current.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict",
                "report revision changed before evidence supplementation",
            )
        if current.status not in {
            ReportStatus.evidence_collecting,
            ReportStatus.needs_more_evidence,
            ReportStatus.routed_to_task_author,
            ReportStatus.environment_failed,
            ReportStatus.verification_failed,
        }:
            raise CollaborationConflict(
                "report_not_revisable",
                "report evidence cannot be rewritten after handling has begun",
            )
        if current.category != request.report.category:
            raise CollaborationConflict(
                "report_category_immutable",
                "a report category cannot change during evidence supplementation",
            )
        if (
            current.original_goal != request.report.original_goal
            or current.requirement_digest != request.report.requirement_digest
        ):
            raise CollaborationConflict(
                "report_task_context_immutable",
                "original goal and requirement digest cannot change during evidence supplementation",
            )
        capability_revision = current.category in _CAPABILITY_CATEGORIES
        if capability_revision:
            route = ReportRoute.capability_approval
            next_status = ReportStatus.evidence_collecting
            validation_status = (
                ReportStatus.awaiting_user_review
                if request.report.is_complete_for_routing()
                else ReportStatus.needs_more_evidence
            )
        else:
            route, next_status = self._initial_report_state(
                category=request.report.category,
                complete=request.report.is_complete_for_routing(),
                approval_mode=channel.approval_mode,
            )
            validation_status = None
        auto_forward = (
            capability_revision
            and validation_status is ReportStatus.awaiting_user_review
            and channel.approval_mode is ApprovalMode.auto_forward
        )
        next_revision = current.revision + 1
        message_id = uuid4()
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.report,
            sender_role=SenderRole.lilies,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(current),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.report_v1,
            payload=request.report,
            message_id=message_id,
            client_request_digest=client_request_digest,
        )
        replacement = CollaborationReport(
            **request.report.model_dump(mode="python"),
            channel_id=channel_id,
            source_message_id=message_id,
            route=route,
            status=next_status,
            revision=next_revision,
            created_at=current.created_at,
            updated_at=self._now(),
        )
        validation_transition: dict[str, Any] | None = None
        validated_revision = next_revision
        if validation_status is not None:
            revision_key_digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            validation_transition = {
                "status": validation_status.value,
                "route": ReportRoute.capability_approval.value,
                "visibility": MessageVisibility.user_and_lilies.value,
                "actor_role": SenderRole.platform.value,
                "actor_id": "platform-schema-validator",
                "idempotency_key": f"validate-platform:{revision_key_digest}",
            }
            validated_revision += 1
        auto_forward_record: dict[str, Any] | None = None
        if auto_forward:
            auto_key = (
                "auto-forward:" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            )
            now = self._now()
            approval = ApprovalDecision(
                approval_id=uuid5(
                    NAMESPACE_URL,
                    f"lilies:auto-forward-approval:{report_id}:{auto_key}",
                ),
                channel_id=channel_id,
                report_id=report_id,
                expected_report_revision=validated_revision,
                resulting_report_revision=validated_revision + 1,
                decision=ReportDecision.approve,
                actor_id="platform-auto-forward",
                reason="user-confirmed task-local auto-forward",
                idempotency_key=auto_key,
                created_at=now,
            )
            approval_message = self._message(
                channel_id=channel_id,
                message_type=MessageType.approval,
                sender_role=SenderRole.platform,
                sender_id="platform-auto-forward",
                correlation_id=report_id,
                causal_parent_id=message_id,
                idempotency_key=auto_key,
                visibility=MessageVisibility.user_and_lilies,
                payload_schema=PayloadSchema.approval_v1,
                payload=approval,
            )
            auto_forward_record = {
                "approval": approval.model_dump(mode="json", exclude_none=True),
                "message": approval_message,
                "audit": self._audit_record(
                    channel_id=channel_id,
                    entity_kind="report",
                    entity_id=report_id,
                    event_type="collaboration.report_auto_forwarded",
                    actor_role=SenderRole.platform,
                    actor_id="platform-auto-forward",
                    idempotency_key=auto_key,
                    details={"task_revision": channel.task_revision},
                ),
                "outbox": self._outbox_record(
                    channel_id=channel_id,
                    message_id=UUID(str(approval_message["message_id"])),
                    destination="developer_inbox",
                    idempotency_key=auto_key,
                    payload={"report_id": str(report_id)},
                ),
                "next_report_status": ReportStatus.approved_for_codex.value,
                "next_report_route": ReportRoute.developer.value,
                "next_visibility": MessageVisibility.approved_developer.value,
            }
        stored = await self.store.revise_report(
            report_id,
            expected_revision=current.revision,
            idempotency_key=request.idempotency_key,
            actor_role=actor.role.value,
            actor_id=actor.sender_id,
            changes={
                "payload": request.report.model_dump(mode="json", exclude_none=True),
                "status": replacement.status.value,
                "route": replacement.route.value,
                "visibility": MessageVisibility.user_and_lilies.value,
                "phase": replacement.phase.value,
                "severity": replacement.severity.value,
            },
            message=message,
            auto_forward=auto_forward_record,
            validation_transition=validation_transition,
            expected_channel_revision=(channel.revision if auto_forward else None),
            expected_approval_mode=(ApprovalMode.auto_forward.value if auto_forward else None),
            consume_evidence_budget=True,
        )
        if stored.get("budget_exhausted") is True:
            await self._raise_report_evidence_budget_exhausted(
                stored,
                report_id=report_id,
            )
        self._notify_events(channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="report.revise",
        digest_operation="report.withdraw",
        scope_parameter="report_id",
        binding_parameters=("channel_id", "report_id"),
    )
    async def withdraw_report(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        report_id: UUID,
        request: ReportWithdrawalRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies or (
            CollaborationScope.report_write.value not in actor.scopes
        ):
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "report.withdraw",
            request,
            channel_id=channel_id,
            report_id=report_id,
        )
        operation_replay = await self._operation_replay(
            operation="report.revise",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.control_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_report(report_id))
        channel = await self._channel(channel_id, principal=actor)
        self._require_open(channel)
        current = CollaborationReport.model_validate(await self.store.get_report(report_id))
        if current.channel_id != channel_id:
            raise CollaborationNotFound()
        if current.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict",
                "report revision changed before withdrawal",
            )
        if (
            current.category not in _CAPABILITY_CATEGORIES
            or current.status is not ReportStatus.awaiting_user_review
        ):
            raise CollaborationConflict(
                "report_not_withdrawable",
                "only a capability report awaiting user review may be withdrawn",
            )
        control = ControlMessage(
            control_id=uuid4(),
            channel_id=channel_id,
            kind=ControlKind.report_status_changed,
            actor_id=actor.sender_id,
            reason=request.reason,
            report_id=report_id,
            previous_value=current.status.value,
            new_value=ReportStatus.withdrawn.value,
            created_at=self._now(),
        )
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.control,
            sender_role=SenderRole.lilies,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(current),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.control_v1,
            payload=control,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.revise_report(
            report_id,
            expected_revision=current.revision,
            idempotency_key=request.idempotency_key,
            actor_role=actor.role.value,
            actor_id=actor.sender_id,
            changes={
                "status": ReportStatus.withdrawn.value,
                "route": ReportRoute.capability_approval.value,
                "visibility": MessageVisibility.user_and_lilies.value,
            },
            message=message,
            audit=self._audit_record(
                channel_id=channel_id,
                entity_kind="report",
                entity_id=report_id,
                event_type="collaboration.report_withdrawn",
                actor_role=SenderRole.lilies,
                actor_id=actor.sender_id,
                idempotency_key=request.idempotency_key,
                details={
                    "previous_status": current.status.value,
                    "reason": request.reason,
                },
            ),
        )
        self._notify_events(channel_id)
        return _as_dict(stored)

    async def list_events(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        after: int,
        limit: int,
        history_replay: bool = False,
    ) -> list[dict[str, Any]]:
        actor = self._principal(principal)
        await self._channel(channel_id, principal=actor)
        if actor.role is SenderRole.user:
            allowed_visibilities: list[str] | None = None
        elif actor.role is SenderRole.lilies:
            allowed_visibilities = [MessageVisibility.user_and_lilies.value]
        elif actor.role is SenderRole.codex:
            allowed_visibilities = [MessageVisibility.approved_developer.value]
        else:
            allowed_visibilities = [MessageVisibility.verifier.value]
        records = _as_list(
            await self.store.list_messages(
                channel_id,
                after_seq=after,
                limit=limit,
                visibilities=allowed_visibilities,
                lilies_claim_sender_id=(
                    actor.sender_id if actor.role is SenderRole.lilies and history_replay else None
                ),
            )
        )
        visible: list[dict[str, Any]] = []
        for raw in records:
            projected = _as_dict(raw)
            visibility = MessageVisibility(str(projected["visibility"]))
            lilies_own_claim = (
                actor.role is SenderRole.lilies
                and history_replay
                and visibility is MessageVisibility.verifier
                and projected.get("sender_role") == SenderRole.lilies.value
                and projected.get("sender_id") == actor.sender_id
                and projected.get("message_type") == MessageType.verification_claim.value
            )
            allowed = (
                allowed_visibilities is None
                or visibility.value in allowed_visibilities
                or lilies_own_claim
            )
            if allowed:
                # Validate the typed envelope at the access-control boundary.
                envelope = CollaborationMessageEnvelope.model_validate(projected)
                visible.append(envelope.model_dump(mode="json", exclude_none=True))
        return visible

    async def get_lilies_channel_state(
        self,
        *,
        principal: Any,
        channel_id: UUID,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies:
            raise CollaborationNotFound()
        channel = await self._channel(channel_id, principal=actor)
        lookup_cursor = getattr(self.store, "get_reader_cursor", None)
        if callable(lookup_cursor):
            raw_cursor = await lookup_cursor(
                channel_id,
                actor.sender_id,
                reader_role=actor.role.value,
            )
            cursor = ReaderCursor.model_validate(raw_cursor)
        else:
            cursor = ReaderCursor(
                channel_id=channel_id,
                reader_role=actor.role,
                reader_id=actor.sender_id,
                ack_seq=0,
                revision=0,
            )
        latest_claim: dict[str, Any] | None = None
        lookup_claim = getattr(self.store, "get_latest_claim", None)
        if callable(lookup_claim):
            raw_claim = await lookup_claim(
                channel_id=channel_id,
                assignment_id=channel.assignment_id,
            )
            latest_claim = _as_dict(raw_claim) if raw_claim is not None else None
        else:  # pragma: no cover - compatibility for narrow external stores
            list_claims = getattr(self.store, "list_claims", None)
            if callable(list_claims):
                raw_claims = _as_list(
                    await list_claims(
                        channel_id=channel_id,
                        assignment_id=channel.assignment_id,
                        after=0,
                        limit=5_000,
                    )
                )
                if raw_claims:
                    latest_claim = _as_dict(raw_claims[-1])

        state = {
            **channel.model_dump(mode="json", exclude_none=True),
            "reader_cursor": cursor.model_dump(mode="json", exclude_none=True),
        }
        if latest_claim is not None:

            def collection_digest(value: Any) -> str:
                canonical = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

            artifact_refs = latest_claim.get("artifact_refs", [])
            host_receipt_refs = latest_claim.get("host_receipt_refs", [])
            remaining_limits = latest_claim.get("remaining_limits", [])
            state["latest_claim_resume"] = {
                key: latest_claim[key]
                for key in (
                    "schema_version",
                    "claim_id",
                    "assignment_id",
                    "application_id",
                    "claim_revision",
                    "draft_revision",
                    "content_hash",
                    "published_version",
                    "status",
                    "claim",
                    "test_run_ids",
                    "business_run_ids",
                    "resolved_report_ids",
                    "created_at",
                    "invalidated_at",
                    "invalidation_reason",
                )
                if latest_claim.get(key) is not None
            }
            state["latest_claim_resume"].update(
                {
                    "artifact_refs": {
                        "count": len(artifact_refs),
                        "digest": collection_digest(artifact_refs),
                    },
                    "host_receipt_refs": {
                        "count": len(host_receipt_refs),
                        "digest": collection_digest(host_receipt_refs),
                    },
                    "remaining_limits": {
                        "count": len(remaining_limits),
                        "digest": collection_digest(remaining_limits),
                    },
                    "claim_digest": collection_digest(latest_claim),
                }
            )
        return state

    async def resolve_event_cursor(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        requested_after: int,
        durable: bool,
    ) -> int:
        actor = self._principal(principal)
        await self._channel(channel_id, principal=actor)
        if not durable:
            return requested_after
        try:
            raw = await self.store.get_reader_cursor(
                channel_id,
                actor.sender_id,
                reader_role=actor.role.value,
            )
        except Exception:
            return 0
        cursor = ReaderCursor.model_validate(raw)
        if cursor.reader_role is not actor.role:
            raise CollaborationNotFound()
        # Persistent acknowledgement is authoritative; Last-Event-ID cannot
        # skip a message that was rendered but never durably processed.
        return cursor.ack_seq

    async def ack_events(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: ReaderAckRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        await self._channel(channel_id, principal=actor)
        if request.reader_role is not actor.role or request.reader_id != actor.sender_id:
            raise CollaborationNotFound()
        stored = await self.store.ack_reader(
            channel_id,
            actor.sender_id,
            request.ack_seq,
            reader_role=actor.role.value,
            expected_revision=request.expected_cursor_revision,
            idempotency_key=request.idempotency_key,
        )
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="report.approval",
        digest_operation="report.decision",
        scope_parameter="report_id",
        binding_parameters=("report_id",),
    )
    async def decide_report(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: ApprovalDecisionRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        current = CollaborationReport.model_validate(await self.store.get_report(report_id))
        client_request_digest = self._client_request_digest(
            "report.decision", request, report_id=report_id
        )
        operation_replay = await self._operation_replay(
            operation="report.approval",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=current.channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.approval_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = ApprovalDecision.model_validate(replay.payload)
            return await self._persisted_replay(
                "get_approval", persisted.approval_id, replay.payload
            )
        channel = await self._channel(current.channel_id)
        self._require_open(channel)
        if current.category not in _CAPABILITY_CATEGORIES:
            raise CollaborationConflict(
                "report_not_approvable",
                "task and environment reports use their direct routes",
            )
        if current.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict",
                "report revision changed before approval",
            )
        if current.status is not ReportStatus.awaiting_user_review:
            raise CollaborationConflict(
                "report_not_awaiting_review",
                "report is not awaiting a user decision",
            )
        if request.decision is ReportDecision.approve:
            if current.completeness_issues():
                raise CollaborationConflict(
                    "report_evidence_incomplete",
                    "an incomplete platform report cannot be approved",
                )
            next_status = ReportStatus.approved_for_codex
            next_route = ReportRoute.developer
        elif request.decision is ReportDecision.reject:
            next_status = ReportStatus.rejected
            next_route = ReportRoute.capability_approval
        else:
            next_status = ReportStatus.needs_more_evidence
            next_route = ReportRoute.capability_approval
        now = self._now()
        approval = ApprovalDecision(
            approval_id=uuid4(),
            channel_id=current.channel_id,
            report_id=report_id,
            expected_report_revision=current.revision,
            resulting_report_revision=current.revision + 1,
            decision=request.decision,
            actor_id=actor.sender_id,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
            created_at=now,
        )
        message = self._message(
            channel_id=current.channel_id,
            message_type=MessageType.approval,
            sender_role=SenderRole.user,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(current),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.approval_v1,
            payload=approval,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_approval(
            {
                **approval.model_dump(mode="json", exclude_none=True),
                "next_report_route": next_route.value,
                "next_visibility": (
                    MessageVisibility.approved_developer.value
                    if next_status is ReportStatus.approved_for_codex
                    else MessageVisibility.user_and_lilies.value
                ),
            },
            next_report_status=next_status.value,
            message=message,
            outbox=(
                self._outbox_record(
                    channel_id=current.channel_id,
                    message_id=UUID(str(message["message_id"])),
                    destination="developer_inbox",
                    idempotency_key=request.idempotency_key,
                    payload={"report_id": str(report_id)},
                )
                if next_status is ReportStatus.approved_for_codex
                else None
            ),
        )
        self._notify_events(current.channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="channel.settings",
        digest_operation="channel.settings",
        scope_parameter="channel_id",
        binding_parameters=("channel_id",),
    )
    async def set_channel_approval_mode(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: ChannelSettingsRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "channel.settings", request, channel_id=channel_id
        )
        operation_replay = await self._operation_replay(
            operation="channel.settings",
            scope_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.control_v1,
            correlation_id=channel_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_channel(channel_id))
        channel = await self._channel(channel_id)
        self._require_open(channel)
        if channel.revision != request.expected_channel_revision:
            raise CollaborationConflict(
                "channel_revision_conflict",
                "channel settings changed before this update",
            )
        if channel.approval_mode is request.approval_mode:
            # Storage idempotency still owns replay semantics; this message
            # cannot be used as an implicit confirmation for a distinct key.
            next_revision = channel.revision
        else:
            next_revision = channel.revision + 1
        control = ControlMessage(
            control_id=uuid4(),
            channel_id=channel_id,
            kind=ControlKind.approval_mode_changed,
            actor_id=actor.sender_id,
            reason=(
                "user explicitly confirmed task-local auto-forward"
                if request.approval_mode is ApprovalMode.auto_forward
                else "user restored per-report approval"
            ),
            previous_value=channel.approval_mode.value,
            new_value=request.approval_mode.value,
            created_at=self._now(),
        )
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.control,
            sender_role=SenderRole.user,
            sender_id=actor.sender_id,
            correlation_id=channel_id,
            causal_parent_id=None,
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.control_v1,
            payload=control,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.set_channel_approval_mode(
            channel_id,
            approval_mode=request.approval_mode.value,
            expected_revision=channel.revision,
            resulting_revision=next_revision,
            idempotency_key=request.idempotency_key,
            actor_id=actor.sender_id,
            message=message,
            audit=self._audit_record(
                channel_id=channel_id,
                entity_kind="channel",
                entity_id=channel_id,
                event_type="collaboration.approval_mode_changed",
                actor_role=SenderRole.user,
                actor_id=actor.sender_id,
                idempotency_key=request.idempotency_key,
                details={
                    "task_id": channel.task_id,
                    "task_revision": channel.task_revision,
                    "previous_value": channel.approval_mode.value,
                    "new_value": request.approval_mode.value,
                },
            ),
        )
        self._notify_events(channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="channel.close",
        digest_operation="channel.close",
        scope_parameter="channel_id",
        binding_parameters=("channel_id",),
    )
    async def close_channel(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: ChannelCloseRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "channel.close", request, channel_id=channel_id
        )
        operation_replay = await self._operation_replay(
            operation="channel.close",
            scope_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.control_v1,
            correlation_id=channel_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_channel(channel_id))
        channel = await self._channel(channel_id)
        self._require_open(channel)
        if channel.revision != request.expected_channel_revision:
            raise CollaborationConflict(
                "channel_revision_conflict",
                "channel changed before close",
            )
        control = ControlMessage(
            control_id=uuid4(),
            channel_id=channel_id,
            kind=ControlKind.channel_closed,
            actor_id=actor.sender_id,
            reason=request.reason,
            previous_value=channel.status.value,
            new_value=ChannelStatus.closed.value,
            created_at=self._now(),
        )
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.control,
            sender_role=SenderRole.user,
            sender_id=actor.sender_id,
            correlation_id=channel_id,
            causal_parent_id=None,
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.control_v1,
            payload=control,
            client_request_digest=client_request_digest,
        )
        if self._assignment_cancel_handler is not None:
            cancellation = self._assignment_cancel_handler(
                channel.assignment_id,
                f"collaboration.close.{request.idempotency_key}",
                request.reason,
            )
            if inspect.isawaitable(cancellation):
                await cancellation
        stored = await self.store.close_channel(
            channel_id,
            expected_revision=channel.revision,
            idempotency_key=request.idempotency_key,
            actor_id=actor.sender_id,
            reason=request.reason,
            message=message,
            audit=self._audit_record(
                channel_id=channel_id,
                entity_kind="channel",
                entity_id=channel_id,
                event_type="collaboration.channel_closed",
                actor_role=SenderRole.user,
                actor_id=actor.sender_id,
                idempotency_key=request.idempotency_key,
                details={"reason": request.reason},
            ),
        )
        self._notify_events(channel_id)
        return _as_dict(stored)

    async def list_channels(
        self,
        *,
        principal: Any,
        status: str | None,
        limit: int,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        parsed_status = ChannelStatus(status) if status is not None else None
        records = await self.store.list_channels(
            status=parsed_status.value if parsed_status is not None else None,
            limit=limit,
        )
        channels: list[dict[str, Any]] = []
        get_reader_cursor = getattr(self.store, "get_reader_cursor", None)
        for item in _as_list(records):
            channel = CollaborationChannel.model_validate(item)
            projected = channel.model_dump(mode="json", exclude_none=True)
            if callable(get_reader_cursor):
                cursor = _as_dict(
                    await get_reader_cursor(
                        channel.channel_id,
                        actor.sender_id,
                        reader_role=SenderRole.user,
                    )
                )
                projected["unread_count"] = max(
                    0,
                    channel.next_seq - 1 - int(cursor.get("ack_seq", 0)),
                )
            channels.append(projected)
        return {"channels": channels, "count": len(channels)}

    async def get_channel_detail(
        self,
        *,
        principal: Any,
        channel_id: UUID,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        channel = await self._channel(channel_id)
        typed_reports = [
            CollaborationReport.model_validate(item)
            for item in _as_list(await self.store.list_reports(channel_id=channel_id))
        ]
        reports = [item.model_dump(mode="json", exclude_none=True) for item in typed_reports]
        messages: list[dict[str, Any]] = []
        after = 0
        while True:
            page = await self.list_events(
                principal=actor,
                channel_id=channel_id,
                after=after,
                limit=5_000,
            )
            messages.extend(page)
            if len(page) < 5_000:
                break
            after = int(page[-1]["seq"])
        context_projection: dict[str, Any] | None = None
        result: dict[str, Any] = {
            "channel": channel.model_dump(mode="json", exclude_none=True),
            "reports": reports,
            "timeline": messages,
        }
        if self._studio_context_provider is not None:
            context = self._studio_context_provider(channel)
            if inspect.isawaitable(context):
                context = await context
            if isinstance(context, BaseModel):
                context = context.model_dump(mode="json", exclude_none=True)
            if not isinstance(context, Mapping):
                raise CollaborationConflict(
                    "studio_context_invalid",
                    "Studio collaboration context projection is unavailable",
                )
            context_projection = dict(context)
            result["context"] = context_projection
        active_leases: list[dict[str, Any]] = []
        get_active_lease = getattr(self.store, "get_active_lease", None)
        if callable(get_active_lease):
            for report in typed_reports:
                lease = await get_active_lease(report.report_id, now=self._now())
                if lease is not None:
                    active_leases.append(
                        DeveloperLease.model_validate(lease).model_dump(
                            mode="json",
                            exclude_none=True,
                        )
                    )
        claims: list[dict[str, Any]] = []
        list_claims = getattr(self.store, "list_claims", None)
        if callable(list_claims):
            claims = [
                VerificationClaim.model_validate(item).model_dump(
                    mode="json",
                    exclude_none=True,
                )
                for item in _as_list(await list_claims(channel_id=channel_id, after=0, limit=5_000))
            ]
        unread_count = max(0, channel.next_seq - 1)
        reader_cursor: dict[str, Any] | None = None
        get_reader_cursor = getattr(self.store, "get_reader_cursor", None)
        if callable(get_reader_cursor):
            reader_cursor = _as_dict(
                await get_reader_cursor(
                    channel_id,
                    actor.sender_id,
                    reader_role=SenderRole.user,
                )
            )
            unread_count = max(
                0,
                channel.next_seq - 1 - int(reader_cursor.get("ack_seq", 0)),
            )
        result["active_leases"] = active_leases
        result["claims"] = claims
        result["derived"] = _studio_derived_status(
            channel=channel,
            reports=typed_reports,
            active_leases=active_leases,
            claims=claims,
            context=context_projection,
            unread_count=unread_count,
        )
        ack_reader = getattr(self.store, "ack_reader", None)
        highest_seq = channel.next_seq - 1
        if callable(ack_reader) and callable(get_reader_cursor):
            # Reading the detail marks the current durable sequence as seen, but
            # concurrent browser refreshes must not turn this GET into a 409.
            # Re-read and retry the monotonic cursor after a compare-and-set race.
            current_cursor = reader_cursor
            for _ in range(3):
                if current_cursor is None:
                    current_cursor = _as_dict(
                        await get_reader_cursor(
                            channel_id,
                            actor.sender_id,
                            reader_role=SenderRole.user,
                        )
                    )
                if int(current_cursor.get("ack_seq", 0)) >= highest_seq:
                    break
                cursor_revision = int(current_cursor.get("revision", 0))
                try:
                    await ack_reader(
                        channel_id,
                        actor.sender_id,
                        highest_seq,
                        reader_role=SenderRole.user,
                        expected_cursor_revision=cursor_revision,
                        idempotency_key=(
                            f"studio.read.{channel_id.hex}.{highest_seq}.{cursor_revision}"
                        ),
                    )
                    break
                except Exception as error:
                    if getattr(error, "status_code", None) != 409:
                        raise
                    current_cursor = None
        return result

    async def export_causal_chain(
        self,
        *,
        principal: Any,
        channel_id: UUID,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.user:
            raise CollaborationNotFound()
        await self._channel(channel_id)
        exported = _as_dict(await self.store.export_channel(channel_id))
        messages = sorted(
            (_as_dict(item) for item in _as_list(exported.get("messages"))),
            key=lambda item: int(item["seq"]),
        )
        ids: dict[str, int] = {}
        correlations: set[str] = set()
        for expected_seq, message in enumerate(messages, start=1):
            if int(message["seq"]) != expected_seq:
                raise CollaborationConflict(
                    "causal_export_gap",
                    "channel export contains a non-contiguous message sequence",
                )
            message_id = str(message["message_id"])
            parent = message.get("causal_parent_id")
            if parent is not None and str(parent) not in ids:
                raise CollaborationConflict(
                    "causal_export_parent_invalid",
                    "channel export contains a missing or forward causal parent",
                )
            ids[message_id] = expected_seq
            correlations.add(str(message["correlation_id"]))
            CollaborationMessageEnvelope.model_validate(message)
        exported["messages"] = messages
        canonical = json.dumps(
            exported,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return {
            "schema_version": "1.0",
            "channel_id": str(channel_id),
            "export": exported,
            "counters": {
                "messages": len(messages),
                "correlations": len(correlations),
                "reports": len(_as_list(exported.get("reports"))),
                "claims": len(_as_list(exported.get("claims"))),
            },
            "digest": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        }

    async def developer_inbox(
        self,
        *,
        principal: Any,
        after: int,
        limit: int,
        route: str | None,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex:
            raise CollaborationNotFound()
        await self.store.expire_developer_leases(now=self._now())
        parsed_route = ReportRoute(route) if route is not None else None
        delivery_reader = getattr(self.store, "list_developer_inbox_deliveries", None)
        if callable(delivery_reader):
            visible_reports: list[dict[str, Any]] = []
            claims: list[dict[str, Any]] = []
            cursor = after
            exhausted = False
            while len(visible_reports) + len(claims) < limit and not exhausted:
                deliveries = _as_list(await delivery_reader(after=cursor, limit=5_000))
                if not deliveries:
                    break
                exhausted = len(deliveries) < 5_000
                for delivery in deliveries:
                    cursor = int(delivery["delivery_seq"])
                    payload = _as_dict(delivery.get("payload", {}))
                    report_id = payload.get("report_id")
                    claim_id = payload.get("claim_id")
                    if report_id is not None:
                        report = CollaborationReport.model_validate(
                            delivery.get("report_snapshot")
                            or await self.store.get_report(str(report_id))
                        )
                        if report.status not in {
                            ReportStatus.approved_for_codex,
                            ReportStatus.implementing,
                            ReportStatus.ready_for_lilies_verification,
                            ReportStatus.routed_to_task_author,
                            ReportStatus.task_package_amended,
                            ReportStatus.environment_failed,
                            ReportStatus.environment_restored,
                            ReportStatus.unresolved,
                        }:
                            continue
                        if parsed_route is not None and report.route is not parsed_route:
                            continue
                        visible_reports.append(report.model_dump(mode="json", exclude_none=True))
                    elif claim_id is not None:
                        claim = VerificationClaim.model_validate(
                            delivery.get("claim_snapshot")
                            or await self.store.get_claim(str(claim_id))
                        )
                        if claim.status is not ClaimStatus.frozen:
                            continue
                        claims.append(claim.model_dump(mode="json", exclude_none=True))
                    if len(visible_reports) + len(claims) >= limit:
                        break
            pending = bool(await self.store.has_pending_user_action())
            return {
                "reports": visible_reports,
                "claims": claims,
                "pending_user_action": pending,
                "next_cursor": cursor,
            }
        reports = _as_list(
            await self.store.list_reports(
                statuses=None,
                developer_visible_only=True,
                route=parsed_route.value if parsed_route is not None else None,
                after=0,
                limit=5_000,
            )
        )
        visible_entries: list[tuple[str, str, str, dict[str, Any]]] = []
        for item in reports:
            report = CollaborationReport.model_validate(item)
            if report.status not in {
                ReportStatus.approved_for_codex,
                ReportStatus.implementing,
                ReportStatus.ready_for_lilies_verification,
                ReportStatus.routed_to_task_author,
                ReportStatus.task_package_amended,
                ReportStatus.environment_failed,
                ReportStatus.environment_restored,
                ReportStatus.unresolved,
            }:
                continue
            visible_entries.append(
                (
                    report.updated_at.isoformat(),
                    "report",
                    str(report.report_id),
                    report.model_dump(mode="json", exclude_none=True),
                )
            )
        if hasattr(self.store, "list_claims"):
            for item in _as_list(
                await self.store.list_claims(statuses=[ClaimStatus.frozen.value], limit=5_000)
            ):
                claim = VerificationClaim.model_validate(item)
                visible_entries.append(
                    (
                        claim.created_at.isoformat(),
                        "claim",
                        str(claim.claim_id),
                        claim.model_dump(mode="json", exclude_none=True),
                    )
                )
        visible_entries.sort(key=lambda item: item[:3])
        page = visible_entries[after : after + limit]
        visible_reports = [item[3] for item in page if item[1] == "report"]
        claims = [item[3] for item in page if item[1] == "claim"]
        if hasattr(self.store, "has_pending_user_action"):
            pending = bool(await self.store.has_pending_user_action())
        else:
            pending_rows = _as_list(
                await self.store.list_reports(
                    statuses=[ReportStatus.awaiting_user_review.value],
                    developer_visible_only=False,
                    limit=1,
                )
            )
            pending = bool(pending_rows)
        return {
            "reports": visible_reports,
            "claims": claims,
            # This is deliberately global.  No task filter, count, category,
            # or timing metadata may expose pre-approval business content.
            "pending_user_action": pending,
            "next_cursor": after + len(page),
        }

    @_replay_lease_after_conflict("acquire")
    async def acquire_developer_lease(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: LeaseAcquireRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.owner_id != actor.sender_id:
            raise CollaborationNotFound()
        replay = await self._developer_lease_replay(
            report_id=report_id,
            operation="acquire",
            actor=actor,
            idempotency_key=request.idempotency_key,
            expected_revision=request.expected_report_revision,
            ttl_seconds=request.ttl_seconds,
        )
        if replay is not None:
            replay_report = CollaborationReport.model_validate(
                await self.store.get_report(report_id)
            )
            self._require_developer_visible(replay_report)
            replay_channel = await self._channel(replay_report.channel_id)
            self._require_open(replay_channel)
            return await self._lease_with_developer_workspace(
                replay,
                replay_channel,
            )
        await self.store.expire_developer_leases(now=self._now())
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict", "report changed before lease acquisition"
            )
        if report.status not in {
            ReportStatus.approved_for_codex,
            ReportStatus.verification_failed,
            ReportStatus.routed_to_task_author,
            ReportStatus.environment_failed,
            ReportStatus.unresolved,
        }:
            raise CollaborationConflict(
                "report_not_leaseable", "report is not available for developer handling"
            )
        developer_workspace = await self._developer_workspace(channel)
        now = self._now()
        lease = DeveloperLease(
            lease_id=uuid4(),
            report_id=report_id,
            report_revision=report.revision,
            owner_id=actor.sender_id,
            status="active",
            revision=1,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=request.ttl_seconds),
        )
        stored = await self.store.acquire_developer_lease(
            {
                **lease.model_dump(mode="json", exclude_none=True),
                "idempotency_key": request.idempotency_key,
            },
            ttl_seconds=request.ttl_seconds,
            now=now,
            next_report_status=(
                ReportStatus.implementing.value
                if report.category in _CAPABILITY_CATEGORIES
                else (
                    ReportStatus.routed_to_task_author.value
                    if report.category is ReportCategory.environment_gap
                    else None
                )
            ),
        )
        result = _as_dict(stored)
        if developer_workspace is not None:
            result["developer_workspace"] = developer_workspace.model_dump(
                mode="json",
                exclude_none=True,
            )
        return result

    @_replay_lease_after_conflict("renew")
    async def renew_developer_lease(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: LeaseRenewRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.owner_id != actor.sender_id:
            raise CollaborationNotFound()
        replay = await self._developer_lease_replay(
            report_id=report_id,
            operation="renew",
            actor=actor,
            idempotency_key=request.idempotency_key,
            expected_revision=request.expected_lease_revision,
            ttl_seconds=request.ttl_seconds,
        )
        if replay is not None:
            return replay
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        self._require_developer_visible(report)
        self._require_open(await self._channel(report.channel_id))
        lease = await self._active_owned_lease(report=report, actor=actor)
        stored = await self.store.renew_developer_lease(
            lease.lease_id,
            owner_id=actor.sender_id,
            expected_revision=request.expected_lease_revision,
            ttl_seconds=request.ttl_seconds,
            idempotency_key=request.idempotency_key,
            now=self._now(),
        )
        return _as_dict(stored)

    @_replay_lease_after_conflict("release")
    async def release_developer_lease(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: LeaseReleaseRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.owner_id != actor.sender_id:
            raise CollaborationNotFound()
        replay = await self._developer_lease_replay(
            report_id=report_id,
            operation="release",
            actor=actor,
            idempotency_key=request.idempotency_key,
            expected_revision=request.expected_lease_revision,
            reason=request.reason,
        )
        if replay is not None:
            return replay
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        self._require_developer_visible(report)
        self._require_open(await self._channel(report.channel_id))
        lease = await self._active_owned_lease(report=report, actor=actor)
        stored = await self.store.release_developer_lease(
            lease.lease_id,
            owner_id=actor.sender_id,
            expected_revision=request.expected_lease_revision,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            now=self._now(),
        )
        return _as_dict(stored)

    async def _active_owned_lease(
        self,
        *,
        report: CollaborationReport,
        actor: CollaborationPrincipal,
        lease_id: UUID | None = None,
    ) -> DeveloperLease:
        await self.store.expire_developer_leases(now=self._now())
        try:
            lease = DeveloperLease.model_validate(
                await self.store.get_active_lease(report.report_id)
            )
        except Exception as error:
            raise CollaborationConflict(
                "developer_lease_required",
                "an active developer lease is required for this response",
            ) from error
        if lease.owner_id != actor.sender_id or lease.report_revision != report.revision:
            raise CollaborationConflict(
                "developer_lease_mismatch",
                "developer lease owner or report revision does not match",
            )
        if lease.expires_at <= self._now():
            await self.store.expire_developer_leases(now=self._now())
            raise CollaborationConflict(
                "developer_lease_expired", "developer lease expired before the response"
            )
        if lease_id is not None and lease.lease_id != lease_id:
            raise CollaborationConflict(
                "developer_lease_mismatch", "developer response references another lease"
            )
        return lease

    async def run_developer_worker(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: DeveloperWorkerRunRequest,
    ) -> dict[str, Any]:
        """Run the fixed developer runtime without disclosing its API bearer."""

        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.lease_owner_id != actor.sender_id:
            raise CollaborationNotFound()
        report = CollaborationReport.model_validate(
            await self.store.get_report(report_id)
        )
        client_request_digest = self._client_request_digest(
            "developer.worker",
            request,
            report_id=report_id,
        )
        replay = await self._operation_replay(
            operation="developer.worker",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            try:
                return DeveloperWorkerReceipt.model_validate(replay).model_dump(
                    mode="json",
                    exclude_none=True,
                )
            except ValidationError as error:
                raise CollaborationConflict(
                    "developer_worker_receipt_untrusted",
                    "the archived developer worker receipt is invalid",
                ) from error
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.category not in _CAPABILITY_CATEGORIES:
            raise CollaborationConflict(
                "wrong_response_route",
                "only capability reports can run the developer worker",
            )
        if report.status is not ReportStatus.implementing:
            raise CollaborationConflict(
                "report_not_implementing",
                "report is not in the implementing state",
            )
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict",
                "report changed before developer worker execution",
            )
        lease = await self._active_owned_lease(
            report=report,
            actor=actor,
            lease_id=request.lease_id,
        )
        provider = self._developer_worker_provider
        if provider is None:
            raise CollaborationConflict(
                "developer_worker_unavailable",
                "the trusted OS developer worker is unavailable",
            )
        try:
            supplied = provider(channel, report, lease, request)
            if inspect.isawaitable(supplied):
                supplied = await supplied
            receipt = DeveloperWorkerReceipt.model_validate(supplied)
        except Exception as error:
            raise CollaborationConflict(
                "developer_worker_rejected",
                "the developer runtime did not produce a trusted OS receipt",
            ) from error
        if (
            receipt.assignment_id != channel.assignment_id
            or receipt.channel_id != channel.channel_id
            or receipt.report_id != report.report_id
            or receipt.report_revision != report.revision
            or receipt.lease_id != lease.lease_id
            or receipt.lease_owner_id != lease.owner_id
            or receipt.response_id != request.response_id
        ):
            raise CollaborationConflict(
                "developer_worker_receipt_untrusted",
                "the developer worker receipt has another formal binding",
            )
        reference = DeveloperWorkerReceiptReference(
            receipt_id=receipt.receipt_id,
            receipt_digest=receipt.receipt_digest,
        )
        await self._require_developer_worker_receipt(
            channel=channel,
            report=report,
            lease=lease,
            response_id=request.response_id,
            reference=reference,
            require_success=False,
        )
        persist = getattr(self.store, "record_operation_receipt", None)
        if not callable(persist):
            raise CollaborationConflict(
                "developer_worker_receipt_store_unavailable",
                "the trusted developer worker receipt store is unavailable",
            )
        stored = await persist(
            operation="developer.worker",
            scope_id=report_id,
            actor_role=actor.role.value,
            actor_id=actor.sender_id,
            idempotency_key=request.idempotency_key,
            request_digest=client_request_digest,
            response_kind="developer_worker_receipt",
            response=receipt.model_dump(mode="json", exclude_none=True),
        )
        try:
            return DeveloperWorkerReceipt.model_validate(stored).model_dump(
                mode="json",
                exclude_none=True,
            )
        except ValidationError as error:
            raise CollaborationConflict(
                "developer_worker_receipt_untrusted",
                "the durable developer worker receipt changed during archival",
            ) from error

    async def promote_developer_source(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: DeveloperSourcePromotionRequest,
    ) -> dict[str, Any]:
        """Promote the exact private workspace delta before DeveloperResponse."""

        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.lease_owner_id != actor.sender_id:
            raise CollaborationNotFound()
        report = CollaborationReport.model_validate(
            await self.store.get_report(report_id)
        )
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.category not in _CAPABILITY_CATEGORIES:
            raise CollaborationConflict(
                "wrong_response_route",
                "only capability reports can promote developer source",
            )
        if report.status is not ReportStatus.implementing:
            raise CollaborationConflict(
                "report_not_implementing",
                "report is not in the implementing state",
            )
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict",
                "report changed before developer source promotion",
            )
        lease = await self._active_owned_lease(
            report=report,
            actor=actor,
            lease_id=request.lease_id,
        )
        await self._require_developer_worker_receipt(
            channel=channel,
            report=report,
            lease=lease,
            response_id=request.response_id,
            reference=request.developer_worker_receipt,
            require_success=True,
        )
        provider = self._developer_source_promotion_provider
        if provider is None:
            raise CollaborationConflict(
                "developer_source_promotion_unavailable",
                "trusted developer source promotion is not configured",
            )
        try:
            promoted = provider(channel, report, lease, request)
            if inspect.isawaitable(promoted):
                promoted = await promoted
        except Exception as error:
            raise CollaborationConflict(
                "developer_source_promotion_rejected",
                "the lease-bound developer workspace delta could not be promoted",
            ) from error
        result = _as_dict(promoted)
        if (
            str(result.get("assignment_id")) != str(channel.assignment_id)
            or str(result.get("channel_id")) != str(channel.channel_id)
            or str(result.get("report_id")) != str(report.report_id)
            or str(result.get("response_id")) != str(request.response_id)
            or result.get("activation_state") != "activated"
            or not result.get("commit_sha")
        ):
            raise CollaborationConflict(
                "developer_source_promotion_invalid",
                "trusted promotion returned another report or activation state",
            )
        return result

    @_replay_receipt_after_conflict(
        receipt_operation="developer.response",
        digest_operation="developer.response",
        scope_parameter="report_id",
        binding_parameters=("report_id",),
    )
    async def submit_developer_response(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: DeveloperResponseRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex or request.lease_owner_id != actor.sender_id:
            raise CollaborationNotFound()
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        client_request_digest = self._client_request_digest(
            "developer.response", request, report_id=report_id
        )
        operation_replay = await self._operation_replay(
            operation="developer.response",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            replay_response_id = operation_replay.get("response_id")
            if replay_response_id is not None:
                await self._record_formal_source_response(
                    channel_id=report.channel_id,
                    response_id=UUID(str(replay_response_id)),
                )
            return operation_replay
        replay = await self._message_replay(
            channel_id=report.channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.developer_response_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = DeveloperResponse.model_validate(replay.payload)
            await self._record_formal_source_response(
                channel_id=report.channel_id,
                response_id=persisted.response_id,
            )
            return await self._persisted_replay(
                "get_developer_response", persisted.response_id, replay.payload
            )
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.category not in _CAPABILITY_CATEGORIES:
            raise CollaborationConflict(
                "wrong_response_route",
                "task and environment reports require their dedicated response schemas",
            )
        if report.status is not ReportStatus.implementing:
            raise CollaborationConflict(
                "report_not_implementing", "report is not in the implementing state"
            )
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict", "report changed before developer response"
            )
        lease = await self._active_owned_lease(
            report=report,
            actor=actor,
            lease_id=request.lease_id,
        )
        response = DeveloperResponse.model_validate(
            {
                **request.response.model_dump(mode="json", exclude_none=True),
                "channel_id": report.channel_id,
                "report_id": report_id,
                "report_revision": report.revision,
                "created_at": self._now(),
            }
        )
        await self._require_developer_worker_receipt(
            channel=channel,
            report=report,
            lease=lease,
            response_id=response.response_id,
            reference=request.developer_worker_receipt,
            require_success=response.outcome.value == "implemented",
        )
        await self._require_developer_response_evidence(channel, response)
        next_status = (
            ReportStatus.ready_for_lilies_verification
            if response.outcome.value == "implemented"
            else ReportStatus.evidence_collecting
        )
        next_route = (
            ReportRoute.developer
            if response.outcome.value == "implemented"
            else ReportRoute.capability_approval
        )
        next_visibility = (
            MessageVisibility.approved_developer
            if response.outcome.value == "implemented"
            else MessageVisibility.user_and_lilies
        )
        message = self._message(
            channel_id=report.channel_id,
            message_type=MessageType.developer_response,
            sender_role=SenderRole.codex,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(report),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.developer_response_v1,
            payload=response,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_developer_response(
            {
                **response.model_dump(mode="json", exclude_none=True),
                "lease_id": str(request.lease_id),
                "lease_owner_id": actor.sender_id,
                "idempotency_key": request.idempotency_key,
                "next_report_route": next_route.value,
                "next_visibility": next_visibility.value,
            },
            next_report_status=next_status.value,
            message=message,
            outbox=self._outbox_record(
                channel_id=report.channel_id,
                message_id=UUID(str(message["message_id"])),
                destination="lilies_updates",
                idempotency_key=request.idempotency_key,
                payload={"report_id": str(report_id)},
            ),
        )
        await self._record_formal_source_response(
            channel_id=report.channel_id,
            response_id=response.response_id,
        )
        self._notify_events(report.channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="task.amendment",
        digest_operation="task.amendment",
        scope_parameter="report_id",
        binding_parameters=("report_id",),
        receipt_actor_role=SenderRole.task_author,
    )
    async def submit_task_amendment(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: TaskPackageAmendmentRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex:
            raise CollaborationNotFound()
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        client_request_digest = self._client_request_digest(
            "task.amendment", request, report_id=report_id
        )
        task_author = CollaborationPrincipal(
            role=SenderRole.task_author,
            sender_id=actor.sender_id,
            scopes=actor.scopes,
        )
        operation_replay = await self._operation_replay(
            operation="task.amendment",
            scope_id=report_id,
            actor=task_author,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=report.channel_id,
            actor=task_author,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.task_amendment_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = TaskPackageAmendment.model_validate(replay.payload)
            return await self._persisted_replay(
                "get_task_amendment", persisted.amendment_id, replay.payload
            )
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.category is not ReportCategory.task_spec_gap:
            raise CollaborationConflict("wrong_response_route", "report is not a task-package gap")
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict", "report changed before task amendment"
            )
        if (
            request.amendment.previous_task_revision != channel.task_revision
            or request.amendment.previous_requirement_digest != report.requirement_digest
        ):
            raise CollaborationConflict(
                "task_amendment_context_mismatch",
                "task amendment does not match the routed task revision and requirement",
            )
        lease = await self._active_owned_lease(report=report, actor=actor)
        amendment = TaskPackageAmendment.model_validate(
            {
                **request.amendment.model_dump(mode="json", exclude_none=True),
                "channel_id": report.channel_id,
                "report_id": report_id,
                "report_revision": report.revision,
                "task_id": channel.task_id,
                "created_at": self._now(),
            }
        )
        next_status = (
            ReportStatus.task_package_amended
            if amendment.outcome is TaskAmendmentOutcome.amended
            else ReportStatus.rejected_with_evidence
        )
        message = self._message(
            channel_id=report.channel_id,
            message_type=MessageType.task_amendment,
            sender_role=SenderRole.task_author,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(report),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.task_amendment_v1,
            payload=amendment,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_task_amendment(
            {
                **amendment.model_dump(mode="json", exclude_none=True),
                "lease_id": str(lease.lease_id),
                "lease_owner_id": actor.sender_id,
                "idempotency_key": request.idempotency_key,
                "next_report_status": next_status.value,
            },
            message,
        )
        self._notify_events(report.channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="environment.response",
        digest_operation="environment.response",
        scope_parameter="report_id",
        binding_parameters=("report_id",),
        receipt_actor_role=SenderRole.task_author,
    )
    async def submit_environment_response(
        self,
        *,
        principal: Any,
        report_id: UUID,
        request: EnvironmentResponseRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.codex:
            raise CollaborationNotFound()
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        client_request_digest = self._client_request_digest(
            "environment.response", request, report_id=report_id
        )
        task_author = CollaborationPrincipal(
            role=SenderRole.task_author,
            sender_id=actor.sender_id,
            scopes=actor.scopes,
        )
        operation_replay = await self._operation_replay(
            operation="environment.response",
            scope_id=report_id,
            actor=task_author,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=report.channel_id,
            actor=task_author,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.environment_response_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = EnvironmentResponse.model_validate(replay.payload)
            return await self._persisted_replay(
                "get_environment_response", persisted.response_id, replay.payload
            )
        self._require_developer_visible(report)
        channel = await self._channel(report.channel_id)
        self._require_open(channel)
        if report.category is not ReportCategory.environment_gap:
            raise CollaborationConflict("wrong_response_route", "report is not an environment gap")
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict", "report changed before environment response"
            )
        lease = await self._active_owned_lease(report=report, actor=actor)
        response = EnvironmentResponse.model_validate(
            {
                **request.response.model_dump(mode="json", exclude_none=True),
                "channel_id": report.channel_id,
                "report_id": report_id,
                "report_revision": report.revision,
                "created_at": self._now(),
            }
        )
        next_status = (
            ReportStatus.environment_restored
            if response.outcome is EnvironmentOutcome.restored
            else ReportStatus.unresolved
        )
        message = self._message(
            channel_id=report.channel_id,
            message_type=MessageType.environment_response,
            sender_role=SenderRole.task_author,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(report),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.environment_response_v1,
            payload=response,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_environment_response(
            {
                **response.model_dump(mode="json", exclude_none=True),
                "lease_id": str(lease.lease_id),
                "lease_owner_id": actor.sender_id,
                "idempotency_key": request.idempotency_key,
                "next_report_status": next_status.value,
            },
            message,
        )
        self._notify_events(report.channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="lilies.reprobe",
        digest_operation="lilies.reprobe",
        scope_parameter="report_id",
        binding_parameters=("channel_id", "report_id"),
    )
    async def submit_lilies_reprobe(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        report_id: UUID,
        request: LiliesReprobeResultRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies:
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "lilies.reprobe",
            request,
            channel_id=channel_id,
            report_id=report_id,
        )
        operation_replay = await self._operation_replay(
            operation="lilies.reprobe",
            scope_id=report_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.lilies_reprobe_result_v1,
            correlation_id=report_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = LiliesReprobeResult.model_validate(replay.payload)
            return await self._persisted_replay("get_reprobe", persisted.reprobe_id, replay.payload)
        channel = await self._channel(channel_id, principal=actor)
        self._require_open(channel)
        report = CollaborationReport.model_validate(await self.store.get_report(report_id))
        if report.channel_id != channel_id:
            raise CollaborationNotFound()
        if report.revision != request.expected_report_revision:
            raise CollaborationConflict(
                "report_revision_conflict", "report changed before Lilies reprobe"
            )
        if report.category in _CAPABILITY_CATEGORIES:
            if report.status is not ReportStatus.ready_for_lilies_verification:
                raise CollaborationConflict(
                    "report_not_ready_for_reprobe",
                    "platform report has no complete developer response to reprobe",
                )
            if hasattr(self.store, "get_latest_developer_response"):
                response = DeveloperResponse.model_validate(
                    await self.store.get_latest_developer_response(report_id)
                )
                if (
                    response.new_contract_digest is not None
                    and request.result.contract_digest != response.new_contract_digest
                ):
                    raise CollaborationConflict(
                        "contract_refresh_required",
                        "Lilies must refresh and reprobe the developer response contract digest",
                    )
                if request.result.steps != response.reprobe_steps:
                    raise CollaborationConflict(
                        "reprobe_steps_mismatch",
                        "Lilies reprobe must execute the developer response steps in order",
                    )
            next_status = (
                ReportStatus.lilies_verified
                if request.result.outcome is ReprobeOutcome.lilies_verified
                else ReportStatus.verification_failed
            )
        elif report.category is ReportCategory.task_spec_gap:
            if report.status not in {
                ReportStatus.task_package_amended,
                ReportStatus.rejected_with_evidence,
            }:
                raise CollaborationConflict(
                    "task_amendment_not_ready", "no amended task package is ready to recheck"
                )
            latest = await self._latest_report_chain_message(report)
            if latest is None or latest.payload_schema is not PayloadSchema.task_amendment_v1:
                raise CollaborationConflict(
                    "task_amendment_evidence_missing",
                    "task recheck requires the latest persisted amendment response",
                )
            amendment = TaskPackageAmendment.model_validate(latest.payload)
            expected_digest = (
                amendment.new_requirement_digest
                if amendment.outcome is TaskAmendmentOutcome.amended
                else amendment.previous_requirement_digest
            )
            if request.result.contract_digest != expected_digest:
                raise CollaborationConflict(
                    "task_revision_refresh_required",
                    "Lilies must recheck the exact amended task requirement digest",
                )
            next_status = (
                ReportStatus.lilies_rechecks
                if request.result.outcome is ReprobeOutcome.lilies_verified
                else ReportStatus.routed_to_task_author
            )
        else:
            if report.status is not ReportStatus.environment_restored:
                raise CollaborationConflict(
                    "environment_not_restored",
                    "an unresolved environment cannot be reported as rechecked",
                )
            latest = await self._latest_report_chain_message(report)
            if latest is None or latest.payload_schema is not PayloadSchema.environment_response_v1:
                raise CollaborationConflict(
                    "environment_evidence_missing",
                    "environment recheck requires the latest persisted health response",
                )
            environment = EnvironmentResponse.model_validate(latest.payload)
            if request.result.contract_digest != environment.environment_digest:
                raise CollaborationConflict(
                    "environment_refresh_required",
                    "Lilies must recheck the exact restored environment digest",
                )
            expected_steps = [
                {
                    "order": index,
                    "action": check.command,
                    "expected": f"exit_code={check.exit_code}: {check.summary}",
                }
                for index, check in enumerate(environment.health_checks, start=1)
            ]
            if [step.model_dump(mode="json") for step in request.result.steps] != expected_steps:
                raise CollaborationConflict(
                    "environment_health_steps_mismatch",
                    "Lilies must rerun every restored health-check command in order",
                )
            response_evidence = {
                (check.evidence_ref.evidence_id, check.evidence_ref.digest)
                for check in environment.health_checks
            }
            reprobe_evidence = {
                (evidence.evidence_id, evidence.digest) for evidence in request.result.evidence_refs
            }
            if response_evidence.intersection(reprobe_evidence) or len(reprobe_evidence) < len(
                environment.health_checks
            ):
                raise CollaborationConflict(
                    "environment_health_evidence_not_independent",
                    "Lilies must produce fresh evidence for its own restored-host health checks",
                )
            if any(
                evidence.captured_at < environment.created_at
                for evidence in request.result.evidence_refs
            ):
                raise CollaborationConflict(
                    "environment_health_evidence_stale",
                    "Lilies health-check evidence must postdate the environment response",
                )
            next_status = (
                ReportStatus.lilies_health_checks
                if request.result.outcome is ReprobeOutcome.lilies_verified
                else ReportStatus.environment_failed
            )
        result = LiliesReprobeResult.model_validate(
            {
                **request.result.model_dump(mode="json", exclude_none=True),
                "channel_id": channel_id,
                "report_id": report_id,
                "report_revision": report.revision,
                "created_at": self._now(),
            }
        )
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.control,
            sender_role=SenderRole.lilies,
            sender_id=actor.sender_id,
            correlation_id=report_id,
            causal_parent_id=await self._latest_report_chain_parent(report),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.lilies_reprobe_result_v1,
            payload=result,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_reprobe(
            {
                **result.model_dump(mode="json", exclude_none=True),
                "idempotency_key": request.idempotency_key,
                "next_report_status": next_status.value,
            },
            message,
        )
        self._notify_events(channel_id)
        return _as_dict(stored)

    @_replay_receipt_after_conflict(
        receipt_operation="verification.claim",
        digest_operation="verification.claim",
        scope_parameter="channel_id",
        binding_parameters=("channel_id",),
    )
    async def submit_verification_claim(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: VerificationClaimRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies:
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "verification.claim", request, channel_id=channel_id
        )
        operation_replay = await self._operation_replay(
            operation="verification.claim",
            scope_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.verification_claim_v1,
            correlation_id=request.claim.claim_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            return _as_dict(await self.store.get_claim(request.claim.claim_id))
        channel = await self._channel(channel_id, principal=actor)
        self._require_open(channel)
        if channel.revision != request.expected_channel_revision:
            raise CollaborationConflict(
                "channel_revision_conflict", "channel changed before claim submission"
            )
        if request.claim.application_id not in channel.application_ids:
            raise CollaborationConflict(
                "claim_application_mismatch",
                "claim application is outside this assignment's application binding",
            )
        current_draft = await self._current_draft(request.claim.application_id)
        if current_draft is not None and (
            int(current_draft.get("revision", -1)) != request.claim.draft_revision
            or _canonical_sha256(current_draft.get("content_hash"))
            != _canonical_sha256(request.claim.content_hash)
        ):
            raise CollaborationConflict(
                "claim_draft_mismatch",
                "claim revision and content hash do not match the current draft",
            )
        channel_reports: list[CollaborationReport] = []
        offset = 0
        while True:
            page = _as_list(
                await self.store.list_reports(
                    channel_id=channel_id,
                    after=offset,
                    limit=5_000,
                )
            )
            channel_reports.extend(CollaborationReport.model_validate(item) for item in page)
            if len(page) < 5_000:
                break
            offset += len(page)
        submitted_report_ids = set(request.claim.resolved_report_ids)
        channel_report_ids = {report.report_id for report in channel_reports}
        if submitted_report_ids != channel_report_ids:
            raise CollaborationConflict(
                "claim_report_accounting_incomplete",
                "claim must account for every collaboration report in this channel",
            )
        resolved_statuses = {
            ReportCategory.task_spec_gap: {
                ReportStatus.lilies_rechecks,
                ReportStatus.independently_verified,
            },
            ReportCategory.environment_gap: {
                ReportStatus.lilies_health_checks,
                ReportStatus.independently_verified,
            },
            ReportCategory.platform_capability_gap: {
                ReportStatus.lilies_verified,
                ReportStatus.independently_verified,
                ReportStatus.rejected,
                ReportStatus.withdrawn,
            },
            ReportCategory.platform_defect_suspected: {
                ReportStatus.lilies_verified,
                ReportStatus.independently_verified,
                ReportStatus.rejected,
                ReportStatus.withdrawn,
            },
        }
        if any(
            report.status not in resolved_statuses[report.category] for report in channel_reports
        ):
            raise CollaborationConflict(
                "claim_report_unresolved",
                "claim cannot omit or complete an unresolved collaboration report",
            )
        claim = VerificationClaim.model_validate(
            {
                **request.claim.model_dump(mode="json", exclude_none=True),
                "channel_id": channel_id,
                "assignment_id": channel.assignment_id,
                "claim_revision": 1,
                "status": ClaimStatus.frozen,
                "created_at": self._now(),
                "invalidated_at": None,
                "invalidation_reason": None,
            }
        )
        await self._require_trusted_verification_claim(
            channel=channel,
            claim=claim,
        )
        message = self._message(
            channel_id=channel_id,
            message_type=MessageType.verification_claim,
            sender_role=SenderRole.lilies,
            sender_id=actor.sender_id,
            correlation_id=claim.claim_id,
            causal_parent_id=None,
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.verifier,
            payload_schema=PayloadSchema.verification_claim_v1,
            payload=claim,
            message_id=uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-verification-claim-message:{claim.claim_id}",
            ),
            client_request_digest=client_request_digest,
        )
        stored = await self.store.create_verification_claim(
            {
                **claim.model_dump(mode="json", exclude_none=True),
                "expected_channel_revision": request.expected_channel_revision,
                "idempotency_key": request.idempotency_key,
                "outbox": self._outbox_record(
                    channel_id=channel_id,
                    message_id=UUID(str(message["message_id"])),
                    destination="developer_inbox",
                    idempotency_key=request.idempotency_key,
                    payload={"claim_id": str(claim.claim_id)},
                ),
            },
            message,
        )
        self._notify_events(channel_id)
        return _as_dict(stored)

    async def prepare_formal_run_archive(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: Any,
    ) -> dict[str, Any]:
        """Prepare claim-bound evidence only through the platform-owned exporter."""

        actor = self._principal(principal)
        if actor.role is not SenderRole.lilies:
            raise CollaborationNotFound()
        channel = await self._channel(channel_id, principal=actor)
        idempotency_key = str(getattr(request, "idempotency_key", ""))
        client_request_digest = self._client_request_digest(
            "formal_archive.prepare",
            request,
            channel_id=channel_id,
            assignment_id=channel.assignment_id,
        )
        replay = await self._operation_replay(
            operation="formal_archive.prepare",
            scope_id=channel_id,
            actor=actor,
            idempotency_key=idempotency_key,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            projected = replay
        else:
            self._require_open(channel)
            expected_revision = getattr(request, "expected_channel_revision", None)
            if expected_revision != channel.revision:
                raise CollaborationConflict(
                    "channel_revision_conflict",
                    "channel changed before formal archive preparation",
                )
            provider = self._formal_archive_provider
            if provider is None:
                raise CollaborationConflict(
                    "formal_archive_unavailable",
                    "the trusted formal run archiver is unavailable",
                )
            try:
                result = provider(channel, request, actor.sender_id)
                if inspect.isawaitable(result):
                    result = await result
                projected = _as_dict(result)
            except CollaborationConflict:
                raise
            except Exception as error:
                raise CollaborationConflict(
                    "formal_archive_rejected",
                    "the durable formal run could not be archived",
                ) from error
        if (
            str(projected.get("assignment_id")) != str(channel.assignment_id)
            or str(projected.get("channel_id")) != str(channel.channel_id)
            or str(projected.get("task_id")) != channel.task_id
            or int(projected.get("revision", -1)) != channel.task_revision
        ):
            raise CollaborationConflict(
                "formal_archive_binding_mismatch",
                "the formal archive result changed its channel binding",
            )
        if replay is not None:
            return projected
        persist_receipt = getattr(self.store, "record_operation_receipt", None)
        if not callable(persist_receipt):
            raise CollaborationConflict(
                "formal_archive_unavailable",
                "the trusted formal archive receipt store is unavailable",
            )
        stored = await persist_receipt(
            operation="formal_archive.prepare",
            scope_id=channel_id,
            actor_role=actor.role.value,
            actor_id=actor.sender_id,
            idempotency_key=idempotency_key,
            request_digest=client_request_digest,
            response_kind="formal_run_archive",
            response=projected,
        )
        return _as_dict(stored)

    async def finalize_formal_archive_claim(
        self,
        *,
        channel_id: UUID,
        actor_id: str,
        archive_request: Any,
        archive_result: Any,
    ) -> dict[str, Any]:
        """Freeze the platform-authored archive claim without another daemon turn."""

        projected = _as_dict(archive_result)
        claim_payload = projected.get("verification_claim")
        if not isinstance(claim_payload, Mapping):
            raise CollaborationConflict(
                "formal_archive_claim_invalid",
                "the formal archive did not produce a verification claim",
            )
        channel = await self._channel(channel_id)
        if (
            str(projected.get("assignment_id")) != str(channel.assignment_id)
            or str(projected.get("channel_id")) != str(channel.channel_id)
            or str(claim_payload.get("claim_id"))
            != str(getattr(archive_request, "claim_id", ""))
        ):
            raise CollaborationConflict(
                "formal_archive_claim_binding_mismatch",
                "the archived verification claim changed its frozen assignment binding",
            )
        principal = CollaborationPrincipal(
            role=SenderRole.lilies,
            sender_id=actor_id,
            scopes=frozenset({CollaborationScope.report_write.value}),
            channel_id=channel.channel_id,
            assignment_id=channel.assignment_id,
        )
        claim_id = UUID(str(claim_payload["claim_id"]))
        return await self.submit_verification_claim(
            principal=principal,
            channel_id=channel.channel_id,
            request=VerificationClaimRequest(
                idempotency_key=f"formal.archive.claim.{claim_id.hex}",
                expected_channel_revision=int(
                    getattr(archive_request, "expected_channel_revision")
                ),
                claim=claim_payload,
            ),
        )

    async def invalidate_claims_for_draft(
        self,
        *,
        application_id: UUID,
        assignment_id: UUID | None = None,
        current_draft_revision: int,
        current_content_hash: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        """Workflow mutation hook: independently invalidate stale frozen claims."""

        self.require_enabled()
        records = await self.store.invalidate_verification_claims(
            application_id=application_id,
            assignment_id=assignment_id,
            current_draft_revision=current_draft_revision,
            current_content_hash=current_content_hash,
            reason=reason,
            now=self._now(),
        )
        return [_as_dict(item) for item in _as_list(records)]

    @_replay_receipt_after_conflict(
        receipt_operation="verification.result",
        digest_operation="verification.result",
        scope_parameter="claim_id",
        binding_parameters=("claim_id",),
    )
    async def submit_verification_result(
        self,
        *,
        principal: Any,
        claim_id: UUID,
        request: VerificationResultRequest,
    ) -> dict[str, Any]:
        actor = self._principal(principal)
        if actor.role is not SenderRole.verifier:
            raise CollaborationNotFound()
        claim = VerificationClaim.model_validate(await self.store.get_claim(claim_id))
        if actor.channel_id != claim.channel_id:
            raise CollaborationNotFound()
        client_request_digest = self._client_request_digest(
            "verification.result", request, claim_id=claim_id
        )
        operation_replay = await self._operation_replay(
            operation="verification.result",
            scope_id=claim_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            client_request_digest=client_request_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = await self._message_replay(
            channel_id=claim.channel_id,
            actor=actor,
            idempotency_key=request.idempotency_key,
            payload_schema=PayloadSchema.verification_result_v1,
            correlation_id=claim_id,
            client_request_digest=client_request_digest,
        )
        if replay is not None:
            persisted = VerificationResult.model_validate(replay.payload)
            return await self._persisted_replay(
                "get_verification", persisted.verification_id, replay.payload
            )
        channel = await self._channel(claim.channel_id, principal=actor)
        # This is the sole post-close domain mutation.  The claim must already
        # have existed and still be a non-invalidated frozen revision.
        if channel.status is ChannelStatus.archived:
            raise CollaborationClosed()
        if claim.status is not ClaimStatus.frozen:
            raise CollaborationConflict(
                "claim_not_verifiable", "claim is invalidated or already verified"
            )
        if claim.claim_revision != request.expected_claim_revision:
            raise CollaborationConflict(
                "claim_revision_conflict", "claim changed before independent verification"
            )
        current_draft = await self._current_draft(claim.application_id)
        if current_draft is not None and (
            int(current_draft.get("revision", -1)) != claim.draft_revision
            or _canonical_sha256(current_draft.get("content_hash"))
            != _canonical_sha256(claim.content_hash)
        ):
            await self.invalidate_claims_for_draft(
                application_id=claim.application_id,
                assignment_id=claim.assignment_id,
                current_draft_revision=int(current_draft.get("revision", -1)),
                current_content_hash=str(current_draft.get("content_hash")),
                reason="draft revision or content changed before independent verification",
            )
            raise CollaborationConflict(
                "claim_invalidated",
                "claim draft changed before independent verification",
            )
        result = VerificationResult.model_validate(
            {
                **request.result.model_dump(mode="json", exclude_none=True),
                "channel_id": claim.channel_id,
                "claim_id": claim_id,
                "claim_revision": claim.claim_revision,
                "verifier_id": actor.sender_id,
                "created_at": self._now(),
            }
        )
        await self._require_trusted_verification_result(
            channel=channel,
            claim=claim,
            result=result,
        )
        next_status = (
            ClaimStatus.independently_verified
            if result.verdict is VerificationVerdict.independently_verified
            else ClaimStatus.verification_failed
        )
        message = self._message(
            channel_id=claim.channel_id,
            message_type=MessageType.verification_result,
            sender_role=SenderRole.verifier,
            sender_id=actor.sender_id,
            correlation_id=claim_id,
            causal_parent_id=uuid5(
                NAMESPACE_URL,
                f"lilies:collaboration-verification-claim-message:{claim_id}",
            ),
            idempotency_key=request.idempotency_key,
            visibility=MessageVisibility.user_and_lilies,
            payload_schema=PayloadSchema.verification_result_v1,
            payload=result,
            client_request_digest=client_request_digest,
        )
        stored = await self.store.record_verification(
            {
                **result.model_dump(mode="json", exclude_none=True),
                "idempotency_key": request.idempotency_key,
                "next_claim_status": next_status.value,
            },
            message,
        )
        self._notify_events(claim.channel_id)
        return _as_dict(stored)
